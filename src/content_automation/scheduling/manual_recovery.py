"""
manual_recovery.py — intentional, user-initiated recovery of a platform post
the automatic jobs have stopped working on (Milestone 3.13: Reconciliation +
Recovery).

What it does:
  retry_platform_post() is the one transition back into the automatic
  pipeline from a FAILED or UNKNOWN platform_posts row. It never publishes or
  calls the platform itself — it only moves the row to the state the
  existing jobs already handle, so the hosted worker does the actual work:

    FAILED, no platform_post_id    nothing was ever accepted -> PENDING
                                   (a fresh attempt: retry_count reset, like
                                   publish_tiktok.publish_video's CLI retry)
    FAILED, with platform_post_id  the platform itself reported that
                                   submission failed (since 3.13 FAILED is
                                   only written for an id when the platform
                                   says so) -> PENDING, id cleared; the old
                                   id is logged, not kept on the row
    UNKNOWN, with platform_post_id accepted, outcome unknown -> PUBLISHING
                                   with a fresh status-check budget:
                                   reconciliation re-checks it (never a
                                   resubmission). E.g. after reconnecting
                                   TikTok.
    UNKNOWN, no platform_post_id   may have been accepted and there is
                                   nothing to check -> refused unless the
                                   caller confirms the post is not on the
                                   platform (confirm_not_published), then
                                   PENDING. The same confirmation lets an
                                   UNKNOWN row with an id be resubmitted
                                   instead of re-checked.
    PENDING / PUBLISHING / PUBLISHED  refused (RetryRejectedError).

  A requeued row gets next_retry_at = one RETRY_BACKOFF_MINUTES[0] interval
  out, so the Queue shows it as scheduled (not "schedule missed") until the
  worker's next cycle picks it up. failure_code/failure_reason are kept as
  the record of what went wrong last; the next attempt overwrites them only
  if it fails too.

  Atomic: the transition is a compare-and-swap on updated_at, scoped to the
  owner (update_platform_post_if_unchanged), so a worker that touched the row
  in between makes the retry fail with CONCURRENT_UPDATE instead of
  clobbering it.

Dependencies:
  persistence.protocol, publishing.publish_status (the retry guards, shared
  with the Queue's can_retry flags), scheduling.worker (log_event),
  scheduling.slot_matcher, config.
"""

from datetime import datetime, timedelta, timezone

from content_automation.config import RETRY_BACKOFF_MINUTES
from content_automation.persistence.content_store import PlatformPostRecord
from content_automation.persistence.protocol import ContentStoreProtocol
from content_automation.publishing.publish_status import (
    PUBLISH_REQUESTED,
    post_can_retry,
    post_retry_requires_confirmation,
)
from content_automation.scheduling.slot_matcher import now_in_config_timezone
from content_automation.scheduling.worker import log_event


class RetryRejectedError(Exception):
    """The post can't be retried as asked. `code` is machine-stable:
    NOT_RETRYABLE, CONFIRMATION_REQUIRED or CONCURRENT_UPDATE."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def retry_platform_post(
    store: ContentStoreProtocol, post: PlatformPostRecord, *, user_id: int, confirm_not_published: bool = False,
) -> PlatformPostRecord:
    """Move `post` (owned by user_id — the caller has already checked) back
    into the automatic pipeline. See module docstring for the rules."""
    if not post_can_retry(post):
        raise RetryRejectedError(
            "NOT_RETRYABLE", f"A {post.status} post can't be retried — only FAILED or UNKNOWN posts can.",
        )
    if post_retry_requires_confirmation(post) and not confirm_not_published:
        raise RetryRejectedError(
            "CONFIRMATION_REQUIRED",
            "It's unknown whether this was published. Check the platform, then confirm it wasn't posted to retry.",
        )

    now_utc = datetime.now(timezone.utc).isoformat()
    publish_requested = post.submission_state == PUBLISH_REQUESTED
    if post.status == "UNKNOWN" and post.platform_post_id and (not confirm_not_published or publish_requested):
        # Milestone 4.2: a confirmed retry of a parked Instagram publish
        # re-checks the SAME container (its status is read before anything
        # is posted), clearing the checkpoint so finalization may publish it
        # if it is still unpublished. It never creates a new container.
        decision = "RECHECK"
        fields = {"status": "PUBLISHING", "status_check_count": 0, "next_status_check_at": None}
        if publish_requested:
            fields["submission_state"] = None
    else:
        decision = "RESUBMIT"
        next_retry_at = (now_in_config_timezone() + timedelta(minutes=RETRY_BACKOFF_MINUTES[0])).isoformat()
        fields = {
            "status": "PENDING", "platform_post_id": None, "retry_count": 0, "next_retry_at": next_retry_at,
            "status_check_count": 0, "next_status_check_at": None, "submission_state": None,
        }

    moved = store.update_platform_post_if_unchanged(
        post.id, expected_updated_at=post.updated_at, updated_at=now_utc, user_id=user_id, **fields,
    )
    if not moved:
        raise RetryRejectedError("CONCURRENT_UPDATE", "This post changed while retrying. Refresh and try again.")

    log_event(
        "manual_retry", platform_post_row_id=post.id, video_id=post.video_id, platform=post.platform,
        user_id=user_id, from_status=post.status, decision=decision, failure_code=post.failure_code,
        previous_platform_post_id=post.platform_post_id if decision == "RESUBMIT" else None,
    )
    return store.get_platform_post(post.video_id, post.platform)
