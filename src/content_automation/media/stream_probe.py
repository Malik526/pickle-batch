"""
stream_probe.py — the detailed ffprobe reading platform media
normalization needs (Milestone 4.2.1), beyond media/inspection.py's
MediaInfo.

What it does:
  probe_streams(path) → StreamDetails: container family, video codec, pixel
  format, coded and DISPLAY dimensions (phones record portrait video as
  landscape pixels plus a rotation flag — an iPhone 4K portrait clip is
  coded 3840x2160 with a 90° rotation and displays 2160x3840), frame rate,
  video bitrate, audio codec/sample rate/channels, colour transfer (HDR
  detection), duration and size. Raises inspection.CorruptMediaError when
  ffprobe can't read the file or it has no video stream.

  Read-only; never changes the file. Kept separate from inspection.py so the
  persisted MediaInfo fields and local ingestion stay exactly as they are.

Dependencies:
  ffprobe on PATH, media.inspection (errors, frame-rate parsing).
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from content_automation.media.inspection import CorruptMediaError, _parse_frame_rate, _safe_float

_HDR_TRANSFERS = frozenset({"arib-std-b67", "smpte2084"})  # HLG, PQ


@dataclass(frozen=True)
class StreamDetails:
    path: Path
    container: str  # first ffprobe format name, as media.inspection stores it
    format_names: frozenset[str]
    video_codec: str
    pixel_format: str | None
    coded_width: int
    coded_height: int
    rotation: int  # degrees, normalized to 0/90/180/270
    fps: float | None
    video_bitrate: int | None  # bits/s, when ffprobe reports it
    audio_codec: str | None
    audio_sample_rate: int | None
    audio_channels: int | None
    color_transfer: str | None
    duration_seconds: float | None
    file_size_bytes: int

    @property
    def display_width(self) -> int:
        return self.coded_height if self.rotation in (90, 270) else self.coded_width

    @property
    def display_height(self) -> int:
        return self.coded_width if self.rotation in (90, 270) else self.coded_height

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in _HDR_TRANSFERS


def _rotation(stream: dict) -> int:
    """Rotation from the display-matrix side data (ffmpeg ≥5) or the legacy
    `rotate` tag (ffmpeg 4.x), as 0/90/180/270."""
    raw = None
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            raw = side_data["rotation"]
            break
    if raw is None:
        raw = (stream.get("tags") or {}).get("rotate")
    try:
        degrees = int(round(float(raw))) if raw is not None else 0
    except (TypeError, ValueError):
        degrees = 0
    return degrees % 360 if degrees % 90 == 0 else 0


def _int(raw) -> int | None:
    try:
        return int(raw) if raw not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def probe_streams(path: Path) -> StreamDetails:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise CorruptMediaError(f"ffprobe timed out inspecting {path.name}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise CorruptMediaError(f"ffprobe could not read {path.name}")
    try:
        probe = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CorruptMediaError(f"ffprobe returned invalid JSON for {path.name}") from exc

    fmt = probe.get("format") or {}
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None or not video.get("codec_name") or not video.get("width") or not video.get("height"):
        raise CorruptMediaError(f"{path.name} has no readable video stream")

    fps = _parse_frame_rate(video.get("r_frame_rate"))
    if not fps or fps > 1000:  # some containers report a timebase, not a rate
        fps = _parse_frame_rate(video.get("avg_frame_rate"))

    names = [name.strip() for name in (fmt.get("format_name") or "").split(",") if name.strip()]
    return StreamDetails(
        path=path,
        container=names[0] if names else path.suffix.lstrip(".").lower(),
        format_names=frozenset(names),
        video_codec=video["codec_name"],
        pixel_format=video.get("pix_fmt"),
        coded_width=int(video["width"]),
        coded_height=int(video["height"]),
        rotation=_rotation(video),
        fps=fps,
        video_bitrate=_int(video.get("bit_rate")),
        audio_codec=audio.get("codec_name") if audio else None,
        audio_sample_rate=_int(audio.get("sample_rate")) if audio else None,
        audio_channels=_int(audio.get("channels")) if audio else None,
        color_transfer=video.get("color_transfer"),
        duration_seconds=_safe_float(fmt.get("duration")),
        file_size_bytes=int(fmt.get("size") or path.stat().st_size),
    )
