# Content Automation — Changelog

## 2026-10-11

### Milestone 4.2.3 — Resource-Safe Instagram Normalization + No Re-Encode Loops

- Root cause of `exit_code=-9`: ffmpeg sized its thread pools from the visible host cores, and
  its memory with them; SIGKILL then came from the OOM killer. Reproduced with an iPhone-style
  portrait 4K source: auto threads → 66 threads, 1,185 MB peak; capped → 7 threads, 343 MB, no
  slower.
- Normalization now caps decoder and encoder threads (`INSTAGRAM_NORMALIZATION_THREADS`,
  default 2) and filter threads (1). The output policy is unchanged. It logs `threads`, the
  container's `memory_limit_mb`/CPU counts, ffmpeg `peak_rss_mb` and encode time.
- Encode endings are classified: real error → `…_PREPARATION_FAILED`; timeout → `…_TIMEOUT`;
  SIGKILL → `…_KILLED` (new, terminal: explicit Retry); other signal → `…_INTERRUPTED` (new,
  bounded backoff).
- No more re-encode loops: preparation runs under a new `submission_state = PREPARING_MEDIA`.
  Crash recovery retries a stale preparing claim once (after 15 min), then parks it FAILED;
  before, it requeued it without limit. The Queue shows Processing while preparing. Duplicate
  prevention and M4.2.2 Retry are unchanged.
- Tests: +6 normalization tests (thread caps, real `kill -9`/`-15`/exit classification, RSS
  sampling, a constrained 4K encode under 700 MB) and +6 worker flow scenarios × SQLite/Postgres
  (due 4K publish, deterministic and OOM failures never reclaimed, interrupted backoff, worker
  death bounded, heartbeat plus Processing). See
  `docs/evaluations/productization/milestone-4.2.3-resource-safe-normalization.md`.
- Full backend 1568 passed, 1 failed (the known pre-existing test-bucket failure). No frontend
  change.

### Milestone 4.2.2 — Retry for Instagram Slots Reaches the Post

- Root cause: the Queue's Retry sends no platform and `RetryPublishRequest.platform` defaulted
  to `"tiktok"`, so an Instagram-only slot's Retry returned 404 and the failed row never moved.
  Production's 4K post 33 was untouched since 2026-10-10.
- With no platform named, Retry now retries every retryable post of the slot
  (`manual_recovery.retry_slot_posts`), each through the existing per-post rules: no new
  container while one may exist, confirmation before anything changes, and the confirmation flag
  passed only where needed. A named platform behaves as before.
- Legacy `INSTAGRAM_MEDIA_RESOLUTION` failures now retry into 4.2.1 media preparation from the
  stored source (no re-upload). `INSTAGRAM_MEDIA_DURATION` is not retryable: Retry is hidden and
  the API answers 409 `NOT_RETRYABLE`.
- Tests: 6 flow scenarios × SQLite/Postgres in `test_instagram_publishing_flow.py` (real
  normalization of a rotated 4K fixture on retry, derivative reuse, duration refusal, container
  re-check, both-platform retry with confirmation, owner isolation) and 3 API tests (the 404
  reproduced on committed code). TikTok retry tests are unchanged. Full results are in the 4.2.1
  evaluation.

### Docs — 4.2 Live Result, 4.2.1 Evaluation

- `milestone-4.2-instagram-reels-publishing.md` addendum: the live Reel published (post 34,
  verified read-only); this supersedes "not run".
- New `milestone-4.2.1-instagram-media-normalization.md`: what 4.2.1 (`10a717e`, committed
  mid-implementation, no changelog entry at the time) does and verifies, what's still outstanding
  from its handover, and 4.2.2.
- `PROJECT_STATE.md` and the roadmap index updated.

## 2026-10-10

### Milestone 4.2 — Instagram Reels Publishing

Implemented and tested with mocked Meta on SQLite and Postgres. Live Reel publication is not
verified yet: it needs this code deployed to both Railway services. Not deployed; migration `0012`
not applied to production.

- Instagram publisher (`publishing/instagram/content_publishing.py`, `publisher.py`):
  - Reels container from a short-lived signed URL, container status, `media_publish`;
  - Meta errors mapped to classified reason codes with no token, URL or body in messages;
  - hosted per-user credentials through the 4.1 credential store (the worker needs no Instagram
    secret).
- `media_requirements.py`: Reels limits (MOV/MP4, H.264/HEVC, AAC, 3 s–15 min, 23–60 fps,
  ≤300 MB, ≤1920 px wide) and caption limits (2,200 chars, 30 hashtags, 20 mentions), checked
  before any Meta call. No transcoding, so current 4K uploads are rejected with an actionable
  reason.
- Shared machinery:
  - `Publisher.requires_finalize` / `finalize()` and `STATUS_READY_TO_FINALIZE`;
  - `scheduling/finalization.py` as the only `media_publish` caller, behind a
    compare-and-swap `PUBLISH_REQUESTED` checkpoint: ambiguous outcomes resolve from container
    status, a container is never re-published automatically, and unconfirmed publishes are parked
    UNKNOWN;
  - READY handling in the inline poll, reconciliation and crash recovery;
  - pull-URL execution path (signed URL issued after the checkpoint, never stored or logged);
  - manual retry re-checks the same container, with confirmation when a publish may have
    happened.
- Persistence: `platform_posts.platform_media_id` (Postgres `0012`, SQLite column migration).
  The container id stays in `platform_post_id`. Postgres reads ignore unknown columns.
- Worker dispatches by platform with errors isolated per user and platform. `log_event` moved to
  `scheduling/telemetry.py` (re-exported from `worker`).
- API: optional `platforms` on the assign endpoints (validated publishable and connected; omitted
  keeps the default). Publications carry `stage` (PROCESSING/PUBLISHING).
- Retry classification and failure-taxonomy entries for the new Instagram codes. Instagram is
  `publishing_available=True`.
- Web: Queue "Publish to" picker (only when Instagram is connected) and per-platform delivery
  rows (Scheduled / Processing / Publishing / Published / Failed / Unknown).
- Tests:
  - new `test_instagram_content_publishing.py` (37), `test_instagram_publishing_flow.py`
    (20 × SQLite/Postgres), `test_postgres_forward_compat.py`,
    `test_sqlite_platform_media_id.py`; `test_api_queue.py` +4;
    `web/tests/routes/queue-instagram.test.tsx` (6);
  - updated: the 4.0 registry tests (they encoded "not publishable until 4.2"; the guard is now
    tested with a synthetic platform), the exact-shape publication test (+`stage`), and
    `test_api_platforms_instagram.py` (no longer depends on the developer's `.env`).
  - Backend 1488 passed, 1 failed: `test_media_storage_postgres`, which also fails on the
    committed code (stale object at a shared key in the test bucket).
  - Frontend 214/214; eslint, `tsc --noEmit`, `next build`, `git diff --check` clean.
- Live verification: a signed Supabase URL serves the private object without auth (206/200,
  `video/quicktime`); the tokenless and public paths are refused; it expires on time. The
  production Instagram credential decrypts and belongs to @picklebatchapp, with both scopes.

### Operations — Accidental Production Migration 0012 Rolled Back

- 19:11 UTC: a read-only investigation query run from the 4.2 working tree opened a production
  `PostgresContentStore`, which applies pending migrations, and added
  `platform_posts.platform_media_id`. Pre-4.2 deployed code can't read rows with unknown columns,
  so Queue, Library and worker reads of `platform_posts` failed until rollback; `/api/health`
  stayed 200.
- Rolled back with the user's approval in one guarded transaction: the column was empty; it was
  dropped and its `schema_migrations` row deleted. Verified the deployed code then read
  production without error, with no pending migrations. No posts were affected.
- Prevention: Postgres reads ignore unknown columns from 4.2 on. AGENTS.md now forbids opening
  stores against production from a working tree with unreleased migrations, and requires deploying
  the API and worker together.

### Milestone 4.2 — Docs

- ADR-0018 2026-10-10 addendum: implementation decisions (ids, publisher contract, the no
  automatic re-publish rule, assignment, validation, dispatch, `stage`) and the
  migration-on-open deployment rule.
- `docs/evaluations/productization/milestone-4.2-instagram-reels-publishing.md`: state-transition
  table, automated and live evidence, incident record, live-test runbook, limitations.
- `PROJECT_STATE.md`, `AGENTS.md` (durable rules), `README.md`, `.env.example`, roadmap index.

### Web — Public User Data Deletion Page and Real Contact Address

- New public `/data-deletion` page (`web/app/(marketing)/data-deletion/page.tsx`): Meta's "Data
  deletion instructions URL". It is static and needs no login or JavaScript. It covers:
  - how to request deletion by email (prefilled "Data Deletion Request" subject, sender
    verification);
  - the in-app controls that really exist (delete a video in Library; disconnect a platform in
    Settings removes stored tokens);
  - the data categories Pickle Batch actually stores;
  - Pickle Batch data vs Meta accounts and data;
  - revoking access through Meta's settings;
  - conservative retention wording, and links to the Privacy Policy and contact.
- It deliberately makes no promise of deletion timing, automated deletion, deletion from backups
  or deletion from Meta's systems. It is not Meta's Data Deletion Callback endpoint, which doesn't
  exist yet.
- Footer links "Data Deletion" (and now wraps on narrow screens). Legal pages style numbered lists
  (`.legal-content ol`).
- `siteConfig.contactEmail` is now a real, monitored address. The former placeholder
  `support@content-automation.app` used a domain with no mail records, so the Privacy Policy,
  Terms and footer were advertising an undeliverable address. All four pages now share the new
  address.
- Verified:
  - Frontend 208/208, eslint, `tsc --noEmit`, `next build` (exports `out/data-deletion/index.html`).
  - Chromium with JavaScript disabled at 390 px: 200, all sections, no horizontal overflow, mailto
    and Privacy links valid.
  - Production returns 404 until deployed. `/privacy` shows the expected pattern after deploy
    (301 → trailing slash → 200).

## 2026-10-06

### Milestone 4.1 — Instagram OAuth Integration (M4.1A)

Implemented and tested with mocked Meta responses only. **Live Meta OAuth is not verified**
(M4.1B). Not deployed; migration `0011` has not been applied anywhere. No publishing.

- Instagram connect flow: `POST /api/platforms/instagram/connect` (protected), public
  `GET .../callback`, `POST .../disconnect`; status now shows `@username`. Callback binds only
  through a single-use, expiring, Instagram-scoped server state (`code_verifier=''`), exchanges
  with the stored redirect URI, stores only the encrypted long-lived token, records the current
  Instagram account id (also on reconnect to a different account) and redirects to the attempt's
  allowlisted target with `?instagram=connected|denied|invalid_state|expired_state|exchange_failed|unavailable`.
- `publishing/instagram/`: `oauth.py` (authorization URL, short- → long-lived exchange, refresh,
  `GET /me`; structured, secret-free errors), `credential_store.py` (refresh when ≥ 24 h old and
  inside `INSTAGRAM_TOKEN_REFRESH_WINDOW_SECONDS`, default 30 days, under the per-connection lock
  with CAS), `identity.py` (live, best-effort username).
- Shared `publishing/credential_encryption.py`. TikTok's credential store re-exports the same
  helpers; TikTok behavior is unchanged.
- OAuth state consumption is one atomic, platform-scoped update on both backends; a TikTok state
  can't be used, or consumed, by the Instagram callback and vice versa.
- `oauth_states.return_target` (nullable): SQLite fresh schema + additive migration, Postgres
  migration `0011`, protocol and both stores. New `api/oauth_return_targets.py`: exact-match
  server-owned allowlist (web Settings by default, plus optional `OAUTH_EXTRA_RETURN_TARGETS`).
- New `update_platform_connection_external_account` on the protocol and both stores.
- Access-log filter redacts `code`/`state` on both the TikTok and Instagram callback paths.
- Registry: Instagram `connection_available=True`; `publishing_available` stays False.
- Web: `connectInstagram`/`disconnectInstagram`, `useInstagramActions` (updates/invalidates the
  user-scoped Instagram cache entry), `InstagramConnectionSection` in Settings with its own
  progress, errors and callback handling (removes only the `instagram` parameter). TikTok's
  Settings behavior is unchanged.
- Manual takeover: the Autobuild run implemented this, then stopped before validation and
  review because its filename guard flags any `.env.*` change. Here that was the tracked
  `.env.example`, whose credential values are all empty: a false positive. The diff was reviewed
  and finished by hand. Fixed: an empty `FRONTEND_BASE_URL` (as `.env.example` ships it) now
  falls back to the first CORS origin instead of disabling Instagram Connect and sending TikTok's
  callback to a host-relative path.
- Validation:
  - pytest, `DATABASE_URL` unset: **1312 passed, 92 skipped** (from 1142/88; 4 new Postgres tests
    skip).
  - pytest with `DATABASE_URL`, disposable test schema: **1393 passed, 11 skipped**. That
    includes migration `0011` and platform-scoped consumption on Postgres. The skips are
    Supabase Storage tests needing a service-role key.
  - Web: deps OK, lint and `tsc --noEmit` clean, vitest **24 files / 205 tests** (from 22/181),
    and `npm run build` passes with a real `node_modules`.

  Evidence: `docs/evaluations/productization/milestone-4.1-instagram-oauth.md`; ADR-0018
  addendum.

### Milestone 4.1 — Approved Autobuild Plan

- Approved two implementation briefs under `docs/roadmap/milestone-4.1/`: GREEN
  credential-independent Instagram OAuth implementation with mocked Meta
  responses, followed by YELLOW live Meta validation after developer-app and
  tester access become available.
- The GREEN item covers connect/callback/disconnect, shared encrypted
  credential helpers, Instagram token lifecycle and `@username` identity,
  platform-scoped OAuth state, allowlisted return targets, callback-log
  redaction, Settings actions, and automated backend/frontend coverage.
- Reels publishing, worker integration, production deployment, Meta App Review,
  Advanced Access, Business Verification, and live credentials remain outside
  the GREEN implementation.

## 2026-10-05

### Tooling — Project Roadmap And Planner Handoff

- Added `docs/roadmap/README.md` as the project-owned index for approved
  Autobuild briefs, with briefs organized as
  `docs/roadmap/<milestone>/<implementation-brief>.md`.
- Documented the Claude Code/Codex planning flow: discuss and refine, wait for
  explicit approval, write and validate the schema-compatible brief, then stop
  for the human to review and commit the planning files before running
  Autobuild (this project requires a clean Git tree).
- Updated `.autobuild/config.yaml` and `.autobuild/README.md` to match the real
  roadmap structure and show the validate → dry-run → run handoff. No fake
  implementation brief was created.
- `autobuild config .` and `autobuild agents .` pass with the new roadmap
  directory present and no missing-path warning.

### Tooling — Autobuild Configuration Repair

- `.autobuild/config.yaml` now matches the current Autobuild contracts, where
  validation runs sandboxed against a copy of the run's source, with no network
  and no inherited environment.
- Removed the `PYTHONPATH` injection, which the controller rejects as unsafe.
  Its job (testing the run's `src/` rather than the checkout the `.venv`
  editable install names) moved to `pytest.ini`: `pythonpath = src cli
  tools/evaluation`. Local behavior is unchanged.
- `.venv` and `web/node_modules` are mounted read-only from the checkout
  (`validation.runtime_paths`). Commands are argv lists.
- `web-install` (`npm ci`) became the offline `web-deps` (`npm ls --depth=0`).
  Next's native packages exceed the sandbox's per-file write limit, so `npm ci`
  can't run there. `npm ls` still fails on dependency drift.
- `web-test` passes `--configLoader runner`, so vitest loads its config without
  writing into `node_modules`.
- Updated stale phase comments; `.autobuild/README.md` uses the `autobuild`
  command.
- Validation, through Autobuild's sandboxed validation of a clean snapshot: all
  five commands PASS with no network.
  - pytest: 1142 passed, 88 skipped. The skipped tests are the Postgres
    integration tests, which need `DATABASE_URL` from the local `.env`;
    Autobuild validation deliberately can't see it. Locally, 1230 pass.
  - web: dependency check, lint, typecheck, and vitest 22 files / 181 tests.

## 2026-10-04

### Milestone 4.0 — Instagram Integration Foundation

No live Instagram connection or publishing yet. No schema change.

- Platform registry (`publishing/platforms.py`): `tiktok` and `instagram`, with a minimal
  capability model covering only real differences (`media_delivery` push_file/pull_url,
  `requires_finalize_step`, `caption_max_chars`, `connection_available`, `publishing_available`).
  Instagram's availability flags stay False until 4.1 and 4.2. Failure-taxonomy labels come from
  the registry.
- Materializer guard: `platform_posts` rows are created only for platforms with a hosted
  publisher, so listing `instagram` in `CONTENT_CALENDAR_TARGET_PUBLISHING_PLATFORMS` early can't
  create posts nothing publishes.
- Instagram config contract (`config.py`, `publishing/instagram/configuration.py`):
  `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_REDIRECT_URI` (API service only),
  optional `INSTAGRAM_GRAPH_API_VERSION` (v25.0) and `INSTAGRAM_MEDIA_URL_TTL_SECONDS` (3600,
  bounded 300–86,400). Validation names problems without echoing values. Empty optional lines
  fall back to defaults. Verified scopes: `instagram_business_basic`,
  `instagram_business_content_publish`.
- Signed media URLs: `StorageProtocol.create_signed_url`. `SupabaseStorage` uses
  `/object/sign`; `LocalStorage` raises `SignedUrlUnsupportedError`. `storage/signed_urls.py`
  handles lifetime bounds and log redaction. Verified against the real project: an
  unauthenticated range request returns 206; tokenless and public-bucket paths are refused, so
  the bucket stays private.
- `GET /api/platforms/instagram/status`: platform-neutral `PlatformConnectionStatus` from the
  shared tables, owner-scoped, no credential fields, `connect_available` False until 4.1.
  TikTok endpoints are unchanged.
- Web: `PlatformId.INSTAGRAM`, `PlatformConnectionStatus` type, `getInstagramConnection`,
  `useInstagramConnection` (cached per user and platform), and `PlatformConnectionCard`
  (extracted unchanged from the TikTok card, with an accessible group name). Settings shows
  Instagram's real status with a "coming soon" note and no Connect button.
- Tests: `test_platforms.py`, `test_instagram_configuration.py`, `test_signed_urls.py`,
  `test_api_platforms_instagram.py`, the real-bucket signed URL round trip in
  `test_storage_supabase.py`, and Settings Instagram-card and API-call tests. TikTok assertions
  are scoped to the TikTok card.

### Milestone 4.0 — Docs: ADR-0018, Meta Setup Checklist

- `docs/decisions/0018-instagram-integration-architecture.md`:
  - Instagram Login chosen over Facebook Login, with Meta requirements verified 2026-10-04;
  - TikTok-specific assumptions found;
  - shared-table design, signed-URL media delivery;
  - container lifecycle mapped onto the 3.13 checkpoint/UNKNOWN rules;
  - the 4.2 generic changes (media-source publisher input, finalize step,
    `platform_media_id`);
  - native-ready OAuth `return_target`.
- `docs/evaluations/productization/milestone-4.0-instagram-foundation.md`: verification record,
  manual Meta developer-console checklist (now vs before real users), 4.1 scope.
- `PROJECT_STATE.md`, `AGENTS.md` (durable platform rules), `README.md`, `web/README.md`,
  `.env.example`, `docs/architecture/native-ios-readiness.md`.

### Milestone 3.15 Follow-up — 44px Mobile Tap Targets

- New `tap-target` utility (`web/app/globals.css`): on touch screens only (`pointer: coarse`),
  an invisible centered `::after` hit box of at least 44×44 px. Applied to the `Button` and
  `ErrorState` primitives and to every small control in the `/app` screens: header links, Home
  step links, Library tabs and Delete/Confirm/Cancel, cadence day labels, Remove, Add posting time
  and the picker's Add/Cancel, Queue assign and List/Calendar, calendar month arrows, slot-card
  actions and hint links, and caption buttons.
- Native controls (timezone select, time picker, slot picker, file input) get
  `pointer-coarse:min-h-11`. That's the only visible change, and only on touch devices.
- `tap-target-dot` for calendar slot dots: 44×44 px for a single slot per day. With several
  slots, each is 44 px tall and as wide as its own column, so they never overlap.
- Verified (Chromium, touch emulation, 390 px, including confirm/picker/calendar states): every
  control has an effective hit area of at least 44 px except multi-slot calendar dots. No
  horizontal overflow. Screenshots with the hit boxes on and off are pixel-identical. Desktop is
  unchanged. Frontend 176/176, eslint, `tsc --noEmit`, `next build`.

### Autobuild — Validation Commands and Checkpoint Commits

Configuration only. No application, backend or `web/` change.

- `.autobuild/config.yaml` now defines the controller-owned validation that
  harness autobuild 0.2 (`autobuild run`) runs in each run worktree:
  `pytest`, using the shared `.venv` with `PYTHONPATH=${WORKTREE}/src` so the
  worktree's code is tested rather than this checkout's editable install, and,
  only when `web/` files change, `npm ci`, `npm run lint`, `npx tsc --noEmit`
  and `npm test` in `web/`. `next build` is left out because it depends on
  deployment environment variables.
- `git.checkpoint_commits: true`: after validation passes, the controller
  makes one commit on the run's `agent/` branch. It never pushes or merges.
  `limits.implementer_timeout_seconds: 3600`.
- Validation: every command passed against a clean export of `HEAD`
  (pytest 1103 passed / 86 skipped; web install, lint, typecheck, test).
  Confirmed `PYTHONPATH` makes tests import the worktree's `src/`.

## 2026-10-03

### Autobuild — Project Configuration

Configuration only. No application, backend or `web/` change.

- Adopts **autobuild**, the planner → implementer → independent-reviewer
  control plane in the AI engineering harness (`~/.agents/autobuild`). The
  framework, schemas and policy live in the harness. This repo owns only
  `.autobuild/config.yaml` and `.autobuild/runs/`.
- Config: role assignments (planner `codex`, implementer `claude`, reviewer
  `codex`), protected branch `main`, branch prefix `agent/`, paths to
  `PROJECT_STATE.md` / `docs/decisions/` / `docs/roadmap/` (created by the
  planner on the first approved plan), review-cycle and rollover limits.
  Notifications and remote stop are disabled until harness phase 0.6.
- `.autobuild/runs/*` is git-ignored except `.gitkeep`. Run artifacts (agent
  logs, diffs, screenshots) stay local.
- Validation: `~/.agents/autobuild/bin/autobuild config .` OK (one expected
  warning: `docs/roadmap` doesn't exist yet).

### Milestone 3.15 — Client Server-State Cache and Navigation UX

Frontend-only (`web/`); no backend/API change.

- Root causes of flicker and refetching: every page held fetched data in its own state and
  refetched on mount, rendered a spinner until then, and fetched the same resources separately
  (videos ×3, TikTok status ×2, cadence ×2). Cross-view refresh was manual (`refreshKey`,
  "was uploading" ref), refresh errors replaced content, and the session context re-rendered the
  shell on every duplicate Supabase auth event. Details in ADR-0017.
- Server state: TanStack Query v5 (`@tanstack/react-query`). One client lives in the persistent
  `/app` layout (`components/app/AppDataProviders.tsx`, `lib/query/`). Resource hooks are in
  `hooks/` (`useVideos`, `useCadence`, `useQueueSlots`, `useTikTokConnection`). Keys are
  user-scoped and never include the token. Stale times: videos/queue 30 s with 15 s polling
  while anything is publishing; cadence/TikTok 5 min. Refetch on focus and reconnect.
- Mutations (`useSaveCadence`, `useQueueActions`, `useVideoActions`, `useTikTokActions`, upload
  manager) update or invalidate exactly the affected entries. A Queue action now also refreshes
  Library statuses; cadence save refreshes the Queue and updates Home from the cache.
- Rendering: placeholders only on first load. A failed background refresh keeps the data and
  shows Retry. Home now shows load errors instead of silently falling back to quick links.
- UI state: `lib/ui-state.tsx` keeps the Library filter, Queue view/month and an unsaved cadence
  draft in memory across navigation. It's user-scoped and never written to browser storage.
- User isolation: an account switch or sign-out removes the previous user's queries and remounts
  UI-state and upload providers. Tests caught that a full `clear()` discarded the new user's
  first fetch; the provider now removes only the previous user's keys.
- `lib/session.tsx` keeps the same session object for auth events that change nothing.
- Native-safe contracts: `lib/domain/publishing.ts` (publish-status vocabulary, platform ids, no
  framework imports). `lib/api/client.ts`'s `createApiRequest(baseUrl)` lets another client
  supply its own base URL.
- Mobile: Library filter tabs wrap (they overflowed 66 px at 390 px since 3.14).
- Tests: `tests/routes/app-state.test.tsx` (navigation, loading, errors, mutations, UI state,
  user isolation), a session-stability test, and `tests/test-utils.tsx`
  (`renderWithProviders`); existing page tests run inside the real provider stack. Frontend
  176/176, eslint, `tsc --noEmit`, `next build`.
- Browser validation (Playwright, static export, 400 ms fixture API): revisits went from
  ~810 ms with a spinner to ~45 ms with none, and a 13-step walk from 27 requests to 4.

### Milestone 3.15 — Docs: ADR-0017, Native/iOS Readiness, Evaluation

- `docs/decisions/0017-client-server-state-cache.md`: cache architecture, stale/invalidation
  policy, server-state vs UI-state rule, user isolation.
- `docs/architecture/native-ios-readiness.md`: shared backend vs web vs future native (iOS
  first). Auth is already client-neutral. TikTok OAuth's web-only return redirect and the native
  upload adapter are documented Milestone 7 changes, as is a future direct-to-storage upload
  option.
- `docs/evaluations/productization/milestone-3.15-functional-ux-native-prep.md`: before/after
  browser measurements and remaining tap-target issues for the redesign.
- `AGENTS.md`: durable frontend server-state rule. `PROJECT_STATE.md` and `web/README.md`
  updated.

### Milestone 3.14 Follow-up — Library Status: Production Validated, Localhost Mismatch Diagnosed

- **Production validation passed:** after the backend deployed to Railway, known published
  videos render as Published in the Library and appear in the Published filter.
- **Localhost root cause:** `web/.env.local` points localhost at the Railway production API, not a
  local one (confirmed in the served dev bundle). Before the backend deployed, localhost ran the
  new Library code against the old API, which had no `publish_status`. The page first showed
  "Needs attention" (missing field fell through to the unknown-status label), then "Scheduled"
  (the `assigned_slot_id` fallback). Production and localhost now use the same Library code, the
  same Railway API (which returns `publish_status`) and the same database. No Next.js caching was
  involved: Library data is a client-side `fetch` and the API sends no cache headers. Publishing
  status logic is unchanged.
- **Fix:** dev builds now log a console warning when `/api/videos` omits `publish_status`, so the
  fallback can't silently hide a version mismatch. `web/README.md` documents choosing the local
  vs. deployed API and restarting `npm run dev` after changing it.
- Validation: frontend 162/162, eslint, `tsc --noEmit`, `next build`. No backend code changed.

### Milestone 3.14 Final UX Follow-up — Library Publishing Status

- Root cause: the Library derived its tabs and badge from `videos.assigned_slot_id` alone, so a
  published video (which keeps its slot) still showed as Scheduled, and failed or uncertain posts
  were labeled Scheduled too.
- API: `VideoResponse.publish_status` on `GET`/`POST /api/videos`, from the new
  `publish_status.resolve_video_publish_status`: `UNSCHEDULED`, or the Queue's own
  `resolve_slot_publish_status` result for the video's slot. Read-only; no schema change, and no
  scheduling, publishing, retry or reconciliation behavior changed.
- Library: tabs are All / Unscheduled / Scheduled / Published. Scheduled holds Scheduled,
  Publishing, Failed and Needs attention; badges reuse `presentQueueStatus`. Bucketing lives in
  `lib/status.ts`'s `libraryFilterFor`.
- Multi-platform ambiguity and the N+1 listing read are recorded in ADR-0013's new addendum.
- Follow-up fix (same day): localhost showed real published videos as "Needs attention". The
  backend was right: tracing the real Postgres rows (e.g. video 15 → slot 184 → post 28,
  PUBLISHED) through both resolvers and the route serializer returned PUBLISHED. The cause was
  `web/.env.local` pointing localhost at the deployed Railway API, which predates
  `publish_status`. The missing field then fell through `presentQueueStatus` to "Needs attention".
  The Library now reads the field via `libraryPublishStatus`, which falls back to the old
  `assigned_slot_id` reading when an older API omits it. That can also happen briefly in
  production because Netlify and Railway deploy separately. Added regressions shaped like the
  real rows (past schedule, stale `updated_at`) for every state plus a genuinely broken
  video/slot link, and a frontend test for the missing-field case.
- Tests: `tests/test_api_videos_publish_status.py` (each state equals the Queue's status, upload
  response, tenant isolation), resolver unit tests in `tests/test_publish_status.py`, and
  rewritten Library tab tests plus a `libraryFilterFor` test.

### Milestone 3.14 Final UX Follow-up — Mobile Browser Chrome Uses Brand Green

- Root cause: the pickle-green rebrand changed `globals.css` only. The pre-rebrand indigo
  `#4338ca` was still hardcoded as `viewport.themeColor` in `web/app/layout.tsx` (emitted as
  `<meta name="theme-color">`, which iOS Safari and Android Chrome use to tint their chrome) and as
  `theme_color` in `web/public/manifest.webmanifest`.
- Fix: new `web/lib/brand-theme.ts` holds theme color `#2c7a51` (`--color-accent`) and background
  `#f4f9f4` (`--color-background`). The layout reads it; the manifest's `theme_color` and
  `background_color` (previously `#fafafa`) match it. Still metadata only, not a PWA.
- Tests: `web/tests/lib/brand-theme.test.ts` checks the constant against the CSS tokens, the
  layout's `viewport` export and the manifest, and that no `#4338ca` remains. Verified in the
  static export: `out/index.html` and `out/app/index.html` emit `theme-color` `#2c7a51`.

### Milestone 3.14 UX Follow-up — Remove Default 9:00 AM Cadence Time

Frontend-only; no backend/API/cadence-semantics changes.

- Root cause: `WeeklyRhythmEditor` derived a day's checkbox purely from "has at least
  one posting time", so enabling a day had to insert a real `09:00` entry to stay
  checked. Users then had to remove that phantom time after adding the one they wanted.
- Fix: a day is now on if it has times **or** the user just enabled it
  (`emptyEnabledDays`, local UI state never sent to the API). Enabling adds no time.
  `09:00` remains only as the draft picker's starting value, committed only on "Add".
- Unchanged: disabling a day drops all its times; removing a day's last time turns it
  off; duplicate (weekday, time) adds are a no-op; Save sends exactly the visible chips.
- Tests: replaced "enables a weekday and seeds it with a default time" with
  zero-times/no-09:00, draft-not-committed, cancel-adds-nothing, and
  add-one/add-two/remove/save-exact tests. The save and refresh-after-save tests now add a
  time explicitly. Verified that the new tests fail against the previous component.
  `vitest` 150/150, `eslint`, `tsc --noEmit`, `next build` pass.

### Milestone 3.14 Product Cleanup Follow-up — Activation Flow, Cadence UX + Pickle Batch Brand Baseline

Frontend-only pass: no backend/API/schema changes. This is a usability baseline and
brand starting point, not the final UI/UX redesign — a larger professional redesign
informed by real user feedback and usage data is future scope.

**Cadence time-entry interaction fixed**
- Root cause: `WeeklyRhythmEditor` always rendered a `<input type="time">` showing
  the default value (`09:00`) alongside any already-persisted chip — making it
  impossible to tell what was saved vs. still a draft.
- Fix: replaced the always-visible draft input with an explicit `+ Add posting time`
  trigger per day. The time picker only appears after the user deliberately clicks
  the trigger; it is confirmed by a separate "Add" button. Persisted times show only
  as chips. Cancelling dismisses without committing.
- Tests: new "does not show a draft time input until trigger is clicked" and
  "cancels without committing"; "adds a second time" updated to click the trigger
  first.

**Cadence control copy clarified**
- `Active` checkbox → `Posting schedule enabled` with supporting copy:
  "When enabled, Pickle Batch creates future posting slots from this schedule."

**Timezone browser-detected default for new users**
- When a user's saved timezone is `null` (not yet configured), the timezone select
  pre-fills with `Intl.DateTimeFormat().resolvedOptions().timeZone` instead of
  hard-coding "America/New_York". An existing saved timezone always wins.
- The options list includes the current timezone even if it falls outside the curated
  short list (e.g. "Europe/Berlin" is appended dynamically).
- Saved timezone is never silently overwritten.

**Activation/status home (`/app`)**
- Replaced the Milestone 3.5 quick-link grid with a real setup checklist using
  data from existing APIs (TikTok connection, cadence, video list — no new endpoints).
- Four steps: Connect TikTok, Set up posting schedule, Upload videos, Add to Queue.
  Done items show their current detail (account name, timezone, counts); not-done
  items show a direct link to the relevant section.
- All configured: heading changes to "Your posting pipeline is active."
- Graceful degradation: API error or dev-mock session (null accessToken) falls back
  to the original quick-link grid.
- Tests: new `tests/routes/app-home.test.tsx` covering unconfigured, partial, and
  fully-configured states; the `app-routes.test.tsx` home test updated with
  `SessionProvider` wrapper and updated assertion.

**Library filter tabs**
- Added All / Unscheduled / Scheduled filter bar (client-local, no API change).
- Filter logic: `assigned_slot_id !== null` → Scheduled; otherwise Unscheduled.
- Each video card shows a "Scheduled" badge when `assigned_slot_id` is set.
- File size moved to `hidden sm:block` (visible on desktop, hidden on mobile to
  reduce row crowding).
- Empty-state copy for each filter ("All your videos are already in the queue" /
  "Head to Queue to schedule a video").
- Tests: 5 new filter tests in `library.test.tsx`; existing fixtures updated to
  include `assigned_slot_id: null`.

**Pickle Batch brand baseline**
- `globals.css`: replaced indigo accent (`#4338ca`) with pickle green (`#2c7a51`,
  contrast 5.2:1 on white — passes WCAG AA). Updated related tokens:
  `--color-accent-hover`, `--color-accent-soft`, `--color-background` (warm faint-
  green off-white), `--color-border` (green-tinted). Added `--color-brand-secondary`
  (`#1a3d2b`) for deep-green logo/branding use.
- `AppHeader`: "Pickle Batch" logo text uses `text-accent` (pickle green), giving
  immediate brand recognition. All other interactive elements (buttons, active nav
  states, active tab states) automatically updated via the token change.
- Status colors (semantic: success/danger/progress/attention) are unchanged.

**Frontend tests:** 147 passed (was 133 — 14 new). Lint clean. Build clean.
**Backend tests:** 1171 passed. No backend changes in this task.

### Milestone 3.14 follow-up — Captions are optional (fix `CAPTION_MISSING`)

- **Production discovery:** a valid scheduled post was claimed, then FAILED with
  `CAPTION_MISSING`. The pre-publish check required a caption, but TikTok's Direct Post
  `title` is optional.
- **Fix:**
  - `resolve_publish_caption` returns `None` for a missing, empty or whitespace-only
    caption.
  - The `CAPTION_MISSING` check is removed from `_validate_ready_to_publish`.
  - `TikTokPublisher` omits `post_info.title` when there is no caption (no `null`, no
    placeholder) and sends a present caption unchanged, still enforcing `CAPTION_TOO_LONG`.
  - `Publisher.publish(caption: str | None)` documents optional captions for every
    platform.
- **Recovery:** old `CAPTION_MISSING` rows recover with Retry and publish without a caption.
  The taxonomy keeps the code for display.
- **Frontend:** unchanged; clearing a caption was already allowed.
- **Docs:** ADR-0014 addendum (captions optional; future AI/auto captions fill the optional
  value), 3.14 evaluation, AGENTS.md, PROJECT_STATE.md.
- **Tests:**
  - Backend 1171 passed (was 1159). New: no-caption, empty and whitespace publish with
    `title` omitted; a present caption sent unchanged; the length limit still enforced; a
    claimed captionless post submitted; Retry of a `CAPTION_MISSING` row publishes.
  - The two tests that asserted the old caption-required behavior were rewritten to assert
    the new one.
  - Note: `test_media_storage_postgres.py` failed once intermittently in an earlier full
    run (stale object read from the shared Supabase test bucket). That is pre-existing and
    unrelated; it passed 3/3 in isolation and in the final full run.

### Milestone 3.14 follow-up — Build secret hardening, overdue telemetry, confirmed live publish

See `docs/evaluations/productization/milestone-3.14-hosted-e2e-validation.md` ("Live Run",
"Build Secret Hardening", "Overdue Publishing").

- **Confirmed live:**
  - The Railway worker (`dry_run=False`) found two overdue PENDING posts, claimed them,
    submitted them, and received TikTok publish IDs.
  - Reconciliation resolved both as PUBLISHED, and they were verified on the connected
    TikTok account.
  - This is the first real hosted end-to-end publish. Both posts were 58–65 min late,
    because the worker came online after their scheduled times.
- **Build secrets (configuration fix):**
  - Railway's Nixpacks builder turned every service variable into `ARG X` + `ENV X=$X` in
    its generated Dockerfile. Runtime secrets were therefore present during the image build
    and written into the image's ENV config, which is what the `SecretsUsedInArgOrEnv`
    warnings were about. No value was printed and nothing indicates disclosure, so no
    rotation.
  - Both services now build from a repo-root `Dockerfile` that declares no `ARG`, so with
    Railway's Dockerfile builder no service variable enters the build. Railway injects them
    at runtime.
  - The image is `python:3.11-slim-bookworm` with apt `ffmpeg`; `nixpacks.toml` is removed.
  - New `.dockerignore`.
  - The API start command is now `python3 cli/run_api.py`: Railway runs Dockerfile start
    commands without a shell, so `$PORT` can't be expanded there, and `run_api` reads it
    itself.
  - `tests/test_deploy_config.py` (14) guards all of this.
- **Overdue publishing — V1 decision: keep automatic catch-up (provisional, no grace window).**
  - Exact semantics: PENDING + `scheduled_at <= now` (slot timezone), no lateness cutoff,
    no MISSED state, no batch limit, no spacing.
  - Documented risks: a backlog posts back to back, and beyond TikTok's 6 inits per minute
    the remainder end FAILED (`rate_limit_exceeded`, retryable via Retry). No duplicates.
  - The future Post now / Reschedule / Skip UX is documented, not built.
- **Overdue telemetry:**
  - `hosted_due_selection.lateness_seconds` measures lateness in the slot's timezone,
    DST-correct.
  - New `due_backlog` event (`due`, `overdue`, `max_lateness_seconds`) per user per cycle.
  - `post_claimed` gained `claimed_at`, `lateness_seconds` and `previous_failure_code`.
  - Outcome events and `dry_run_would_claim` gained `lateness_seconds`.
  - Logging only; selection is unchanged.
  - `tests/test_hosted_overdue_semantics.py` (18) pins the semantics and the telemetry.
- **Verification:** backend 1159 passed (was 1127). No Docker here, so the image build itself
  is verified on Railway's next deploy.

### Milestone 3.14 follow-up — Show the connected TikTok account

Settings showed only "TikTok: Connected", so during live validation it was impossible to tell
which TikTok account had authorized the app (and so received the published posts).

- **Backend:** `GET /api/platforms/tiktok/status` now asks TikTok's `creator_info` endpoint,
  using the user's own stored credential, which account is connected
  (`publishing/tiktok/creator_identity.py`). This is the same call the publisher already
  makes before every post, under the `video.publish` scope, so no new scope is needed.
  - New response fields: `creator_username`, `creator_nickname`, `creator_avatar_url`.
  - `account_label` is now `@username`, else the nickname.
  - The lookup is best-effort with a 5s timeout. On failure the identity fields are null,
    `connected` is unchanged, and the failure is logged as
    `event=tiktok_creator_info_failed` with a code only.
  - The `open_id` and tokens are still never returned.
  - `TikTokPublisher.query_creator_info` takes an optional `timeout`.
- **Frontend:** Settings shows "Connected as @username" (or the nickname, or just
  "Connected" when unknown), via `lib/tiktokAccount.connectedAccountName`.
- **Tests:**
  - Backend 1127 passed (+8): username, nickname-only, no identity, three failure modes that
    leave the connection ACTIVE, no `open_id`/token leakage, and no lookup when
    disconnected. Existing status tests stub the lookup, so they never reach TikTok.
  - `web/` 133 passed (+3); lint and build clean.

## 2026-10-02

### Milestone 3.14 — Queue refresh fix + Retry UI (live validation in progress)

See `docs/evaluations/productization/milestone-3.14-hosted-e2e-validation.md`.

- **Fix: Queue didn't show slots generated by a cadence save until a page reload.**
  - `QueueBoard` only loaded on mount and month change.
  - The Queue page now bumps a `refreshKey` after a successful save
    (`QueueScheduling.onSaved`), and the board reloads slots and videos, so list and calendar
    both update.
  - Regression tests added (confirmed to fail without the fix).
- **Retry UI:** Queue slots show **Retry** when `can_retry`.
  - Posts that need confirmation (`retry_requires_confirmation`) get an inline "check TikTok
    first" step, and only the explicit confirm sends `confirm_not_published=true`.
  - `lib/api/queue.retrySlotPublication`.
- **Fix:** `lib/api/client.ts` now reads structured `{code, message}` error details (the
  3.13 retry endpoint's 409s). Before, these would have rendered as "[object Object]".
- **Verification:**
  - `web/` 130 tests passed (was 123); lint and build clean.
  - Read-only production checks: API healthy, 3.13 routes and migration `0010` live, and a
    worker `--dry-run --once` against production was clean (`users=1 due=0`).
  - The real publish, recovery, restart and smoke-test steps are pending (they need the
    account owner).

### Milestone 3.13 — Reconciliation + Recovery

Hardens the hosted worker against ambiguous and partial failures without a new state machine.
See ADR-0016 and `docs/evaluations/productization/milestone-3.13-reconciliation-recovery.md`.

- **Submission checkpoint (duplicate-publish fix):**
  - `platform_posts.submission_state`/`submission_started_at` are written before publishing.
  - `TikTokPublisher` hands back its `publish_id` before the upload PUT, and it is persisted
    there. With `FILE_UPLOAD` nothing posts until the upload completes.
  - This closes two duplicate paths: a crash between submission and persistence, and an
    upload that timed out after TikTok had the bytes (previously retried as a fresh
    submission).
  - Errors after the id is persisted go to reconciliation only.
- **Crash recovery** splits stale unsubmitted claims by checkpoint:
  - never submitted → requeue
  - interrupted before an id (no media sent) → bounded retry (`SUBMISSION_INTERRUPTED`)
  - unknowable → new status `UNKNOWN`
- **Reconciliation:**
  - A terminal status-check error (e.g. reauth) now parks the post as `UNKNOWN` (code and
    id kept) instead of marking it FAILED without evidence.
  - Checks are capped at `CONTENT_CALENDAR_STATUS_CHECK_MAX_ATTEMPTS` (150) →
    `UNKNOWN`/`STATUS_UNRESOLVED`.
- **Manual retry API:** `POST /api/queue/slots/{id}/retry` (`scheduling/manual_recovery.py`).
  - Owner only, atomic (CAS).
  - FAILED → PENDING.
  - UNKNOWN with an id → status re-check.
  - UNKNOWN without an id → requires `confirm_not_published`.
  - PENDING/PUBLISHING/PUBLISHED → 409.
  - Queue slots gain `can_retry`/`retry_requires_confirmation`; UNKNOWN shows as
    NEEDS_ATTENTION / `OUTCOME_UNKNOWN`.
- **Token refresh:** per-connection Postgres advisory lock
  (`PostgresContentStore.credential_refresh_lock`).
  - One refresh per expiry; waiters reuse the result.
  - The lock is released on rollback or process death.
  - Bounded wait → retryable `CREDENTIAL_REFRESH_BUSY`.
  - SQLite/CLI behavior unchanged.
  - Multiple worker replicas are now supported.
- **Media metadata:** publish-time ffprobe of hosted uploads is persisted to the video row and
  reused by retries. The write is best-effort, and nothing is transcoded.
- **Silent videos:** allowed on the publish path (`inspect_media(require_audio=False)`).
  TikTok's documented media requirements contain no audio requirement. Local ingestion still
  requires audio.
- **Schema:** Postgres migration `0010`; SQLite additive columns; status value `UNKNOWN`.
- **Logging:** structured recovery/reconciliation/retry/credential events, IDs and codes only.
- **Verification:**
  - Backend 1119 passed, 0 skipped (was 1063), including real-Postgres concurrency tests:
    6 refreshers → 1 refresh; 5 concurrent retries → 1 applied.
  - The lock test was confirmed to fail with the lock disabled.
  - No `web/` change, no deploy, no real TikTok call.

## 2026-09-30

### Milestone 3.12 — Hosted Scheduler + Worker Execution

Hosted scheduled posts now execute automatically through a separate worker process that
reuses the existing local state machine (atomic claim, publish path, retry/backoff, crash
recovery, reconciliation). No new publishing engine and no schema change. See ADR-0015 and
`docs/evaluations/productization/milestone-3.12-hosted-scheduler-worker.md`.

- **Worker:** `cli/run_worker.py` → `scheduling/hosted_worker.py`.
  - Each cycle runs crash recovery → reconciliation → due posts, per hosted user, with that
    user's own TikTok credential.
  - Fresh store per cycle; per-user error isolation; quiet when idle.
  - SIGTERM/SIGINT finish the current cycle.
  - `--dry-run` / `--once`; `CONTENT_CALENDAR_WORKER_POLL_INTERVAL_SECONDS` (default 60).
  - Refuses to start without Postgres, Supabase storage, the encryption key, or ffprobe.
- **Hosted gaps fixed:**
  - Per-user token provider (`publishing/tiktok/hosted_publisher.py`,
    `TikTokPublisher(access_token_provider=)`).
  - Timezone-exact due selection (`scheduling/hosted_due_selection.py`;
    `run_due_posts_once(due_posts=)`).
  - ffprobe inspection of never-inspected hosted uploads at publish time.
  - Storage materialization failures now end in the existing retry/FAILED path instead of a
    stuck PUBLISHING row.
- **Hosted eligibility:** only users with a hosted login (`auth_identities`). The legacy CLI
  identity's rows stay with the CLI worker.
- **Deployment:** `railway.worker.json` (worker service), `nixpacks.toml` (ffmpeg).
- **Observability:** structured `event=...` lines for startup, cycles, claim, publish
  start/success/failure, retry and reconciliation scheduling, and shutdown. IDs and codes only,
  never tokens or raw error text.
- **Verification:**
  - Backend 1063 passed (was 1027), including real-Postgres concurrency tests (4 workers →
    one publish; concurrent startup migration-safe).
  - `run_worker.py --dry-run --once` ran read-only against the real stack.
  - No deploy, no real publish.

### Fix — Concurrent Postgres migration race (production 500s)

**Symptom:** after the 3.10.1/3.11 deploy, API routes returned 500 with `duplicate key value
violates unique constraint "pg_class_relname_nsp_index" ... (video_hashtags_id_seq, 2200)`,
raised from `postgres_migrate.apply_migrations()` while applying `0008_add_video_hashtags.sql`.

**Cause:** not a broken or partially committed migration. Every request constructs a
`PostgresContentStore`, which applies pending migrations with no lock. Several requests raced
to run the same `CREATE TABLE IF NOT EXISTS`, which is not concurrency-safe in Postgres: the
losers fail on the system catalog's unique index. A read-only check of production found it
consistent:
- `0008` and `0009` are both recorded in `schema_migrations`.
- `video_hashtags` exists with its indexes and an identity sequence owned by the table
  (0 rows).
- `platform_posts.failure_code` exists.

No data was lost and no repair was needed. Because DDL is transactional, a half-applied
`0008` can't occur.

**Fix (`persistence/postgres_migrate.py` only):** each migration, and the `schema_migrations`
bootstrap, now runs under a transaction-scoped advisory lock keyed to the current schema, and
re-checks `schema_migrations` after acquiring it. Concurrent appliers queue behind the first
and skip what it applied. Transaction-scoped so it stays correct behind Supabase's
transaction-pooling pooler. Migration files and application behavior are unchanged.

**Verification:** new `tests/test_postgres_migrate.py` (3, real Postgres, throwaway schema):
- a deterministic reproduction of the production interleaving
- 6 concurrent appliers over the real migrations
- re-applying `0008`/`0009` over existing objects preserves `video_hashtags` rows and still
  applies `0009`

The two concurrency tests fail with the same catalog unique violation against the pre-fix
runner. Backend 1027 passed (was 1024).

### Milestone 3.11 — User-Facing Publish States + Errors

The Queue/Calendar now shows each slot as Open, Scheduled, Publishing, Published, Failed or
Needs attention, with a sanitized explanation and an advisory next step. Raw backend text is
never shown, and nothing is retried. See ADR-0013's 3.11 addendum and
`docs/evaluations/productization/milestone-3.11-publish-states-errors.md`.

- **New column `platform_posts.failure_code`** (SQLite on open; Postgres migration `0009`,
  additive). It is now written at every publish/reconciliation failure site from the existing
  `PublishError.reason_code`, new `PublishTikTokError.reason_code` precondition codes, or the
  platform's `fail_reason`. `failure_reason` stays internal. Status, retry and claim behavior
  is unchanged.
- **New `publishing/failure_taxonomy.py`** (+ `publishing/tiktok/failure_codes.py`): code →
  platform-neutral category → fixed copy + action hint. Unknown or pre-3.11 codes →
  `UNKNOWN_ERROR`.
- **New `publishing/publish_status.py`:** the one resolver from slot + platform posts to
  display state. `NEEDS_ATTENTION` covers missed schedules (new
  `config.PUBLISH_OVERDUE_GRACE_MINUTES`), stalled/unconfirmed publishing, contradictory rows,
  and assigned videos with no platform post. `PUBLISHED` now requires a platform post ID.
- **`GET /api/queue/slots`:**
  - `display_status` value set changed: `ASSIGNED` → `SCHEDULED`, plus `NEEDS_ATTENTION`.
  - Added `reason_code`, `message`, `action_hint`, `published_at`, `can_unassign` and
    per-platform `publications`.
- **Frontend:** shared `presentQueueStatus`/`presentActionHint` in `lib/status.ts` (list and
  calendar now use one map). The slot card shows the message, a "Reconnect in Settings" link
  and the publish time. Remove is gated on `can_unassign`. New `attention` badge tone. No
  retry button.
- **Verification:** backend 1024 passed (was 956), including Postgres; frontend 123 passed
  (was 115); lint and build clean. Three `test_api_queue.py` tests and one calendar test were
  updated for the intended rename and new rules (explained in the evaluation record).

### Milestone 3.10.1 — Caption Hashtag Parsing + Structured Metadata

Hashtags are now derived deterministically from the free-form caption and stored as
structured internal metadata. The exact `videos.caption_text` string stays the publishing
source of truth, and TikTok still receives it unchanged via `resolve_publish_caption`.

- **Parser:** new `media/hashtags.py` `extract_hashtags()` — character scanning, no regex or
  LLM. Tag characters are Unicode letters/numbers, `_` and combining marks (`#développement`,
  `#東京`, `#हिंदी`). It ignores a bare `#`, mid-word `#` (`C#`, `page#section`) and all-digit
  tags (`#1`). Chained tags split (`#coding#saas`). Output keeps caption order, exact casing
  and duplicate occurrences (no dedup convention exists).
- **Persistence:** new relational `video_hashtags` table (`video_id`, `position`, `hashtag`),
  following this schema's no-JSON-columns convention. SQLite creates it on store open;
  Postgres uses additive migration
  `postgres_migrations/0008_add_video_hashtags.sql`, auto-applied when a
  `PostgresContentStore` opens.
- **Sync:** new `ContentStoreProtocol.set_video_caption(...)` (both backends) writes
  `caption_text`/`caption_source` and replaces the video's hashtag rows in one transaction.
  Every caption write goes through it: save, generate, regenerate, clear (rows removed),
  and the CLI pipeline's `transcript_auto` step. `delete_video` removes the rows too.
  Provenance behavior is unchanged.
- **API:** caption responses (and `assigned_video.caption` on Queue slots) gain read-only
  `hashtags`. Request bodies and the editor UI are unchanged.
- **Not backfilled:** videos captioned before this change have no hashtag rows until their
  caption is next written.
- **Verification:** backend 956 passed (was 922), including Postgres-backed tests with
  migration 0008; frontend 115 passed; lint and build clean.

### Milestone 3.10 — Caption Generation + Editing

Made each video's publishing caption durable, user-controllable product data that the TikTok
publisher consumes. No schema migration. See
`docs/decisions/0014-canonical-caption-ownership-and-provenance.md` and
`docs/evaluations/productization/milestone-3.10-caption-generation-editing.md`.

- **Source of truth:** the existing video-level `videos.caption_text` stays canonical (keyed
  by `videos.id`, so identical-hash records are captioned independently). New
  `publishing/caption_resolution.resolve_publish_caption(video, platform)` is now the one
  caption lookup in `scheduling/publish_tiktok.py`. It is the seam for a future per-platform
  override on `platform_posts`; behavior is unchanged today.
- **Provenance:** `caption_source` gains `transcript_auto_edited` (generated text later
  edited by the user). The API exposes only `NONE`/`MANUAL`/`GENERATED`/`GENERATED_EDITED`.
- **Generation boundary:** new `media/caption_generation.py` contract
  (transcript/metadata/platform/preferences → text + source + metadata) backed only by the
  existing `transcript_auto` derivation. Hosted uploads have no transcript, so generation is
  unavailable for them (`can_generate: false`). This is an intentional limitation; no
  transcription pipeline was built.
- **Editing rules** (`media/caption_editing.py`): regeneration over existing text requires
  `overwrite=true` (409 otherwise). The caption locks (409) once a platform post is
  `PUBLISHING`/`PUBLISHED` or has a `platform_post_id`. `PENDING` stays editable, and the
  worker reads the saved caption at execution time.
- **API:** `GET`/`PUT /api/videos/{id}/caption` and `POST /api/videos/{id}/caption/generate`
  (authenticated; cross-user access is 404; no transcript exposed). `GET /api/queue/slots`
  now includes `assigned_video.caption`. New `config.CAPTION_TEXT_MAX_CHARS` (10000) is a
  sanity limit on the canonical caption only.
- **Queue UI:** new `components/app/CaptionEditor.tsx` in every assigned `QueueSlotCard`
  (textarea, Save, Generate/Regenerate with an explicit "Replace caption" confirm, read-only
  once locked). `lib/api/captions.ts` added.
- **Verification:** backend 922 passed (was 881), including the Postgres-backed tests;
  frontend 115 passed (was 107); lint and production build clean. No live deployed-stack or
  TikTok check.

## 2026-09-27

### Milestone 3.9 — Queue + Calendar Functionality

Built the first real video-to-slot assignment/unassignment and a functional Queue/Calendar
view on top of Milestone 3.8's cadence + slot generation and the pre-existing
`assign_slot`/FIFO-matching primitives (previously reachable only from the CLI ingestion
pipeline, `media/processing.py`). No schema migration — every column this milestone reads
or writes already existed. See `docs/decisions/0013-queue-calendar-assignment-and-display-status.md`
for the full reasoning behind each decision below.

**Investigation before writing any code found a real gap between the schema and reality**:
`content_slots.status` only ever holds `OPEN` or `ASSIGNED` in this codebase — no code path,
including `scheduling/worker.py`, has ever written `PUBLISHED`/`FAILED` onto a slot (ADR-0005
explicitly deferred a "Publishing-readiness state model"). The real outcome lives on
`platform_posts.status`. **Decided: derive a separate `display_status` at the read boundary**
(`OPEN`/`ASSIGNED`/`PUBLISHING`/`PUBLISHED`/`FAILED`, computed from the assigned video's
`platform_posts` row) rather than starting to write those values onto `content_slots` —
keeps `scheduling/`'s existing, already-validated publish/retry/reconciliation semantics
completely untouched.

**New backend surface:**
- `ContentStoreProtocol.unassign_slot(slot_id)` (both backends) — the atomic reverse of
  `assign_slot`: reopens the slot, clears the video's `assigned_slot_id`, and deletes the
  video's `platform_posts` row *only if it's still `PENDING`* (pure intent, safe to reverse —
  and necessary, since `insert_platform_post_if_missing` never overwrites an existing row, so
  a stale one would silently break a future reassignment). Raises the new
  `PlatformPostInProgressError`, without changing anything, if a real publish attempt already
  exists (`PUBLISHING`/`PUBLISHED`/`FAILED`) — "preserve published/history records safely."
  Narrower than the CLI's own `unassign_video_for_slot` (which resets a video to `CLASSIFIED`
  for the local ingestion pipeline's state machine — not applicable here).
- `scheduling/queue_assignment.py` (new module) — `assign_video_to_slot` (manual: a specific
  slot) and `assign_video_to_next_open_slot` (automatic/FIFO: reuses
  `scheduling.slot_matcher.select_slot_fifo` unchanged, not a second scheduler), both gluing
  `assign_slot` + `platform_post_materializer.materialize_platform_posts_for_assignment`
  together exactly like `media/processing.py`'s local-ingestion pipeline already does. Also
  guards against double-booking: `VideoAlreadyScheduledError` if the video already has an
  `assigned_slot_id` — `assign_slot` itself only checks the slot's own availability, so this
  check was missing entirely before now.
- `api/routes/queue.py` (new): `GET /api/queue/slots?from=&to=` (real content_slots + assigned
  video + `display_status`/`platform_post_status`), `POST /api/queue/slots/{id}/assign`
  (manual, `{video_id}`), `POST /api/queue/assign-next` (automatic/FIFO, `{video_id}`),
  `POST /api/queue/slots/{id}/unassign` ("Remove from schedule"). Every route follows the
  existing ownership convention (404 for "not yours" and "doesn't exist" alike).
- `VideoResponse` (`GET /api/videos`) gained `assigned_slot_id` — the Queue's "Unscheduled
  videos" list needs to know which of the caller's own videos are eligible for assignment.

**Frontend (`/app/queue`):** a new "Queue" section beneath the existing "Posting rhythm"
editor — `components/app/QueueBoard.tsx` (data/orchestration), `QueueList.tsx`/
`QueueCalendarMonth.tsx` (the two view modes — a plain month grid with per-day status dots,
no drag/drop), and `QueueSlotCard.tsx` (one slot's full detail + actions, reused as every
List row and as Calendar mode's selected-slot panel, so assign/remove logic lives in exactly
one place). An "Unscheduled videos" list offers automatic (FIFO) assignment per video; each
`OPEN` slot offers manual assignment via its own video picker; an `ASSIGNED` slot (not yet
published) offers "Remove from schedule" — a `PUBLISHED`/`FAILED`/`PUBLISHING` slot is
read-only, matching the backend's own refusal. `components/app/UpcomingSchedulePreview.tsx`
(Milestone 3.8.1) is deleted — `QueueScheduling.tsx` no longer fetches
`GET /api/cadence/slots` itself, since `QueueBoard` now shows the real, richer picture of
what occupies the cadence's generated capacity. `GET /api/cadence/slots` itself is untouched
and still works, just no longer called from this page.

Tests: backend 881 passed (853 baseline + 28 net new — 3 new `unassign_slot` SQLite tests,
2 new real-Postgres `unassign_slot` tests, 6 new `scheduling/queue_assignment.py` unit tests,
17 new `tests/test_api_queue.py` route tests covering tenant isolation, FIFO earliest-slot
selection, double-assignment refusal, and the full assign→unassign→delete-video regression
proof). Frontend: 107 passed (100 baseline − 2 retired upcoming-slots-preview tests in
`queue-scheduling.test.tsx` [superseded by the new Queue section] + 9 new
`tests/routes/queue-board.test.tsx` tests, plus 1 updated `app-routes.test.tsx` case for the
real empty state); `npm run lint` and `npm run build` both clean.

Deferred to a later milestone, per this milestone's own scope: full calendar/queue editing
polish, drag/drop, manual one-off slot creation, rescheduling, a dedicated published-history
browsing view, and any change to `scheduling/`'s existing FIFO/publish/retry contract.

### Milestone 3.8.1 — Scheduling UX Correction

Frontend-only usability correction on top of Milestone 3.8 — no backend/schema/API
change. Real hosted use of the 3.8 Settings section surfaced two real usability gaps: the
cadence editor only let a user add one `(weekday, time)` pair at a time via two dropdowns,
and the upcoming-slots preview was a raw `scheduled_at` ISO string next to the literal
status word `OPEN`, neither of which a real user could read at a glance.

**Moved scheduling out of Settings and into Queue.** `/app/queue` (previously a
Milestone 3.5 placeholder shell with a hardcoded empty `QueueItem[]`) is now the cadence
home — "Posting rhythm" and "Upcoming schedule" sections, both driven by the unchanged
`GET/PUT /api/cadence` and `GET /api/cadence/slots` contract. Settings' own "Scheduling"
section was removed outright (not reduced to a partial editor) so there is exactly one
place that reads/writes the cadence config, per the brief's "avoid creating two separate
sources of truth."

**New `components/app/WeeklyRhythmEditor.tsx`** replaces the old one-weekday-at-a-time
picker with a full seven-row grid (Monday–Sunday), each day independently toggleable and
each active day holding an arbitrary number of times with individual Remove affordances
and its own "+ Add time" control. A day's on/off state is derived (on iff it has at least
one time) rather than a second stored flag — turning a day on seeds it with one default
time (09:00), turning it off drops all of that day's times from the pending edit. Adding
an exact-duplicate `(weekday, time)` pair is a client-side no-op, since
`posting_cadence_times` has a real `UNIQUE(cadence_id, weekday, posting_time)` constraint
this UI can simply avoid tripping rather than surfacing as a save-time error.

**New `components/app/UpcomingSchedulePreview.tsx`** replaces the raw ISO/`OPEN` list
with locale-formatted entries (e.g. "Mon, Oct 5, 9:00 AM"). `scheduled_at` is a
naive-local string already in the cadence's own timezone (see AGENTS.md's timestamp
convention note); `new Date(...)` on a bare (no `Z`/offset) ISO string parses it as local
wall-clock time in JS, so formatting it back out reformats the same numbers rather than
converting between timezones. `OPEN` (every slot's normal state today — no assignment
endpoint exists yet) is never shown; a real, meaningful status (e.g. `ASSIGNED`) is shown
capitalized when one exists, for 3.9 to build on.

Saving still calls `PUT /api/cadence` once, exactly as 3.8 built it — the atomic
save+reconcile+regenerate behavior is unchanged; this pass only changed how the existing
config is edited and previewed.

`components/app/SchedulingSettings.tsx` (3.8's Settings-page container) is deleted,
superseded by `components/app/QueueScheduling.tsx`, which also fixes a real dev-mock-session
gap the old component didn't handle: with no `accessToken` (no backend to call — the same
case `library/page.tsx` already handles), it now settles immediately to "not configured"
instead of spinning forever.

Tests: frontend 100 passed (97 baseline − 5 retired `tests/routes/settings-scheduling.test.tsx`
+ 8 new `tests/routes/queue-scheduling.test.tsx`, covering: existing cadence loads into the
weekly grid, a weekday can be enabled/disabled, multiple times can be added to one day, an
individual time can be removed, save sends the complete weekly cadence, upcoming slots
render human-readable rather than raw ISO/`OPEN`, and a real non-`OPEN` status is shown
when present); `tests/routes/settings-tiktok.test.tsx`'s now-unnecessary cadence mock was
removed (Settings no longer touches `lib/api/cadence`); `tests/routes/app-routes.test.tsx`'s
`/app/queue` case updated for the new page (real empty state is now "no cadence configured"
under a `SessionProvider`, not the old hardcoded placeholder text). `npm run lint` and
`npm run build` both clean. Backend suite untouched and not re-run for this frontend-only
change beyond confirming no backend files were modified.

Deferred to 3.9, per the brief: the full month/week calendar view, drag/drop, manual
one-off slot creation, video-to-slot assignment, rescheduling, and published-history UI.

### Milestone 3.8 — Scheduling + Cadence Configuration

Added the first hosted, per-user posting-cadence configuration + future slot generation —
scoped deliberately to schema + generation only, per the milestone's own brief; no
queue/calendar editing UI, publishing, or assignment endpoint yet (still future scope).
See `docs/decisions/0012-hosted-cadence-configuration.md` for the full design and the
reasoning behind each of the decisions below.

**Investigation before writing any code found two real gaps the brief didn't
anticipate**, both resolved before building the feature (see the ADR's Decisions 1–2):
the 9 existing `content_slots`/5 `platform_posts`/5 `videos` rows from CLI usage belong to
`user_id=1` (`local@pickle-batch.local`, no web-auth login at all) — **not** the real
hosted account (`user_id=2`) as assumed; decided to leave them separate rather than
merge ownership. `content_slots.scheduled_at` had a *global* `UNIQUE` constraint, not
per-user — two hosted users generating a slot at the same wall-clock timestamp would have
silently collided; widened to `UNIQUE(user_id, scheduled_at)` on both backends (a strict,
non-destructive widening — every existing row already satisfies it).

**New cadence model:** `posting_cadences` (one stable-id-across-edits cadence per user —
timezone, active/inactive) + `posting_cadence_times` (its "weekday, HH:MM" rows), both
backends. `content_slots` gains `timezone` (stamped at generation time, `NULL` for every
legacy row) and `cadence_id` (provenance — `NULL` means manual/legacy, never touched by
cadence-edit reconciliation). New Postgres migration:
`postgres_migrations/0007_hosted_cadence_and_per_user_slot_uniqueness.sql`; SQLite via a
new rebuild function reusing the existing `PRAGMA legacy_alter_table=ON` FK-safety
technique.

**Slot generation:** new `calendar/hosted_cadence.py` (pure, stdlib-only, reuses
`calendar/cadence.py`'s `WEEKDAY_NAMES`/`parse_posting_time`/`ScheduleConfigError` rather
than redefining them — `cadence.py` itself is untouched, still serving the unrelated
CLI/global path). Rolling 28-day horizon
(`config.CADENCE_GENERATION_HORIZON_DAYS`), multiple posting times per weekday, real
per-user IANA timezone. Explicit, tested DST policy: a nonexistent local time
(spring-forward gap) is skipped for that instance, never silently shifted; an ambiguous
local time (fall-back) resolves via Python's default `fold=0`, documented rather than
accidental.

**Cadence-edit reconciliation** (added after plan review caught the gap): editing a
cadence, or setting it inactive, now removes the previous config's future `OPEN`
cadence-generated slots before inserting the new horizon — one atomic store method,
`save_cadence_and_regenerate_slots`, mirroring `assign_slot`'s own "one method, one
transaction" shape. `ASSIGNED`/`PUBLISHED`/`FAILED` slots and any manual/legacy
(`cadence_id IS NULL`) slot are preserved unconditionally, regardless of date.

**API** (`api/routes/cadence.py`, new): `GET /api/cadence`, `PUT /api/cadence`
(save + reconcile + regenerate, one atomic operation), `GET /api/cadence/slots` (upcoming
slots for the Settings preview). `api/app.py`'s CORS `allow_methods` gained `PUT`.

**Frontend:** new "Scheduling" section on the existing Settings page
(`components/app/SchedulingSettings.tsx`, `lib/api/cadence.ts`) — timezone select, active
toggle, add/remove posting times, save, and a simple upcoming-slots preview. No
drag/drop, no per-slot editing, no calendar view (3.9's scope).

Tests: backend 853 passed (824 baseline + 29 net new — `tests/test_hosted_cadence.py` is
new (15, pure generation/DST/validation logic), `tests/test_api_cadence.py` is new (11,
real end-to-end including the per-user-uniqueness proof and reconciliation), one new
Postgres integration test proving the same against real `POSTGRES_TEST_SCHEMA`, two new
SQLite migration tests, one new `test_api_cors.py` test (`PUT`/`DELETE` preflight allowed)
plus one existing CORS test updated (`PUT` is no longer a "disallowed method" example,
`PATCH` is), and five existing `insert_slot_if_missing`-based tests updated to pass a real
`user_id` (matching real `generate_calendar.py` usage) now that uniqueness is per-user —
one of those five gained a sibling test documenting the accepted unscoped/`NULL`
consequence explicitly, so the net test-count change is fully explainable. Frontend: 97
passed (92 baseline + 5 new `tests/routes/settings-scheduling.test.tsx`); `npm run lint`
and `npm run build` both clean.

Not yet applied to real production Postgres, and not yet deployed — migration 0007 exists
in this repo, will be picked up automatically by the existing packaging fix (glob-based,
covers the whole `postgres_migrations/*.sql` directory), and — like 0006 before it — should
be deployed with this code rather than applied ahead of it (no destructive rename this
time, so there is no strict ordering hazard, but the new tables/columns are meaningless to
any code that doesn't yet exist in production).

### Milestone 3.7 — Upload Failure Semantics + Telemetry Accuracy

Fixed three real gaps a live Supabase 413 EntityTooLarge exposed during hosted-upload
testing (two of three re-uploaded files exceeded the project's effective Storage size
cap — see `docs/evaluations/upload-benchmarks/` for the investigation and byte sizes).

**Failed uploads now leave an explicit, structured failure state, never a bare
`DISCOVERED` row.** `media/media_storage.py`'s `create_video_from_upload` now wraps
`storage.put()`: on failure the already-inserted video row is marked
`status="FAILED"`/`failure_reason=<the storage error's reason_code>`/`processed_at=now`,
then the original exception is re-raised so the route's own per-attempt failure handling
is unchanged. This reuses `media/processing.py`'s own established local-ingestion
failure convention rather than inventing a second one. The row is kept, not deleted (real
evidence an upload was attempted). Known residual gap, left alone deliberately to keep
this pass minimal: the corresponding `upload_attempts` row still isn't linked
(`video_id`) back to this now-FAILED row — the two can only be correlated by filename/
timing, same as before this fix.

**`SupabaseStorage.put()` gained a specific `"OBJECT_TOO_LARGE"` reason_code for a 413
response** (`storage/supabase_storage.py`), distinct from the generic `"HTTP_ERROR"` used
for every other 4xx/5xx — lets a failed video's `failure_reason` and its attempt's
`error_code` both say specifically "too large," which a future UI message like "File
exceeds current storage upload limit." would key off.

**A failed `upload_attempts` row now retains its true attempted `file_size_bytes`**
instead of `None`. `api/routes/videos.py`'s `_process_one_upload` measured the size
before calling `create_video_from_upload` but discarded it in the exception branch even
though it was already known — silently breaking any later analysis of file size vs.
failure. The measurement now happens outside the `try` and is passed through on both
success and failure.

**`upload_batches` gained explicit outcome/byte-accounting columns**
(`persistence/postgres_migrations/0006_upload_failure_semantics_and_byte_accounting.sql`,
SQLite via `_migrate_upload_batches_rename_total_bytes`/`_ensure_upload_batches_columns`
in `persistence/content_store.py`): `success_count`/`failure_count` make a batch's
outcome explicit without overloading `status` (which stays a pure lifecycle field —
`IN_PROGRESS` -> `COMPLETED` — never a result field like an invented `PARTIAL_SUCCESS`).
`total_bytes` — which silently only ever summed *successful* files' sizes, a bug, not
documented intent, misleading for any batch containing a failure — is renamed to
`attempted_bytes` (every file's measured size, success or fail), with a new
`successful_bytes` column carrying the previously-intended successful-only meaning
explicitly. **Deployment-ordering note:** unlike migrations 0004/0005 (purely additive),
0006 renames an existing column — it must be applied at the same time as or after this
code is deployed, never ahead of it, or the currently-running old code's
`_record_batch_completion` (which still writes to `total_bytes`) will start failing on
every upload the moment the rename lands.

**Sequential per-file upload processing is unchanged** — still one file at a time, no
concurrency introduced. This remains a deliberate baseline for the upload-benchmark
comparison; concurrency stays a future benchmark question, not part of this fix (see
`api/routes/videos.py`'s own comment at the batch-completion call site).

No architecture change, no compression/transcoding, no direct-to-storage — exactly as
scoped. Raising the Supabase bucket's `file_size_limit` (currently unset, falling back to
the project's default) remains a separate, not-yet-made configuration decision.

Tests: backend 824 passed (816 baseline + 8 net new — `tests/test_media_storage.py`
gained 2 (storage failure marks the row FAILED with a reason_code, and with a generic
fallback reason when the exception carries none); `tests/test_storage_supabase_error_codes.py`
is new (3, unconditional/network-free, monkeypatched HTTP responses) proving the 413 ->
`OBJECT_TOO_LARGE` mapping specifically, plus the unchanged 500 -> `HTTP_ERROR` and 2xx ->
success cases; `tests/test_content_store.py` gained 2 (the SQLite rename migration against
a legacy database, and a fresh database getting the new columns directly); `tests/test_api_videos.py`
gained 1 real end-to-end test (a simulated 413 via a size-capped storage double,
reproducing the real incident) and had 2 existing tests extended with
success_count/failure_count/attempted_bytes/successful_bytes assertions;
`tests/test_postgres_content_store.py` had 1 existing test extended and genuinely
exercised migration 0006 against real Postgres (`POSTGRES_TEST_SCHEMA`, not the real
`public` schema). No frontend changes — `web/` untouched, so no frontend verification was
run.

Not yet applied to real production Postgres, and not yet deployed — migration 0006 exists
in this repo (and will be picked up automatically by the same packaging fix from the
previous entry below, since that fix covers the whole `postgres_migrations/*.sql` glob,
not a hardcoded file list) but requires the deployment-ordering care noted above.

## 2026-09-26

### Documentation — Split Upload Benchmark Data Into Its Own Folder

Moved the upload-comparison raw data (methodology, Iteration 1, Iteration 2, comparison
table) out of `docs/evaluations/productization/milestone-3.7-upload-benchmark-snapshot.md`
into a new dedicated folder, `docs/evaluations/upload-benchmarks/` (`README.md`,
`iteration-1-original-upload.md`, `iteration-2-reupload.md`, `comparison.md`) — still
inside the existing `docs/evaluations/` tree, not a new parallel top-level location.
Reason: milestone evaluation docs are meant to stay concise status/decision records; this
benchmark's raw per-iteration data (captured today — see the packaging/CORS fixes above
for why production was touched) doesn't belong growing inside one. The old file is kept
in place as a short forward-pointing note per this repo's own "don't delete relocated
evaluation evidence" convention (see Milestone 3.0's own doc for the precedent) rather
than deleted. Updated `AGENTS.md`'s Milestone 3.7 bullet and
`milestone-3.7-batch-upload-readiness.md`'s existing forward-pointing note to point at the
new location; no other file's content changed. Documentation-only — no code, tests, or
production data touched.

### Production Fix — Postgres Migration Packaging

Fixed a second production bug found while diagnosing the CORS fix below: once DELETE
requests reached the handler, they 500'd because `upload_batches`/`upload_attempts` don't
exist in production Postgres, and (confirmed on inspection) `videos.file_hash` still has
its old `UNIQUE` constraint too — migrations `0004_add_upload_telemetry.sql` and
`0005_drop_videos_file_hash_uniqueness.sql` had never reached the real `public` schema.

Root cause: `railway.json`'s build (`pip install -r requirements.txt && pip install .` —
a real, non-editable build, unlike the `pip install -e .` used for local dev) silently
excluded `persistence/postgres_migrations/*.sql` from the installed package entirely.
`postgres_migrations/` has no `__init__.py` (it's deliberately just numbered `.sql` files,
never a Python subpackage — see `postgres_migrate.py`'s own docstring), and
`pyproject.toml` had no packaging config beyond `[tool.setuptools.packages.find]`, which
only discovers real packages. Reproduced directly: building this repo in a clean throwaway
venv with Railway's exact install command showed the entire `postgres_migrations/`
directory absent from the installed `content_automation` package — meaning
`postgres_migrate.MIGRATIONS_DIR.glob("*.sql")` (called automatically from
`PostgresContentStore.__init__` on every request, per ADR-0008's "self-deploying
migrations" design) found nothing at all on Railway, silently, no error, for every
migration, not just 0004/0005. Migrations 0001–0003 only ever reached real production
because a developer ran a Postgres-backed CLI script (`cli/migrate_sqlite_to_postgres.py`,
`cli/link_bootstrap_user.py`) locally against the real `DATABASE_URL`, from a full source
checkout where the directory genuinely exists — not through the deployed Railway app.
Confirmed via read-only inspection of production's own `schema_migrations` table (only
0001–0003 recorded) and `pg_constraint`/`to_regclass` checks (`upload_batches`/
`upload_attempts` absent, `videos_file_hash_key` UNIQUE constraint still present).

Fix: added `[tool.setuptools.package-data]` to `pyproject.toml` —
`"content_automation.persistence" = ["postgres_migrations/*.sql"]`. Minimum correct fix:
no `__init__.py` added (the directory is intentionally not a subpackage), no change to
`postgres_migrate.py`'s discovery logic or migration semantics, no change to
`railway.json`. Verified by reproducing Railway's exact `pip install -r requirements.txt
&& pip install .` in a fresh throwaway venv: all five `.sql` files, including 0004 and
0005, are now present in the installed package, and importing
`content_automation.persistence.postgres_migrate` from that installed copy and globbing
`MIGRATIONS_DIR` finds all five — the exact runtime path Railway's own app uses. This
means Railway's existing automatic-on-connect mechanism will now genuinely self-apply
0004/0005 (and any future migration) the next time the deployed app opens a
`PostgresContentStore` after this fix ships — no separate deploy-time migration step
needed, matching ADR-0008's original design intent.

Added `tests/test_packaging.py`: asserts `pyproject.toml` declares the package-data entry
for `content_automation.persistence`, and asserts every currently-committed migration
file sits directly under `postgres_migrations/` (not a nested subdirectory the declared
glob pattern wouldn't cover) — a fast, no-network, no-build config-content guard against
this exact class of silent regression recurring; the real build-based proof is the manual
clean-venv reproduction above, not part of the automated suite (this project's `.venv`
doesn't otherwise depend on `wheel`/`build`).

Tests: backend 816 passed (814 baseline + 2 net new, both in `tests/test_packaging.py`).
**Not yet deployed, and the two pending migrations have not yet been applied to
production** — applying them requires opening a `PostgresContentStore` against the real
`DATABASE_URL` (the same mechanism `cli/migrate_sqlite_to_postgres.py` uses), which is a
production-mutating action requiring explicit user action to run.

### Production Fix — CORS DELETE Preflight

Fixed a production bug reported as "DELETE /api/videos/{id} fails from
https://picklebatch.netlify.app with a CORS error." Investigation (both a local
`TestClient` repro mirroring the browser's exact preflight and a live `OPTIONS` request
against the deployed Railway API) isolated the failure to a single layer:
`CORSMiddleware`'s `allow_methods` in `api/app.py` was `["GET", "POST"]` and had never
been updated when `DELETE /api/videos/{id}` was added (the Milestone 3.7 Delete Video
follow-up, above) — Starlette's `CORSMiddleware` rejects any preflight whose
`Access-Control-Request-Method` isn't in that list with a 400 `"Disallowed CORS
method"`, entirely inside the middleware, before the request reaches routing or the
`get_current_user` auth dependency. The frontend origin allowlist, its parsing, and the
auth layer were all confirmed correct and were not the cause. Fix: added `"DELETE"` to
`allow_methods`; no other methods added (no route needs `PUT`/`PATCH` yet).

Added `tests/test_api_cors.py`: asserts `allow_methods` includes `DELETE`/`GET`/`POST`
directly on the middleware config, exercises a real DELETE preflight end-to-end via
`TestClient` (200, `DELETE` present in `Access-Control-Allow-Methods`), confirms GET/POST
preflights are unaffected, and confirms a still-disallowed method (`PUT`) continues to
fail closed. Tests read the allowed origin from `config.API_CORS_ALLOWED_ORIGINS` rather
than hardcoding the production origin literal, since that allowlist is environment-
specific configuration (this repo's local `.env` and Railway's real dashboard value
differ) — the bug was in method matching, not origin matching, so the fix is verified
against whichever origin an environment actually has configured.

Tests: backend 814 passed (810 baseline + 4 net new, all in `test_api_cors.py`). Before
this fix, a live `OPTIONS /api/videos/{id}` preflight against the deployed Railway API
with `Origin: https://picklebatch.netlify.app` and `Access-Control-Request-Method:
DELETE` was confirmed returning 400 with `Access-Control-Allow-Methods: GET, POST`. Not
yet deployed as of this entry — see `PROJECT_STATE.md`/this repo's own commit history for
whether the post-deploy live re-check has been recorded.

### Milestone 3.7 — Re-upload Architecture + Controlled Evaluation Snapshot

**`videos.file_hash` is no longer globally unique.** `videos.id` is a record's real
identity; `file_hash` is a reusable content fingerprint, not a uniqueness constraint — a
creator intentionally re-uploading the exact same bytes later (new caption, new
schedule, new campaign, fresh performance history) now gets a distinct video record
every time, same user or a different one, instead of being silently handed back the
original row or rejected outright. Dropped the old `UNIQUE(file_hash)` constraint on
both backends, replacing it with a plain index so hash-based lookups/future duplicate-
detection queries stay fast: SQLite via a new structural migration
(`persistence/content_store.py`'s `_migrate_videos_drop_file_hash_uniqueness`, the same
rebuild shape as this codebase's other in-place-unalterable-constraint migrations, run
automatically the first time an existing database is opened), Postgres via
`postgres_migrations/0005_drop_videos_file_hash_uniqueness.sql`. Removed
`DuplicateVideoContentError` entirely — `create_video_from_upload` no longer looks
`file_hash` up before inserting; every upload always creates a new row. Also fixed a
related correctness issue this change exposed: SQLite's `insert_video` re-resolved its
own just-inserted row by `file_hash` rather than by `id` — with duplicate hashes now
possible, that could return a *different* pre-existing row instead of the one just
created; it now looks itself up by `id`. Local CLI ingestion
(`media/processing.py`) is unaffected — it only ever looks a hash up to resume
processing a physically-rediscovered file, which stays correct by construction
regardless of the schema-level constraint. Full record, including one narrower pre-
existing edge case this slightly widens (local/hosted cross-tenant hash collisions) and
explicitly does not fix, in ADR-0009's new 2026-09-26 addendum.

Added a small, temporary evaluation workflow — `docs/evaluations/productization/milestone-3.7-upload-benchmark-snapshot.md`
— to capture and compare real hosted-upload performance/behavior before and after this
change, for the three real videos already uploaded through the deployed product. A
documented, copy-pasteable read-only SQL workflow, not a script or a new evaluation
skill/agent (this repository's own "don't over-automate a one-off" judgment — see that
doc's own "Why no script/skill" section). The actual Iteration 1 baseline capture is
**not yet done**: this agent session cannot read the real hosted Postgres database
directly (the sandbox's own auto-mode classifier denies direct production reads,
confirmed again in this session) — real values still need to be captured by running the
documented queries against the real database and recorded in that doc before the
existing three test videos are deleted.

Tests: backend 810 passed (803 baseline + 7 net new — `test_content_store.py` gained 4
covering duplicate-hash insertion, independent divergence of duplicate-hash rows, the
SQLite migration itself, and a fresh-database no-op guard; `test_postgres_content_store.py`
gained 1 proving the same against real Postgres — the Postgres migration genuinely
applied and was exercised for real in this environment, not merely collected-and-skipped,
per the same `DATABASE_URL`-via-`.env` behavior noted in the prior follow-up entry below;
`test_media_storage.py` gained 1 proving deleting one duplicate-hash video leaves the
other's row and storage object completely untouched; `test_api_videos.py` gained 1
proving upload telemetry links each of two duplicate-hash uploads in the same batch to
its own distinct `video_id`, never conflating the two).
`tests/test_media_storage.py` and `tests/test_api_videos.py` had their now-superseded
idempotent-reupload/duplicate-rejection tests replaced with tests proving the new
distinct-record behavior instead (net test count in those two files unchanged — see the
evaluation doc for the exact list). No frontend changes in this pass.

### Milestone 3.7 — Delete Video

Added the Library's first destructive action: `DELETE /api/videos/{id}`, backed by a new
`media.media_storage.delete_video(store, storage, video_id, user_id)` domain function (both
persistence backends implement the two new `ContentStoreProtocol` methods it needs —
`list_platform_posts_for_video`/`delete_video`). Ownership is checked first (404 either way for
"not yours" and "doesn't exist" — same cross-tenant silence convention as every other route).
Before touching anything, refuses (409, safe message) if the video is still assigned to a
content_slot or has any `platform_posts` row (any platform, any status) — "fail safely, tell the
caller to cancel/remove those entries first" rather than cascading the delete into
scheduling/publishing state; no queue/calendar editing was added to make that possible in this
pass. When safe: deletes the stored object via the same `StorageProtocol` the upload path uses
(skipped entirely for a legacy local-only video with no `storage_key` — its local file, per
ADR-0009's "Retention", stays untouched and out of scope), then deletes the `videos` row.
`upload_attempts.video_id` is nulled out (not deleted) for any attempt that pointed at the deleted
video, preserving that row's success/failure/timing telemetry for analytics while dropping the now-
dangling reference; `upload_batches` is untouched (never about a single video). Deleting a video
frees its `file_hash`, so the exact same file can be re-uploaded afterward and is treated as brand
new — verified directly in a new test (`test_delete_video_frees_the_file_hash_for_re_upload` /
`test_deleting_a_video_lets_the_exact_same_file_be_uploaded_again`).

`web/app/app/library/page.tsx` gained a per-row Delete action with an inline two-click confirm
(no modal primitive exists yet, and this was scoped as a small delete action, not a new UI
primitive) — `lib/api/videos.ts`'s new `deleteVideo()`, and `lib/api/client.ts`'s `apiRequest` now
resolves to `undefined` for a `204 No Content` response instead of trying (and failing) to parse
an empty body as JSON. A 409 refusal from the backend surfaces as plain text on the page, exactly
as the backend phrased it, and the video stays in the list since nothing was actually deleted.

No queue/calendar editing UI was added — that remains explicitly out of scope for this pass; a
video with a schedule/queue reference can currently only be un-blocked from the backend/CLI side.

Verification: backend 803 passed (`.venv/bin/python3 -m pytest`, `DATABASE_URL` unset locally to
skip the two Postgres-only additions from running against a real database in that mode — they also
ran and passed for real against `POSTGRES_TEST_SCHEMA` in this environment), frontend 92 passed
(`npm run test`), `npm run lint` and `npm run build` both clean.

### Milestone 3.7 — Data Quality + Upload Instrumentation Follow-up

Triggered by the first real hosted uploads and a direct DB inspection, which found rows with
`storage_provider="local"` even though their `storage_key` looked Supabase-shaped. Investigated
and confirmed this is **not a code defect**: every write site stamps `storage_provider` from the
actually-injected `StorageProtocol.provider_name`, never a literal string — proven by a new stub-
backend test. The real cause is `config.STORAGE_BACKEND` defaulting to `"local"` when unset (a
pre-existing, deliberate default documented in ADR-0009's own "Consequences" section), combined
with `build_storage()` never having been called from any live entry point before this milestone's
upload endpoint — if Railway's dashboard was never given `STORAGE_BACKEND=supabase` explicitly,
the bytes are genuinely sitting in that process's local filesystem, not Supabase. A `storage_key`
alone never proves which backend holds the bytes, since `build_storage_key()` is deliberately
backend-agnostic. Fixed the *visibility* of this class of gap (an operational Railway env var, not
a code bug) by extending `api/app.py`'s existing startup diagnostic to also log `STORAGE_BACKEND`.
Full root-cause record in ADR-0009's 2026-09-26 addendum.

Confirmed `canonical_media_path` staying `NULL` for hosted uploads is intentional (it already was,
from the prior pass) — formalized in the same ADR-0009 addendum: `storage_key`/`storage_provider`
is the one canonical hosted-media identifier; a hosted upload has no local "lineage" to record.

Added minimal, event/attempt-level upload-performance instrumentation: `upload_batches` (one row
per `POST /api/videos` request) and `upload_attempts` (one row per file, `video_id` nullable for a
failed attempt) on both persistence backends, plus a new Postgres migration
(`0004_add_upload_telemetry.sql`). Wired into `api/routes/videos.py`: every file's extension
rejection, storage/DB success, or duplicate-content rejection now gets its own timed attempt row
(`error_code`: `UNSUPPORTED_FILE_TYPE`/`DUPLICATE_CONTENT`/other). Every `duration_ms` is a
server-side `time.monotonic()` delta spanning this process's own hash+storage+DB work — explicitly
documented as *not* network latency, since this server never observes the client's actual upload
transfer time. Throughput is deliberately not stored as its own column — derive `total_bytes /
total_duration_ms` at query time instead. Documented (not built) exactly which media-metadata
fields (`container`/`video_codec`/`audio_codec`/`width`/`height`/`fps`/`duration_seconds`) a future
background job could populate via the existing `media.inspection.inspect_media`, and why that job
belongs on an interval, never synchronously in the upload request.

Implemented navigation-safe uploads: a new `web/lib/uploads.tsx` (`UploadProvider`/
`useUploadManager()`) now owns in-progress/result state, mounted in `app/app/layout.tsx` above the
per-route page content — exactly like `SessionProvider` — so it survives the user navigating to
Queue/Settings and back mid-upload. The underlying `fetch()` was never actually cancelled by
navigation (not tied to React's component tree); the real gap was that the *feedback* lived only
in `VideoUploadForm`'s local state. `LibraryPage` now refreshes its list whenever the shared
`uploading` flag transitions to false while it's mounted, and always does a fresh fetch on mount
regardless — so a batch finishing while the user is elsewhere is picked up correctly on return.
Browser-close/resumable-upload support remains explicitly out of scope.

Tests: backend 783 passed (777 baseline + 6 net new — batch/attempt persistence, success/failure
timing, tenant isolation of telemetry, the stub-backend `storage_provider` proof; a Postgres parity
test was added but not run in this sandboxed environment, same production-DB-access restriction as
before). Frontend 86 passed (81 baseline + 5 new), lint clean, build clean. One existing frontend
test's assertion was deliberately updated (a failed upload now also triggers a list refresh, not
just a successful one — a documented design simplification, not a regression); no test was
weakened or removed.

Files changed: `src/content_automation/persistence/{content_store,postgres_content_store,protocol}.py`,
new `postgres_migrations/0004_add_upload_telemetry.sql`, `media/media_storage.py`,
`api/routes/videos.py`, `api/app.py`, `docs/decisions/0009-object-storage-media-lifecycle.md`,
`tests/test_api_videos.py`, `tests/test_media_storage.py`, `tests/test_postgres_content_store.py`,
new `web/lib/uploads.tsx`, `web/components/forms/VideoUploadForm.tsx`, `web/app/app/library/page.tsx`,
`web/app/app/layout.tsx`, new `web/tests/lib/uploads.test.tsx`, `web/tests/routes/library.test.tsx`,
`web/tests/routes/app-routes.test.tsx`.

Full detail, including the exact live-validation steps for the next real batch (Railway
`STORAGE_BACKEND` fix + redeploy, then checking the real Supabase bucket and the new telemetry
tables directly), in `docs/evaluations/productization/milestone-3.7-batch-upload-readiness.md`'s
2026-09-26 addendum.

Milestone 3.7 remains **IN PROGRESS** — this follow-up's own instrumentation and corrected
metadata are not yet validated against a real hosted upload themselves.

### Milestone 3.7 — Batch Upload UX: Readiness Check + Minimum Implementation

Inspection first (per the milestone brief): the storage abstraction (`StorageProtocol`/`LocalStorage`/`SupabaseStorage`, Milestone 3.4) existed and was tested but had never been called from any live entry point; `media.media_storage.upload_canonical_media()` only migrates an *existing* locally-ingested video to storage, with no path for creating a brand-new video row directly from freshly-uploaded bytes; no `/api/videos*` routes existed at all; `web/app/app/library/page.tsx` was a static "coming soon" shell with zero data fetching; `web/lib/api/client.ts` only supported JSON bodies, not multipart file uploads; and `videos.file_hash` is globally `UNIQUE` (not scoped per user) on both backends — a real pre-existing constraint a multi-tenant upload feature has to handle, not a bug to fix by loosening the schema.

Implemented only the minimum to close those gaps. Backend: `media.media_storage.create_video_from_upload()` (new) — the hosted-upload counterpart to `upload_canonical_media()` — creates an owned `videos` row directly from received bytes, uploads it via the existing `StorageProtocol`, and is idempotent per (user, exact content); raises the new `DuplicateVideoContentError` (never names the other account) when the same content already belongs to a different user. `persistence.{content_store,postgres_content_store}.list_videos_for_user()` (new, added to `ContentStoreProtocol` alongside `insert_video`). `api/routes/videos.py` (new): `POST /api/videos` (one multipart batch request, one `VideoUploadResult` per file — a bad file never aborts the rest of the batch) and `GET /api/videos` (the caller's own videos only). Deliberately a plain `def` route, not `async def` — mixing `UploadFile`'s async `.read()` with a separately-dispatched `run_in_threadpool` call for the DB work risked handing the same SQLite connection (only safe from one thread at a time) to two different worker threads; caught during implementation and avoided by reading each upload via its underlying sync file object instead, matching every other route in this API. `api/schemas/videos.py` deliberately excludes `storage_key`/`storage_provider`/internal paths from the response — same "don't surface an opaque internal identifier" lesson as the 2026-09-25 TikTok `open_id` finding. Added `python-multipart` to `requirements.txt` (required by FastAPI to parse any file upload at all — was missing entirely).

Frontend: `lib/api/client.ts`'s `apiRequest()` now passes a `FormData` body straight through (never `JSON.stringify`'d, no manual `Content-Type` so the browser sets its own multipart boundary) — every existing JSON caller is unaffected. `lib/api/videos.ts` (new), `components/forms/VideoUploadForm.tsx` (new — select is a distinct step from upload, so a wrong pick can be cleared first; shows a per-file success/failure result list). `app/app/library/page.tsx` rewritten from the static shell to a real client component: loads the user's own videos, shows the upload form, refreshes the list after a batch completes; no `accessToken` (dev-mock-session fallback) settles to the same "no videos yet" state a genuinely empty account would show, not a perpetual spinner.

Tests: backend 777 passed (759 baseline + 18 new — `tests/test_api_videos.py`, `tests/test_media_storage.py`, `tests/test_content_store.py`; a Postgres-parity test for `list_videos_for_user` was added to `tests/test_postgres_content_store.py` but not run in this environment, which blocks any `DATABASE_URL`-set pytest invocation as a production-access safeguard — will run wherever that's configured normally). Frontend: 81 passed (67 baseline + 14 new), lint clean, build clean. Zero existing tests modified or weakened.

Full detail, including the exact manual steps to run with real video files and what's still blocking live validation (`python-multipart` reaching the next Railway deploy; no real upload has been driven through real Supabase Storage/production Postgres from this environment yet), in `docs/evaluations/productization/milestone-3.7-batch-upload-readiness.md`.

Milestone 3.7: readiness/implementation work **COMPLETE**; the milestone itself remains **IN PROGRESS** pending a real user upload against the real deployed stack — not marked COMPLETE on the assumption that will pass, matching Milestone 3.6's own established pattern.

### Milestone 3.6 — Real Authentication + TikTok Account Connection — COMPLETE

Closed the one gap the 2026-09-21 addendum and the 2026-09-25 security review both left open: a real TikTok OAuth connect→callback cycle completing end to end against the live deployed stack (Railway backend, Netlify frontend). The interactive Google sign-in and TikTok authorization steps require a real browser session with real account credentials — outside what this agent can perform itself — so the user ran the live walkthrough directly and confirmed: authenticated `GET /api/platforms/tiktok/status` → `200`; `POST /api/platforms/tiktok/disconnect` → `200`; reconnect (`POST /api/platforms/tiktok/connect`) started successfully; the real TikTok redirect to `GET /api/platforms/tiktok/callback` → `302`; Railway's captured logs for that callback show `code`/`state` redacted (confirming the 2026-09-25 `api/app.py` log filter is active in the real deployed process, not just under test); and the post-callback status check → `200` again. This also exercises `oauth_states`/`platform_credentials` against real Postgres for the first time, closing that item from the milestone's own Deferred list.

Before this confirmation, this session independently verified the deployed stack's non-interactive surface directly from this environment (no credentials involved): `GET /api/health` → `200`; a CORS preflight from the real frontend origin (`https://picklebatch.netlify.app`) returns a matching `access-control-allow-origin`; `/api/me` and `/api/platforms/tiktok/status` both reject an unauthenticated request with a clean `401` and no leaked detail; a callback hit with no `code`/`state` redirects safely (`302` → `/app/settings?tiktok=invalid_state`) rather than erroring. A direct read-only query against the live production Postgres tables, attempted to independently corroborate credential persistence/encryption without ever printing a value, was blocked by this environment's own production-access safeguard and not pursued further — the credential-persistence/disconnect/reconnect result rests on the user's own direct confirmation of the live walkthrough, not this agent's independent inspection of production data.

Regression suite re-run clean immediately before this close-out, with no route/application code changed since the 2026-09-25 security review: backend 759 passed (run with `DATABASE_URL` unset so the ~20 Postgres-integration tests skip rather than touching production); frontend 70 passed, lint clean, build clean.

Updated `AGENTS.md` (Milestone 3.6 and 3.6.1 status lines, "Next up" now points to Milestone 3.7) and `docs/evaluations/productization/milestone-3.6-auth-tiktok-connection.md` (2026-09-26 addendum recording the live result and final COMPLETE verdict) accordingly. No application code changed in this pass — documentation/status close-out only.

Milestone 3.6: **COMPLETE**. Milestone 3.6.1 (production deployment): confirmed live and validated as part of the same result.

## 2026-09-25

### Security — Milestone 3.6 Final Security Review: TikTok OAuth Data Exposure

Reviewed the now-working hosted TikTok OAuth flow (Pickle Batch → TikTok authorization → Railway callback → token exchange → encrypted storage → Connected) for two concerns raised after real Railway logs showed `GET /api/platforms/tiktok/callback?code=<value>&state=<value>` lines, and after the Connected UI was observed displaying a long opaque identifier.

**Callback query-string logging.** Grepped every TikTok/OAuth/API code path (`api/routes/platforms_tiktok.py`, `publishing/tiktok/auth.py`, `publishing/tiktok/credential_store.py`, `persistence/content_store.py`) for `print`/`logging`/`logger` calls touching `code`, `state`, `access_token`, or `refresh_token`: none exist — this application never logs the authorization code, state, or any token intentionally. Traced the Railway log lines to their real source: `cli/run_api.py` calls `uvicorn.run(...)` with no `access_log=False`/custom `log_config`, so uvicorn's default `uvicorn.access` logger is active and its `AccessFormatter.formatMessage()` (verified directly against the installed uvicorn 0.53.0) always logs the request's full path *including its raw query string* for every request — not something Railway's platform layer adds on its own (Railway only captures whatever reaches stdout), so it was within this app's control. Fixed with the smallest workable app-layer change: `api/app.py` now attaches a `logging.Filter` to the `uvicorn.access` logger that redacts only the `code`/`state` query-parameter values, only on `/api/platforms/tiktok/callback`, leaving every other route's access logging (and every other logger) completely untouched; it fails open (leaves the record alone) rather than raising if uvicorn's internal record shape ever changes, so a future uvicorn upgrade can't turn a logging filter into a request-handling failure.

Also found, while tracing every place a token-shaped value could end up in a string: `publishing/tiktok/auth.py`'s `_post_token_request()` embedded TikTok's raw response payload (`{payload!r}`) into the `TikTokAuthError` message whenever TikTok's token endpoint returned an error — in every real case that payload is TikTok's `{"error": ..., "error_description": ...}` rejection shape with no token fields, and the exception is caught and discarded (never logged) by `platforms_tiktok.py`'s callback handler today, so this was not an active leak. Sanitized anyway (redact `access_token`/`refresh_token` keys before formatting) as defense-in-depth, since "never log access_token/refresh_token" has to hold even for a message TikTok itself controls the shape of, not just for this codebase's own logging calls.

**Opaque identifier in the Connected UI.** Traced the long identifier shown in `web/app/app/settings/page.tsx` next to "tiktok": it was `TikTokConnectionStatus.account_label`, populated in `api/routes/platforms_tiktok.py` from `platform_connections.external_account_id` — which is TikTok's `open_id`, captured from the token response at callback time (`token.get("open_id")`). `open_id` is an opaque, per-app platform identifier (not a handle/username, not something the current OAuth scope exposes as human-readable) — not a credential/secret, but not useful to a user either. `external_account_id` stays in persistence (`platform_connections`) unchanged, since it is legitimately needed for account-association record-keeping; only the API's `account_label` field stopped being populated with it (`get_tiktok_status`/`disconnect_tiktok` in `api/routes/platforms_tiktok.py`) — it now stays `None` until a real display name/username is available through an approved additional TikTok scope, matching what the frontend and its own pre-existing test (`shows connected state with the account label`) already expected `account_label` to mean. No frontend code changed: `web/app/app/settings/page.tsx`'s existing `{connection.account_label ? ... : null}` render already renders nothing for `null`.

Verified server-only token handling stayed intact throughout (no code path change made here touches this): tokens are Fernet-encrypted before persistence (`credential_store.py`, unchanged), `TikTokConnectionStatus` never carries a credential field (`api/schemas/platforms.py`, Phase 16's existing contract, unchanged), `CREDENTIAL_ENCRYPTION_KEY`/`TIKTOK_CLIENT_SECRET` are read only server-side and never referenced from `web/`, state/PKCE consumption remains a single atomic compare-and-swap (`consume_oauth_state`, unchanged), and disconnect only deletes the stored credential row and never returns it (`disconnect_tiktok`, unchanged behavior — only its `account_label` field changed, per above).

Updated `tests/test_api_platforms_tiktok.py`: two assertions that expected the raw TikTok `open_id` back as `account_label` now assert `None`; the tenant-isolation test (`test_two_users_have_completely_independent_connections`) was rewritten to prove per-user `open_id` isolation directly against the persistence layer (`ContentStore.get_platform_connection(...).external_account_id`) instead of through the now-redacted API field, since that field can no longer carry the isolation proof. Full backend suite: 759 passed (0 removed, assertions corrected in 2 existing tests, isolation coverage preserved by relocating its assertion, not weakened). Frontend: `npm run test` 70 passed, `npm run lint` clean, `npm run build` clean — no frontend files were changed.

Files changed: `src/content_automation/api/app.py`, `src/content_automation/api/routes/platforms_tiktok.py`, `src/content_automation/api/schemas/platforms.py`, `src/content_automation/publishing/tiktok/auth.py`, `tests/test_api_platforms_tiktok.py`.

Not fixed / residual: Railway's own log storage/retention/access-control for whatever reaches stdout (who can view captured logs, how long they're retained) is platform infrastructure this repository has no code-level control over — redacting the query string at the app layer above closes the one thing that was actually this app's responsibility. Milestone 3.6 remains **PARTIAL**, unchanged by this review: a real TikTok OAuth connection completing end-to-end (as described by the user) is a strong sign the blocking gap is closing, but AGENTS.md's own instruction is to verify against this CHANGELOG/the milestone evaluation doc before marking it COMPLETE, and no new evaluation record was written in this pass — this was a security review of the existing flow, not the completion validation itself. Changes are staged and verified but **not committed**, pending review, per the task's own instruction.

## 2026-09-24

### Fix — Local `.env` Had the Hosted TikTok Redirect URI Under the Wrong Variable Name

Diagnosed a `TikTok web redirect URI is not configured` error from the hosted connect endpoint (`api/routes/platforms_tiktok.py`'s `_resolve_web_redirect_uri()` failing closed as designed). Root cause was a local `.env` editing mistake, not a code defect: the `TIKTOK_WEB_REDIRECT_URI` entry (copied from `.env.example`, comment block intact — including its own "Distinct from `TIKTOK_REDIRECT_URI` above" line) had its key manually retyped to `TIKTOK_REDIRECT_URI`, so the hosted flow's variable was effectively unset while the unrelated local desktop/CLI variable (`publishing/tiktok/auth.py`, `cli/tiktok_auth.py`) silently held the Railway callback URL instead — a value that code path never reads. Confirmed via full trace that `TIKTOK_WEB_REDIRECT_URI` and `TIKTOK_REDIRECT_URI` are already correctly separate end to end (distinct `config.py` entries, distinct consumers, no shared fallback, no request-host/proxy-derived redirect URI in the hosted path), so no runtime code changed.

Fixed the local `.env` key name back to `TIKTOK_WEB_REDIRECT_URI` (same value: `https://content-calander-production.up.railway.app/api/platforms/tiktok/callback`). Added a forward-reference to `.env.example`'s `TIKTOK_REDIRECT_URI` block pointing to `TIKTOK_WEB_REDIRECT_URI` for the hosted case, since the desktop variable's own doc block previously gave no signal that a separate hosted variable existed elsewhere in the file — the likely reason the wrong one was set. Added `test_desktop_redirect_uri_does_not_configure_the_hosted_web_flow` to `tests/test_api_platforms_tiktok.py`, closing the one property the existing redirect-URI test group didn't already cover (that setting `TIKTOK_REDIRECT_URI` alone never satisfies the hosted flow's requirement). `tests/test_api_platforms_tiktok.py` + `tests/test_tiktok_auth.py`: 56 passed (55 pre-existing + 1 new), zero existing tests modified.

Same correction needs to be made in Railway's dashboard variables: confirm `TIKTOK_WEB_REDIRECT_URI` (not `TIKTOK_REDIRECT_URI`) is set there to the Railway callback URL — not verified from this environment, since Railway's dashboard isn't accessible from here.

## 2026-09-21

### Milestone 3.6.1 — Production API Deployment (Railway) — in progress

Preparing the FastAPI backend (`src/content_automation/api/`, previously local-only via `cli/run_api.py`) for a real, persistent HTTPS deployment, so the hosted TikTok OAuth flow has a permanent callback URL and the frontend (already live on Netlify's free subdomain, `picklebatch.netlify.app` — no custom domain owned yet) can talk to a real API instead of `localhost`. Railway selected as the host (simple GitHub-connected deploys, persistent HTTPS, env-var secrets, no Kubernetes/container orchestration needed). Added `railway.json` (repo root, config-as-code mirroring how `netlify.toml` already documents the frontend's own deploy config) — Nixpacks builder, healthcheck `/api/health`.

**Found and fixed during a real deploy attempt:** the first `railway.json` used `pip install -e .` (editable install, matching local dev's own documented setup) in the build command, which produced `ModuleNotFoundError: No module named 'content_automation'` at Railway's start step — an editable install's link back to the build-time source path doesn't reliably survive Nixpacks' build→runtime image transition. Reproduced directly: a clean scratch venv with a non-editable `pip install .` correctly copies the package into `site-packages` and both `import content_automation` and `from content_automation.api.app import app` succeed, while the failure mode matches what Railway reported. Fixed by switching `railway.json`'s build command to `pip install -r requirements.txt && pip install .` (non-editable) — production gets a real, portable install; local dev is completely unaffected (`pyproject.toml`/README's `pip install -e .` instructions, and the existing `.venv`, untouched, since editable install is still correct for a live-reload local dev workflow). `cli/run_api.py`'s `--port` now also defaults from the `$PORT` environment variable when set (falling back to `8000` otherwise) as defense-in-depth alongside the start command's explicit `--port $PORT`, so the entry point works correctly even if invoked without that flag. `build/` (setuptools' non-editable-install staging directory, a new local artifact now that `pip install .` is used anywhere) added to `.gitignore`.

Verified end-to-end in a clean scratch venv reproducing Railway's actual invocation: `pip install .` → `python3 cli/run_api.py --host 0.0.0.0` with `$PORT` set and no `--port` flag → real `GET /api/health` returns `200 {"status":"ok"}`. Full backend suite re-run after all changes: 758 passed, unchanged — no route/application-behavior code was touched, only build/entry-point configuration.

**Second deploy attempt:** the fix above got the package importable, but Railway's log then showed `Uvicorn running on http://127.0.0.1:8080` despite `railway.json`'s `deploy.startCommand` explicitly passing `--host 0.0.0.0` — `$PORT` (8080) reached `uvicorn.run()` correctly, so only the `--host` flag failed to take effect. `cli/run_api.py`'s argument parsing and `uvicorn.run(host=args.host, ...)` wiring were confirmed correct directly (a local run with the exact same flags produces `Uvicorn running on http://0.0.0.0:8080`), and no `Procfile`/`Dockerfile`/`nixpacks.toml` exists in the repo to conflict with `railway.json` — so the most likely explanation is that the actual process Railway ran did not include the `--host` flag (e.g. a dashboard-configured Start Command overriding `railway.json`, which Railway allows and which I have no visibility into from this repo). Rather than depend on that flag reaching the process at all, `--host` now defaults to `0.0.0.0` whenever `$PORT` is set (the same hosted-environment signal already used for `--port`'s default) and `127.0.0.1` otherwise — mirroring, not duplicating, the existing `--port` reasoning. Verified locally: explicit `--host 0.0.0.0 --port 8080` → `0.0.0.0:8080`; only `PORT=8080` set, no flags → also `0.0.0.0:8080`; no `PORT` set, no flags (plain local dev) → unchanged `127.0.0.1:8000`. Full backend suite re-run again: 758 passed, unchanged.

Still outstanding before this milestone is COMPLETE: the actual Railway project/service creation, production environment variables, the resulting permanent domain, `TIKTOK_WEB_REDIRECT_URI`/`NEXT_PUBLIC_API_BASE_URL` set to real values, and live verification (health check, auth, real TikTok OAuth connect) against the deployed service — all pending the user's own account/dashboard work. `AGENTS.md` and `docs/architecture/hosted-product-boundary.md` §14/§16 updated to record Railway as selected but not yet live.

Milestone 3.6.1: **IN PROGRESS** — repo is deploy-ready and the package-install bug found in the first real attempt is fixed; live deployment and TikTok OAuth validation are still outstanding.

### Milestone 3.6 — Real Authentication + TikTok Account Connection

Replaced the Milestone 3.5 mock-session boundary with real, server-verifiable user authentication (Supabase Auth, Google as the first provider — evaluated against Auth.js/NextAuth, Firebase Auth/Clerk/Auth0, and a custom Google-Identity-Services flow; selected because Postgres/Storage already live in the same Supabase project, it issues server-verifiable JWTs without a per-request network call in the preferred JWKS mode, and it has a real React Native/Expo SDK satisfying the future-native-compatibility requirement directly), then built the first real user-owned product integration on top of it: connecting a TikTok account from the web app. `NEXT_PUBLIC_ALLOW_MOCK_SESSION` no longer exists anywhere in this codebase — `web/lib/session.tsx` wraps real Supabase Auth (PKCE flow), and a production build with Supabase unconfigured now fails closed unconditionally, with no bypass flag in either direction (verified directly: a real `npm run build` without Supabase env vars set fails; with them set, all 10 routes including new `/login` and `/auth/callback` build cleanly). A new `src/content_automation/api/` package (FastAPI, the location `hosted-product-boundary.md` §12 predicted) is the first real backend HTTP surface this project has: `GET /api/me`, `GET/POST /api/platforms/tiktok/{status,connect,callback,disconnect}` — every protected route derives identity exclusively from a verified bearer token (`identity/token_verification.py`, JWKS-preferred with HS256 fallback, proven against real self-signed RSA cryptography, not mocked away) via a new `api/dependencies/auth.py#get_current_user` dependency; a client-supplied `user_id` is never accepted anywhere. `identity/user_resolution.py` maps a verified identity onto `users`/`auth_identities` (ADR-0007's schema, unchanged) on first login, deliberately without auto-linking by matching email (a real security footgun this milestone declined to take on for no corresponding benefit). A new `cli/link_bootstrap_user.py` — explicit, one-off, human-run, dry-run-capable — exists to link the pre-3.6 local bootstrap user's real data (5 videos, content_slots, platform_posts, the existing TikTok connection) to a real login without orphaning it; not yet run against real data.

The hosted TikTok OAuth connection flow (`api/routes/platforms_tiktok.py`) reuses `publishing/tiktok/auth.py`'s PKCE generation, state generation, authorization-URL building, and token exchange/refresh **completely unmodified** — no rewrite of proven OAuth logic, only new hosted callback/storage code layered alongside the still-fully-supported local CLI flow. The hosted callback (`GET /api/platforms/tiktok/callback`) is deliberately unauthenticated (TikTok redirects the browser directly, carrying no bearer header) — binding to "which user does this belong to" comes entirely from a new server-side `oauth_states` row created while the caller *was* authenticated, atomically consumed on callback (a compare-and-swap `UPDATE ... WHERE consumed_at IS NULL`) so a replayed, unknown, or expired state is rejected identically, never distinguished in a way that would help an attacker; a denial, invalid state, or exchange failure each redirect to `/app/settings?tiktok=<reason>` with no raw TikTok error detail ever reaching the browser. A new `platform_credentials` table (SQLite + Postgres migration `0003_...sql`) — deliberately separate from `platform_connections`, which ADR-0007 already documented to carry no secrets — holds a hosted TikTok connection's access/refresh token pair, Fernet-encrypted (`config.CREDENTIAL_ENCRYPTION_KEY`, server-side only, `cryptography` already a transitive dependency, no new one added) and refreshed via a bounded optimistic-concurrency (CAS) retry loop against `updated_at` (`publishing/tiktok/credential_store.py`) instead of the CLI's process-local `fcntl` lock, which remains completely untouched for the local path. This closes the *data-integrity* risk of concurrent hosted refresh fully; one residual limitation is honestly documented, not silently ignored: two truly simultaneous refresh attempts could both call TikTok's own refresh endpoint before either persists, which a real distributed lock (not built this milestone, per the brief's own explicit allowance to document rather than solve this specific edge) would close fully. Tenant isolation was proven directly with two real users across the full connect→callback→status→disconnect cycle, including that a state bound to one user's connect attempt resolves to that user's connection regardless of which session happens to hit the callback. Disconnect removes the stored credential and marks the connection `DISCONNECTED` without deleting connection history or touching any video/post data; no provider-side token revocation call was implemented (TikTok's current docs publish no distinct revoke endpoint this codebase could target confidently).

On the frontend: `web/app/login/page.tsx` (new, public) — a single "Continue with Google" action, no password auth; `web/app/auth/callback/page.tsx` (new) completes the OAuth redirect and navigates into `/app`; `components/app/AppAuthGate.tsx` (new) redirects an unauthenticated visitor from `/app/*` to `/login` client-side — a UX affordance only, since this is a static export with no server-side session check possible, not the actual security boundary (the backend's independent token verification is). `web/app/app/settings/page.tsx` was rewritten to call the real backend (`web/lib/api/platforms.ts`, new typed functions) instead of Milestone 3.5's mocked platform-connection data, with real loading/connected/disconnected/error states. Found and fixed a real UI-primitives gap while wiring the Connect/Disconnect actions: the existing `Button` component only supported `href`-based navigation, which would have forced an `href="#"` + `preventDefault()` anti-pattern for a real action — extended `Button` with a genuine `type="button"` + `onClick` mode instead (a real `<button>` element for a real action, never a disguised link), verified all 9 pre-existing `href`-based usages unaffected. `web/lib/api/client.ts`'s `apiRequest()` gained an `accessToken` option (attaches `Authorization: Bearer <token>`) while staying session/framework-agnostic — the caller supplies the token, this module never imports `lib/session.tsx` or the Supabase client directly, keeping it reusable by a future native client per ADR-0010's own future-compatibility goal.

57 new backend tests (`test_token_verification.py` ×10 — real RSA-signed JWTs, both JWKS and HS256 modes, every rejection path; `test_user_resolution.py` ×7; `test_link_bootstrap_user.py` ×7, including a byte-for-byte proof it touches nothing but `auth_identities`; `test_credential_store.py` ×13, including a real simulated lost-CAS-race scenario; `test_api_me.py` ×6 and `test_api_platforms_tiktok.py` ×14, both exercised through a real FastAPI `TestClient` against a real temp-file SQLite database, TikTok's own HTTP calls mocked). Full backend suite: 753 passed (696 baseline + 57 new, zero existing tests modified). 21 net new frontend tests (67 total: 46 baseline − 3 replaced session-contract tests + 4 rewritten + 2 Button action-mode + 5 `platforms.test.ts` + 3 `AppAuthGate.test.tsx` + 6 `settings-tiktok.test.tsx` + 4 `login.test.tsx`). No real Google Cloud OAuth client, no real Supabase Auth configuration, and no real TikTok OAuth exchange exist yet — real setup was still in progress with the user as of this writing; `netlify.toml` deliberately does not set `NEXT_PUBLIC_SUPABASE_URL`/`NEXT_PUBLIC_SUPABASE_ANON_KEY`/`NEXT_PUBLIC_API_BASE_URL` yet, so the deployed shell correctly fails closed rather than silently rendering broken. `AGENTS.md`, `.env.example` (root and `web/`), `docs/architecture/hosted-product-boundary.md` (§8/§12/§14/§17/§18) updated. Full details in `docs/decisions/0011-real-authentication-and-tiktok-connection.md` and `docs/evaluations/productization/milestone-3.6-auth-tiktok-connection.md`.

Milestone 3.6: **PARTIAL** — everything buildable without real external credentials is complete and tested; closing to COMPLETE requires the user finishing real Supabase/Google Cloud setup, one real login, running `cli/link_bootstrap_user.py` against real data, and a real TikTok OAuth connection — a follow-up validation pass, not further implementation work.

**Update:** real Supabase login and `cli/link_bootstrap_user.py` have since been run against real data (verified directly against real Postgres: one real `auth_identities` row, all 5 pre-existing videos linked to the real user). The real hosted TikTok OAuth connection is still outstanding — see the fix below, found while attempting to validate it live.

### Fix — Hosted TikTok OAuth Used a Request-Derived Redirect URI Instead of an Explicitly Configured One

Found while attempting the real hosted TikTok OAuth connection validation Milestone 3.6 left outstanding: `api/routes/platforms_tiktok.py`'s `start_tiktok_connect` built the `redirect_uri` it sends to TikTok via Starlette's `request.url_for("tiktok_oauth_callback")` — derived from whatever host/scheme the *incoming request* appeared to arrive on, rather than a fixed, explicitly configured value. `cli/run_api.py`'s `uvicorn.run(...)` call passes no `proxy_headers`/`forwarded_allow_ips`, so behind any reverse proxy or hosting platform terminating TLS in front of uvicorn, `request.url_for()` resolves from the raw (often internal, often plain-`http://`) connection uvicorn actually sees — silently producing a `redirect_uri` that can never match whatever exact URL is registered in TikTok's Developer Portal (Login Kit requires an exact match), the specific real-world failure mode that surfaced this.

Added `config.TIKTOK_WEB_REDIRECT_URI` (`.env.example` updated) — a fixed, explicitly configured redirect URI for the hosted flow specifically, deliberately separate from the existing `TIKTOK_REDIRECT_URI` (still unchanged, still only feeding the local desktop/CLI flow in `publishing/tiktok/auth.py`, still permitted to be a plain-http loopback address). `start_tiktok_connect` now resolves it via a new `_resolve_web_redirect_uri()` helper, validated at connect-time — before any TikTok call or `oauth_states` write — to be non-empty and an absolute `https://` URL, raising a clear `HTTPException(500, ...)` otherwise rather than ever sending TikTok (or persisting) a value that could never be registered. No change was needed to the callback or token-exchange leg: `consumed.redirect_uri` (the `oauth_states` row's own persisted value, unchanged since Milestone 3.6) already carries the same string through to `exchange_code_for_token`, so the "same value used throughout the whole transaction" property holds by construction, not by a second lookup that could drift. The now-unused `Request` parameter/import was removed from `start_tiktok_connect`.

5 new tests in `tests/test_api_platforms_tiktok.py` (a new autouse `tiktok_web_redirect_uri` fixture supplies a fixed valid value for every other existing test, none of which needed further changes): the configured URI appears exactly in the authorization URL's `redirect_uri` parameter; the callback's token exchange receives that identical string (captured directly, not just asserted equal after the fact); a request carrying a spoofed `Host`/`X-Forwarded-Proto` header still produces the one configured value, proving it's no longer request-derived at all; connect fails closed (500, no `oauth_states` row written — verified via a direct row-count check) when the value is unset; and fails closed when set but not `https://`. Full backend suite: 758 passed (753 baseline + 5 new, zero existing tests modified); `tests/test_tiktok_auth.py` (local CLI flow, 35 tests) re-run unchanged to confirm no effect on that path. `AGENTS.md`'s test-baseline line updated. `docs/decisions/0011-real-authentication-and-tiktok-connection.md` and `docs/evaluations/productization/milestone-3.6-auth-tiktok-connection.md` updated in place with dated correction notes (original evidence not rewritten).

## 2026-09-20

### Milestone 3.5 — Mobile-First Responsive Web App Shell

Built the mobile-first product app shell Milestones 3.6+ will build the real product UI inside — not the features themselves: no real authentication, no TikTok connection UI, no batch upload, no real scheduling controls, no real queue/calendar backend data, no hosted workers, per the milestone's own explicit guardrails. `web/` (previously a public marketing site only) gained two Next.js route groups with no shared chrome: `app/(marketing)/` (`/`, `/privacy`, `/terms` — unchanged URLs, moved via `git mv` to preserve history) and `app/app/` (new: `/app`, `/app/library`, `/app/queue`, `/app/settings`), with the true root layout cut down to only genuinely global concerns (html/body/font/metadata) and each route group owning its own chrome. Mobile-first navigation: `components/app/AppNav.tsx` defines one `navItems` list and renders it twice — a fixed bottom tab bar on mobile (`md:hidden`, safe-area-inset padding) and a left sidebar on desktop (`hidden md:block`) — mirroring the marketing site's own existing mobile/desktop nav pattern rather than inventing a new one. Added an API-client boundary (`lib/api/client.ts`'s `apiRequest()`, normalizing every failure mode into a stable `ApiError` with a `reasonCode`) that nothing calls yet since no backend exists — product pages read `lib/api/mockData.ts` instead, or ship genuinely empty (`/app/library`/`/app/queue` render their real "nothing yet" state by default). Frontend domain types (`lib/api/types.ts`) are deliberately decoupled from the backend's actual database columns. A status-presentation layer (`lib/status.ts`) translates backend lifecycle enums into human labels/tones instead of any page displaying a raw enum string. Six new UI primitives (`Card`, `Badge`, `PageHeader`, `Spinner`, `EmptyState`, `ErrorState`) extend the marketing site's existing token system rather than replacing it (new status-color tokens, one new app-content-width token).

**Session/auth boundary, and the temporary mock-session deployment decision.** `lib/session.tsx`'s `useSession()` is the one seam every `/app/*` page depends on, backed only by a loudly-marked development-only mock user (`DEV_MOCK_USER`) — no real authentication provider was selected or integrated. `SessionProvider` fails closed by default: it throws in a production build unless `NEXT_PUBLIC_ALLOW_MOCK_SESSION=true` is explicitly set, proven directly by `tests/lib/session.test.tsx` (mock renders outside production; throws in production without the flag; renders with the flag explicitly set). The actual deployed Netlify build sets that flag deliberately (`netlify.toml`) — a temporary, explicitly-documented shell-development configuration, not a shipped product decision, and not itself a secret (it is `NEXT_PUBLIC_` and browser-visible by design). Both `netlify.toml` and `lib/session.tsx` carry the same explicit "BLOCKER BEFORE REAL USER DATA ACCESS" checklist (real server-verifiable auth; remove the mock path; remove the Netlify flag; re-verify fail-closed; never trust a client-supplied `user_id`) — recorded durably in a new `docs/decisions/0010-frontend-app-shell.md`. A second guardrail test (`tests/lib/no-secrets-in-client-bundle.test.ts`) statically scans `app/`/`components/`/`lib/` for any non-`NEXT_PUBLIC_` `process.env` reference and fails if one exists — proof against that specific mistake, not a general secret-leak guarantee (documented precisely as such in ADR-0010, not overclaimed).

Replaced a placeholder monogram icon with the project's real app icon (`images/pickle-icon-no-bg.png`) across all PWA/browser surfaces — generated `icon-192.png`/`icon-512.png`/`apple-touch-icon.png` and a real multi-resolution `favicon.ico`, wired into `app/layout.tsx` metadata and `public/manifest.webmanifest` (light PWA metadata only — manifest + correct viewport + icons, no service worker, per the milestone's own guardrail). Found and fixed one real responsive defect during manual validation: `/app/settings`'s platform-connection row held its status badge and "Connect" button on a fixed horizontal line that wrapped awkwardly at 320-375px widths and separately duplicated "Not connected" as both a plain-text label and the status badge — fixed by stacking the row responsively (`flex-col` below `sm:`, matching `PageHeader`'s existing pattern) and removing the redundant label. Validated with a real headless Chromium (Playwright) against the actual static export at six widths (320/375/390/768/1024/1440px) across all 7 routes (42 checks): zero horizontal overflow (`scrollWidth`/`clientWidth` compared programmatically, not just read from Tailwind classes), confirmed visually via full-page screenshots. Deep-link/direct-load behavior verified against the real static export served locally: every route returns 200 with its real content at its canonical trailing-slash URL, a bare path 301-redirects to it, and an unknown path 404s (`out/404.html` exists at the correct root-level path for Netlify's static-hosting convention to pick up automatically).

Set up a frontend test framework (none existed before this milestone) — Vitest + React Testing Library, `web/vitest.config.mts`. 46 new tests across 9 files: public/product route rendering, mobile/desktop nav (destination list, active-state prefix matching, nested-route handling), all UI primitives, `QueueItemCard`, `apiRequest()`'s error-normalization (network failure, non-2xx with/without a JSON body, malformed successful response), `lib/status.ts`'s presentation mapping, and the two session/secrets guardrail tests above. `npm run lint` and `npm run build` (which runs the TypeScript typecheck) are both clean; the production static export emits all 8 routes correctly. A security review (`git grep` across `web/`) confirmed no `DATABASE_URL`/service-role/secret pattern exists anywhere in frontend source outside comments explicitly warning against it, `.env.local` stays gitignored while `.env.example` (new, documenting `NEXT_PUBLIC_API_BASE_URL`/`NEXT_PUBLIC_ALLOW_MOCK_SESSION`) is trackable (a pre-existing blanket `.env*` gitignore rule was fixed with a `!.env.example` exception). Full backend suite re-run after all frontend work: 696 passed, unchanged from the Milestone 3.4 baseline — confirming zero backend regression, as expected from a frontend-only milestone with no shared code in either direction. `docs/architecture/hosted-product-boundary.md` gained a new §18 (Frontend Application Boundary) plus update notes at the top and in §14's frontend-hosting row; `AGENTS.md`, root `README.md`, and `web/README.md` updated. Full details in `docs/decisions/0010-frontend-app-shell.md` and `docs/evaluations/productization/milestone-3.5-mobile-web-shell.md`.

Milestone 3.5: **COMPLETE**.

### Milestone 3.4 — Object Storage + Media Lifecycle

Removed the hosted publishing and hosted processing paths' dependency on permanent local media files — canonical video files no longer have to live permanently on one computer to be published *or* processed by the hosted pipeline; local CLI ingestion remains fully supported, unmodified, and permanent-local-file for local development. Added a real second object-storage backend (Supabase Storage, via plain `requests` against its REST API — no new SDK dependency, every API shape verified directly against a real project first) alongside the existing local filesystem, mirroring Milestone 3.3's exact two-parallel-implementation pattern: `storage.protocol.StorageProtocol` (put/exists/delete/materialize, a `typing.Protocol`), `storage.local.LocalStorage` (dev/test default, no network), `storage.supabase_storage.SupabaseStorage` (real Supabase, private buckets, service-role key only), and `storage.factory.build_storage()` (no silent hosted-to-local fallback, same philosophy as `store_factory.build_content_store()`). New `media.media_storage` module bridges `ContentStore` rows to a `StorageProtocol` instance — `upload_canonical_media`/`materialize_canonical_media`, both verifying the caller's `user_id` against the video's real owner *before* touching storage at all (`MediaOwnershipError` otherwise), and both keyed by a deterministic, tenant-scoped `users/<user_id>/videos/<video_id>/source<ext>` object key (naturally idempotent — a repeat upload overwrites, never duplicates). Two new nullable `videos` columns, `storage_provider`/`storage_key` — nullable on both backends, deliberately different from Milestone 3.3's `user_id` decision (no real row had a value to backfill, so `NOT NULL` had nothing to apply to), added via SQLite's existing additive-column pattern and a genuinely new Postgres forward migration (`0002_add_media_storage_columns.sql` — `0001_initial_schema.sql`, already applied to real production data, was not touched). `scheduling/publish_tiktok.py`'s `execute_claimed_platform_post`/`publish_video` gained an optional `storage` parameter and now materialize a storage-backed video to a real temp local path before validating/publishing — `TikTokPublisher` itself was not modified at all, per the brief's own explicit preference; a video with no `storage_provider` (every pre-3.4 video) never even consults `storage`, verified by the full pre-existing 646-test suite passing completely unchanged immediately after this specific refactor, before any new capability was layered on top.

**Correction found and fixed during review, before this milestone was treated as closed:** the first pass left `media/processing.py` (local ingestion — `process_one`/`discover_videos`/`_move_file`) completely untouched and as the *only* processing path, which meant a storage-backed video (uploaded but never locally processed) had no way to be processed without first being forced through a permanent local round trip — directly contradicting this milestone's own "local files are only temporary materializations" framing. Fixed with the smallest additive change: `process_one` gained optional `on_failed`/`on_assigned` hooks (default to the exact pre-3.4 `_move_file` behavior, so every existing caller including `cli/process_content.py` is unaffected), and a new `media.processing.process_storage_backed_video` entry point reuses `process_one`'s inspection/transcription/caption/scheduling logic verbatim against an object-storage materialization instead of duplicating the pipeline. 8 new focused tests (`tests/test_process_storage_backed_video.py`) prove: a storage-backed video processes correctly with its local source absent; the temp materialization is removed on both success and failure; ownership is checked (via a spy storage implementation) before any storage call; and existing local `process_one` behavior is unchanged. The real end-to-end test (`tests/test_media_storage_postgres.py`) gained a second, fuller case starting from a real ffmpeg-synthesized upload through real hosted processing into publishing, rather than only from an already-prepared video (a real test-timing bug — `process_storage_backed_video`'s FIFO matching has no injectable `now`, unlike `worker.run_due_posts_once` — was found and fixed while writing it). ADR-0009 and the architecture doc's §7 were corrected to state the "temporary materializations" claim applies to the hosted paths specifically, not to local ingestion.

No deletion/retention policy was implemented for canonical media on either backend — a deliberate, documented V1 choice (multi-platform publishing doesn't exist yet to make a terminal-state rule evaluable), not an oversight; `StorageProtocol.delete()` exists and is tested but no production path calls it against real media. A new `cli/migrate_media_to_object_storage.py` uploaded all 5 real production videos (56MB total) to Supabase Storage with explicit user confirmation obtained first (dry run shown beforehand), each verified byte-for-byte via SHA-256 after upload, and never deleted or modified any local original. 50 new tests total: 41 run unconditionally (`test_storage_local.py`, `test_storage_factory.py`, `test_media_storage.py`, `test_publish_tiktok_storage.py`, `test_migrate_media_to_object_storage.py`, `test_process_storage_backed_video.py`) and 9 require real credentials and skip automatically otherwise (`test_storage_supabase.py` ×7 against a disposable test bucket, `test_media_storage_postgres.py` ×2 — full real-Postgres + real-object-storage + FakePublisher end-to-end pipelines through the actual production modules, one starting from an already-prepared video and one from real upload through real hosted processing, both proving materialize → assign/process → worker-materialize → publish → `PUBLISHED` → temp-file-cleanup, with zero live TikTok calls). Full suite: 696 passed (646 baseline + 50 new; one existing test's hardcoded migration-count assertion was legitimately updated to compare against the real migrations directory instead of a stale literal, since this milestone added the second real migration file that assertion existed to eventually catch). No object deletion, no upload UI, no hosted workers, no real auth integration, no TikTok credential changes, no change to local ingestion's own behavior. `AGENTS.md`/`.env.example`/`.gitignore` updated. Full details in `docs/decisions/0009-object-storage-media-lifecycle.md` and `docs/evaluations/productization/milestone-3.4-object-storage-media-lifecycle.md`.

Milestone 3.4: **COMPLETE**.

### Milestone 3.3 — Hosted Database / Postgres Migration

Added Postgres as a second, real, tested persistence backend (Supabase, via the Session Pooler — this sandbox has no outbound IPv6 and Supabase's direct connection host is IPv6-only, confirmed by a real "Network is unreachable" failure before falling back to the pooler string) without migrating any CLI entry point over to it by default, changing scheduling/publishing semantics, or touching object storage/real auth/hosted workers. New `PostgresContentStore` (`persistence/postgres_content_store.py`, psycopg3, no ORM) implements the same method surface as `ContentStore` (SQLite, unchanged) — two parallel concrete classes rather than a shared abstraction, now documented as a shared contract via a new `ContentStoreProtocol` (`typing.Protocol`, structural, no inheritance forced on either class). Fresh Postgres schema (`persistence/postgres_migrations/0001_initial_schema.sql`, applied via a new dependency-free versioned-migration runner — not Alembic, which would pull in SQLAlchemy) — not a replay of SQLite's own historical rebuild-based migrations, which have no Postgres equivalent need. `user_id` is `NOT NULL` under Postgres (verified: a raw NULL insert raises `psycopg.errors.NotNullViolation`) — Milestone 3.2's nullable-under-SQLite compromise was explicitly transition-only and not carried forward, per this milestone's own instruction. Timestamps preserved exactly per-column, not normalized: `TIMESTAMP` for naive-local columns (`scheduled_at`, `next_retry_at`), `TIMESTAMPTZ` for aware-UTC ones (`created_at`/`updated_at`/`published_at`/`next_status_check_at`), with the connection's session time zone forced to UTC at connect time and every returned `datetime` converted back to an isoformat string so callers see exactly the same `str`-typed fields regardless of backend. The two concurrency primitives every scheduling module depends on were verified under **real** concurrent Postgres connections, not assumed: `claim_platform_post` — 5 real threads/connections racing for one row, exactly 1 wins; `update_platform_post_if_unchanged` — a stale-holding actor's compare-and-swap correctly fails while a newer write survives. Cross-tenant isolation (Milestone 3.2's own guarantee) was re-proven against real Postgres, including a full end-to-end pipeline (upload → assign → publish → PUBLISHED; async PROCESSING → reconciliation → PUBLISHED; crash-recovery requeue) run through the *actual* production modules (`slot_matcher`, `platform_post_materializer`, `worker`, `reconciliation`, `crash_recovery`) with zero code changes to any of them. A new `cli/migrate_sqlite_to_postgres.py` copied the real SQLite database into Postgres's `public` schema, preserving every original integer id (`OVERRIDING SYSTEM VALUE` + post-import sequence advancement — verified a subsequent real insert doesn't collide) and every publishing-critical field; rehearsed first against a disposable schema, verified via a dedicated `--verify-only` field-by-field comparison (zero mismatches across all 6 tables: users=1, auth_identities=0, platform_connections=1, content_slots=9, videos=5, platform_posts=5), and made zero TikTok API calls. 20 new tests (`tests/test_postgres_content_store.py` ×18, `tests/test_store_factory.py` ×2): the 18 in `test_postgres_content_store.py` run only against a dedicated `pickle_batch_test` schema (dropped/recreated per session, truncated per test) and are skipped automatically when `DATABASE_URL` is unset; the 2 in `test_store_factory.py` (backend-selection logic) run unconditionally and touch no schema at all. SQLite remains the default/fast-test backend, zero network dependency for the other 626 tests. `persistence/store_factory.py`'s `build_content_store()` selects backend by `DATABASE_URL` presence and never silently falls back from a broken Postgres connection to SQLite — a real bug in the original implementation (relying on `PostgresContentStore`'s own default parameter instead of passing `dsn` explicitly, so a reconfigured `DATABASE_URL` was silently ignored) was found and fixed while proving this. RLS was evaluated and explicitly deferred — this codebase's planned `client -> FastAPI -> database` shape means server-side tenant scoping (already proven) is the correct current boundary; enabling RLS now would require a session-identity mechanism no auth layer exists yet to supply. Full suite: 646 passed (626 baseline + 20 new, zero existing tests modified). No CLI entry point was switched to Postgres by default; no object storage, hosted workers, or real auth integration were added; no TikTok credential secret was moved. `AGENTS.md` updated (stage line, test baseline, persistence package description, `.env` credential list). Full details in `docs/decisions/0008-postgres-persistence-migration.md` and `docs/evaluations/productization/milestone-3.3-postgres-migration.md`.

Milestone 3.3: **COMPLETE**.

### Milestone 3.2 — User Authentication and Ownership Model

Introduced the first real multi-user product boundary, without migrating to Postgres and without moving TikTok credentials into hosted storage. Added three new tables (`users`, `auth_identities` — `UNIQUE(provider, provider_subject)`, `platform_connections` — `UNIQUE(user_id, platform)`, identity/status only, no credential secret) and a nullable `user_id` column on `videos`/`content_slots`/`platform_posts` (nullable rather than `NOT NULL`, verified directly that SQLite supports `ALTER TABLE ADD COLUMN ... REFERENCES` with working FK enforcement, but cannot cleanly add a `NOT NULL` FK column to an already-populated table without a full rebuild — deferred to the Postgres migration; see `docs/decisions/0007-user-ownership-model.md`). Every `ContentStore` method this milestone touches (`insert_video`, `insert_slot_if_missing`, `insert_platform_post`, `insert_platform_post_if_missing`, `find_earliest_open_slot`(`_fifo`), `get_due_platform_posts`, `get_recoverable_platform_posts`, `get_reconcilable_platform_posts`, `claim_platform_post`, `update_platform_post_if_unchanged`) gained an **optional** `user_id` parameter — omitting it reproduces the exact pre-3.2 unscoped behavior, so all 596 pre-existing tests pass completely unchanged; a `ScopedContentStore`-style wrapper object was evaluated and set aside in favor of this simpler, lower-risk shape for now (full reasoning in the ADR). `ContentStore.assign_slot()` gained an `OwnershipMismatchError` check: a video and the slot it's being assigned to may not carry different non-null owners (legacy/unscoped rows — either side `NULL` — are unaffected, preserving every existing test). `worker.run_due_posts_once`, `reconciliation.reconcile_pending_status_checks_once`, `crash_recovery.recover_stale_posts_once`, and `media.processing.process_one` each gained the same optional `user_id`, implementing the multi-tenant execution invariant `docs/architecture/hosted-product-boundary.md` §5 stated but did not yet enforce ("a hosted background job must never operate on one user's records using another user's credentials") — proven directly by new integration tests (two real users, two real due/stale posts, a worker/reconciliation/crash-recovery pass scoped to one user provably never touches the other's row). `ContentStore.get_or_create_local_user()`/`get_or_create_platform_connection()` (idempotent, modeled on `calendar_manager.resolve_app_calendar`'s existing resolve-or-create pattern) let every CLI entry point (`cli/worker.py`, `cli/reconciliation.py`, `cli/crash_recovery.py`, `cli/process_content.py`, `cli/generate_calendar.py`) resolve and thread a real local user through without a separate manual bootstrap step — smoke-tested as real subprocesses against an isolated temp DB, confirming the bootstrap user and TikTok `platform_connection` are created once and correctly reused across separate process invocations. A new one-off `cli/backfill_ownership.py` (matching the existing `backfill_platform_posts.py`/`migrate_relocated_paths.py` convention) retroactively attributes every pre-3.2 row (`user_id IS NULL`) to the bootstrap user and bridges the existing cached TikTok token file's `open_id` into a `platform_connections` row — a local file read only (`tiktok_auth.load_token()`, never `get_access_token()`), making zero TikTok API calls. 30 new tests (`tests/test_ownership.py` ×22, `tests/test_backfill_ownership.py` ×8) cover user/auth-identity/connection creation and uniqueness, ownership stamping, the `assign_slot` invariant (both the rejection and the legacy-passthrough cases), scoped-vs-unscoped selector behavior, scoped claim/update rejecting a wrong-tenant caller, the three full job-level cross-tenant integration proofs, and the backfill script's idempotency/dry-run/never-reassign/publishing-state-untouched guarantees. Full suite: 626 passed (596 baseline + 30 new, zero existing tests modified). No Postgres migration, no object storage, no hosted worker/scheduler, no real auth-provider integration, no production API — all explicitly deferred per this milestone's own guardrails; provider selection for real authentication was evaluated and explicitly deferred since no transport layer exists yet to make it consequential. Real `data/content.db` migration ran with explicit user confirmation obtained first (backup taken beforehand): attributed all 5 `videos`, 9 `content_slots`, and 5 `platform_posts` real rows to one bootstrap user, created one real `platform_connections` row whose `external_account_id` correctly picked up the actual cached TikTok token's `open_id`, and made zero TikTok API calls; verified read-only afterward that `videos.status`, `platform_posts.(video_id, status, platform_post_id)`, and the `content_slots` row count are byte-identical to before — only `user_id` was added. Full suite re-run after the real migration: 626 passed, unchanged. `AGENTS.md`'s "Current Product Stage" and test-baseline lines updated. Full details in `docs/decisions/0007-user-ownership-model.md` and `docs/evaluations/productization/milestone-3.2-user-auth-ownership.md`.

Milestone 3.2: **COMPLETE**.

## 2026-09-19

### Milestone 3.1 — Hosted Architecture Boundary

Documentation-only milestone: defined the target hosted architecture for Pickle Batch before any hosted infrastructure is built, per Milestone 3.0's own closing note that 3.1 would build directly on its package-import boundary. Investigated the full post-3.0 `src/content_automation/` runtime surface (~7,700 lines across `media/`, `scheduling/`, `publishing/`, `persistence/`, `calendar/`, plus `cli/`) directly rather than assuming 3.0's summary still held, and confirmed it does: `grep -rl "^import sqlite3" src/ cli/ tools/` shows `ContentStore` is the sole SQLite access point, every `cli/*.py` file is argument parsing only, and all four scheduling background operations (`worker.run_due_posts_once`, `reconciliation.reconcile_pending_status_checks_once`, `crash_recovery.recover_stale_posts_once`, `media.processing.process_one`) are already plain one-pass functions with no daemon/CLI coupling — exactly the shape a future FastAPI service or job runner needs, requiring no adapter. Produced `docs/architecture/hosted-product-boundary.md`, the canonical target-architecture reference for Milestones 3.2–3.14: current vs. target architecture, the five-layer model (API / application-domain / domain-runtime / persistence-infra / background jobs), which operations must stay synchronous-API-safe vs. must move behind a background job (checked against real timeouts — e.g. `TikTokPublisher`'s 300s upload timeout, `ffprobe`/`ffmpeg`'s 30s/300s subprocess timeouts — not assumed), the persistence boundary (decision: `ContentStore` remains the persistence abstraction through both the ownership (3.2) and Postgres (3.3) migrations, and no repository/interface abstraction was extracted on top of it — but, clarified during review, its current method signatures are not frozen: 3.2's ownership work will likely need explicit tenant scoping added to methods like `get_due_platform_posts`/`claim_platform_post`/`update_platform_post_if_unchanged`, with only the underlying scheduling/concurrency semantics — atomic claim, optimistic concurrency — required to survive unchanged), a multi-tenant execution invariant added during review (a hosted background job must never operate on one user's records using another user's credentials — scheduled publishing, reconciliation, crash/stale recovery, and media processing must all eventually execute within explicit ownership/account scope; not enforced today, single-tenant), the media/storage contract local-filesystem call sites will need to satisfy under future object storage, the credential/platform-connection model current single-global-TikTok-token/single-global-Calendar-OAuth assumptions will need to become, a full `config.py` classification (static app config / deployment config / per-user settings / platform-connection state / runtime state), a user-ownership impact map, five target request/job flows, the future `src/content_automation/api/` package location, a service-layer audit (four of the brief's five named example operations already exist as callable package functions post-3.0; one genuine gap — "reschedule an already-assigned post" — found and recorded, not built), a hosted-infrastructure decision matrix with every provider choice explicitly deferred to its corresponding future milestone, and a migration-risk list ranked must-fix-before-hosted-launch / milestone-specific / safe-to-defer. No ADR was created — the target shape was the milestone brief's own stated goal, not an independently-arrived-at architectural decision, matching Milestone 3.0's own reasoning for skipping one. No code changes were made or needed: the persistence, publishing, and CLI boundaries this milestone was asked to identify already existed from Milestone 3.0's refactor. Full suite: 596 passed, unchanged from Milestone 3.0's baseline. Real `data/content.db` read read-only (`sqlite3 -readonly`) before and after — 5 `videos` rows and 5 `platform_posts` rows confirmed untouched; no schema migration ran; no TikTok API calls or Google Calendar writes were made. `AGENTS.md`'s "Current Product Stage" section and its repository-structure/routing sections updated to record 3.1 as complete and point to the new architecture document; `PROJECT_STATE.md`/`README.md` left unchanged since no runtime behavior changed. Full details in `docs/evaluations/productization/milestone-3.1-hosted-architecture-boundary.md`.

Milestone 3.1: **COMPLETE**.

## 2026-09-18

### Milestone 3.0 — Backend Package / Source Organization Refactor

Reorganized the backend from 29 flat root-level Python files into a responsibility-based package — behavior-preserving only, zero scheduling/publishing/retry/reconciliation/crash-recovery/auth semantic changes, zero schema changes, zero real-data mutation. New layout: `src/content_automation/{config.py, media/, scheduling/, publishing/ (+ publishing/tiktok/), persistence/, calendar/}` for runtime logic, `cli/` for thin argument-parsing entry points (`python3 cli/worker.py`, etc. — six files plus `tiktok_auth.py` split into package logic + thin wrapper so a future service can call the same functions directly, per Milestone 3.1's stated need), and `tools/evaluation/` for engineering benchmarking tooling (kept separate from the pre-existing gitignored `evaluation/` golden-dataset directory to avoid a name collision). An editable install (`pyproject.toml`, `pip install -e .`) makes `content_automation` importable from anywhere without `sys.path` hacks — replacing `conftest.py`'s previous hack, which was deleted. Investigation before any move found two real path-resolution hazards and fixed them: `config.py` and three evaluation scripts computed critical paths (`.env`, `data/content.db`, `content/`, the `evaluation/` dataset dir) via `Path(__file__).with_name(...)`, correct only while those files sat at repo root — replaced with a single exported `config.REPO_ROOT`, verified via a real subprocess run against the real repo tree. The pre-existing root-level `scheduling.py` (calendar posting-date generation) was renamed to `calendar/cadence.py` to avoid colliding with the new `scheduling/` package (due-post selection, worker, retry, reconciliation, crash recovery) — a different concern that happened to share the word "scheduling." All 34 dependent test files had their imports updated; 19 needed their `monkeypatch.setattr` targets re-verified against where the split code actually performs its attribute lookup at call time (package module vs. thin CLI wrapper — two independently-patchable bindings after the split), not merely their import lines changed. Full suite: 596 passed both before and after (test count unchanged); all 10 CLI entry points verified as real subprocesses; real `data/content.db` confirmed byte-identical throughout, no TikTok API calls. `README.md`/`PROJECT_STATE.md` updated to the new invocation paths (plus `PROJECT_STATE.md`'s stale test-pass-count/`conftest.py` description corrected); historical `docs/decisions/`/`docs/evaluations/` records deliberately left unrewritten. Added a root-level `AGENTS.md` — a thin, verified-accurate project entry point for any coding agent (current milestone stage, package boundaries, global-policy pointers, credential/scope guardrails), modeled structurally (not in content) on `~/w2_pipeline/AGENTS.md`. Full details in `docs/evaluations/productization/milestone-3.0-backend-package-refactor.md`.

Milestone 3.0: **COMPLETE**.

### Fix — Reconciliation Treated REAUTHORIZATION_REQUIRED as Retryable (Milestone 2.1.10 correction)

Found by user review after Milestone 2.1.10 was committed, before treating it as closed: `reconciliation.py`'s `get_status()` failure handling treated every `PublishError` identically — reschedule and stay `PUBLISHING` — so a genuine `TikTokReauthorizationRequiredError` (refresh token revoked/expired, Milestone 2.1.8) would sit `PUBLISHING` and get silently re-polled forever instead of ever resolving. Fixed by classifying the exception through the existing `retry_classification.is_retryable(reason_code, http_status)` predicate — the same classifier `publish_tiktok._schedule_retry_or_fail` already uses for the identical reason/http_status shape — instead of one undifferentiated branch: a terminal reason (`REAUTHORIZATION_REQUIRED`) now ends the row `FAILED` immediately with the actionable `--authorize` reconnect message and schedules no further check; a retryable reason (transient network/5xx) keeps the original stay-`PUBLISHING`-and-reschedule behavior. No new lifecycle status — reuses `FAILED`, the same terminal state every other failure path already uses. `tests/test_reconciliation.py` grew from 23 to 25 tests (one existing test updated in place to assert the corrected outcome, two added for the retryable/terminal split); full suite now 596 passed, no regressions. Evaluation doc updated in place with a dated correction section; original evidence not rewritten. See `docs/evaluations/scheduling/milestone-2.1.10-asynchronous-publish-reconciliation.md`.

### Milestone 2.1.10 — Asynchronous Publish Reconciliation

Closed the last manual step Milestone 2.1.9's live validation exposed: a real TikTok submission still `PROCESSING_UPLOAD` after the worker's one inline poll previously needed a human to rerun `publish_tiktok.py --poll-only`. Added `reconciliation.py` — a one-pass module, mirroring `crash_recovery.py`'s existing separate-CLI shape (not folded into `worker.py`, which is untouched by this milestone), that automatically re-checks every `PUBLISHING` `platform_posts` row TikTok has already accepted (`platform_post_id` set) on a capped backoff schedule (`config.STATUS_CHECK_BACKOFF_SECONDS`, default 30s/60s/2m/5m/10m, indexed by a new `status_check_count` column and capped — not exhausted — at the last interval, since there's no retry-budget equivalent to burn). A new `ContentStore.get_reconcilable_platform_posts` selector stays deliberately separate from due-post selection (still PENDING-only) and from crash recovery's staleness-gated safety net. Found and removed a real pre-existing duplication in scope: `publish_tiktok.py`'s `--poll-only` path and `crash_recovery.py`'s Case B independently maintained the same TikTok-status-to-DB-state mapping; extracted into a shared `publish_tiktok._resolve_poll_outcome()`, now used by both plus the new reconciliation module — a verified behavior-preserving refactor (existing tests pass unchanged). Reconciliation never calls `publish()` — structurally guarded and tested. Every write uses the existing `update_platform_post_if_unchanged` optimistic-concurrency primitive, so two concurrent reconciliation passes, or a reconciliation pass racing crash recovery over the same row, can't corrupt it — verified directly with real threads. Two integration tests prove composition with Milestone 2.1.8's token lifecycle (a stale cached token silently refreshes mid-reconciliation; a revoked refresh token surfaces without triggering a resubmission). New columns `next_status_check_at` (aware UTC — deliberately the opposite convention from `scheduled_at`'s naive-local time, since this field never shares a query with `scheduled_at` and needs to compare against `updated_at`/`published_at` instead) and `status_check_count`, via the existing additive-migration pattern. `tests/test_reconciliation.py` (23 new tests) plus 4 outside-pytest local simulations (processing-then-complete, repeated-processing-with-increasing-backoff, terminal-failure, transient-status-API-failure) all passed. Real `data/content.db` migration applied cleanly; the row Milestone 2.1.9 published live is confirmed byte-identical and untouched. Full suite: 594 passed, no regressions. No live TikTok calls were made by this milestone's implementation work. Full details in `docs/evaluations/scheduling/milestone-2.1.10-asynchronous-publish-reconciliation.md`; a short follow-up note was added to the 2.1.9 record without rewriting its live evidence.

Milestone 2.1.10: **COMPLETE**.

### Milestone 2.1.9 — Real Unattended TikTok Scheduled Publishing Validation

Closed Milestone 2.1 with the final live acceptance test: a real scheduled post moved through the entire production path — due detection, atomic claiming, automatic token refresh, real TikTok submission, and terminal persistence — without a human manually invoking the publish command at execution time. With explicit confirmation before any live-mutating step (a real credential refresh, a real DB schedule adjustment, and a real publish are each hard to reverse), selected video 3 (never previously attempted, media/caption/codec preconditions all clean) and temporarily moved its `platform_posts.scheduled_at` from 2026-09-19 to ~5 minutes out (original value preserved in the evaluation record; no other row touched; DB backed up first). `TikTokPublisher.query_creator_info()` against the live API triggered and completed a real token refresh (the access token was genuinely expired) — the first live exercise of Milestone 2.1.8's refresh/lock path outside deterministic tests. A background wait crossed the real scheduled time, then the unmodified `worker.py` CLI (no injected `now`) discovered, claimed, and submitted the post for real; TikTok's Sandbox processing was still `PROCESSING_UPLOAD` after the worker's single poll — a real timing case no mocked test had produced — and resolved to `PUBLISHED` via the existing `--poll-only` re-check mechanism (status-only, never resubmits). Final row: `status=PUBLISHED`, `scheduled_at` byte-identical to the adjusted value, real `platform_post_id`, lateness of 2m06s computed by the real `calculate_schedule_delay` helper. A second worker pass afterward discovered nothing and left the row completely untouched — no duplicate submission, no duplicate row. No code changes were required; full suite (571) passed unchanged before and after. Full details, including the one real limitation (this codebase has no "list my posts" API, so visual duplicate-checking on the TikTok app itself is a manual spot-check, not programmatically verified here) in `docs/evaluations/scheduling/milestone-2.1.9-real-unattended-tiktok-validation.md`.

Milestone 2.1.9: **COMPLETE**. Milestone 2.1 (TikTok scheduled-publishing reliability) is closed.

### Milestone 2.1.8 — TikTok Token Lifecycle and Automatic Refresh

Made TikTok authorization reliable over long-running scheduled publishing so a creator can batch content days in advance without reopening Pickle Batch just because the short-lived access token expired. Investigation first: `tiktok_auth.get_access_token()` already was the one centralized token-manager contract every TikTok API call goes through (`TikTokPublisher._headers()`), already persisted absolute aware-UTC expiry timestamps, already had a refresh safety skew, and already handled TikTok's refresh-token rotation correctly (confirmed against TikTok's current OAuth docs: 24h access tokens, 365-day refresh tokens, "must use the newly-returned refresh token if different"). Found two real, previously-invisible gaps instead of building from scratch: (1) every TikTok-auth failure — a transient network blip reaching the token endpoint, a temporary 5xx, and an actually-revoked/expired refresh token — collapsed into the same hardcoded `PublishError(reason_code="AUTH_ERROR")`, unconditionally terminal per `retry_classification.py`, so a transient refresh failure burned zero retry budget and failed the post immediately, identical to a genuinely revoked token; (2) no protection existed against two separate worker processes concurrently refreshing the same soon-to-rotate token. Fixed (1) by giving `TikTokAuthError` the same structured `reason_code`/`http_status` shape `PublishError` already has, and adding a `TikTokReauthorizationRequiredError` subclass (`reason_code="REAUTHORIZATION_REQUIRED"`, added to `retry_classification.py`'s terminal list) for the cases that genuinely require re-running `--authorize` — classified by HTTP status since TikTok's docs don't publish a distinct error code for a revoked refresh token specifically. Fixed (2) with an `fcntl.flock`-based lock (`tiktok_auth._refresh_lock()`) around the read-refresh-persist sequence, with a compare-and-reload check after acquiring it — deliberately a process/host-local lock, matching this repository's single-host CLI architecture, not distributed coordination. Moved the refresh skew from a hardcoded constant into `config.TIKTOK_TOKEN_REFRESH_SKEW_SECONDS` (env-overridable, default 300s), matching the existing `RETRY_BACKOFF_MINUTES` pattern. `tests/test_token_lifecycle.py` (20 new tests) proves the classification split, concurrent-refresh protection (5 real threads, exactly one network call), and both retry-composition outcomes end-to-end through the real worker (`worker.run_due_posts_once`). While authoring one of those tests, caught and fixed a genuine false-positive in the test itself (an unmocked `media.inspect_media` meant it was actually exercising a real ffprobe `CORRUPT_MEDIA` failure, and a loose substring assertion happened to match text leaked from pytest's own tmp-path naming) — fixed by mocking media inspection and asserting the specific exception message instead. Also caught and fixed a real test-isolation gap: the new `TIKTOK_REFRESH_LOCK_PATH` constant wasn't yet redirected to `tmp_path` in the existing `test_tiktok_auth.py` fixture, which left a stray empty lock file in the real `~/.config/content-calendar/` directory on one run — cleaned up and the fixture fixed before proceeding. Four outside-pytest local simulations (silent refresh, rotation, revoked authorization, concurrent refresh) all passed. Real token cache inspected read-only (metadata only, no secret values ever printed) — access token is currently expired, refresh token valid until 2027; live refresh was deliberately deferred to Milestone 2.1.9 rather than mutating production credentials without prior explicit authorization in this conversation, per the milestone's own guardrail. Full suite: 571 passed, no regressions. Full details in `docs/evaluations/scheduling/milestone-2.1.8-token-lifecycle.md`.

Milestone 2.1.8: **COMPLETE**.

### Milestone 2.1.7 — Missed-Schedule Behavior

Answered what happens when a `PENDING` post's `scheduled_at` has already passed before a worker gets to run it (offline, crashed, deploy window). Investigation first, per the brief: traced `due_post_selector.py`, `content_store.get_due_platform_posts`, `worker.py`, `crash_recovery.py`, and `retry_classification.py`, and confirmed the policy already existed as an emergent property — `scheduled_at <= now` has no upper bound, `scheduled_at` is structurally write-once (grepped every write site; only ever set at materialization, never rewritten), retry timing (`next_retry_at`) composes independently via its own gate, and `published_at` already existed in the schema and is written at real execution time. No new status, no new persisted flag, and no worker/selector logic changes were needed. Added a pure `due_post_selector.calculate_schedule_delay(scheduled_at, published_at)` helper for on-demand lateness derivation — building it surfaced a real pre-existing bug: `scheduled_at` is naive local time but `published_at` is aware UTC, so a raw subtraction raises `TypeError` (or would silently misreport lateness by the UTC offset if tzinfo were stripped instead); fixed by normalizing `published_at` into `config.TIMEZONE` inside the helper, scoped to that helper only. `tests/test_missed_schedule.py` (13 new tests) proves overdue due-selection at the brief's actual scale (1 minute, hours, days — prior tests only covered 1 hour), the retry-gate/overdue composition at day-scale, and an end-to-end worker run against a 3-day-overdue post proving `scheduled_at` stays untouched while `published_at` reflects the late execution time. Three outside-pytest local simulations (simple offline overdue, retry-gated overdue, multi-day overdue) all passed against isolated temp SQLite DBs. Real `data/content.db` read read-only via `sqlite3 -readonly` (not `ContentStore()`, to guarantee no write path could run) — all 5 rows byte-identical to Milestone 2.1.6's recorded state; none of the real `PENDING` rows are yet overdue as of today, so the guardrail (no real row touched) was never at risk. Full suite: 551 passed, no regressions. Full details in `docs/evaluations/scheduling/milestone-2.1.7-missed-schedule-behavior.md`.

Milestone 2.1.7: **COMPLETE**.

## 2026-09-17

### Milestone 2.1.6 — Retry Classification and Backoff

Added `retry_classification.py` — a pure predicate classifying a `PublishError` as retryable (transient) or terminal (permanent), using the existing `reason_code` field (`NETWORK_ERROR`/`UPLOAD_NETWORK_ERROR` retryable; `CAPTION_TOO_LONG`, `AUTH_ERROR`, and five other codes terminal) plus a new structured `http_status` field on `PublishError` for any unrecognized/dynamic code (`5xx` retryable, `4xx` and "no status at all" terminal — fail-closed by default). `platform_posts` gained `retry_count`/`next_retry_at` columns via the same additive migration pattern `videos` already used. A centrally configured backoff policy (`config.RETRY_BACKOFF_MINUTES = [1, 5, 15, 30]`, env-overridable; `config.MAX_RETRY_ATTEMPTS` derived from its length) governs `publish_tiktok._schedule_retry_or_fail()`: a retryable failure under the ceiling returns the row to `PENDING` with an incremented `retry_count` and a future `next_retry_at`, re-entering the normal `claim_platform_post()` ownership path exactly like any other due work; a terminal failure, or a retryable one that has exhausted its budget, ends `FAILED` with `failure_reason` preserved. `ContentStore.get_due_platform_posts()` now also requires `next_retry_at IS NULL OR next_retry_at <= now` so a row waiting out its backoff window isn't claimed early — ordering stays `scheduled_at`-first, deliberately never reordered by retry timing. `worker.py`'s one-pass loop already isolates one claimed post's failure from the rest of the batch; confirmed by test that a retryable or terminal failure never aborts the pass. Found and fixed a real pre-existing bug in scope: an uncaught local-validation failure inside `execute_claimed_platform_post()` could leave a claimed row stuck `PUBLISHING` forever with no `failure_reason` — now caught and marked `FAILED` immediately. A human's manual CLI rerun of a `FAILED` row now resets `retry_count`/`next_retry_at` to zero, since a manual retry is a fresh attempt independent of the automatic budget. `platform_post_id`-bearing rows remain unconditionally exempt from retry classification — only ever polled, never resubmitted. All three local validation scenarios (transient-failure-then-success, terminal-failure-never-retried, retry-exhaustion) reproduced against a temp SQLite DB with `FakePublisher`; real `data/content.db` confirmed the migration applied with sane defaults (`retry_count=0`, `next_retry_at=NULL`) on all 5 existing rows, no other field mutated, no TikTok API calls made. Full details in `docs/evaluations/scheduling/milestone-2.1.6-retry-backoff.md`; the 2.1.5 record was updated with a dated completion note on the (orthogonal) interaction with crash recovery.

Milestone 2.1.6: **COMPLETE**.

### Milestone 2.1.5 — Crash Recovery for Interrupted Publishing Jobs

Added `crash_recovery.recover_stale_posts_once()` — one-pass recovery for `platform_posts` rows stuck `PUBLISHING` after a worker crash, closing the gap 2.1.4 explicitly deferred. Two crash classes handled differently: claimed-but-unsubmitted (`platform_post_id IS NULL`) is requeued to `PENDING` so the normal `claim_platform_post()` path can pick it up again; submitted-but-unresolved (`platform_post_id` set) is only polled via TikTok's existing status endpoint, never resubmitted — Milestone 2.0's idempotency rule holds unconditionally, verified by test. Staleness is `updated_at` older than `config.PLATFORM_POST_STALE_MINUTES` (default 30, env-overridable) — no new lease/heartbeat column needed. Added a general optimistic-concurrency primitive, `ContentStore.update_platform_post_if_unchanged()`, used for every recovery write so recovery can only ever act on a row that is still the exact stale record it inspected; verified directly against a simulated after-selection race, not just reasoned about. Both crash scenarios from the brief reproduced and recovered correctly via the real module entry point, outside pytest. Full details in `docs/evaluations/scheduling/milestone-2.1.5-crash-recovery.md`; the 2.1.4 record was updated with a dated completion note.

Milestone 2.1.5: **COMPLETE**.

### Milestone 2.1.4 — Worker Execution

Added `worker.run_due_posts_once(store, publisher, platform="tiktok", now=None)` — a one-pass worker connecting `due_post_selector` (2.1.1/2.1.2), `ContentStore.claim_platform_post()` (2.1.3), and the real TikTok publish flow (2.0). Extracted `publish_tiktok.execute_claimed_platform_post()` so both the manual CLI and the worker share exactly one publishing implementation. Reconciled `publish_tiktok.py`'s `publish_video()` to claim through `claim_platform_post()` instead of a plain unconditional status write — `claim_platform_post()` is now the single supported `PENDING -> PUBLISHING` mechanism repository-wide, closing the overlap 2.1.3 found and deferred. Proven with a real two-thread, two-connection test running the full discover→claim→execute pipeline (not just the raw claim primitive): exactly one publish, every time, across 10 isolated reruns. A local validation ran the actual worker entry point end to end (discovered=1, claimed=1, published=1) with no human invoking the publish flow. Full details in `docs/evaluations/scheduling/milestone-2.1.4-worker-execution.md`; the 2.1.3 record was updated with a dated completion note.

Milestone 2.1.4: **COMPLETE**.

### Milestone 2.1.3 — Atomic Platform-Post Claiming

Added `ContentStore.claim_platform_post(post_id, updated_at)` — a single atomic `UPDATE ... WHERE id = ? AND status = 'PENDING'` conditional mutation that transitions a row `PENDING -> PUBLISHING`, with success read from `rowcount` rather than a separate `SELECT`-then-`UPDATE` (which would allow two concurrent claimants to both observe `PENDING`). Proven with a real two-thread, two-connection concurrency test (`threading.Barrier`-synchronized), re-run 10 times in isolation with no flakiness: exactly one winner every time. `due_post_selector.get_due_posts()` unchanged — still read-only. Found and documented (not fixed) a real pre-existing overlap: `publish_tiktok.py` already performs its own non-atomic `PENDING -> PUBLISHING` transition; reconciling the two into one ownership mechanism is deferred to a future worker-execution milestone. Full contract, atomicity reasoning, and the publisher-compatibility finding recorded in `docs/evaluations/scheduling/milestone-2.1.3-atomic-platform-post-claiming.md`.

Milestone 2.1.3: **COMPLETE**.

### Milestone 2.1.2 — Scheduled Platform-Post Materialization + Pending-Only Due State

Closed the architectural gap Milestone 2.1.1 reported: `platform_post_materializer.materialize_platform_posts_for_assignment()` now creates a `PENDING` `platform_posts` row (via a new idempotent `ContentStore.insert_platform_post_if_missing()`, mirroring `insert_slot_if_missing`) immediately when a video is assigned a `content_slot` (`process_content.py`), instead of only when `publish_tiktok.py` is manually run. Corrected `due_post_selector.ELIGIBLE_STATUSES` from `["PENDING", "PUBLISHING"]` to `["PENDING"]` — `PUBLISHING` means already claimed/in progress, not eligible for initial execution. One-time `backfill_platform_posts.py` materialized the 3 real videos (`id`s 3, 4, 5) already assigned before this milestone existed, without touching the 2 existing rows. Full contract, lifecycle semantics, idempotency verification, and real-DB backfill results recorded in `docs/evaluations/scheduling/milestone-2.1.2-platform-post-materialization.md`; the 2.1.1 record was corrected in place with a dated note.

Milestone 2.1.2: **COMPLETE**.

### Milestone 2.1.1 — Due-Post Detection

Added `due_post_selector.get_due_posts(store, platform, now=None)` (backed by a new `ContentStore.get_due_platform_posts()` query method) — deterministic, read-only selection of which `platform_posts` rows are due for execution right now. No worker, claiming, retries, or publishing side effects — selection logic only, mirroring `slot_matcher.py`'s existing shape. Full contract, time-semantics reasoning, and a reported (not fixed) architectural gap in `platform_posts` row creation timing are recorded in `docs/evaluations/scheduling/milestone-2.1.1-due-post-detection.md`.

Milestone 2.1.1: **COMPLETE**.

### Milestone 2.0 — First Real Live TikTok Publish Validated

Validated the full real TikTok Direct Post publishing path end-to-end against the live API for the first time, via the existing `publish_tiktok.py` CLI — no new/temporary script, no architecture changes, no code changes required. Full validation evidence (test subject, execution path, persistence and idempotency verification) recorded in `docs/evaluations/tiktok/milestone-2.0-live-publish-validation.md`.

Milestone 2.0: **COMPLETE**.


### Fix TikTok Desktop PKCE Challenge Encoding

`tiktok_auth.py`'s token exchange failed with `invalid_request: Code verifier or code challenge is invalid` after a successful browser authorization/callback. Root cause: `generate_pkce_pair()` derived `code_challenge` as `base64url(SHA256(verifier))` (RFC 7636's standard S256 encoding, and what most other OAuth providers use), but TikTok's Desktop Login Kit documentation requires `code_challenge` as the **lowercase hex digest** of `SHA256(verifier)` instead — confirmed directly against TikTok's current docs, not just the reported error. The verifier itself, its lifecycle (never regenerated between building the authorization URL and the token exchange, in both the interactive and manual-fallback flows), and the token exchange's `code_verifier` field were all already correct.

- `tiktok_auth.py`: `generate_pkce_pair()` now computes `challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()` instead of base64url-encoding the digest. Removed the now-unused `base64` import. No other function changed — verifier generation, state/CSRF handling, token exchange, refresh, and persistence are untouched.
- `tests/test_tiktok_auth.py`: updated the two tests that encoded the old base64url assumption (challenge length 43 → 64, comparison against `base64.urlsafe_b64encode` → `hashlib...hexdigest()`), added an explicit hex-charset assertion, and added `test_authorize_interactive_sends_same_verifier_that_produced_the_challenge` — an end-to-end guard proving the exact verifier used to build the challenge on the authorization URL is the same one presented at token exchange, and that it's the correct SHA256 preimage of that challenge.

Verification: `pytest tests/test_tiktok_auth.py` (35 passed) and the full suite (`pytest`, 427 passed). Not yet verified against TikTok's real token endpoint — a fresh `python3 tiktok_auth.py --authorize` run follows this fix, per the existing "never reuse a verifier/challenge across attempts" contract.

### Public Brand Rename — Content Automation → Pickle Batch (web frontend only)

Renamed the public-facing SaaS/product brand from "Content Automation" to "Pickle Batch" across `web/`. The backend/internal engineering name (this repository, Python modules, database tables, CLI scripts, ADRs/history, this changelog) stays "Content Automation" — the two names now deliberately diverge: internal engineering name vs. public product name.

- `web/lib/site-config.ts`: `name` and `description` updated — this is the single source `web/app/layout.tsx`'s page-title template and meta description already read from, so title/metadata changed automatically with no separate edit.
- Replaced 8 literal `"Content Automation"` strings that were hardcoded directly in JSX/data instead of sourced from `siteConfig.name` — `Hero.tsx`, `CTASection.tsx`, `PlatformDirection.tsx`, `HowItWorks.tsx`, and 4 occurrences in `app/privacy/page.tsx` — with `{siteConfig.name}` (or, for `Hero.tsx`'s description paragraph, which was a verbatim duplicate of `siteConfig.description`, with `{siteConfig.description}` directly, removing the duplication rather than just fixing the brand word in both places). `app/terms/page.tsx` already sourced every mention from `siteConfig.name` and needed no changes.
- `SiteHeader.tsx`/`SiteFooter.tsx` already read `siteConfig.name` and needed no changes.
- Deliberately left unchanged: `siteConfig.url` and `siteConfig.contactEmail` (infrastructure — domain/email — not copy; changing either without confirming the actual domain/mailbox exists could point the site at something nonexistent) and root `README.md`'s "Content Automation" mentions (internal engineering documentation describing this repository, not site visitor-facing copy — same category the task explicitly said to leave alone). No favicon/logo text existed to update — `app/favicon.ico` is a generic icon with no embedded brand text.

Validation: `npm run lint` (clean) and `npm run build` (Next.js 16.3.5, Turbopack — compiles, typechecks, and statically exports all 4 routes successfully). Confirmed directly in the static `out/` output: zero remaining occurrences of "Content Automation" in any rendered HTML, "Pickle Batch" appears correctly on every page including `<title>Pickle Batch</title>` on the homepage.

### Milestone 2.0.1 — Content Automation Web Presence / SaaS Frontend Foundation

Added the first public-facing website, inside this repository rather than as a separate throwaway compliance site — it provides real public URLs for TikTok Developer Portal configuration now, and is meant to become the foundation of the full SaaS UI later.

- Added `web/`: a Next.js 16 (App Router, TypeScript, Tailwind CSS v4) app, scaffolded and structured per this session's global Next.js convention (`components/ui`/`components/layout`/`components/sections`/`lib/`) rather than a bespoke structure. Own `package.json`/toolchain, kept logically separate from the Python backend — no shared code, no imports either direction.
- Three real, routable pages: `/` (homepage), `/privacy`, `/terms`. Static export (`next.config.ts`: `output: "export"`, `trailingSlash: true`) — no server-rendered/dynamic routes exist yet, so no Netlify Next.js runtime plugin or serverless functions were introduced.
- Homepage sections: Hero, How it works (Upload → process → fill schedule → publish, as specified), What it does today (batch workflow, automatic scheduling, transcript generation, caption preparation, FIFO queue), Where publishing is headed (TikTok "In development", Instagram/YouTube Shorts "Planned"), and a contact CTA. Copy describes only what exists or is genuinely in progress — no unsupported feature claims.
- Provisional V1 design system, centralized in exactly two places: `web/app/globals.css`'s `:root`/`@theme` block (neutral light background/surface/ink/border + one restrained accent color, `#4338CA`) and `web/lib/site-config.ts` (site name/tagline/description/contact email). Every component references tokens via Tailwind utilities — no hardcoded hex values. Mobile-first responsive throughout, including a client-side mobile nav toggle.
- Real Privacy Policy and Terms of Service, proportionate to an early-stage/testing-phase product (not enterprise-length): what data is accessed when connecting TikTok/Google, that OAuth tokens are stored securely for that purpose, video/transcript/caption processing, no data sale, third-party processing under TikTok's/Google's own policies (with links), a deletion-request contact path, and explicit "in active development" framing. **`lib/site-config.ts`'s `contactEmail` is a placeholder (`support@content-automation.app`) and must be replaced with a real, monitored address before public deployment** — both legal pages promise it as a real contact method.
- `netlify.toml` added at the repository root (`base = "web"`, `command = "npm run build"`, `publish = "out"`, pinned `NODE_VERSION`) — scopes Netlify's build to `web/` only, since the repo root also holds the unrelated Python backend.
- Future authenticated SaaS routes (`/app/calendar`, `/app/uploads`, `/app/settings`, `/app/integrations`) were deliberately not started, not even as placeholders — judged unnecessary for this milestone; the `app/` structure already accommodates adding them later.
- Updated root `README.md` (new "Public Website" section), `PROJECT_STATE.md` (new "Public Website / Frontend Foundation" section + Directory Ownership entry). No ADR added — the stack/structure choices follow an existing global convention directly rather than introducing a new durable decision.

Validation: `npm run build` compiles cleanly (Next.js 16.3.5, Turbopack) and `npm run lint` reports no issues; all three routes plus the default not-found page export as static HTML. The static `out/` output was served locally and checked directly: all three real routes return HTTP 200 with correct titles/content, footer navigation and the contact `mailto:` link render correctly, and a nonexistent path correctly 404s. **Not verified live**: an actual Netlify deployment — no Netlify account/site was created in this environment. The config is correct per Next.js's documented static-export guidance, but the real deployed URLs are unconfirmed. That, and replacing the placeholder contact email, remain the user's manual steps before using the resulting URLs in TikTok Developer Portal / Sandbox configuration.

### Repository Relocation — `content-calendar` → `content-automation`

Repository moved and renamed:

```
~/growth_agency/internal-tools/content-calendar  →  ~/content-automation
Content Calendar (project name)                  →  Content Automation
```

Investigated before changing anything, per an explicit classify-first requirement: every reference to the old path/name was checked and put into one of four buckets — runtime path needing a fix, persisted state needing migration, current-facing name reference to update, or intentional historical/unrelated reference to leave alone. Nothing was blindly replaced.

- **Git verified intact**: `git status` clean, `git rev-parse --show-toplevel` resolves to the new directory, `git log` history unaffected. A pre-existing `origin` remote (`github.com/Malik526/content-calander.git`) is untouched — not renamed, not pushed to.
- **`.venv` was broken by the move and recreated, not patched.** Venvs embed an absolute path at creation time; `bin/pip`/`bin/pytest` failed outright ("bad interpreter"), and `source .venv/bin/activate` silently fell through to the system Python rather than erroring — `python3 -m pytest` had appeared to keep working through the move only because this machine's system Python coincidentally already had every dependency installed, not because anything was actually fine. Fixed with the standard remedy: delete and recreate from `requirements.txt`/`requirements-eval.txt`.
- **`data/content.db` was the real, correctly-flagged risk.** All 5 real Shofo videos' `videos.canonical_media_path` and `videos.original_path` were still absolute paths under the old repository location. Added `migrate_relocated_paths.py`, a narrow one-time repair: rewrites only a stored value that is exactly the old root or rooted under it (never a value that merely shares a text prefix, e.g. a sibling `content-calendar-archive` directory) to the equivalent path under the current repo root (derived from the script's own location, never hardcoded); `canonical_media_path` is verified to exist at the new location before being rewritten (skipped with a clear reason otherwise, independent of the row's other column); `original_path` is remapped without that check, since it's a stable historical identity key, not a live file reference, and by the time a video is `ASSIGNED` its incoming-file has already moved to `content/processed/` by design. Idempotent; never touches the database file itself, only targeted `UPDATE`s. Applied for real: 5 rows repaired, then confirmed a rerun reports nothing left to do. Every other table/field was checked and found to hold no absolute repository paths at all (`content_slots`, `platform_posts`, `data/calendar_state.json`, and `evaluation/video_pipeline/*.jsonl` — the last already relative by original Milestone 1.3.1 design).
- **`~/.config/content-calendar/` and `~/.cache/content-calendar/fastembed` were deliberately left unrenamed.** They live outside the repository and never depended on its location (a fixed literal path, not derived from `__file__`), so the move didn't break them; both real Calendar OAuth files were confirmed still present and readable. Renaming them would risk losing the already-completed Calendar OAuth consent for zero functional benefit. Every doc/config reference to this real, unchanged path (`config.py`, `.env.example`, `README.md`, ADRs, prior `CHANGELOG.md` entries) was left as-is — correct, not missed.
- **Project naming updated wherever the project itself is named**: `README.md`/`PROJECT_STATE.md`/`CHANGELOG.md` titles, `.env.example`'s header, `generate_calendar.py`'s module docstring (which had also, unrelatedly, mis-attributed itself to "MoreClientsCo" — corrected while already touching that line) and its printed schedule-summary headers, `config.APP_CALENDAR_DESCRIPTION` (cosmetic only — doesn't retroactively rewrite the already-existing live Google Calendar's description), and one prose reference in `download_shofo_samples.py`. ADR historical narrative under `docs/decisions/` describing the tool by its name **at the time each decision was made** was deliberately left unchanged. Module/table names containing "calendar" (`generate_calendar.py`, `calendar_manager.py`, `content_slots`, Google Calendar itself) were not renamed.
- Added `tests/test_migrate_relocated_paths.py`: the pure path-matching logic (in/out of scope, exact-root, already-migrated no-op, similarly-prefixed-sibling rejection) and the full repair against a real `ContentStore` (both columns, all other fields/relationships preserved, missing-target-file skip, untouched-if-never-under-old-root, idempotency, `--dry-run`, multiple independent rows).
- No ADR added — a relocation/repair, not a new durable architectural decision.

Validation: `python3 -m pytest` (425 passed, up from 409). Verified live: real dry-runs of `generate_calendar.py --dry-run` and `process_content.py --dry-run` from the new location print the corrected header text; `data/content.db` inspected directly post-repair — all 5 videos' `canonical_media_path` resolve to real files under the new `content/processed/`, all 5 `content_slots` `ASSIGNED` rows (with real Google Calendar event IDs) and the 1 `platform_posts` row remain correctly linked, `PRAGMA foreign_key_check` clean, and a rerun of the migration script confirmed idempotency. Google Calendar OAuth and TikTok token paths resolve correctly with no secret content printed. No new TikTok post was made as part of this task.

### Milestone 2.0 Correction Pass — TikTok Desktop OAuth (PKCE) + Publishing Boundary Validation

Corrects the initial Milestone 2.0 implementation against TikTok's current official Desktop Login Kit / Direct Post documentation, without redesigning the publisher architecture. No scheduled publishing, workers, object storage, or frontend work started.

- **Corrected TikTok desktop OAuth**: the original implementation assumed TikTok's OAuth doesn't support a useful localhost/loopback flow and shipped manual-only. That assumption was wrong — TikTok's current Desktop Login Kit documentation supports `localhost`/`127.0.0.1` redirect URIs (including a wildcard port) and mandates PKCE for desktop apps. `tiktok_auth.py` now has a preferred `--authorize` flow: fresh cryptographically random `state` + fresh PKCE `code_verifier`/S256 `code_challenge` per attempt (never reused, never persisted) → temporary localhost callback server (OS-assigned port, or the exact host/port from `TIKTOK_REDIRECT_URI` if fixed-port registration is required) → prints/opens the authorization URL → blocks for the redirect → verifies `state` matches exactly (constant-time) before exchanging anything → exchanges the code with `code_verifier` → caches the token. The manual two-command fallback (`--print-auth-url` / `--exchange-code <code> --state <state>`) remains, now also PKCE + state-validated, with the pending verifier/state cached only transiently between the two commands and deleted immediately after use, success or failure.
- **Tightened unaudited-client privacy handling**: this app hasn't completed TikTok's review, and TikTok restricts unaudited Direct Post clients to `SELF_ONLY` regardless of what else an account's `creator_info` reports. `TikTokPublisher(unaudited=True)` (the default) now requires `privacy_level == "SELF_ONLY"` (checked locally, before any network call) *and* requires `"SELF_ONLY"` to actually appear in `creator_info`'s `privacy_level_options` (`SELF_ONLY_UNAVAILABLE` if not — an empty/absent list is no longer treated as "unrestricted"). `unaudited=False` preserves the prior, more permissive check for a future audited client.
- **TikTok-bound caption length validation**: TikTok's 2200-unit Direct Post caption limit is defined in UTF-16 code units, not Python characters (a non-BMP character, e.g. many emoji, is 1 Python character but a 2-unit UTF-16 surrogate pair). Added `tiktok_publisher._utf16_length()` and a pre-network-call check that fails with `CAPTION_TOO_LONG` rather than silently truncating — `videos.caption_text` stays canonical and untouched either way; this was the simpler of the two options considered (fail vs. derive a separate truncated value).
- **Creator-specific duration enforcement**: `creator_info`'s `max_video_post_duration_sec` is now checked against the video's real duration via `media.inspect_media()` (reused — no second media-inspection path), rejecting with `VIDEO_TOO_LONG` before `init` if exceeded; a capability that isn't reported doesn't block everything.
- Fixed a real ordering bug caught by the new tests: the unaudited privacy-level mismatch check originally ran *after* `query_creator_info()`, meaning a test with no network mock made a real HTTP call to TikTok's servers (and got a real "access_token_invalid" response back). Moved the pure local check before any network call, since it needs no server response to evaluate.
- Updated `docs/decisions/0006-tiktok-publisher-foundation.md` (correction note, rewritten OAuth section, new privacy/caption/duration subsections, updated Consequences/Guardrails/Current Implementation), `README.md` (corrected setup steps), `PROJECT_STATE.md`, `.env.example` (`TIKTOK_REDIRECT_URI` now optional, added `CONTENT_CALENDAR_TIKTOK_MAX_CAPTION_UTF16_UNITS`).
- Added `tests/test_tiktok_auth.py` coverage: PKCE verifier/challenge generation and freshness, state generation and freshness, the interactive flow exercised via a **real, non-mocked local HTTP round trip** (a background thread hits the actual bound callback URL as a browser redirect would) covering success, state-mismatch rejection, an OAuth error callback, and a timeout, plus the manual fallback's pending-cache lifecycle and state validation. Added `tests/test_tiktok_publisher.py` coverage: SELF_ONLY presence/absence under both unaudited modes, UTF-16 caption boundary exactly at/over 2200 (including a non-BMP-character case proving the counting method itself, not just the threshold, is correct), and creator-specific max-duration rejection/acceptance/not-reported. All TikTok network calls remain mocked; the loopback server in the OAuth tests is real but same-machine only.

Validation: `python3 -m pytest` (409 passed, up from 376). Verified live in this environment: a real localhost PKCE/state/callback round trip (fake credentials, mocked token endpoint) completed successfully end to end, and separately, that a corrupted/mismatched `state` is correctly rejected. Re-ran `publish_tiktok.py --video-id 1` against the real database and real (post-repair) processed file: the new caption-length check correctly caught real video 1's actual transcript-derived caption at 2522 UTF-16 units (over the 2200 limit) with `CAPTION_TOO_LONG`, persisted as a safely-retriable `FAILED`/`platform_post_id=NULL` record — a genuine, previously-undetected real-data finding. Also discovered and cleaned up an unrelated side effect from this session's own manual verification: an earlier ad-hoc sanity check of the interactive OAuth flow had written a fake token to the real `~/.config/content-calendar/tiktok_token.json` (isolation oversight in that one-off script, not a code defect); removed it, along with the platform_posts test row it produced. **Not verified live**: the actual TikTok Developer Portal app setup, a real redirect URI registration, and the real OAuth exchange/publish round trip against TikTok's servers — no TikTok developer credentials or dedicated test account were available in this environment.

### Milestone 2.0 — TikTok Publisher Foundation

Proves `one known local MP4 → connected TikTok test account → TikTok API → private post → poll/check result → persist publishing result` for exactly one manually-chosen video. Deliberately narrow: no scheduler, worker, retry engine, or second platform — see Non-Goals below.

- Added `platform_posts` table (`content_store.py`): one row per `(video_id, platform)`, `UNIQUE(video_id, platform)` schema-enforced idempotency, statuses `PENDING`/`PUBLISHING`/`PUBLISHED`/`FAILED`. Preserves the existing three-way separation (`videos` = content/media metadata, `content_slots` = scheduling assignment, `platform_posts` = external publishing state) — no TikTok-specific columns added to `videos`. Pure additive `CREATE TABLE IF NOT EXISTS`, wired into `ContentStore.__init__`. Added `get_video(id)`, `get_slot(id)`, `get_platform_post`, `insert_platform_post`, `update_platform_post`.
- Added `publisher.py`: platform-neutral `Publisher` interface (`publish`, `get_status`), `PublishError`/`PublishResult`/`PublishStatusResult`, and `build_publisher()` — mirrors the existing `Transcriber`/`ContentClassifier` interface-plus-factory pattern. Only `"tiktok"` registered; anything else raises `UnsupportedPlatformError` immediately. Instagram/YouTube deliberately not stubbed.
- Added `tiktok_publisher.py`: `TikTokPublisher`, implementing the real Content Posting API v2 `FILE_UPLOAD` flow — queries `creator_info` for the account's actual `privacy_level_options` before ever publishing (never assumes `SELF_ONLY` is offered), `init` → `PUT` raw bytes to the returned `upload_url` → `status/fetch` to poll. All TikTok-specific request/response parsing lives only here. **Implemented from TikTok's public API documentation, not verified against a live call** — no TikTok developer credentials were available in this environment (same constraint as ADR-0004's Google Calendar OAuth).
- Added `tiktok_auth.py`: TikTok OAuth, completely separate from Google Calendar's — different config vars (`TIKTOK_CLIENT_KEY`/`TIKTOK_CLIENT_SECRET`/`TIKTOK_REDIRECT_URI`), different cached token file (`~/.config/content-calendar/tiktok_token.json`, outside the repo), different scopes, no shared code. TikTok's OAuth has no loopback-server flow to rely on (unlike `calendar_manager.py`'s `InstalledAppFlow.run_local_server()`), so authorization is a deliberate manual two-command flow (`--print-auth-url` then `--exchange-code <code>`) rather than an automated approximation. `get_access_token()` refreshes transparently afterward.
- Added `publish_tiktok.py --video-id <id>` (`--privacy-level`, `--poll-only`): the standalone manual CLI — load video → verify local file exists and is TikTok-compatible (`media.is_tiktok_compatible`, reused) → verify a stored caption exists → publish → persist `platform_post_id` immediately (before polling, so a crash between submission and polling can't lose it) → poll → persist outcome. Not wired into `process_content.py` or any scheduled execution.
- **Idempotency**: once `platform_post_id` is set for a `(video, platform)`, `publish()` is never called again — including when status comes back `FAILED` (TikTok accepted the submission, then reported failure: a post-processing failure, not a submission failure, and the `publish_id` is preserved). Only a video that never obtained a `publish_id` at all (true submission failure — missing file, bad credentials, network/upload error) is safe to retry, reusing the same `platform_posts` row rather than inserting a second one.
- **Prerequisite fix, discovered while verifying this live**: `process_content.py`'s `videos.canonical_media_path` was set once at inspect time (the `content/incoming/` discovery path) and never updated when the file moved to `content/processed/` on assignment — silently stale for any consumer reading stored state instead of re-discovering the file, exactly what `publish_tiktok.py` does. `_move_file` now returns the destination path, folded into the same `update_video` call that sets `status="ASSIGNED"`. Real database repair: the 5 already-`ASSIGNED` Shofo videos' `canonical_media_path` values were corrected to their actual `content/processed/` locations.
- Added `docs/decisions/0006-tiktok-publisher-foundation.md`. Updated `README.md` (new "TikTok Publishing" section + file reference), `PROJECT_STATE.md` (new "TikTok Publishing" section, Directory Ownership, Main Execution Paths, Testing, External Services/Credentials, Non-Goals retitled to "through Milestone 2.0"), `.env.example` (TikTok config placeholders, no real values).
- `requests` is now a direct `requirements.txt` dependency (previously only transitive).
- Added `tests/test_publisher.py`, `tests/test_tiktok_auth.py`, `tests/test_tiktok_publisher.py`, `tests/test_publish_tiktok.py` (idempotency suite: republish-after-`PUBLISHED`, rerun-while-processing, rerun-after-TikTok-reported-failure, rerun-after-true-submission-failure, exactly-one-row-ever, `--poll-only`), plus `platform_posts` coverage in `tests/test_content_store.py` (persistence, uniqueness, pre-2.0-database migration, and a dedicated regression test confirming the existing `_repair_videos_assigned_slot_fk` fix also protects `platform_posts.video_id`'s FK from the same rename-corruption class of bug) and a `canonical_media_path` regression in `tests/test_fifo_process_content.py`. All TikTok network calls mocked — no real account required for the suite.

Validation: `python3 -m pytest` (376 passed, up from 297). Verified live in this environment up to the credentials boundary: `publish_tiktok.py --video-id 1` ran for real against the actual `data/content.db` and the real (post-repair) file at `content/processed/7000056250327174406.mp4` — correctly passed every precondition check, attempted a real HTTP call, and failed with the clear "no TikTok credentials" error, persisted as `platform_posts.status=FAILED`/`platform_post_id=NULL` (a true submission failure, safely retriable) with `scheduled_at` correctly carried over from the video's assigned slot. **Not verified live**: the actual TikTok Developer Portal app setup, the manual OAuth authorization flow, and a real `creator_info`/`init`/upload/`status`/private-post round trip — no TikTok developer credentials or dedicated test account were available here. These, plus confirming the resulting Google Calendar event, remain the user's manual steps.

### Milestone 1.3.1 Real-Video Walkthrough — Completed and Verified Live

Documentation-only update recording that the full Shofo real-video walkthrough (`evaluation/video_pipeline/README.md`) has now been completed successfully, end to end, in this environment. No code changed.

- **Acquisition**: 12/12 real Shofo clips downloaded successfully (`evaluation/video_pipeline/videos/`, `metadata.jsonl`).
- **Transcription**: real `evaluate_transcription.py` run against all 12 with the production `FasterWhisperTranscriber` — 12/12 scored, 0 errors, mean WER 7.2%, median WER 7.3% (`evaluation/video_pipeline/results.jsonl`; independently reconfirmed by recomputing directly from the file).
- **FIFO dry-run**: 5 of the 12 clips staged into `content/incoming/`; `process_content.py --dry-run --verbose` completed successfully for all 5 after the `videos.assigned_slot_id` FK migration repair (see the fix entry below).
- **FIFO real run**: `process_content.py --verbose` (no `--dry-run`) then completed successfully — 5 discovered, 5 assigned, 0 needs review, 0 waiting for slot, 0 failed. Each video was atomically assigned to a distinct future `content_slots` row: Sep 16, 18, 19, 21, 23 2026 (all 9:00 AM), confirmed directly in `data/content.db` (`status=ASSIGNED`, `transcription_status=COMPLETE`, `caption_source=transcript_auto`, `assigned_slot_id` correct on every video, `PRAGMA foreign_key_check` clean) and on disk (`content/incoming/` empty, all 5 originals moved to `content/processed/`).
- Confirms the complete real-media production path: media inspection → transcription → transcript-derived caption → SQLite persistence → earliest OPEN FIFO slot → atomic assignment → move to processed.
- **Scope preserved**: this validates the local ingestion/scheduling pipeline only. Confirming the resulting events in the dedicated "Content Automation" Google Calendar remains the user's manual step. TikTok (or any platform) publishing is still not implemented and remains the next major milestone — nothing in this walkthrough posts anywhere.
- Updated `PROJECT_STATE.md` ("Shofo Real-Video Evaluation Corpus") to replace the prior "still not verified" wording for real faster-whisper/real FIFO assignment with this confirmed-live status, narrowing the one remaining outstanding item to the Google Calendar check.

No ADR added; no architecture or code changed.

### Fix — SQLite Migration Left `videos.assigned_slot_id` Referencing a Dropped Temporary Table

The first real Shofo FIFO dry-run failed in `ContentStore.insert_video()`: `sqlite3.OperationalError: no such table: main.content_slots_old`. This was a pre-existing `content_store.py` schema-migration defect, not a FIFO, transcription, or Shofo-corpus issue.

Root cause, confirmed against the real database's actual schema (not assumed from the traceback): `_migrate_content_slots_unique_constraint` rebuilds `content_slots` via `RENAME TABLE content_slots -> content_slots_old` (SQLite has no in-place way to change a table's constraints). By default, modern SQLite's enhanced `ALTER TABLE RENAME` automatically rewrites any `REFERENCES` clause in *other* tables that pointed at the renamed table — verified empirically against a scratch database, not just asserted from documentation. That rename silently rewrote `videos.assigned_slot_id`'s FK to say `content_slots_old`; dropping that temporary table afterward left the FK dangling. `videos`' own stored `CREATE TABLE` SQL in the real database read `REFERENCES "content_slots_old"(id)` — confirmed via `PRAGMA foreign_key_list(videos)` and `SELECT sql FROM sqlite_master`. SQLite raises this error at DML statement-preparation time — even for a row that never sets `assigned_slot_id` (left `NULL`), exactly matching the real crash in a plain `INSERT INTO videos (...)` with no slot columns given.

- **Prevention**: `_migrate_content_slots_unique_constraint`'s rename now runs under `PRAGMA legacy_alter_table = ON`, which suppresses the cross-table FK rewrite entirely (verified empirically: without it, a sibling table's FK gets rewritten on rename; with it, the FK text is untouched). The whole rebuild (rename, recreate, copy, drop) now runs inside one `BEGIN IMMEDIATE`/`COMMIT`, rolling back cleanly on any failure — `PRAGMA foreign_keys`/`legacy_alter_table` are toggled before `BEGIN`/after `COMMIT` since SQLite treats `foreign_keys` as a no-op mid-transaction; `Connection.executescript()` (which implicitly commits a pending transaction) is no longer used inside the transaction, replaced with plain `execute()` for the single-statement schema DDL.
- **Repair**: added `_videos_fk_needs_repair()` (detects `assigned_slot_id`'s FK pointing at anything other than `content_slots`, via `PRAGMA foreign_key_list` — not string-matching the stale name) and `_repair_videos_assigned_slot_fk()` (rebuilds `videos` the same safe way, also under `legacy_alter_table = ON` so the *reverse* corruption — `content_slots.assigned_video_id` getting rewritten to `videos_old` — can't happen; copies every column by name via `PRAGMA table_info`, not a hardcoded list; verifies `PRAGMA foreign_key_check` is clean afterward and raises rather than silently leaving a broken database if not). Both run automatically the first time `ContentStore` opens a database, are no-ops on an already-correct one, and are idempotent on reopen.
- No FIFO semantics, transcription behavior, caption logic, or Google Calendar behavior changed. No ADR added — this corrects an existing migration's behavior rather than introducing a new migration strategy.
- Added regression coverage in `tests/test_content_store.py`: fresh current-schema DB (no repair runs), an old pre-Milestone-1.3 DB (content_slots rebuild no longer corrupts videos' FK as a side effect), a constructed already-broken DB (FK referencing a nonexistent `content_slots_old` while only `content_slots` exists — detected and repaired, every row/relationship preserved, `PRAGMA foreign_key_check` clean, `insert_video()` succeeds afterward), and idempotency on reopen.

Validation: `python3 -m pytest` (297 passed, up from 291). Verified the real database directly: `PRAGMA foreign_key_list(videos)` showed `assigned_slot_id -> content_slots_old` before, `-> content_slots` after; `PRAGMA foreign_key_check` clean both before and after (SQLite's `foreign_key_check` doesn't itself error on a FK naming a nonexistent table — only DML statement preparation does, which is what originally crashed); all 9 pre-existing `content_slots` rows and the (previously empty, since every insert had crashed) `videos` table preserved. Backed up `data/content.db` to `data/content.db.pre-fk-repair.backup` (gitignored) before applying the live repair. Reran `python3 process_content.py --dry-run --verbose` against the 5 real Shofo videos already staged in `content/incoming/`: all 5 discovered, transcribed, captioned, and matched to the earliest OPEN FIFO slot with 0 failures — no `content_slots_old` exception. Reran a second time to confirm the repair does not fire again and behavior is unchanged.

### Shofo Real-Video Transcription Benchmark — Milestone 1.3.1, Verified

Ran `evaluate_transcription.py` for real against all 12 real Shofo clips acquired in Milestone 1.3.1 (`evaluation/video_pipeline/`), using the actual production `FasterWhisperTranscriber` (`base` model, CPU) — no mocks, no synthetic fixtures.

- **12/12 clips transcribed successfully, 0 errors.**
- **Mean WER 7.2%** (median 7.3%, range 0.0%-13.5%) against the dataset's own WEBVTT reference transcripts — independently recomputed from `evaluation/video_pipeline/results.jsonl` directly (not taken on faith from the run's own printed summary), confirming the reported numbers.
- **Mean `realtime_factor` 0.083** — transcription took ~8% of each clip's duration on average (roughly 12x faster than real-time on CPU), indicating local faster-whisper is practical for this workflow at the `base` model size.
- Confirms the media/transcription leg of the Milestone 1.3.1 pipeline test (`video download → ffprobe inspection → audio extraction → faster-whisper transcription → transcript comparison`) end to end against real data, not just the mocked unit-level contract.
- `results.jsonl` (real transcripts/scores) stays gitignored per the corpus's existing rule — not committed.

Validation: real run of `evaluate_transcription.py` against the live corpus; results independently re-verified by recomputing mean/median WER and mean realtime_factor directly from `results.jsonl`'s raw rows rather than trusting the printed report. Updated `PROJECT_STATE.md` ("Shofo Real-Video Evaluation Corpus") to record this as verified live and narrow the remaining outstanding manual step to real FIFO/Google-Calendar assignment only.

## 2026-09-13

### Fix — download_shofo_samples.py Real Schema Mismatch (No `file_name` Column)

A real `--count 1` run after the torchcodec fix below completed with no decode error but reported **zero candidates passed filtering**. Investigated with one minimal authenticated, decode-disabled read of a real streamed row (no video download, no decode): the dataset has **no `file_name` column at all** — an assumption made from the dataset card, never verified, that silently filtered out every row. The downloadable reference actually lives inside the raw (non-decoded) `video` feature value as an `hf://datasets/<repo>@<revision>/<path>` URI. Every other assumed field name/type (`video_id`, `tiktok_url`, `duration_ms`, `resolution`, `width`, `height`, `fps`, `codec`, `bitrate`, `has_audio`, `language`, `transcript`) matched the dataset card exactly.

- `_disable_video_decoding()` no longer removes the `"video"` column (it only disables decoding on it) — the column is needed downstream to derive the file reference. With decoding off, its value is a small `{"path": ..., "bytes": None}` dict, not actual video bytes, so keeping it costs nothing extra.
- Added `_relative_path_from_video_field()`: parses the `hf://datasets/<repo>@<revision>/<path>` URI down to the plain repo-relative path `hf_hub_download(filename=...)` needs, rejecting a URI naming a different repo and passing through an already-relative path unchanged; returns `None` (never raises) for anything unusable.
- Added `normalize_row()` and an explicit `_DIRECT_FIELD_MAP`: the one place Shofo's real field names are translated to this project's internal manifest names (`transcript` → `reference_transcript`; everything else passes through 1:1; `file_name` is derived, not mapped). Every other function — `is_valid_candidate`, `stratify_sample`, `download_sample`, `build_manifest_record` — now operates only on this normalized shape; Shofo-specific naming no longer leaks past this one boundary.
- `is_valid_candidate()` still applies the same required filters (`has_audio=True`, `language=en`, a real `file_name`, a non-empty `reference_transcript`) with no loosening — a missing field and a validly-falsy field are both still correctly rejected, not conflated.
- Added `--verbose` to `download_shofo_samples.py`: prints the discovered raw source column names before filtering, so a future schema drift is visible immediately instead of silently producing zero candidates again.
- Documented the verified real schema in `evaluation/video_pipeline/README.md` ("Observed Schema"), including a second real finding from scanning the full metadata stream: **`has_music` is `False` for all 10,000 rows** in this dataset release (not a sampling bug — `stratify_sample`'s music alternation is a no-op today but will pick up `True` rows automatically if a future revision adds any).
- Added `tests/test_download_shofo_samples.py` fixtures/tests shaped like the real verified row (`_raw_shofo_row`) covering `_relative_path_from_video_field` (matching/mismatched repo, plain-path fallback, missing/malformed input) and `normalize_row` (full field mapping, derived `file_name`, missing-video-field handling), plus explicit missing-vs-falsy-value filter tests. No Torch/TorchCodec added; no production pipeline dependency changed.

Validation: `python3 -m pytest` (291 passed). Ran the real, live acquisition end to end in this environment: `--count 1 --verbose` succeeded (1 downloaded, 0 failed, real MP4 confirmed valid via `media.inspect_media()` — h264/aac, 576x1024, 25.5s); `--count 12 --seed 42` then succeeded (12 downloaded, 0 failed, 112.9MB total) with a clean 4/4/4 short/medium/long spread (all `has_music=False`, matching the now-documented real dataset property, not a bug).

### Fix — download_shofo_samples.py torchcodec Decode Error

`iter_dataset_rows()` failed before any row was yielded with `"To support decoding videos, please install torchcodec."` — a real run surfaced this immediately, contradicting the milestone's explicit requirement to never decode video or add a torch/torchcodec dependency during acquisition.

- Root cause: `getattr(ds, "features", {})` (used to decide whether to drop the `"video"` column) is not safe on a streaming `IterableDataset` whose schema isn't already known — accessing `.features` peeks one real example to infer types, and that peek decodes the `"video"` field, invoking the same decoder backend a full iteration would.
- Fix: added `_disable_video_decoding()`, which never touches `.features`. It calls `IterableDataset.decode(False)` — the documented, version-supported way to disable decoding for every Audio/Image/Video feature before any row is pulled — when available, falls back to explicitly recasting the `"video"` column to `Video(decode=False)` on older `datasets` versions without `.decode()`, and then drops the `"video"` column entirely either way (wrapped in try/except `ValueError` so a dataset/config without that column is a no-op, not a crash). No torch or torchcodec dependency was added; `requirements-eval.txt` bumped to `datasets>=3.0.0` (where `Video`/`.decode(False)` support is available) with a comment explaining why.
- Added `tests/test_download_shofo_samples.py::test_iter_dataset_rows_never_decodes_video` (and two focused `_disable_video_decoding` tests for the `.decode()` and `Video(decode=False)`-fallback paths) against a fake streaming dataset that raises the real error text the moment a `"video"` value is actually decoded — this is a genuine regression test, not just a shape check, and would have caught the original bug.

Validation: `python3 -m pytest` (282 passed). Manually ran `python3 download_shofo_samples.py --count 1` for real in this environment — `datasets` (5.0.1) and `huggingface_hub` were both already installed in `.venv` with a cached Hugging Face token in place (pre-existing local setup, not created by this fix), so this exercised the real streaming path end to end: iteration completed with no torchcodec error, confirming the fix. It reported zero candidates passed filtering, which is a separate, not-yet-investigated question (likely the real dataset's column names differ from the ones assumed from the dataset card) — flagged to the user rather than guessed at here, since it wasn't the reported bug and confirming it means another real call against the live gated dataset.

### Shofo Real-Video Evaluation Corpus — Milestone 1.3.1

Added a test/evaluation utility for exercising the pipeline against real short-form social video instead of only synthetic fixtures: video download → ffprobe inspection → audio extraction → faster-whisper transcription → transcript comparison → transcript-derived caption → FIFO assignment → Google Calendar scheduling. This is a mechanical pipeline test, not a production feature — it does not change scheduling behavior, and it explicitly does not evaluate pillar-classification accuracy (the sampled clips' topics are unrelated to `config.CONTENT_TYPES`).

- Added `download_shofo_samples.py`: pulls a small (~12, `--count`), reproducible (`--seed`, default 42), stratified sample of raw MP4s + reference metadata from the gated `Shofo/shofo-talking-head-en` Hugging Face dataset (~10k clips, ~104GB) — never the full dataset. Streams dataset metadata only, dropping the `video` feature column before iterating so no video is ever decoded during selection (no torch/torchcodec dependency); downloads only the selected rows' original MP4s via `huggingface_hub.hf_hub_download(file_name)`, never transcoded. Filters candidates on `has_audio=True`, `language=en`, a real `file_name`, and a non-empty `transcript`; stratifies across short/medium/long duration buckets and alternates `has_music` within each bucket for practical variety (not statistically representative sampling). Idempotent (an existing non-empty target file is never re-downloaded) and never crashes the whole batch on one bad clip — failures are recorded and reported, verified via optional real `media.inspect_media()` when ffmpeg/ffprobe are available. Gated dataset access failures raise `DatasetAccessError` with an actionable message (accept the dataset's access conditions, `huggingface-cli login` or `HF_TOKEN`) — never a token in source code. Imports only `media.py`; never `content_store`, `slot_matcher`, `classification`, or `calendar_manager`.
- Added `transcript_metrics.py`: pure, dependency-free functions — `strip_webvtt()` (extracts spoken text from the dataset's segment-level WEBVTT transcripts, discarding the header, cue identifiers, timestamps, NOTE/STYLE blocks, and inline tags), `normalize_for_wer()`, and `word_error_rate()`/`character_error_rate()` via a local Levenshtein implementation (no external WER package, no ML framework). Both metrics return `None` (not 0.0 or 1.0) when the reference has no words — undefined, not a score.
- Added `evaluate_transcription.py`: runs the real, production `transcription.FasterWhisperTranscriber` (no second Whisper implementation) against each downloaded clip, compares its output to the dataset's WEBVTT reference transcript (treated as a reference ASR output from another model, not ground truth) via `transcript_metrics.py`, and reports per-clip WER/CER plus `realtime_factor` (transcription time / video duration). Aggregates mean/median WER, best/worst clip, and a WER-by-`has_music` breakdown; writes `evaluation/video_pipeline/results.jsonl`. Deliberately separate from `process_content.py` (benchmark tooling, not the pipeline) and from `evaluate_classifier.py` (different subject).
- Added `requirements-eval.txt` (`datasets`, `huggingface_hub`) — kept out of `requirements.txt` so the production dependency set is untouched; `huggingface_hub` is often already present transitively via `faster-whisper`/`fastembed` but is declared explicitly since `download_shofo_samples.py` imports it directly.
- Added `evaluation/video_pipeline/` (gitignored except `README.md`, following the same pattern as the existing `evaluation/` classifier corpus) with its own `videos/.gitkeep`. Updated `.gitignore`.
- No ADR added — no persistent architectural dependency on Hugging Face was introduced (acquisition is a standalone, optional, sample-only utility).
- Added `tests/test_transcript_metrics.py`, `tests/test_download_shofo_samples.py`, `tests/test_evaluate_transcription.py` — 57 new tests, all Hugging Face access mocked per project convention (no real network, no real download, no full-dataset fetch); real ffprobe verification is exercised only against a locally ffmpeg-synthesized clip, skipped automatically if ffmpeg/ffprobe are not on `PATH`.
- Updated `README.md` (new "Shofo Real-Video Evaluation Corpus" section) and `PROJECT_STATE.md` (new section + Directory Ownership + Testing entries).

**Not verified live in this environment**: this dataset is gated and no Hugging Face authentication/access-acceptance was available here, so no real acquisition, real faster-whisper transcription against Shofo audio, real transcript-accuracy comparison, or real FIFO/Google-Calendar assignment against this corpus has been exercised — only the unit-level contract (dataset access mocked) plus real ffprobe-backed media verification against a synthesized clip. The full manual walkthrough (accept dataset terms → authenticate → `download_shofo_samples.py --count 12` → `evaluate_transcription.py` → copy 5 into `content/incoming/` → `process_content.py --dry-run` then for real → confirm FIFO order and Google Calendar scope) is the user's remaining manual step — see `evaluation/video_pipeline/README.md`.

Validation: `python3 -m pytest` (279 passed, up from the 222 baseline before this milestone). Manually confirmed both scripts' `--help` output; and ran `download_shofo_samples.py --count 1` for real in this environment (`huggingface_hub` is already present transitively via `faster-whisper`, but `datasets` is not installed here) — it correctly failed with the actionable `pip install -r requirements-eval.txt` message rather than a bare `ImportError`, exit code 1.

## 2026-09-12

### FIFO Baseline Routing, Optional Pillar Strategy, and First-Class Captions — Milestone 1.3

Refactored the baseline V1 workflow so scheduling a video no longer requires a content pillar or classification — the core product value is now "drop in finished videos, pick a cadence, they get scheduled first-in-first-out," with the prior classify-then-match strategy preserved as a fully-functional opt-in mode. Also made caption data first-class video metadata, ahead of the eventual TikTok publishing milestone.

- Added `config.ROUTING_MODE` (`CONTENT_CALENDAR_ROUTING_MODE`, default **`"fifo"`**, alternative `"pillar"`) and `config.CAPTION_MODE` (`CONTENT_CALENDAR_CAPTION_MODE`, default `"transcript_auto"`, alternatives `"manual"`/`"none"`). Both fail immediately and clearly on an unrecognized value (`scheduling.validate_routing_mode`, `caption.validate_caption_mode`), validated at first use in both CLIs — never inferred from `CONTENT_TYPES`'s presence.
- `generate_calendar.build_schedule(year, month, start_at=None, routing_mode=None)` branches: `fifo` produces untyped `OPEN` slots (no `allocate_pillars`/`distribute_pillars` call, no prompt attached, `"Content Post"` as the fixed Google Calendar event title, no `colorId`) straight from the future-only date set; `pillar` is the unchanged Milestone 1/1.1/1.2 behavior. Future-only filtering (`filter_future_dates`) applies identically in both modes.
- `content_slots.pillar_key` is now nullable (was `NOT NULL`) — `UNIQUE(scheduled_at)` unchanged. `content_store`'s existing rebuild migration now detects either the old two-column unique constraint or a `NOT NULL` `pillar_key` and fixes both in one rebuild pass; existing rows are untouched.
- `slot_matcher.select_slot_fifo` (new): earliest `OPEN` slot at/after now (inclusive), no pillar filter — the FIFO matching counterpart to the unchanged pillar-mode `select_slot` (exclusive of now).
- `process_content.py` rewritten to branch on `ROUTING_MODE`: `fifo` mode never constructs or calls a `ContentClassifier` at all (`classification.build_classifier()` isn't even invoked), so an invalid/unset `CONTENT_CALENDAR_CLASSIFIER` cannot block it and no embedding-model load cost is paid; `pillar` mode is functionally unchanged.
- **Transcription decoupled from scheduling, in FIFO mode only.** A transcription failure in `fifo` mode now sets `videos.status="TRANSCRIPTION_FAILED"` + `transcription_status="FAILED"` + `failure_reason` (existing taxonomy) instead of aborting the pipeline — the video still proceeds to caption (falls back to `caption_source="none"`) and FIFO slot matching in the same run, and keeps retrying slot matching (never re-attempting transcription) on subsequent runs. Media validity (ffprobe) remains a hard blocking precondition in both modes — only transcription's *success* stopped being required for scheduling. `pillar` mode is unchanged: transcription failure is still a hard `FAILED`, moved to `content/failed/`, since classification has nothing to work with otherwise. See `docs/decisions/0005-...md` for why transcription stays *before* (not after) slot assignment in the pipeline despite being non-blocking — reordering would risk permanently losing transcript data if the process were interrupted between slot assignment and transcription.
- Added `caption.py`: `build_caption_from_transcript()` (whitespace normalization only — deliberately **no truncation**; platform-specific caption limits are deferred to the eventual per-platform publisher, not baked into the canonical stored caption) and `validate_caption_mode()`. `videos.caption_text`/`caption_source` added (nullable `ALTER TABLE`, same pattern as the Milestone 1.2 classifier columns). Caption derivation runs in **both** routing modes, right after the transcribe stage, idempotently (skipped once `caption_source` is set) and never overwrites an existing manual caption.
- `process_content.discover_videos(store, incoming_dir)` (signature gained `store`): a video already known to the database (still waiting in `content/incoming/` from a prior run) now sorts by its immutable `videos.created_at` instead of the file's current mtime, so its FIFO position survives being touched or copied between runs — explicitly does **not** survive a rename, which is treated as a newly discovered file (`content_store.get_video_by_path`, documented limitation rather than a broken promise).
- `clear_calendar.py`: prints `[fifo]` instead of the pillar key for an untyped slot (was previously guaranteed non-null).
- Considered and deliberately deferred, with reasoning recorded in the ADR: a `platform_posts` table (video:post stays 1:1 until TikTok publishing actually exists), a caption approval state machine (`DRAFT`/`APPROVED`/`AUTO`), and rewriting the Google Calendar event title after a FIFO video is assigned.
- Added `docs/decisions/0005-fifo-baseline-and-optional-strategy-routing.md`. Updated `README.md` (new "Routing Mode" and "Captions" sections, mode-scoped notes throughout) and `PROJECT_STATE.md` (new "Routing Strategy"/"Captions" sections, updated Directory Ownership/Scheduling Strategy/Pipeline Behavior/Classification/Testing/Non-Goals).
- Added `tests/test_fifo_scheduling.py`, `tests/test_fifo_process_content.py`, `tests/test_caption.py`. Extended `tests/test_scheduling.py` and `tests/test_content_store.py`. Updated `tests/test_generate_calendar.py`, `tests/test_future_only_schedule.py`, and `tests/test_process_content_integration.py` to pass `routing_mode="pillar"` explicitly where they assert pillar-specific behavior, now that `fifo` is the default. Updated `tests/test_clear_calendar.py` for the `[fifo]` label.

Validation: `python3 -m pytest` (222 passed, up from the 173 baseline before this milestone). Manually confirmed: `CONTENT_CALENDAR_ROUTING_MODE=fifo|pillar python3 generate_calendar.py --dry-run` both produce the expected summary format; an invalid `ROUTING_MODE`/`CAPTION_MODE` fails immediately and clearly from both `generate_calendar.py` and `process_content.py`; `process_content.py` with no videos in `content/incoming/` completes in well under a second under the default `fifo` mode (no embedding model load attempted, confirming the classifier is genuinely never constructed).

### Future-Only Calendar Generation — Milestone 1.1.1

Schedule generation now discards posting datetimes that have already passed *before* pillar weights are allocated, so `generate_calendar.py` run partway through the current month only schedules what's left — matching what the routing pipeline can actually assign videos into.

- `scheduling.py`: added `filter_future_dates(dates, start_at)` — pure, inclusive boundary (`scheduled_at >= start_at`, so a same-day slot is kept if its `POSTING_TIME` hasn't passed yet), takes no clock reading of its own.
- `generate_calendar.build_schedule(year, month, start_at=None)`: filters candidate dates via `filter_future_dates` immediately after `generate_posting_dates`, *before* calling `allocate_pillars`/`distribute_pillars` — pillar counts are computed against the remaining future count, not the full month. `start_at` is injectable (tests pass it explicitly); omitted, it resolves via `slot_matcher.now_in_config_timezone()` — reusing the app's one existing "what does now mean" convention rather than introducing a second one.
- `generate_calendar.py main()`: an empty schedule (entirely past month) prints `No future posting slots remain for <Month> <Year>.` and returns before resolving/creating any calendar or opening `ContentStore` — no Calendar writes, no `content_slots` writes, in both dry-run and real mode.
- A future month, or the future portion of the current month, is unaffected — this only ever removes candidates, never adds or reorders them.
- Added `tests/test_future_only_schedule.py` and extended `tests/test_scheduling.py` with `filter_future_dates` coverage (past-date removal, same-day boundary inclusive/exclusive, future/entirely-past months, ascending order). Updated `tests/test_generate_calendar.py` and `tests/test_calendar_target.py` to pass an explicit fixed `start_at`/mocked "now" — those tests exercise full-month composition and calendar-targeting respectively, not this feature, so they needed to stop depending on wall-clock reality once filtering became real.
- Transcription, classification, slot matching, OAuth/calendar ownership, and embedding behavior are unchanged, as scoped.

Validation: `python3 -m pytest` (173 passed, up from 157). Manually confirmed against the real current date (2026-09-12): `--dry-run` for September 2026 correctly starts at today's still-upcoming slot and allocates 40/30/20/10 across the 11 remaining posts (5/3/2/1); July 2026 (entirely past) prints the clean message with zero side effects in both dry-run and real mode; October 2026 (entirely future) generates its full 18-post schedule unaffected.

### Dedicated App-Owned Google Calendar — Milestone 1.4

Replaced the implicit "whatever calendar `DEFAULT_CALENDAR_ID`/`--calendar` names, default `primary`" target with one dedicated, app-owned Google Calendar ("Content Automation") that normal operation always uses, and that `clear_calendar.py` can safely wipe without any risk to the user's primary or other calendars.

- Added `calendar_manager.py`: OAuth authentication (`build_oauth_calendar_service`, `google-auth-oauthlib`'s `InstalledAppFlow`, cached refresh token) and dedicated-calendar resolution (`resolve_app_calendar`) — reuse persisted `calendar_id` if accessible, recover via owner-only display-name search if not, create only if neither works. Never falls back to `"primary"` or any other calendar. Calendar identity persisted at `data/calendar_state.json` (gitignored).
- **Authentication decision** (see `docs/decisions/0004-dedicated-google-calendar-ownership.md`): the dedicated calendar is created/owned via OAuth as the human account, not the shared service account — reviewed and confirmed appropriate per Google's guidance for apps that create secondary calendars. The shared service account is kept, but now used only for the explicit `--calendar <id>` advanced/debug override on both scripts (unchanged behavior from before this milestone); it is never reachable from any default, no-argument invocation.
- `generate_calendar.py`: `--calendar` now defaults to `None` (was `DEFAULT_CALENDAR_ID`, i.e. `"primary"`). No value -> OAuth + dedicated calendar (normal path); a value -> service account + that exact calendar (opt-in override, unchanged). Dry-run resolves nothing and calls neither auth path — zero Google Calendar contact, as before.
- `clear_calendar.py` rewritten: default path resolves the dedicated calendar (`create_if_missing=False` — fails closed with a clear error if none exists, never falls back to `"primary"`) and deletes only `content_slots` rows in `OPEN` status plus their exact tracked calendar event, not a calendar-wide date-range wipe. `--all` extends this to `ASSIGNED` slots too — destructive, explicit, and documented: since `videos.assigned_slot_id` has a foreign-key constraint, the referencing video is reset to `status=CLASSIFIED`/`assigned_slot_id=NULL` before its slot is deleted, without touching its transcript/classification. The calendar row itself is never deleted by either mode. `--dry-run` reports scope/count with zero mutation. The original `--calendar <id> [--start/--end]` full-range service-account clear is preserved as an explicit, opt-in override.
- `content_store.py`: added `list_slots_by_status`, `delete_slot`, `unassign_video_for_slot` to support the above.
- Added `tests/test_calendar_manager.py`, `tests/test_calendar_target.py`, `tests/test_clear_calendar.py`, and extended `tests/test_content_store.py` — all Google API calls mocked, no live credentials required.
- Added `docs/decisions/0004-dedicated-google-calendar-ownership.md`. Updated `README.md` (new "Calendar Ownership" section, revised Setup steps 3-4, `.env.example`) and `PROJECT_STATE.md`.

Validation: `python3 -m pytest` (157 passed, up from 129). Manually confirmed (real, unmocked): `generate_calendar.py --dry-run` performs zero Google Calendar contact; real (non-dry-run) generation with no OAuth client secrets configured yet fails immediately with a clear, actionable setup message (never falls back to the service account or "primary"); same for `clear_calendar.py`'s default path; no stray state files were created by either failure.

**Not verified live in this environment**, and explicitly out of reach here: completing the one-time Google Cloud OAuth Client ID setup and the interactive browser consent flow requires the user's own Google Cloud Console access and a real browser, neither available to this session. The full manual walkthrough (real secondary calendar appears under the account, events land only there, primary/other calendars stay untouched, `clear`/regenerate reuses the same calendar id) is the user's remaining manual step — see README.md "Calendar Ownership" setup and PROJECT_STATE.md.

### Provisional Engineering-Focused Pillar Set — Milestone 1.3

Configuration/content-only change: replaced the agency-oriented `CONTENT_TYPES` (acquisition/building/execution/mindset) with a provisional engineering-focused set to test classification and routing against the content strategy actually being produced now. No classifier, scheduling, persistence, or transcription architecture changed.

- `config.py`: `CONTENT_TYPES` now `engineering` (40%, Software Engineering & Building), `career` (30%, Early-Career Software Engineering), `building_in_public` (20%, Building in Public), `mindset` (10%, Mindset & Discipline — key unchanged, label/description/weight updated). Weights sum to 1.0. Each pillar keeps `label`/`color_id`/`weight`/`description`/`classification_examples` — no type-annotation or schema change.
- `prompts.py`: retired the `acquisition`/`building`/`execution` prompt lists. Added minimal 3-item placeholder prompt lists for `engineering`/`career`/`building_in_public` (functional, not a designed content plan — see the module docstring). `mindset`'s prompt list is unchanged (same key, old agency-era content); reconciling it with the new framing is separate follow-up work, out of scope here.
- Fixed a latent bug in `evaluate_classifier.sweep_from_scores` surfaced by this change: it looked up `config.CONTENT_TYPES[top_key]["label"]` purely to build a `_gate_decision` reason string that sweep mode already discards, so any scored sample using a pillar key outside the *current* live config (e.g., a saved sweep from a prior pillar strategy) raised `KeyError`. Now passes `top_key` directly instead of resolving a real label — sweep math no longer depends on the live `CONTENT_TYPES` at all.
- Updated `tests/test_generate_calendar.py` (`test_build_schedule_matches_configured_pillar_weights` now checks against the new key set — this test asserts real `config.CONTENT_TYPES` output, so it must track whatever strategy is active) and `tests/test_classification.py` (decoupled from `config.CONTENT_TYPES`: now uses a fully arbitrary local `PILLAR_KEYS` list, since that file tests `_validate_result`'s generic contract, not any particular pillar strategy, and had been silently depending on old pillar keys existing).
- Other tests referencing `"building"`/`"acquisition"`/`"execution"` as example keys (`test_scheduling.py`, `test_slot_matcher.py`, `test_content_store.py`, `test_embedding_classifier.py`, `test_evaluate_classifier.py`, and the remaining `test_generate_calendar.py` cases) were left unchanged — they pass their own local pillar-key dicts/fixtures and never import `config.CONTENT_TYPES`, so they test generic mechanisms independent of whichever pillar strategy is currently configured.
- Added `tests/test_config.py`: locks the active pillar key set and weight sum, confirms every pillar has a description and examples, and confirms `build_classifier()`/`EmbeddingClassifier` initialize with no `ANTHROPIC_API_KEY`.
- Updated `README.md` (provisional-strategy notice, refreshed `CONTENT_TYPES` examples) and `PROJECT_STATE.md` (new "Active Pillar Strategy (Provisional)" section, noting `EMBEDDING_MIN_SIMILARITY`/`MIN_MARGIN` have not been re-calibrated against the new pillars).

Validation: `python3 -m pytest` (129 passed, up from the 123 baseline — 5 tests failed immediately after the config swap as expected, all traced to genuine dependencies on the old keys and fixed above). Manually confirmed: `config.py` imports cleanly with the new keys; `EmbeddingClassifier` builds semantic profiles for all four new pillars with no `ANTHROPIC_API_KEY` set; `generate_calendar.py --month 9 --year 2026 --dry-run` produces a 40/30/20/10 schedule (September 2026: 12/9/6/3 of 30 posts) containing only the new pillar labels; `process_content.py --dry-run` (with and without a real ffmpeg-synthesized video) initializes and runs cleanly with no API key.

### Local Embedding Classifier & Evaluation Harness — Milestone 1.2

Replaced Claude as the *required* classifier with a local, fully offline embedding classifier as the default, keeping Claude available as an optional comparison/reference implementation. `process_content.py` now needs no Anthropic API key by default: ffmpeg + faster-whisper + local embeddings + SQLite.

- Added `EmbeddingClassifier` (`classification.py`): local cosine-similarity classification via `fastembed` + `BAAI/bge-small-en-v1.5` (384-dim, ~65MB quantized ONNX, CPU-only, no torch/CUDA — chosen over `sentence-transformers` specifically to avoid the torch dependency). Each pillar's `label` + `description` + new `classification_examples` (added to `config.CONTENT_TYPES`) is combined into one profile text and embedded once per run; pillar vectors and the model itself are cached on the classifier instance. Persistent cache directory: `config.EMBEDDING_CACHE_DIR` (defaults to `~/.cache/content-calendar/fastembed` — fastembed's own default is a `/tmp` path, which isn't durable across reboots).
- Added a two-gate auto-assign policy for embeddings: top pillar similarity must clear `EMBEDDING_MIN_SIMILARITY` **and** its margin over the second-best pillar must clear `EMBEDDING_MIN_MARGIN`, or the video is marked `NEEDS_REVIEW`. Both thresholds ship as explicitly uncalibrated placeholders (`0.50`/`0.03`) pending real calibration.
- Added `config.CLASSIFIER` (`"embeddings"` default, `"claude"` optional) and `classification.build_classifier()`; `process_content.py` now depends only on `classification.ContentClassifier`, never a concrete classifier class. An unsupported value fails immediately (`Unsupported CLASSIFIER='foo'. Valid values: embeddings, claude`).
- Moved the auto-assign decision fully inside each classifier: `ClaudeClassifier` now applies `AUTO_ASSIGN_THRESHOLD` itself and returns `pillar=None` to abstain (this previously lived in `process_content.py`). `process_content.py`'s gate simplified to `eligible = result.pillar is not None` — it no longer applies any classifier-specific threshold itself, so embedding similarity scores and Claude's self-reported confidence can never be cross-contaminated by the same threshold.
- `ClassificationResult` gained `classifier: str`, `second_score: float | None`, `margin: float | None`. Its `confidence` field is kept (to avoid a broader pipeline rename) but is now explicitly documented as classifier-dependent: a genuine self-reported confidence for Claude, a raw (uncalibrated) cosine similarity for embeddings. CLI output labels it "Similarity score" instead of "Confidence" for anything but Claude.
- `content_store.py`: added nullable `videos.classification_second_score`, `classification_margin`, `classifier` columns via a simple `ALTER TABLE` migration (`_ensure_videos_columns`) — no table rebuild needed.
- Added `evaluate_classifier.py`: loads a labeled dataset (`labels.csv` + `transcripts/*.txt`), runs any `ContentClassifier` over it, and reports total/auto-assigned/review counts, auto-assigned accuracy, **wrong auto-assignments** (the headline metric), per-pillar accuracy, a confusion breakdown, and average latency. `--sweep` re-applies the two-gate policy to pre-computed scores across a similarity/margin grid without re-embedding. Never imports `content_store` — cannot mutate production state.
- Added `evaluation/` (gitignored real corpus; only `evaluation/README.md` committed) and `tests/fixtures/eval_sample/` (small committed synthetic dataset for testing the harness itself).
- Added `docs/decisions/0003-local-embedding-classification.md`. Updated `README.md` and `PROJECT_STATE.md`.
- Added `tests/test_embedding_classifier.py` and `tests/test_evaluate_classifier.py` (embedding model mocked — no real ONNX download required for the suite); updated `tests/test_content_store.py` (videos-column migration test) and `tests/test_process_content_integration.py` (`FakeClassifier` now self-gates on confidence, matching the real classifier contract).
- Media inspection, transcription, and `slot_matcher.py` routing policy are unchanged, as scoped.

Validation: `python3 -m pytest` (123 passed). Manually ran the real (unmocked) pipeline end to end with no `ANTHROPIC_API_KEY` set: `process_content.py` against a real ffmpeg-synthesized video (empty transcript correctly abstained via the embedding classifier, no crash); `evaluate_classifier.py --classifier embeddings` and `--sweep` against the committed synthetic fixture with the real `BAAI/bge-small-en-v1.5` model (confirmed persistent caching — first run ~8s including model download, second run <1s); `CONTENT_CALENDAR_CLASSIFIER=foo` failing immediately with the exact documented message; `CLASSIFIER=claude` with no key still failing cleanly per-video as before.

### Configurable Posting Cadence & Weighted Pillar Allocation — Milestone 1.1

Replaced the fixed `WEEKLY_SCHEDULE` (weekday name -> pillar key) and its `FIFTH_SUNDAY_CONTENT_TYPE` rebalancing special case with a general scheduling strategy: a configurable posts-per-week cadence, an "auto" or explicit posting-day selection, a configurable posting time, and per-pillar percentage weights for any number of pillars.

- Added `scheduling.py`: `generate_posting_dates` (posts/week + posting days + posting time -> real calendar dates, deterministic evenly-spaced auto-day selection, no LLM), `allocate_pillars` (largest-remainder method, exact integer counts, config-order tie-break), `distribute_pillars` (smooth weighted round-robin so pillars interleave instead of clustering), `validate_schedule_config` (clear rejection of invalid cadence/day/time/weight configuration).
- `generate_calendar.build_schedule(year, month)` now composes `scheduling.py` instead of the fixed weekday mapping; `ScheduledPost` carries a full `scheduled_at: datetime` and an optional `prompt`. CLI (`--month`/`--year`/`--calendar`/`--dry-run`), Google Calendar push, and prompt rotation behavior are otherwise unchanged.
- `config.py`: `CONTENT_TYPES[*]["target_percent"]` renamed to `"weight"`; added `POSTS_PER_WEEK`, `POSTING_DAYS`, `POSTING_TIME`, `PROMPT_GENERATION_ENABLED`. Removed `WEEKLY_SCHEDULE` and `FIFTH_SUNDAY_CONTENT_TYPE`.
- `content_store.py`: `content_slots` uniqueness changed from `(scheduled_at, pillar_key)` to `scheduled_at` alone — one posting datetime is one slot regardless of which pillar a strategy assigns it — and `prompt` is now nullable. A database created under the old constraint is migrated automatically and non-destructively the first time it's opened (`_migrate_content_slots_unique_constraint`), keeping the most-advanced-status row for any timestamp that had duplicates under the old constraint. Regenerating a month after a strategy change remains additive-only: existing slots are never overwritten, only newly-covered dates get new ones.
- Video ingestion, transcription, classification, and `slot_matcher.py`'s routing policy are unchanged — `process_content.py` required no code changes.
- Added `tests/test_scheduling.py` (posting-date generation across leap/non-leap/30/31-day months, 1-7 posts/week, auto/explicit days, largest-remainder allocation incl. the spec's worked example, pillar sequencing, all documented validation-error cases) and `docs/decisions/0002-configurable-cadence-and-weighted-pillar-allocation.md`. Updated `tests/test_generate_calendar.py` and `tests/test_content_store.py` (new migration test) for the new model.
- Updated `README.md` and `PROJECT_STATE.md`.

Validation: `python3 -m pytest` (94 passed). Manually confirmed `generate_calendar.py --dry-run` still works for the default 7-posts/week config, and reproduced the brief's worked example exactly (3 posts/week, Mon/Wed/Fri, September 2026 -> 13 slots: 6 building / 4 acquisition / 3 mindset, interleaved rather than clustered).

### Video Ingestion Pipeline — Milestone 1

Added the first automated content-processing workflow on top of the existing calendar generator: drop `.mov`/`.mp4` files into `content/incoming/`, run `python3 process_content.py`, and have the system inspect, transcribe, classify, and route each video to the earliest matching future posting slot without manual assignment.

- Added `content_store.py` (SQLite `videos` + `content_slots` tables), `media.py` (ffprobe inspection, TikTok-compatibility check, audio extraction), `transcription.py` (`Transcriber` interface, `FasterWhisperTranscriber`), `classification.py` (`ContentClassifier` interface, `ClaudeClassifier` via forced tool-use), `slot_matcher.py` (deterministic earliest-open-slot selection), and `process_content.py` (thin orchestrator + CLI).
- Extended `generate_calendar.py` to persist a `content_slots` row per generated event alongside the existing Google Calendar push, idempotently on `(scheduled_at, pillar_key)` so re-running a month does not duplicate slots. Existing CLI, allocation logic, weekly mapping, fifth-Sunday rebalancing, prompt rotation, and dry-run behavior are unchanged.
- Extended `config.py` with pillar descriptions (for the classifier), `AUTO_ASSIGN_THRESHOLD`, transcription/classification vendor settings, file lifecycle directories, and TikTok generic compatibility targets.
- Added `pytest` suite (`tests/`) covering media inspection, scheduling, classification-policy validation, content_store idempotency, existing calendar-generation behavior, and a full-pipeline integration test against a real ffmpeg-synthesized video.
- Added `docs/decisions/0001-video-ingestion-pipeline.md` (architecture decision) and `PROJECT_STATE.md` (current-state snapshot), following the pattern already established in `internal-tools/service-business-prospecting-assistant`.
- Non-goals for this milestone: TikTok/Instagram/YouTube publishing, Google Calendar → content_slots backfill, web/mobile frontend, concurrency.

Validation: `python3 -m pytest` (37 passed). Manually confirmed `generate_calendar.py --dry-run` is unchanged, and ran `process_content.py` against a real ffmpeg-synthesized video through real ffprobe/ffmpeg/faster-whisper — classification correctly failed cleanly (`CLASSIFICATION_FAILED`, file routed to `content/failed/`) with no `ANTHROPIC_API_KEY` configured in this environment. Live Claude classification against the real API was not exercised here.
