# ADR-0018: Instagram Integration Architecture

## Status

Accepted (Milestone 4.0, foundation). OAuth is Milestone 4.1, Reels publishing 4.2, multi-platform
Queue behavior 4.3, worker/reconciliation integration 4.4.

## Context

Instagram is the second publishing platform and the first test of whether the infrastructure
built for TikTok (Milestones 2–3) is platform-agnostic. Meta's requirements were verified
against Meta's official documentation on 2026-10-04 (sources at the end), not older examples.

### What already generalizes

- **Tables.** `platform_connections` (user, platform, external account id, status),
  `platform_credentials` (one Fernet-encrypted JSON payload per connection), `oauth_states`
  (user-bound, single-use, expiring) and `platform_posts` (unique per video and platform, with
  `submission_state`, `failure_code`, retry and status-check columns) are all keyed by a platform
  string. Instagram needs no new tables: no `instagram_platform_posts` / `instagram_videos` /
  `instagram_queue`.
- **Publish state.** `publishing/publish_status.py` resolves each platform's post separately and
  aggregates a slot by urgency (PUBLISHED only when every post is published). Queue responses
  already carry per-platform `publications`.
- **Failure taxonomy.** `publishing/failure_taxonomy.py` has per-platform label and code tables.
- **Caption seam.** `publishing/caption_resolution.resolve_publish_caption(video, platform)`.
- **Worker plumbing.** `scheduling/worker.py`, `reconciliation.py` and `crash_recovery.py` take a
  `platform` argument and a `Publisher`.
- **Credential refresh lock.** `PostgresContentStore.credential_refresh_lock` is per connection.
- **Frontend.** The 3.15 query cache keys connection status per user and platform.

### TikTok-specific assumptions found

1. **`Publisher.publish(video_path: Path, ...)` assumes we push bytes.** Instagram pulls the
   video from a URL we provide.
2. **One-step publish.** TikTok publishes on submission. Instagram needs a client-driven second
   step (`media_publish`) after asynchronous processing, so reconciliation must be able to finish
   a publish, not only observe one.
3. **One external id.** `platform_posts.platform_post_id` holds TikTok's `publish_id`. Instagram
   has a container id (the submission handle) and, after publishing, a different media id.
4. **Global target list.** `config.TARGET_PUBLISHING_PLATFORMS` is one env list for every user,
   not each user's connected platforms. Adding `instagram` would have created Instagram posts
   for everyone that nothing publishes. (Guarded in 4.0, see Decision 3.)
5. **TikTok-named shared code.** The generic publish orchestration lives in
   `scheduling/publish_tiktok.py` (with one hardcoded `"tiktok"` caption lookup and TikTok media
   checks). Fernet helpers live in `publishing/tiktok/credential_store.py`. The hosted worker has
   `PLATFORM = "tiktok"` and a TikTok-only publisher factory.
6. **Web-only OAuth return.** The TikTok callback always redirects to
   `FRONTEND_BASE_URL/app/settings` (already recorded in `native-ios-readiness.md`).
7. **`oauth_states.code_verifier` is NOT NULL** (PKCE). Instagram Login documents no PKCE.

## Decisions

### 1. Instagram API with Instagram Login

Use **Instagram API with Instagram Login** (Business Login for Instagram), not Instagram API with
Facebook Login.

- Works with Instagram **professional accounts (Business or Creator)**. **No Facebook Page** is
  required. That fits creators who only have an Instagram presence.
- Base host `graph.instagram.com`. It lacks hashtag search, product tagging and partnership ads,
  none of which Pickle Batch needs.
- Facebook Login would add a Page requirement and Page tokens for no capability we use. Revisit
  only if Page-linked features become a product need.

Verified flow and limits:

| Item | Current value |
|---|---|
| Authorize | `GET https://www.instagram.com/oauth/authorize` with `client_id` (Instagram app ID), `redirect_uri` (exact match), `response_type=code`, `scope`, `state`; optional `enable_fb_login`, `force_reauth`. PKCE not documented. |
| Scopes | `instagram_business_basic`, `instagram_business_content_publish`. The old `business_*` names were deprecated on 2025-01-27. |
| Callback | `?code=...` (valid 1 hour, single use; strip a trailing `#_`). Cancel: `error=access_denied&error_reason=user_denied&error_description=...` |
| Short-lived token | `POST https://api.instagram.com/oauth/access_token` with `client_id`, `client_secret`, `grant_type=authorization_code`, `redirect_uri`, `code` → `access_token` (1 hour), `user_id`, `permissions` |
| Long-lived token | `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret=...&access_token=<short>` → 60 days |
| Refresh | `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=<long>`. Allowed once the token is at least 24 h old and still valid; needs `instagram_business_basic`. No app secret. A token not refreshed within 60 days can't be refreshed. |
| Publish limit | 100 API-published posts per 24-hour moving window; `GET /<IG_ID>/content_publishing_limit` |
| Access levels | Standard Access (default): people with a role on the app; no review. Advanced Access (accounts you don't own or manage): App Review plus Business Verification. |

### 2. Shared tables, Instagram adapter

- `platform_connections` row with `platform='instagram'`. `external_account_id` = the
  Instagram user id returned by the token exchange. It's stored but never shown to users, like
  TikTok's `open_id`.
- `platform_credentials.encrypted_payload` = JSON
  `{access_token, token_type, expires_at, user_id, permissions, obtained_at, last_refreshed_at}`
  (long-lived token only; the short-lived token is never stored).
- In 4.1, the Fernet helpers move from `publishing/tiktok/credential_store.py` to a shared
  module, with TikTok re-exporting them so TikTok behavior is unchanged. Instagram's refresh rules
  live in `publishing/instagram/`.
- No Instagram-specific columns on generic tables. The two generic additions the lifecycle needs
  are in Decision 5.

### 3. Platform registry with a minimal capability model (implemented in 4.0)

`publishing/platforms.py` registers `tiktok` and `instagram`. Capability fields exist only
where the two genuinely differ: `media_delivery` (`push_file` / `pull_url`),
`requires_finalize_step`, `caption_max_chars`, `connection_available`, `publishing_available`.
Instagram has both availability flags False until 4.1 and 4.2.

- Failure-taxonomy labels come from the registry.
- `platform_post_materializer` creates rows only for `publishable(TARGET_PUBLISHING_PLATFORMS)`,
  so listing `instagram` early can't create posts nothing publishes.
- The frontend mirrors the ids in `web/lib/domain/publishing.ts`.

### 4. Media delivery: short-lived signed URLs over private storage (helper implemented in 4.0)

Meta requires `video_url` to be on "a publicly accessible server" and fetches it with cURL.
Canonical media stays in the **private** Supabase bucket; the bucket is never made public.

- `StorageProtocol.create_signed_url(key, expires_in_seconds)`:
  - `SupabaseStorage`: `POST /storage/v1/object/sign/{bucket}/{key}`.
  - `LocalStorage` raises `SignedUrlUnsupportedError`, since a developer machine is never
    reachable by Meta.
- Verified against the real project (2026-10-04): the signed URL serves the object with no
  credentials, supports HTTP range requests (206) and returns `video/quicktime` for `.mov`. The
  same path without the token, or via the public-bucket route, is refused (400).
- **Lifetime:** `INSTAGRAM_MEDIA_URL_TTL_SECONDS`, default 3600, bounded 300–86,400 s. Meta
  fetches while the container is `IN_PROGRESS`, normally minutes after creation.
- **When generated:** immediately before container creation, inside the worker's claimed
  publish attempt. Never at queue time, never stored in the database, never returned by the API.
- **Expiry before ingestion:** the container goes to `ERROR`. Because an unpublished container
  never posts, the retry creates a new container with a fresh URL.
- **Logging:** a signed URL is a bearer credential until it expires. Log or persist only
  `storage.signed_urls.redact_signed_url(url)` (query string dropped). `failure_reason` must never
  contain the raw URL.
- **Still unverified:** whether Meta's fetcher accepts Supabase signed URLs. First thing to test
  in 4.2 with a tester account. If it doesn't, the documented fallback is Meta's resumable upload
  (`upload_type=resumable`, then `POST https://rupload.facebook.com/ig-api-upload/<container_id>`),
  which pushes bytes and makes Instagram a `push_file` platform.
- Worker requirement for 4.2: `STORAGE_BACKEND=supabase`, which production already needs.

### 5. Publishing lifecycle mapping (design for 4.2/4.4)

Instagram Reels, verified:
1. `POST /<IG_ID>/media` (`media_type=REELS`, `video_url`, `caption`, optional `share_to_feed`
   etc.) returns a container id.
2. `GET /<container_id>?fields=status_code` reports `IN_PROGRESS` / `FINISHED` / `ERROR` /
   `EXPIRED` (unpublished after 24 h) / `PUBLISHED`.
3. `POST /<IG_ID>/media_publish` with `creation_id` returns the Instagram media id.

The step that can create a post is `media_publish`. Container creation never posts. So the
3.13 checkpoint generalizes from "before media transfer" to "before any step that can create a
post":

| Pickle Batch | Instagram | Rule |
|---|---|---|
| PENDING → claim → PUBLISHING, `submission_state=AWAITING_PLATFORM_ID` | — | unchanged |
| crash before a container id is persisted | maybe an orphan container | safe: bounded retry (`SUBMISSION_INTERRUPTED`); orphans never post and expire in 24 h |
| `on_platform_post_id(container_id)` → `platform_post_id` | container created | persisted **before** `media_publish` |
| PUBLISHING, status check `IN_PROGRESS` | processing | keep polling (`next_status_check_at`) |
| status check `FINISHED` | ready, unpublished | **new**: reconciliation calls `media_publish` (finalize). Safe to repeat: a container publishes at most once (status becomes `PUBLISHED`). Verify against a tester account in 4.2. |
| `media_publish` ambiguous (timeout/crash) | unknown | stay PUBLISHING with the container id; the next check resolves it (`FINISHED` → finalize again, `PUBLISHED` → done). Never a blind new container. |
| `PUBLISHED` | published | PUBLISHED; store the media id when the publish response was received; if recovered from status only, the media id may be NULL |
| `ERROR` | processing failed (e.g. URL unreachable or expired, bad media) | FAILED with id = platform-reported. Unlike TikTok, automatic retry is safe (the container never posted), so a new container is allowed within the retry budget for retryable error codes. |
| `EXPIRED` | not published within 24 h | same as ERROR: new container allowed |
| status unreadable (auth lost, repeated errors) | unknown | UNKNOWN (unchanged ADR-0016 rules; manual retry re-checks, never resubmits) |
| 100/24 h limit reached | — | check `content_publishing_limit` before creating a container; defer (PENDING with `next_retry_at`) rather than fail |

Generic changes this needs (4.2, without weakening TikTok):

- **`Publisher.publish` takes a media source, not a path.** It offers `local_path()` (context
  manager, TikTok) and `signed_url(ttl)` (Instagram), chosen by `media_delivery`. TikTok behaves
  the same.
- **Finalize step.** `PublishStatusResult` gains a "ready to finalize" state, and `Publisher`
  gains an optional `finalize(platform_post_id) -> media_id`, used only by platforms with
  `requires_finalize_step`.
- **New column `platform_posts.platform_media_id`** (nullable, both backends): the platform's id
  for the published post when it differs from the submission id. `platform_post_id` stays the
  submission handle used for status checks. Useful for TikTok's post id later too.
- **"Safe to resubmit" failure categories.** Container `ERROR`/`EXPIRED` are known-not-posted,
  so they may go through `_schedule_retry_or_fail` even though an id exists. TikTok keeps the
  never-resubmit-with-id rule.
- **Platform-aware pre-validation.** Reels: MOV/MP4, H.264/HEVC, AAC (≤48 kHz), 23–60 fps,
  3 s–15 min, ≤300 MB, ≤1920 px wide, ≤25 Mbps. Captions: ≤2,200 characters, ≤30 hashtags,
  ≤20 @-tags. Failures get Instagram `failure_code`s and taxonomy entries.
- The generic orchestration moves out of `publish_tiktok.py` (or is parameterized) so the
  caption lookup and media checks take the platform.

### 6. Connection design (4.1)

```text
Settings → POST /api/platforms/instagram/connect (bearer) → oauth_states(user, 'instagram', state, '', redirect_uri, return_target)
        → authorization_url (Instagram) → user approves
        → GET /api/platforms/instagram/callback?code&state (public; user bound only by state)
        → short-lived token → long-lived token → encrypted platform_credentials → connection ACTIVE
        → redirect to the attempt's return_target (default FRONTEND_BASE_URL/app/settings?instagram=connected)
```

- Same protections as TikTok: server-side single-use state with a TTL, callback bound to the
  user only through state, credentials encrypted, owner isolation, no credential fields in any
  response.
- `code_verifier` stores `''` (column stays NOT NULL; Instagram documents no PKCE). If Meta adds
  PKCE, populate it.
- **Native-ready from day one:** 4.1 adds a nullable `oauth_states.return_target`, chosen at
  connect time from a server-side allowlist (default the web Settings URL; later an app scheme
  or universal link). The callback redirects there. TikTok keeps its current redirect until
  Milestone 7 moves it onto the same column.
- Identity for the card: `GET /me?fields=user_id,username` (instagram_business_basic), shown as
  `@username`. Never the numeric id.
- Disconnect deletes the credential and marks the connection DISCONNECTED, like TikTok. Meta's
  deauthorize and data-deletion callbacks are configured before App Review (see setup checklist).

### 7. API and UI foundation (implemented in 4.0)

- `GET /api/platforms/instagram/status` returns the platform-neutral `PlatformConnectionStatus`
  `{platform, connected, status, account_label, connect_available}` from the shared tables.
  `connect_available` is False until the connect flow exists and is configured, so clients never
  offer a button that can't work. TikTok's status endpoint and shape are unchanged.
- Settings shows Instagram's real status through the shared `PlatformConnectionCard` (extracted
  unchanged from the TikTok card), cached per user and platform (`useInstagramConnection`), with
  no Connect button yet.

## Consequences

- Instagram fits the existing tables; the real generalization work is the `Publisher` contract
  (media source, finalize step) and one nullable column, all in 4.2.
- The container id gives Instagram a stronger recovery story than TikTok: ambiguous publishes
  resolve from status, and failed or expired containers are safely retryable.
- Worker processes never hold the Instagram app secret.
- Multi-platform Queue behavior (4.3) still needs a per-user target set (connected platforms, a
  per-video choice) replacing the global `TARGET_PUBLISHING_PLATFORMS`, plus UI for per-platform
  status within one slot.

## Explicitly deferred

Live OAuth (4.1), Reels publishing (4.2), multi-platform Queue posting (4.3), worker and
reconciliation integration (4.4), production E2E (4.5). Analytics, comments, messages, Stories,
carousels, images, cross-platform AI captions and the visual redesign are out of Milestone 4's
initial scope.

## Sources (verified 2026-10-04)

- Content publishing: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing
- Business Login: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login
- IG User media reference (Reels specs): https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
- Platform overview (login models, access levels): https://developers.facebook.com/docs/instagram-platform/overview
- Get started / create an app: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/get-started , https://developers.facebook.com/documentation/development/create-an-app/other-app-types/instagram-apis.md
- Caption limits: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/error-codes

## Addendum — 2026-10-06 (Milestone 4.1 implementation, mocked Meta only)

The Decision 6 connection flow is implemented and covered by automated tests with every Meta
response mocked. **Live Meta OAuth is not verified** (that is M4.1B, blocked on Meta app and
tester access). The decisions above stand; implementation resolved these details:

- **Shared encryption module.** The Fernet helpers live in `publishing/credential_encryption.py`
  (`encrypt_credential`, `decrypt_credential`, `CredentialStoreError`).
  `publishing/tiktok/credential_store.py` re-exports them as `encrypt_token`, `decrypt_token`
  and `CredentialStoreError`, so TikTok's behavior and callers are unchanged.
- **Refresh window.** Decision 1's rule (≥ 24 h old, still valid) is necessary but not a
  schedule. A stored token is refreshed on first use once it is ≥ 24 h since it was obtained or
  last refreshed **and** has at most `INSTAGRAM_TOKEN_REFRESH_WINDOW_SECONDS` left (default 30
  days, validated 1–59 days). Settings status reads use the token for the identity lookup, so a
  connection used at least monthly never lapses. A transient refresh failure (network, 5xx,
  malformed) keeps using a still-valid token; a 4xx means reconnect. Refreshes run under
  `credential_refresh_lock` with a re-read and the CAS write, like TikTok.
- **Platform-scoped, atomic state consumption.** `consume_oauth_state(state, now, *, platform)`
  is one conditional `UPDATE` (state, platform, unconsumed, unexpired). A state is never
  consumed, or marked, by another platform's callback. This applies to TikTok too.
- **Return targets.** `api/oauth_return_targets.py`: exact string match against the web Settings
  URL (`FRONTEND_BASE_URL` + `/app/settings`, the default) plus
  `OAUTH_EXTRA_RETURN_TARGETS`. Entries must be `https://` without user info (`http://` only on
  localhost). The callback re-checks the stored target against the current allowlist and falls
  back to Settings, and appends `instagram=<outcome>` while keeping the target's own query
  parameters. Without a usable `FRONTEND_BASE_URL`, `connect_available` is false.
- **Account id.** `platform_connections.external_account_id` is the token exchange's `user_id`,
  written on every successful callback (`update_platform_connection_external_account`), so a
  reconnect that picks a different Instagram account updates it. `GET /me` also returns a
  `user_id`. Whether `/<IG_ID>/media` needs the exchange id or the `/me` id must be checked
  against a tester account (M4.1B / 4.2) before publishing relies on it.
- **Identity.** `GET /me?fields=user_id,username` is read live at status time with the stored
  credential (`publishing/instagram/identity.py`), like TikTok's `creator_info`. Usernames are
  never persisted; a failure or an invalid username shows no label.
- **Response shapes.** Meta documents some responses wrapped in a `data` list and `user_id` as a
  number or string; both shapes are accepted. Transport errors are re-raised without the
  original exception because `requests` messages include URLs carrying the app secret or a token.
- **Callback outcomes:** `connected`, `denied`, `invalid_state`, `expired_state`,
  `exchange_failed`, `unavailable`. The connection is created inactive and activated only after
  its credential is stored, so an encryption failure never leaves an ACTIVE row behind.
- Instagram's registry `connection_available` is now True; `publishing_available` stays False,
  so no Instagram `platform_posts` rows are created. Postgres migration `0011` adds
  `oauth_states.return_target`.

Evidence: `docs/evaluations/productization/milestone-4.1-instagram-oauth.md`.

## Addendum — 2026-10-10 (Milestone 4.2 implementation: Reels publishing)

Implemented through the existing machinery, as Decision 5 planned. The changes from that plan,
and what was settled during implementation:

- **IDs.** `platform_posts.platform_post_id` holds the container id for the whole life of the row
  (status checks and recovery use it; never overwritten). New nullable
  `platform_posts.platform_media_id` (Postgres migration `0012`, SQLite column migration) holds
  the Reel's media id from `media_publish`. It stays NULL when publication was confirmed only from
  the container's status.
- **Publisher contract.** Rather than a media-source object, a pull-URL publisher receives
  `media_url=` in `publish()`, issued by the caller right after the submission checkpoint and
  immediately before container creation. TikTok's signature is untouched. `Publisher` gains
  `requires_finalize` and an optional `finalize()`. `PublishStatusResult` gains
  `STATUS_READY_TO_FINALIZE` and optional `failure_code` / `platform_media_id`.
- **One finalize path.** `scheduling/finalization.finalize_ready_submission` is the only place
  `media_publish` is called. It's used by the inline poll, reconciliation and crash recovery.
- **No automatic re-publish of a container.** Meta doesn't document whether `media_publish` is
  idempotent per container, so Decision 5's "safe to repeat" assumption is **not** relied on:
  - a `PUBLISH_REQUESTED` checkpoint is written, compare-and-swap, before the call;
  - an ambiguous outcome (timeout, network, 5xx) keeps the checkpoint, and the container's status
    decides;
  - a still-unpublished container after `PLATFORM_POST_STALE_MINUTES` is parked UNKNOWN
    (`PUBLISH_OUTCOME_UNKNOWN`);
  - retrying that requires the user to confirm it wasn't posted, and then re-checks the **same**
    container;
  - an HTTP 4xx answer clears the checkpoint, since Meta rejected the request and nothing was
    posted.
- **Container ERROR/EXPIRED → FAILED, retried deliberately.** A manual retry creates a new
  container. Automatic replacement of failed containers is not implemented; a few retryable Meta
  errors (not-ready, transient, media fetch, rate limit) use the normal retry budget before any
  container exists.
- **Assignment.** Queue assign endpoints accept an optional `platforms` list (validated
  publishable and connected before anything changes). Omitted keeps the configured default.
  This is the minimal 4.2 path; the multi-platform distribution model is 4.3.
- **Validation before submission.** `publishing/instagram/media_requirements.py` covers
  container, codecs, 3 s–15 min, 23–60 fps, ≤300 MB, ≤1920 px wide, and caption limits. No
  transcoding, so the existing 4K iPhone uploads (3840 px) are rejected with an actionable reason.
- **Worker dispatch** is by platform (`hosted_worker.default_publisher_factories`), with errors
  isolated per user and platform. The worker still needs no Instagram app secret.
- **Status.** Publications carry `stage` (`PROCESSING` / `PUBLISHING`) while an Instagram post
  is PUBLISHING; the Queue lists deliveries per platform.

Deployment rule found during implementation: opening a `PostgresContentStore` applies pending
migrations. Before 4.2, the deployed code built records from `SELECT *` rows, so a migration
applied under older running code broke every read of that table. 4.2 makes reads ignore
unknown columns, but the deployed code before 4.2 doesn't. Deploy the API and worker together,
and never open a store against production from a working tree with unreleased migrations (see
the 4.2 evaluation's incident record).
