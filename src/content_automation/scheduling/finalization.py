"""
finalization.py — the one place a two-step platform's "create the post"
call is made (Milestone 4.2: Instagram's media_publish).

What it does:
  finalize_ready_submission(store, record, publisher, now=...) is called by
  every path that sees a submission reported STATUS_READY_TO_FINALIZE — the
  inline poll right after submission (publish_tiktok._poll_and_update),
  routine reconciliation, and crash recovery — so the duplicate-prevention
  rules exist exactly once:

  1. A PUBLISH_REQUESTED checkpoint is written, compare-and-swap on
     updated_at, immediately before publisher.finalize(). Losing the swap
     means another process owns this step: do nothing.
  2. Success: PUBLISHED, platform_media_id stored, checkpoint cleared.
  3. An HTTP 4xx answer means the platform received the request and
     rejected it, so nothing was posted: the checkpoint is cleared. A
     retryable rejection (media not ready, rate limited) waits for the next
     status check; anything else parks the row UNKNOWN with that
     failure_code — manual retry re-checks the same container (never a new
     one).
  4. Anything else (timeout, network, 5xx, malformed) is ambiguous: the
     publish may have happened. The checkpoint stays and the row waits for
     the next status check.
  5. A row already carrying PUBLISH_REQUESTED whose container is still
     ready (not PUBLISHED) is NOT published again automatically. Meta
     doesn't document whether media_publish is idempotent per container.
     Within the grace period (config.PLATFORM_POST_STALE_MINUTES since the
     request) it keeps waiting; after it, the row is parked UNKNOWN
     (PUBLISH_OUTCOME_UNKNOWN). Retrying that requires the user to confirm
     it wasn't posted (publish_status.post_retry_requires_confirmation), and
     even then the container's status is checked again first.

  Status checks that find the container PUBLISHED resolve every case
  through the shared _resolve_poll_outcome (which also clears the
  checkpoint), so a confirmed publish is never repeated.

Returns one of "PUBLISHED", "WAITING", "UNKNOWN", "SKIPPED".

Dependencies:
  persistence.protocol, publishing.publisher, scheduling.retry_classification,
  scheduling.telemetry (log_event), config.
"""

from datetime import datetime, timedelta, timezone

from content_automation.config import PLATFORM_POST_STALE_MINUTES, STATUS_CHECK_BACKOFF_SECONDS
from content_automation.persistence.content_store import PlatformPostRecord
from content_automation.persistence.protocol import ContentStoreProtocol
from content_automation.publishing.publish_status import PUBLISH_REQUESTED
from content_automation.publishing.publisher import PublishError, Publisher
from content_automation.scheduling import retry_classification
from content_automation.scheduling.telemetry import log_event

# platform_posts.submission_state while a finalize call is (or may be) in
# flight — defined once in publishing/publish_status.py.

PUBLISHED, WAITING, UNKNOWN, SKIPPED = "PUBLISHED", "WAITING", "UNKNOWN", "SKIPPED"


def _next_check(record: PlatformPostRecord, now: datetime) -> dict:
    index = min(record.status_check_count, len(STATUS_CHECK_BACKOFF_SECONDS) - 1)
    return {
        "next_status_check_at": (now + timedelta(seconds=STATUS_CHECK_BACKOFF_SECONDS[index])).isoformat(),
        "status_check_count": record.status_check_count + 1,
    }


def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def finalize_ready_submission(
    store: ContentStoreProtocol, record: PlatformPostRecord, publisher: Publisher, *, now: datetime | None = None,
    user_id: int | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    # Every write is compare-and-swap scoped to the row's owner. Callers
    # without a user scope (the inline poll right after submission) act for
    # the row's own owner; Postgres's CAS never matches a NULL user_id.
    user_id = user_id if user_id is not None else record.user_id
    ids = {"platform_post_row_id": record.id, "video_id": record.video_id, "platform": record.platform,
           "container_id": record.platform_post_id}
    log_event(f"{record.platform}_container_ready", **ids)

    if record.submission_state == PUBLISH_REQUESTED:
        return _resolve_stuck_request(store, record, now, user_id, ids)

    requested_at = now.isoformat()
    claimed = store.update_platform_post_if_unchanged(
        record.id, expected_updated_at=record.updated_at, updated_at=requested_at, user_id=user_id,
        submission_state=PUBLISH_REQUESTED, submission_started_at=requested_at,
    )
    if not claimed:
        return SKIPPED
    log_event(f"{record.platform}_publish_started", **ids)

    try:
        result = publisher.finalize(record.platform_post_id)
    except PublishError as exc:
        return _handle_finalize_error(store, record, requested_at, exc, now, user_id, ids)

    store.update_platform_post_if_unchanged(
        record.id, expected_updated_at=requested_at, updated_at=datetime.now(timezone.utc).isoformat(), user_id=user_id,
        status="PUBLISHED", published_at=datetime.now(timezone.utc).isoformat(),
        platform_media_id=result.platform_media_id, submission_state=None,
    )
    log_event(f"{record.platform}_post_published", **ids, media_id=result.platform_media_id)
    return PUBLISHED


def _handle_finalize_error(store, record, requested_at: str, exc: PublishError, now: datetime, user_id, ids) -> str:
    status = getattr(exc, "http_status", None)
    if status is not None and 400 <= status < 500:
        # The platform answered and rejected the request: nothing was posted.
        if retry_classification.is_retryable(exc.reason_code, status):
            store.update_platform_post_if_unchanged(
                record.id, expected_updated_at=requested_at, updated_at=_utc_now(), user_id=user_id,
                submission_state=None, **_next_check(record, now),
            )
            log_event(f"{record.platform}_publish_rejected_will_retry", **ids, failure_code=exc.reason_code)
            return WAITING
        store.update_platform_post_if_unchanged(
            record.id, expected_updated_at=requested_at, updated_at=_utc_now(), user_id=user_id,
            status="UNKNOWN", submission_state=None, failure_code=exc.reason_code, failure_reason=str(exc),
        )
        log_event(f"{record.platform}_post_unknown", **ids, failure_code=exc.reason_code, decision="PUBLISH_REJECTED")
        return UNKNOWN
    # Ambiguous: the publish may have gone through. Keep the checkpoint;
    # the next status check sees PUBLISHED, or _resolve_stuck_request decides.
    store.update_platform_post_if_unchanged(
        record.id, expected_updated_at=requested_at, updated_at=_utc_now(), user_id=user_id, **_next_check(record, now),
    )
    log_event(f"{record.platform}_publish_outcome_pending", **ids, failure_code=exc.reason_code)
    return WAITING


def _resolve_stuck_request(store, record, now: datetime, user_id, ids) -> str:
    requested = _parse(record.submission_started_at)
    if requested is not None and now - requested < timedelta(minutes=PLATFORM_POST_STALE_MINUTES):
        store.update_platform_post_if_unchanged(
            record.id, expected_updated_at=record.updated_at, updated_at=_utc_now(), user_id=user_id,
            **_next_check(record, now),
        )
        return WAITING
    parked = store.update_platform_post_if_unchanged(
        record.id, expected_updated_at=record.updated_at, updated_at=_utc_now(), user_id=user_id,
        status="UNKNOWN", failure_code="PUBLISH_OUTCOME_UNKNOWN",
        failure_reason="A publish request was sent but its outcome couldn't be confirmed; not publishing again automatically.",
    )
    if parked:
        log_event(f"{record.platform}_post_unknown", **ids, failure_code="PUBLISH_OUTCOME_UNKNOWN")
    return UNKNOWN if parked else SKIPPED


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
