"""
content_store.py — SQLite persistence for videos, content_slots, and
platform_posts.

What it does:
  Owns the three tables that make the content-processing/publishing
  pipeline restart-safe and idempotent: `videos` (one row per ingested
  file or hosted upload), `content_slots` (one row per calendar
  posting slot, written by generate_calendar.py and consumed by
  process_content.py / slot_matcher.py), and `platform_posts` (one row per
  video-platform publishing attempt, written/read by publish_tiktok.py —
  see docs/decisions/0006-tiktok-publisher-foundation.md). Each owns a
  distinct concern: videos is canonical content/media metadata,
  content_slots is scheduling assignment, platform_posts is external
  publishing state/result — never cram one concern's state into another
  table's columns.

  videos.id (not file_hash) is the real record identity — see Milestone
  3.7's re-upload-architecture follow-up. file_hash is a content
  fingerprint (indexed, not unique): local ingestion (media/processing.py)
  still uses get_video_by_hash to resume/dedup a physically-rediscovered
  file, which stays correct because that code path only ever inserts at
  most one row per hash itself; the hosted upload path
  (media.media_storage.create_video_from_upload) now always inserts a new
  row per upload, on the reasoning that a creator legitimately re-uploading
  the exact same bytes later (new caption/schedule/campaign) is a distinct
  record, not a duplicate to collapse. _videos_file_hash_needs_migration()/
  _migrate_videos_drop_file_hash_uniqueness() below drop the old global
  UNIQUE constraint (present in every database created before this
  change) the first time such a database is opened.

  Google Calendar remains the source of truth for what gets posted when;
  content_slots is an internal mirror that lets process_content.py query and
  atomically claim open slots without re-deriving the schedule.

  content_slots.scheduled_at is unique on its own (not (scheduled_at,
  pillar_key)) — one posting opportunity is one slot, regardless of which
  pillar the strategy later assigns it. See
  docs/decisions/0002-configurable-cadence-and-weighted-pillar-allocation.md.
  _migrate_content_slots_unique_constraint() upgrades any database created
  under the old two-column constraint the first time it's opened. It runs
  with PRAGMA legacy_alter_table=ON so renaming content_slots during the
  rebuild never rewrites videos.assigned_slot_id's REFERENCES clause to the
  temporary table name — SQLite's enhanced ALTER TABLE RENAME behavior does
  exactly that by default, which is what corrupted a real database's schema
  before this guard existed. _repair_videos_assigned_slot_fk() detects and
  fixes that already-corrupted state (assigned_slot_id referencing anything
  other than content_slots) on databases that migrated before the guard was
  added, by rebuilding videos the same safe way.

Dependencies:
  stdlib sqlite3 only.
"""

import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from content_automation.config import DB_PATH


def _utc_now_iso() -> str:
    """Aware-UTC isoformat timestamp — the same convention every other
    aware-UTC write in this codebase uses (updated_at, published_at,
    next_status_check_at; see publish_tiktok._now_iso/worker._now_iso).
    Used only by the get-or-create helpers below, which are the one place
    ContentStore itself originates a timestamp rather than receiving one
    from a caller — every other write method still takes created_at/
    updated_at as an explicit parameter, unchanged."""
    return datetime.now(timezone.utc).isoformat()

SCHEMA_VIDEOS = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    original_path TEXT NOT NULL,
    canonical_media_path TEXT,
    container TEXT,
    video_codec TEXT,
    audio_codec TEXT,
    width INTEGER,
    height INTEGER,
    fps REAL,
    duration_seconds REAL,
    file_size_bytes INTEGER,
    transcript TEXT,
    transcript_language TEXT,
    transcription_status TEXT,
    classified_pillar TEXT,
    classification_confidence REAL,
    classification_reason TEXT,
    classification_second_score REAL,
    classification_margin REAL,
    classifier TEXT,
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    failure_reason TEXT,
    assigned_slot_id INTEGER REFERENCES content_slots(id),
    created_at TEXT NOT NULL,
    processed_at TEXT
);
"""

SCHEMA_CONTENT_SLOTS = """
CREATE TABLE IF NOT EXISTS content_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_at TEXT NOT NULL,
    pillar_key TEXT,
    prompt TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN',
    assigned_video_id INTEGER REFERENCES videos(id),
    google_calendar_event_id TEXT,
    created_at TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    timezone TEXT,
    cadence_id INTEGER REFERENCES posting_cadences(id),
    UNIQUE(user_id, scheduled_at)
);
"""

# Added Milestone 2.0 (TikTok Publisher Foundation — see
# docs/decisions/0006-tiktok-publisher-foundation.md). Deliberately its own
# table rather than columns on `videos`: `videos` is canonical content/media
# metadata, `content_slots` is scheduling assignment, `platform_posts` is
# external publishing state/result — one video can eventually have zero or
# more platform_posts rows (one per platform), so this could never be a
# 1:1 column addition to `videos` even for a single platform today.
# UNIQUE(video_id, platform) is the idempotency primitive: a video can have
# at most one publishing record per platform, so a repeated manual publish
# attempt must look that row up (get_platform_post) rather than ever being
# able to insert a second one.
SCHEMA_PLATFORM_POSTS = """
CREATE TABLE IF NOT EXISTS platform_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    platform TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    platform_post_id TEXT,
    scheduled_at TEXT,
    published_at TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(video_id, platform)
);
"""

# Added Milestone 3.2 (user authentication / ownership model — see
# docs/decisions/0007-user-ownership-model.md and
# docs/architecture/hosted-product-boundary.md). Three new tables,
# deliberately additive and independent of the existing schema:
#
#   users              — the canonical internal Pickle Batch identity.
#                         Never keyed by an external provider ID; external
#                         identities map onto it via auth_identities below.
#   auth_identities     — one row per (external auth provider, external
#                         subject) a user has signed in with. Kept separate
#                         from `users` from day one so a single Pickle Batch
#                         account can later have more than one linked
#                         provider (Google, Apple, email/magic-link)
#                         without a schema change.
#   platform_connections — one row per (user, publishing platform) —
#                         identity/status metadata only (e.g. TikTok
#                         open_id). Deliberately does NOT store credential
#                         secrets: the real TikTok access/refresh token
#                         stays exactly where it already lives
#                         (config.TIKTOK_TOKEN_PATH, a single local file) —
#                         moving it into this table, or into Postgres, is
#                         explicitly out of this milestone's scope. See
#                         "Current Local Credential Bridge" in the
#                         evaluation record for how the one real existing
#                         TikTok credential maps onto this model today.
#
# No user_id anywhere in this schema is enforced NOT NULL at the SQL level
# — SQLite cannot add a NOT NULL column with a FOREIGN KEY to an existing
# populated table without the same rename/rebuild dance already used for
# content_slots/videos above, and forcing that risk onto real production
# rows was judged not worth it for a nullable-by-transition column. The
# invariant is enforced at the application layer instead (every write path
# that matters is exercised with an explicit user_id; ContentStore's own
# ownership-consistency check in assign_slot() below is the one place two
# already-written rows' ownership is cross-checked) — see
# docs/architecture/hosted-product-boundary.md for why this is judged
# acceptable under SQLite and what changes under the Postgres migration.
SCHEMA_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SCHEMA_AUTH_IDENTITIES = """
CREATE TABLE IF NOT EXISTS auth_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL,
    provider_subject TEXT NOT NULL,
    provider_email TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, provider_subject)
);
"""

# UNIQUE(user_id, platform): one connection per (user, platform) for V1 —
# the same "one TikTok account" shape this deployment already has today,
# rescoped to per-user rather than per-deployment. Deliberately decided,
# not incidental: if a future product tier needs multiple accounts per
# platform per user, that is a new milestone's schema change, not an
# oversight here (see docs/decisions/0007-user-ownership-model.md).
SCHEMA_PLATFORM_CONNECTIONS = """
CREATE TABLE IF NOT EXISTS platform_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    platform TEXT NOT NULL,
    external_account_id TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, platform)
);
"""

# Added Milestone 3.6 (real authentication + hosted TikTok connection — see
# docs/decisions/0011-real-authentication-and-tiktok-connection.md).
# Deliberately its own table, not new columns on platform_connections:
# platform_connections is documented (above) to carry no credential
# secrets, and that boundary is preserved here rather than broken —
# identity/status stays in platform_connections, the actual encrypted
# access/refresh token pair lives only here. UNIQUE(platform_connection_id)
# — one credential per connection, matching platform_connections' own
# UNIQUE(user_id, platform). encrypted_payload is a Fernet ciphertext of the
# same token JSON shape publishing/tiktok/auth.py's save_token() already
# writes (access_token, refresh_token, access_token_expires_at,
# refresh_token_expires_at, open_id, scope) — see
# publishing/tiktok/credential_store.py. updated_at backs an optimistic-
# concurrency (CAS) refresh, the same update_platform_post_if_unchanged
# pattern already used elsewhere in this store, replacing tiktok_auth.py's
# fcntl-based lock for this hosted, multi-process-safe path specifically —
# the existing local-file/fcntl path for the CLI's own token is untouched.
SCHEMA_PLATFORM_CREDENTIALS = """
CREATE TABLE IF NOT EXISTS platform_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_connection_id INTEGER NOT NULL UNIQUE REFERENCES platform_connections(id),
    encrypted_payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Added Milestone 3.6. Server-side, DB-backed pending-OAuth-attempt state —
# the hosted equivalent of tiktok_auth.py's TIKTOK_PENDING_AUTH_PATH file,
# but user-bound (so a callback can only ever complete the flow it belongs
# to — see api/routes/platforms_tiktok.py) and usable across separate
# stateless API requests (connect and callback are two different HTTP
# requests, possibly handled by two different processes, unlike the local
# CLI's single long-lived process). `state` is UNIQUE so a raw duplicate
# insert is rejected outright; `consumed_at` (set exactly once, via an
# atomic CAS update — see consume_oauth_state()) makes replaying an
# already-used state a no-op rejection rather than a second successful
# completion, and `expires_at` bounds how long an abandoned attempt stays
# valid (config.OAUTH_STATE_TTL_SECONDS).
#
# Milestone 4.1: return_target (nullable) is where the callback sends the
# browser afterwards, chosen at connect time from a server-owned allowlist
# (api/oauth_return_targets.py). NULL for every TikTok row and every row
# created before 4.1 — those callers keep their fixed Settings redirect.
# Existing databases gain the column through _ensure_oauth_states_columns.
SCHEMA_OAUTH_STATES = """
CREATE TABLE IF NOT EXISTS oauth_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    platform TEXT NOT NULL,
    state TEXT NOT NULL UNIQUE,
    code_verifier TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    return_target TEXT
);
"""

# Milestone 3.7 follow-up (upload performance instrumentation). Deliberately
# event/attempt-level, not columns bolted onto `videos` — a video row
# describes durable content; a batch/attempt describes one transient
# upload *event*, and a single video could in principle be the target of
# more than one attempt (a retried duplicate upload — see
# media_storage.create_video_from_upload's idempotency). upload_attempts.
# video_id is nullable because an attempt can fail before any video row
# exists at all (unsupported file type), and — as of the 2026-09-27
# upload-failure-semantics follow-up — even a storage-layer failure whose
# video row *does* exist (marked status="FAILED") still isn't linked back
# here in this pass; see api/routes/videos.py's _process_one_upload for
# why that residual gap was left alone. duration_ms on both tables is
# server-side wall-clock only (time.monotonic() deltas at the point this
# process measures it) — see api/routes/videos.py's own docstring for
# exactly what span each duration covers and why it is never labeled
# "network latency" (this server never observes the client's actual
# upload transfer time; by the time a route handler runs, Starlette has
# already fully received the multipart body).
#
# attempted_bytes/successful_bytes/success_count/failure_count (also
# 2026-09-27): attempted_bytes replaces the old total_bytes name, which
# silently only ever summed *successful* files' sizes — misleading for any
# batch containing a real failure (see CHANGELOG's Milestone 3.7
# telemetry-accuracy entry). status stays IN_PROGRESS -> COMPLETED, a pure
# lifecycle field; success_count/failure_count carry the outcome
# explicitly instead of overloading status with a value like
# "PARTIAL_SUCCESS".
SCHEMA_UPLOAD_BATCHES = """
CREATE TABLE IF NOT EXISTS upload_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    file_count INTEGER NOT NULL,
    attempted_bytes INTEGER NOT NULL DEFAULT 0,
    successful_bytes INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER,
    failure_count INTEGER,
    total_duration_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'IN_PROGRESS'
);
"""

SCHEMA_UPLOAD_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS upload_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES upload_batches(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    video_id INTEGER REFERENCES videos(id),
    original_filename TEXT NOT NULL,
    file_size_bytes INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'IN_PROGRESS',
    error_code TEXT
);
"""

# Milestone 3.8 (hosted scheduling cadence configuration). One row per user
# for the cadence itself (a single active-or-inactive cadence per user, not
# multiple named cadences), a child table for its "weekday -> posting time"
# pairs — a plain relational shape rather than a JSON blob column, matching
# this codebase's existing preference throughout (no JSON columns exist
# anywhere else in this schema). weekday values match cadence.py's
# WEEKDAY_NAMES ("monday".."sunday") for consistency with the existing
# global cadence model, even though this is a separate, per-user one (see
# docs/decisions/0012-hosted-cadence-configuration.md for why a second,
# richer model was added instead of extending calendar/cadence.py in
# place). id is stable across edits — PUT /api/cadence updates this same
# row's timezone/is_active/updated_at rather than inserting a new one, so
# content_slots.cadence_id keeps meaning "this user's one cadence" across
# any number of edits, which the reconciliation logic below depends on.
SCHEMA_POSTING_CADENCES = """
CREATE TABLE IF NOT EXISTS posting_cadences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    timezone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SCHEMA_POSTING_CADENCE_TIMES = """
CREATE TABLE IF NOT EXISTS posting_cadence_times (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cadence_id INTEGER NOT NULL REFERENCES posting_cadences(id),
    weekday TEXT NOT NULL,
    posting_time TEXT NOT NULL,
    UNIQUE(cadence_id, weekday, posting_time)
);
"""

# Milestone 3.10.1 (caption hashtag metadata). One row per hashtag
# occurrence in a video's current caption_text, in caption order
# (media.hashtags.extract_hashtags) — derived data, never the publishing
# source of truth (caption_text is). Relational rather than a JSON column,
# same preference as posting_cadence_times above. Rewritten only through
# set_video_caption(), atomically with caption_text, so the two never drift.
SCHEMA_VIDEO_HASHTAGS = """
CREATE TABLE IF NOT EXISTS video_hashtags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    position INTEGER NOT NULL,
    hashtag TEXT NOT NULL,
    UNIQUE(video_id, position)
);
"""


def _migrate_upload_batches_rename_total_bytes(conn: sqlite3.Connection) -> None:
    """2026-09-27 upload-failure-semantics/telemetry-accuracy follow-up:
    total_bytes silently only ever summed *successful* files' sizes (a
    bug, not documented intent), misleading for any batch with a failure.
    Renamed to attempted_bytes (every file's measured size, success or
    fail); successful_bytes is a genuinely new column, added by
    _ensure_upload_batches_columns below. ALTER TABLE ... RENAME COLUMN is
    supported since SQLite 3.25 (2018) — no table rebuild needed since
    this changes a name, not a type or constraint. A no-op for a brand-new
    database, which is created with attempted_bytes directly (see
    SCHEMA_UPLOAD_BATCHES above) and never had total_bytes to rename."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(upload_batches)").fetchall()}
    if "total_bytes" in existing and "attempted_bytes" not in existing:
        conn.execute("ALTER TABLE upload_batches RENAME COLUMN total_bytes TO attempted_bytes")


_UPLOAD_BATCHES_MIGRATION_COLUMNS = {
    "successful_bytes": "INTEGER NOT NULL DEFAULT 0",
    "success_count": "INTEGER",
    "failure_count": "INTEGER",
}


def _ensure_upload_batches_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(upload_batches)").fetchall()}
    for column, sql_type in _UPLOAD_BATCHES_MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE upload_batches ADD COLUMN {column} {sql_type}")


# The one local bootstrap identity every pre-3.2 row (and every CLI
# invocation, until a real auth layer exists) is attributed to. Not a
# secret, not exposed externally — a fixed, documented anchor, exactly the
# role config.APP_CALENDAR_SUMMARY plays for the dedicated Calendar. See
# ContentStore.get_or_create_local_user().
LOCAL_BOOTSTRAP_USER_EMAIL = "local@pickle-batch.local"

_SLOT_STATUS_PRIORITY = {"OPEN": 0, "FAILED": 0, "ASSIGNED": 1, "PUBLISHED": 2}

# New nullable videos columns added for Milestone 1.2 (local embedding
# classification observability — see
# docs/decisions/0003-local-embedding-classification.md) and Milestone 1.3
# (first-class caption state — see
# docs/decisions/0005-fifo-baseline-and-optional-strategy-routing.md).
# Adding a nullable column is a simple ALTER TABLE, unlike the content_slots
# rebuild below.
_VIDEOS_MIGRATION_COLUMNS = {
    "classification_second_score": "REAL",
    "classification_margin": "REAL",
    "classifier": "TEXT",
    "caption_text": "TEXT",
    "caption_source": "TEXT",
    # Milestone 3.2 (ownership) — see SCHEMA_USERS' docstring above for why
    # this is nullable rather than NOT NULL at the SQL level.
    "user_id": "INTEGER REFERENCES users(id)",
    # Milestone 3.4 (object storage) — both NULL means "legacy/local-direct":
    # canonical_media_path is still the authoritative local filesystem path,
    # exactly as before this milestone. Both set means canonical_media_path
    # is no longer authoritative — storage_provider/storage_key are, and
    # media.media_storage.materialize_canonical_media() resolves bytes
    # through the configured StorageProtocol backend instead. See
    # docs/decisions/0009-object-storage-media-lifecycle.md "Canonical
    # Media Reference".
    "storage_provider": "TEXT",
    "storage_key": "TEXT",
}


def _ensure_videos_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    for column, sql_type in _VIDEOS_MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {column} {sql_type}")


# New platform_posts columns added for Milestone 2.1.6 (retry classification
# and backoff — see docs/evaluations/scheduling/milestone-2.1.6-retry-backoff.md).
# retry_count defaults to 0 for every existing row (a row from before this
# milestone has never been retried); next_retry_at stays NULL until a
# retryable failure schedules one. No last_error_code column — the existing
# failure_reason already carries enough for this milestone's needs (see the
# evaluation doc for why a separate structured column wasn't justified).
_PLATFORM_POSTS_MIGRATION_COLUMNS = {
    "retry_count": "INTEGER NOT NULL DEFAULT 0",
    "next_retry_at": "TEXT",
    # Milestone 2.1.10 (asynchronous publish reconciliation): distinct from
    # next_retry_at — next_retry_at gates re-*submitting* a not-yet-accepted
    # PENDING row; next_status_check_at gates re-*polling* a PUBLISHING row
    # TikTok has already accepted (platform_post_id set). Aware-UTC
    # isoformat, matching updated_at's convention (not scheduled_at's naive
    # local-time convention) — see reconciliation.py.
    "next_status_check_at": "TEXT",
    "status_check_count": "INTEGER NOT NULL DEFAULT 0",
    # Milestone 3.2 (ownership) — see SCHEMA_USERS' docstring above.
    "user_id": "INTEGER REFERENCES users(id)",
    # Milestone 3.11 (user-facing publish states): the structured
    # reason_code behind failure_reason (PublishError.reason_code, a local
    # precondition code, or the platform's own reported fail code).
    # Revisits 2.1.6's "no error-code column" call above: failure_reason is
    # raw exception text (sometimes embedding API response bodies) and
    # must never be shown to a user or parsed, so the user-facing failure
    # taxonomy (publishing/failure_taxonomy.py) keys off this instead.
    # NULL for every row written before 3.11.
    "failure_code": "TEXT",
    # Milestone 3.13 (reconciliation + recovery): a submission checkpoint,
    # written immediately before publisher.publish() and cleared once the
    # outcome is known (platform_post_id persisted, or a pre-submission
    # failure recorded). A stale PUBLISHING row with platform_post_id NULL
    # tells crash recovery which of three cases it is in:
    #   NULL                  — claimed, never reached the platform (requeue)
    #   AWAITING_PLATFORM_ID  — submission started by a publisher that hands
    #                           over its platform id before transferring any
    #                           media (TikTokPublisher); no id persisted means
    #                           no media was sent (bounded requeue)
    #   SUBMITTING            — any other publisher; the outcome is unknowable
    #                           (parked as UNKNOWN for manual recovery)
    # See scheduling/publish_tiktok.py "Submission checkpoint".
    "submission_state": "TEXT",
    # Aware UTC, when the most recent submission attempt began. Diagnostic.
    "submission_started_at": "TEXT",
    # Milestone 4.2: the platform's id for the PUBLISHED post when it differs
    # from the submission handle in platform_post_id. Instagram:
    # platform_post_id = the media container id (what status checks use and
    # what recovery needs); platform_media_id = the published Reel's media id
    # from media_publish. NULL until published, and also NULL if publication
    # was only confirmed from the container's status. TikTok leaves it NULL.
    "platform_media_id": "TEXT",
}


# Milestone 4.1: additive, nullable — see SCHEMA_OAUTH_STATES.
_OAUTH_STATES_MIGRATION_COLUMNS = {
    "return_target": "TEXT",
}


def _ensure_oauth_states_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(oauth_states)").fetchall()}
    for column, sql_type in _OAUTH_STATES_MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE oauth_states ADD COLUMN {column} {sql_type}")


def _ensure_platform_posts_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(platform_posts)").fetchall()}
    for column, sql_type in _PLATFORM_POSTS_MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE platform_posts ADD COLUMN {column} {sql_type}")


# Milestone 3.2 (ownership) — content_slots had no prior "_ensure_*_columns"
# helper (its only prior schema change was the rebuild-based unique-
# constraint migration above); this is its first additive nullable-column
# migration, following the exact same pattern as videos/platform_posts.
_CONTENT_SLOTS_MIGRATION_COLUMNS = {
    "user_id": "INTEGER REFERENCES users(id)",
    # Milestone 3.8 (hosted cadence configuration): timezone is stamped at
    # generation time (NULL for every legacy row, meaning "assume the
    # global config.TIMEZONE" — identical to today's actual behavior for
    # those rows). cadence_id is provenance — NULL means manual/legacy
    # (never touched by cadence-edit reconciliation), non-NULL means "this
    # exact posting_cadences row generated this slot".
    "timezone": "TEXT",
    "cadence_id": "INTEGER REFERENCES posting_cadences(id)",
}


def _ensure_content_slots_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(content_slots)").fetchall()}
    for column, sql_type in _CONTENT_SLOTS_MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE content_slots ADD COLUMN {column} {sql_type}")


def _content_slots_needs_migration(conn: sqlite3.Connection) -> bool:
    """True if content_slots was created under either pre-Milestone-1.3
    schema this rebuilds away from: the old UNIQUE(scheduled_at, pillar_key)
    constraint (Milestone 1.1), or a NOT NULL pillar_key (pre-Milestone 1.3 —
    FIFO slots need pillar_key nullable). Both are fixed by the same rebuild
    pass below, run at most once regardless of which (or both) applied."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'content_slots'"
    ).fetchone()
    sql = (row["sql"] or "") if row is not None else ""
    return "UNIQUE(scheduled_at, pillar_key)" in sql or "pillar_key TEXT NOT NULL" in sql


def _migrate_content_slots_unique_constraint(conn: sqlite3.Connection) -> None:
    """Rebuild content_slots under the current schema (UNIQUE(scheduled_at)
    alone, pillar_key nullable).

    SQLite cannot alter a table's constraints in place, so this renames the
    old table, creates the new one, and copies rows across — keeping at most
    one row per scheduled_at. If a timestamp somehow has more than one row
    under the old (scheduled_at, pillar_key) constraint, the row with the
    most "advanced" status wins (PUBLISHED > ASSIGNED > OPEN/FAILED), tied
    by lowest id, so an already-assigned/published slot is never silently
    discarded in favor of a still-open duplicate. Existing rows already have
    non-null pillar_key values, so relaxing that constraint doesn't change
    any copied data — it only makes the column newly insertable as NULL.

    IMPORTANT — PRAGMA legacy_alter_table: by default (legacy_alter_table
    OFF, the modern SQLite behavior), `ALTER TABLE content_slots RENAME TO
    content_slots_old` automatically rewrites any REFERENCES clause in
    *other* tables that pointed at "content_slots" to say "content_slots_old"
    instead — including videos.assigned_slot_id. Dropping content_slots_old
    afterward then leaves that FK dangling, pointing at a table that no
    longer exists (this is exactly the bug _repair_videos_assigned_slot_fk
    fixes for a database that already migrated before this guard existed).
    legacy_alter_table=ON suppresses that cross-table rewrite entirely, so
    videos' FK text is left untouched (still "content_slots") by this
    rename — verified empirically, not merely asserted from documentation.

    Runs inside one transaction: either the whole rebuild lands, or none of
    it does. PRAGMA foreign_keys can only be toggled outside a pending
    transaction (SQLite treats it as a no-op mid-transaction), so it — and
    legacy_alter_table — must be set before BEGIN and restored after COMMIT.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE content_slots RENAME TO content_slots_old")
            # NOT executescript(): Connection.executescript() implicitly
            # COMMITs any pending transaction before running, regardless of
            # isolation_level - which would silently end our BEGIN IMMEDIATE
            # here (SCHEMA_CONTENT_SLOTS is one statement, so plain execute()
            # is both correct and safe inside the transaction).
            conn.execute(SCHEMA_CONTENT_SLOTS)

            rows = conn.execute("SELECT * FROM content_slots_old ORDER BY scheduled_at, id").fetchall()
            kept: dict[str, sqlite3.Row] = {}
            for row in rows:
                key = row["scheduled_at"]
                current = kept.get(key)
                if current is None or _SLOT_STATUS_PRIORITY.get(row["status"], 0) > _SLOT_STATUS_PRIORITY.get(current["status"], 0):
                    kept[key] = row

            for row in kept.values():
                conn.execute(
                    """
                    INSERT INTO content_slots
                        (id, scheduled_at, pillar_key, prompt, status, assigned_video_id, google_calendar_event_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], row["scheduled_at"], row["pillar_key"], row["prompt"],
                        row["status"], row["assigned_video_id"], row["google_calendar_event_id"], row["created_at"],
                    ),
                )
            conn.execute("DROP TABLE content_slots_old")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")

    dropped = len(rows) - len(kept)
    if dropped:
        print(
            f"[content_store] migrated content_slots to UNIQUE(scheduled_at): "
            f"consolidated {dropped} duplicate-datetime row(s) from the old "
            f"(scheduled_at, pillar_key) constraint, keeping the most-advanced "
            f"status per timestamp.",
            file=sys.stderr,
        )


def _content_slots_needs_per_user_uniqueness_migration(conn: sqlite3.Connection) -> bool:
    """True if content_slots' stored CREATE TABLE SQL does not already
    contain the Milestone 3.8 composite constraint. Checked structurally
    against sqlite_master rather than assumed from a version number, same
    technique as _content_slots_needs_migration above."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'content_slots'"
    ).fetchone()
    sql = (row["sql"] or "") if row is not None else ""
    return "UNIQUE(user_id, scheduled_at)" not in sql


def _migrate_content_slots_to_per_user_uniqueness(conn: sqlite3.Connection) -> None:
    """Milestone 3.8 (hosted scheduling cadence configuration): widens
    content_slots' uniqueness from the global UNIQUE(scheduled_at) to
    UNIQUE(user_id, scheduled_at) — required for real multi-tenant slot
    generation (two different users generating a slot for the same
    wall-clock timestamp must not silently collide via INSERT OR IGNORE,
    which is exactly what the old global constraint caused). This is a
    strict widening, never a narrowing: every row that satisfied
    UNIQUE(scheduled_at) trivially satisfies UNIQUE(user_id, scheduled_at)
    too, so no existing data can violate it — no dedup/conflict-resolution
    pass is needed here, unlike _migrate_content_slots_unique_constraint
    above. One accepted consequence, deliberately not engineered around:
    SQL NULL is never equal to itself for uniqueness purposes, so rows
    with user_id IS NULL no longer participate in any uniqueness
    guarantee at all. Real production data has zero NULL-user_id rows
    (fully backfilled — see ADR-0007's own follow-up), so this only
    theoretically affects an intentionally-unscoped test/tooling caller.

    Uses the same rename+recreate+copy+drop technique (and the same
    PRAGMA legacy_alter_table=ON FK-safety guard) as
    _migrate_content_slots_unique_constraint — see that function's own
    docstring for why the guard is necessary. Column set is read
    dynamically from the old table (PRAGMA table_info) rather than
    assumed, so this is correct whether or not user_id/timezone/cadence_id
    already exist on the table being migrated."""
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE content_slots RENAME TO content_slots_old")
            conn.execute(SCHEMA_CONTENT_SLOTS)

            old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(content_slots_old)").fetchall()}
            new_columns = [
                "id", "scheduled_at", "pillar_key", "prompt", "status", "assigned_video_id",
                "google_calendar_event_id", "created_at", "user_id", "timezone", "cadence_id",
            ]
            select_list = ", ".join(c if c in old_columns else "NULL" for c in new_columns)
            conn.execute(
                f"INSERT INTO content_slots ({', '.join(new_columns)}) "
                f"SELECT {select_list} FROM content_slots_old"
            )
            conn.execute("DROP TABLE content_slots_old")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def _videos_fk_needs_repair(conn: sqlite3.Connection) -> bool:
    """True if videos.assigned_slot_id's foreign key points at anything
    other than content_slots — e.g. the stale "content_slots_old" name left
    behind by SQLite's automatic FK-reference rewrite during an earlier
    RENAME TABLE-based content_slots migration, before the
    legacy_alter_table guard above existed. Checked structurally via PRAGMA
    foreign_key_list rather than string-matching the stored CREATE TABLE
    SQL, so it doesn't depend on the exact stale name."""
    for row in conn.execute("PRAGMA foreign_key_list(videos)").fetchall():
        if row["from"] == "assigned_slot_id" and row["table"] != "content_slots":
            return True
    return False


def _rebuild_videos_table(conn: sqlite3.Connection) -> None:
    """Shared rebuild body for every "videos needs a structural change
    SQLite cannot make in place" migration (a stale FK target, or the old
    global UNIQUE(file_hash) — see the two callers below). Renames videos
    aside, recreates it fresh under the *current* SCHEMA_VIDEOS (plus
    _ensure_videos_columns), copies every column of every row across
    unchanged, drops the renamed original, and verifies
    PRAGMA foreign_key_check is clean afterward as a hard safety check.

    legacy_alter_table=ON is required: without it, `ALTER TABLE videos
    RENAME TO videos_old` would itself rewrite
    content_slots.assigned_video_id's REFERENCES clause to "videos_old" —
    verified empirically that legacy_alter_table=ON prevents that rewrite
    (see _migrate_content_slots_unique_constraint's own note on the mirror-
    image version of this same SQLite behavior).

    Column list to copy is the *intersection* of videos_old's actual
    columns and the freshly (re)created table's columns — not simply every
    new column — so this tolerates rebuilding a table that predates some
    newer nullable columns too (e.g. a pre-Milestone-1.2 fixture missing
    classifier/caption/storage/user_id columns entirely, which would
    otherwise never trigger this rebuild's FK-repair trigger but can now
    also trigger the file_hash-uniqueness one below). Any current column
    absent from videos_old is simply never in the copied column list,
    leaving it at its default (NULL — every such column is nullable) for
    every copied row, exactly as if that row had always lacked it.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE videos RENAME TO videos_old")
            old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(videos_old)").fetchall()}
            # NOT executescript() — see the matching note in
            # _migrate_content_slots_unique_constraint: it implicitly
            # COMMITs a pending transaction, which would silently end this
            # BEGIN IMMEDIATE. SCHEMA_VIDEOS is one statement.
            conn.execute(SCHEMA_VIDEOS)
            _ensure_videos_columns(conn)

            new_columns = [row["name"] for row in conn.execute("PRAGMA table_info(videos)").fetchall()]
            column_list = ", ".join(c for c in new_columns if c in old_columns)
            conn.execute(f"INSERT INTO videos ({column_list}) SELECT {column_list} FROM videos_old")

            conn.execute("DROP TABLE videos_old")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            f"content_store: videos table rebuild left {len(violations)} "
            f"foreign_key_check violation(s): {[dict(v) for v in violations]!r}"
        )


def _repair_videos_assigned_slot_fk(conn: sqlite3.Connection) -> None:
    """Repair a videos table whose assigned_slot_id foreign key was left
    pointing at a stale/nonexistent table name (see _videos_fk_needs_repair)
    by rebuilding it under the current schema — see _rebuild_videos_table."""
    _rebuild_videos_table(conn)
    print(
        "[content_store] repaired videos.assigned_slot_id: it referenced a stale table name "
        "left behind by an earlier content_slots rename migration; it now correctly "
        "references content_slots.",
        file=sys.stderr,
    )


def _videos_file_hash_needs_migration(conn: sqlite3.Connection) -> bool:
    """True if videos was created under the old schema where file_hash was
    globally UNIQUE (every database created before Milestone 3.7's
    re-upload-architecture follow-up). Checked structurally against the
    stored CREATE TABLE SQL, the same detection style
    _content_slots_needs_migration already uses."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'videos'"
    ).fetchone()
    sql = (row["sql"] or "") if row is not None else ""
    return "file_hash TEXT NOT NULL UNIQUE" in sql


def _migrate_videos_drop_file_hash_uniqueness(conn: sqlite3.Connection) -> None:
    """Rebuild videos without file_hash's global UNIQUE constraint (see
    _rebuild_videos_table and this module's own docstring). Every row is
    copied across completely unchanged — unlike
    _migrate_content_slots_unique_constraint, the old constraint could only
    ever produce zero or one row per file_hash, so there is no possible
    duplicate-row conflict to resolve on the way in."""
    _rebuild_videos_table(conn)
    print(
        "[content_store] migrated videos: dropped file_hash's global UNIQUE constraint "
        "(re-upload architecture, Milestone 3.7 follow-up) — file_hash is now a plain "
        "indexed content fingerprint, not a record-identity constraint.",
        file=sys.stderr,
    )


@dataclass
class VideoRecord:
    id: int
    file_hash: str
    original_filename: str
    original_path: str
    canonical_media_path: str | None
    container: str | None
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    duration_seconds: float | None
    file_size_bytes: int | None
    transcript: str | None
    transcript_language: str | None
    transcription_status: str | None
    classified_pillar: str | None
    classification_confidence: float | None
    classification_reason: str | None
    classification_second_score: float | None
    classification_margin: float | None
    classifier: str | None
    status: str
    failure_reason: str | None
    assigned_slot_id: int | None
    created_at: str
    processed_at: str | None
    caption_text: str | None
    caption_source: str | None
    user_id: int | None
    storage_provider: str | None
    storage_key: str | None


@dataclass
class SlotRecord:
    id: int
    scheduled_at: str
    pillar_key: str | None
    prompt: str | None
    status: str
    assigned_video_id: int | None
    google_calendar_event_id: str | None
    created_at: str
    user_id: int | None
    timezone: str | None
    cadence_id: int | None


@dataclass
class CadenceRecord:
    id: int
    user_id: int
    timezone: str
    is_active: bool
    created_at: str
    updated_at: str
    posting_times: list["CadenceTimeRecord"]


@dataclass
class CadenceTimeRecord:
    id: int
    cadence_id: int
    weekday: str
    posting_time: str


@dataclass
class PlatformPostRecord:
    id: int
    video_id: int
    platform: str
    status: str
    platform_post_id: str | None
    scheduled_at: str | None
    published_at: str | None
    failure_reason: str | None
    created_at: str
    updated_at: str
    retry_count: int
    next_retry_at: str | None
    next_status_check_at: str | None
    status_check_count: int
    user_id: int | None
    failure_code: str | None
    # Milestone 3.13 — see _PLATFORM_POSTS_MIGRATION_COLUMNS.
    submission_state: str | None = None
    submission_started_at: str | None = None
    # Milestone 4.2 — see _PLATFORM_POSTS_MIGRATION_COLUMNS.
    platform_media_id: str | None = None


@dataclass
class UserRecord:
    id: int
    email: str
    display_name: str | None
    created_at: str
    updated_at: str


@dataclass
class AuthIdentityRecord:
    id: int
    user_id: int
    provider: str
    provider_subject: str
    provider_email: str | None
    created_at: str
    updated_at: str


@dataclass
class PlatformConnectionRecord:
    id: int
    user_id: int
    platform: str
    external_account_id: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass
class PlatformCredentialRecord:
    id: int
    platform_connection_id: int
    encrypted_payload: str
    created_at: str
    updated_at: str


@dataclass
class OAuthStateRecord:
    id: int
    user_id: int
    platform: str
    state: str
    code_verifier: str
    redirect_uri: str
    created_at: str
    expires_at: str
    consumed_at: str | None
    # Milestone 4.1 — None for TikTok and pre-4.1 rows.
    return_target: str | None = None


@dataclass
class UploadBatchRecord:
    id: int
    user_id: int
    started_at: str
    completed_at: str | None
    file_count: int
    attempted_bytes: int
    successful_bytes: int
    success_count: int | None
    failure_count: int | None
    total_duration_ms: int | None
    status: str


@dataclass
class UploadAttemptRecord:
    id: int
    batch_id: int
    user_id: int
    video_id: int | None
    original_filename: str
    file_size_bytes: int | None
    started_at: str
    completed_at: str | None
    duration_ms: int | None
    status: str
    error_code: str | None


def _row_to_video(row: sqlite3.Row) -> VideoRecord:
    return VideoRecord(**dict(row))


def _row_to_slot(row: sqlite3.Row) -> SlotRecord:
    return SlotRecord(**dict(row))


def _row_to_platform_post(row: sqlite3.Row) -> PlatformPostRecord:
    return PlatformPostRecord(**dict(row))


def _row_to_user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(**dict(row))


def _row_to_auth_identity(row: sqlite3.Row) -> AuthIdentityRecord:
    return AuthIdentityRecord(**dict(row))


def _row_to_platform_connection(row: sqlite3.Row) -> PlatformConnectionRecord:
    return PlatformConnectionRecord(**dict(row))


def _row_to_platform_credential(row: sqlite3.Row) -> PlatformCredentialRecord:
    return PlatformCredentialRecord(**dict(row))


def _row_to_oauth_state(row: sqlite3.Row) -> OAuthStateRecord:
    return OAuthStateRecord(**dict(row))


def _row_to_upload_batch(row: sqlite3.Row) -> UploadBatchRecord:
    return UploadBatchRecord(**dict(row))


def _row_to_upload_attempt(row: sqlite3.Row) -> UploadAttemptRecord:
    return UploadAttemptRecord(**dict(row))


class ContentStore:
    """Thin wrapper around a single SQLite connection for this pipeline."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA_VIDEOS)
        _ensure_videos_columns(self._conn)
        if _videos_fk_needs_repair(self._conn):
            _repair_videos_assigned_slot_fk(self._conn)
        if _videos_file_hash_needs_migration(self._conn):
            _migrate_videos_drop_file_hash_uniqueness(self._conn)
        # Replaces the implicit index SQLite maintained for the old
        # UNIQUE(file_hash) constraint — file_hash is still looked up
        # directly (get_video_by_hash) even though it's no longer unique,
        # so a plain index keeps that lookup (and any future duplicate-
        # detection/analytics query) fast. Idempotent; also needed by a
        # brand-new database, which never had the old constraint to
        # migrate away from in the first place.
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_file_hash ON videos(file_hash)")
        if _content_slots_needs_migration(self._conn):
            _migrate_content_slots_unique_constraint(self._conn)
        self._conn.executescript(SCHEMA_CONTENT_SLOTS)
        if _content_slots_needs_per_user_uniqueness_migration(self._conn):
            _migrate_content_slots_to_per_user_uniqueness(self._conn)
        _ensure_content_slots_columns(self._conn)
        self._conn.executescript(SCHEMA_PLATFORM_POSTS)
        _ensure_platform_posts_columns(self._conn)
        # Milestone 3.2 (ownership): created after videos/content_slots/
        # platform_posts so the REFERENCES users(id) clauses those tables'
        # new user_id columns carry are meaningful from the first run
        # (SQLite does not require the referenced table to exist first —
        # verified directly — but creating users() first keeps the
        # dependency order obvious to a reader).
        self._conn.executescript(SCHEMA_USERS)
        self._conn.executescript(SCHEMA_AUTH_IDENTITIES)
        self._conn.executescript(SCHEMA_PLATFORM_CONNECTIONS)
        # Milestone 3.6: created after platform_connections/users so their
        # REFERENCES clauses are meaningful from the first run.
        self._conn.executescript(SCHEMA_PLATFORM_CREDENTIALS)
        self._conn.executescript(SCHEMA_OAUTH_STATES)
        _ensure_oauth_states_columns(self._conn)
        # Milestone 3.7 follow-up: created after users/videos so their
        # REFERENCES clauses are meaningful from the first run.
        self._conn.executescript(SCHEMA_UPLOAD_BATCHES)
        _migrate_upload_batches_rename_total_bytes(self._conn)
        _ensure_upload_batches_columns(self._conn)
        self._conn.executescript(SCHEMA_UPLOAD_ATTEMPTS)
        # Milestone 3.8: created after users so its REFERENCES users(id)
        # clause is meaningful from the first run. content_slots' own
        # cadence_id REFERENCES posting_cadences(id) is declared earlier
        # (above) purely for readability of SCHEMA_CONTENT_SLOTS — SQLite
        # does not require the referenced table to exist first, same as
        # every other forward reference in this __init__.
        self._conn.executescript(SCHEMA_POSTING_CADENCES)
        self._conn.executescript(SCHEMA_POSTING_CADENCE_TIMES)
        # Milestone 3.10.1: after videos, for its REFERENCES videos(id).
        self._conn.executescript(SCHEMA_VIDEO_HASHTAGS)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ContentStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        """Wrap a block of writes in a single atomic transaction."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # -- videos ---------------------------------------------------------

    def get_video_by_hash(self, file_hash: str) -> VideoRecord | None:
        """Returns *some* row with this file_hash, or None — file_hash is
        no longer unique (Milestone 3.7 re-upload follow-up), so this is
        only meaningful where a caller's own insertion discipline
        guarantees at most one match. That currently holds for local
        ingestion (media.processing.process_one only ever inserts a row
        for a given hash once, then reuses it for restart-safety) but not
        for the hosted upload path (media.media_storage.create_video_from_upload
        never looks this up at all anymore, and always inserts a new row —
        see that function's own docstring)."""
        row = self._conn.execute(
            "SELECT * FROM videos WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return _row_to_video(row) if row else None

    def get_video(self, video_id: int) -> VideoRecord | None:
        row = self._conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        return _row_to_video(row) if row else None

    def get_video_by_path(self, original_path: str) -> VideoRecord | None:
        """Look up a video by its original discovery path.

        Used only for FIFO ordering (see process_content.discover_videos):
        a video already known to the store sorts by its immutable
        created_at instead of the current (possibly touched/copied) file
        mtime. Scoped to path stability — a renamed file is not matched
        here and is treated as newly discovered; see
        docs/decisions/0005-fifo-baseline-and-optional-strategy-routing.md.
        """
        row = self._conn.execute(
            "SELECT * FROM videos WHERE original_path = ?", (original_path,)
        ).fetchone()
        return _row_to_video(row) if row else None

    def insert_video(
        self, file_hash: str, original_filename: str, original_path: str, created_at: str,
        user_id: int | None = None,
    ) -> VideoRecord:
        """user_id (Milestone 3.2) is optional and defaults to None (legacy/
        unscoped) purely for backward compatibility with every pre-3.2
        caller and test — every real production caller
        (media.processing.process_one) always passes the resolved local
        user's id. See docs/architecture/hosted-product-boundary.md's
        persistence-boundary section for why this stays optional at the
        ContentStore layer rather than required."""
        cur = self._conn.execute(
            """
            INSERT INTO videos (file_hash, original_filename, original_path, status, created_at, user_id)
            VALUES (?, ?, ?, 'DISCOVERED', ?, ?)
            """,
            (file_hash, original_filename, original_path, created_at, user_id),
        )
        # Milestone 3.7 re-upload follow-up: looked up by the row's own id,
        # never by file_hash — file_hash is no longer unique, so
        # get_video_by_hash could return a *different* pre-existing row
        # sharing the same hash instead of the one just inserted here.
        return self.get_video(cur.lastrowid) or _raise_missing(cur.lastrowid)

    def update_video(self, video_id: int, **fields) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = [*fields.values(), video_id]
        self._conn.execute(f"UPDATE videos SET {columns} WHERE id = ?", values)

    def set_video_caption(
        self, video_id: int, caption_text: str | None, caption_source: str | None, hashtags: list[str],
    ) -> None:
        """Write a video's caption and replace its derived video_hashtags
        rows in one transaction (Milestone 3.10.1), so the structured
        hashtags always describe the current caption_text. Callers derive
        `hashtags` with media.hashtags.extract_hashtags(caption_text)."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE videos SET caption_text = ?, caption_source = ? WHERE id = ?",
                (caption_text, caption_source, video_id),
            )
            conn.execute("DELETE FROM video_hashtags WHERE video_id = ?", (video_id,))
            conn.executemany(
                "INSERT INTO video_hashtags (video_id, position, hashtag) VALUES (?, ?, ?)",
                [(video_id, position, tag) for position, tag in enumerate(hashtags)],
            )

    def list_video_hashtags(self, video_id: int) -> list[str]:
        """The video's derived hashtags, in caption order (duplicates kept)."""
        rows = self._conn.execute(
            "SELECT hashtag FROM video_hashtags WHERE video_id = ? ORDER BY position", (video_id,)
        ).fetchall()
        return [row["hashtag"] for row in rows]

    def list_videos_for_user(self, user_id: int) -> list[VideoRecord]:
        """Every video owned by user_id, newest first — the Library API's
        one query (Milestone 3.7). Scoped strictly to user_id; there is no
        "list everything" variant, matching this codebase's existing
        tenant-isolation convention (media_storage.py's ownership check
        before any read) — a caller must always know which user it's
        asking for."""
        rows = self._conn.execute(
            "SELECT * FROM videos WHERE user_id = ? ORDER BY created_at DESC, id DESC", (user_id,)
        ).fetchall()
        return [_row_to_video(row) for row in rows]

    # -- content_slots ----------------------------------------------------

    def insert_slot_if_missing(
        self,
        scheduled_at: str,
        pillar_key: str | None,
        prompt: str | None,
        created_at: str,
        google_calendar_event_id: str | None = None,
        user_id: int | None = None,
        timezone: str | None = None,
        cadence_id: int | None = None,
    ) -> bool:
        """Insert a content_slot unless one already exists for this
        (user_id, scheduled_at) pair (Milestone 3.8 widened this from a
        global scheduled_at uniqueness — see
        _migrate_content_slots_to_per_user_uniqueness's own docstring).

        Returns True if a new row was created, False if it already existed —
        this is what keeps re-running generate_calendar.py for the same month
        (or the hosted cadence generator for the same horizon) from
        duplicating slots, and what stops a changed strategy from silently
        overwriting an existing slot's pillar/prompt: the row already there
        always wins, regardless of what the current config would now compute
        for that timestamp.

        user_id (Milestone 3.2) is optional, defaulting to None (legacy/
        unscoped) — see insert_video's docstring for why. The CLI caller
        (calendar.generate_calendar.push_events) always passes the resolved
        local user's id and leaves timezone/cadence_id at their defaults
        (NULL — this is not a hosted-cadence-generated slot). The hosted
        cadence generator (calendar.hosted_cadence, Milestone 3.8) always
        passes all four.
        """
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO content_slots
                (scheduled_at, pillar_key, prompt, status, google_calendar_event_id, created_at,
                 user_id, timezone, cadence_id)
            VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """,
            (scheduled_at, pillar_key, prompt, google_calendar_event_id, created_at, user_id, timezone, cadence_id),
        )
        return cur.rowcount > 0

    def get_slot(self, slot_id: int) -> SlotRecord | None:
        row = self._conn.execute("SELECT * FROM content_slots WHERE id = ?", (slot_id,)).fetchone()
        return _row_to_slot(row) if row else None

    def find_earliest_open_slot(
        self, pillar_key: str, after_iso: str, user_id: int | None = None
    ) -> SlotRecord | None:
        """user_id (Milestone 3.2) is optional; when provided, restricts the
        match to slots owned by that user (or unowned, legacy, slots —
        `user_id = ? OR user_id IS NULL` — see docstring on the equivalent
        clause in find_earliest_open_slot_fifo below for why NULL stays
        eligible). Omitting it preserves the exact pre-3.2 unscoped query."""
        if user_id is None:
            row = self._conn.execute(
                """
                SELECT * FROM content_slots
                WHERE pillar_key = ? AND status = 'OPEN' AND scheduled_at > ?
                ORDER BY scheduled_at ASC
                LIMIT 1
                """,
                (pillar_key, after_iso),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT * FROM content_slots
                WHERE pillar_key = ? AND status = 'OPEN' AND scheduled_at > ?
                  AND (user_id = ? OR user_id IS NULL)
                ORDER BY scheduled_at ASC
                LIMIT 1
                """,
                (pillar_key, after_iso, user_id),
            ).fetchone()
        return _row_to_slot(row) if row else None

    def find_earliest_open_slot_fifo(self, after_iso: str, user_id: int | None = None) -> SlotRecord | None:
        """Earliest OPEN slot at or after after_iso, regardless of pillar_key.
        Inclusive (>=), matching the FIFO matching contract in
        docs/decisions/0005-fifo-baseline-and-optional-strategy-routing.md —
        deliberately different from find_earliest_open_slot's exclusive (>),
        which stays unchanged for pillar mode.

        user_id (Milestone 3.2) is optional; when provided, restricts the
        match to slots owned by that user, or unowned (NULL) slots —
        NULL stays eligible so a real month's worth of pre-3.2 legacy slots
        (created before ownership existed) remain assignable to the first
        scoped caller that reaches them, rather than becoming permanently
        stuck OPEN and unmatchable. Omitting user_id entirely preserves the
        exact pre-3.2 unscoped query.
        """
        if user_id is None:
            row = self._conn.execute(
                """
                SELECT * FROM content_slots
                WHERE status = 'OPEN' AND scheduled_at >= ?
                ORDER BY scheduled_at ASC
                LIMIT 1
                """,
                (after_iso,),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT * FROM content_slots
                WHERE status = 'OPEN' AND scheduled_at >= ? AND (user_id = ? OR user_id IS NULL)
                ORDER BY scheduled_at ASC
                LIMIT 1
                """,
                (after_iso, user_id),
            ).fetchone()
        return _row_to_slot(row) if row else None

    def list_slots_by_status(self, statuses: list[str]) -> list[SlotRecord]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM content_slots WHERE status IN ({placeholders}) ORDER BY scheduled_at",
            statuses,
        ).fetchall()
        return [_row_to_slot(row) for row in rows]

    def delete_slot(self, slot_id: int) -> None:
        """Delete a content_slot row. Raises an sqlite3 IntegrityError (FK
        violation) if a video still references it via assigned_slot_id —
        call unassign_video_for_slot(slot_id) first for an ASSIGNED slot."""
        self._conn.execute("DELETE FROM content_slots WHERE id = ?", (slot_id,))

    def unassign_video_for_slot(self, slot_id: int) -> None:
        """Reset any video assigned to this slot back to CLASSIFIED with no
        assignment, so the slot can be deleted and the video can be routed
        to a different slot on a future process_content.py run. Used by
        clear_calendar.py --all; does not touch the video's transcript or
        classification, only its scheduling state."""
        self._conn.execute(
            "UPDATE videos SET assigned_slot_id = NULL, status = 'CLASSIFIED', processed_at = NULL "
            "WHERE assigned_slot_id = ?",
            (slot_id,),
        )

    def assign_slot(self, video_id: int, slot_id: int) -> None:
        """Atomically claim a slot for a video. Raises SlotUnavailableError if
        the slot is no longer OPEN.

        Milestone 3.2 (ownership): also raises OwnershipMismatchError if both
        the video and the slot already carry a non-NULL user_id and those
        two values differ — the Phase 7 invariant
        (video.user_id == assigned_slot.user_id) enforced at the one real
        write-time choke point, rather than only trusted to callers picking
        a same-owner slot correctly. Deliberately does NOT raise when either
        side is NULL (legacy/unscoped) — every pre-3.2 call site, and every
        existing test, assigns unowned videos to unowned slots and must keep
        working unchanged.
        """
        with self.transaction() as conn:
            slot_row = conn.execute(
                "SELECT status, user_id FROM content_slots WHERE id = ?", (slot_id,)
            ).fetchone()
            if slot_row is None or slot_row["status"] != "OPEN":
                raise SlotUnavailableError(f"content_slot {slot_id} is not OPEN")

            video_row = conn.execute("SELECT user_id FROM videos WHERE id = ?", (video_id,)).fetchone()
            video_user_id = video_row["user_id"] if video_row else None
            slot_user_id = slot_row["user_id"]
            if video_user_id is not None and slot_user_id is not None and video_user_id != slot_user_id:
                raise OwnershipMismatchError(
                    f"video {video_id} (user_id={video_user_id}) cannot be assigned to "
                    f"content_slot {slot_id} (user_id={slot_user_id}) — different owners."
                )

            conn.execute(
                "UPDATE content_slots SET status = 'ASSIGNED', assigned_video_id = ? WHERE id = ?",
                (video_id, slot_id),
            )
            conn.execute(
                "UPDATE videos SET assigned_slot_id = ? WHERE id = ?",
                (slot_id, video_id),
            )

    def unassign_slot(self, slot_id: int) -> None:
        """Reverse an assign_slot() claim (Milestone 3.9 — Queue's "Remove
        from schedule" action): resets the slot back to OPEN and clears the
        video's assigned_slot_id, deleting any still-PENDING platform_posts
        row for that video (the materialize_platform_posts_for_assignment
        side effect of the original assignment) so the video can be cleanly
        reassigned to a different slot, or deleted, afterward.

        Raises SlotUnavailableError if the slot is not currently ASSIGNED —
        there is nothing to remove. Raises PlatformPostInProgressError,
        without changing anything, if the assigned video already has a
        platform_posts row that is PUBLISHING/PUBLISHED/FAILED — a real
        publish attempt has already started or completed, so that record
        (and the schedule relationship it documents) is preserved rather
        than silently reversed; only a still-PENDING (never attempted) row
        is safe to delete alongside the slot claim itself.

        Deliberately narrower than clear_calendar.py's own
        unassign_video_for_slot (which resets the video to CLASSIFIED for
        the local CLI ingestion pipeline's own state machine — not
        applicable to an already-uploaded hosted video, which has no
        CLASSIFIED step to return to)."""
        with self.transaction() as conn:
            slot_row = conn.execute(
                "SELECT status, assigned_video_id FROM content_slots WHERE id = ?", (slot_id,)
            ).fetchone()
            if slot_row is None or slot_row["status"] != "ASSIGNED" or slot_row["assigned_video_id"] is None:
                raise SlotUnavailableError(f"content_slot {slot_id} is not currently assigned")
            video_id = slot_row["assigned_video_id"]

            post_rows = conn.execute(
                "SELECT status FROM platform_posts WHERE video_id = ?", (video_id,)
            ).fetchall()
            if any(row["status"] != "PENDING" for row in post_rows):
                raise PlatformPostInProgressError(
                    f"video {video_id} has a platform post that is already publishing, published, "
                    "or failed — remove it from schedule is refused to preserve that record."
                )

            conn.execute("DELETE FROM platform_posts WHERE video_id = ? AND status = 'PENDING'", (video_id,))
            conn.execute(
                "UPDATE content_slots SET status = 'OPEN', assigned_video_id = NULL WHERE id = ?", (slot_id,)
            )
            conn.execute("UPDATE videos SET assigned_slot_id = NULL WHERE id = ?", (video_id,))

    def list_content_slots_for_user(self, user_id: int, from_iso: str, to_iso: str) -> list[SlotRecord]:
        """Every content_slot owned by user_id with scheduled_at in
        [from_iso, to_iso) — the hosted cadence preview's one read query
        (Milestone 3.8). Legacy/unowned (user_id IS NULL) rows are
        deliberately excluded, unlike find_earliest_open_slot*'s
        assignment-matching queries — this is a per-user listing, not a
        claim, so there is no "fall back to unowned" case to honor."""
        rows = self._conn.execute(
            """
            SELECT * FROM content_slots
            WHERE user_id = ? AND scheduled_at >= ? AND scheduled_at < ?
            ORDER BY scheduled_at ASC
            """,
            (user_id, from_iso, to_iso),
        ).fetchall()
        return [_row_to_slot(row) for row in rows]

    # -- posting_cadences (Milestone 3.8) ---------------------------------

    def get_cadence_for_user(self, user_id: int) -> CadenceRecord | None:
        row = self._conn.execute("SELECT * FROM posting_cadences WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        time_rows = self._conn.execute(
            "SELECT * FROM posting_cadence_times WHERE cadence_id = ? ORDER BY weekday, posting_time",
            (row["id"],),
        ).fetchall()
        return CadenceRecord(
            id=row["id"], user_id=row["user_id"], timezone=row["timezone"], is_active=bool(row["is_active"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            posting_times=[CadenceTimeRecord(**dict(t)) for t in time_rows],
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
        """Atomically: upsert the user's one posting_cadences row (stable id
        across edits — an edit UPDATEs in place, never inserting a second
        row per user), fully replace its posting_cadence_times, delete
        every future OPEN slot this cadence previously generated
        (reconciliation — a changed cadence, or one just set inactive,
        must not leave stale OPEN slots behind), then insert the freshly
        generated slots. All in one transaction: a caller must never be
        able to observe a saved cadence whose slot pool doesn't match it.

        Two different "now" values are required, matching this codebase's
        existing scheduled_at (naive local) vs. updated_at (aware UTC)
        convention split (see AGENTS.md "Timestamp conventions"):
        now_utc_iso stamps created_at/updated_at (aware UTC, matching
        every other *_at column that isn't scheduled_at); now_local_iso is
        the naive-local boundary compared against scheduled_at in the
        reconciliation DELETE below, and must be the exact same "now" the
        caller's calendar.hosted_cadence.generate_slot_datetimes() used to
        decide which candidates were still in the future — otherwise a
        slot could be reconciled away and regenerated inconsistently
        around the boundary instant.

        Preserved unconditionally by the reconciliation DELETE below,
        regardless of cadence_id: any slot with status
        ASSIGNED/PUBLISHED/FAILED, and any slot with cadence_id IS NULL
        (manual/legacy — includes every pre-3.8 CLI-created row). Only a
        *future*, *OPEN*, *this-cadence's* slot is ever removed here.

        generated_slots is a list of (scheduled_at, timezone) pairs
        already computed by calendar.hosted_cadence (pure, no DB access)
        — this method only persists them, mirroring
        media_storage.create_video_from_upload's own "pure computation,
        then one persistence call" shape. An inactive cadence's caller
        passes an empty generated_slots list — the reconciliation DELETE
        still runs, correctly clearing any previously-generated future
        OPEN slots even though nothing new is inserted.
        """
        with self.transaction() as conn:
            existing = conn.execute("SELECT id FROM posting_cadences WHERE user_id = ?", (user_id,)).fetchone()
            if existing is None:
                cur = conn.execute(
                    "INSERT INTO posting_cadences (user_id, timezone, is_active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, timezone, int(is_active), now_utc_iso, now_utc_iso),
                )
                cadence_id = cur.lastrowid
            else:
                cadence_id = existing["id"]
                conn.execute(
                    "UPDATE posting_cadences SET timezone = ?, is_active = ?, updated_at = ? WHERE id = ?",
                    (timezone, int(is_active), now_utc_iso, cadence_id),
                )

            conn.execute("DELETE FROM posting_cadence_times WHERE cadence_id = ?", (cadence_id,))
            for weekday, posting_time in posting_times:
                conn.execute(
                    "INSERT INTO posting_cadence_times (cadence_id, weekday, posting_time) VALUES (?, ?, ?)",
                    (cadence_id, weekday, posting_time),
                )

            conn.execute(
                "DELETE FROM content_slots "
                "WHERE user_id = ? AND cadence_id = ? AND status = 'OPEN' AND scheduled_at > ?",
                (user_id, cadence_id, now_local_iso),
            )

            for scheduled_at, slot_timezone in generated_slots:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO content_slots
                        (scheduled_at, pillar_key, prompt, status, created_at, user_id, timezone, cadence_id)
                    VALUES (?, NULL, NULL, 'OPEN', ?, ?, ?, ?)
                    """,
                    (scheduled_at, now_utc_iso, user_id, slot_timezone, cadence_id),
                )

        return self.get_cadence_for_user(user_id)

    # -- platform_posts (Milestone 2.0) ----------------------------------

    def get_platform_post(self, video_id: int, platform: str) -> PlatformPostRecord | None:
        row = self._conn.execute(
            "SELECT * FROM platform_posts WHERE video_id = ? AND platform = ?", (video_id, platform)
        ).fetchone()
        return _row_to_platform_post(row) if row else None

    def list_platform_posts_for_video(self, video_id: int) -> list[PlatformPostRecord]:
        """Every platform_posts row for video_id, regardless of platform or
        status (Milestone 3.7 follow-up — delete_video's queue/schedule-
        reference check). A non-empty result means this video has a
        publishing record — pending, in flight, published, or failed — so
        media.media_storage.delete_video refuses to delete it; deleting a
        video out from under a real or historical publish attempt would
        silently orphan that record's video_id."""
        rows = self._conn.execute(
            "SELECT * FROM platform_posts WHERE video_id = ? ORDER BY id ASC", (video_id,)
        ).fetchall()
        return [_row_to_platform_post(row) for row in rows]

    def insert_platform_post(
        self, video_id: int, platform: str, created_at: str, scheduled_at: str | None = None,
        user_id: int | None = None,
    ) -> PlatformPostRecord:
        """Create the one publishing record for this (video, platform) pair.

        Raises sqlite3.IntegrityError (UNIQUE(video_id, platform)) if one
        already exists — callers must check get_platform_post() first and
        update the existing row instead; this is the idempotency guard
        against accidentally submitting the same post twice, enforced by
        the schema rather than only by caller discipline.

        user_id (Milestone 3.2) is optional, defaulting to None (legacy/
        unscoped) — see insert_video's docstring for why.
        """
        self._conn.execute(
            """
            INSERT INTO platform_posts (video_id, platform, status, scheduled_at, created_at, updated_at, user_id)
            VALUES (?, ?, 'PENDING', ?, ?, ?, ?)
            """,
            (video_id, platform, scheduled_at, created_at, created_at, user_id),
        )
        return self.get_platform_post(video_id, platform)

    def insert_platform_post_if_missing(
        self, video_id: int, platform: str, scheduled_at: str, created_at: str, user_id: int | None = None,
    ) -> bool:
        """Create a PENDING platform_posts row for (video_id, platform)
        unless one already exists. Returns True if a new row was created,
        False if one already existed — mirrors insert_slot_if_missing's
        contract exactly (INSERT OR IGNORE against the existing
        UNIQUE(video_id, platform) constraint).

        Milestone 2.1.2 (platform-post materialization): unlike
        insert_platform_post (which raises on a duplicate, expecting the
        caller to have already checked get_platform_post()), this is safe
        to call unconditionally and repeatedly for the same pair — it never
        runs an UPDATE, so it can never reset an existing row's
        status/platform_post_id/published_at/failure_reason, no matter how
        many times assignment/materialization logic is revisited for the
        same video.

        user_id (Milestone 3.2) is optional, defaulting to None (legacy/
        unscoped) — the real production caller
        (scheduling.platform_post_materializer.materialize_platform_posts_for_assignment)
        always passes the owning video's user_id.
        """
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO platform_posts
                (video_id, platform, status, scheduled_at, created_at, updated_at, user_id)
            VALUES (?, ?, 'PENDING', ?, ?, ?, ?)
            """,
            (video_id, platform, scheduled_at, created_at, created_at, user_id),
        )
        return cur.rowcount > 0

    def update_platform_post(self, post_id: int, updated_at: str, **fields) -> None:
        fields = {**fields, "updated_at": updated_at}
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = [*fields.values(), post_id]
        self._conn.execute(f"UPDATE platform_posts SET {columns} WHERE id = ?", values)

    def claim_platform_post(self, post_id: int, updated_at: str, user_id: int | None = None) -> bool:
        """Atomically transition platform_posts.id=post_id from PENDING to
        PUBLISHING. Returns True if this call performed the transition
        (this caller now owns the row), False if the row didn't exist or
        was no longer PENDING (already claimed by another caller, or in a
        terminal PUBLISHING/PUBLISHED/FAILED state) — never raises merely
        because another claimant won first.

        Milestone 2.1.3 (atomic platform-post claiming): a single
        UPDATE ... WHERE id = ? AND status = 'PENDING' is one indivisible
        SQLite write — two connections racing to claim the same row are
        serialized by SQLite's own file-level locking, so at most one
        UPDATE can ever see status = 'PENDING' still true; the loser's
        WHERE clause simply no longer matches (rowcount = 0). Deliberately
        not a SELECT-then-UPDATE — that would let two callers both
        observe PENDING before either writes. See
        docs/evaluations/scheduling/milestone-2.1.3-atomic-platform-post-claiming.md.

        Only status and updated_at are ever written by a claim —
        platform_post_id, scheduled_at, published_at, failure_reason,
        video_id, and platform are never touched here.

        Milestone 3.2 (ownership): user_id is optional; when provided, the
        WHERE clause also requires `user_id = ?`, so a row this caller does
        not own returns rowcount = 0 exactly like "already claimed by
        someone else" — the same "never raises, just doesn't win" contract,
        now covering a cross-tenant claim attempt too. This is
        defense-in-depth: the real selection guarantee already lives in
        get_due_platform_posts' own user_id scoping (a caller scoped to
        user A never even sees user B's row id to pass here) — this is the
        second, independent check at the actual ownership-transition point.
        """
        if user_id is None:
            cur = self._conn.execute(
                "UPDATE platform_posts SET status = 'PUBLISHING', updated_at = ? WHERE id = ? AND status = 'PENDING'",
                (updated_at, post_id),
            )
        else:
            cur = self._conn.execute(
                "UPDATE platform_posts SET status = 'PUBLISHING', updated_at = ? "
                "WHERE id = ? AND status = 'PENDING' AND user_id = ?",
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
            "SELECT DISTINCT p.user_id FROM platform_posts p WHERE p.platform = ? "
            "AND p.status IN ('PENDING', 'PUBLISHING') AND p.user_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM auth_identities a WHERE a.user_id = p.user_id) ORDER BY p.user_id",
            (platform,),
        ).fetchall()
        return [row["user_id"] for row in rows]

    def get_due_platform_posts(
        self, platform: str, now_iso: str, eligible_statuses: list[str], user_id: int | None = None
    ) -> list[PlatformPostRecord]:
        """platform_posts rows for `platform` that are scheduled (scheduled_at
        IS NOT NULL), due (scheduled_at <= now_iso — inclusive, so a row
        scheduled exactly at now_iso is due), not waiting on a scheduled
        retry (next_retry_at IS NULL OR next_retry_at <= now_iso — same
        now_iso and same naive-local-time convention as scheduled_at, see
        Milestone 2.1.6), and in one of eligible_statuses. Ordered by the
        original scheduled_at first, ties broken by id — deliberately NOT
        by next_retry_at: scheduled_at reflects the calendar-driven posting
        order that matters to the business, and a retry's internal backoff
        timing should never reorder that relative to other due content
        (see docs/evaluations/scheduling/milestone-2.1.6-retry-backoff.md
        "Due Selection"). Pure read — never mutates a row.

        Milestone 2.1.1 (due-post detection): this is the query layer only.
        now_iso and eligible_statuses are supplied by the caller (see
        due_post_selector.get_due_posts) rather than decided here, so this
        method carries no timezone or business-eligibility logic of its
        own — same division of responsibility as find_earliest_open_slot_fifo/
        slot_matcher.py.

        Milestone 3.2 (ownership): user_id is optional; when provided,
        restricts results to that owner's rows only — this is the actual
        enforcement point behind the "a hosted background job must never
        operate on one user's records using another user's credentials"
        invariant (docs/architecture/hosted-product-boundary.md §5).
        Omitting it preserves the exact pre-3.2 unscoped query.
        """
        placeholders = ", ".join("?" for _ in eligible_statuses)
        if user_id is None:
            rows = self._conn.execute(
                f"""
                SELECT * FROM platform_posts
                WHERE platform = ?
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at <= ?
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  AND status IN ({placeholders})
                ORDER BY scheduled_at ASC, id ASC
                """,
                (platform, now_iso, now_iso, *eligible_statuses),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"""
                SELECT * FROM platform_posts
                WHERE platform = ?
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at <= ?
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  AND status IN ({placeholders})
                  AND user_id = ?
                ORDER BY scheduled_at ASC, id ASC
                """,
                (platform, now_iso, now_iso, *eligible_statuses, user_id),
            ).fetchall()
        return [_row_to_platform_post(row) for row in rows]

    def get_recoverable_platform_posts(
        self, platform: str, stale_before_iso: str, user_id: int | None = None
    ) -> list[PlatformPostRecord]:
        """platform_posts rows for `platform` that are PUBLISHING and have
        not been touched since before stale_before_iso — candidates for
        crash_recovery.py, not ordinary due work (get_due_platform_posts
        stays PENDING-only). Ordered oldest-updated first, ties broken by
        id, for deterministic output. Pure read — never mutates a row.

        Milestone 2.1.5 (crash recovery): stale_before_iso must be an aware
        UTC isoformat string, matching how updated_at is always written
        (see publish_tiktok._now_iso/worker._now_iso) — this is a
        deliberately different time convention from
        get_due_platform_posts' now_iso, which is naive local time
        matching scheduled_at. Mixing the two would silently miscompare.

        user_id (Milestone 3.2) is optional; see get_due_platform_posts'
        docstring — same scoping contract.
        """
        if user_id is None:
            rows = self._conn.execute(
                "SELECT * FROM platform_posts WHERE platform = ? AND status = 'PUBLISHING' AND updated_at < ? "
                "ORDER BY updated_at ASC, id ASC",
                (platform, stale_before_iso),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM platform_posts WHERE platform = ? AND status = 'PUBLISHING' AND updated_at < ? "
                "AND user_id = ? ORDER BY updated_at ASC, id ASC",
                (platform, stale_before_iso, user_id),
            ).fetchall()
        return [_row_to_platform_post(row) for row in rows]

    def get_reconcilable_platform_posts(
        self, platform: str, now_iso: str, user_id: int | None = None
    ) -> list[PlatformPostRecord]:
        """platform_posts rows for `platform` that TikTok has already
        accepted (status = PUBLISHING AND platform_post_id IS NOT NULL) and
        are due for another status check (next_status_check_at IS NULL OR
        next_status_check_at <= now_iso) — reconciliation.py's routine
        polling candidates (Milestone 2.1.10), deliberately distinct from
        both get_due_platform_posts (PENDING-only — work never yet
        submitted) and get_recoverable_platform_posts (staleness-gated
        safety net for an abandoned/crashed claim, which does not require
        platform_post_id to be set at all — see crash_recovery.py's Case
        A/B split). A row here is never re-submitted, only re-polled — see
        publish_tiktok._resolve_poll_outcome, the one shared mapping every
        status-check caller (this module, crash_recovery.py's Case B, and
        the inline post-submission poll) applies.

        NULL next_status_check_at is immediately eligible — same
        NULL-means-no-gate convention get_due_platform_posts already uses
        for next_retry_at — covering any row that predates this milestone's
        migration and has never had a check scheduled for it yet.

        now_iso must be an aware UTC isoformat string, matching how
        next_status_check_at/updated_at are always written — the same
        convention get_recoverable_platform_posts already requires (and a
        deliberately different one from get_due_platform_posts' naive-
        local-time now_iso). Ordered oldest-due-for-a-check first, ties
        broken by id. Pure read — never mutates a row.

        user_id (Milestone 3.2) is optional; see get_due_platform_posts'
        docstring — same scoping contract.
        """
        if user_id is None:
            rows = self._conn.execute(
                "SELECT * FROM platform_posts WHERE platform = ? AND status = 'PUBLISHING' "
                "AND platform_post_id IS NOT NULL "
                "AND (next_status_check_at IS NULL OR next_status_check_at <= ?) "
                "ORDER BY next_status_check_at ASC, id ASC",
                (platform, now_iso),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM platform_posts WHERE platform = ? AND status = 'PUBLISHING' "
                "AND platform_post_id IS NOT NULL "
                "AND (next_status_check_at IS NULL OR next_status_check_at <= ?) "
                "AND user_id = ? "
                "ORDER BY next_status_check_at ASC, id ASC",
                (platform, now_iso, user_id),
            ).fetchall()
        return [_row_to_platform_post(row) for row in rows]

    def update_platform_post_if_unchanged(
        self, post_id: int, expected_updated_at: str, updated_at: str, user_id: int | None = None, **fields
    ) -> bool:
        """Optimistic-concurrency update: apply fields (plus updated_at)
        only if the row's updated_at still equals expected_updated_at —
        i.e. only if nothing has touched it since the caller last read it.
        Returns True if this call performed the update, False if the row
        had already changed (or didn't exist) — never raises merely
        because it lost a race.

        Milestone 2.1.5 (crash recovery): every write in this codebase
        that mutates a platform_posts row also bumps updated_at (claim,
        submission, poll outcomes, materialization is insert-only) — so
        updated_at already serves as a de facto version/CAS token with no
        new column needed. Used by crash_recovery.py so it can act only on
        a row that is still the exact stale record it inspected, never on
        one an active worker resumed and already moved on.

        Milestone 3.2 (ownership): user_id is optional (keyword-only in
        practice — always pass fields by keyword, as every existing caller
        already does); when provided, the WHERE clause also requires
        `user_id = ?`, the same defense-in-depth pattern as
        claim_platform_post. A caller scoped to the wrong user simply loses
        the compare-and-swap, exactly like any other lost race.
        """
        fields = {**fields, "updated_at": updated_at}
        columns = ", ".join(f"{key} = ?" for key in fields)
        if user_id is None:
            values = [*fields.values(), post_id, expected_updated_at]
            cur = self._conn.execute(
                f"UPDATE platform_posts SET {columns} WHERE id = ? AND updated_at = ?", values
            )
        else:
            values = [*fields.values(), post_id, expected_updated_at, user_id]
            cur = self._conn.execute(
                f"UPDATE platform_posts SET {columns} WHERE id = ? AND updated_at = ? AND user_id = ?", values
            )
        return cur.rowcount > 0

    # -- users / auth_identities / platform_connections (Milestone 3.2) -

    def create_user(self, email: str, display_name: str | None, created_at: str) -> UserRecord:
        """Raises sqlite3.IntegrityError if email already exists (UNIQUE) —
        callers that want get-or-create semantics should use
        get_or_create_local_user() or check get_user_by_email() first."""
        self._conn.execute(
            "INSERT INTO users (email, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (email, display_name, created_at, created_at),
        )
        return self.get_user_by_email(email)

    def get_user(self, user_id: int) -> UserRecord | None:
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> UserRecord | None:
        row = self._conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _row_to_user(row) if row else None

    def get_or_create_local_user(self) -> UserRecord:
        """Resolve the single local bootstrap user (LOCAL_BOOTSTRAP_USER_EMAIL),
        creating it on first call. This is the transitional bridge every CLI
        entry point uses until a real authenticated multi-user API exists
        (Milestone 3.2's Phase 9/16) — analogous to
        calendar_manager.resolve_app_calendar's "reuse via persisted state,
        create if missing" pattern, applied to the local user identity
        instead of the dedicated Calendar. Idempotent: calling this
        repeatedly against the same database always returns the same user,
        never creates a second one (get_user_by_email is checked first)."""
        existing = self.get_user_by_email(LOCAL_BOOTSTRAP_USER_EMAIL)
        if existing is not None:
            return existing
        now = _utc_now_iso()
        return self.create_user(LOCAL_BOOTSTRAP_USER_EMAIL, "Local Bootstrap User", now)

    def create_auth_identity(
        self, user_id: int, provider: str, provider_subject: str, provider_email: str | None, created_at: str,
    ) -> AuthIdentityRecord:
        """Raises sqlite3.IntegrityError if (provider, provider_subject) is
        already linked to a user (UNIQUE) — a given external identity can
        never map to two different Pickle Batch accounts."""
        cur = self._conn.execute(
            "INSERT INTO auth_identities (user_id, provider, provider_subject, provider_email, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, provider, provider_subject, provider_email, created_at, created_at),
        )
        row = self._conn.execute("SELECT * FROM auth_identities WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_auth_identity(row)

    def get_user_by_auth_identity(self, provider: str, provider_subject: str) -> UserRecord | None:
        row = self._conn.execute(
            "SELECT users.* FROM users JOIN auth_identities ON auth_identities.user_id = users.id "
            "WHERE auth_identities.provider = ? AND auth_identities.provider_subject = ?",
            (provider, provider_subject),
        ).fetchone()
        return _row_to_user(row) if row else None

    def create_platform_connection(
        self, user_id: int, platform: str, external_account_id: str | None, status: str, created_at: str,
    ) -> PlatformConnectionRecord:
        """Raises sqlite3.IntegrityError if (user_id, platform) already has a
        connection (UNIQUE — one connection per platform per user for V1,
        see SCHEMA_PLATFORM_CONNECTIONS). Callers that want get-or-create
        semantics should use get_or_create_platform_connection()."""
        cur = self._conn.execute(
            "INSERT INTO platform_connections (user_id, platform, external_account_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, platform, external_account_id, status, created_at, created_at),
        )
        row = self._conn.execute("SELECT * FROM platform_connections WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_platform_connection(row)

    def get_platform_connection(self, user_id: int, platform: str) -> PlatformConnectionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM platform_connections WHERE user_id = ? AND platform = ?", (user_id, platform)
        ).fetchone()
        return _row_to_platform_connection(row) if row else None

    def get_or_create_platform_connection(
        self, user_id: int, platform: str, external_account_id: str | None = None,
    ) -> PlatformConnectionRecord:
        """Resolve the (user_id, platform) connection, creating it (status
        ACTIVE) on first call. Idempotent like get_or_create_local_user().
        external_account_id is only applied on creation — an existing
        connection's external_account_id is never overwritten by a later
        get_or_create call, matching insert_slot_if_missing/
        insert_platform_post_if_missing's "the row already there always
        wins" convention elsewhere in this store."""
        existing = self.get_platform_connection(user_id, platform)
        if existing is not None:
            return existing
        now = _utc_now_iso()
        return self.create_platform_connection(user_id, platform, external_account_id, "ACTIVE", now)

    def update_platform_connection_external_account(
        self, connection_id: int, external_account_id: str | None, updated_at: str,
    ) -> None:
        """Milestone 4.1: record which platform account a connection is
        currently authorized as. A reconnect may pick a different account
        (Instagram lets the user choose), and get_or_create_platform_connection
        deliberately never rewrites an existing row, so the OAuth callback
        calls this after every successful exchange. Unconditional, like
        update_platform_connection_status — driven by one explicit user
        action."""
        self._conn.execute(
            "UPDATE platform_connections SET external_account_id = ?, updated_at = ? WHERE id = ?",
            (external_account_id, updated_at, connection_id),
        )

    def update_platform_connection_status(self, connection_id: int, status: str, updated_at: str) -> None:
        """Used by the hosted OAuth connect/disconnect flow (Milestone
        3.6) — e.g. DISCONNECTED on disconnect, back to ACTIVE on a
        reconnect. Unconditional (no CAS) — status transitions here are
        always driven by one explicit, authenticated user action at a
        time, not a background refresh race like platform_credentials'
        update_platform_credential_if_unchanged."""
        self._conn.execute(
            "UPDATE platform_connections SET status = ?, updated_at = ? WHERE id = ?",
            (status, updated_at, connection_id),
        )

    # -- platform_credentials / oauth_states (Milestone 3.6) -------------

    def get_platform_credential(self, platform_connection_id: int) -> PlatformCredentialRecord | None:
        row = self._conn.execute(
            "SELECT * FROM platform_credentials WHERE platform_connection_id = ?", (platform_connection_id,)
        ).fetchone()
        return _row_to_platform_credential(row) if row else None

    def upsert_platform_credential(
        self, platform_connection_id: int, encrypted_payload: str, now: str,
    ) -> PlatformCredentialRecord:
        """Create-or-unconditionally-overwrite a connection's stored
        credential. Used by the OAuth connect flow (a fresh, explicit
        user-initiated authorization always wins, exactly like
        tiktok_auth.save_token()'s own unconditional overwrite) — never
        used by the refresh path, which must use
        update_platform_credential_if_unchanged instead so a concurrent
        refresh can't silently clobber a newer one."""
        existing = self.get_platform_credential(platform_connection_id)
        if existing is None:
            self._conn.execute(
                "INSERT INTO platform_credentials (platform_connection_id, encrypted_payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (platform_connection_id, encrypted_payload, now, now),
            )
        else:
            self._conn.execute(
                "UPDATE platform_credentials SET encrypted_payload = ?, updated_at = ? WHERE platform_connection_id = ?",
                (encrypted_payload, now, platform_connection_id),
            )
        return self.get_platform_credential(platform_connection_id)

    def update_platform_credential_if_unchanged(
        self, platform_connection_id: int, encrypted_payload: str, expected_updated_at: str, new_updated_at: str,
    ) -> bool:
        """Optimistic-concurrency (CAS) update for the refresh path — the
        same update_platform_post_if_unchanged pattern (compare-and-swap on
        updated_at) used elsewhere in this store, so two concurrent hosted
        requests refreshing the same expired TikTok credential can't both
        win: the second writer's expected_updated_at is stale by the time it
        writes, its update affects zero rows, and it re-reads instead of
        overwriting an already-refreshed (and possibly already-rotated-away)
        token. Returns True if this call's write won."""
        cur = self._conn.execute(
            "UPDATE platform_credentials SET encrypted_payload = ?, updated_at = ? "
            "WHERE platform_connection_id = ? AND updated_at = ?",
            (encrypted_payload, new_updated_at, platform_connection_id, expected_updated_at),
        )
        return cur.rowcount > 0

    @contextmanager
    def credential_refresh_lock(self, platform_connection_id: int):
        """Milestone 3.13: serialize token refreshes for one connection
        across processes. A no-op on SQLite — the local/single-process
        backend has no second worker to coordinate with, and the local CLI
        never uses this path (auth.py's own token file + fcntl lock). The
        CAS in update_platform_credential_if_unchanged still guards the
        write. See PostgresContentStore.credential_refresh_lock."""
        yield

    def delete_platform_credential(self, platform_connection_id: int) -> None:
        """Used by disconnect (Phase 20) — removes only the credential
        secret, never the platform_connections row itself (which retains
        identity/status history; its status is set to DISCONNECTED by the
        caller, not deleted)."""
        self._conn.execute(
            "DELETE FROM platform_credentials WHERE platform_connection_id = ?", (platform_connection_id,)
        )

    def create_oauth_state(
        self, user_id: int, platform: str, state: str, code_verifier: str, redirect_uri: str,
        created_at: str, expires_at: str, return_target: str | None = None,
    ) -> OAuthStateRecord:
        """Raises sqlite3.IntegrityError on a state collision (UNIQUE) —
        astronomically unlikely (state is secrets.token_urlsafe-generated)
        but fails loudly rather than silently reusing another attempt's
        row. return_target (Milestone 4.1) must already be allowlisted by
        the caller; this layer stores it verbatim."""
        cur = self._conn.execute(
            "INSERT INTO oauth_states "
            "(user_id, platform, state, code_verifier, redirect_uri, created_at, expires_at, return_target) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, platform, state, code_verifier, redirect_uri, created_at, expires_at, return_target),
        )
        row = self._conn.execute("SELECT * FROM oauth_states WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_oauth_state(row)

    def consume_oauth_state(self, state: str, now: str, *, platform: str) -> OAuthStateRecord | None:
        """Atomically marks a pending OAuth state consumed and returns the
        row that was consumed — or None if `state` doesn't exist, belongs
        to a different platform, was already consumed (replay), or is past
        expires_at.

        Milestone 4.1: one conditional UPDATE is the whole decision — every
        condition (state, platform, not yet consumed, not expired) is in its
        WHERE clause, so two concurrent callbacks presenting the same state
        can't both win (the loser's UPDATE matches zero rows), and a state
        created for one platform is never consumed, or even marked, by
        another platform's callback. The row is read back only after this
        call's write won."""
        cur = self._conn.execute(
            "UPDATE oauth_states SET consumed_at = ? "
            "WHERE state = ? AND platform = ? AND consumed_at IS NULL AND expires_at > ?",
            (now, state, platform, now),
        )
        if cur.rowcount == 0:
            return None
        row = self._conn.execute("SELECT * FROM oauth_states WHERE state = ?", (state,)).fetchone()
        return _row_to_oauth_state(row)

    # -- upload_batches / upload_attempts (Milestone 3.7 follow-up —
    # upload performance instrumentation; see SCHEMA_UPLOAD_BATCHES'
    # module-level comment for why this is event/attempt-level rather than
    # columns on videos) -----------------------------------------------

    def create_upload_batch(self, user_id: int, started_at: str, file_count: int) -> UploadBatchRecord:
        """One row per POST /api/videos request. total_bytes/
        total_duration_ms/status are filled in by update_upload_batch once
        every file has been attempted — this call only marks that the
        batch started."""
        cur = self._conn.execute(
            "INSERT INTO upload_batches (user_id, started_at, file_count) VALUES (?, ?, ?)",
            (user_id, started_at, file_count),
        )
        row = self._conn.execute("SELECT * FROM upload_batches WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_upload_batch(row)

    def update_upload_batch(self, batch_id: int, **fields) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = [*fields.values(), batch_id]
        self._conn.execute(f"UPDATE upload_batches SET {columns} WHERE id = ?", values)

    def list_upload_batches_for_user(self, user_id: int) -> list[UploadBatchRecord]:
        """Scoped strictly to user_id — same tenant-isolation convention as
        list_videos_for_user. No "list everything" variant."""
        rows = self._conn.execute(
            "SELECT * FROM upload_batches WHERE user_id = ? ORDER BY started_at DESC, id DESC", (user_id,)
        ).fetchall()
        return [_row_to_upload_batch(row) for row in rows]

    def create_upload_attempt(
        self, batch_id: int, user_id: int, original_filename: str, started_at: str,
    ) -> UploadAttemptRecord:
        """One row per file in a batch, created before that file's own
        processing starts — video_id/file_size_bytes/duration_ms/status/
        error_code are filled in by update_upload_attempt once the outcome
        (success or a specific failure) is known."""
        cur = self._conn.execute(
            "INSERT INTO upload_attempts (batch_id, user_id, original_filename, started_at) VALUES (?, ?, ?, ?)",
            (batch_id, user_id, original_filename, started_at),
        )
        row = self._conn.execute("SELECT * FROM upload_attempts WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_upload_attempt(row)

    def update_upload_attempt(self, attempt_id: int, **fields) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = [*fields.values(), attempt_id]
        self._conn.execute(f"UPDATE upload_attempts SET {columns} WHERE id = ?", values)

    def get_upload_attempts_for_batch(self, batch_id: int) -> list[UploadAttemptRecord]:
        rows = self._conn.execute(
            "SELECT * FROM upload_attempts WHERE batch_id = ? ORDER BY id ASC", (batch_id,)
        ).fetchall()
        return [_row_to_upload_attempt(row) for row in rows]

    def delete_video(self, video_id: int) -> None:
        """Delete a videos row (Milestone 3.7 follow-up — Delete Video).
        Callers (media.media_storage.delete_video) are responsible for the
        actual safety checks (ownership, no assigned_slot_id, no
        platform_posts row) and for deleting the backing storage object
        first — this method only does the DB half, and does it
        unconditionally.

        upload_attempts.video_id is nullable specifically for this case
        (see SCHEMA_UPLOAD_ATTEMPTS's own comment): each attempt row that
        created/touched this video has its video_id cleared rather than
        being deleted, so the batch's per-file timing/success/failure
        telemetry survives for analytics — "preserve unrelated historical
        telemetry" per the brief this shipped under — while never leaving
        a dangling FK to a video that no longer exists. upload_batches is
        untouched: a batch is never about a single video.

        content_slots/platform_posts are not touched here — the caller
        must already have verified neither references this video before
        calling this method at all, so there is nothing to null out."""
        with self.transaction() as conn:
            conn.execute("UPDATE upload_attempts SET video_id = NULL WHERE video_id = ?", (video_id,))
            # Derived caption metadata (Milestone 3.10.1) goes with its video.
            conn.execute("DELETE FROM video_hashtags WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))


class SlotUnavailableError(Exception):
    """Raised when a slot is claimed between selection and assignment."""


class OwnershipMismatchError(Exception):
    """Raised by assign_slot() (Milestone 3.2) when a video and the
    content_slot it's being assigned to are both explicitly owned by
    different users. See assign_slot's docstring."""


class CredentialRefreshLockTimeout(Exception):
    """Raised by credential_refresh_lock() (Milestone 3.13, Postgres only)
    when another process held the connection's refresh lock for longer
    than config.CREDENTIAL_REFRESH_LOCK_TIMEOUT_SECONDS."""


class PlatformPostInProgressError(Exception):
    """Raised by unassign_slot() (Milestone 3.9) when the assigned video's
    platform_posts row is no longer PENDING. See unassign_slot's
    docstring."""


def _raise_missing(lastrowid: int):
    raise RuntimeError(f"insert succeeded but row {lastrowid} could not be re-read")
