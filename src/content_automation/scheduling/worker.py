"""
worker.py — One-pass worker execution: discover due platform_posts,
atomically claim each one, and execute the existing TikTok publish flow
(Milestone 2.1.4).

What it does:
  Connects the pieces already proven in isolation:

    due_post_selector.get_due_posts()          (2.1.1, corrected in 2.1.2)
      -> content_store.ContentStore.claim_platform_post()  (2.1.3)
      -> publish_tiktok.execute_claimed_platform_post()    (2.0)

  For every due PENDING platform_posts row, attempt to claim it. A
  successful claim executes the exact same proven publish path the manual
  CLI uses (execute_claimed_platform_post) — no duplicated TikTok
  publishing logic. A failed claim (another process won first, e.g. a
  concurrently-running worker or a manual publish_tiktok.py invocation) is
  skipped, not retried or treated as an error — that is the correct,
  expected outcome of losing a race, not a failure.

  One pass only: discovers whatever is due right now, attempts each once,
  and returns. No loop, no cron, no daemon, no retry/backoff, no
  stale-PUBLISHING recovery — see
  docs/evaluations/scheduling/milestone-2.1.4-worker-execution.md for why
  that is this milestone's deliberate boundary. If the process crashes
  after a successful claim but before completion, that row is left
  PUBLISHING; recovering it is explicitly deferred to a future
  crash-recovery milestone.

Run (Milestone 3.0: thin CLI entry point at cli/worker.py):
  python3 cli/worker.py
  python3 cli/worker.py --platform tiktok

Dependencies:
  content_automation.persistence.content_store,
  content_automation.scheduling.due_post_selector,
  content_automation.scheduling.publish_tiktok,
  content_automation.publishing.publisher,
  content_automation.publishing.tiktok.publisher, config.py.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from content_automation.persistence.content_store import ContentStore, PlatformPostRecord
from content_automation.publishing.publisher import Publisher
from content_automation.scheduling import due_post_selector
from content_automation.scheduling.publish_tiktok import PublishTikTokError, execute_claimed_platform_post
# Re-exported: log_event lives in scheduling/telemetry.py since Milestone 4.2.
from content_automation.scheduling.telemetry import log_event  # noqa: F401
from content_automation.storage.protocol import StorageProtocol


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()




@dataclass
class WorkerRunSummary:
    """One one-pass run's outcome. published/failed reflect the row's
    status immediately after execution — a row that ends this run still
    PUBLISHING (TikTok reported an in-progress, non-terminal status) is
    counted in neither; claimed - published - failed - retry_scheduled -
    len(errors) is exactly that count.

    retry_scheduled (Milestone 2.1.6) counts a claimed post whose
    execution failed with a retryable error (retry_classification.py) and
    was returned to PENDING with a future next_retry_at rather than marked
    FAILED — see publish_tiktok._schedule_retry_or_fail. A single
    retryable/terminal failure never aborts the rest of this pass; every
    other due post is still attempted."""
    discovered: int = 0
    claimed: int = 0
    skipped: int = 0
    published: int = 0
    failed: int = 0
    retry_scheduled: int = 0
    errors: list[str] = field(default_factory=list)


def run_due_posts_once(
    store: ContentStore, publisher: Publisher, *, platform: str = "tiktok", now: datetime | None = None,
    user_id: int | None = None, storage: StorageProtocol | None = None,
    due_posts: list[PlatformPostRecord] | None = None,
    lateness_seconds: Callable[[PlatformPostRecord], int] | None = None,
) -> WorkerRunSummary:
    """Discover due PENDING platform_posts rows for `platform`, attempt to
    atomically claim each one, and execute the proven publish flow for
    every successful claim. One pass — never loops, never waits, never
    retries a failed claim. `now` is forwarded to
    due_post_selector.get_due_posts() for deterministic testing (see that
    module's docstring for the naive-local-time convention this repository
    uses for scheduling).

    user_id (Milestone 3.2, ownership) is optional and, when supplied,
    scopes both discovery and claiming to that user's own rows only — the
    multi-tenant execution invariant
    (docs/architecture/hosted-product-boundary.md §5: "a hosted background
    job must never operate on one user's records using another user's
    credentials") applied to this specific job. Every real CLI invocation
    (cli/worker.py) resolves and passes the local user's id; omitting it
    preserves the exact pre-3.2 unscoped single-tenant behavior every
    existing test relies on. Making this required is deferred until a real
    multi-connection scheduler exists to always supply it — see the
    Milestone 3.2 evaluation record.

    storage (Milestone 3.4, object storage) is optional and forwarded
    unchanged to execute_claimed_platform_post — only consulted for a
    video with storage_provider set; every video without one (every
    pre-3.4 video, and every existing test) is completely unaffected by
    whether this is supplied.

    due_posts (Milestone 3.12) lets a caller supply its own already-selected
    due rows instead of due_post_selector.get_due_posts — the hosted worker
    does, because hosted slots are scheduled in each user's own timezone
    (scheduling/hosted_due_selection.py). Claiming and execution below are
    identical either way; only discovery differs.

    lateness_seconds (Milestone 3.14 follow-up, overdue telemetry) is an
    optional callable giving how late a post is right now; when supplied,
    post_claimed and the outcome events carry lateness_seconds so overdue
    publishing can be measured from logs. Logging only — it never affects
    what is claimed or how it runs.
    """
    summary = WorkerRunSummary()

    if due_posts is None:
        due_posts = due_post_selector.get_due_posts(store, platform, now=now, user_id=user_id)
    summary.discovered = len(due_posts)

    for post in due_posts:
        claimed = store.claim_platform_post(post.id, updated_at=_now_iso(), user_id=user_id)
        if not claimed:
            summary.skipped += 1
            log_event("claim_skipped", platform_post_row_id=post.id, video_id=post.video_id, platform=post.platform)
            continue
        summary.claimed += 1
        lateness = {"lateness_seconds": lateness_seconds(post)} if lateness_seconds else {}
        # previous_failure_code: why an earlier attempt didn't publish (a
        # retry), when there was one — the knowable part of "why late".
        log_event(
            "post_claimed", platform_post_row_id=post.id, video_id=post.video_id, platform=post.platform,
            scheduled_at=post.scheduled_at, user_id=post.user_id, retry_count=post.retry_count,
            claimed_at=_now_iso(), **lateness, previous_failure_code=post.failure_code,
        )
        log_event("publish_started", platform_post_row_id=post.id, video_id=post.video_id, platform=post.platform)

        try:
            execute_claimed_platform_post(store, post.video_id, post.platform, publisher, storage=storage)
        except PublishTikTokError as exc:
            summary.errors.append(str(exc))

        final = store.get_platform_post(post.video_id, post.platform)
        outcome = {"platform_post_row_id": post.id, "video_id": post.video_id, "platform": post.platform}
        if lateness_seconds:
            outcome["lateness_seconds"] = lateness_seconds(post)
        if final is not None and final.status == "PUBLISHED":
            summary.published += 1
            log_event("publish_succeeded", **outcome, status=final.status)
        elif final is not None and final.status == "FAILED":
            summary.failed += 1
            log_event("publish_failed", **outcome, status=final.status, failure_code=final.failure_code)
        elif final is not None and final.status == "PENDING" and final.next_retry_at is not None:
            # A row this pass claimed and executed can only be back at
            # PENDING because a retryable failure scheduled a retry —
            # never a plain never-attempted PENDING (this pass already
            # claimed it, so it can't still be in that state).
            summary.retry_scheduled += 1
            log_event(
                "retry_scheduled", **outcome, status=final.status, failure_code=final.failure_code,
                retry_count=final.retry_count, next_retry_at=final.next_retry_at,
            )
        elif final is not None and final.status == "PUBLISHING" and final.platform_post_id is not None:
            # Accepted by the platform, final outcome asynchronous —
            # reconciliation.py picks it up at next_status_check_at.
            log_event(
                "reconciliation_scheduled", **outcome, status=final.status,
                next_status_check_at=final.next_status_check_at,
            )

    return summary
