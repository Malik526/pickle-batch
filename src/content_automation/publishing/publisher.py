"""
publisher.py — Publisher interface for pushing a local video to an external
platform, and PublishError/result types shared by every implementation.

What it does:
  Defines a platform-neutral interface (Publisher) so publish_tiktok.py (and
  any future per-platform CLI) never depends on a specific platform's API
  shape. Milestone 2.0 ships exactly one implementation, TikTokPublisher
  (tiktok_publisher.py) — see
  docs/decisions/0006-tiktok-publisher-foundation.md. Mirrors the existing
  Transcriber (transcription.py) / ContentClassifier (classification.py)
  interface-plus-factory pattern already used in this codebase.

  No I/O of its own — this module only defines contracts. Every
  platform-specific request format, endpoint, token, and response-parsing
  detail lives inside that platform's own adapter module, never here and
  never in process_content.py/publish_tiktok.py.

Dependencies:
  stdlib only.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class PublishError(Exception):
    """Raised when a publish or status-check call fails for any reason:
    missing local file, missing/expired credentials, a malformed platform
    response, an HTTP-level failure, or the platform reporting its own
    error. `reason_code` is a short machine-stable label (e.g.
    "LOCAL_FILE_MISSING", "MALFORMED_RESPONSE", "UPLOAD_FAILED", or a
    platform-reported error code) so callers can persist/report it without
    parsing the message string.

    `http_status` (Milestone 2.1.6 — retry classification) is the real
    numeric HTTP status code, when one was actually available at the raise
    site (a transport-level failure with no response at all leaves it
    None). Structured, not parsed from the message string, so
    retry_classification.py can tell a transient 5xx from a permanent 4xx
    without any text-matching heuristics."""

    def __init__(self, message: str, reason_code: str = "PUBLISH_FAILED", http_status: int | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.http_status = http_status


@dataclass
class PublishResult:
    """Returned by Publisher.publish() once a video has been submitted.
    `status` is the platform's own reported state immediately after
    submission (e.g. TikTok's "PROCESSING_UPLOAD") — publishing is
    asynchronous, so this is rarely a terminal state; call get_status()
    later to find out what actually happened."""
    platform_post_id: str
    status: str
    raw_response: dict | None = None


# Milestone 4.2: the shared status vocabulary publish_tiktok._resolve_poll_outcome
# understands. TikTokPublisher already reports PUBLISH_COMPLETE / FAILED
# natively; a platform with its own vocabulary (Instagram) normalizes to
# these. Anything else means "still processing".
STATUS_PUBLISH_COMPLETE = "PUBLISH_COMPLETE"
STATUS_FAILED = "FAILED"
# Processing finished but nothing is posted until the client takes the
# platform's second step (Publisher.finalize — Instagram's media_publish).
STATUS_READY_TO_FINALIZE = "READY_TO_FINALIZE"


@dataclass
class PublishStatusResult:
    """Returned by Publisher.get_status(). `status` is the platform's own
    vocabulary (not normalized to this project's platform_posts.status
    values) — callers map it to PENDING/PUBLISHING/PUBLISHED/FAILED
    themselves, since that mapping is platform-specific.

    Milestone 4.2 (optional, default None, so TikTok is unchanged):
    failure_code is a machine label for a platform-reported failure when it
    differs from failure_reason; platform_media_id is the published post's
    id when the status check itself reveals it."""
    status: str
    failure_reason: str | None = None
    raw_response: dict | None = None
    failure_code: str | None = None
    platform_media_id: str | None = None


@dataclass
class FinalizeResult:
    """Returned by Publisher.finalize(): the published post's platform id
    (Instagram: the media id from media_publish)."""
    platform_media_id: str


class Publisher(ABC):
    # Milestone 3.13 (reconciliation + recovery) — the submission checkpoint
    # contract. A publisher that sets this True accepts an
    # on_platform_post_id callback in publish() and guarantees to call it
    # with the platform's id BEFORE transferring any media (the step that
    # can make a post exist), and to transfer nothing if the callback
    # raises. The caller persists the id there, so "no id persisted" then
    # provably means "no media sent" — see scheduling/publish_tiktok.py
    # "Submission checkpoint". Publishers that leave it False are called
    # with (video_path, caption) only, and a crash mid-submission with them
    # is recorded as an unknown outcome rather than retried.
    reports_platform_post_id_before_media_transfer: bool = False

    @abstractmethod
    def publish(
        self, video_path: Path, caption: str | None, on_platform_post_id: Callable[[str], None] | None = None,
    ) -> PublishResult:
        """Upload video_path and submit it for publishing with caption.
        caption is optional (Milestone 3.14 follow-up): None means publish
        without one, which every implementation must support unless its
        platform genuinely requires a caption.
        Raises PublishError on any failure — missing file, auth, upload,
        or a malformed/error platform response. Must not silently retry an
        ambiguous result as a brand-new submission; that policy lives in
        the caller (publish_tiktok.py), which owns idempotency via
        content_store.get_platform_post(). on_platform_post_id: see
        reports_platform_post_id_before_media_transfer above."""

    # Milestone 4.2: a publisher that sets this True publishes in two
    # client-driven steps. publish() only creates a submission (Instagram:
    # a media container, which never posts by itself); get_status() reports
    # STATUS_READY_TO_FINALIZE once the platform has processed it; and
    # finalize() is the step that creates the post. The caller persists a
    # PUBLISH_REQUESTED checkpoint before finalize() — see
    # scheduling/finalization.py.
    requires_finalize: bool = False

    # Milestone 4.2: a pull-URL publisher (publishing/platforms.py
    # media_delivery == "pull_url") receives `media_url=` in publish(): a
    # short-lived signed URL the platform fetches the video from, issued by
    # the caller immediately before the call. video_path is then unused. A
    # publisher must never log, store or return that URL.

    @abstractmethod
    def get_status(self, platform_post_id: str) -> PublishStatusResult:
        """Check a previously submitted post's current status. Raises
        PublishError on a malformed response or platform-reported error —
        never returns a fabricated status just to avoid raising."""


    def finalize(self, platform_post_id: str) -> FinalizeResult:
        """Take the step that creates the post for a submission that
        get_status() reported STATUS_READY_TO_FINALIZE. Only called when
        requires_finalize is True. Raises PublishError; an error carrying
        an HTTP 4xx status means the platform answered and rejected the
        request (nothing was posted), anything else is ambiguous."""
        raise NotImplementedError(f"{type(self).__name__} has no finalize step")


class UnsupportedPlatformError(Exception):
    """Raised by build_publisher() for a platform with no registered implementation."""


def build_publisher(platform: str, **kwargs) -> Publisher:
    """Construct the Publisher for `platform`. Only "tiktok" exists as of
    Milestone 2.0 — deliberately not pre-building an Instagram/YouTube stub,
    per that milestone's explicit scope. Raises UnsupportedPlatformError
    immediately and clearly on anything else, mirroring
    classification.build_classifier()'s unsupported-value message style."""
    if platform == "tiktok":
        from content_automation.publishing.tiktok.publisher import TikTokPublisher
        return TikTokPublisher(**kwargs)
    raise UnsupportedPlatformError(f"Unsupported platform={platform!r}. Valid values: tiktok")
