"""
media_requirements.py — Instagram Reels media and caption limits (Milestone
4.2; split into hard limits and normalizable mismatches in 4.2.1).

What it does:
  check_duration(seconds)     HARD limit, 3 s–15 min. Never "fixed": trimming
                              or padding would change the video's content,
                              so it fails with an actionable reason.
  check_caption(caption)      HARD: ≤2,200 characters, ≤30 hashtags,
                              ≤20 @-mentions.
  compatibility_problems(d)   The NORMALIZABLE mismatches between a probed
                              file (media/stream_probe.StreamDetails) and
                              Meta's Reels specification. An empty list means
                              the file can be sent to Instagram as-is;
                              otherwise publishing/instagram/normalization.py
                              makes a compliant derivative. Also the check
                              every derivative must pass before it's used.

  Limits are Meta's published Reels specification (IG User media reference,
  verified 2026-10-04 — ADR-0018): MOV/MP4, H.264 or HEVC, progressive 4:2:0,
  AAC ≤48 kHz mono/stereo, 23–60 fps, ≤25 Mbps video, ≤300 MB, at most 1920
  horizontal pixels. "Horizontal" is the displayed width, after the rotation
  flag phones write (a portrait iPhone clip is coded landscape). Only 8-bit
  4:2:0 counts as compatible: 10-bit/HDR sources are normalized rather than
  sent as-is.

Dependencies:
  media.stream_probe (StreamDetails).
"""

import re
from dataclasses import dataclass

from content_automation.media.stream_probe import StreamDetails

CONTAINERS = frozenset({"mov", "mp4"})
VIDEO_CODECS = frozenset({"h264", "hevc"})
PIXEL_FORMATS = frozenset({"yuv420p", "yuvj420p"})
AUDIO_CODECS = frozenset({"aac"})
MAX_AUDIO_SAMPLE_RATE = 48_000
MAX_AUDIO_CHANNELS = 2
MIN_FPS, MAX_FPS = 23.0, 60.0
MIN_DURATION_SECONDS, MAX_DURATION_SECONDS = 3.0, 15 * 60.0
MAX_FILE_SIZE_BYTES = 300 * 1024 * 1024
MAX_WIDTH_PIXELS = 1920
MAX_VIDEO_BITRATE = 25_000_000

CAPTION_MAX_CHARS = 2200
CAPTION_MAX_HASHTAGS = 30
CAPTION_MAX_MENTIONS = 20

_HASHTAG = re.compile(r"(?<![\w#])#\w+", re.UNICODE)
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9._]+")

# compatibility_problems() labels (internal, logged; not failure codes).
CONTAINER, VIDEO_CODEC, PIXEL_FORMAT, RESOLUTION, FRAME_RATE, BITRATE, AUDIO, FILE_SIZE = (
    "container", "video_codec", "pixel_format", "resolution", "frame_rate", "bitrate", "audio", "file_size",
)


@dataclass(frozen=True)
class RequirementProblem:
    reason_code: str
    message: str


def check_duration(seconds: float | None) -> RequirementProblem | None:
    if seconds is None or not MIN_DURATION_SECONDS <= seconds <= MAX_DURATION_SECONDS:
        length = "of unknown length" if seconds is None else f"{seconds:.1f} seconds"
        return RequirementProblem(
            "INSTAGRAM_MEDIA_DURATION", f"Instagram Reels must be 3 seconds to 15 minutes long (this video is {length}).",
        )
    return None


def compatibility_problems(details: StreamDetails) -> list[str]:
    problems = []
    if not details.format_names & CONTAINERS:
        problems.append(CONTAINER)
    if details.video_codec not in VIDEO_CODECS:
        problems.append(VIDEO_CODEC)
    if details.pixel_format not in PIXEL_FORMATS or details.is_hdr:
        problems.append(PIXEL_FORMAT)
    if details.display_width > MAX_WIDTH_PIXELS:
        problems.append(RESOLUTION)
    if details.fps is None or not MIN_FPS <= details.fps <= MAX_FPS:
        problems.append(FRAME_RATE)
    if details.video_bitrate is not None and details.video_bitrate > MAX_VIDEO_BITRATE:
        problems.append(BITRATE)
    if details.audio_codec is not None and (
        details.audio_codec not in AUDIO_CODECS
        or (details.audio_sample_rate or 0) > MAX_AUDIO_SAMPLE_RATE
        or (details.audio_channels or 0) > MAX_AUDIO_CHANNELS
    ):
        problems.append(AUDIO)
    if details.file_size_bytes > MAX_FILE_SIZE_BYTES:
        problems.append(FILE_SIZE)
    return problems


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
