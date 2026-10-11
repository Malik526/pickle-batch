# Milestone 4.2.1 — Instagram Media Normalization (+ 4.2.2 Legacy Retry): Evaluation

Written 2026-10-11. 4.2.1 was committed (`10a717e`) from a mid-implementation state; its design
and the work it left outstanding are in
[`docs/roadmap/milestone-4.2.1/M4.2.1-handover.md`](../../roadmap/milestone-4.2.1/M4.2.1-handover.md).
This record states what is verified as of this date. 4.2.2 (below) is uncommitted.

## 4.2.1 — status

**Implemented and covered by unit and real-encode tests. Live 4K normalization not yet verified.**

- Root cause of the 4.2 failure (production, read-only): iPhone portrait 4K is coded 3840×2160
  with a rotation flag and displays 2160×3840. 4.2 compared the coded width with Instagram's 1920
  limit.
- Behavior: a video that meets the Reels spec is published untouched. Otherwise a single ffmpeg
  pass makes a private derivative:
  - H.264/AAC MP4, longer side ≤1920 (portrait 4K → 1080×1920), never upscaled or cropped;
  - fps kept within 23–60 (>60 → 60, <23 → 30);
  - HDR tone-mapped; metadata such as GPS dropped; faststart;
  - verified before use.
- Storage: `users/<u>/videos/<v>/derived/instagram/reel-<policy>-<file_hash>.mp4`. The key is the
  persistence: no schema change. A retry reuses it with no download and no encode; a changed
  source or policy version creates a new key.
- Hard limits stay failures: 3 s–15 min duration, caption limits.
- A long encode refreshes its claimed post about every minute; it times out after
  `INSTAGRAM_NORMALIZATION_TIMEOUT_SECONDS` (default 3600).
- Tests: `tests/test_instagram_media_normalization.py` (34; real encodes of a rotated portrait
  4K, landscape 4K, 60/120 fps, PCM-audio MOV and 10-bit HEVC fixtures; reuse; stale hash; policy
  version; ffmpeg failure; timeout with heartbeat; corrupt; too short or too long). The 4K
  portrait synthetic fixture (3.5 s) encoded in about 3.5 s on the development machine.

### Still outstanding from 4.2.1 (per the handover)

- A Supabase test-bucket test for the derived-key path.
- `media_storage.delete_video` doesn't remove derivatives. This isn't currently reachable,
  because delete is refused while a video has any platform post.
- Representative timings for a longer real 4K clip on the production worker.
- Live: an iPhone portrait 4K clip normalized and published, and a 1080p clip published without
  re-encoding.

## 4.2.2 — Retry of pre-normalization failures

### Root cause

Not a stale failure state or the worker: Retry never reached the post. The Queue's Retry button
sends no platform. `RetryPublishRequest.platform` defaulted to `"tiktok"`, so for an
Instagram-only slot the endpoint looked up a TikTok post, found none, and returned **404**. The
Instagram row was never touched, and the card kept showing the old failure.

Production confirmed it read-only: the 4K post (`platform_posts` 33, video 18,
`INSTAGRAM_MEDIA_RESOLUTION`) was unchanged since 2026-10-10 20:39 UTC. The new API test
reproduces the 404 on the committed code.

Once a retry does go through, the 4.2.1 worker path already re-runs media preparation (probe,
then normalize or reuse) from the stored source, so no re-upload is needed.

### Fix

- `RetryPublishRequest.platform` is now optional. Omitted, the endpoint retries **every
  retryable post** of the slot's video (`scheduling/manual_recovery.retry_slot_posts`), each
  through the existing per-post rules. A named platform behaves exactly as before.
- All checks run before anything changes. If any retryable post needs the "it wasn't posted"
  confirmation and it wasn't given, nothing is retried, and the confirmation flag is passed only
  to posts that need it.
- A retried legacy Instagram failure goes FAILED → PENDING (the 3.13 resubmit semantics: retry
  budget reset, stored failure code kept for audit until replaced, `manual_retry` event logged
  with the previous code). The Queue shows Scheduled with no old message; a new failure replaces
  it.
- `INSTAGRAM_MEDIA_DURATION` is no longer retryable (`publish_status.post_can_retry`): the source
  is immutable and is never trimmed or padded. Retry is hidden, and the API answers 409
  `NOT_RETRYABLE` with "retrying won't change that". Caption failures stay retryable (the caption
  can be edited first), as does `INSTAGRAM_MEDIA_RESOLUTION`.

### Duplicate-prevention

Unchanged, because each post still goes through `retry_platform_post`:
- no container → safe restart from media preparation;
- UNKNOWN with a container → re-check of the same container;
- a container that may have been published needs confirmation;
- only a FAILED ERROR/EXPIRED container is deliberately replaced.

### Tests

- `tests/test_instagram_publishing_flow.py` (+6 scenarios × SQLite and Postgres):
  - legacy too-wide failure → retry → real normalization → Instagram receives the derivative →
    published, source intact, one container;
  - an existing derivative is reused;
  - a duration failure isn't retryable;
  - a container-holding post is re-checked, never re-created;
  - a slot retry covers both platforms and respects confirmation;
  - another user's post can't be retried.
- `tests/test_api_queue_retry.py` (+3): an Instagram-only slot retry with no platform → 200 and
  PENDING (404 on the committed code); a duration failure → 409; naming a platform with no post
  → still 404. The existing TikTok retry tests are unchanged and pass.
- Full backend suite (SQLite and Postgres, 2026-10-11): **1545 passed, 1 failed**. The failure is
  the known pre-existing `tests/test_media_storage_postgres.py::test_upload_to_published_…`
  (stale object in the shared Supabase test bucket; fails on committed code too). No frontend
  change, so web checks weren't rerun. `git diff --check` clean.

### Live validation

Pending deploy. Then press Retry on the 4K post (video 18): expect Scheduled → Processing →
Published with no re-upload, and the worker log `instagram_media_normalized …
output_resolution=1080x1920`.

## Addendum — 2026-10-11 (later): live 4.2.2 retry reached normalization

After the 4.2.2 deploy, Retry on video 18's post reached media preparation: the 404 is fixed.
The 4K encode was then killed with `exit_code=-9`, and production shows post 33
`FAILED / INSTAGRAM_MEDIA_PREPARATION_FAILED` at 2026-10-11 00:22:30 UTC. Cause: unbounded ffmpeg
threads exhausting the worker's memory. Fixed in
[4.2.3](milestone-4.2.3-resource-safe-normalization.md).
