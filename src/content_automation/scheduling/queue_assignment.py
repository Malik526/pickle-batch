"""
queue_assignment.py — On-demand video-to-slot assignment for the hosted
Queue (Milestone 3.9: Queue + Calendar Functionality).

What it does:
  Glues together three already-existing primitives exactly the way
  media/processing.py's local-ingestion pipeline already does — assign a
  slot, then materialize its platform_posts row(s) — so an already-uploaded
  hosted video (api/routes/videos.py's POST /api/videos) can be assigned
  the same way outside that CLI ingestion pipeline: manually (a specific
  slot the caller picked) or automatically (the earliest eligible OPEN
  slot, via scheduling.slot_matcher.select_slot_fifo — the same FIFO rule,
  not a second scheduler).

  Deliberately does not duplicate assign_slot()'s own ownership/
  availability checks (SlotUnavailableError, OwnershipMismatchError) — it
  relies on those being enforced inside assign_slot() itself, and only
  adds the "which slot" resolution and the one guard assign_slot doesn't
  make: a video already occupying a slot must be explicitly removed from
  its current schedule (see persistence.content_store.unassign_slot)
  before it can be assigned to a different one, so a slot is never left
  ASSIGNED to a video whose own assigned_slot_id has silently moved on.

Dependencies:
  persistence.protocol.ContentStoreProtocol, scheduling.slot_matcher,
  scheduling.platform_post_materializer.
"""

from datetime import datetime

from content_automation.persistence.content_store import SlotRecord
from content_automation.persistence.protocol import ContentStoreProtocol
from content_automation.publishing.platforms import get_platform, publishable
from content_automation.scheduling.platform_post_materializer import materialize_platform_posts_for_assignment
from content_automation.scheduling.slot_matcher import select_slot_fifo


class VideoAlreadyScheduledError(Exception):
    """Raised when the requested video already has an assigned_slot_id —
    remove it from its current schedule first (see
    persistence.content_store.unassign_slot) rather than assigning it to a
    second slot, which would leave the first slot stuck ASSIGNED to a video
    whose own assigned_slot_id no longer points back to it."""


class InvalidPlatformSelectionError(Exception):
    """Milestone 4.2: an explicit platforms list named an unknown platform,
    one that can't publish yet, or was empty."""


class PlatformNotConnectedError(Exception):
    """Milestone 4.2: an explicitly chosen platform has no ACTIVE connection
    for this user, so its post could never publish."""


def resolve_target_platforms(store: ContentStoreProtocol, user_id: int | None, requested: list[str] | None) -> list[str] | None:
    """None (the caller didn't choose) keeps the configured default, exactly
    as before Milestone 4.2. An explicit list is deduplicated (order kept)
    and every entry must be publishable and connected for user_id —
    checked before anything is assigned, so a bad choice changes nothing."""
    if requested is None:
        return None
    chosen = list(dict.fromkeys(requested))
    if not chosen:
        raise InvalidPlatformSelectionError("Choose at least one platform to publish to.")
    unusable = [platform for platform in chosen if platform not in publishable([platform])]
    if unusable:
        raise InvalidPlatformSelectionError(f"Can't publish to: {', '.join(unusable)}.")
    for platform in chosen:
        connection = store.get_platform_connection(user_id, platform) if user_id is not None else None
        if connection is None or connection.status != "ACTIVE":
            raise PlatformNotConnectedError(f"Connect {get_platform(platform).label} in Settings before scheduling to it.")
    return chosen


class NoOpenSlotAvailableError(Exception):
    """Raised by assign_video_to_next_open_slot when the caller has no
    eligible OPEN slot at all — e.g. no active posting cadence, or every
    generated slot is already claimed."""


def assign_video_to_slot(
    store: ContentStoreProtocol, video_id: int, slot_id: int, created_at: str, user_id: int | None = None,
    platforms: list[str] | None = None,
) -> SlotRecord:
    """Manual assignment: claim slot_id for video_id, then materialize its
    platform_posts row(s). Re-raises assign_slot's own SlotUnavailableError/
    OwnershipMismatchError unchanged — the caller (api/routes/queue.py)
    maps those to HTTP statuses, exactly like every other store-exception
    route in this codebase."""
    video = store.get_video(video_id)
    if video is not None and video.assigned_slot_id is not None:
        raise VideoAlreadyScheduledError(
            f"video {video_id} is already assigned to content_slot {video.assigned_slot_id} — "
            "remove it from schedule before assigning it elsewhere."
        )

    targets = resolve_target_platforms(store, user_id, platforms)
    store.assign_slot(video_id, slot_id)
    materialize_platform_posts_for_assignment(store, video_id, slot_id, created_at, user_id=user_id, platforms=targets)
    return store.get_slot(slot_id)


def assign_video_to_next_open_slot(
    store: ContentStoreProtocol, video_id: int, created_at: str, user_id: int, now: datetime | None = None,
    platforms: list[str] | None = None,
) -> SlotRecord:
    """Automatic/FIFO assignment: the same "first eligible video -> earliest
    OPEN slot" rule media/processing.py already applies during local
    ingestion, invoked here on demand for one specific already-uploaded
    video instead of as part of a full ingestion run. Raises
    NoOpenSlotAvailableError if the user has no eligible OPEN slot right
    now (e.g. an inactive or unconfigured cadence).

    now is optional and forwarded unchanged to select_slot_fifo — the same
    deterministic-testing override tests/test_slot_matcher.py already
    relies on; the real API route (api/routes/queue.py) never passes it,
    so production behavior always uses the real wall clock."""
    resolve_target_platforms(store, user_id, platforms)  # fail before choosing a slot
    slot = select_slot_fifo(store, now=now, user_id=user_id)
    if slot is None:
        raise NoOpenSlotAvailableError("No open posting slot is available yet — check your posting cadence.")
    return assign_video_to_slot(store, video_id, slot.id, created_at, user_id=user_id, platforms=platforms)
