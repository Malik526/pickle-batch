# Milestone 4.0 — Instagram Integration Foundation: Evaluation

Architecture and verified Meta requirements:
[ADR-0018](../../decisions/0018-instagram-integration-architecture.md).

## Status

Foundation complete. No live Instagram connection or publishing exists yet. Nothing was run
against Meta's API: no app credentials exist yet, and they're required to call it.

## What was verified, and how

| Claim | How verified |
|---|---|
| Login model, scopes, token lifecycle, callback params, publishing endpoints, container statuses, Reels specs, limits, access levels | Meta's official docs, read 2026-10-04 (ADR-0018 sources) |
| Supabase signed URL serves private media to an unauthenticated client | Real project, existing hosted video (id 15, `.mov`): 5-minute signed URL → `206` with `video/quicktime`, range requests honored |
| Bucket stays private | Same object without the token → `400`; public-bucket route → `400` |
| Signed URL round trip | `tests/test_storage_supabase.py` against the Supabase test bucket (put → sign → unauthenticated GET returns the bytes; tokenless path refused) |
| Instagram status endpoint, isolation, no credential leakage, `connect_available` stays false | `tests/test_api_platforms_instagram.py` |
| Materializer creates no Instagram posts before publishing exists | `tests/test_platforms.py` |
| TikTok unchanged | Full backend and frontend suites (results in CHANGELOG) |

**Not verified (needs a Meta app and a tester account, Milestone 4.2):** that Meta's fetcher
accepts a Supabase signed URL as `video_url`, and that calling `media_publish` twice on one
container cannot create a second post.

## Manual Meta developer setup

Steps are from Meta's current docs. Where the docs don't give an exact dashboard label, the step
says "confirm in the dashboard". Never paste secrets into chat or commit them; put them only in
Railway's Variables tab and your local `.env`.

### Required now (development and testing, before 4.1)

1. **Instagram account:** make the Instagram account you'll test with a **professional account
   (Business or Creator)**: Instagram app → Settings → Account type and tools. Meta's setup
   guide also says the account added to the app must be **public**. No Facebook Page is needed.
2. **Create the app:** at developers.facebook.com → My Apps → **Create app**. Use case
   **Other**, app type **Business**. An existing non-Business app can't be converted; create a
   new one.
3. **Add Instagram:** in the app dashboard, on the **Instagram** product, click **Set up**. This
   adds **API setup with Instagram login**.
4. **Add your account:** add your Instagram account under **Instagram > API setup with Instagram
   login** (or **App roles > Roles**). If it shows as an invitation, accept it while signed in to
   that Instagram account (confirm where the invite appears in the dashboard flow). Only accounts
   with a role on the app can connect while the app has Standard Access.
5. **Business login settings:** under **Instagram > API setup with Instagram login > Set up
   Instagram business login**, add the OAuth redirect URI, exactly:
   `https://<your-railway-api-domain>/api/platforms/instagram/callback`
   It must match `INSTAGRAM_REDIRECT_URI` character for character. The route ships in 4.1;
   registering it now is fine.
6. **Copy the credentials:** copy the **Instagram app ID** and **Instagram app secret** shown for
   API setup with Instagram login (confirm the exact labels in the dashboard). They are **not**
   the Meta app ID / app secret under App settings > Basic. Set them on the **Railway API
   service only**:
   - `INSTAGRAM_APP_ID`
   - `INSTAGRAM_APP_SECRET`
   - `INSTAGRAM_REDIRECT_URI` (the URL from step 5)

   Don't add them to the worker service or Netlify. Add them to local `.env` only if you'll test
   OAuth locally, which also needs an https callback, e.g. a tunnel.
7. **Permissions:** the app requests `instagram_business_basic` and
   `instagram_business_content_publish`. With Standard Access these work for accounts with a role
   on the app without App Review.
8. **Optional smoke test:** the dashboard's **Generate token** button next to your account
   produces a 60-day token. It's useful to sanity-check the account, but don't paste it anywhere;
   4.1 obtains tokens through the real flow.

### Required later (before real users can connect)

- **Advanced Access** for both permissions, which requires **App Review** and **Business
  Verification**. Meta also states Instagram "requires successful completion of the App Review
  process before your app can access live data."
- **Deauthorize callback** and **data deletion request** URLs in Business login settings, plus a
  privacy policy URL (`https://picklebatch.netlify.app/privacy`) and app icon/details for review.
  The callback routes are built before review.
- **Update 2026-10-10:** the data deletion **instructions** page exists:
  `https://picklebatch.netlify.app/data-deletion/`, live once deployed. Enter it under App
  settings > Basic > User data deletion > Data deletion instructions URL. Meta's data deletion
  **callback** (a signed backend request) is still not built and isn't required when the
  instructions URL is used. The public contact address is now a real, monitored inbox.
- A screencast and use-case descriptions for review: connect → choose a Reel → scheduled publish.
- Switching the app to **Live** mode.
- If Google sign-in remains, plan Sign in with Apple before the iOS App Store (unrelated to Meta
  review, see `native-ios-readiness.md`).

## Readiness for 4.1 (Instagram OAuth)

Ready once steps 1–6 are done. Proposed 4.1 scope:

- connect, callback and disconnect routes;
- short- to long-lived token exchange, encrypted credential storage, `@username` identity;
- `oauth_states.return_target` (nullable, allowlisted; both backends plus a Postgres migration),
  with `code_verifier=''` for Instagram;
- shared Fernet helpers (TikTok re-exports, unchanged);
- Instagram token refresh (≥24 h old, before 60 days) under the existing per-connection lock;
- `connection_available=True` for Instagram, plus Connect/Disconnect in Settings through the
  existing card and cache;
- tests covering state misuse, denial, expiry, exchange failure and isolation.

No publishing.
