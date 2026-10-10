"""
platform_post_materializer.py — Materializes intended platform deliveries
for a video as soon as it is assigned a content_slot (Milestone 2.1.2).

What it does:
  Closes the architectural gap Milestone 2.1.1 found and reported (see
  docs/evaluations/scheduling/milestone-2.1.1-due-post-detection.md):
  ContentStore.assign_slot() previously left no trace in platform_posts at
  all, so due_post_selector.get_due_posts() could never discover a
  scheduled delivery until someone manually ran publish_tiktok.py for that
  video at least once. materialize_platform_posts_for_assignment() creates
  one PENDING platform_posts row per config.TARGET_PUBLISHING_PLATFORMS
  entry, with scheduled_at copied exactly from the assigned content_slot,
  called right after assign_slot() succeeds (see process_content.py).

  Deliberately NOT folded into ContentStore.assign_slot() itself: that
  primitive's job is narrowly "atomically claim a slot for a video," has
  existing callers/tests with no platform notion (e.g.
  tests/test_slot_matcher.py), and widening its contract to also decide
  platform-delivery intent would widen it for every caller. This is called
  as a clearly separate, additive step immediately after assign_slot()
  succeeds — not perfectly atomic with the slot claim itself (a crash
  between the two calls would leave the pre-2.1.2 gap for that one video),
  but crash recovery is explicitly out of this milestone's scope; see the
  evaluation doc.

  Idempotent and non-destructive by construction:
  ContentStore.insert_platform_post_if_missing() is INSERT OR IGNORE
  against the existing UNIQUE(video_id, platform) constraint — a repeated
  call for an already-materialized (video, platform) pair is a silent
  no-op. It never runs an UPDATE, so it can never reset an existing row's
  status/platform_post_id/published_at/failure_reason, regardless of how
  many times assignment/materialization logic is revisited for the same
  video.

  Only "tiktok" is a real publisher today (see publisher.build_publisher).
  config.TARGET_PUBLISHING_PLATFORMS is a plain list specifically so a
  second platform is a one-line config change plus a real Publisher
  implementation later — not a change to this function.

Dependencies:
  content_store.py, config.TARGET_PUBLISHING_PLATFORMS.
"""

from content_automation.persistence.content_store import ContentStore
from content_automation.config import TARGET_PUBLISHING_PLATFORMS
from content_automation.publishing.platforms import publishable


def materialize_platform_posts_for_assignment(
    store: ContentStore, video_id: int, slot_id: int, created_at: str, user_id: int | None = None,
    platforms: list[str] | None = None,
) -> None:
    """Create a PENDING platform_posts row (scheduled_at = the assigned
    slot's scheduled_at) for every platform in
    config.TARGET_PUBLISHING_PLATFORMS, unless one already exists for that
    (video, platform) pair. Call this right after
    ContentStore.assign_slot(video_id, slot_id) succeeds. Idempotent — safe
    to call more than once for the same assignment; never touches an
    existing row's publishing state.

    user_id (Milestone 3.2, ownership) is optional and forwarded unchanged
    to ContentStore.insert_platform_post_if_missing — the real production
    caller (media.processing.process_one) always passes the assigned
    video's own user_id, so every materialized platform_posts row is owned
    by the same user as its video from the moment it's created.
    """
    slot = store.get_slot(slot_id)
    if slot is None:
        raise ValueError(f"content_slot {slot_id} not found — was assign_slot() called first?")

    # Milestone 4.0: only platforms with a hosted publisher get a row —
    # listing "instagram" before Milestone 4.2 must not create posts that
    # nothing will ever publish (they'd surface as SCHEDULE_MISSED).
    # Milestone 4.2: `platforms` is an explicit, already-validated choice
    # (scheduling/queue_assignment.resolve_target_platforms); None keeps the
    # configured default.
    targets = publishable(platforms if platforms is not None else TARGET_PUBLISHING_PLATFORMS)
    for platform in targets:
        store.insert_platform_post_if_missing(
            video_id, platform, scheduled_at=slot.scheduled_at, created_at=created_at, user_id=user_id
        )
