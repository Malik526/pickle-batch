"""Instagram Reels publishing through the real hosted pipeline (Milestone
4.2): queue assignment → hosted worker claim → validation → signed URL →
container → status checks → media_publish → PUBLISHED, plus every restart /
ambiguity / retry path and the duplicate-prevention rules of
scheduling/finalization.py.

Runs on SQLite and real Postgres (throwaway schema; Postgres skipped without
DATABASE_URL). Meta is never called: the publisher is a scripted fake
speaking the shared Publisher contract (the real InstagramPublisher's Meta
mapping is covered in test_instagram_content_publishing.py). Storage is a
LocalStorage that issues fake signed URLs, so no bucket is touched."""

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest

from content_automation.config import DATABASE_URL, PLATFORM_POST_STALE_MINUTES, POSTGRES_SCHEMA, POSTGRES_TEST_SCHEMA
from content_automation.media.caption_editing import save_caption
from content_automation.media.media_storage import create_video_from_upload
from content_automation.persistence.content_store import ContentStore
from content_automation.publishing.publish_status import (
    post_retry_requires_confirmation,
    resolve_slot_publish_status,
)
from content_automation.publishing.publisher import (
    STATUS_FAILED,
    STATUS_PUBLISH_COMPLETE,
    STATUS_READY_TO_FINALIZE,
    FinalizeResult,
    PublishError,
    PublishResult,
    PublishStatusResult,
)
from content_automation.scheduling.finalization import finalize_ready_submission
from content_automation.scheduling.hosted_worker import run_hosted_cycle
from content_automation.scheduling.manual_recovery import RetryRejectedError, retry_platform_post, retry_slot_posts
from content_automation.scheduling.queue_assignment import (
    InvalidPlatformSelectionError,
    PlatformNotConnectedError,
    assign_video_to_next_open_slot,
    assign_video_to_slot,
)
from content_automation.storage.local import LocalStorage

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg/ffprobe not on PATH")

PG_SCHEMA = f"{POSTGRES_TEST_SCHEMA}_instagram"
assert PG_SCHEMA != POSTGRES_SCHEMA
NEEDS_PG = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not configured — Postgres integration tests skipped")
NY = ZoneInfo("America/New_York")
SIGNED_TOKEN = "eyJ-signed-url-secret"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local(minutes_from_now: int) -> str:
    moment = datetime.now(timezone.utc).astimezone(NY) + timedelta(minutes=minutes_from_now)
    return moment.replace(second=0, microsecond=0, tzinfo=None).isoformat()


# --- fixtures ------------------------------------------------------------------------

def _make_clip(path: Path, *, seconds: int, size: str = "720x1280") -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=30",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-c:a", "aac",
         "-shortest", "-loglevel", "error", str(path)],
        check=True,
    )
    return path


@pytest.fixture(scope="module")
def reel_file(tmp_path_factory):
    return _make_clip(tmp_path_factory.mktemp("media") / "reel.mp4", seconds=4)


@pytest.fixture(scope="module")
def short_file(tmp_path_factory):
    return _make_clip(tmp_path_factory.mktemp("media") / "short.mp4", seconds=1)


@pytest.fixture(scope="module")
def _pg_schema():
    if not DATABASE_URL:
        yield None
        return
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA IF EXISTS "{PG_SCHEMA}" CASCADE')
        admin.execute(f'CREATE SCHEMA "{PG_SCHEMA}"')
    yield PG_SCHEMA
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA IF EXISTS "{PG_SCHEMA}" CASCADE')


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=NEEDS_PG)])
def store(request, tmp_path, _pg_schema):
    if request.param == "sqlite":
        with ContentStore(db_path=tmp_path / "test.db") as s:
            yield s
        return
    from content_automation.persistence.postgres_content_store import PostgresContentStore

    with PostgresContentStore(dsn=DATABASE_URL, schema=PG_SCHEMA) as s:
        s._conn.execute("TRUNCATE users, videos, content_slots, platform_posts RESTART IDENTITY CASCADE")
        yield s


class SigningStorage(LocalStorage):
    """LocalStorage plus fake signed URLs (a real deployment uses SupabaseStorage)."""

    def __init__(self, root):
        super().__init__(root=root)
        self.signed = []

    def create_signed_url(self, key: str, expires_in_seconds: int) -> str:
        self.signed.append((key, expires_in_seconds))
        return f"https://storage.example/sign/{key}?token={SIGNED_TOKEN}"


@pytest.fixture
def storage(tmp_path):
    return SigningStorage(tmp_path / "objects")


@dataclass
class FakeInstagram:
    """A scripted Instagram publisher. `statuses` are returned by successive
    get_status calls (the last one repeats); finalize_results likewise."""
    statuses: list = field(default_factory=lambda: ["IN_PROGRESS", STATUS_READY_TO_FINALIZE])
    finalize_results: list = field(default_factory=lambda: ["media-1"])
    create_error: Exception | None = None
    error_after_container: Exception | None = None
    creates: list = field(default_factory=list)
    urls: list = field(default_factory=list)
    captions: list = field(default_factory=list)
    status_calls: list = field(default_factory=list)
    finalize_calls: list = field(default_factory=list)
    reports_platform_post_id_before_media_transfer = True
    requires_finalize = True

    def publish(self, video_path, caption, on_platform_post_id=None, *, media_url=None):
        self.urls.append(media_url)
        self.captions.append(caption)
        if self.create_error is not None:
            raise self.create_error
        container_id = f"container-{len(self.creates) + 1}"
        self.creates.append(container_id)
        on_platform_post_id(container_id)
        if self.error_after_container is not None:
            raise self.error_after_container
        return PublishResult(platform_post_id=container_id, status="IN_PROGRESS")

    def get_status(self, platform_post_id):
        self.status_calls.append(platform_post_id)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, Exception):
            raise status
        if isinstance(status, PublishStatusResult):
            return status
        return PublishStatusResult(status=status)

    def finalize(self, platform_post_id):
        self.finalize_calls.append(platform_post_id)
        result = self.finalize_results.pop(0) if len(self.finalize_results) > 1 else self.finalize_results[0]
        if isinstance(result, Exception):
            raise result
        return FinalizeResult(platform_media_id=result)


def _user(store, email="creator@example.com", *, instagram=True, tiktok=False):
    user = store.create_user(email, None, _iso_now())
    store.create_auth_identity(user.id, "supabase", f"sub-{email}", email, _iso_now())
    if instagram:
        store.get_or_create_platform_connection(user.id, "instagram", external_account_id=f"ig-{user.id}")
    if tiktok:
        store.get_or_create_platform_connection(user.id, "tiktok", external_account_id=f"tt-{user.id}")
    return user


def _video(store, storage, user, clip: Path, tmp_path, name="reel"):
    upload = tmp_path / f"upload-{name}-{user.id}.mp4"
    shutil.copy(clip, upload)
    return create_video_from_upload(
        store, storage, user.id, local_path=upload, original_filename=f"{name}.mp4",
        file_hash=f"hash-{name}-{user.id}", file_size_bytes=upload.stat().st_size, created_at=_iso_now(),
    )


def _slot(store, user, minutes_from_now=-5):
    scheduled_at = _local(minutes_from_now)
    store.insert_slot_if_missing(scheduled_at, None, None, _iso_now(), user_id=user.id, timezone="America/New_York")
    return next(s for s in store.list_content_slots_for_user(user.id, "2000-01-01T00:00:00", "2200-01-01T00:00:00")
                if s.scheduled_at == scheduled_at)


def _instagram_post(store, storage, user, clip, tmp_path, *, caption="Made with Pickle Batch #reels", name="reel",
                    minutes_from_now=-5):
    video = _video(store, storage, user, clip, tmp_path, name=name)
    slot = _slot(store, user, minutes_from_now)
    assign_video_to_slot(store, video.id, slot.id, _iso_now(), user_id=user.id, platforms=["instagram"])
    if caption is not None:
        save_caption(store, store.get_video(video.id), caption)
    return store.get_platform_post(video.id, "instagram")


def _cycle(store, storage, instagram, *, minutes_later=0, tiktok=None):
    """One hosted worker cycle; `instagram`/`tiktok` map user_id -> fake publisher."""
    factories = {"instagram": lambda _s, uid: instagram[uid]}
    if tiktok is not None:
        factories["tiktok"] = lambda _s, uid: tiktok[uid]
    return run_hosted_cycle(
        store, storage, publisher_factories=factories,
        now_utc=datetime.now(timezone.utc) + timedelta(minutes=minutes_later),
    )


def _row(store, post):
    return store.get_platform_post(post.video_id, "instagram")


# --- happy path ------------------------------------------------------------------------

def test_reel_is_created_processed_and_published_exactly_once(store, storage, reel_file, tmp_path, caplog):
    caplog.set_level("INFO")
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    fake = FakeInstagram()

    first = _cycle(store, storage, {user.id: fake})
    row = _row(store, post)
    assert first.claimed == 1
    assert (row.status, row.platform_post_id, row.submission_state) == ("PUBLISHING", "container-1", None)
    assert row.next_status_check_at is not None  # processing → scheduled for reconciliation

    _cycle(store, storage, {user.id: fake}, minutes_later=2)
    row = _row(store, post)
    assert row.status == "PUBLISHED"
    assert (row.platform_post_id, row.platform_media_id) == ("container-1", "media-1")  # both ids kept
    assert row.submission_state is None and row.published_at
    assert fake.creates == ["container-1"] and fake.finalize_calls == ["container-1"]
    assert fake.captions == ["Made with Pickle Batch #reels"]

    # The signed URL was issued once, right before submission, and is never persisted or logged.
    assert len(storage.signed) == 1 and storage.signed[0][1] == 3600
    assert SIGNED_TOKEN in fake.urls[0]
    assert SIGNED_TOKEN not in repr(row) and SIGNED_TOKEN not in caplog.text
    assert "instagram_post_published" in caplog.text and "media_id=media-1" in caplog.text
    assert resolve_slot_publish_status(store.get_slot(store.get_video(post.video_id).assigned_slot_id), [row]).display_status == "PUBLISHED"


def test_captionless_reel_publishes_without_a_caption(store, storage, reel_file, tmp_path):
    user = _user(store)
    _instagram_post(store, storage, user, reel_file, tmp_path, caption=None)
    fake = FakeInstagram()
    _cycle(store, storage, {user.id: fake})
    assert fake.captions == [None]


# --- failures before a container exists ------------------------------------------------------

def test_timeout_creating_the_container_retries_without_any_container_id(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    fake = FakeInstagram(create_error=PublishError("timed out", reason_code="NETWORK_ERROR"))

    summary = _cycle(store, storage, {user.id: fake})

    row = _row(store, post)
    assert summary.retry_scheduled == 1
    assert (row.status, row.platform_post_id, row.submission_state, row.retry_count) == ("PENDING", None, None, 1)
    assert row.failure_code == "NETWORK_ERROR"


def test_unsupported_media_fails_before_any_instagram_call(store, storage, short_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, short_file, tmp_path)
    fake = FakeInstagram()

    _cycle(store, storage, {user.id: fake})

    row = _row(store, post)
    assert (row.status, row.failure_code) == ("FAILED", "INSTAGRAM_MEDIA_DURATION")
    assert fake.creates == [] and storage.signed == []


def test_caption_over_instagram_limits_fails_before_any_instagram_call(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path, caption=" ".join(f"#tag{i}" for i in range(31)))
    fake = FakeInstagram()
    _cycle(store, storage, {user.id: fake})
    assert _row(store, post).failure_code == "INSTAGRAM_CAPTION_TOO_MANY_HASHTAGS"
    assert fake.creates == []


def test_storage_that_cannot_sign_urls_fails_clearly(store, reel_file, tmp_path):
    user = _user(store)
    local = LocalStorage(root=tmp_path / "objects")
    post = _instagram_post(store, local, user, reel_file, tmp_path)
    fake = FakeInstagram()
    _cycle(store, local, {user.id: fake})
    row = _row(store, post)
    assert (row.status, row.failure_code, row.platform_post_id) == ("FAILED", "STORAGE_UNAVAILABLE", None)
    assert fake.creates == []


# --- restarts and ambiguity: never a second container ------------------------------------

def test_error_after_container_creation_keeps_the_container_and_never_creates_another(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    fake = FakeInstagram(error_after_container=PublishError("connection reset", reason_code="NETWORK_ERROR"),
                         statuses=[STATUS_READY_TO_FINALIZE])

    _cycle(store, storage, {user.id: fake})
    row = _row(store, post)
    assert (row.status, row.platform_post_id) == ("PUBLISHING", "container-1")

    fake.error_after_container = None
    _cycle(store, storage, {user.id: fake}, minutes_later=2)
    assert _row(store, post).status == "PUBLISHED"
    assert fake.creates == ["container-1"]  # resumed the persisted container


def test_worker_restart_during_processing_resumes_the_persisted_container(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    # State left by a worker that died after persisting the container id.
    stale = (datetime.now(timezone.utc) - timedelta(minutes=PLATFORM_POST_STALE_MINUTES + 5)).isoformat()
    store.claim_platform_post(post.id, updated_at=stale, user_id=user.id)
    store.update_platform_post(post.id, updated_at=stale, platform_post_id="container-9")
    fake = FakeInstagram(statuses=[STATUS_READY_TO_FINALIZE])

    _cycle(store, storage, {user.id: fake})

    row = _row(store, post)
    assert (row.status, row.platform_media_id) == ("PUBLISHED", "media-1")
    assert fake.creates == [] and fake.finalize_calls == ["container-9"]


def test_worker_restart_before_the_container_id_was_saved_retries_safely(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=PLATFORM_POST_STALE_MINUTES + 5)).isoformat()
    store.claim_platform_post(post.id, updated_at=stale, user_id=user.id)
    store.update_platform_post(post.id, updated_at=stale, submission_state="AWAITING_PLATFORM_ID")

    _cycle(store, storage, {user.id: FakeInstagram()})

    row = _row(store, post)
    # No container id was ever saved, so nothing can be posted: bounded retry.
    assert (row.status, row.failure_code, row.retry_count) == ("PENDING", "SUBMISSION_INTERRUPTED", 1)


def test_ambiguous_media_publish_is_resolved_from_container_status_without_publishing_again(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    fake = FakeInstagram(
        statuses=[STATUS_READY_TO_FINALIZE, STATUS_READY_TO_FINALIZE, STATUS_PUBLISH_COMPLETE],
        finalize_results=[PublishError("read timed out", reason_code="NETWORK_ERROR")],
    )

    _cycle(store, storage, {user.id: fake})  # create → inline status READY → finalize times out
    row = _row(store, post)
    assert (row.status, row.submission_state) == ("PUBLISHING", "PUBLISH_REQUESTED")

    _cycle(store, storage, {user.id: fake}, minutes_later=2)  # still READY, inside grace: wait
    assert _row(store, post).submission_state == "PUBLISH_REQUESTED"

    _cycle(store, storage, {user.id: fake}, minutes_later=10)  # container now PUBLISHED
    row = _row(store, post)
    assert (row.status, row.submission_state, row.platform_media_id) == ("PUBLISHED", None, None)
    assert fake.finalize_calls == ["container-1"] and fake.creates == ["container-1"]


def test_unconfirmed_publish_is_parked_unknown_and_retried_only_on_the_same_container(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    fake = FakeInstagram(statuses=[STATUS_READY_TO_FINALIZE],
                         finalize_results=[PublishError("read timed out", reason_code="NETWORK_ERROR"), "media-2"])

    _cycle(store, storage, {user.id: fake})
    _cycle(store, storage, {user.id: fake}, minutes_later=PLATFORM_POST_STALE_MINUTES + 5)
    row = _row(store, post)
    assert (row.status, row.failure_code, row.platform_post_id) == ("UNKNOWN", "PUBLISH_OUTCOME_UNKNOWN", "container-1")
    assert fake.finalize_calls == ["container-1"]  # never re-published automatically

    assert post_retry_requires_confirmation(row)
    with pytest.raises(RetryRejectedError) as rejected:
        retry_platform_post(store, row, user_id=user.id)
    assert rejected.value.code == "CONFIRMATION_REQUIRED"

    retried = retry_platform_post(store, row, user_id=user.id, confirm_not_published=True)
    assert (retried.status, retried.platform_post_id, retried.submission_state) == ("PUBLISHING", "container-1", None)

    _cycle(store, storage, {user.id: fake}, minutes_later=PLATFORM_POST_STALE_MINUTES + 10)
    row = _row(store, post)
    assert (row.status, row.platform_media_id) == ("PUBLISHED", "media-2")
    assert fake.creates == ["container-1"]  # still the one container


def test_rejected_media_publish_clears_the_checkpoint_and_tries_again(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    fake = FakeInstagram(statuses=[STATUS_READY_TO_FINALIZE], finalize_results=[
        PublishError("not ready", reason_code="INSTAGRAM_MEDIA_NOT_READY", http_status=400), "media-3"])

    _cycle(store, storage, {user.id: fake})
    row = _row(store, post)
    assert (row.status, row.submission_state) == ("PUBLISHING", None)  # Meta answered: nothing posted

    _cycle(store, storage, {user.id: fake}, minutes_later=2)
    assert _row(store, post).platform_media_id == "media-3"
    assert fake.finalize_calls == ["container-1", "container-1"]


def test_auth_rejection_at_publish_is_unknown_and_rechecks_without_confirmation(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    fake = FakeInstagram(statuses=[STATUS_READY_TO_FINALIZE], finalize_results=[
        PublishError("token expired", reason_code="REAUTHORIZATION_REQUIRED", http_status=400), "media-4"])

    _cycle(store, storage, {user.id: fake})
    row = _row(store, post)
    assert (row.status, row.failure_code, row.submission_state) == ("UNKNOWN", "REAUTHORIZATION_REQUIRED", None)
    assert not post_retry_requires_confirmation(row)

    retry_platform_post(store, row, user_id=user.id)
    _cycle(store, storage, {user.id: fake}, minutes_later=2)
    assert _row(store, post).platform_media_id == "media-4"
    assert fake.creates == ["container-1"]


def test_failed_container_allows_a_deliberate_new_container(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    fake = FakeInstagram(statuses=[PublishStatusResult(status=STATUS_FAILED, failure_code="INSTAGRAM_CONTAINER_ERROR",
                                                       failure_reason="Error: media could not be fetched")])

    _cycle(store, storage, {user.id: fake})
    row = _row(store, post)
    assert (row.status, row.failure_code, row.platform_post_id) == ("FAILED", "INSTAGRAM_CONTAINER_ERROR", "container-1")
    assert not post_retry_requires_confirmation(row)

    retry_platform_post(store, row, user_id=user.id)  # the user's deliberate retry
    fake.statuses = [STATUS_READY_TO_FINALIZE]
    _cycle(store, storage, {user.id: fake}, minutes_later=5)
    row = _row(store, post)
    assert (row.status, row.platform_post_id) == ("PUBLISHED", "container-2")
    assert fake.creates == ["container-1", "container-2"] and len(storage.signed) == 2  # fresh URL per container


def test_concurrent_finalize_attempts_publish_once(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    store.claim_platform_post(post.id, updated_at=_iso_now(), user_id=user.id)
    store.update_platform_post(post.id, updated_at=_iso_now(), platform_post_id="container-1")
    snapshot = _row(store, post)  # two processes read the same row
    fake = FakeInstagram()

    assert finalize_ready_submission(store, snapshot, fake, user_id=user.id) == "PUBLISHED"
    assert finalize_ready_submission(store, snapshot, fake, user_id=user.id) == "SKIPPED"
    assert fake.finalize_calls == ["container-1"]


# --- status presentation ---------------------------------------------------------------

def test_queue_distinguishes_processing_from_publishing_on_instagram(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _instagram_post(store, storage, user, reel_file, tmp_path)
    store.claim_platform_post(post.id, updated_at=_iso_now(), user_id=user.id)
    store.update_platform_post(post.id, updated_at=_iso_now(), platform_post_id="container-1", next_status_check_at=_iso_now())
    slot = store.get_slot(store.get_video(post.video_id).assigned_slot_id)

    processing = resolve_slot_publish_status(slot, [_row(store, post)])
    assert processing.display_status == "PUBLISHING"
    assert processing.publications[0].platform == "instagram"
    assert processing.publications[0].message == "Instagram is processing the video…"
    assert processing.publications[0].stage == "PROCESSING"

    store.update_platform_post(post.id, updated_at=_iso_now(), submission_state="PUBLISH_REQUESTED")
    publishing = resolve_slot_publish_status(slot, [_row(store, post)]).publications[0]
    assert (publishing.message, publishing.stage) == ("Publishing to Instagram…", "PUBLISHING")


# --- assignment ------------------------------------------------------------------------

def test_explicit_platform_choice_creates_only_those_posts_and_requires_a_connection(store, storage, reel_file, tmp_path):
    connected = _user(store, "a@example.com", instagram=True)
    video = _video(store, storage, connected, reel_file, tmp_path)
    slot = _slot(store, connected, 60)

    assign_video_to_slot(store, video.id, slot.id, _iso_now(), user_id=connected.id, platforms=["instagram"])
    assert store.get_platform_post(video.id, "instagram") is not None
    assert store.get_platform_post(video.id, "tiktok") is None  # no TikTok post was created

    stranger = _user(store, "b@example.com", instagram=False)
    other = _video(store, storage, stranger, reel_file, tmp_path, name="other")
    with pytest.raises(PlatformNotConnectedError):
        assign_video_to_next_open_slot(store, other.id, _iso_now(), user_id=stranger.id, platforms=["instagram"])
    assert store.get_video(other.id).assigned_slot_id is None  # nothing changed

    with pytest.raises(InvalidPlatformSelectionError):
        assign_video_to_slot(store, other.id, slot.id, _iso_now(), user_id=stranger.id, platforms=["myspace"])
    with pytest.raises(InvalidPlatformSelectionError):
        assign_video_to_slot(store, other.id, slot.id, _iso_now(), user_id=stranger.id, platforms=[])


def test_default_assignment_is_unchanged_tiktok_only(store, storage, reel_file, tmp_path):
    user = _user(store)
    video = _video(store, storage, user, reel_file, tmp_path)
    assign_video_to_slot(store, video.id, _slot(store, user, 60).id, _iso_now(), user_id=user.id)
    assert store.get_platform_post(video.id, "tiktok") is not None
    assert store.get_platform_post(video.id, "instagram") is None


# --- dispatch, isolation, TikTok regression ---------------------------------------------------

@dataclass
class FakeTikTok:
    publishes: list = field(default_factory=list)

    def publish(self, video_path, caption):
        self.publishes.append(caption)
        return PublishResult(platform_post_id="tt-1", status="PROCESSING_UPLOAD")

    def get_status(self, platform_post_id):
        return PublishStatusResult(status="PUBLISH_COMPLETE")


def test_worker_dispatches_by_platform_and_isolates_failures(store, storage, reel_file, tmp_path):
    user = _user(store, tiktok=True)
    tiktok_video = _video(store, storage, user, reel_file, tmp_path, name="tt")
    assign_video_to_slot(store, tiktok_video.id, _slot(store, user, -6).id, _iso_now(), user_id=user.id, platforms=["tiktok"])
    instagram_post = _instagram_post(store, storage, user, reel_file, tmp_path)
    tiktok = FakeTikTok()

    def broken_instagram(_s, _uid):
        raise RuntimeError("instagram factory exploded")

    summary = run_hosted_cycle(store, storage, publisher_factories={"tiktok": lambda _s, uid: tiktok, "instagram": broken_instagram})

    assert store.get_platform_post(tiktok_video.id, "tiktok").status == "PUBLISHED"  # TikTok unaffected
    assert tiktok.publishes and summary.user_errors == [user.id]
    assert _row(store, instagram_post).status == "PENDING"  # untouched, retried next cycle


def test_each_user_publishes_only_with_their_own_publisher(store, storage, reel_file, tmp_path):
    alice, bob = _user(store, "a@example.com"), _user(store, "b@example.com")
    a_post = _instagram_post(store, storage, alice, reel_file, tmp_path, name="a")
    b_post = _instagram_post(store, storage, bob, reel_file, tmp_path, name="b")
    fakes = {alice.id: FakeInstagram(statuses=[STATUS_READY_TO_FINALIZE], finalize_results=["media-a"]),
             bob.id: FakeInstagram(statuses=[STATUS_READY_TO_FINALIZE], finalize_results=["media-b"])}

    _cycle(store, storage, fakes)

    assert _row(store, a_post).platform_media_id == "media-a"
    assert _row(store, b_post).platform_media_id == "media-b"
    assert len(fakes[alice.id].creates) == 1 and len(fakes[bob.id].creates) == 1


# --- Milestone 4.2.2: retrying failures from before normalization existed -------------------

@pytest.fixture(scope="module")
def iphone_4k_file(tmp_path_factory):
    """An iPhone-style portrait 4K clip: coded 3840x2160 plus a rotation flag."""
    root = tmp_path_factory.mktemp("media4k")
    raw = root / "raw.mov"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=3840x2160:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(raw)],
        check=True,
    )
    clip = root / "iphone-portrait-4k.mov"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-c", "copy", "-metadata:s:v:0", "rotate=90", str(clip)],
                   check=True)
    return clip


def _legacy_failed_post(store, storage, user, clip, tmp_path, *, failure_code, name="legacy", width=3840, height=2160,
                        duration=4.0, platform_post_id=None, status="FAILED"):
    """A post exactly as M4.2 left it: never submitted, FAILED with a
    pre-normalization media code, the source's probe stored in its row."""
    post = _instagram_post(store, storage, user, clip, tmp_path, name=name)
    store.update_video(post.video_id, container="mov", video_codec="h264", audio_codec="aac", width=width, height=height,
                       fps=30.0, duration_seconds=duration)
    store.update_platform_post(post.id, updated_at=_iso_now(), status=status, platform_post_id=platform_post_id,
                               failure_code=failure_code, failure_reason=f"Video {post.video_id}: legacy {failure_code}")
    return _row(store, post)


def _slot_status(store, post):
    slot = store.get_slot(store.get_video(post.video_id).assigned_slot_id)
    return resolve_slot_publish_status(slot, store.list_platform_posts_for_video(post.video_id))


def test_legacy_too_wide_failure_retries_through_normalization_and_publishes(store, storage, iphone_4k_file, tmp_path):
    user = _user(store)
    post = _legacy_failed_post(store, storage, user, iphone_4k_file, tmp_path, failure_code="INSTAGRAM_MEDIA_RESOLUTION")
    assert _slot_status(store, post).can_retry

    [retried] = retry_slot_posts(store, store.list_platform_posts_for_video(post.video_id), user_id=user.id)
    assert (retried.status, retried.platform_post_id) == ("PENDING", None)
    status = _slot_status(store, post)
    assert status.display_status == "SCHEDULED" and status.message is None  # the old failure isn't the active state

    fake = FakeInstagram()
    _cycle(store, storage, {user.id: fake}, minutes_later=3)  # retry backoff elapsed → claimed, normalized, submitted
    _cycle(store, storage, {user.id: fake}, minutes_later=5)
    row = _row(store, post)
    assert (row.status, row.platform_media_id) == ("PUBLISHED", "media-1")
    [(signed_key, _ttl)] = storage.signed
    assert "/derived/instagram/reel-" in signed_key and SIGNED_TOKEN in fake.urls[0]  # Instagram got the derivative
    assert storage.exists(store.get_video(post.video_id).storage_key)  # source untouched, no re-upload
    assert fake.creates == ["container-1"]


def test_legacy_retry_reuses_an_existing_derivative(store, storage, iphone_4k_file, tmp_path, monkeypatch):
    from content_automation.publishing.instagram import normalization
    from content_automation.publishing.instagram.media_preparation import prepare_publishable_media

    user = _user(store)
    post = _legacy_failed_post(store, storage, user, iphone_4k_file, tmp_path, failure_code="INSTAGRAM_MEDIA_RESOLUTION")
    existing = prepare_publishable_media(store, storage, store.get_video(post.video_id))  # made by an earlier attempt
    monkeypatch.setattr(normalization, "normalize", lambda *a, **k: pytest.fail("must reuse the derivative"))

    retry_slot_posts(store, store.list_platform_posts_for_video(post.video_id), user_id=user.id)
    _cycle(store, storage, {user.id: FakeInstagram(statuses=[STATUS_READY_TO_FINALIZE])}, minutes_later=3)

    assert _row(store, post).status == "PUBLISHED"
    assert storage.signed[0][0] == existing.storage_key


def test_hard_duration_failure_is_still_not_retryable(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _legacy_failed_post(store, storage, user, reel_file, tmp_path, failure_code="INSTAGRAM_MEDIA_DURATION",
                               width=720, height=1280, duration=1.0)
    assert not _slot_status(store, post).can_retry
    with pytest.raises(RetryRejectedError) as rejected:
        retry_slot_posts(store, store.list_platform_posts_for_video(post.video_id), user_id=user.id)
    assert rejected.value.code == "NOT_RETRYABLE" and "retrying won't change that" in str(rejected.value)
    assert _row(store, post).status == "FAILED"  # nothing changed


def test_legacy_retry_with_an_existing_container_follows_container_recovery(store, storage, reel_file, tmp_path):
    user = _user(store)
    post = _legacy_failed_post(store, storage, user, reel_file, tmp_path, failure_code="REAUTHORIZATION_REQUIRED",
                               width=720, height=1280, platform_post_id="container-7", status="UNKNOWN")
    [retried] = retry_slot_posts(store, store.list_platform_posts_for_video(post.video_id), user_id=user.id)
    assert (retried.status, retried.platform_post_id) == ("PUBLISHING", "container-7")  # re-check, no new container

    fake = FakeInstagram(statuses=[STATUS_READY_TO_FINALIZE])
    _cycle(store, storage, {user.id: fake}, minutes_later=2)
    assert _row(store, post).status == "PUBLISHED"
    assert fake.creates == [] and fake.finalize_calls == ["container-7"] and storage.signed == []


def test_slot_retry_retries_every_failed_platform_and_respects_confirmation(store, storage, reel_file, tmp_path):
    user = _user(store, tiktok=True)
    video = _video(store, storage, user, reel_file, tmp_path, name="both")
    assign_video_to_slot(store, video.id, _slot(store, user, -6).id, _iso_now(), user_id=user.id, platforms=["tiktok", "instagram"])
    tiktok, instagram = store.get_platform_post(video.id, "tiktok"), store.get_platform_post(video.id, "instagram")
    store.update_platform_post(tiktok.id, updated_at=_iso_now(), status="FAILED", failure_code="NETWORK_ERROR")
    store.update_platform_post(instagram.id, updated_at=_iso_now(), status="UNKNOWN", failure_code="SUBMISSION_OUTCOME_UNKNOWN")

    with pytest.raises(RetryRejectedError) as rejected:  # Instagram's outcome is unknown with no id
        retry_slot_posts(store, store.list_platform_posts_for_video(video.id), user_id=user.id)
    assert rejected.value.code == "CONFIRMATION_REQUIRED"
    assert store.get_platform_post(video.id, "tiktok").status == "FAILED"  # nothing moved

    retried = retry_slot_posts(store, store.list_platform_posts_for_video(video.id), user_id=user.id, confirm_not_published=True)
    assert sorted(post.platform for post in retried) == ["instagram", "tiktok"]
    assert all(post.status == "PENDING" for post in retried)


def test_slot_retry_never_touches_another_users_post(store, storage, iphone_4k_file, tmp_path):
    alice, bob = _user(store, "a@example.com"), _user(store, "b@example.com")
    post = _legacy_failed_post(store, storage, alice, iphone_4k_file, tmp_path, failure_code="INSTAGRAM_MEDIA_RESOLUTION")
    with pytest.raises(RetryRejectedError):
        retry_slot_posts(store, store.list_platform_posts_for_video(post.video_id), user_id=bob.id)
    assert _row(store, post).status == "FAILED"
