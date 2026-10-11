"""
routes/queue.py — the hosted Queue's real backend surface: listing slots
with their occupying video/publish state, and video-to-slot assignment/
unassignment (Milestone 3.9: Queue + Calendar Functionality).

What it does:
  GET /api/queue/slots?from=&to=  — the caller's content_slots in
                                     [from, to) (ContentStoreProtocol.
                                     list_content_slots_for_user, the same
                                     read Milestone 3.8 built for the
                                     cadence preview), each widened with
                                     its assigned video (if any) and a
                                     derived display_status.
  POST /api/queue/slots/{slot_id}/assign   — manual assignment: {video_id}.
  POST /api/queue/assign-next              — automatic/FIFO assignment:
                                              {video_id}.
  POST /api/queue/slots/{slot_id}/unassign — "Remove from schedule."
  POST /api/queue/slots/{slot_id}/retry    — Milestone 3.13: retry/recover
                                              the slot's FAILED or UNKNOWN
                                              platform post
                                              ({platform, confirm_not_published};
                                              scheduling.manual_recovery has
                                              the rules). 409 for a post that
                                              is PENDING/PUBLISHING/PUBLISHED,
                                              needs confirmation, or changed
                                              concurrently.

  Milestone 3.11: display_status (and the sanitized reason/message/
  action_hint/published_at/can_unassign/publications fields) now comes from
  publishing.publish_status.resolve_slot_publish_status — the one
  platform-neutral resolver; this route only shapes its output. The 3.9
  notes below still describe why it is derived rather than stored.

  display_status vs. status: content_slots.status only ever holds OPEN or
  ASSIGNED in this codebase — no code path has ever written PUBLISHED or
  FAILED onto a content_slot (confirmed directly: the only two
  `UPDATE content_slots ... SET status` statements in this codebase are
  both inside assign_slot(), both writing 'ASSIGNED'). See
  docs/decisions/0005-fifo-baseline-and-optional-strategy-routing.md's own
  "Publishing-readiness state model" as a deferred future direction. The
  real "did it publish" signal lives on platform_posts.status
  (PENDING/PUBLISHING/PUBLISHED/FAILED — written by scheduling/worker.py).
  display_status is computed here, at this read boundary only, by looking
  up the assigned video's platform_posts row(s) — content_slots.status
  itself is never written to PUBLISHED/FAILED by this route, so
  scheduling/worker.py's existing, already-validated semantics stay
  completely untouched.

  Assignment (manual or FIFO) is two existing primitives glued together by
  scheduling.queue_assignment exactly like media/processing.py's local-
  ingestion pipeline already does: ContentStoreProtocol.assign_slot, then
  scheduling.platform_post_materializer.materialize_platform_posts_for_assignment.
  Unassignment is the new, symmetric ContentStoreProtocol.unassign_slot.

  Ownership: every route depends on get_current_user/get_store exactly
  like videos.py/cadence.py, and a slot/video not owned by the caller is
  reported as 404 — the same "not yours" and "doesn't exist" are
  indistinguishable convention used throughout this API.

Dependencies:
  api.dependencies.auth (get_current_user, get_store),
  scheduling.queue_assignment, persistence.protocol.ContentStoreProtocol.
"""

from datetime import datetime
from datetime import timezone as dt_timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from content_automation.api.dependencies.auth import get_current_user, get_store
from content_automation.api.routes.captions import to_caption_response
from content_automation.api.schemas.queue import (
    AssignNextRequest,
    PublicationStatusResponse,
    AssignToSlotRequest,
    QueueSlotListResponse,
    QueueSlotResponse,
    QueueVideoSummary,
    RetryPublishRequest,
)
from content_automation.persistence.content_store import (
    OwnershipMismatchError,
    PlatformPostInProgressError,
    SlotRecord,
    SlotUnavailableError,
    UserRecord,
)
from content_automation.persistence.protocol import ContentStoreProtocol
from content_automation.publishing.publish_status import resolve_slot_publish_status
from content_automation.scheduling.manual_recovery import RetryRejectedError, retry_platform_post, retry_slot_posts
from content_automation.scheduling.queue_assignment import (
    InvalidPlatformSelectionError,
    NoOpenSlotAvailableError,
    PlatformNotConnectedError,
    VideoAlreadyScheduledError,
    assign_video_to_next_open_slot,
    assign_video_to_slot,
)

router = APIRouter()


def _now_iso() -> str:
    return datetime.now(dt_timezone.utc).isoformat()


def _to_queue_slot_response(store: ContentStoreProtocol, slot: SlotRecord) -> QueueSlotResponse:
    assigned_video: QueueVideoSummary | None = None
    posts = []

    if slot.assigned_video_id is not None:
        video = store.get_video(slot.assigned_video_id)
        if video is not None:
            assigned_video = QueueVideoSummary(
                id=video.id, original_filename=video.original_filename, caption=to_caption_response(store, video),
            )
        posts = store.list_platform_posts_for_video(slot.assigned_video_id)

    publish = resolve_slot_publish_status(slot, posts)

    return QueueSlotResponse(
        id=slot.id, scheduled_at=slot.scheduled_at, timezone=slot.timezone,
        status=slot.status, display_status=publish.display_status,
        reason_code=publish.reason_code, message=publish.message, action_hint=publish.action_hint,
        published_at=publish.published_at, can_unassign=publish.can_unassign,
        can_retry=publish.can_retry, retry_requires_confirmation=publish.retry_requires_confirmation,
        assigned_video=assigned_video,
        # Single-platform in practice today (config.TARGET_PUBLISHING_PLATFORMS
        # defaults to just "tiktok"); publications carries every platform.
        platform_post_status=posts[0].status if posts else None,
        publications=[PublicationStatusResponse(**vars(p)) for p in publish.publications],
    )


def _get_owned_slot(store: ContentStoreProtocol, slot_id: int, user_id: int) -> SlotRecord:
    slot = store.get_slot(slot_id)
    if slot is None or slot.user_id != user_id:
        raise HTTPException(status_code=404, detail="No content slot with that id.")
    return slot


def _get_owned_video_id(store: ContentStoreProtocol, video_id: int, user_id: int) -> None:
    video = store.get_video(video_id)
    if video is None or video.user_id != user_id:
        raise HTTPException(status_code=404, detail="No video with that id.")


@router.get("/queue/slots", response_model=QueueSlotListResponse)
def list_queue_slots(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    user: UserRecord = Depends(get_current_user),
    store: ContentStoreProtocol = Depends(get_store),
) -> QueueSlotListResponse:
    slots = store.list_content_slots_for_user(user.id, from_, to)
    return QueueSlotListResponse(slots=[_to_queue_slot_response(store, s) for s in slots])


@router.post("/queue/slots/{slot_id}/assign", response_model=QueueSlotResponse)
def assign_slot_manually(
    slot_id: int,
    body: AssignToSlotRequest,
    user: UserRecord = Depends(get_current_user),
    store: ContentStoreProtocol = Depends(get_store),
) -> QueueSlotResponse:
    _get_owned_slot(store, slot_id, user.id)
    _get_owned_video_id(store, body.video_id, user.id)
    try:
        slot = assign_video_to_slot(store, body.video_id, slot_id, _now_iso(), user_id=user.id, platforms=body.platforms)
    except InvalidPlatformSelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlatformNotConnectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SlotUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (OwnershipMismatchError, VideoAlreadyScheduledError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _to_queue_slot_response(store, slot)


@router.post("/queue/assign-next", response_model=QueueSlotResponse)
def assign_next_open_slot(
    body: AssignNextRequest,
    user: UserRecord = Depends(get_current_user),
    store: ContentStoreProtocol = Depends(get_store),
) -> QueueSlotResponse:
    _get_owned_video_id(store, body.video_id, user.id)
    try:
        slot = assign_video_to_next_open_slot(store, body.video_id, _now_iso(), user_id=user.id, platforms=body.platforms)
    except InvalidPlatformSelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlatformNotConnectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except NoOpenSlotAvailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except VideoAlreadyScheduledError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _to_queue_slot_response(store, slot)


@router.post("/queue/slots/{slot_id}/unassign", response_model=QueueSlotResponse)
def unassign_slot(
    slot_id: int,
    user: UserRecord = Depends(get_current_user),
    store: ContentStoreProtocol = Depends(get_store),
) -> QueueSlotResponse:
    _get_owned_slot(store, slot_id, user.id)
    try:
        store.unassign_slot(slot_id)
    except SlotUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PlatformPostInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _to_queue_slot_response(store, store.get_slot(slot_id))


@router.post("/queue/slots/{slot_id}/retry", response_model=QueueSlotResponse)
def retry_slot_publication(
    slot_id: int,
    body: RetryPublishRequest | None = None,
    user: UserRecord = Depends(get_current_user),
    store: ContentStoreProtocol = Depends(get_store),
) -> QueueSlotResponse:
    body = body or RetryPublishRequest()
    slot = _get_owned_slot(store, slot_id, user.id)
    if body.platform is not None:
        post = store.get_platform_post(slot.assigned_video_id, body.platform) if slot.assigned_video_id else None
        posts = [post] if post is not None and post.user_id == user.id else []
    else:
        posts = [p for p in store.list_platform_posts_for_video(slot.assigned_video_id) if p.user_id == user.id] if slot.assigned_video_id else []
    if not posts:
        raise HTTPException(status_code=404, detail="No platform post to retry for this slot.")
    try:
        if body.platform is not None:
            retry_platform_post(store, posts[0], user_id=user.id, confirm_not_published=body.confirm_not_published)
        else:
            retry_slot_posts(store, posts, user_id=user.id, confirm_not_published=body.confirm_not_published)
    except RetryRejectedError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)})
    return _to_queue_slot_response(store, store.get_slot(slot_id))
