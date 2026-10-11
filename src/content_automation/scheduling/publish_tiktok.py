"""
publish_tiktok.py — Standalone manual CLI: publish one already-processed
video to TikTok. Also the shared TikTok execution path used by worker.py's
one-pass worker (Milestone 2.1.4) — both drive execute_claimed_platform_post()
so there is exactly one real implementation of the publish flow.

What it does:
  python3 publish_tiktok.py --video-id <id>

  load video from SQLite -> resolve its local processed MP4 -> load stored
  caption, if any (publishing.caption_resolution — Milestone 3.10; optional
  since 3.14) -> verify media/file exists -> query TikTok creator/account
  capabilities -> initialize upload -> upload local MP4 -> publish
  privately -> obtain publish ID -> poll/check publish status -> persist
  outcome in platform_posts.

  Not wired into process_content.py — that only assigns a slot and
  materializes a PENDING platform_posts row (platform_post_materializer.py,
  Milestone 2.1.2). Actual publishing happens here, invoked either
  manually (this CLI) or by worker.py's one-pass worker.

Ownership (Milestone 2.1.4 — reconciled; previously a plain unconditional
write, see docs/evaluations/scheduling/milestone-2.1.3-atomic-platform-post-claiming.md
"Publisher Compatibility Finding"):
  The one real PENDING -> PUBLISHING mechanism is
  content_store.ContentStore.claim_platform_post() — an atomic conditional
  UPDATE. publish_video() (this CLI) now claims through it exactly like
  worker.py does, instead of writing status="PUBLISHING" directly. A
  FAILED row that never obtained a platform_post_id (a true submission
  failure — see Idempotency below) is requeued to PENDING first so it can
  be claimed again through the same mechanism, preserving the existing
  manual-retry behavior without a second ownership path.

Idempotency:
  Exactly one platform_posts row exists per (video, platform) — enforced by
  a UNIQUE constraint, not just caller discipline (content_store.py). Once
  a real TikTok publish_id has been obtained for a video, this script never
  submits it again: it only re-polls that existing submission's status,
  whether the fetch of it errors, is still processing, or is already
  terminal. Only a video that has NEVER obtained a publish_id (no
  platform_posts row, or one still PENDING/requeued-from-FAILED with
  platform_post_id=NULL — a true submission failure: local file missing,
  auth error, network error, or an upload rejected before TikTok ever
  returned an id) is eligible to (re)submit, and only via a successful
  claim_platform_post() — this distinguishes "submission never succeeded"
  from "submission succeeded but post-processing/status came back FAILED",
  which is left as a terminal FAILED record rather than silently retried.

Submission checkpoint (Milestone 3.13 — closes the crash-during-request
duplicate-publish window ADR-0015 left open):
  Before 3.13 the publish_id was persisted only after publisher.publish()
  returned, i.e. after the media upload. A crash in between, or an upload
  PUT that timed out after TikTok had the bytes (UPLOAD_NETWORK_ERROR,
  classified retryable), left a row with no platform_post_id that was then
  requeued and resubmitted — a possible duplicate post. TikTok's API has no
  idempotency key and no way to look a submission up without its
  publish_id, so the fix has to be ordering, not lookup:

    1. platform_posts.submission_state is written immediately before
       publisher.publish() (AWAITING_PLATFORM_ID for a publisher that
       declares reports_platform_post_id_before_media_transfer — TikTok —
       SUBMITTING for any other).
    2. TikTokPublisher calls back with publish_id right after init and
       BEFORE the upload; the callback persists it (status stays
       PUBLISHING, submission_state cleared). With FILE_UPLOAD nothing is
       posted until the upload completes, so every byte that could create a
       post is sent only after the id is durable.
    3. A PublishError after that point is NOT a pre-submission failure: the
       row keeps its id and goes to reconciliation (status checks only) —
       never _schedule_retry_or_fail, never resubmitted.

  What crash recovery can then conclude (scheduling/crash_recovery.py):
  id set -> poll; no id + AWAITING_PLATFORM_ID -> no media was sent, bounded
  requeue; no id + SUBMITTING -> unknowable, parked as status UNKNOWN for
  manual recovery; no id + NULL -> never submitted, requeue (unchanged).

Run (Milestone 3.0: thin CLI entry point at cli/publish_tiktok.py):
  python3 cli/publish_tiktok.py --video-id 3
  python3 cli/publish_tiktok.py --video-id 3 --privacy-level SELF_ONLY
  python3 cli/publish_tiktok.py --video-id 3 --poll-only   # re-check an existing in-flight submission only

  publish_video() below is plain parameter-driven logic (store, video_id,
  publisher) with no argparse coupling — callable directly (e.g. a future
  FastAPI endpoint, Milestone 3.1+) without going through the CLI at all.

Dependencies:
  content_automation.persistence.content_store,
  content_automation.publishing.publisher,
  content_automation.publishing.tiktok.publisher,
  content_automation.media.inspection, config.py
"""

import logging
import sys
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from content_automation.config import (
    INSTAGRAM_MEDIA_URL_TTL_SECONDS,
    INSTAGRAM_NORMALIZATION_THREADS,
    INSTAGRAM_NORMALIZATION_TIMEOUT_SECONDS,
    MAX_RETRY_ATTEMPTS,
    RETRY_BACKOFF_MINUTES,
    STATUS_CHECK_BACKOFF_SECONDS,
)
from content_automation.media import inspection as media
from content_automation.media.media_storage import MediaNotUploadedError, MediaOwnershipError, materialize_canonical_media
from content_automation.persistence.content_store import ContentStore, PlatformPostRecord, VideoRecord
from content_automation.publishing.caption_resolution import resolve_publish_caption
from content_automation.publishing.instagram import media_requirements as instagram_requirements
from content_automation.publishing.instagram.media_preparation import PreparationError, prepare_publishable_media
from content_automation.publishing.platforms import PLATFORMS, PULL_URL, TIKTOK
from content_automation.publishing.publish_status import PREPARING_MEDIA
from content_automation.publishing.publisher import STATUS_READY_TO_FINALIZE, PublishError, Publisher, PublishStatusResult
from content_automation.scheduling.finalization import finalize_ready_submission
from content_automation.storage.signed_urls import SignedUrlUnsupportedError
from content_automation.storage.local import StorageObjectNotFoundError
from content_automation.storage.protocol import StorageProtocol
from content_automation.storage.supabase_storage import StorageError
from content_automation.scheduling import retry_classification
from content_automation.scheduling.slot_matcher import now_in_config_timezone

# Milestone 3.13: platform_posts.submission_state values — see the module
# docstring's "Submission checkpoint".
AWAITING_PLATFORM_ID = "AWAITING_PLATFORM_ID"
SUBMITTING = "SUBMITTING"
# Milestone 4.2.3: PREPARING_MEDIA (imported above from publish_status, where
# the Queue reads it) marks a claim that is preparing media; nothing has been
# sent to the platform. See scheduling/crash_recovery.py.

# A video row's stable, file-level metadata (Milestone 3.13 — persisted
# after the first successful publish-time inspection of a hosted upload).
_PERSISTED_MEDIA_FIELDS = ("container", "video_codec", "audio_codec", "width", "height", "fps", "duration_seconds")


logger = logging.getLogger(__name__)


class PublishTikTokError(Exception):
    """User-facing failure — caught by main() and reported with exit(1).

    reason_code (Milestone 3.11) is persisted as platform_posts.failure_code
    when this marks a row FAILED, so the hosted UI can explain the failure
    (publishing/failure_taxonomy.py) without ever reading the message text."""

    def __init__(self, message: str, reason_code: str = "PRECONDITION_FAILED"):
        super().__init__(message)
        self.reason_code = reason_code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slot_scheduled_at(store: ContentStore, video: VideoRecord) -> str | None:
    if video.assigned_slot_id is None:
        return None
    slot = store.get_slot(video.assigned_slot_id)
    return slot.scheduled_at if slot else None


def _validate_ready_to_publish(store: ContentStore, video: VideoRecord, media_path: Path, platform: str = TIKTOK) -> None:
    """media_path is the real local file to validate — either
    video.canonical_media_path directly (legacy/local-direct, unchanged
    pre-3.4 behavior) or a temp path materialized from object storage
    (Milestone 3.4) — the caller (execute_claimed_platform_post) resolves
    which one applies; this function no longer decides that itself, so it
    validates identically either way."""
    if not media_path.exists():
        raise PublishTikTokError(
            f"Local media file for video {video.id} not found ({media_path!r}).", reason_code="LOCAL_FILE_MISSING",
        )
    # No caption check (Milestone 3.14 follow-up): captions are optional —
    # TikTok's Direct Post `title` is optional, and a captionless video
    # publishes without one. CAPTION_MISSING is no longer raised anywhere.

    if video.container is None:
        # Milestone 3.12: a hosted upload was never inspected (3.7 stores
        # bytes only — see api/routes/videos.py), so probe the materialized
        # file itself. Read-only ffprobe; the file is never altered. A
        # locally ingested video keeps using its stored inspection result.
        # Milestone 3.13: audio is not required to publish (see
        # inspection.inspect_media), and a successful probe is persisted so
        # retries and later attempts use the stored result.
        try:
            info = media.inspect_media(media_path, require_audio=False)
        except media.MediaError as exc:
            raise PublishTikTokError(
                f"Video {video.id} failed media inspection: {exc}", reason_code=exc.reason_code,
            ) from exc
        _persist_media_metadata(store, video, info)
    else:
        info = _stored_media_info(video, media_path)
    _check_platform_requirements(video, info, platform)


def _stored_media_info(video: VideoRecord, media_path: Path) -> media.MediaInfo:
    """The video row's persisted inspection result as a MediaInfo."""
    return media.MediaInfo(
        path=media_path, container=video.container, video_codec=video.video_codec,
        audio_codec=video.audio_codec, width=video.width, height=video.height, fps=video.fps,
        duration_seconds=video.duration_seconds, file_size_bytes=video.file_size_bytes,
    )


def _check_platform_requirements(video: VideoRecord, info: media.MediaInfo, platform: str) -> None:
    """Platform-specific media limits for push-file platforms (TikTok)."""
    # Instagram never reaches this push-file path: its limits are applied in
    # _execute_pull_url_post and publishing/instagram/media_preparation.py.
    compatible, reason = media.is_tiktok_compatible(info)
    if not compatible:
        raise PublishTikTokError(f"Video {video.id} is not TikTok-compatible: {reason}", reason_code="MEDIA_INCOMPATIBLE")


def _persist_media_metadata(store: ContentStore, video: VideoRecord, info: media.MediaInfo) -> None:
    """Write a fresh inspection back to the video row (Milestone 3.13), so
    the next attempt for this video (a retry, a manual retry, a re-claim
    after a crash) reads it instead of probing again. Metadata only — the
    file is never re-encoded. Best-effort: these fields are a cache of
    facts about immutable bytes, so failing to save them must not fail a
    publish that is otherwise ready; the next attempt simply probes again."""
    fields = {name: getattr(info, name) for name in _PERSISTED_MEDIA_FIELDS}
    if video.file_size_bytes is None:
        fields["file_size_bytes"] = info.file_size_bytes
    try:
        store.update_video(video.id, **fields)
    except Exception:  # noqa: BLE001 — cache write only, see docstring
        logger.warning("event=media_metadata_persist_failed video_id=%s", video.id)


def _schedule_retry_or_fail(store: ContentStore, record: PlatformPostRecord, error: PublishError) -> None:
    """A PublishError occurred before a platform_post_id was ever obtained
    for `record` (the platform_post_id rule stays absolute — see module
    docstring — this is only ever called pre-submission). Classify it
    (retry_classification.py) against the existing retry budget:

    - retryable AND retry_count < config.MAX_RETRY_ATTEMPTS: back to
      PENDING, retry_count incremented, next_retry_at set
      config.RETRY_BACKOFF_MINUTES[retry_count] minutes out (naive local
      time — the same convention scheduled_at uses, so
      due_post_selector's single `now` compares against both). Not
      claimed again here — the normal claim_platform_post() path picks it
      up once next_retry_at arrives, exactly like any other due work.
    - otherwise (terminal, or retries exhausted): FAILED. Retry
      exhaustion preserves this final failure_reason so a future UI can
      explain why the post stopped retrying.

    Either way failure_reason is set to str(error) — never silently
    dropped, whether this is attempt 1 or the final one.
    """
    if retry_classification.classify(error) and record.retry_count < MAX_RETRY_ATTEMPTS:
        delay_minutes = RETRY_BACKOFF_MINUTES[record.retry_count]
        next_retry_at = (now_in_config_timezone() + timedelta(minutes=delay_minutes)).isoformat()
        store.update_platform_post(
            record.id, updated_at=_now_iso(), status="PENDING",
            retry_count=record.retry_count + 1, next_retry_at=next_retry_at,
            failure_reason=str(error), failure_code=error.reason_code, submission_state=None,
        )
        print(f"Retryable failure ({error.reason_code}) — retry {record.retry_count + 1}/{MAX_RETRY_ATTEMPTS} at {next_retry_at}.")
    else:
        store.update_platform_post(
            record.id, updated_at=_now_iso(), status="FAILED", failure_reason=str(error), failure_code=error.reason_code,
            submission_state=None,
        )


def _next_status_check_at(status_check_count: int, now: datetime) -> str:
    """Aware-UTC isoformat timestamp for the next automatic reconciliation
    check (Milestone 2.1.10), indexed by how many status checks a row has
    already had — capped at config.STATUS_CHECK_BACKOFF_SECONDS' last
    (longest) interval rather than growing unbounded or ever exhausting
    (there is no retry-budget equivalent here; nothing was ever
    resubmitted to "use up" — TikTok will eventually reach a terminal
    status). `now` must be aware UTC, matching next_status_check_at's own
    storage convention (see content_store.py's migration comment)."""
    index = min(status_check_count, len(STATUS_CHECK_BACKOFF_SECONDS) - 1)
    return (now + timedelta(seconds=STATUS_CHECK_BACKOFF_SECONDS[index])).isoformat()


def _resolve_poll_outcome(status_result: PublishStatusResult) -> tuple[str, dict]:
    """The one TikTok-status -> platform_posts-fields mapping, shared
    (Milestone 2.1.10) by every caller that ever checks an existing
    submission's status: _poll_and_update below (the synchronous poll
    right after submission, and the manual --poll-only CLI),
    crash_recovery.py's Case B (stale-PUBLISHING safety net), and
    reconciliation.py (the routine automatic re-check). Never two
    independently-maintained copies of this decision.

    Returns (outcome, fields): outcome is one of "PUBLISHED"/"FAILED"/
    "PROCESSING"; fields is what to persist for a terminal outcome
    (excluding updated_at, which every caller already supplies itself) —
    empty for "PROCESSING", since what to persist there (next_status_check_at/
    status_check_count) depends on each caller's own scheduling state, not
    on the status result alone."""
    if status_result.status == "PUBLISH_COMPLETE":
        # Milestone 4.2: submission_state is cleared (a two-step platform may
        # be confirmed here while its PUBLISH_REQUESTED checkpoint is set),
        # and the media id is kept when the status check revealed one.
        fields = {"status": "PUBLISHED", "published_at": _now_iso(), "submission_state": None}
        if status_result.platform_media_id:
            fields["platform_media_id"] = status_result.platform_media_id
        return "PUBLISHED", fields
    if status_result.status == "FAILED":
        # failure_code (Milestone 3.11): the platform's own fail code when it
        # reported one — a machine label, mapped to a user-facing category by
        # publishing/failure_taxonomy.py, never shown raw. Milestone 4.2: a
        # publisher may supply the label separately (failure_code).
        return "FAILED", {
            "status": "FAILED", "failure_reason": status_result.failure_reason,
            "failure_code": status_result.failure_code or status_result.failure_reason or "PLATFORM_REPORTED_FAILURE",
            "submission_state": None,
        }
    if status_result.status == STATUS_READY_TO_FINALIZE:
        # Milestone 4.2: processed but not posted — the caller hands the row
        # to scheduling/finalization.finalize_ready_submission.
        return "READY", {}
    return "PROCESSING", {}


def _poll_and_update(store: ContentStore, record, publisher: Publisher) -> None:
    """Check an existing submission's status and persist the result.
    Never resubmits — only ever reads/updates the record it's given.

    Milestone 2.1.10: a still-processing outcome now also schedules the
    first automatic reconciliation check (next_status_check_at/
    status_check_count) instead of leaving the row to wait on a human
    rerunning --poll-only — reconciliation.py picks it up from here."""
    try:
        status_result = publisher.get_status(record.platform_post_id)
    except PublishError as exc:
        print(f"WARNING: could not fetch publish status: {exc}", file=sys.stderr)
        return

    print(f"{_label(record.platform)} status: {status_result.status}")
    outcome, fields = _resolve_poll_outcome(status_result)

    if outcome == "READY":
        result = finalize_ready_submission(store, record, publisher)
        print(f"Finalize: {result}")
        return

    if outcome == "PROCESSING":
        fields = {
            "next_status_check_at": _next_status_check_at(record.status_check_count, datetime.now(timezone.utc)),
            "status_check_count": record.status_check_count + 1,
        }
        store.update_platform_post(record.id, updated_at=_now_iso(), **fields)
        print("Not yet final — scheduled for automatic reconciliation (see reconciliation.py), "
              "or rerun with --poll-only to check again immediately.")
        return

    store.update_platform_post(record.id, updated_at=_now_iso(), **fields)
    if outcome == "PUBLISHED":
        print(f"Published. platform_post_id={record.platform_post_id}")
    else:
        print(f"{_label(record.platform)} reported failure: {status_result.failure_reason}")


@contextmanager
def _resolved_media_path(store: ContentStore, video: VideoRecord, storage: StorageProtocol | None):
    """Yield the real local Path to publish from — materialized via
    `storage` if video.storage_provider is set (Milestone 3.4), or
    video.canonical_media_path directly (legacy, no network) otherwise.
    Raises PublishTikTokError immediately (before any cleanup-requiring
    resource is opened) if the video is storage-backed but no `storage`
    was supplied — shared by execute_claimed_platform_post and
    publish_video so both resolve exactly the same way."""
    if video.storage_provider:
        if storage is None:
            raise PublishTikTokError(
                f"video {video.id} has storage_provider={video.storage_provider!r} but no storage backend "
                "was supplied.",
                reason_code="STORAGE_UNAVAILABLE",
            )
        with materialize_canonical_media(store, storage, video.id, video.user_id) as media_path:
            yield media_path
        return

    yield Path(video.canonical_media_path) if video.canonical_media_path else Path("")


def _validate_and_submit(
    store: ContentStore, video: VideoRecord, record: PlatformPostRecord, media_path: Path, publisher: Publisher,
) -> None:
    """The actual validate -> checkpoint -> submit -> persist -> poll
    sequence, factored out so execute_claimed_platform_post can run it
    identically whether media_path came straight from
    video.canonical_media_path (legacy) or from a Milestone 3.4
    object-storage materialization — this function has no idea which, and
    doesn't need to. See the module docstring's "Submission checkpoint"
    (Milestone 3.13) for the ordering guarantees."""
    try:
        _validate_ready_to_publish(store, video, media_path, record.platform)
    except PublishTikTokError as exc:
        _mark_precondition_failed(store, record, exc)
        raise
    _submit(store, video, record, media_path, publisher)


def _mark_precondition_failed(store: ContentStore, record: PlatformPostRecord, exc: PublishTikTokError) -> None:
    store.update_platform_post(
        record.id, updated_at=_now_iso(), status="FAILED", failure_reason=str(exc), failure_code=exc.reason_code,
        submission_state=None,  # Milestone 4.2.3: clears PREPARING_MEDIA
    )


def _submit(
    store: ContentStore, video: VideoRecord, record: PlatformPostRecord, media_path: Path, publisher: Publisher,
    media_url: Callable[[], str] | None = None,
) -> None:
    """Checkpoint -> submit -> persist -> poll (Milestone 3.13 ordering).
    media_url (Milestone 4.2, pull-URL platforms only) issues the short-lived
    signed URL; it's called after the checkpoint and immediately before
    publisher.publish(), and the URL itself is never logged or stored."""
    checkpointed = bool(getattr(publisher, "reports_platform_post_id_before_media_transfer", False))
    store.update_platform_post(
        record.id, updated_at=_now_iso(), submission_state=AWAITING_PLATFORM_ID if checkpointed else SUBMITTING,
        submission_started_at=_now_iso(),
    )

    def persist_platform_post_id(platform_post_id: str) -> None:
        # Raising here (e.g. the database is unreachable) aborts the publish
        # before any media is sent — see Publisher's checkpoint contract.
        store.update_platform_post(
            record.id, updated_at=_now_iso(), status="PUBLISHING", platform_post_id=platform_post_id,
            submission_state=None,
        )

    caption = resolve_publish_caption(video, record.platform)
    try:
        extra = {"media_url": _issue_media_url(media_url)} if media_url is not None else {}
        if checkpointed:
            result = publisher.publish(media_path, caption, on_platform_post_id=persist_platform_post_id, **extra)
        else:
            result = publisher.publish(media_path, caption, **extra)
    except PublishError as exc:
        current = store.get_platform_post(video.id, record.platform)
        if current is not None and current.platform_post_id:
            # The platform issued an id and the media transfer may have
            # reached it (e.g. an upload that timed out after TikTok got
            # the bytes). Never resubmit: hand off to reconciliation, which
            # only ever checks this id's status.
            store.update_platform_post(
                record.id, updated_at=_now_iso(), next_status_check_at=_now_iso(),
            )
            logger.info(
                "event=submission_outcome_pending_reconciliation platform_post_row_id=%s video_id=%s "
                "failure_code=%s", record.id, video.id, exc.reason_code,
            )
            raise PublishTikTokError(
                f"Submission outcome unconfirmed (publish_id={current.platform_post_id}): {exc}",
                reason_code=exc.reason_code,
            ) from exc
        _schedule_retry_or_fail(store, record, exc)
        raise PublishTikTokError(f"Submission failed: {exc}") from exc

    if not checkpointed or store.get_platform_post(video.id, record.platform).platform_post_id is None:
        # Publishers without the checkpoint (and, defensively, one that
        # declared it but never called back) — persist the id now, as
        # before Milestone 3.13. A crash between submission and polling is
        # still safe from here on: the next run sees platform_post_id set
        # and only polls, never resubmits.
        persist_platform_post_id(result.platform_post_id)
    print(f"Submitted to {_label(record.platform)}: publish_id={result.platform_post_id}")

    refreshed = store.get_platform_post(video.id, record.platform)
    _poll_and_update(store, refreshed, publisher)


def execute_claimed_platform_post(
    store: ContentStore, video_id: int, platform: str, publisher: Publisher, storage: StorageProtocol | None = None,
) -> None:
    """Execute the proven TikTok publish flow for a platform_posts row that
    has ALREADY been claimed (status == PUBLISHING) via
    ContentStore.claim_platform_post(). Does not claim, and does not
    require or re-check PENDING — ownership must already be established by
    the caller before this is invoked.

    Shared by publish_video() (this file's manual CLI, after it claims)
    and worker.py's one-pass worker (Milestone 2.1.4), so both drive
    exactly the same publish path instead of duplicating TikTok publishing
    logic. See module docstring for the ownership reconciliation.

    Validates the video is actually publishable before calling the
    publisher (belt-and-suspenders: publish_video() already validates
    before ever inserting/claiming a row for a brand-new video, but
    worker.py claims a pre-existing row with no equivalent earlier
    checkpoint, so this is the one place that check is guaranteed to run
    for every caller).

    Milestone 2.1.6 fix: a precondition failure here (missing file,
    missing caption, incompatible container/codec) used to propagate
    uncaught, leaving an already-claimed row stuck PUBLISHING forever with
    no failure_reason — crash_recovery.py would eventually requeue it
    (platform_post_id is still NULL), it would get re-claimed, fail the
    same validation again, and repeat indefinitely for a video whose
    problem never resolves on its own. Local validation failures are
    unconditionally terminal (see retry_classification.py's module
    docstring for why they're not routed through classification at all) —
    now caught here and marked FAILED immediately, same as any other
    terminal publishing failure.

    Milestone 3.4 (object storage): `storage` is optional and, for a
    video with no storage_provider (every pre-3.4 video, and any new one
    that hasn't been uploaded to object storage — see
    media.media_storage), is never even looked at — behavior is byte-for-
    byte identical to before this milestone, reading directly from
    video.canonical_media_path. For a video that HAS been uploaded to
    object storage, `storage` must be supplied; the row is materialized
    to a temp local path (media.media_storage.materialize_canonical_media)
    for the duration of validation+submission, then cleaned up — the
    application/job layer resolves storage, TikTokPublisher itself never
    learns anything about Supabase/S3 (see
    docs/decisions/0009-object-storage-media-lifecycle.md "Materialization
    Boundary"). Missing `storage` for a storage-backed video is a
    terminal failure (marked FAILED immediately, same as any other
    precondition failure above) — never silently falls back to a stale/
    nonexistent local path.
    """
    video = store.get_video(video_id)
    if video is None:
        raise PublishTikTokError(f"No video with id={video_id}.")
    record = store.get_platform_post(video_id, platform)
    if record is None:
        raise PublishTikTokError(f"No platform_posts row for video={video_id} platform={platform!r}.")

    if video.storage_provider and storage is None:
        exc = PublishTikTokError(
            f"video {video_id} has storage_provider={video.storage_provider!r} but no storage backend was supplied.",
            reason_code="STORAGE_UNAVAILABLE",
        )
        store.update_platform_post(
            record.id, updated_at=_now_iso(), status="FAILED", failure_reason=str(exc), failure_code=exc.reason_code,
        )
        raise exc

    if platform in PLATFORMS and PLATFORMS[platform].media_delivery == PULL_URL:
        _execute_pull_url_post(store, video, record, publisher, storage)
        return

    with ExitStack() as stack:
        try:
            media_path = stack.enter_context(_resolved_media_path(store, video, storage))
        except _MATERIALIZATION_ERRORS as exc:
            # Milestone 3.12: fetching hosted media is part of executing a
            # claimed post — a failure here must end in the same retry/
            # FAILED state as any other pre-submission failure, never
            # escape and leave the row PUBLISHING. Nothing was submitted,
            # so _schedule_retry_or_fail's pre-submission contract holds.
            error = _materialization_publish_error(exc)
            _schedule_retry_or_fail(store, record, error)
            raise PublishTikTokError(f"Could not load media for video {video_id}: {error}") from exc
        _validate_and_submit(store, video, record, media_path, publisher)


_MATERIALIZATION_ERRORS = (StorageError, StorageObjectNotFoundError, MediaNotUploadedError, MediaOwnershipError)


def _execute_pull_url_post(
    store: ContentStore, video: VideoRecord, record: PlatformPostRecord, publisher: Publisher,
    storage: StorageProtocol | None,
) -> None:
    """Milestone 4.2: a platform that fetches the video itself (Instagram).
    Stored objects stay private; the platform gets a short-lived signed URL
    issued just before submission — since 4.2.1 for the object
    publishing/instagram/media_preparation.py resolves (the original, or a
    normalized derivative)."""
    if not video.storage_provider or not video.storage_key or storage is None:
        exc = PublishTikTokError(
            f"video {video.id} isn't in object storage, which {_label(record.platform)} needs to fetch it from.",
            reason_code="STORAGE_UNAVAILABLE",
        )
        _mark_precondition_failed(store, record, exc)
        raise exc

    # Hard limits first, from data already stored, so a video that can never
    # go to Instagram fails without being downloaded.
    hard_problem = instagram_requirements.check_caption(resolve_publish_caption(video, record.platform))
    if hard_problem is None and video.duration_seconds is not None:
        hard_problem = instagram_requirements.check_duration(video.duration_seconds)
    if hard_problem is not None:
        exc = PublishTikTokError(f"Video {video.id}: {hard_problem.message}", reason_code=hard_problem.reason_code)
        _mark_precondition_failed(store, record, exc)
        raise exc

    # Milestone 4.2.1: the stored object Instagram fetches is the original
    # when it already meets the Reels spec, otherwise a normalized private
    # derivative (made once, then reused). The heartbeat keeps this claim
    # fresh during a long encode.
    # Milestone 4.2.3: PREPARING_MEDIA marks the claim as "preparing media,
    # nothing sent to the platform", so if the whole worker dies mid-encode
    # (e.g. the container is OOM-killed), crash recovery retries it at most
    # once instead of requeueing it forever (scheduling/crash_recovery.py).
    store.update_platform_post(record.id, updated_at=_now_iso(), submission_state=PREPARING_MEDIA)
    try:
        prepared = prepare_publishable_media(
            store, storage, video,
            heartbeat=lambda: store.update_platform_post(record.id, updated_at=_now_iso()),
            timeout_seconds=INSTAGRAM_NORMALIZATION_TIMEOUT_SECONDS,
            on_probed=lambda details: _persist_probed_metadata(store, video, details),
            threads=INSTAGRAM_NORMALIZATION_THREADS,
        )
    except PreparationError as exc:
        # Deterministic failures (bad media, hard limits, a real ffmpeg error,
        # the OOM killer, our timeout) park the post FAILED until the user
        # retries. Only an interrupted encode (a non-KILL signal, e.g. the
        # worker stopping) takes the normal bounded backoff.
        if retry_classification.is_retryable(exc.reason_code):
            error = PublishError(f"Video {video.id}: {exc}", reason_code=exc.reason_code)
            _schedule_retry_or_fail(store, record, error)
            raise PublishTikTokError(str(error), reason_code=exc.reason_code) from None
        error = PublishTikTokError(f"Video {video.id}: {exc}", reason_code=exc.reason_code)
        _mark_precondition_failed(store, record, error)
        raise error from None
    except _MATERIALIZATION_ERRORS as exc:
        error = _materialization_publish_error(exc)
        _schedule_retry_or_fail(store, record, error)
        raise PublishTikTokError(f"Could not load media for video {video.id}: {error}") from exc

    video = store.get_video(video.id)
    _submit(
        store, video, record, Path(prepared.storage_key), publisher,
        media_url=lambda: storage.create_signed_url(prepared.storage_key, INSTAGRAM_MEDIA_URL_TTL_SECONDS),
    )


def _persist_probed_metadata(store: ContentStore, video: VideoRecord, details) -> None:
    """Cache a never-inspected video's probe in its row (Milestone 3.13's
    convention), in the same format media.inspection writes."""
    if video.container is not None:
        return
    _persist_media_metadata(store, video, media.MediaInfo(
        path=details.path, container=details.container, video_codec=details.video_codec,
        audio_codec=details.audio_codec, width=details.coded_width, height=details.coded_height, fps=details.fps,
        duration_seconds=details.duration_seconds, file_size_bytes=details.file_size_bytes,
    ))


def _issue_media_url(issue: Callable[[], str]) -> str:
    """Issue the signed URL, translating storage failures into the
    pre-submission PublishError shapes the retry classifier understands."""
    try:
        return issue()
    except SignedUrlUnsupportedError as exc:
        raise PublishError(str(exc), reason_code="STORAGE_UNAVAILABLE") from None
    except _MATERIALIZATION_ERRORS as exc:
        raise _materialization_publish_error(exc) from None


def _label(platform: str) -> str:
    return PLATFORMS[platform].label if platform in PLATFORMS else platform


def _materialization_publish_error(exc: Exception) -> PublishError:
    """Translate a media-materialization failure into the PublishError
    shape retry_classification understands. Only storage transport
    failures can be transient (STORAGE_NETWORK_ERROR always; STORAGE_HTTP_ERROR
    by http_status — 5xx retryable, 4xx terminal); a missing object or an
    ownership mismatch never fixes itself."""
    if isinstance(exc, StorageError):
        if exc.reason_code == "NETWORK_ERROR":
            return PublishError(str(exc), reason_code="STORAGE_NETWORK_ERROR")
        if exc.reason_code == "HTTP_ERROR":
            return PublishError(str(exc), reason_code="STORAGE_HTTP_ERROR", http_status=exc.http_status)
        return PublishError(str(exc), reason_code="STORAGE_UNAVAILABLE")
    if isinstance(exc, MediaOwnershipError):
        return PublishError(str(exc), reason_code="MEDIA_OWNERSHIP_MISMATCH")
    return PublishError(str(exc), reason_code="STORAGE_OBJECT_MISSING")


def publish_video(
    store: ContentStore, video_id: int, publisher: Publisher, *, poll_only: bool = False,
    storage: StorageProtocol | None = None,
) -> None:
    video = store.get_video(video_id)
    if video is None:
        raise PublishTikTokError(f"No video with id={video_id}.")

    record = store.get_platform_post(video_id, "tiktok")

    # A publish_id already exists: TikTok has already accepted a submission
    # for this video. Never submit a second one — only re-check status,
    # regardless of whether it's still processing or already terminal.
    if record is not None and record.platform_post_id:
        if record.status == "PUBLISHED":
            print(f"Video {video_id} is already PUBLISHED as TikTok post {record.platform_post_id}.")
            return
        _poll_and_update(store, record, publisher)
        return

    if poll_only:
        raise PublishTikTokError(f"No in-flight TikTok submission for video {video_id} to poll.")

    if record is None:
        # Brand-new video: validate before ever creating a row, so a
        # precondition failure (missing file/caption) leaves nothing to
        # clean up — matches Milestone 2.0's original guarantee. Milestone
        # 3.4: _resolved_media_path raises directly (no row exists yet to
        # mark FAILED) if the video is storage-backed but no storage was
        # supplied — same "nothing to clean up" guarantee extended to the
        # object-storage case.
        with _resolved_media_path(store, video, storage) as media_path:
            _validate_ready_to_publish(store, video, media_path)
        record = store.insert_platform_post(
            video_id, "tiktok", created_at=_now_iso(), scheduled_at=_slot_scheduled_at(store, video)
        )
    elif record.status == "FAILED":
        # A true submission failure (no platform_post_id was ever obtained
        # — a row WITH one already returned above) is a legitimate manual
        # retry, not a duplicate. Requeue to PENDING so
        # claim_platform_post() — the one real PENDING->PUBLISHING
        # ownership mechanism — can claim it like any other due work,
        # instead of writing PUBLISHING directly. A human explicitly
        # rerunning this CLI is a fresh attempt, independent of the
        # automatic retry/backoff budget (Milestone 2.1.6) — reset it
        # rather than inheriting whatever retry_count the automatic path
        # had already accumulated.
        store.update_platform_post(
            record.id, updated_at=_now_iso(), status="PENDING", retry_count=0, next_retry_at=None
        )

    claimed = store.claim_platform_post(record.id, updated_at=_now_iso())
    if not claimed:
        current = store.get_platform_post(video_id, "tiktok")
        raise PublishTikTokError(
            f"Video {video_id}'s TikTok post could not be claimed for submission "
            f"(current status: {current.status if current else 'unknown'} — "
            "likely already claimed by another process)."
        )

    execute_claimed_platform_post(store, video_id, "tiktok", publisher, storage=storage)
