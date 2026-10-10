"""
normalization.py — turns a video Instagram won't accept as-is into a
compliant Reels derivative (Milestone 4.2.1). The canonical upload is never
modified; the derivative is a disposable, recreatable artifact.

What it does:
  plan_normalization(details) → NormalizationPlan | None. None means the
  source already meets the Reels spec (media_requirements.compatibility_problems
  is empty) and must be published untouched. Otherwise the plan says how to
  re-encode, deterministically, from the probed source:

    Resolution  Displayed size (after the phone's rotation flag) scaled down
                proportionally so the longer side is ≤1920, both sides even. Never
                upscaled, never cropped, aspect ratio kept: iPhone portrait 4K
                2160x3840 → 1080x1920; landscape 3840x2160 → 1920x1080.
    Frame rate  Kept when 23–60 fps (30 stays 30, 60 stays 60). Above 60 →
                60 (e.g. 120/240 fps slow-motion files); below 23 → 30.
    Video       H.264 High, 8-bit 4:2:0, CRF 20 (preset veryfast), capped at
                10 Mbps — or lower when needed to keep a long video under
                300 MB (90% of the limit, minus audio). Closed GOP, every
                2 seconds. HDR (HLG/PQ) is tone-mapped to SDR BT.709 with
                zscale.
    Audio       Copied when already AAC ≤48 kHz mono/stereo; otherwise AAC
                128 kb/s 48 kHz stereo. A silent video stays silent.
    Container   MP4 with the moov atom first (faststart). Rotation is applied
                to the pixels, and source metadata (e.g. GPS location) isn't
                carried over.

  normalize(source, destination, plan, heartbeat=...) runs one ffmpeg pass
  and then requires the output to be fully compatible and the same length
  (±max(0.5 s, 1%)). heartbeat() is called about once a minute while ffmpeg
  runs, so a long encode keeps its claimed post from looking stale to crash
  recovery. Failures raise NormalizationError with a reason_code persisted as
  platform_posts.failure_code and a safe stage/exit-code detail (no paths
  beyond file names, no URLs).

  POLICY_VERSION is part of the derivative's storage key: change it whenever
  the output policy changes so existing derivatives are regenerated.

Dependencies:
  ffmpeg/ffprobe on PATH, media.stream_probe, publishing.instagram.media_requirements.
"""

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from content_automation.media.inspection import CorruptMediaError
from content_automation.media.stream_probe import StreamDetails, probe_streams
from content_automation.publishing.instagram import media_requirements as reqs

POLICY_VERSION = "v1"

TARGET_FPS_HIGH = 60
TARGET_FPS_LOW = 30
CRF = 20
PRESET = "veryfast"
MAX_VIDEO_KBPS = 10_000
MIN_VIDEO_KBPS = 1_000
AUDIO_KBPS = 128
SIZE_BUDGET_FRACTION = 0.9
HEARTBEAT_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 3600


class NormalizationError(Exception):
    """reason_code: INSTAGRAM_MEDIA_PREPARATION_FAILED or
    INSTAGRAM_MEDIA_PREPARATION_TIMEOUT. stage/exit_code: safe diagnostics."""

    def __init__(self, message: str, *, reason_code: str = "INSTAGRAM_MEDIA_PREPARATION_FAILED",
                 stage: str, exit_code: int | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.stage = stage
        self.exit_code = exit_code


@dataclass(frozen=True)
class NormalizationPlan:
    reasons: tuple[str, ...]
    width: int
    height: int
    fps: float | None  # None = keep the source rate
    max_video_kbps: int
    copy_audio: bool
    has_audio: bool
    tonemap_hdr: bool


def _even(value: float) -> int:
    rounded = int(round(value))
    return max(2, rounded - (rounded % 2))


def output_dimensions(display_width: int, display_height: int) -> tuple[int, int]:
    """Proportional downscale so the LONGER side is ≤1920 (portrait 4K →
    1080x1920, landscape 4K → 1920x1080), even sides; never upscales."""
    longest = max(display_width, display_height)
    if longest <= reqs.MAX_WIDTH_PIXELS:
        return _even(display_width), _even(display_height)
    scale = reqs.MAX_WIDTH_PIXELS / longest
    return _even(display_width * scale), _even(display_height * scale)


def target_fps(fps: float | None) -> float | None:
    if fps is None:
        return TARGET_FPS_LOW
    if fps > reqs.MAX_FPS:
        return TARGET_FPS_HIGH
    if fps < reqs.MIN_FPS:
        return TARGET_FPS_LOW
    return None


def video_bitrate_cap_kbps(duration_seconds: float | None) -> int:
    if not duration_seconds:
        return MAX_VIDEO_KBPS
    budget = reqs.MAX_FILE_SIZE_BYTES * SIZE_BUDGET_FRACTION * 8 / duration_seconds / 1000 - AUDIO_KBPS
    return int(max(MIN_VIDEO_KBPS, min(MAX_VIDEO_KBPS, budget)))


def plan_normalization(details: StreamDetails) -> NormalizationPlan | None:
    reasons = reqs.compatibility_problems(details)
    if not reasons:
        return None
    width, height = output_dimensions(details.display_width, details.display_height)
    audio_ok = (
        details.audio_codec in reqs.AUDIO_CODECS
        and (details.audio_sample_rate or 0) <= reqs.MAX_AUDIO_SAMPLE_RATE
        and (details.audio_channels or 0) <= reqs.MAX_AUDIO_CHANNELS
    )
    return NormalizationPlan(
        reasons=tuple(reasons), width=width, height=height, fps=target_fps(details.fps),
        max_video_kbps=video_bitrate_cap_kbps(details.duration_seconds),
        copy_audio=audio_ok, has_audio=details.audio_codec is not None, tonemap_hdr=details.is_hdr,
    )


def build_ffmpeg_command(source: Path, destination: Path, plan: NormalizationPlan) -> list[str]:
    filters = [f"scale={plan.width}:{plan.height}"]
    if plan.tonemap_hdr:
        filters += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                    "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
    if plan.fps is not None:
        filters.append(f"fps={plan.fps:g}")
    filters.append("format=yuv420p")
    gop = int(round((plan.fps or 30) * 2))
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF), "-profile:v", "high",
        "-maxrate", f"{plan.max_video_kbps}k", "-bufsize", f"{plan.max_video_kbps * 2}k",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
    ]
    if plan.has_audio:
        command += ["-c:a", "copy"] if plan.copy_audio else ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", "48000", "-ac", "2"]
    command += ["-movflags", "+faststart", "-f", "mp4", str(destination)]
    return command


def _run(command: list[str], *, timeout_seconds: int, heartbeat: Callable[[], None] | None) -> tuple[int, str]:
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    started = last_beat = time.monotonic()
    while True:
        try:
            _, stderr = process.communicate(timeout=5)
            return process.returncode, stderr or ""
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if now - started > timeout_seconds:
                process.kill()
                process.communicate()
                raise NormalizationError(
                    f"Preparing the video for Instagram took longer than {timeout_seconds} s.",
                    reason_code="INSTAGRAM_MEDIA_PREPARATION_TIMEOUT", stage="encode",
                ) from None
            if heartbeat is not None and now - last_beat >= HEARTBEAT_SECONDS:
                heartbeat()
                last_beat = now


def normalize(source: Path, destination: Path, plan: NormalizationPlan, *, source_details: StreamDetails,
              heartbeat: Callable[[], None] | None = None, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> StreamDetails:
    """Encode `source` into `destination` per `plan` and return the
    verified output's details. Raises NormalizationError."""
    exit_code, stderr = _run(build_ffmpeg_command(source, destination, plan), timeout_seconds=timeout_seconds, heartbeat=heartbeat)
    if exit_code != 0 or not destination.exists():
        # ffmpeg's last stderr line names the failing component; it carries the
        # local temp file names at most, never URLs or credentials.
        last_line = stderr.strip().splitlines()[-1][:200] if stderr.strip() else "no output"
        raise NormalizationError(f"ffmpeg failed (exit {exit_code}): {last_line}", stage="encode", exit_code=exit_code)
    try:
        output = probe_streams(destination)
    except CorruptMediaError as exc:
        raise NormalizationError(f"The prepared file couldn't be read: {exc}", stage="verify") from None
    remaining = reqs.compatibility_problems(output)
    if remaining:
        raise NormalizationError(f"The prepared file still doesn't meet Instagram's spec: {', '.join(remaining)}", stage="verify")
    expected, actual = source_details.duration_seconds, output.duration_seconds
    if expected and actual is not None and abs(actual - expected) > max(0.5, expected * 0.01):
        raise NormalizationError(f"The prepared file's length changed ({expected:.2f}s → {actual:.2f}s).", stage="verify")
    return output
