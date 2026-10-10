"""Request/response schemas for /api/queue (Milestone 3.9: Queue +
Calendar Functionality). Same "explicit response model, never return a raw
dataclass" convention as schemas/videos.py/cadence.py."""

from pydantic import BaseModel

from content_automation.api.schemas.captions import CaptionResponse


class QueueVideoSummary(BaseModel):
    """Just enough to identify the video occupying a slot — same
    "don't surface an opaque internal identifier with no user value"
    restraint as schemas/videos.py's own VideoResponse, but id has real
    value here (it's what Assign/Remove-from-schedule act on)."""

    id: int
    original_filename: str
    # Milestone 3.10 — the video's canonical publishing caption, same shape
    # as GET /api/videos/{id}/caption, so the Queue can show/edit it
    # without a second request per slot.
    caption: CaptionResponse


class PublicationStatusResponse(BaseModel):
    """One platform's publish state for the slot's video (Milestone 3.11).
    Platform-neutral: a second platform is just a second entry."""

    platform: str
    display_status: str
    platform_post_status: str
    published_at: str | None
    reason_code: str | None
    message: str | None
    action_hint: str | None
    # Milestone 4.2: "PROCESSING" / "PUBLISHING" for a two-step platform
    # (Instagram) while PUBLISHING; null otherwise.
    stage: str | None = None


class QueueSlotResponse(BaseModel):
    id: int
    scheduled_at: str
    timezone: str | None
    status: str  # raw content_slots.status — only ever "OPEN" or "ASSIGNED"
    # Milestone 3.11: OPEN | SCHEDULED | PUBLISHING | PUBLISHED | FAILED |
    # NEEDS_ATTENTION, from publishing.publish_status (the one resolver —
    # see its docstring for every NEEDS_ATTENTION trigger). Replaces 3.9's
    # ASSIGNED display value with SCHEDULED.
    display_status: str
    # A failure category (publishing.failure_taxonomy) or an attention code;
    # message is fixed, sanitized copy — never failure_reason/exception text.
    reason_code: str | None
    message: str | None
    # RECONNECT_ACCOUNT | EDIT_CAPTION | TRY_AGAIN_LATER | null — advisory
    # only; the retry action itself is POST .../retry (Milestone 3.13).
    action_hint: str | None
    published_at: str | None  # aware UTC, set only when PUBLISHED
    can_unassign: bool  # mirrors unassign_slot's guard (every post still PENDING)
    # Milestone 3.13: mirror POST /api/queue/slots/{id}/retry's guards — a
    # post is FAILED or UNKNOWN; confirmation is needed when one is UNKNOWN
    # with nothing to check (scheduling.manual_recovery).
    can_retry: bool
    retry_requires_confirmation: bool
    assigned_video: QueueVideoSummary | None
    platform_post_status: str | None
    publications: list[PublicationStatusResponse]


class QueueSlotListResponse(BaseModel):
    slots: list[QueueSlotResponse]


class AssignToSlotRequest(BaseModel):
    video_id: int
    # Milestone 4.2: which platforms to publish this video to. Omitted (or
    # null) keeps the configured default (TikTok). Each listed platform must
    # be publishable and connected — see scheduling/queue_assignment.py.
    platforms: list[str] | None = None


class AssignNextRequest(BaseModel):
    video_id: int
    platforms: list[str] | None = None


class RetryPublishRequest(BaseModel):
    """Milestone 3.13. confirm_not_published: the user checked the platform
    and the post isn't there — required to resubmit a post whose outcome is
    UNKNOWN with no platform id (see scheduling.manual_recovery)."""

    platform: str = "tiktok"
    confirm_not_published: bool = False
