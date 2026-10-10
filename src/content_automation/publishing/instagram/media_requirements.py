"""
media_requirements.py — Instagram Reels media and caption limits, checked
before anything is sent to Meta (Milestone 4.2).

What it does:
  check_reel_media(info) and check_caption(caption) return None when
  acceptable, else a RequirementProblem with a platform-specific reason_code
  (persisted as platform_posts.failure_code, explained by
  publishing/failure_taxonomy.py) and an actionable message. Both are
  terminal: nothing about the file or caption changes by retrying. Nothing is
  transcoded (out of scope for 4.2): an unsupported file fails clearly.

  Limits are Meta's published Reels specification (IG User media reference,
  verified 2026-10-04 — ADR-0018 Decision 5) for the fields ffprobe already
  gives us (media/inspection.py MediaInfo): container MOV/MP4, video
  H.264/HEVC, audio AAC when present, 23–60 fps, 3 s–15 min, ≤300 MB, at most
  1920 px horizontally. Bitrate, GOP and moov-atom placement aren't probed;
  Meta reports those through the container's ERROR status instead.
  Captions: ≤2,200 characters, ≤30 hashtags, ≤20 @-mentions.

Dependencies:
  media.inspection (MediaInfo).
"""

import re
from dataclasses import dataclass

from content_automation.media.inspection import MediaInfo

CONTAINERS = frozenset({"mov", "mp4"})
VIDEO_CODECS = frozenset({"h264", "hevc"})
AUDIO_CODECS = frozenset({"aac"})
MIN_FPS, MAX_FPS = 23.0, 60.0
MIN_DURATION_SECONDS, MAX_DURATION_SECONDS = 3.0, 15 * 60.0
MAX_FILE_SIZE_BYTES = 300 * 1024 * 1024
MAX_WIDTH_PIXELS = 1920

CAPTION_MAX_CHARS = 2200
CAPTION_MAX_HASHTAGS = 30
CAPTION_MAX_MENTIONS = 20

_HASHTAG = re.compile(r"(?<![\w#])#\w+", re.UNICODE)
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9._]+")


@dataclass(frozen=True)
class RequirementProblem:
    reason_code: str
    message: str


def _container_key(info: MediaInfo) -> str:
    # ffprobe reports MOV and MP4 as one format family ("mov,mp4,m4a,3gp,…").
    names = {name.strip() for name in info.container.lower().split(",")}
    if "mp4" in names or info.path.suffix.lower() == ".mp4":
        return "mp4"
    if "mov" in names or info.path.suffix.lower() == ".mov":
        return "mov"
    return info.container.lower()


def check_reel_media(info: MediaInfo) -> RequirementProblem | None:
    if _container_key(info) not in CONTAINERS:
        return RequirementProblem("INSTAGRAM_MEDIA_UNSUPPORTED_FORMAT", f"Instagram Reels must be MOV or MP4 (this file is {info.container}).")
    if info.video_codec not in VIDEO_CODECS:
        return RequirementProblem("INSTAGRAM_MEDIA_UNSUPPORTED_CODEC", f"Instagram Reels must be H.264 or HEVC video (this file is {info.video_codec or 'not a video'}).")
    if info.audio_codec is not None and info.audio_codec not in AUDIO_CODECS:
        return RequirementProblem("INSTAGRAM_MEDIA_UNSUPPORTED_CODEC", f"Instagram Reels audio must be AAC (this file is {info.audio_codec}).")
    if info.duration_seconds is None or not MIN_DURATION_SECONDS <= info.duration_seconds <= MAX_DURATION_SECONDS:
        return RequirementProblem("INSTAGRAM_MEDIA_DURATION", f"Instagram Reels must be 3 seconds to 15 minutes long (this video is {_seconds(info.duration_seconds)}).")
    if info.fps is None or not MIN_FPS <= info.fps <= MAX_FPS:
        return RequirementProblem("INSTAGRAM_MEDIA_FRAME_RATE", f"Instagram Reels must be 23–60 frames per second (this video is {info.fps or 'unknown'} fps).")
    if info.file_size_bytes > MAX_FILE_SIZE_BYTES:
        return RequirementProblem("INSTAGRAM_MEDIA_TOO_LARGE", f"Instagram Reels must be 300 MB or smaller (this file is {info.file_size_bytes // (1024 * 1024)} MB).")
    if info.width is None or info.width > MAX_WIDTH_PIXELS:
        return RequirementProblem(
            "INSTAGRAM_MEDIA_RESOLUTION",
            f"Instagram Reels can be at most 1920 pixels wide (this video is {info.width or 'unknown'} pixels). Export it at 1080p and upload it again.",
        )
    return None


def check_caption(caption: str | None) -> RequirementProblem | None:
    if not caption:
        return None
    if len(caption) > CAPTION_MAX_CHARS:
        return RequirementProblem("CAPTION_TOO_LONG", f"Instagram captions can be at most {CAPTION_MAX_CHARS} characters.")
    if len(_HASHTAG.findall(caption)) > CAPTION_MAX_HASHTAGS:
        return RequirementProblem("INSTAGRAM_CAPTION_TOO_MANY_HASHTAGS", f"Instagram captions can have at most {CAPTION_MAX_HASHTAGS} hashtags.")
    if len(_MENTION.findall(caption)) > CAPTION_MAX_MENTIONS:
        return RequirementProblem("INSTAGRAM_CAPTION_TOO_MANY_MENTIONS", f"Instagram captions can have at most {CAPTION_MAX_MENTIONS} @-mentions.")
    return None


def _seconds(value: float | None) -> str:
    return "of unknown length" if value is None else f"{value:.1f} seconds"
