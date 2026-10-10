"""
media_preparation.py — decides which stored object Instagram fetches for a
video: the canonical upload when it already meets the Reels spec, otherwise
a normalized derivative (Milestone 4.2.1). Runs in the worker after the post
is claimed and before the signed URL is issued.

What it does:
  prepare_publishable_media(store, storage, video, heartbeat=...) →
  PreparedMedia(storage_key, derived, ...):

    1. Reuse. With the source's content hash known (videos.file_hash, set at
       upload), the derivative's key — derived/instagram/reel-<policy>-<hash>.mp4
       — is computed without downloading anything; if it exists, it was
       verified before it was stored, so it is reused.
    2. Otherwise download the source once and probe it. Hard limits
       (duration) fail here. A compatible source is published as-is.
    3. Otherwise normalize (publishing/instagram/normalization.py), verify
       the output, and store it at the derivative key.

  Persistence is the storage key itself: no database column, no job table.
  The key encodes the normalization policy version and the source's
  SHA-256, so a changed source or policy maps to a new key (stale
  derivatives are simply never looked up again), and the private bucket
  answers "does a valid derivative exist?". Only one worker can hold a claimed
  post at a time (ContentStore.claim_platform_post), and the upload is an upsert of a
  verified file to a deterministic key, so concurrent attempts can't leave a
  conflicting derivative.

  Errors: PreparationError (terminal — persisted failure_code, user-facing
  copy in publishing/failure_taxonomy.py) for hard limits, unreadable media
  and normalization failures. Storage errors propagate unchanged for the
  caller's existing retry classification.

  Telemetry (one event per decision, safe fields only): source/output
  resolution, codec, fps, size, duration, reasons, encode seconds.

Dependencies:
  media.media_storage, media.stream_probe, media.inspection,
  publishing.instagram.{normalization, media_requirements}, scheduling.telemetry.
"""

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from content_automation.media import inspection
from content_automation.media.inspection import MediaError
from content_automation.media.media_storage import (
    build_derived_storage_key,
    derived_media_exists,
    materialize_canonical_media,
    store_derived_media,
)
from content_automation.media.stream_probe import StreamDetails, probe_streams
from content_automation.persistence.content_store import VideoRecord
from content_automation.persistence.protocol import ContentStoreProtocol
from content_automation.publishing.instagram import media_requirements as reqs
from content_automation.publishing.instagram import normalization
from content_automation.publishing.platforms import INSTAGRAM
from content_automation.scheduling.telemetry import log_event
from content_automation.storage.protocol import StorageProtocol


class PreparationError(Exception):
    def __init__(self, message: str, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class PreparedMedia:
    storage_key: str
    derived: bool
    reused: bool
    source_details: StreamDetails | None = None


def derivative_key(video: VideoRecord, source_hash: str) -> str:
    name = f"reel-{normalization.POLICY_VERSION}-{source_hash[:32]}.mp4"
    return build_derived_storage_key(video.user_id, video.id, INSTAGRAM, name)


def _summary(details: StreamDetails, prefix: str) -> dict:
    return {
        f"{prefix}_resolution": f"{details.display_width}x{details.display_height}",
        f"{prefix}_codec": details.video_codec,
        f"{prefix}_fps": details.fps,
        f"{prefix}_bytes": details.file_size_bytes,
        f"{prefix}_seconds": round(details.duration_seconds or 0, 2),
    }


def prepare_publishable_media(
    store: ContentStoreProtocol, storage: StorageProtocol, video: VideoRecord, *,
    heartbeat: Callable[[], None] | None = None, timeout_seconds: int = normalization.DEFAULT_TIMEOUT_SECONDS,
    on_probed: Callable[[StreamDetails], None] | None = None,
) -> PreparedMedia:
    ids = {"video_id": video.id, "user_id": video.user_id, "platform": INSTAGRAM}

    if video.file_hash:
        key = derivative_key(video, video.file_hash)
        if derived_media_exists(store, storage, video.id, video.user_id, key):
            log_event("instagram_media_reused", **ids, policy=normalization.POLICY_VERSION)
            return PreparedMedia(storage_key=key, derived=True, reused=True)

    with materialize_canonical_media(store, storage, video.id, video.user_id) as source_path:
        try:
            details = probe_streams(source_path)
        except MediaError as exc:
            log_event("instagram_media_unreadable", **ids, failure_code=exc.reason_code)
            raise PreparationError("This video file couldn't be read, so it can't be prepared for Instagram.",
                                   reason_code=exc.reason_code) from None
        if on_probed is not None:
            on_probed(details)

        problem = reqs.check_duration(details.duration_seconds)
        if problem is not None:
            raise PreparationError(problem.message, reason_code=problem.reason_code)

        plan = normalization.plan_normalization(details)
        if plan is None:
            log_event("instagram_media_original", **ids, **_summary(details, "source"))
            return PreparedMedia(storage_key=video.storage_key, derived=False, reused=False, source_details=details)

        key = derivative_key(video, video.file_hash or inspection.file_hash(source_path))
        log_event("instagram_media_normalizing", **ids, reasons="+".join(plan.reasons), target=f"{plan.width}x{plan.height}",
                  target_fps=plan.fps or details.fps, **_summary(details, "source"))
        with tempfile.TemporaryDirectory(prefix="instagram-reel-") as workdir:
            output_path = Path(workdir) / "reel.mp4"
            started = time.monotonic()
            try:
                output = normalization.normalize(source_path, output_path, plan, source_details=details,
                                                 heartbeat=heartbeat, timeout_seconds=timeout_seconds)
            except normalization.NormalizationError as exc:
                log_event("instagram_media_normalization_failed", **ids, stage=exc.stage, exit_code=exc.exit_code,
                          failure_code=exc.reason_code, reasons="+".join(plan.reasons), **_summary(details, "source"))
                message = (
                    "Preparing this video for Instagram took too long." if exc.reason_code.endswith("TIMEOUT")
                    else "This video could not be prepared for Instagram."
                )
                raise PreparationError(message, reason_code=exc.reason_code) from None
            encode_seconds = round(time.monotonic() - started, 2)
            store_derived_media(store, storage, video.id, video.user_id, key, output_path)
        log_event("instagram_media_normalized", **ids, encode_seconds=encode_seconds, reasons="+".join(plan.reasons),
                  **_summary(details, "source"), **_summary(output, "output"))
        return PreparedMedia(storage_key=key, derived=True, reused=False, source_details=details)
