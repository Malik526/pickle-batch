"""
postgres_content_store.py — PostgresContentStore: the Postgres-backed
implementation of this codebase's persistence boundary (Milestone 3.3).

What it does:
  Implements the same public method surface as persistence.content_store.
  ContentStore (the original, still-supported SQLite implementation — see
  persistence.protocol.ContentStoreProtocol for the shared contract both
  satisfy) against real Postgres, using psycopg (not an ORM — see
  docs/decisions/0008-postgres-persistence-migration.md "Driver"/"Persistence
  Architecture" for why raw SQL + two parallel concrete implementations was
  chosen over SQLAlchemy or a shared query-builder abstraction).

  Deliberately NOT a drop-in-identical twin of ContentStore in every
  respect — two real differences, both intentional (see the ADR):

    1. Ownership is NOT NULL here, not optional. videos/content_slots/
       platform_posts.user_id is a required parameter on every write method
       (no `= None` default) and required on every scoped selector, because
       the Postgres schema itself enforces NOT NULL (Milestone 3.2's
       nullable-user_id compromise was explicitly a SQLite-transition-only
       decision — see ADR-0007 and ADR-0008's "Ownership" section). There is
       no legacy/unscoped data in a fresh Postgres database to support a
       backward-compatible optional parameter for.
    2. assign_slot()'s ownership check has no "skip if either side is None"
       carve-out (SQLite's does, for legacy rows) — under Postgres, both
       sides are always non-null, so the check is unconditional.

  Every returned dataclass instance (VideoRecord, SlotRecord,
  PlatformPostRecord, UserRecord, AuthIdentityRecord, PlatformConnectionRecord)
  and every raised exception (SlotUnavailableError, OwnershipMismatchError)
  is imported directly from content_store.py, not redefined — they are pure
  data/exception types with no SQLite dependency, so there is exactly one
  definition of each, shared by both backends.

Dependencies:
  psycopg. content_automation.config (DATABASE_URL, POSTGRES_SCHEMA).
  content_automation.persistence.postgres_migrate (schema migrations).
  content_automation.persistence.content_store (shared dataclasses/exceptions).
"""

from contextlib import contextmanager
import dataclasses
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from content_automation.config import CREDENTIAL_REFRESH_LOCK_TIMEOUT_SECONDS, DATABASE_URL, POSTGRES_SCHEMA
from content_automation.persistence import postgres_migrate
from content_automation.persistence.content_store import (
    AuthIdentityRecord,
    CadenceRecord,
    CadenceTimeRecord,
    CredentialRefreshLockTimeout,
    OAuthStateRecord,
    OwnershipMismatchError,
    PlatformConnectionRecord,
    PlatformCredentialRecord,
    PlatformPostInProgressError,
    PlatformPostRecord,
    SlotRecord,
    SlotUnavailableError,
    UploadAttemptRecord,
    UploadBatchRecord,
    UserRecord,
    VideoRecord,
    _utc_now_iso,
)

LOCAL_BOOTSTRAP_USER_EMAIL = "local@pickle-batch.local"

# Every real column this store ever selects with SELECT * — listed
# explicitly (not relying on dict_row's natural column order) only where a
# dataclass's field order must match; dict_row already returns column-name
# keys, so **row unpacking into a dataclass works regardless of column
# order, same as content_store.py's _row_to_* helpers.


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert every datetime.datetime/datetime.date value in a returned row
    to an isoformat() string, matching how content_store.py's SQLite
    implementation always stores and returns these fields as TEXT.

    This is what makes Postgres's native TIMESTAMP/TIMESTAMPTZ columns
    (psycopg adapts these to real Python datetime objects) produce the exact
    same str-typed VideoRecord/SlotRecord/PlatformPostRecord fields the rest
    of this codebase already depends on (due_post_selector, worker,
    reconciliation, crash_recovery all call datetime.fromisoformat(...) or
    compare these fields as strings) — see
    docs/decisions/0008-postgres-persistence-migration.md "Timestamps".
    A TIMESTAMPTZ value's .isoformat() carries a real UTC offset (+00:00)
    because the connection's session time zone is set to UTC at connect
    time (see _connect below) — never left to the server/provider's
    default, which is not guaranteed to be UTC.
    """
    return {
        key: (value.isoformat() if isinstance(value, (datetime, date)) else value)
        for key, value in row.items()
    }


def _record(cls, row: dict[str, Any]):
    """Build a record from a SELECT * row, ignoring columns the dataclass
    doesn't declare (Milestone 4.2). Without this, an additive migration
    applied by a newer deployment (e.g. 0012's platform_posts.platform_media_id)
    made every read in still-running older code raise TypeError until it was
    redeployed. Only unknown extras are dropped; a missing column still fails."""
    known = _FIELD_NAMES.get(cls)
    if known is None:
        known = _FIELD_NAMES[cls] = frozenset(field.name for field in dataclasses.fields(cls))
    return cls(**{key: value for key, value in _normalize_row(row).items() if key in known})


_FIELD_NAMES: dict[type, frozenset[str]] = {}


def _row_to_video(row: dict) -> VideoRecord:
    return _record(VideoRecord, row)


def _row_to_slot(row: dict) -> SlotRecord:
    return _record(SlotRecord, row)


def _row_to_platform_post(row: dict) -> PlatformPostRecord:
    return _record(PlatformPostRecord, row)


def _row_to_user(row: dict) -> UserRecord:
    return _record(UserRecord, row)


def _row_to_auth_identity(row: dict) -> AuthIdentityRecord:
    return _record(AuthIdentityRecord, row)


def _row_to_platform_credential(row: dict) -> PlatformCredentialRecord:
    return _record(PlatformCredentialRecord, row)


def _row_to_oauth_state(row: dict) -> OAuthStateRecord:
    return _record(OAuthStateRecord, row)


def _row_to_platform_connection(row: dict) -> PlatformConnectionRecord:
    return _record(PlatformConnectionRecord, row)


def _row_to_upload_batch(row: dict) -> UploadBatchRecord:
    return _record(UploadBatchRecord, row)


def _row_to_upload_attempt(row: dict) -> UploadAttemptRecord:
    return _record(UploadAttemptRecord, row)


def _row_to_cadence_time(row: dict) -> CadenceTimeRecord:
    return _record(CadenceTimeRecord, row)


def _connect(dsn: str, schema: str) -> psycopg.Connection:
    """One real Postgres connection, session time zone forced to UTC (see
    _normalize_row's docstring for why), search_path set to `schema` so
    every unqualified table name in this module's SQL resolves there —
    letting the same SQL text serve both the real "public" schema and a
    disposable test schema with zero query changes. autocommit=True by
    default, matching content_store.py's SQLite connection
    (isolation_level=None) — each statement is its own implicit
    transaction unless explicitly wrapped in conn.transaction() (used by
    assign_slot() and postgres_migrate.apply_migrations(), the multi-
    statement operations that need atomicity — the same set of operations
    ContentStore.transaction()/BEGIN IMMEDIATE covers under SQLite)."""
    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    return conn


class PostgresContentStore:
    """Postgres-backed implementation of this codebase's persistence
    boundary. See module docstring for the two deliberate divergences from
    ContentStore (SQLite)."""

    def __init__(self, dsn: str = DATABASE_URL, schema: str = POSTGRES_SCHEMA):
        if not dsn:
            raise ValueError(
                "PostgresContentStore requires a real DATABASE_URL. "
                "Set it in .env, or use persistence.content_store.ContentStore (SQLite) instead — "
                "see persistence.store_factory.build_content_store() for the normal selection path."
            )
        self.dsn = dsn
        self.schema = schema
        self._conn = _connect(dsn, schema)
        postgres_migrate.apply_migrations(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PostgresContentStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- users / auth_identities / platform_connections ------------------

    def create_user(self, email: str, display_name: str | None, created_at: str) -> UserRecord:
        row = self._conn.execute(
            "INSERT INTO users (email, display_name, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s) RETURNING *",
            (email, display_name, created_at, created_at),
        ).fetchone()
        return _row_to_user(row)

    def get_user(self, user_id: int) -> UserRecord | None:
        row = self._conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        return _row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> UserRecord | None:
        row = self._conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        return _row_to_user(row) if row else None

    def get_or_create_local_user(self) -> UserRecord:
        existing = self.get_user_by_email(LOCAL_BOOTSTRAP_USER_EMAIL)
        if existing is not None:
            return existing
        now = _utc_now_iso()
        return self.create_user(LOCAL_BOOTSTRAP_USER_EMAIL, "Local Bootstrap User", now)

    def create_auth_identity(
        self, user_id: int, provider: str, provider_subject: str, provider_email: str | None, created_at: str,
    ) -> AuthIdentityRecord:
        row = self._conn.execute(
            "INSERT INTO auth_identities (user_id, provider, provider_subject, provider_email, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (user_id, provider, provider_subject, provider_email, created_at, created_at),
        ).fetchone()
        return _row_to_auth_identity(row)

    def get_user_by_auth_identity(self, provider: str, provider_subject: str) -> UserRecord | None:
        row = self._conn.execute(
            "SELECT users.* FROM users JOIN auth_identities ON auth_identities.user_id = users.id "
            "WHERE auth_identities.provider = %s AND auth_identities.provider_subject = %s",
            (provider, provider_subject),
        ).fetchone()
        return _row_to_user(row) if row else None

    def create_platform_connection(
        self, user_id: int, platform: str, external_account_id: str | None, status: str, created_at: str,
    ) -> PlatformConnectionRecord:
        row = self._conn.execute(
            "INSERT INTO platform_connections (user_id, platform, external_account_id, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (user_id, platform, external_account_id, status, created_at, created_at),
        ).fetchone()
        return _row_to_platform_connection(row)

    def get_platform_connection(self, user_id: int, platform: str) -> PlatformConnectionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM platform_connections WHERE user_id = %s AND platform = %s", (user_id, platform)
        ).fetchone()
        return _row_to_platform_connection(row) if row else None

    def get_or_create_platform_connection(
        self, user_id: int, platform: str, external_account_id: str | None = None,
    ) -> PlatformConnectionRecord:
        existing = self.get_platform_connection(user_id, platform)
        if existing is not None:
            return existing
        now = _utc_now_iso()
        return self.create_platform_connection(user_id, platform, external_account_id, "ACTIVE", now)

    def update_platform_connection_external_account(
        self, connection_id: int, external_account_id: str | None, updated_at: str,
    ) -> None:
        self._conn.execute(
            "UPDATE platform_connections SET external_account_id = %s, updated_at = %s WHERE id = %s",
            (external_account_id, updated_at, connection_id),
        )

    def update_platform_connection_status(self, connection_id: int, status: str, updated_at: str) -> None:
        self._conn.execute(
            "UPDATE platform_connections SET status = %s, updated_at = %s WHERE id = %s",
            (status, updated_at, connection_id),
        )

    # -- platform_credentials / oauth_states (Milestone 3.6) -------------

    def get_platform_credential(self, platform_connection_id: int) -> PlatformCredentialRecord | None:
        row = self._conn.execute(
            "SELECT * FROM platform_credentials WHERE platform_connection_id = %s", (platform_connection_id,)
        ).fetchone()
        return _row_to_platform_credential(row) if row else None

    def upsert_platform_credential(
        self, platform_connection_id: int, encrypted_payload: str, now: str,
    ) -> PlatformCredentialRecord:
        row = self._conn.execute(
            "INSERT INTO platform_credentials (platform_connection_id, encrypted_payload, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (platform_connection_id) DO UPDATE SET "
            "encrypted_payload = EXCLUDED.encrypted_payload, updated_at = EXCLUDED.updated_at "
            "RETURNING *",
            (platform_connection_id, encrypted_payload, now, now),
        ).fetchone()
        return _row_to_platform_credential(row)

    def update_platform_credential_if_unchanged(
        self, platform_connection_id: int, encrypted_payload: str, expected_updated_at: str, new_updated_at: str,
    ) -> bool:
        cur = self._conn.execute(
            "UPDATE platform_credentials SET encrypted_payload = %s, updated_at = %s "
            "WHERE platform_connection_id = %s AND updated_at = %s",
            (encrypted_payload, new_updated_at, platform_connection_id, expected_updated_at),
        )
        return cur.rowcount > 0

    @contextmanager
    def credential_refresh_lock(self, platform_connection_id: int):
        """Milestone 3.13: hold a transaction-scoped advisory lock keyed to
        (schema, platform_connection_id) for the duration of the block, so
        only one process at a time can refresh a given connection's TikTok
        token — the same pg_advisory_xact_lock(hashtext(...)) convention
        postgres_migrate uses, scoped per connection instead of per schema.
        Other connections (other users) are never blocked.

        Transaction-scoped, so it is released on commit, on rollback when
        the block raises (a failed refresh), and if this process dies
        (Postgres ends the session) — never leaked. Safe behind Supabase's
        transaction-pooling pooler for the same reason. Every statement the
        block runs on this store (re-reading the credential, the CAS write)
        joins the transaction. Waiting is bounded by lock_timeout:
        CredentialRefreshLockTimeout is raised instead of hanging a worker."""
        timeout_ms = int(CREDENTIAL_REFRESH_LOCK_TIMEOUT_SECONDS * 1000)
        with self._conn.transaction():
            self._conn.execute(f"SET LOCAL lock_timeout = {timeout_ms}")
            try:
                self._conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext("
                    "'content_automation.platform_credential_refresh:' || current_schema() || ':' || %s))",
                    (str(platform_connection_id),),
                )
            except psycopg.errors.LockNotAvailable as exc:
                raise CredentialRefreshLockTimeout(
                    f"platform_connection {platform_connection_id}: refresh lock not acquired within "
                    f"{CREDENTIAL_REFRESH_LOCK_TIMEOUT_SECONDS}s"
                ) from exc
            yield

    def delete_platform_credential(self, platform_connection_id: int) -> None:
        self._conn.execute(
            "DELETE FROM platform_credentials WHERE platform_connection_id = %s", (platform_connection_id,)
        )

    def create_oauth_state(
        self, user_id: int, platform: str, state: str, code_verifier: str, redirect_uri: str,
        created_at: str, expires_at: str, return_target: str | None = None,
    ) -> OAuthStateRecord:
        row = self._conn.execute(
            "INSERT INTO oauth_states "
            "(user_id, platform, state, code_verifier, redirect_uri, created_at, expires_at, return_target) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (user_id, platform, state, code_verifier, redirect_uri, created_at, expires_at, return_target),
        ).fetchone()
        return _row_to_oauth_state(row)

    def consume_oauth_state(self, state: str, now: str, *, platform: str) -> OAuthStateRecord | None:
        """Milestone 4.1: a single conditional UPDATE ... RETURNING, so
        the platform/replay/expiry checks and the consumption are one atomic
        statement. See ContentStore.consume_oauth_state."""
        row = self._conn.execute(
            "UPDATE oauth_states SET consumed_at = %s "
            "WHERE state = %s AND platform = %s AND consumed_at IS NULL AND expires_at > %s "
            "RETURNING *",
            (now, state, platform, now),
        ).fetchone()
        return _row_to_oauth_state(row) if row else None

    # -- upload_batches / upload_attempts (Milestone 3.7 follow-up) --------
    # See ContentStore's identical methods for the shared contract/docstrings.

    def create_upload_batch(self, user_id: int, started_at: str, file_count: int) -> UploadBatchRecord:
        row = self._conn.execute(
            "INSERT INTO upload_batches (user_id, started_at, file_count) VALUES (%s, %s, %s) RETURNING *",
            (user_id, started_at, file_count),
        ).fetchone()
        return _row_to_upload_batch(row)

    def update_upload_batch(self, batch_id: int, **fields) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = %s" for key in fields)
        values = [*fields.values(), batch_id]
        self._conn.execute(f"UPDATE upload_batches SET {columns} WHERE id = %s", values)

    def list_upload_batches_for_user(self, user_id: int) -> list[UploadBatchRecord]:
        rows = self._conn.execute(
            "SELECT * FROM upload_batches WHERE user_id = %s ORDER BY started_at DESC, id DESC", (user_id,)
        ).fetchall()
        return [_row_to_upload_batch(row) for row in rows]

    def create_upload_attempt(
        self, batch_id: int, user_id: int, original_filename: str, started_at: str,
    ) -> UploadAttemptRecord:
        row = self._conn.execute(
            "INSERT INTO upload_attempts (batch_id, user_id, original_filename, started_at) "
            "VALUES (%s, %s, %s, %s) RETURNING *",
            (batch_id, user_id, original_filename, started_at),
        ).fetchone()
        return _row_to_upload_attempt(row)

    def update_upload_attempt(self, attempt_id: int, **fields) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = %s" for key in fields)
        values = [*fields.values(), attempt_id]
        self._conn.execute(f"UPDATE upload_attempts SET {columns} WHERE id = %s", values)

    def get_upload_attempts_for_batch(self, batch_id: int) -> list[UploadAttemptRecord]:
        rows = self._conn.execute(
            "SELECT * FROM upload_attempts WHERE batch_id = %s ORDER BY id ASC", (batch_id,)
        ).fetchall()
        return [_row_to_upload_attempt(row) for row in rows]

    # -- videos ------------------------------------------------------------

    def get_video_by_hash(self, file_hash: str) -> VideoRecord | None:
        row = self._conn.execute("SELECT * FROM videos WHERE file_hash = %s", (file_hash,)).fetchone()
        return _row_to_video(row) if row else None

    def get_video(self, video_id: int) -> VideoRecord | None:
        row = self._conn.execute("SELECT * FROM videos WHERE id = %s", (video_id,)).fetchone()
        return _row_to_video(row) if row else None

    def get_video_by_path(self, original_path: str) -> VideoRecord | None:
        row = self._conn.execute("SELECT * FROM videos WHERE original_path = %s", (original_path,)).fetchone()
        return _row_to_video(row) if row else None

    def insert_video(
        self, file_hash: str, original_filename: str, original_path: str, created_at: str, user_id: int,
    ) -> VideoRecord:
        """user_id is required (not optional) — see module docstring."""
        row = self._conn.execute(
            "INSERT INTO videos (file_hash, original_filename, original_path, status, created_at, user_id) "
            "VALUES (%s, %s, %s, 'DISCOVERED', %s, %s) RETURNING *",
            (file_hash, original_filename, original_path, created_at, user_id),
        ).fetchone()
        return _row_to_video(row)

    def update_video(self, video_id: int, **fields) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = %s" for key in fields)
        values = [*fields.values(), video_id]
        self._conn.execute(f"UPDATE videos SET {columns} WHERE id = %s", values)

    def set_video_caption(
        self, video_id: int, caption_text: str | None, caption_source: str | None, hashtags: list[str],
    ) -> None:
        """See ContentStore.set_video_caption — identical contract."""
        with self._conn.transaction():
            self._conn.execute(
                "UPDATE videos SET caption_text = %s, caption_source = %s WHERE id = %s",
                (caption_text, caption_source, video_id),
            )
            self._conn.execute("DELETE FROM video_hashtags WHERE video_id = %s", (video_id,))
            if hashtags:
                with self._conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO video_hashtags (video_id, position, hashtag) VALUES (%s, %s, %s)",
                        [(video_id, position, tag) for position, tag in enumerate(hashtags)],
                    )

    def list_video_hashtags(self, video_id: int) -> list[str]:
        """See ContentStore.list_video_hashtags — identical contract."""
        rows = self._conn.execute(
            "SELECT hashtag FROM video_hashtags WHERE video_id = %s ORDER BY position", (video_id,)
        ).fetchall()
        return [row["hashtag"] for row in rows]

    def list_videos_for_user(self, user_id: int) -> list[VideoRecord]:
        """See ContentStore.list_videos_for_user — identical contract."""
        rows = self._conn.execute(
            "SELECT * FROM videos WHERE user_id = %s ORDER BY created_at DESC, id DESC", (user_id,)
        ).fetchall()
        return [_row_to_video(row) for row in rows]

    # -- content_slots -------------------------------------------------------

    def insert_slot_if_missing(
        self,
        scheduled_at: str,
        pillar_key: str | None,
        prompt: str | None,
        created_at: str,
        user_id: int,
        google_calendar_event_id: str | None = None,
        timezone: str | None = None,
        cadence_id: int | None = None,
    ) -> bool:
        """user_id is required — see module docstring. ON CONFLICT target
        widened to (user_id, scheduled_at) — Milestone 3.8, see
        content_store.py's _migrate_content_slots_to_per_user_uniqueness
        for why the old (scheduled_at) alone constraint had to change."""
        cur = self._conn.execute(
            "INSERT INTO content_slots "
            "(scheduled_at, pillar_key, prompt, status, google_calendar_event_id, created_at, user_id, timezone, cadence_id) "
            "VALUES (%s, %s, %s, 'OPEN', %s, %s, %s, %s, %s) ON CONFLICT (user_id, scheduled_at) DO NOTHING",
            (scheduled_at, pillar_key, prompt, google_calendar_event_id, created_at, user_id, timezone, cadence_id),
        )
        return cur.rowcount > 0

    def get_slot(self, slot_id: int) -> SlotRecord | None:
        row = self._conn.execute("SELECT * FROM content_slots WHERE id = %s", (slot_id,)).fetchone()
        return _row_to_slot(row) if row else None

    def find_earliest_open_slot(self, pillar_key: str, after_iso: str, user_id: int) -> SlotRecord | None:
        """user_id is required — see module docstring. No unscoped/legacy
        fallback exists under Postgres (unlike ContentStore's SQLite
        version), so this only ever matches that user's own slots."""
        row = self._conn.execute(
            "SELECT * FROM content_slots WHERE pillar_key = %s AND status = 'OPEN' AND scheduled_at > %s "
            "AND user_id = %s ORDER BY scheduled_at ASC LIMIT 1",
            (pillar_key, after_iso, user_id),
        ).fetchone()
        return _row_to_slot(row) if row else None

    def find_earliest_open_slot_fifo(self, after_iso: str, user_id: int) -> SlotRecord | None:
        row = self._conn.execute(
            "SELECT * FROM content_slots WHERE status = 'OPEN' AND scheduled_at >= %s AND user_id = %s "
            "ORDER BY scheduled_at ASC LIMIT 1",
            (after_iso, user_id),
        ).fetchone()
        return _row_to_slot(row) if row else None

    def list_slots_by_status(self, statuses: list[str]) -> list[SlotRecord]:
        rows = self._conn.execute(
            "SELECT * FROM content_slots WHERE status = ANY(%s) ORDER BY scheduled_at", (statuses,)
        ).fetchall()
        return [_row_to_slot(row) for row in rows]

    def delete_slot(self, slot_id: int) -> None:
        self._conn.execute("DELETE FROM content_slots WHERE id = %s", (slot_id,))

    def unassign_video_for_slot(self, slot_id: int) -> None:
        self._conn.execute(
            "UPDATE videos SET assigned_slot_id = NULL, status = 'CLASSIFIED', processed_at = NULL "
            "WHERE assigned_slot_id = %s",
            (slot_id,),
        )

    def assign_slot(self, video_id: int, slot_id: int) -> None:
        """Atomically claim a slot for a video. Raises SlotUnavailableError
        if the slot is no longer OPEN, or OwnershipMismatchError if the
        video and slot belong to different users — unconditional under
        Postgres (both sides are always non-null; contrast
        ContentStore.assign_slot's SQLite version, which skips the check
        when either side is legacy/unscoped)."""
        with self._conn.transaction():
            slot_row = self._conn.execute(
                "SELECT status, user_id FROM content_slots WHERE id = %s", (slot_id,)
            ).fetchone()
            if slot_row is None or slot_row["status"] != "OPEN":
                raise SlotUnavailableError(f"content_slot {slot_id} is not OPEN")

            video_row = self._conn.execute("SELECT user_id FROM videos WHERE id = %s", (video_id,)).fetchone()
            video_user_id = video_row["user_id"] if video_row else None
            slot_user_id = slot_row["user_id"]
            if video_user_id != slot_user_id:
                raise OwnershipMismatchError(
                    f"video {video_id} (user_id={video_user_id}) cannot be assigned to "
                    f"content_slot {slot_id} (user_id={slot_user_id}) — different owners."
                )

            self._conn.execute(
                "UPDATE content_slots SET status = 'ASSIGNED', assigned_video_id = %s WHERE id = %s",
                (video_id, slot_id),
            )
            self._conn.execute("UPDATE videos SET assigned_slot_id = %s WHERE id = %s", (slot_id, video_id))

    def unassign_slot(self, slot_id: int) -> None:
        """See ContentStore.unassign_slot — identical contract (reverse an
        assign_slot() claim, deleting only a still-PENDING platform_posts
        row; refuses via PlatformPostInProgressError if a real publish
        attempt already exists)."""
        with self._conn.transaction():
            slot_row = self._conn.execute(
                "SELECT status, assigned_video_id FROM content_slots WHERE id = %s", (slot_id,)
            ).fetchone()
            if slot_row is None or slot_row["status"] != "ASSIGNED" or slot_row["assigned_video_id"] is None:
                raise SlotUnavailableError(f"content_slot {slot_id} is not currently assigned")
            video_id = slot_row["assigned_video_id"]

            post_rows = self._conn.execute(
                "SELECT status FROM platform_posts WHERE video_id = %s", (video_id,)
            ).fetchall()
            if any(row["status"] != "PENDING" for row in post_rows):
                raise PlatformPostInProgressError(
                    f"video {video_id} has a platform post that is already publishing, published, "
                    "or failed — remove it from schedule is refused to preserve that record."
                )

            self._conn.execute("DELETE FROM platform_posts WHERE video_id = %s AND status = 'PENDING'", (video_id,))
            self._conn.execute(
                "UPDATE content_slots SET status = 'OPEN', assigned_video_id = NULL WHERE id = %s", (slot_id,)
            )
            self._conn.execute("UPDATE videos SET assigned_slot_id = NULL WHERE id = %s", (video_id,))

    def list_content_slots_for_user(self, user_id: int, from_iso: str, to_iso: str) -> list[SlotRecord]:
        """See ContentStore.list_content_slots_for_user — identical
        contract. No unowned-fallback case exists under Postgres."""
        rows = self._conn.execute(
            "SELECT * FROM content_slots WHERE user_id = %s AND scheduled_at >= %s AND scheduled_at < %s "
            "ORDER BY scheduled_at ASC",
            (user_id, from_iso, to_iso),
        ).fetchall()
        return [_row_to_slot(row) for row in rows]

    # -- posting_cadences (Milestone 3.8) -------------------------------------

    def get_cadence_for_user(self, user_id: int) -> CadenceRecord | None:
        row = self._conn.execute("SELECT * FROM posting_cadences WHERE user_id = %s", (user_id,)).fetchone()
        if row is None:
            return None
        time_rows = self._conn.execute(
            "SELECT * FROM posting_cadence_times WHERE cadence_id = %s ORDER BY weekday, posting_time",
            (row["id"],),
        ).fetchall()
        normalized = _normalize_row(row)
        return CadenceRecord(
            id=normalized["id"], user_id=normalized["user_id"], timezone=normalized["timezone"],
            is_active=normalized["is_active"], created_at=normalized["created_at"], updated_at=normalized["updated_at"],
            posting_times=[_row_to_cadence_time(t) for t in time_rows],
        )

    def save_cadence_and_regenerate_slots(
        self,
        user_id: int,
        timezone: str,
        is_active: bool,
        posting_times: list[tuple[str, str]],
        generated_slots: list[tuple[str, str]],
        now_utc_iso: str,
        now_local_iso: str,
    ) -> CadenceRecord:
        """See ContentStore.save_cadence_and_regenerate_slots — identical
        contract and identical reasoning for the two distinct "now" values
        (now_utc_iso for created_at/updated_at, now_local_iso as the
        naive-local reconciliation boundary against scheduled_at). user_id
        is required, no unowned/legacy carve-out, matching every other
        write method in this class."""
        with self._conn.transaction():
            existing = self._conn.execute(
                "SELECT id FROM posting_cadences WHERE user_id = %s", (user_id,)
            ).fetchone()
            if existing is None:
                cadence_id = self._conn.execute(
                    "INSERT INTO posting_cadences (user_id, timezone, is_active, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (user_id, timezone, is_active, now_utc_iso, now_utc_iso),
                ).fetchone()["id"]
            else:
                cadence_id = existing["id"]
                self._conn.execute(
                    "UPDATE posting_cadences SET timezone = %s, is_active = %s, updated_at = %s WHERE id = %s",
                    (timezone, is_active, now_utc_iso, cadence_id),
                )

            self._conn.execute("DELETE FROM posting_cadence_times WHERE cadence_id = %s", (cadence_id,))
            for weekday, posting_time in posting_times:
                self._conn.execute(
                    "INSERT INTO posting_cadence_times (cadence_id, weekday, posting_time) VALUES (%s, %s, %s)",
                    (cadence_id, weekday, posting_time),
                )

            self._conn.execute(
                "DELETE FROM content_slots "
                "WHERE user_id = %s AND cadence_id = %s AND status = 'OPEN' AND scheduled_at > %s",
                (user_id, cadence_id, now_local_iso),
            )

            for scheduled_at, slot_timezone in generated_slots:
                self._conn.execute(
                    "INSERT INTO content_slots "
                    "(scheduled_at, pillar_key, prompt, status, created_at, user_id, timezone, cadence_id) "
                    "VALUES (%s, NULL, NULL, 'OPEN', %s, %s, %s, %s) "
                    "ON CONFLICT (user_id, scheduled_at) DO NOTHING",
                    (scheduled_at, now_utc_iso, user_id, slot_timezone, cadence_id),
                )

        return self.get_cadence_for_user(user_id)

    # -- platform_posts ------------------------------------------------------

    def get_platform_post(self, video_id: int, platform: str) -> PlatformPostRecord | None:
        row = self._conn.execute(
            "SELECT * FROM platform_posts WHERE video_id = %s AND platform = %s", (video_id, platform)
        ).fetchone()
        return _row_to_platform_post(row) if row else None

    def list_platform_posts_for_video(self, video_id: int) -> list[PlatformPostRecord]:
        """See ContentStore.list_platform_posts_for_video — identical contract."""
        rows = self._conn.execute(
            "SELECT * FROM platform_posts WHERE video_id = %s ORDER BY id ASC", (video_id,)
        ).fetchall()
        return [_row_to_platform_post(row) for row in rows]

    def insert_platform_post(
        self, video_id: int, platform: str, created_at: str, user_id: int, scheduled_at: str | None = None,
    ) -> PlatformPostRecord:
        row = self._conn.execute(
            "INSERT INTO platform_posts (video_id, platform, status, scheduled_at, created_at, updated_at, user_id) "
            "VALUES (%s, %s, 'PENDING', %s, %s, %s, %s) RETURNING *",
            (video_id, platform, scheduled_at, created_at, created_at, user_id),
        ).fetchone()
        return _row_to_platform_post(row)

    def insert_platform_post_if_missing(
        self, video_id: int, platform: str, scheduled_at: str, created_at: str, user_id: int,
    ) -> bool:
        cur = self._conn.execute(
            "INSERT INTO platform_posts (video_id, platform, status, scheduled_at, created_at, updated_at, user_id) "
            "VALUES (%s, %s, 'PENDING', %s, %s, %s, %s) ON CONFLICT (video_id, platform) DO NOTHING",
            (video_id, platform, scheduled_at, created_at, created_at, user_id),
        )
        return cur.rowcount > 0

    def update_platform_post(self, post_id: int, updated_at: str, **fields) -> None:
        fields = {**fields, "updated_at": updated_at}
        columns = ", ".join(f"{key} = %s" for key in fields)
        values = [*fields.values(), post_id]
        self._conn.execute(f"UPDATE platform_posts SET {columns} WHERE id = %s", values)

    def claim_platform_post(self, post_id: int, updated_at: str, user_id: int) -> bool:
        """user_id is required — see module docstring. Same atomic
        conditional-UPDATE contract as ContentStore.claim_platform_post
        (SQLite): success read from rowcount, never raises merely because
        another claimant (same-tenant) or a cross-tenant caller loses."""
        cur = self._conn.execute(
            "UPDATE platform_posts SET status = 'PUBLISHING', updated_at = %s "
            "WHERE id = %s AND status = 'PENDING' AND user_id = %s",
            (updated_at, post_id, user_id),
        )
        return cur.rowcount > 0

    def list_hosted_user_ids_with_platform_work(self, platform: str) -> list[int]:
        """User ids (ascending) that have PENDING or PUBLISHING platform_posts
        for `platform` AND a real hosted login (an auth_identities row) —
        the hosted worker's per-cycle work list (Milestone 3.12). The local
        CLI bootstrap identity has no auth identity, so its rows stay owned
        by the CLI worker and are never touched by the hosted one."""
        rows = self._conn.execute(
            "SELECT DISTINCT p.user_id FROM platform_posts p WHERE p.platform = %s "
            "AND p.status IN ('PENDING', 'PUBLISHING') AND p.user_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM auth_identities a WHERE a.user_id = p.user_id) ORDER BY p.user_id",
            (platform,),
        ).fetchall()
        return [row["user_id"] for row in rows]

    def get_due_platform_posts(
        self, platform: str, now_iso: str, eligible_statuses: list[str], user_id: int
    ) -> list[PlatformPostRecord]:
        rows = self._conn.execute(
            "SELECT * FROM platform_posts WHERE platform = %s AND scheduled_at IS NOT NULL "
            "AND scheduled_at <= %s AND (next_retry_at IS NULL OR next_retry_at <= %s) "
            "AND status = ANY(%s) AND user_id = %s ORDER BY scheduled_at ASC, id ASC",
            (platform, now_iso, now_iso, eligible_statuses, user_id),
        ).fetchall()
        return [_row_to_platform_post(row) for row in rows]

    def get_recoverable_platform_posts(
        self, platform: str, stale_before_iso: str, user_id: int
    ) -> list[PlatformPostRecord]:
        rows = self._conn.execute(
            "SELECT * FROM platform_posts WHERE platform = %s AND status = 'PUBLISHING' AND updated_at < %s "
            "AND user_id = %s ORDER BY updated_at ASC, id ASC",
            (platform, stale_before_iso, user_id),
        ).fetchall()
        return [_row_to_platform_post(row) for row in rows]

    def get_reconcilable_platform_posts(self, platform: str, now_iso: str, user_id: int) -> list[PlatformPostRecord]:
        rows = self._conn.execute(
            "SELECT * FROM platform_posts WHERE platform = %s AND status = 'PUBLISHING' "
            "AND platform_post_id IS NOT NULL AND (next_status_check_at IS NULL OR next_status_check_at <= %s) "
            "AND user_id = %s ORDER BY next_status_check_at ASC, id ASC",
            (platform, now_iso, user_id),
        ).fetchall()
        return [_row_to_platform_post(row) for row in rows]

    def update_platform_post_if_unchanged(
        self, post_id: int, expected_updated_at: str, updated_at: str, user_id: int, **fields
    ) -> bool:
        fields = {**fields, "updated_at": updated_at}
        columns = ", ".join(f"{key} = %s" for key in fields)
        values = [*fields.values(), post_id, expected_updated_at, user_id]
        cur = self._conn.execute(
            f"UPDATE platform_posts SET {columns} WHERE id = %s AND updated_at = %s AND user_id = %s", values
        )
        return cur.rowcount > 0

    # -- videos (delete) -----------------------------------------------------

    def delete_video(self, video_id: int) -> None:
        """See ContentStore.delete_video — identical contract (null out
        upload_attempts.video_id, then delete the videos row)."""
        with self._conn.transaction():
            self._conn.execute("UPDATE upload_attempts SET video_id = NULL WHERE video_id = %s", (video_id,))
            self._conn.execute("DELETE FROM video_hashtags WHERE video_id = %s", (video_id,))
            self._conn.execute("DELETE FROM videos WHERE id = %s", (video_id,))
