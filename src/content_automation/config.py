"""
config.py — Configuration for the content calendar generator.

Settings include:
  - Content pillar labels, weights, and Google Calendar colors
  - Posting cadence, posting-day strategy, and posting time
  - Google Calendar auth + API settings
  - Event timing and color IDs per content type

No external dependencies.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Repository root
# ---------------------------------------------------------------------------
# Milestone 3.0 (package refactor): this file now lives at
# <repo_root>/src/content_automation/config.py, not <repo_root>/config.py —
# every default path below that used to be Path(__file__).with_name(...)
# (correct only when config.py sat directly at repo root) now anchors off
# REPO_ROOT instead, so .env/data/content resolve to the exact same real
# repo-root locations regardless of where config.py itself lives inside the
# package. Exported (not module-private) so other relocated tooling
# (migrate_relocated_paths.py, the evaluation scripts) can anchor off the
# same single source of truth instead of each recomputing their own
# Path(__file__)-relative guess.
REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Local environment file
# ---------------------------------------------------------------------------
# Load simple KEY=value pairs from .env so local tool settings work without
# requiring shell exports or an extra python-dotenv dependency.
ENV_FILE = REPO_ROOT / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# Content pillar definitions
# Updated: September 2026 — replaced the agency-oriented pillar set
# (acquisition/building/execution/mindset) with a provisional
# engineering-focused set for testing classification/routing against the
# current content strategy. See PROJECT_STATE.md ("Active Pillar Strategy")
# and the 2026-09-12 CHANGELOG.md entry.
# Weights must sum to 1.0 (validated by scheduling.validate_pillar_weights).
# ---------------------------------------------------------------------------
CONTENT_TYPES: dict[str, dict[str, str | float | list[str]]] = {
    "engineering": {
        "label": "Software Engineering & Building",
        "color_id": "10",
        "weight": 0.40,
        "description": (
            "Software engineering, technical projects, AI agents, automation, "
            "system architecture, APIs, debugging, implementation decisions, "
            "and lessons learned while building software."
        ),
        "classification_examples": [
            "Explaining how a software system or AI agent was architected.",
            "Walking through a technical decision made while building a project.",
            "Discussing an API integration, automation, or engineering workflow.",
            "Sharing a bug, performance problem, or implementation lesson.",
        ],
    },
    "career": {
        "label": "Early-Career Software Engineering",
        "color_id": "9",
        "weight": 0.30,
        "description": (
            "The process of becoming a stronger early-career software engineer "
            "and breaking into startup engineering roles: job searching, "
            "interviews, skill development, career decisions, and lessons from "
            "trying to enter the software industry."
        ),
        "classification_examples": [
            "Discussing how to land a first software engineering or startup role.",
            "Reflecting on an engineering interview or job application.",
            "Explaining what an early-career engineer should learn.",
            "Talking about career strategy, resumes, portfolios, or engineering experience.",
        ],
    },
    "building_in_public": {
        "label": "Building in Public",
        "color_id": "5",
        "weight": 0.20,
        "description": (
            "Documenting the real process of building projects and developing "
            "as an engineer: progress updates, experiments, failures, changes "
            "in direction, lessons learned, and what is currently being worked on."
        ),
        "classification_examples": [
            "Sharing progress on a software project currently being built.",
            "Talking about what changed in a project this week.",
            "Reflecting on an experiment that succeeded or failed.",
            "Documenting the process of learning or building something publicly.",
        ],
    },
    "mindset": {
        "label": "Mindset & Discipline",
        "color_id": "3",
        "weight": 0.10,
        "description": (
            "Personal development, discipline, consistency, resilience, and "
            "reflection related to pursuing engineering, building projects, "
            "learning, and long-term career growth."
        ),
        "classification_examples": [
            "Reflecting on discipline or consistency while learning engineering.",
            "Talking about dealing with rejection or setbacks.",
            "Discussing persistence while building a difficult project.",
            "Sharing a personal lesson about growth, courage, or long-term effort.",
        ],
    },
}

# ---------------------------------------------------------------------------
# Default run parameters (overridden by CLI flags)
# ---------------------------------------------------------------------------
DEFAULT_MONTH = int(os.getenv("CONTENT_CALENDAR_DEFAULT_MONTH", "6"))
DEFAULT_YEAR = int(os.getenv("CONTENT_CALENDAR_DEFAULT_YEAR", "2026"))

# ---------------------------------------------------------------------------
# Routing strategy
# Added: Milestone 1.3 — "fifo" (default) schedules videos deterministically
# in ingestion order into untyped slots, with no pillar/classifier involved.
# "pillar" preserves the prior classify-then-match strategy as an opt-in
# mode. Validated by scheduling.validate_routing_mode() at first use (same
# pattern as CLASSIFIER/classification.build_classifier()), not at import
# time. See docs/decisions/0005-fifo-baseline-and-optional-strategy-routing.md.
# ---------------------------------------------------------------------------
ROUTING_MODE = os.getenv("CONTENT_CALENDAR_ROUTING_MODE", "fifo")

# ---------------------------------------------------------------------------
# Caption strategy
# Added: Milestone 1.3. "transcript_auto" derives a caption candidate from
# the video's transcript (caption.build_caption_from_transcript); "manual"
# never writes caption_text itself (reserved for a future editing UI) but
# still records caption_source so the stage is idempotent; "none" produces
# no caption. Validated by caption.validate_caption_mode() at first use.
# ---------------------------------------------------------------------------
CAPTION_MODE = os.getenv("CONTENT_CALENDAR_CAPTION_MODE", "transcript_auto")

# Upper bound on a user-saved canonical caption (Milestone 3.10, the
# captions API). A sanity limit only, deliberately well above any single
# platform's limit — platform limits (e.g. TIKTOK_MAX_CAPTION_UTF16_UNITS
# below) are enforced at each publisher boundary, not on the canonical text.
CAPTION_TEXT_MAX_CHARS = 10000

# ---------------------------------------------------------------------------
# Posting cadence and pillar allocation strategy
# Replaces the old fixed WEEKLY_SCHEDULE / FIFTH_SUNDAY_CONTENT_TYPE mapping,
# which coupled "when to post" to "what pillar" through weekday alone.
# See scheduling.py and docs/decisions/0002-configurable-cadence-and-weighted-pillar-allocation.md.
# ---------------------------------------------------------------------------

# Posts per week, 1-7. The month's actual slot count is derived from real
# calendar dates (scheduling.generate_posting_dates), not posts_per_week * 4.
POSTS_PER_WEEK = 4

# "auto" (scheduling.auto_posting_weekdays picks evenly-spaced weekdays) or an
# explicit list of exactly POSTS_PER_WEEK distinct weekday names, e.g.
# ["monday", "wednesday", "friday"]. Explicit days always override auto.
POSTING_DAYS: str | list[str] = "auto"

# "HH:MM" 24-hour local time (in TIMEZONE) applied to every generated slot.
POSTING_TIME = "09:00"

# Whether generated content_slots get a prompt attached from PROMPTS
# (rotated per pillar, same rule as before: sequential, wraps only after
# every prompt in the list has been used once). When False, every
# content_slot.prompt is None. Either way, scheduling and slot matching
# never depend on prompt text — see slot_matcher.py.
PROMPT_GENERATION_ENABLED = True

# ---------------------------------------------------------------------------
# Google Calendar API
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Path to service account JSON key — expand ~ at runtime
SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "~/growth_agency/credentials/service-account.json",
)

# Legacy/advanced-override calendar ID. No longer used as an implicit
# default anywhere — --calendar (generate_calendar.py, clear_calendar.py)
# defaults to None so the dedicated app calendar (below) is always the
# normal-operation target. Kept only so an explicit --calendar <id> without
# a value has something sane to reference; never auto-applied. See
# docs/decisions/0004-dedicated-google-calendar-ownership.md.
DEFAULT_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Timezone string used for all events
TIMEZONE = os.getenv("CONTENT_CALENDAR_TIMEZONE", "America/New_York")

# Milestone 3.8 (hosted scheduling cadence configuration) — how many days
# ahead of "now" calendar.hosted_cadence.generate_slot_datetimes() generates
# content_slots for, per user, each time a cadence is saved. Deliberately a
# fixed, documented, bounded window (per the milestone's own brief: "a
# reasonable initial generation horizon... rather than inventing an
# indefinite calendar") — 4 weeks is long enough for a meaningful upcoming-
# slots preview and FIFO runway, short enough to never run away into years
# of rows for a cadence that's never touched again. Unrelated to the CLI/
# global cadence path above (POSTS_PER_WEEK/POSTING_DAYS/POSTING_TIME),
# which generates one calendar month at a time instead.
CADENCE_GENERATION_HORIZON_DAYS = 28

# ---------------------------------------------------------------------------
# Dedicated app-owned Google Calendar
# Added: September 2026. Normal operation (no --calendar override) always
# targets this calendar, created/owned via user OAuth (not the shared
# service account) so a human owns it, per Google's guidance for apps that
# create secondary calendars. See
# docs/decisions/0004-dedicated-google-calendar-ownership.md.
# ---------------------------------------------------------------------------

# Display name/description for the calendar the app creates on first real
# (non-dry-run) generation, and reuses on every run after that.
APP_CALENDAR_SUMMARY = os.getenv("CONTENT_CALENDAR_APP_CALENDAR_SUMMARY", "Content Automation")
APP_CALENDAR_DESCRIPTION = (
    "Managed by Content Automation (~/content-automation). "
    "Events here are created/cleared by the app — avoid adding unrelated events."
)

# Persisted identity of the dedicated calendar (calendar_id + summary), so
# it is reused rather than recreated on every run. Not a display-name
# lookup — display names are not unique.
APP_CALENDAR_STATE_PATH = Path(
    os.getenv(
        "CONTENT_CALENDAR_APP_CALENDAR_STATE_PATH",
        str(REPO_ROOT / "data" / "calendar_state.json"),
    )
).expanduser()

# OAuth (not the service account) is used specifically for creating/owning
# the dedicated calendar and writing/clearing its events, so a human account
# owns it. google-auth-oauthlib's InstalledAppFlow caches a refresh token
# after one interactive browser consent, so only the very first real run
# needs a browser. CALENDAR_OAUTH_CLIENT_SECRETS_PATH must be downloaded
# once from Google Cloud Console (OAuth 2.0 Client ID, Desktop app type, on
# a project with the Calendar API enabled) — see README.md setup.
CALENDAR_OAUTH_SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_OAUTH_CLIENT_SECRETS_PATH = Path(
    os.getenv(
        "CONTENT_CALENDAR_OAUTH_CLIENT_SECRETS",
        str(Path.home() / ".config" / "content-calendar" / "calendar_oauth_client_secrets.json"),
    )
).expanduser()
CALENDAR_OAUTH_TOKEN_PATH = Path(
    os.getenv(
        "CONTENT_CALENDAR_OAUTH_TOKEN_PATH",
        str(Path.home() / ".config" / "content-calendar" / "calendar_oauth_token.json"),
    )
).expanduser()

# ---------------------------------------------------------------------------
# Event timing
# Start time now comes from POSTING_TIME above; this only sets event length.
# ---------------------------------------------------------------------------
EVENT_DURATION_MINUTES = 30

# ---------------------------------------------------------------------------
# Content processing pipeline (process_content.py)
# Added: September 2026 — video ingestion, transcription, classification,
# and slot routing. See docs/decisions/0001-video-ingestion-pipeline.md.
# ---------------------------------------------------------------------------

# SQLite database holding videos and content_slots records. Still the
# default/dev/test backend after Milestone 3.3 — see
# docs/decisions/0008-postgres-persistence-migration.md's "Local Development
# Strategy" for why SQLite was kept for fast unit tests rather than requiring
# networked Postgres for every test run.
DB_PATH = Path(
    os.getenv(
        "CONTENT_CALENDAR_DB_PATH",
        str(REPO_ROOT / "data" / "content.db"),
    )
).expanduser()

# ---------------------------------------------------------------------------
# Postgres (Milestone 3.3 — hosted persistence backend)
# Added: September 2026. A real, standard libpq connection string
# (postgresql://user:password@host:port/dbname), never committed — set in
# .env only. Empty by default, matching TIKTOK_CLIENT_KEY/SECRET's own
# "missing setup fails with a clear, actionable error rather than silently
# doing something wrong" pattern: an empty DATABASE_URL means "use SQLite"
# (persistence.store_factory.build_content_store's explicit selection rule),
# never a silent partial/broken Postgres attempt. See
# docs/decisions/0008-postgres-persistence-migration.md.
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Which Postgres schema PostgresContentStore operates against. "public" is
# the real hosted-application schema; the Postgres integration test suite
# overrides this (POSTGRES_TEST_SCHEMA) to a dedicated, disposable schema so
# tests never touch real business rows — see
# tests/test_postgres_content_store.py and Phase 22 of the Milestone 3.3
# evaluation record.
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "public")
POSTGRES_TEST_SCHEMA = os.getenv("POSTGRES_TEST_SCHEMA", "pickle_batch_test")

# ---------------------------------------------------------------------------
# Object storage (Milestone 3.4 — media lifecycle)
# "local" (default) keeps every existing local-filesystem behavior in
# media/processing.py and scheduling/publish_tiktok.py completely unchanged
# — nothing in the existing ingestion/publishing pipeline requires object
# storage to be configured. "supabase" enables the real hosted backend
# (storage.supabase_storage.SupabaseStorage) for the new, additive
# upload/materialize capability (media/media_storage.py) — see
# docs/decisions/0009-object-storage-media-lifecycle.md.
# ---------------------------------------------------------------------------
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")

# Base directory LocalStorage stores objects under. Deliberately separate
# from CONTENT_DIR below (content/incoming|processed|failed/ are the
# existing ingestion pipeline's own directories, untouched by this
# milestone) — this is the new object-storage abstraction's own local
# implementation, used for dev/test and whenever STORAGE_BACKEND=local.
LOCAL_STORAGE_ROOT = Path(
    os.getenv("LOCAL_STORAGE_ROOT", str(REPO_ROOT / "data" / "media_storage"))
).expanduser()

# Supabase Storage — reuses the same project as DATABASE_URL (Milestone
# 3.3), but this is a genuinely separate decision (object storage, not
# Postgres) — see the ADR for why Supabase Storage was still evaluated on
# its own merits, not chosen merely for stack consistency. SUPABASE_URL is
# the same project URL already used to derive the Postgres pooler host.
# The service-role key is server-side only, full storage access, never
# exposed client-side — note the .env variable is literally named
# SERVICE_ROLE_KEY (not SUPABASE_SERVICE_ROLE_KEY), matching how it was
# actually provisioned; config exposes it under the clearer name below.
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SERVICE_ROLE_KEY", "")

# Private bucket (created via the Storage API, not the dashboard — see
# Milestone 3.4 evaluation record) holding real canonical media.
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "pickle-batch-media")

# Separate, disposable bucket the storage integration test suite uses
# instead of SUPABASE_STORAGE_BUCKET — never the real media bucket. Mirrors
# POSTGRES_TEST_SCHEMA's role for the Postgres integration tests.
SUPABASE_STORAGE_TEST_BUCKET = os.getenv("SUPABASE_STORAGE_TEST_BUCKET", "pickle-batch-test")

# File lifecycle directories for process_content.py.
CONTENT_DIR = REPO_ROOT / "content"
INCOMING_DIR = CONTENT_DIR / "incoming"
PROCESSED_DIR = CONTENT_DIR / "processed"
FAILED_DIR = CONTENT_DIR / "failed"

# Video file extensions process_content.py will discover in INCOMING_DIR.
SUPPORTED_VIDEO_EXTENSIONS = {".mov", ".mp4"}

# Claude-classifier-specific confidence threshold for automatic slot routing
# (ClaudeClassifier applies this itself before returning a result). Below
# this, ClaudeClassifier reports pillar=None and process_content.py treats it
# as NEEDS_REVIEW. EmbeddingClassifier uses its own two-gate policy instead
# (EMBEDDING_MIN_SIMILARITY / EMBEDDING_MIN_MARGIN below) — see
# docs/decisions/0003-local-embedding-classification.md.
AUTO_ASSIGN_THRESHOLD = float(os.getenv("CONTENT_CALENDAR_AUTO_ASSIGN_THRESHOLD", "0.80"))

# --- Transcription (classification.transcription.FasterWhisperTranscriber) ---
WHISPER_MODEL_SIZE = os.getenv("CONTENT_CALENDAR_WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("CONTENT_CALENDAR_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("CONTENT_CALENDAR_WHISPER_COMPUTE_TYPE", "int8")

# --- Classification ---
# Added: September 2026 — local embedding classification, with Claude kept as
# an optional comparison/reference implementation. See
# docs/decisions/0003-local-embedding-classification.md.
# Valid values: "embeddings" (default, fully local, no API key) or "claude".
CLASSIFIER = os.getenv("CONTENT_CALENDAR_CLASSIFIER", "embeddings")

# classification.ClaudeClassifier
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CONTENT_CALENDAR_CLAUDE_MODEL", "claude-sonnet-5")

# classification.EmbeddingClassifier — local, CPU-only sentence embeddings via
# fastembed (ONNX runtime backend; no torch, no CUDA). Default model is
# BAAI/bge-small-en-v1.5: 384-dim, ~65MB quantized ONNX weights, downloaded
# once to EMBEDDING_CACHE_DIR on first use (a few seconds), then runs fully
# offline. CPU-only; fastembed does not require a GPU.
EMBEDDING_MODEL = os.getenv("CONTENT_CALENDAR_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_CACHE_DIR = Path(
    os.getenv("CONTENT_CALENDAR_EMBEDDING_CACHE_DIR", str(Path.home() / ".cache" / "content-calendar" / "fastembed"))
).expanduser()

# Two-gate auto-assign policy for EmbeddingClassifier: the top pillar's cosine
# similarity must clear EMBEDDING_MIN_SIMILARITY, AND its margin over the
# second-best pillar must clear EMBEDDING_MIN_MARGIN. Below either gate, the
# video is marked NEEDS_REVIEW rather than auto-assigned.
#
# THESE ARE UNCALIBRATED PLACEHOLDER DEFAULTS, not production-quality values —
# use evaluate_classifier.py --sweep against a real labeled transcript corpus
# to pick real thresholds before relying on this for unattended scheduling.
EMBEDDING_MIN_SIMILARITY = float(os.getenv("CONTENT_CALENDAR_EMBEDDING_MIN_SIMILARITY", "0.50"))
EMBEDDING_MIN_MARGIN = float(os.getenv("CONTENT_CALENDAR_EMBEDDING_MIN_MARGIN", "0.03"))

# --- TikTok generic compatibility targets ---
# https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide
# Used by media.is_tiktok_compatible() as an informational pre-flight check;
# tiktok_publisher.py does not transcode a video that fails it.
TIKTOK_CONTAINERS = {"mov", "mp4", "webm"}
TIKTOK_VIDEO_CODECS = {"h264", "hevc", "vp8", "vp9"}

# ---------------------------------------------------------------------------
# Real user authentication (Milestone 3.6 — Supabase Auth)
# Replaces the frontend's Milestone 3.5 mock session. The backend never
# authenticates a user directly — it verifies a Supabase Auth JWT the
# frontend already obtained (Google as the first provider), the same
# "browser -> identity provider -> backend verifies" shape ADR-0010's
# session boundary was written to support. See
# docs/decisions/0011-real-authentication-and-tiktok-connection.md.
#
# SUPABASE_AUTH_JWT_MODE selects how the backend verifies a token:
#   "jwks"  (default) — Supabase's newer asymmetric signing keys. No shared
#            secret needed server-side; the public JWKS is fetched (and
#            cached) from SUPABASE_AUTH_JWKS_URL, derived from SUPABASE_URL
#            when not set explicitly. Strictly preferred when available.
#   "hs256" — Supabase's legacy shared JWT secret. Requires
#            SUPABASE_JWT_SECRET, which must never be exposed client-side
#            or logged (see ~/.agents/SECURITY.md).
# ---------------------------------------------------------------------------
SUPABASE_AUTH_JWT_MODE = os.getenv("SUPABASE_AUTH_JWT_MODE", "jwks")
SUPABASE_AUTH_JWKS_URL = os.getenv(
    "SUPABASE_AUTH_JWKS_URL",
    f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if SUPABASE_URL else "",
)
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
# Supabase's default JWT "aud" (audience) claim for a signed-in user.
SUPABASE_AUTH_AUDIENCE = os.getenv("SUPABASE_AUTH_AUDIENCE", "authenticated")

# ---------------------------------------------------------------------------
# Hosted platform-credential encryption (Milestone 3.6)
# A hosted TikTok connection's access/refresh token pair is encrypted at
# rest with this symmetric key (Fernet — AES-128-CBC + HMAC, from the
# `cryptography` package already a transitive dependency of google-auth)
# before being stored in platform_credentials.encrypted_payload. Server-side
# only; never exposed client-side, never logged. Generate one with
# `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
# and set it in .env — there is no default, matching TIKTOK_CLIENT_KEY's
# "missing setup fails clearly" pattern. See
# docs/decisions/0011-real-authentication-and-tiktok-connection.md
# "Credential Storage".
# ---------------------------------------------------------------------------
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")

# ---------------------------------------------------------------------------
# API (Milestone 3.6 — first real FastAPI surface, src/content_automation/api/)
# CORS allowlist for the frontend origin(s) actually allowed to call the
# authenticated API — deliberately never "*" (a wildcard would defeat the
# point of verifying identity at all). Comma-separated in .env.
# ---------------------------------------------------------------------------
API_CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("API_CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

# Where the TikTok OAuth callback (api/routes/platforms_tiktok.py) redirects
# the user's browser back to once it has finished — a top-level browser
# navigation TikTok controls, not an XHR the frontend's own fetch client
# handles, so this must be a real, absolute frontend URL. First entry of
# API_CORS_ALLOWED_ORIGINS by default (same origin the frontend is already
# configured to call this API from) unless explicitly overridden. An empty
# value (the blank line .env.example ships) counts as unset, like every other
# optional setting, so it falls back instead of leaving the OAuth callbacks
# with no frontend to return to (Milestone 4.1 fix).
FRONTEND_BASE_URL = (os.getenv("FRONTEND_BASE_URL") or "").strip() or (
    API_CORS_ALLOWED_ORIGINS[0] if API_CORS_ALLOWED_ORIGINS else ""
)

# The TikTok redirect_uri the hosted OAuth flow (api/routes/platforms_tiktok.py)
# sends to TikTok and registers state under — TikTok's own callback URL,
# not FRONTEND_BASE_URL above. Deliberately a fixed, explicitly configured
# value rather than derived from the incoming request (Starlette's
# `request.url_for()`): behind a reverse proxy/hosting platform, uvicorn
# sees the raw (often http://internal-host:port) connection unless
# proxy-header trust is separately configured, so a request-derived value
# can silently drift from whatever exact URI is registered in TikTok's
# Developer Portal, breaking the flow (or worse, matching a proxy's
# internal host and never being registerable at all). Every leg of the
# transaction (authorization URL, oauth_states persistence, callback token
# exchange) must use this exact same string — see
# api/routes/platforms_tiktok.py's connect/callback handlers. No default
# and no scheme fallback: an empty or non-https value fails clearly at
# connect-time rather than silently sending TikTok a value that can never
# match a real registration. Distinct from TIKTOK_REDIRECT_URI above,
# which only feeds the separate local desktop/CLI flow (tiktok_auth.py)
# and is allowed to be a plain-http loopback address.
TIKTOK_WEB_REDIRECT_URI = os.getenv("TIKTOK_WEB_REDIRECT_URI", "")

# How long a pending OAuth attempt's server-side state (oauth_states row) is
# considered valid before it's treated as expired — mirrors the intent of
# tiktok_auth.py's manual-fallback pending cache, but DB-backed and
# user-bound instead of file-backed and single-process, since the hosted
# callback runs in a stateless API request, not the same process that
# started the flow.
OAUTH_STATE_TTL_SECONDS = int(os.getenv("OAUTH_STATE_TTL_SECONDS", "600"))

# Milestone 4.1: extra exact URLs an OAuth attempt may return the browser to
# after its callback (api/oauth_return_targets.py), comma-separated, in
# addition to the always-allowed web Settings page
# (FRONTEND_BASE_URL + "/app/settings"). Empty by default: the web app
# needs nothing else. Reserved for a future native app scheme or universal
# link. Matched exactly — never a prefix, host or pattern — and only
# https:// (or http:// on localhost) entries are honored.
OAUTH_EXTRA_RETURN_TARGETS = [
    target.strip() for target in os.getenv("OAUTH_EXTRA_RETURN_TARGETS", "").split(",") if target.strip()
]

# ---------------------------------------------------------------------------
# Instagram (Milestone 4.0 — configuration contract only; the OAuth flow
# arrives in 4.1 and Reels publishing in 4.2). Uses the Instagram API with
# Instagram Login (Business Login for Instagram): Instagram professional
# accounts (Business or Creator), no linked Facebook Page. See
# docs/decisions/0018-instagram-integration-architecture.md.
# ---------------------------------------------------------------------------

# The Instagram app ID and secret from the Meta App Dashboard's
# "Instagram > API setup with Instagram login" page — distinct from the
# Meta (Facebook) app ID. Needed by the API process only (authorization
# URL + code exchange). The worker never needs the secret: long-lived token
# refresh and publishing use only the user's access token. Never committed;
# empty by default so a missing setup fails clearly.
INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID", "")
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", "")

# The backend callback Instagram redirects to after authorization. Must be
# https and match an entry under "Business login settings > OAuth redirect
# URIs" exactly — same fixed-value reasoning as TIKTOK_WEB_REDIRECT_URI.
INSTAGRAM_REDIRECT_URI = os.getenv("INSTAGRAM_REDIRECT_URI", "")

# Instagram Login hosts (verified against Meta's docs, 2026-10-04):
# authorization on www.instagram.com, code exchange on api.instagram.com,
# long-lived token exchange/refresh and the Content Publishing API on
# graph.instagram.com. Overridable for tests only.
INSTAGRAM_AUTHORIZE_URL = os.getenv("CONTENT_CALENDAR_INSTAGRAM_AUTHORIZE_URL", "https://www.instagram.com/oauth/authorize")
INSTAGRAM_TOKEN_URL = os.getenv("CONTENT_CALENDAR_INSTAGRAM_TOKEN_URL", "https://api.instagram.com/oauth/access_token")
INSTAGRAM_GRAPH_BASE = os.getenv("CONTENT_CALENDAR_INSTAGRAM_GRAPH_BASE", "https://graph.instagram.com")
# Pinned Graph API version for Content Publishing calls (Meta's current
# examples use v25.0). Bump deliberately, not implicitly.
INSTAGRAM_GRAPH_API_VERSION = os.getenv("INSTAGRAM_GRAPH_API_VERSION") or "v25.0"

# Lifetime of the signed storage URL handed to Instagram as a Reel's
# video_url. Meta fetches the video while the container is IN_PROGRESS,
# normally minutes after creation; one hour leaves margin without leaving a
# long-lived link to private media. If it expires before Meta finishes,
# the container goes to ERROR and a retry creates a new container with a
# fresh URL (an unpublished container never posts). Bounded to 5 min–24 h
# by storage.signed_urls.
# `or` (not a getenv default) so an empty line copied from .env.example
# falls back instead of crashing int("") at import.
INSTAGRAM_MEDIA_URL_TTL_SECONDS = int(os.getenv("INSTAGRAM_MEDIA_URL_TTL_SECONDS") or "3600")

# Milestone 4.2.1: the longest the worker spends making an Instagram-compatible
# derivative of one video before failing it (INSTAGRAM_MEDIA_PREPARATION_TIMEOUT).
# The claimed post's updated_at is refreshed about once a minute meanwhile, so
# crash recovery never treats a long encode as a stalled claim.
INSTAGRAM_NORMALIZATION_TIMEOUT_SECONDS = int(os.getenv("INSTAGRAM_NORMALIZATION_TIMEOUT_SECONDS") or "3600")

# Milestone 4.1: when a stored long-lived Instagram token (60 days) is
# refreshed. Meta allows a refresh once the token is at least 24 hours old
# and still valid; this refreshes on first use once fewer than this many
# seconds of validity remain (default 30 days, i.e. about halfway), so a
# connection that is used at least monthly — a publish, or a Settings visit
# — never lapses. Validated by publishing/instagram/configuration.py
# (between 1 and 59 days). See publishing/instagram/credential_store.py.
INSTAGRAM_TOKEN_REFRESH_WINDOW_SECONDS = int(os.getenv("INSTAGRAM_TOKEN_REFRESH_WINDOW_SECONDS") or str(30 * 86_400))

# ---------------------------------------------------------------------------
# TikTok publishing (Milestone 2.0, corrected)
# Added: September 2026 — proves one local MP4 can be published to a
# dedicated TikTok test account via the real Content Posting API v2, using
# publish_tiktok.py as a standalone manual CLI (not wired into scheduled
# execution). See docs/decisions/0006-tiktok-publisher-foundation.md.
#
# Deliberately a separate OAuth flow/credential set from Google Calendar's:
# different provider, different token cache file, different scopes — never
# reuses calendar_manager.py's client secrets or token path. Desktop OAuth
# uses PKCE + a localhost callback (TikTok's current Desktop Login Kit
# documentation supports this, corrected from an earlier assumption that it
# did not) — see tiktok_auth.py.
# ---------------------------------------------------------------------------

# TikTok API hosts. Authorization happens on the web host; token exchange,
# refresh, and all Content Posting API calls happen on the API host.
TIKTOK_AUTHORIZE_BASE = os.getenv("CONTENT_CALENDAR_TIKTOK_AUTHORIZE_BASE", "https://www.tiktok.com")
TIKTOK_API_BASE = os.getenv("CONTENT_CALENDAR_TIKTOK_API_BASE", "https://open.tiktokapis.com")

# From the TikTok Developer Portal app dashboard (Login Kit + Content
# Posting API scopes enabled) — never committed; set in .env only. Empty by
# default so a missing setup fails with a clear, actionable error rather
# than silently trying to authenticate with blank credentials.
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")

# Optional. TikTok's current Desktop Login Kit documentation supports
# localhost/127.0.0.1 redirect URIs (including a wildcard port) for desktop
# apps, so the preferred flow (tiktok_auth.authorize_interactive) starts a
# temporary local callback server on an OS-assigned ephemeral port and
# never needs this set at all. Set it only if this app's Developer Portal
# registration requires an exact fixed port/path rather than relying on
# wildcard-port matching, or to use the manual fallback flow
# (--print-auth-url / --exchange-code) instead of the interactive one. See
# tiktok_auth.py / README.md "TikTok Publishing Setup".
TIKTOK_REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI", "")

# Host/path for the temporary local OAuth callback server when
# TIKTOK_REDIRECT_URI is unset (the ephemeral-port default). 127.0.0.1
# rather than "localhost" to avoid any local DNS resolution ambiguity.
TIKTOK_LOOPBACK_HOST = os.getenv("CONTENT_CALENDAR_TIKTOK_LOOPBACK_HOST", "127.0.0.1")
TIKTOK_LOOPBACK_PATH = os.getenv("CONTENT_CALENDAR_TIKTOK_LOOPBACK_PATH", "/callback")

# Comma-separated in .env; user.info.basic is needed to complete OAuth at
# all, video.publish for the Content Posting API's Direct Post endpoints
# this milestone uses.
TIKTOK_SCOPES = [
    scope.strip()
    for scope in os.getenv("CONTENT_CALENDAR_TIKTOK_SCOPES", "user.info.basic,video.publish").split(",")
    if scope.strip()
]

# Cached access/refresh token pair, written by tiktok_auth.py after the
# one-time authorization flow. Outside the repo (like the Calendar OAuth
# token) so there is nothing publishing-credential-shaped to accidentally
# commit.
TIKTOK_TOKEN_PATH = Path(
    os.getenv(
        "CONTENT_CALENDAR_TIKTOK_TOKEN_PATH",
        str(Path.home() / ".config" / "content-calendar" / "tiktok_token.json"),
    )
).expanduser()

# Milestone 2.1.8: how long before a cached access token's documented
# expiry tiktok_auth.get_access_token() proactively refreshes it, so a
# token that's merely "technically still valid" at the start of a call
# doesn't expire mid-request (creator_info -> init -> upload -> polling).
# TikTok's access tokens live 24h (86400s) per its OAuth docs; a 5-minute
# default skew is centrally defined here (not scattered across call
# sites), matching the RETRY_BACKOFF_MINUTES/PLATFORM_POST_STALE_MINUTES
# env-overridable pattern.
TIKTOK_TOKEN_REFRESH_SKEW_SECONDS = int(os.getenv("CONTENT_CALENDAR_TIKTOK_TOKEN_REFRESH_SKEW_SECONDS", "300"))

# TikTok Direct Post privacy_level for every post this milestone creates.
# SELF_ONLY (private, visible only to the posting account) is the deliberate
# default and the only level TikTokPublisher(unaudited=True) (the default)
# will ever use — see ADR-0006. TikTok restricts unaudited Direct Post
# clients (this app has not completed TikTok's app review/audit) to
# SELF_ONLY regardless of what an account's other privacy_level_options
# report, so TikTokPublisher requires SELF_ONLY specifically rather than
# accepting or falling back to any other available level.
TIKTOK_DEFAULT_PRIVACY_LEVEL = os.getenv("CONTENT_CALENDAR_TIKTOK_DEFAULT_PRIVACY_LEVEL", "SELF_ONLY")

# TikTok's documented Direct Post caption limit, in UTF-16 code units (NOT
# Python characters — a character outside the Basic Multilingual Plane,
# e.g. many emoji, is 1 Python character but a 2-unit UTF-16 surrogate
# pair). tiktok_publisher.py validates against this and fails clearly
# rather than silently truncating the canonical videos.caption_text.
TIKTOK_MAX_CAPTION_UTF16_UNITS = int(os.getenv("CONTENT_CALENDAR_TIKTOK_MAX_CAPTION_UTF16_UNITS", "2200"))

# ---------------------------------------------------------------------------
# Platform-post materialization (Milestone 2.1.2)
# Which platform(s) a scheduled video should be materialized as a PENDING
# platform_posts row for, as soon as it's assigned a content_slot — see
# platform_post_materializer.py and
# docs/evaluations/scheduling/milestone-2.1.2-platform-post-materialization.md.
# Deliberately a plain list, not hardcoded to TikTok inside that module:
# "tiktok" is the only real publisher today (publisher.build_publisher), so
# adding a second platform here alone does nothing until a real Publisher
# implementation for it exists — this list only controls which platforms get
# a scheduling placeholder created, not which platforms can actually publish.
# ---------------------------------------------------------------------------
TARGET_PUBLISHING_PLATFORMS = [
    platform.strip()
    for platform in os.getenv("CONTENT_CALENDAR_TARGET_PUBLISHING_PLATFORMS", "tiktok").split(",")
    if platform.strip()
]

# ---------------------------------------------------------------------------
# Crash recovery (Milestone 2.1.5)
# How long a platform_posts row may sit in PUBLISHING with no activity
# before crash_recovery.py treats it as abandoned rather than owned by a
# still-running worker. platform_posts.updated_at (always written as an
# aware UTC isoformat string — see publish_tiktok._now_iso/worker._now_iso,
# NOT the naive-local-time convention scheduled_at uses) is the "last known
# activity" signal; no separate lease/heartbeat column was added for this
# milestone. Default is comfortably longer than the longest legitimate
# single execution attempt (tiktok_publisher.py's upload timeout alone is
# 300s) so an actively-running worker is never mistaken for a crashed one.
# See docs/evaluations/scheduling/milestone-2.1.5-crash-recovery.md.
# ---------------------------------------------------------------------------
PLATFORM_POST_STALE_MINUTES = int(os.getenv("CONTENT_CALENDAR_PLATFORM_POST_STALE_MINUTES", "30"))

# Milestone 3.11 (user-facing publish states): how long after a slot's
# scheduled_at a still-PENDING platform post (with no automatic retry
# pending) is shown as NEEDS_ATTENTION / SCHEDULE_MISSED rather than
# "Scheduled" — read-only display logic in publishing/publish_status.py;
# nothing is retried or rescheduled because of it.
PUBLISH_OVERDUE_GRACE_MINUTES = int(os.getenv("CONTENT_CALENDAR_PUBLISH_OVERDUE_GRACE_MINUTES", "30"))

# Milestone 3.12 (hosted worker): seconds between hosted scheduler cycles
# (cli/run_worker.py / scheduling/hosted_worker.py). Posts are scheduled to
# the minute, so 60s keeps publishing within about a minute of its time
# without hammering Postgres; each idle cycle is a handful of indexed reads.
WORKER_POLL_INTERVAL_SECONDS = float(os.getenv("CONTENT_CALENDAR_WORKER_POLL_INTERVAL_SECONDS", "60"))

# ---------------------------------------------------------------------------
# Retry classification and backoff (Milestone 2.1.6)
# Deterministic exponential-ish backoff for a RETRYABLE publishing failure
# (see retry_classification.py) — index 0 is the delay before the 1st retry,
# index 1 before the 2nd, and so on. len(RETRY_BACKOFF_MINUTES) is the
# retry ceiling: once retry_count reaches this, a further retryable failure
# becomes terminal (FAILED) instead of scheduling another attempt — Pickle
# Batch does not retry forever. Centralized here rather than scattered
# magic numbers, per this milestone's own instruction. See
# docs/evaluations/scheduling/milestone-2.1.6-retry-backoff.md.
# ---------------------------------------------------------------------------
RETRY_BACKOFF_MINUTES = [
    int(m.strip())
    for m in os.getenv("CONTENT_CALENDAR_RETRY_BACKOFF_MINUTES", "1,5,15,30").split(",")
    if m.strip()
]
MAX_RETRY_ATTEMPTS = len(RETRY_BACKOFF_MINUTES)

# ---------------------------------------------------------------------------
# Asynchronous publish reconciliation (Milestone 2.1.10)
# How long to wait before automatically re-checking a PUBLISHING row TikTok
# has already accepted (platform_post_id set) but has not yet finished
# processing (see reconciliation.py, publish_tiktok._resolve_poll_outcome).
# Indexed by platform_posts.status_check_count, same shape as
# RETRY_BACKOFF_MINUTES — but deliberately capped rather than exhausted:
# once status_check_count reaches the end of this list, further checks
# keep reusing the last (longest) interval indefinitely instead of ever
# giving up, since TikTok will eventually reach a terminal status and
# there is no equivalent to a retry budget here (nothing was ever
# resubmitted to "use up"). Seconds, not minutes — the first checks need
# finer granularity than a publishing retry does. See
# docs/evaluations/scheduling/milestone-2.1.10-asynchronous-publish-reconciliation.md.
# ---------------------------------------------------------------------------
STATUS_CHECK_BACKOFF_SECONDS = [
    int(s.strip())
    for s in os.getenv("CONTENT_CALENDAR_STATUS_CHECK_BACKOFF_SECONDS", "30,60,120,300,600").split(",")
    if s.strip()
]

# Milestone 3.13 (reconciliation + recovery): the cap on the "never give up"
# rule above. A submission TikTok accepted but that has still not reached a
# terminal status after this many checks (~24h at the default backoff — the
# last interval repeats) is parked as UNKNOWN for manual recovery instead of
# being shown as "Publishing…" forever. Nothing is resubmitted either way.
STATUS_CHECK_MAX_ATTEMPTS = int(os.getenv("CONTENT_CALENDAR_STATUS_CHECK_MAX_ATTEMPTS", "150"))

# Milestone 3.13: how long a hosted worker waits for another process that is
# already refreshing the same TikTok connection's token (Postgres advisory
# lock — publishing/tiktok/credential_store.py) before giving up with a
# retryable CREDENTIAL_REFRESH_BUSY. Comfortably above auth.py's own refresh
# request timeout, so a healthy concurrent refresh always finishes first.
CREDENTIAL_REFRESH_LOCK_TIMEOUT_SECONDS = int(os.getenv("CONTENT_CALENDAR_CREDENTIAL_REFRESH_LOCK_TIMEOUT_SECONDS", "45"))

# ---------------------------------------------------------------------------
