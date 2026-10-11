# Milestone 4.2 — Instagram Reels Publishing: Evaluation

Architecture: [ADR-0018](../../decisions/0018-instagram-integration-architecture.md) (Decision 5
and the 2026-10-10 addendum).

## Status

**Implemented and tested with mocked Meta responses on SQLite and Postgres. Live Reel
publication is NOT yet verified**: it needs this code deployed to both Railway services first
(see "Why the live Reel test is blocked"). Live signed-URL delivery and the production Instagram
credential were verified (below). Not committed, not deployed.

## What was built

- **Instagram publisher** (`publishing/instagram/`):
  - `content_publishing.py` holds the only Meta publishing calls: container creation
    (`media_type=REELS`, `video_url`, optional caption), container status, `media_publish`. Meta
    errors map to classified reason codes; no token, URL or response body appears in errors.
  - `publisher.py`: `InstagramPublisher` plus the hosted per-user factory (token from the user's
    own connection, refreshed through the 4.1 credential store; account id from the same stored
    credential).
  - `media_requirements.py`: Reels media and caption limits.
- **Shared machinery:**
  - `Publisher.finalize` / `STATUS_READY_TO_FINALIZE`;
  - `scheduling/finalization.py` (the only `media_publish` caller);
  - READY handling in the inline poll, reconciliation and crash recovery;
  - pull-URL execution path in `publish_tiktok.execute_claimed_platform_post` (stored metadata
    validated without downloading, or the file probed once; signed URL issued just before
    submission);
  - `PUBLISH_REQUESTED` retry rules in `manual_recovery.py` / `publish_status.py`;
  - worker dispatch by platform.
- **Persistence:** `platform_posts.platform_media_id` (migration `0012`, SQLite column
  migration). Postgres reads now ignore unknown columns.
- **API:** optional `platforms` on `POST /api/queue/slots/{id}/assign` and
  `POST /api/queue/assign-next`. Publications carry `stage`.
- **Web:** "Publish to" picker in Queue (only when Instagram is connected) and per-platform
  delivery rows (Scheduled / Processing / Publishing / Published / Failed / Unknown).

## Lifecycle and state transitions

`platform_post_id` = container id; `platform_media_id` = Reel media id.

| Persisted state | Meaning | On restart / next cycle |
|---|---|---|
| PENDING | not submitted | due selection claims it |
| PUBLISHING, no id, `submission_state` NULL | claimed, never reached Instagram | crash recovery requeues |
| PUBLISHING, no id, `AWAITING_PLATFORM_ID` | container creation in progress | crash recovery: bounded retry. No container id persisted ⇒ nothing can be posted (an orphan container never posts and expires in 24 h). |
| PUBLISHING, id, `submission_state` NULL | container created; Meta processing (or ready, about to be published) | status check: IN_PROGRESS → wait; FINISHED → finalize; PUBLISHED → done; ERROR/EXPIRED → FAILED. **Never a new container.** |
| PUBLISHING, id, `PUBLISH_REQUESTED` | `media_publish` sent or in flight | status check: PUBLISHED → PUBLISHED; FINISHED → wait during the grace period, then UNKNOWN (`PUBLISH_OUTCOME_UNKNOWN`). Never re-published automatically. |
| PUBLISHED (+ `platform_media_id`) | Reel published | terminal; never resubmitted |
| FAILED, id | container ERROR/EXPIRED (nothing posted) | manual retry creates a new container |
| FAILED, no id | pre-submission failure (validation, storage, auth) | manual retry |
| UNKNOWN, id, `PUBLISH_REQUESTED` | publish outcome unconfirmed | retry requires the user to confirm it isn't on Instagram, then re-checks the same container |
| UNKNOWN, id, NULL | status unreadable or a terminal 4xx at publish (e.g. auth) | retry re-checks the same container (no confirmation) |

Signed URL: issued after the `AWAITING_PLATFORM_ID` checkpoint, immediately before container
creation, default 1 h lifetime (`INSTAGRAM_MEDIA_URL_TTL_SECONDS`). It's never stored, returned
or logged, and a fresh one is issued for every new container.

## Automated evidence

- `tests/test_instagram_content_publishing.py` (37): Meta client requests and error mapping
  (190, 9007/2207027, 2207042, 9004, transient/5xx, timeout, network, malformed), no secret
  leakage, publisher status mapping, hosted credentials (own connection only, auth/credential
  failures), Reels media and caption limits.
- `tests/test_instagram_publishing_flow.py` (20 × SQLite and Postgres = 40):
  - happy path with both ids kept and the signed URL neither persisted nor logged;
  - captionless;
  - timeout before a container exists; error after container creation; restart during
    processing; restart before the id was saved;
  - ambiguous `media_publish` resolved from status; unconfirmed publish parked UNKNOWN and retried
    only on the same container with confirmation;
  - 4xx retryable and terminal rejections; failed container with deliberate replacement;
  - concurrent finalize (compare-and-swap → published once);
  - validation, caption and unsigned-storage failures before any Meta call;
  - Processing vs Publishing status;
  - explicit platform choice and connection requirement; default assignment unchanged;
  - platform dispatch with TikTok unaffected by an Instagram failure; per-user isolation.

  This run caught a Postgres-only bug, since fixed: finalization without a user scope never
  matched Postgres's `user_id` compare-and-swap.
- `tests/test_api_queue.py` (+4): `platforms` on the assign endpoints.
- `tests/test_postgres_forward_compat.py` (2) and `tests/test_sqlite_platform_media_id.py` (2):
  the new column on fresh and existing databases; reads ignore unknown columns.
- `web/tests/routes/queue-instagram.test.tsx` (6): picker visibility and payload, empty choice
  blocked, per-platform rows, TikTok-only slots unchanged, delivery labels.
- Full results are in the CHANGELOG entry for this milestone.

## Live validation (2026-10-10)

**Signed media URL** (real Supabase, existing hosted video 15, `.mov`, 14.6 MB). Token never
printed:
- HTTPS; a byte-range GET with no auth headers returns 206 `video/quicktime`; a full GET returns
  200 with all bytes (`ftyp` header);
- the same path without the token → 400; the public-bucket route → 400 (bucket stays private);
- a 5-second URL → 206 immediately, then 400 `InvalidJWT` (expired) after 8 s.

**Production Instagram credential** (read-only):
- the ACTIVE connection's credential decrypts with the configured key;
- it holds `instagram_business_basic` and `instagram_business_content_publish`, valid until
  2026-12-09;
- live `GET /me` → **@picklebatchapp**.

**Live Reel publication and restart test: not run.** See below.

## Why the live Reel test is blocked

Any process that opens a `PostgresContentStore` from this code applies migration `0012`. The
code deployed today (pre-4.2) builds records from `SELECT *` rows and raises on the unknown
column, so running the live test against production before deploying would break the live API
and TikTok worker. The test therefore has to follow a deploy of this code to **both** services
(API and worker, so neither runs pre-4.2 code against the migrated table).

### Incident (2026-10-10, resolved)

A read-only investigation query run from this working tree opened a production store and applied
`0012` at 19:11:29 UTC, before that behavior was known. Deployed code was then unable to read
`platform_posts` (Queue, Library, worker cycles), while `/api/health` stayed 200 because it
doesn't read that table.

- **Discovered:** during this milestone's pre-flight checks.
- **Rolled back:** with the user's approval. In one transaction, guarded on the column being
  empty (0 rows had a value): `DROP COLUMN platform_media_id`, and the `0012` row was deleted
  from `schema_migrations`.
- **Verified:** the committed (deployed) code then read production `platform_posts` without error
  and had no pending migrations.
- **Impact:** no posts were affected. User 2's four posts were already published; the only open
  posts are two legacy CLI-user TikTok rows untouched since 2026-09-17; nothing was left
  mid-claim.
- **Follow-ups done:** reads ignore unknown columns from 4.2 on; AGENTS.md records the rule.

### Live test runbook (after deploying this commit to API and worker)

1. Confirm Settings shows Instagram connected as @picklebatchapp.
2. Upload a disposable 1080×1920 (≤1920 px wide), 5–60 s H.264/AAC clip. The existing 4K uploads
   are correctly rejected. One way to make one:
   `ffmpeg -f lavfi -i testsrc=duration=8:size=1080x1920:rate=30 -f lavfi -i sine=duration=8 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -movflags +faststart pickle-batch-test-reel.mp4`
3. Queue: "Publish to" → Instagram only → assign to a slot due soon.
4. When the slot is due, watch: Processing → Publishing → Published. Confirm the Reel exists on
   @picklebatchapp and Pickle Batch shows Published with a media id.
5. Restart test on a second disposable clip: while Queue shows **Processing**, restart the
   Railway worker. The worker must resume the persisted container (worker logs show no second
   `instagram_container_create_started` for that row) and publish exactly once.
6. Delete the test Reels afterwards if wanted.

## Remaining limitations

- Live Reel publication, Meta's acceptance of Supabase signed URLs as `video_url`, and the live
  restart test are unverified until the runbook is run.
- Whether `media_publish` is idempotent per container is undocumented by Meta. The design never
  relies on it; an unconfirmed publish goes to UNKNOWN for a human decision.
- When publication is confirmed only from container status (after an ambiguous `media_publish`),
  `platform_media_id` stays NULL.
- No automatic transcoding: videos wider than 1920 px (all current 4K iPhone uploads) can't
  publish to Instagram.
- Failed or expired containers are replaced only by a manual retry.
- `content_publishing_limit` isn't pre-checked; the limit error is retried a few times, then the
  post fails with a "try again later" explanation.
- Assignment chooses platforms per video only at scheduling time. There's no "add Instagram to an
  already scheduled video" and no per-platform caption yet (Milestone 4.3).
- Long-lived token refresh against Meta (needs a token ≥24 h old) remains to be observed (4.1B
  carry-over).
- `tests/test_media_storage_postgres.py::test_upload_to_published_…` fails in this environment on
  the committed code too (a stale object at a shared key in the Supabase test bucket). Pre-existing
  and unrelated to 4.2.

## Addendum — 2026-10-11: live Reel published

Supersedes "Live Reel publication … not run" above. After the user deployed 4.2, a real
iPhone 1080p clip (video 19, coded 1920×1080 with a rotation flag) published to @picklebatchapp.
Production shows `platform_posts` 34 PUBLISHED at 2026-10-10 20:56:49 UTC, with both the container
id and the Instagram media id stored (checked read-only). The same live session showed iPhone
portrait 4K (video 18) rejected as `INSTAGRAM_MEDIA_RESOLUTION`. That was fixed by 4.2.1
(normalization) and 4.2.2 (Retry for Instagram slots). See
`milestone-4.2.1-instagram-media-normalization.md`. The live restart/recovery test is still not
run.
