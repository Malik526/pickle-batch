"""
media_storage.py — the domain-level bridge between a video's DB row and the
object-storage backend that may hold its canonical media (Milestone 3.4).

What it does:
  Three operations (a fourth, delete_video, added Milestone 3.7
  follow-up — see its own docstring below):

    upload_canonical_media(store, storage, video_id, user_id)
      Uploads the video's current canonical_media_path (a real local file
      from the existing ingestion pipeline — media/processing.py is
      completely unchanged by this milestone) to `storage`, and stamps
      storage_provider/storage_key on the video row. Additive: the
      existing local file is never deleted, and canonical_media_path is
      never cleared — see docs/decisions/0009-object-storage-media-lifecycle.md
      "Retention" for why keeping the local source indefinitely was the
      deliberate V1 choice, not an oversight.

    materialize_canonical_media(store, storage, video_id, user_id)
      Context manager yielding a real local Path to the video's canonical
      media, regardless of where it actually lives: if the video has never
      been uploaded to object storage (storage_provider is NULL — every
      video that existed before this milestone, and any new one that
      hasn't been migrated), yields canonical_media_path directly with no
      network call, exactly matching pre-3.4 behavior. If it has,
      materializes through `storage` instead. Callers (scheduling/
      publish_tiktok.py) never need to know which case applies.

  Both functions verify the video belongs to `user_id` before touching
  anything (Phase 19 — tenant isolation: a caller must never be able to
  resolve/upload/materialize another user's media even with a known
  video_id/storage key) — raises MediaOwnershipError otherwise, the same
  "check before act" shape ContentStore.assign_slot's OwnershipMismatchError
  established in Milestone 3.2.

  Storage key format: users/<user_id>/videos/<video_id>/source<ext> —
  deterministic (not a random UUID), so re-running upload_canonical_media
  for the same video is naturally idempotent at the storage layer (the
  same key is overwritten, not duplicated — see StorageProtocol.put's
  upsert contract) — see Phase 28 of the Milestone 3.4 brief ("document
  the current protection... do not create an expensive locking subsystem
  unless evidence requires one").
  Milestone 4.2.1: platform derivatives live beside the source at
  users/<user_id>/videos/<video_id>/derived/<platform>/<name>
  (build_derived_storage_key). They stay private like the source and are
  never written over it.

Future media intelligence (Milestone 3.7 follow-up — deliberately not
built here; see this module's own guardrail against synchronous probing
in the upload path). Milestone 3.13 update: the hosted worker now writes
these columns at first publish (scheduling/publish_tiktok.py
_persist_media_metadata), so a video is inspected at most once on the
publish path. The upload path still never probes, and an upload-time
enrichment job as described below remains unbuilt:
  A video created by create_video_from_upload has every media-metadata
  column NULL — container, video_codec, audio_codec, width, height, fps,
  duration_seconds — exactly like a freshly-`insert_video`'d row from the
  local pipeline before media.inspection.inspect_media ever runs on it.
  Nothing about this milestone changes what those columns mean or how
  they'd be populated: media.inspection.inspect_media(path) already
  computes every one of them via a single ffprobe call, and already
  accepts a plain local Path — media.media_storage.materialize_canonical_media
  (or, for a storage-backed video specifically, storage.materialize(key)
  directly) already resolves exactly that kind of path for a video
  regardless of where its bytes actually live. The natural, no-new-
  abstraction way to enrich a hosted upload's row is therefore: a
  background job (matching docs/architecture/hosted-product-boundary.md
  §5's existing job-boundary shape — a plain function over "videos with
  storage_provider set and container IS NULL", invoked on an interval,
  not a request) that materializes each one temporarily and calls
  inspect_media, then update_video()s the result. This is deliberately
  NOT wired up by this milestone: hosted-product-boundary.md §4 lists
  ffprobe as explicitly *not* FastAPI-request-appropriate (subprocess,
  unbounded-ish duration), and this milestone's own scope guardrail
  ("upload success must remain independent of transcription, scheduling,
  or publishing") means the upload endpoint must keep working exactly as
  it does today even after that job exists — it should never become a
  precondition for a successful upload. transcript/classification/
  caption/scheduling fields are a separate, later concern (real
  transcription/classification, not just file metadata) — out of scope
  for even this future job, let alone this milestone.

Dependencies:
  content_automation.persistence.content_store (ContentStore, VideoRecord),
  content_automation.persistence.protocol (ContentStoreProtocol — used only
  by create_video_from_upload, which must work against either backend
  since it is called from the hosted API; the pre-existing functions above
  keep their original ContentStore-only type hint, unchanged),
  content_automation.storage.protocol (StorageProtocol).
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from content_automation.persistence.content_store import ContentStore, VideoRecord
from content_automation.persistence.protocol import ContentStoreProtocol
from content_automation.storage.protocol import StorageProtocol


class MediaOwnershipError(Exception):
    """Raised when video_id does not belong to user_id — see module docstring."""


class MediaNotUploadedError(Exception):
    """Raised by upload_canonical_media if the video has no local
    canonical_media_path to upload yet (still mid-ingestion), and by any
    caller that needs storage-backed media for a video that was never
    migrated and has no legacy local file either."""


class VideoHasScheduleReferencesError(Exception):
    """Raised by delete_video when video_id is still assigned to a
    content_slot (assigned_slot_id) or has at least one platform_posts row
    (any platform, any status — pending, published, or failed). Milestone
    3.7 follow-up's own instruction: fail safely rather than cascading the
    delete into scheduling/publishing state, and tell the caller to
    remove/cancel those entries first. Never reveals which slot/post
    specifically — the caller already owns this video, so no cross-tenant
    information is at stake here, but the message stays generic anyway to
    match this module's existing error-message convention."""

    reason_code = "HAS_SCHEDULE_REFERENCES"


def build_storage_key(user_id: int, video_id: int, suffix: str) -> str:
    """users/<user_id>/videos/<video_id>/source<ext> — tenant-scoped,
    deterministic, unique per video, no user-provided filename or secret
    information embedded. See module docstring "Storage key format"."""
    return f"users/{user_id}/videos/{video_id}/source{suffix}"


def build_derived_storage_key(user_id: int, video_id: int, platform: str, name: str) -> str:
    """users/<user_id>/videos/<video_id>/derived/<platform>/<name> — a
    platform-specific derivative of the canonical source (Milestone 4.2.1),
    next to it in the same private bucket. `name` encodes everything the
    derivative depends on (policy version + source hash), so a reusable
    derivative is found by key alone and a changed source maps to a new key."""
    return f"users/{user_id}/videos/{video_id}/derived/{platform}/{name}"


def derived_media_exists(store: ContentStoreProtocol, storage: StorageProtocol, video_id: int, user_id: int, key: str) -> bool:
    """Whether an owned video's derivative is already stored at `key`."""
    _require_own_derived_key(store, video_id, user_id, key)
    return storage.exists(key)


def store_derived_media(
    store: ContentStoreProtocol, storage: StorageProtocol, video_id: int, user_id: int, key: str, local_path: Path,
) -> None:
    """Upload a verified derivative for an owned video. The canonical source
    is never touched; the upload is an upsert of the same key, so two
    attempts racing on one video converge on an equivalent verified file."""
    _require_own_derived_key(store, video_id, user_id, key)
    storage.put(key, local_path)


def _require_own_derived_key(store, video_id: int, user_id: int, key: str) -> None:
    video = _get_owned_video(store, video_id, user_id)
    if not key.startswith(f"users/{video.user_id}/videos/{video.id}/derived/"):
        raise MediaOwnershipError(f"derived key does not belong to video {video_id}.")


def _get_owned_video(store: ContentStore | ContentStoreProtocol, video_id: int, user_id: int) -> VideoRecord:
    video = store.get_video(video_id)
    if video is None:
        raise MediaOwnershipError(f"No video with id={video_id}.")
    if video.user_id != user_id:
        raise MediaOwnershipError(
            f"video {video_id} (user_id={video.user_id}) does not belong to user_id={user_id}."
        )
    return video


def upload_canonical_media(store: ContentStore, storage: StorageProtocol, video_id: int, user_id: int) -> VideoRecord:
    """Upload video_id's current canonical_media_path to `storage` and
    stamp storage_provider/storage_key on its row. Raises
    MediaOwnershipError if video_id does not belong to user_id;
    MediaNotUploadedError if the video has no local canonical_media_path
    yet. Idempotent — safe to call more than once for the same video (the
    deterministic key is simply overwritten with the same bytes)."""
    video = _get_owned_video(store, video_id, user_id)
    if not video.canonical_media_path:
        raise MediaNotUploadedError(f"video {video_id} has no canonical_media_path to upload yet.")

    local_path = Path(video.canonical_media_path)
    if not local_path.exists():
        raise MediaNotUploadedError(f"video {video_id}'s canonical_media_path does not exist: {local_path}")

    key = build_storage_key(user_id, video_id, local_path.suffix)
    storage.put(key, local_path)
    store.update_video(video_id, storage_provider=storage.provider_name, storage_key=key)
    return store.get_video(video_id)


def create_video_from_upload(
    store: ContentStoreProtocol,
    storage: StorageProtocol,
    user_id: int,
    local_path: Path,
    original_filename: str,
    file_hash: str,
    file_size_bytes: int,
    created_at: str,
) -> VideoRecord:
    """The hosted-batch-upload counterpart to upload_canonical_media
    (Milestone 3.7): creates a brand-new owned videos row directly from
    already-received bytes at local_path (a temp file the caller wrote from
    an HTTP upload — api/routes/videos.py), rather than migrating a video
    that already exists from local CLI ingestion. canonical_media_path is
    deliberately left NULL, never set to local_path — that temp file does
    not survive past the request (unlike the local ingestion pipeline's
    permanently-retained content/processed/ copy, see ADR-0009
    "Retention"), so storage_provider/storage_key are the only reference
    this row ever has, exactly the "migrated" shape
    materialize_canonical_media already knows how to resolve. status stays
    the schema default ('DISCOVERED') — no transcription/classification/
    scheduling happens here; this function's only job is "bytes are safely
    stored and there is an owned DB row for them" (see this milestone's own
    scope guardrail).

    Always creates a new row (Milestone 3.7 re-upload-architecture
    follow-up) — never looks file_hash up first. videos.id is this
    record's real identity; file_hash is a reusable content fingerprint,
    not a uniqueness constraint (the schema-level UNIQUE(file_hash) that
    used to make this idempotent-by-content, and that a different user's
    identical upload would collide against, has been dropped — see
    persistence/content_store.py's _migrate_videos_drop_file_hash_uniqueness
    and docs/decisions/0009-object-storage-media-lifecycle.md's follow-up
    addendum). A creator intentionally re-uploading the exact same bytes
    later — a new caption, a new schedule, a new campaign — gets a distinct
    video record every time, same as two different users uploading
    identical content each getting their own. A caller that wants "was
    this exact content uploaded before" for its own purposes (e.g. warning
    a user before they re-upload) should query store.get_video_by_hash
    itself — this function no longer makes that decision on the caller's
    behalf.

    If storage.put() fails (Milestone 3.7 upload-failure-semantics
    follow-up — e.g. a real Supabase 413 EntityTooLarge), the row already
    inserted below is marked status="FAILED"/failure_reason=<the failure's
    reason_code>/processed_at=now, then the original exception is
    re-raised so the caller (api/routes/videos.py's _process_one_upload)
    still records the attempt as failed exactly as before. This reuses the
    exact same status/failure_reason convention media/processing.py's
    local-ingestion pipeline already uses for its own failures, rather
    than inventing a second one — before this fix, a storage failure left
    the row looking like a normal, merely-not-yet-processed DISCOVERED
    video forever, with no signal anything had gone wrong. The row is
    deliberately kept, not deleted, in this pass — it is real evidence an
    upload was attempted (filename/hash/timestamp)."""
    # Milestone 3.7 has no local-discovery-directory concept at all for a
    # hosted upload — original_path only has to be obviously not a real
    # filesystem path (get_video_by_path, used solely by the local CLI's
    # discover_videos FIFO ordering, must never accidentally match one of
    # these — repeated re-uploads of the same content sharing this exact
    # string across multiple rows is harmless, since nothing ever looks
    # this shape of path up). No user-provided filename or secret
    # information embedded, matching build_storage_key's own convention
    # above.
    original_path = f"hosted-upload/{file_hash}"
    video = store.insert_video(
        file_hash=file_hash, original_filename=original_filename, original_path=original_path,
        created_at=created_at, user_id=user_id,
    )
    key = build_storage_key(user_id, video.id, Path(original_filename).suffix)
    try:
        storage.put(key, local_path)
    except Exception as exc:
        store.update_video(
            video.id,
            status="FAILED",
            failure_reason=getattr(exc, "reason_code", "UPLOAD_FAILED"),
            processed_at=datetime.now(timezone.utc).isoformat(),
        )
        raise
    store.update_video(video.id, storage_provider=storage.provider_name, storage_key=key, file_size_bytes=file_size_bytes)
    return store.get_video(video.id)


def delete_video(store: ContentStoreProtocol, storage: StorageProtocol, video_id: int, user_id: int) -> None:
    """Delete an owned video (Milestone 3.7 follow-up — Delete Video, the
    Library's authenticated delete action). Raises MediaOwnershipError if
    video_id does not belong to user_id (also raised, deliberately, if
    video_id does not exist at all — see _get_owned_video; the caller-
    facing distinction between "not yours" and "doesn't exist" is not
    worth making, matching this module's existing cross-tenant-silence
    convention). Raises VideoHasScheduleReferencesError, without touching
    anything, if the video is still assigned to a content_slot or has any
    platform_posts row — see that error's own docstring; this is a fail-
    safe refusal, never a cascading delete into scheduling/publishing
    state.

    When safe to delete:
      - the stored object is removed via `storage` (StorageProtocol.delete
        is idempotent, so a video that was never actually uploaded to that
        backend — storage_key is NULL, e.g. a legacy local-only video from
        before Milestone 3.4 — has nothing to delete and this step is
        skipped entirely; canonical_media_path itself is never touched,
        matching ADR-0009's "Retention" decision to keep the local
        ingestion pipeline's files indefinitely and out of scope here).
      - the owned videos row is removed (store.delete_video), which also
        nulls out (never deletes) any upload_attempts row that pointed at
        it — see ContentStore.delete_video's own docstring for why that
        preserves historical upload-performance telemetry rather than
        erasing it.

    Re-uploading the exact same file afterward always works and always
    creates another brand-new row — create_video_from_upload never checked
    file_hash for this row's existence in the first place (Milestone 3.7
    re-upload-architecture follow-up), so deleting it changes nothing about
    that."""
    video = _get_owned_video(store, video_id, user_id)
    if video.assigned_slot_id is not None:
        raise VideoHasScheduleReferencesError(
            f"video {video_id} is still assigned to a scheduled slot; unassign or cancel it first."
        )
    if store.list_platform_posts_for_video(video_id):
        raise VideoHasScheduleReferencesError(
            f"video {video_id} has a platform post; remove or cancel it first."
        )

    if video.storage_key:
        storage.delete(video.storage_key)
    store.delete_video(video_id)


@contextmanager
def materialize_canonical_media(
    store: ContentStore, storage: StorageProtocol, video_id: int, user_id: int
) -> Iterator[Path]:
    """Yield a real local Path to video_id's canonical media — via
    `storage` if it has been uploaded (storage_provider set), or directly
    from canonical_media_path (no network call) if not. See module
    docstring."""
    video = _get_owned_video(store, video_id, user_id)

    if video.storage_provider and video.storage_key:
        with storage.materialize(video.storage_key) as path:
            yield path
        return

    if not video.canonical_media_path:
        raise MediaNotUploadedError(f"video {video_id} has no canonical_media_path and was never uploaded to storage.")
    yield Path(video.canonical_media_path)
