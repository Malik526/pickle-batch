"""
crash_recovery.py — One-pass recovery for interrupted PUBLISHING
platform_posts rows (Milestone 2.1.5).

What it does:
  Answers: "a worker died mid-job — how does this pipeline safely
  continue?" A row can be left PUBLISHING forever if the process that
  claimed it (worker.py or publish_tiktok.py, both via
  ContentStore.claim_platform_post()) crashes before reaching a terminal
  state. Ordinary due detection (due_post_selector.py) ignores PUBLISHING
  rows entirely — recovery is the only thing that ever looks at them again.

  Two distinct crash classes, handled differently:

    Case A — claimed but never submitted (platform_post_id IS NULL):
      no evidence TikTok ever accepted anything. Requeued to PENDING so
      the normal atomic-claim path (claim_platform_post) can pick it up
      again later — never republished directly here.

    Case B — submitted but unresolved (platform_post_id IS NOT NULL):
      TikTok already accepted the submission. Never resubmit the media —
      Milestone 2.0's idempotency rule holds unconditionally. Only
      publisher.get_status() (read-only) is called, and the row is
      updated to PUBLISHED/FAILED/left PUBLISHING exactly like an ordinary
      poll would.

  Milestone 3.13 (reconciliation + recovery) splits Case A by the
  submission checkpoint (platform_posts.submission_state — see
  publish_tiktok.py "Submission checkpoint"):

    A1 — NULL: claimed, never reached publisher.publish(). Requeued
      exactly as before.
    A2 — AWAITING_PLATFORM_ID: the publisher had started, but had not yet
      handed over its platform id — and it transfers no media before doing
      so. Nothing can have been posted, so this is a bounded retry: back to
      PENDING through the normal retry budget/backoff (failure_code
      SUBMISSION_INTERRUPTED), or FAILED once the budget is spent, so a row
      that crashes the worker every time cannot loop forever.
    A3 — SUBMITTING: a publisher without that guarantee was mid-request.
      It may have been accepted, and there is no id to ask about, so
      nothing is guessed: status UNKNOWN (failure_code
      SUBMISSION_OUTCOME_UNKNOWN). UNKNOWN rows are selected by no
      automatic job; only the manual retry API (scheduling/manual_recovery.py)
      releases them.

  Every decision is logged as one structured event with ids/codes only.

  A row is only "stale" — eligible for recovery at all — if
  config.PLATFORM_POST_STALE_MINUTES have passed since its updated_at with
  no further activity; a row a currently-running worker legitimately owns
  is left alone. Every recovery write is an optimistic-concurrency update
  (ContentStore.update_platform_post_if_unchanged) gated on the exact
  updated_at value read during selection, so recovery can only ever act on
  a row that is still the stale record it inspected — never one an active
  worker resumed and already moved on.

  One pass only: no daemon, no cron, no retry scheduler. See
  docs/evaluations/scheduling/milestone-2.1.5-crash-recovery.md.

Run (Milestone 3.0: thin CLI entry point at cli/crash_recovery.py):
  python3 cli/crash_recovery.py
  python3 cli/crash_recovery.py --platform tiktok

Dependencies:
  content_automation.persistence.content_store,
  content_automation.publishing.publisher,
  content_automation.publishing.tiktok.publisher, config.py.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from content_automation.config import MAX_RETRY_ATTEMPTS, PLATFORM_POST_STALE_MINUTES, RETRY_BACKOFF_MINUTES
from content_automation.persistence.content_store import ContentStore
from content_automation.publishing.publisher import PublishError, Publisher
from content_automation.scheduling.publish_tiktok import AWAITING_PLATFORM_ID, PREPARING_MEDIA, _resolve_poll_outcome
from content_automation.scheduling.slot_matcher import now_in_config_timezone
from content_automation.scheduling.finalization import finalize_ready_submission
from content_automation.scheduling.worker import log_event


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RecoverySummary:
    discovered: int = 0
    requeued: int = 0
    polled: int = 0
    published: int = 0
    failed: int = 0
    still_processing: int = 0
    retry_scheduled: int = 0
    unknown: int = 0
    errors: list = field(default_factory=list)


def recover_stale_posts_once(
    store: ContentStore,
    publisher: Publisher,
    *,
    platform: str = "tiktok",
    now: datetime | None = None,
    stale_after_minutes: int | None = None,
    user_id: int | None = None,
) -> RecoverySummary:
    """Find PUBLISHING platform_posts rows for `platform` that have had no
    activity for at least stale_after_minutes (default:
    config.PLATFORM_POST_STALE_MINUTES), and recover each one: requeue to
    PENDING if no platform_post_id was ever obtained (Case A), or poll
    TikTok's existing status if one was (Case B) — never resubmitting
    media either way. One pass — never loops, never waits.

    `now` must be an aware UTC datetime if supplied (matching how
    updated_at is always written) — pass a fixed value in tests rather
    than relying on wall-clock time.

    user_id (Milestone 3.2, ownership) is optional; see
    worker.run_due_posts_once's docstring for the same scoping contract —
    recovery only ever discovers and touches that user's own stale rows
    when supplied.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    threshold_minutes = (
        stale_after_minutes if stale_after_minutes is not None else PLATFORM_POST_STALE_MINUTES
    )
    stale_before_iso = (now - timedelta(minutes=threshold_minutes)).isoformat()

    summary = RecoverySummary()
    stale_rows = store.get_recoverable_platform_posts(platform, stale_before_iso, user_id=user_id)
    summary.discovered = len(stale_rows)

    for record in stale_rows:
        ids = {"platform_post_row_id": record.id, "video_id": record.video_id, "platform": record.platform}
        if record.platform_post_id is None:
            if record.submission_state is None:
                # Case A1: never reached the publisher — safe to requeue.
                requeued = store.update_platform_post_if_unchanged(
                    record.id, expected_updated_at=record.updated_at, updated_at=_now_iso(),
                    user_id=user_id, status="PENDING",
                )
                if requeued:
                    summary.requeued += 1
                    log_event("recovery_requeued", **ids, decision="NEVER_SUBMITTED")
            elif record.submission_state == AWAITING_PLATFORM_ID:
                _recover_interrupted_submission(store, record, summary, ids, user_id)
            elif record.submission_state == PREPARING_MEDIA:
                _recover_interrupted_preparation(store, record, summary, ids, user_id)
            else:
                # Case A3: may have been accepted; no id to check. Park it.
                parked = store.update_platform_post_if_unchanged(
                    record.id, expected_updated_at=record.updated_at, updated_at=_now_iso(), user_id=user_id,
                    status="UNKNOWN", failure_code="SUBMISSION_OUTCOME_UNKNOWN",
                    failure_reason="Worker stopped mid-submission; the platform may or may not have accepted it.",
                )
                if parked:
                    summary.unknown += 1
                    log_event("recovery_unknown", **ids, failure_code="SUBMISSION_OUTCOME_UNKNOWN")
            continue

        # Case B: TikTok already has this submission — never call
        # publisher.publish() again, only check its status.
        summary.polled += 1
        try:
            status_result = publisher.get_status(record.platform_post_id)
        except PublishError as exc:
            # Left PUBLISHING: reconciliation.py owns rows with an id and
            # decides between rescheduling and parking (Milestone 3.13).
            summary.errors.append(str(exc))
            log_event("recovery_poll_failed", **ids, failure_code=exc.reason_code)
            continue

        # Milestone 2.1.10: the actual status->fields decision is shared
        # with publish_tiktok.py's inline poll and reconciliation.py's
        # routine checks (publish_tiktok._resolve_poll_outcome) — never a
        # second independently-maintained copy of this mapping. Only the
        # still-processing branch stays crash-recovery-specific: it
        # deliberately does NOT write next_status_check_at/
        # status_check_count the way reconciliation.py does, for the same
        # reason it never refreshed updated_at — see the comment below.
        outcome, fields = _resolve_poll_outcome(status_result)
        if outcome == "READY":
            # Milestone 4.2: processed but not posted (Instagram container
            # FINISHED) — finalization decides whether posting is safe.
            result = finalize_ready_submission(store, record, publisher, now=now, user_id=user_id)
            if result == "PUBLISHED":
                summary.published += 1
            elif result == "UNKNOWN":
                summary.unknown += 1
            else:
                summary.still_processing += 1
            log_event("recovery_polled", **ids, outcome=result)
        elif outcome == "PUBLISHED":
            updated = store.update_platform_post_if_unchanged(
                record.id, expected_updated_at=record.updated_at, updated_at=_now_iso(), user_id=user_id, **fields
            )
            if updated:
                summary.published += 1
                log_event("recovery_polled", **ids, outcome=outcome)
        elif outcome == "FAILED":
            updated = store.update_platform_post_if_unchanged(
                record.id, expected_updated_at=record.updated_at, updated_at=_now_iso(), user_id=user_id, **fields
            )
            if updated:
                summary.failed += 1
                log_event("recovery_polled", **ids, outcome=outcome, failure_code=fields.get("failure_code"))
        else:
            # Not yet final — leave the row exactly as-is (no write, same
            # as an ordinary poll's "not yet final" outcome). updated_at is
            # deliberately NOT refreshed here: doing so would reset the
            # staleness clock and could delay the next recovery pass from
            # rechecking an already-complete job for a full threshold
            # period. Leaving it untouched means the next recovery run
            # checks again immediately, which is safe — get_status() is
            # read-only and idempotent. (Milestone 2.1.10: this is
            # precisely why crash recovery stays the safety net and
            # reconciliation.py — which DOES schedule next_status_check_at
            # — is the routine path; see that module's docstring.)
            summary.still_processing += 1

    return summary


def _recover_interrupted_submission(store, record, summary: RecoverySummary, ids: dict, user_id) -> None:
    """Case A2 (Milestone 3.13): a checkpointed submission that never got
    its platform id persisted — so no media was transferred. Retried
    through the same budget and backoff as any retryable publishing
    failure (publish_tiktok._schedule_retry_or_fail), not requeued for
    free, so a post that crashes the worker every time ends FAILED."""
    if record.retry_count < MAX_RETRY_ATTEMPTS:
        next_retry_at = (
            now_in_config_timezone() + timedelta(minutes=RETRY_BACKOFF_MINUTES[record.retry_count])
        ).isoformat()
        moved = store.update_platform_post_if_unchanged(
            record.id, expected_updated_at=record.updated_at, updated_at=_now_iso(), user_id=user_id,
            status="PENDING", retry_count=record.retry_count + 1, next_retry_at=next_retry_at,
            submission_state=None, failure_code="SUBMISSION_INTERRUPTED",
            failure_reason="Worker stopped before the platform issued an id; no media was sent.",
        )
        if moved:
            summary.retry_scheduled += 1
            log_event("recovery_retry_scheduled", **ids, decision="NO_MEDIA_SENT",
                      retry_count=record.retry_count + 1, next_retry_at=next_retry_at)
        return
    moved = store.update_platform_post_if_unchanged(
        record.id, expected_updated_at=record.updated_at, updated_at=_now_iso(), user_id=user_id,
        status="FAILED", submission_state=None, failure_code="SUBMISSION_INTERRUPTED",
        failure_reason="Worker repeatedly stopped before the platform issued an id; retries exhausted.",
    )
    if moved:
        summary.failed += 1
        log_event("recovery_failed", **ids, decision="RETRIES_EXHAUSTED", failure_code="SUBMISSION_INTERRUPTED")


# Milestone 4.2.3: the failure code an interrupted media preparation gets.
PREPARATION_INTERRUPTED = "INSTAGRAM_MEDIA_PREPARATION_INTERRUPTED"
PREPARATION_RETRY_DELAY_MINUTES = 15


def _recover_interrupted_preparation(store, record, summary: RecoverySummary, ids: dict, user_id) -> None:
    """Case A4 (Milestone 4.2.3): the worker died while preparing media —
    nothing was sent to the platform. The heartbeat keeps a live encode
    fresh, so a stale PREPARING_MEDIA row means the worker process itself
    stopped: a deploy restart, or the container being OOM-killed by the
    encode. Before 4.2.3 this fell into Case A1 and was requeued for free
    forever — an encode that kills the container would loop every
    PLATFORM_POST_STALE_MINUTES. Now it's retried once, after
    PREPARATION_RETRY_DELAY_MINUTES (a deploy restart recovers on its own);
    a second interruption parks it FAILED for an explicit Retry."""
    if record.failure_code == PREPARATION_INTERRUPTED:
        moved = store.update_platform_post_if_unchanged(
            record.id, expected_updated_at=record.updated_at, updated_at=_now_iso(), user_id=user_id,
            status="FAILED", submission_state=None, failure_code=PREPARATION_INTERRUPTED,
            failure_reason="The worker stopped twice while preparing this video's media; waiting for an explicit Retry.",
        )
        if moved:
            summary.failed += 1
            log_event("recovery_failed", **ids, decision="PREPARATION_INTERRUPTED_TWICE", failure_code=PREPARATION_INTERRUPTED)
        return
    next_retry_at = (now_in_config_timezone() + timedelta(minutes=PREPARATION_RETRY_DELAY_MINUTES)).isoformat()
    moved = store.update_platform_post_if_unchanged(
        record.id, expected_updated_at=record.updated_at, updated_at=_now_iso(), user_id=user_id,
        status="PENDING", retry_count=record.retry_count + 1, next_retry_at=next_retry_at, submission_state=None,
        failure_code=PREPARATION_INTERRUPTED,
        failure_reason="The worker stopped while preparing this video's media; nothing was sent to the platform.",
    )
    if moved:
        summary.retry_scheduled += 1
        log_event("recovery_retry_scheduled", **ids, decision="PREPARATION_INTERRUPTED",
                  retry_count=record.retry_count + 1, next_retry_at=next_retry_at)
