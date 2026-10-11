"""
failure_taxonomy.py — turns a stored failure code into a safe, user-facing
explanation (Milestone 3.11: User-Facing Publish States + Errors).

What it does:
  describe_failure(platform, failure_code) maps platform_posts.failure_code
  (never the free-text failure_reason) onto one of a small, stable set of
  platform-neutral categories, each with fixed copy and an optional action
  hint. The output only ever contains text written in this module — no
  exception strings, raw API responses, request IDs or token data can pass
  through, because the input code is used purely as a lookup key.

  Lookup order: codes this codebase raises itself (shared by every
  platform — _SHARED_CODE_CATEGORIES), then the platform's own code table
  (e.g. publishing/tiktok/failure_codes.py), then UNKNOWN_ERROR. A NULL
  code (every row failed before 3.11) is UNKNOWN_ERROR; failure_reason's
  text is never parsed to guess better.

  Adding Instagram/YouTube (Milestone 4) means adding a label to
  PLATFORM_LABELS and a code table to _PLATFORM_CODE_TABLES — categories,
  copy and the Queue API shape stay the same.

Dependencies:
  publishing.tiktok.failure_codes.
"""

from dataclasses import dataclass

from content_automation.publishing.platforms import PLATFORMS
from content_automation.publishing.tiktok.failure_codes import TIKTOK_FAILURE_CATEGORIES

# Milestone 4.0: labels come from the platform registry (publishing/platforms.py).
PLATFORM_LABELS = {platform.id: platform.label for platform in PLATFORMS.values()}

# Action hints are stable codes; the frontend owns their button/link text.
RECONNECT_ACCOUNT = "RECONNECT_ACCOUNT"
EDIT_CAPTION = "EDIT_CAPTION"
TRY_AGAIN_LATER = "TRY_AGAIN_LATER"

# Codes raised by this codebase itself (publishing/publisher.py,
# publishing/tiktok/{publisher,auth}.py, scheduling/publish_tiktok.py).
_SHARED_CODE_CATEGORIES: dict[str, str] = {
    "REAUTHORIZATION_REQUIRED": "AUTH_REQUIRED",
    "AUTH_ERROR": "AUTH_REQUIRED",
    # A token-endpoint failure that was NOT a 4xx rejection (those are
    # upgraded to REAUTHORIZATION_REQUIRED in publishing/tiktok/auth.py).
    "AUTH_HTTP_ERROR": "TEMPORARY_PLATFORM_ERROR",
    "CAPTION_TOO_LONG": "CAPTION_INVALID",
    # No longer raised since the Milestone 3.14 follow-up (captions are
    # optional); kept so rows that failed with it before still explain
    # themselves. Such a row recovers with Retry.
    "CAPTION_MISSING": "CAPTION_INVALID",
    "CORRUPT_MEDIA": "MEDIA_INVALID",
    "VIDEO_TOO_LONG": "MEDIA_INVALID",
    "MEDIA_INCOMPATIBLE": "MEDIA_INVALID",
    "LOCAL_FILE_MISSING": "MEDIA_UNAVAILABLE",
    "STORAGE_UNAVAILABLE": "MEDIA_UNAVAILABLE",
    # Milestone 3.12 (hosted worker): media materialization + inspection.
    "STORAGE_NETWORK_ERROR": "MEDIA_UNAVAILABLE",
    "STORAGE_HTTP_ERROR": "MEDIA_UNAVAILABLE",
    "STORAGE_OBJECT_MISSING": "MEDIA_UNAVAILABLE",
    "MEDIA_OWNERSHIP_MISMATCH": "MEDIA_UNAVAILABLE",
    "NO_AUDIO_STREAM": "MEDIA_INVALID",
    "UNSUPPORTED_CODEC": "MEDIA_INVALID",
    "CREDENTIAL_UNAVAILABLE": "AUTH_REQUIRED",
    "SELF_ONLY_UNAVAILABLE": "PLATFORM_REJECTED",
    "UNSUPPORTED_PRIVACY_LEVEL": "PLATFORM_REJECTED",
    "UNAUDITED_CLIENT_PRIVACY_RESTRICTION": "PLATFORM_REJECTED",
    "PLATFORM_REPORTED_FAILURE": "PLATFORM_REJECTED",
    "NETWORK_ERROR": "NETWORK_ERROR",
    "UPLOAD_NETWORK_ERROR": "NETWORK_ERROR",
    "MALFORMED_RESPONSE": "TEMPORARY_PLATFORM_ERROR",
    # Milestone 3.13 (reconciliation + recovery).
    "CREDENTIAL_REFRESH_BUSY": "TEMPORARY_PLATFORM_ERROR",
    # Crash after a submission started but before the platform issued an id
    # (nothing was sent — see scheduling/crash_recovery.py).
    "SUBMISSION_INTERRUPTED": "TEMPORARY_PLATFORM_ERROR",
    "UPLOAD_FAILED": "TEMPORARY_PLATFORM_ERROR",
    # Milestone 4.2 (Instagram Reels): pre-submission media/caption checks
    # (publishing/instagram/media_requirements.py), Meta API errors
    # (publishing/instagram/content_publishing.py), container outcomes
    # (publishing/instagram/publisher.py) and an unconfirmed publish request
    # (scheduling/finalization.py).
    "INSTAGRAM_MEDIA_UNSUPPORTED_FORMAT": "MEDIA_INVALID",
    "INSTAGRAM_MEDIA_UNSUPPORTED_CODEC": "MEDIA_INVALID",
    "INSTAGRAM_MEDIA_DURATION": "MEDIA_INVALID",
    "INSTAGRAM_MEDIA_FRAME_RATE": "MEDIA_INVALID",
    "INSTAGRAM_MEDIA_TOO_LARGE": "MEDIA_INVALID",
    "INSTAGRAM_MEDIA_RESOLUTION": "MEDIA_INVALID",
    "INSTAGRAM_CAPTION_TOO_MANY_HASHTAGS": "CAPTION_INVALID",
    "INSTAGRAM_CAPTION_TOO_MANY_MENTIONS": "CAPTION_INVALID",
    "INSTAGRAM_MEDIA_NOT_READY": "TEMPORARY_PLATFORM_ERROR",
    "INSTAGRAM_TRANSIENT_ERROR": "TEMPORARY_PLATFORM_ERROR",
    "INSTAGRAM_MEDIA_FETCH_FAILED": "MEDIA_UNAVAILABLE",
    "INSTAGRAM_RATE_LIMITED": "RATE_LIMITED",
    "INSTAGRAM_REQUEST_REJECTED": "PLATFORM_REJECTED",
    "INSTAGRAM_CONTAINER_ERROR": "PLATFORM_REJECTED",
    "INSTAGRAM_CONTAINER_EXPIRED": "PLATFORM_REJECTED",
    "PUBLISH_OUTCOME_UNKNOWN": "UNKNOWN_ERROR",
    # Milestone 4.2.1: making an Instagram-compatible derivative failed.
    "INSTAGRAM_MEDIA_PREPARATION_FAILED": "MEDIA_INVALID",
    "INSTAGRAM_MEDIA_PREPARATION_TIMEOUT": "MEDIA_INVALID",
    # Milestone 4.2.3: the encode was killed (resource exhaustion) or interrupted.
    "INSTAGRAM_MEDIA_PREPARATION_KILLED": "TEMPORARY_PLATFORM_ERROR",
    "INSTAGRAM_MEDIA_PREPARATION_INTERRUPTED": "TEMPORARY_PLATFORM_ERROR",
    # HTTP_ERROR / PUBLISH_FAILED / TIKTOK_API_ERROR / PRECONDITION_FAILED
    # are deliberately unlisted: without the HTTP status (not persisted)
    # they could be anything, so they fall through to UNKNOWN_ERROR.
}

_PLATFORM_CODE_TABLES: dict[str, dict[str, str]] = {"tiktok": TIKTOK_FAILURE_CATEGORIES}

# category -> (message template, action hint). {platform} is the platform label.
_CATEGORY_COPY: dict[str, tuple[str, str | None]] = {
    "AUTH_REQUIRED": ("{platform} connection needs to be renewed. Reconnect your account in Settings.", RECONNECT_ACCOUNT),
    "CAPTION_INVALID": ("{platform} didn't accept this caption.", EDIT_CAPTION),
    "MEDIA_INVALID": ("{platform} couldn't process this video file (format, length or resolution).", None),
    "MEDIA_UNAVAILABLE": ("The video file couldn't be loaded for publishing.", None),
    "PLATFORM_REJECTED": ("{platform} declined to publish this post.", None),
    "RATE_LIMITED": ("{platform} is limiting how often this account can post right now. Try again later.", TRY_AGAIN_LATER),
    "TEMPORARY_PLATFORM_ERROR": ("{platform} had a temporary problem. Try again later.", TRY_AGAIN_LATER),
    "NETWORK_ERROR": ("Couldn't reach {platform}. Try again later.", TRY_AGAIN_LATER),
    "UNKNOWN_ERROR": ("Publishing to {platform} failed for an unexpected reason.", None),
}

# A few codes deserve a more specific sentence than their category's.
_CODE_MESSAGE_OVERRIDES: dict[str, str] = {
    "CAPTION_TOO_LONG": "Caption is too long for {platform}.",
    "CAPTION_MISSING": "This video has no caption yet.",
    # Milestone 4.2 — the actionable cases for Instagram Reels.
    # No longer raised since Milestone 4.2.1 (oversized videos are normalized
    # automatically); kept for rows that failed with it before. Retry fixes them.
    "INSTAGRAM_MEDIA_RESOLUTION": "This video was too large for {platform} before automatic resizing existed. Retry to publish it.",
    "INSTAGRAM_MEDIA_PREPARATION_FAILED": "This video could not be prepared for {platform}.",
    "INSTAGRAM_MEDIA_PREPARATION_TIMEOUT": "Preparing this video for {platform} took too long.",
    "INSTAGRAM_MEDIA_PREPARATION_KILLED": "Preparing this video for {platform} ran out of server resources. Retry to try again.",
    "INSTAGRAM_MEDIA_PREPARATION_INTERRUPTED": "Preparing this video for {platform} was interrupted. It will be retried; if it keeps failing, retry it yourself.",
    "INSTAGRAM_MEDIA_DURATION": "{platform} Reels must be between 3 seconds and 15 minutes long.",
    "INSTAGRAM_MEDIA_FRAME_RATE": "{platform} Reels must be 23–60 frames per second.",
    "INSTAGRAM_MEDIA_TOO_LARGE": "{platform} Reels must be 300 MB or smaller.",
    "INSTAGRAM_CAPTION_TOO_MANY_HASHTAGS": "{platform} captions can have at most 30 hashtags.",
    "INSTAGRAM_CAPTION_TOO_MANY_MENTIONS": "{platform} captions can have at most 20 @-mentions.",
    "INSTAGRAM_CONTAINER_ERROR": "{platform} couldn't process this video. Check the file and retry.",
    "INSTAGRAM_CONTAINER_EXPIRED": "{platform} discarded the upload before it was published. Retry to send it again.",
    "PUBLISH_OUTCOME_UNKNOWN": "A publish request was sent to {platform} but couldn't be confirmed. Check {platform}, then retry if it isn't there.",
}


@dataclass(frozen=True)
class FailureDescription:
    category: str
    message: str
    action_hint: str | None


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform.title())


def categorize_failure(platform: str, failure_code: str | None) -> str:
    if failure_code is None:
        return "UNKNOWN_ERROR"
    if failure_code in _SHARED_CODE_CATEGORIES:
        return _SHARED_CODE_CATEGORIES[failure_code]
    return _PLATFORM_CODE_TABLES.get(platform, {}).get(failure_code, "UNKNOWN_ERROR")


def describe_failure(platform: str, failure_code: str | None) -> FailureDescription:
    category = categorize_failure(platform, failure_code)
    template, hint = _CATEGORY_COPY[category]
    template = _CODE_MESSAGE_OVERRIDES.get(failure_code or "", template)
    return FailureDescription(category=category, message=template.format(platform=platform_label(platform)), action_hint=hint)
