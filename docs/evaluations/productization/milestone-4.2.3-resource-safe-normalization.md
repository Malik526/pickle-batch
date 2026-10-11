# Milestone 4.2.3 — Resource-Safe Instagram Normalization + Retry Backoff: Evaluation

Written 2026-10-11. Uncommitted, not deployed. Builds on
[4.2.1/4.2.2](milestone-4.2.1-instagram-media-normalization.md).

## Status

**Implemented and tested locally on SQLite and Postgres. Live validation on Railway is pending a
deploy.** The worker's actual memory limit is still unknown (see "Railway limits").

## Root cause of `exit_code=-9`

**ffmpeg sized its thread pools from the visible core count, and its memory grew with them.**
The normalization command set no thread limits, so x264 frame threads, the H.264 decoder and the
scale filter each scaled to every core the process could see. Each frame thread buffers frames,
and a decoded 4K frame is about 12 MB.

`-9` is SIGKILL from outside ffmpeg; it isn't our timeout, which raises its own
`…_TIMEOUT` error before looking at the exit code. Inside a memory-capped container that is the
kernel OOM killer picking the largest process: ffmpeg.

Reproduced locally with an iPhone-style portrait 4K source (coded 3840×2160 + rotation flag,
displays 2160×3840, 30 fps, H.264, 6 s, deliberately heavy ~113 Mb/s) and the exact production
command:

| Setting | Peak RSS | ffmpeg threads | Wall time | Output |
|---|---|---|---|---|
| auto threads (before) | **1,185 MB** | 66 | 3.4 s | 1080×1920, 30 fps |
| decode 2 / encode 2 / filter 1 (**chosen**) | **343 MB** | 7 | 2.6 s | 1080×1920, 30 fps |
| decode 1 / encode 1 / filter 1 | 267 MB | 1 | 9.1 s | same |
| decode 4 / encode 4 / filter 1 | 453 MB | 13 | 3.3 s | same |
| 2/2/1 + `rc-lookahead=10` | 341 MB | 7 | 4.1 s | same (no gain, not used) |

Machine: 16 visible cores, 7.7 GB RAM, ffmpeg 4.4. The worker image uses Debian bookworm's
ffmpeg 5.1. A container that sees more host cores than this machine would allocate even more
when unbounded.

## Resource fix

- `-threads N` as an input option (decoder) and as an output option (encoder), plus
  `-filter_threads 1`. `N = INSTAGRAM_NORMALIZATION_THREADS`, default 2. Two threads use 3.5× less
  memory than auto with no slowdown on this machine; one thread saves only another ~75 MB at
  3.5× the time.
- Unchanged: the output policy (longer side ≤1920, so portrait 4K → 1080×1920, no crop, no
  upscale, H.264/AAC MP4, fps rules, HDR tone-mapping, metadata stripping), `veryfast`, single
  pass.
- Python never holds video in memory. Download and hashing stream in 1 MB chunks; ffmpeg reads
  and writes files; stderr is tiny (`-loglevel error`). There's one temp copy of the source and
  one output.
- Observability:
  - the start event (`instagram_media_normalizing`) logs `threads`, `memory_limit_mb` (cgroup
    v2/v1), `visible_cpus` and `usable_cpus`;
  - the result events (`instagram_media_normalized`, `instagram_media_normalization_failed`)
    also log `peak_rss_mb` (ffmpeg's VmHWM, sampled every second), `encode_seconds`, `exit_code`
    and `stage`;
  - existing fields: source/target resolution, fps, codec, bytes, duration, reasons, reuse.
  - Never logged: URLs, tokens, paths.

## Failure classification

| Ending | Code | Automatic handling |
|---|---|---|
| ffmpeg nonzero exit (a real encode error), verification failure, corrupt source, duration limit | `INSTAGRAM_MEDIA_PREPARATION_FAILED` / existing codes | FAILED, explicit Retry |
| our timeout (`INSTAGRAM_NORMALIZATION_TIMEOUT_SECONDS`, 3600) | `…_TIMEOUT` | FAILED, explicit Retry |
| SIGKILL from outside (OOM killer) | `…_KILLED` (new) | FAILED, explicit Retry. Retrying automatically would re-run the same out-of-memory encode. |
| another signal (e.g. SIGTERM while the worker stops) | `…_INTERRUPTED` (new) | the normal bounded backoff (`RETRY_BACKOFF_MINUTES`, then FAILED) |
| whole worker dies mid-preparation (stale `PREPARING_MEDIA`) | `…_INTERRUPTED` | one automatic retry after 15 min; a second interruption → FAILED |
| storage download errors | existing storage codes | existing bounded backoff |

## Why failed posts could be reclaimed

A FAILED row is never selected by due selection: only PENDING rows are claimed, and a
preparation failure handled by the worker always ended FAILED. The one automatic path back
was **crash recovery**:
- a claim that was preparing media had `submission_state` NULL, so a stale row looked like
  "claimed, never reached the platform" (Case A1);
- Case A1 is requeued **for free and without limit**;
- if the encode took the whole worker container down (or a deploy restarted it), the post was
  re-claimed and re-encoded after every `PLATFORM_POST_STALE_MINUTES`, forever.

Manual Retry presses also re-run it, by design. Railway's logs weren't available to this session,
so which path produced the observed repeats isn't confirmed. Both are now bounded: the first by
the fix below, and an OOM-killed encode is no longer automatically retryable at all.

Fix:
- the worker writes `submission_state = PREPARING_MEDIA` before preparation, cleared by the
  submission checkpoint or a failure;
- crash recovery's new Case A4 retries a stale preparing claim once, after 15 minutes, then
  parks it FAILED;
- the heartbeat (`updated_at` about every 60 s during an encode) keeps a live encode from
  looking stale, so A4 only acts when the worker really died;
- while preparing, the Queue shows Instagram **Processing** ("Preparing the video for
  Instagram…").

## Safety

- Duplicate prevention is unchanged: preparation happens before the submission checkpoint, so
  nothing has been sent to Instagram during any of the paths above. Container rules are as in
  4.2.
- M4.2.2 Retry is unchanged (Instagram-only, both-platform, confirmation, container re-check).
  KILLED and INTERRUPTED stay manually retryable; only `INSTAGRAM_MEDIA_DURATION` isn't.

## Tests

- `tests/test_instagram_media_normalization.py` (+6):
  - thread caps in the command;
  - exit classification (incl. real processes ended by `kill -9` / `kill -15` / `exit 3`);
  - peak-RSS sampling (an 80 MB child is measured);
  - resource-limit fields;
  - a constrained portrait-4K encode producing 1080×1920 under a 700 MB bound.
- `tests/test_instagram_publishing_flow.py` (+6 × SQLite and Postgres):
  - a due 4K post normalized, derivative signed, published (logs threads, peak RSS, 1080×1920);
  - a deterministic failure encoded once and never reclaimed across later polls;
  - an OOM-killed encode never reclaimed, then explicit Retry → published;
  - an interrupted encode backs off and isn't reclaimed early;
  - worker death mid-preparation → one retry, then FAILED;
  - a long encode keeps its claim fresh (heartbeat) and shows Processing.
- Earlier 4.2.1/4.2.2 tests are unchanged and pass (60 FPS kept, unsupported FPS normalized, 1080p
  bypass, landscape 4K, retry scenarios, TikTok).
- Full backend suite (SQLite and Postgres): **1568 passed, 1 failed**. The failure is the known
  pre-existing `tests/test_media_storage_postgres.py::test_upload_to_published_…` (stale object in
  the shared Supabase test bucket; fails on committed code too). No frontend change, so web
  checks weren't rerun. `git diff --check` clean.

## Railway limits

Not determinable from this session (no Railway CLI or dashboard access). The new
`instagram_media_normalizing` log line records the container's `memory_limit_mb` and CPU counts
on the first encode. **Record them here after the live retry.** At 2 threads the measured peak for
a heavy 4K clip is ~340 MB plus the Python worker; if the worker's limit turns out to be ≤512 MB,
set `INSTAGRAM_NORMALIZATION_THREADS=1` (~270 MB) before considering a larger plan.

## Live validation (pending deploy)

Retry video 18's post. Expect, in order:
1. `instagram_media_normalizing … threads=2 memory_limit_mb=…`;
2. `instagram_media_normalized … output_resolution=1080x1920 peak_rss_mb=… encode_seconds=…`;
3. a container created, then the Reel published, and the Queue shows Published;
4. no repeated `instagram_media_normalizing` for that post, and no duplicate Reel.

Record: transcode time, source and output bytes, peak RSS, memory limit.
