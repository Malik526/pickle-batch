"""
platforms.py — the publishing platforms Pickle Batch knows about, and the
few capabilities where they genuinely differ (Milestone 4.0).

What it does:
  One registry (PLATFORMS) of platform ids, display labels and
  capabilities. Fields exist only where TikTok and Instagram actually
  differ, which code must branch on:

    media_delivery          "push_file": we upload bytes (TikTok FILE_UPLOAD).
                            "pull_url":  the platform fetches the video from a
                            URL we give it (Instagram video_url), so private
                            storage must hand out a short-lived signed URL.
    requires_finalize_step  Instagram publishes in two client-driven steps
                            (create container → media_publish); TikTok
                            publishes on submission. The worker must drive
                            the second step for Instagram (Milestone 4.2).
    caption_max_chars       Platform caption limit (TikTok post title 2200,
                            Instagram caption 2200).
    connection_available    The hosted connect flow exists for this platform.
    publishing_available    A hosted Publisher exists, so platform_posts rows
                            for it can actually be published.

  Instagram's connection_available became True in Milestone 4.1 (the OAuth
  connect flow exists; the status endpoint still reports connect_available
  False unless the server is configured for it) and publishing_available in
  Milestone 4.2 (Reels publishing through publishing/instagram/publisher.py).
  See docs/decisions/0018-instagram-integration-architecture.md.

Dependencies:
  stdlib only.
"""

from dataclasses import dataclass

TIKTOK = "tiktok"
INSTAGRAM = "instagram"

PUSH_FILE = "push_file"
PULL_URL = "pull_url"


@dataclass(frozen=True)
class PlatformCapabilities:
    id: str
    label: str
    media_delivery: str
    requires_finalize_step: bool
    caption_max_chars: int
    connection_available: bool
    publishing_available: bool


PLATFORMS: dict[str, PlatformCapabilities] = {
    TIKTOK: PlatformCapabilities(
        id=TIKTOK, label="TikTok", media_delivery=PUSH_FILE, requires_finalize_step=False,
        caption_max_chars=2200, connection_available=True, publishing_available=True,
    ),
    INSTAGRAM: PlatformCapabilities(
        id=INSTAGRAM, label="Instagram", media_delivery=PULL_URL, requires_finalize_step=True,
        caption_max_chars=2200, connection_available=True, publishing_available=True,
    ),
}


class UnknownPlatformError(ValueError):
    pass


def get_platform(platform_id: str) -> PlatformCapabilities:
    try:
        return PLATFORMS[platform_id]
    except KeyError:
        raise UnknownPlatformError(f"Unknown publishing platform: {platform_id!r}") from None


def publishable(platform_ids: list[str]) -> list[str]:
    """The subset of platform_ids that can actually be published today,
    in order. Unknown ids and platforms without a hosted publisher are
    dropped, so a misconfigured target list can't create platform_posts
    rows that nothing will ever publish."""
    return [pid for pid in platform_ids if pid in PLATFORMS and PLATFORMS[pid].publishing_available]
