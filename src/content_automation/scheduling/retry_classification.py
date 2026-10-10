"""
retry_classification.py — Distinguishes retryable from terminal publishing
failures (Milestone 2.1.6).

What it does:
  Answers, for a given publisher.PublishError: should Pickle Batch try this
  submission again later, or is it permanently stuck until something
  material changes (the file, the caption, the account settings, the
  credentials)? Classification is driven entirely by the error's existing
  structured fields — reason_code (the machine-stable label every
  PublishError already carries, per its own docstring, specifically "so
  callers can persist/report it without parsing the message string") and
  http_status (added this milestone, threaded through every
  tiktok_publisher.py raise site where a real HTTP response was actually
  available) — never by parsing the free-text message.

  Built from the real current PublishError model, inventoried directly
  from tiktok_publisher.py and publish_tiktok.py — not invented ahead of
  investigation. See
  docs/evaluations/scheduling/milestone-2.1.6-retry-backoff.md ("Failure
  Taxonomy") for the full accounting of every reason_code this codebase
  currently raises and why each is classified the way it is.

  Local precondition/validation failures (missing file, missing caption,
  incompatible container/codec — publish_tiktok._validate_ready_to_publish,
  which raises PublishTikTokError, not PublishError) are not classified
  here at all: they are unconditionally terminal by nature — nothing about
  waiting and retrying changes a file that doesn't exist — and are handled
  directly at the call site (execute_claimed_platform_post) rather than
  routed through this module.

  Default is TERMINAL, not retryable: an unrecognized reason_code (e.g. a
  real TikTok-reported API error code this codebase has no documented
  knowledge of — TIKTOK_API_ERROR's `error_code or "TIKTOK_API_ERROR"`
  fallback in tiktok_publisher._parse_response can carry any string TikTok
  chooses to send) is treated as terminal rather than blindly retried —
  "fail closed" rather than hammering an endpoint that explicitly rejected
  the request for an unknown reason.

Dependencies:
  publisher.PublishError. No I/O.
"""

# Always retryable regardless of http_status — transport/availability
# failures with no evidence anything about the request itself was wrong.
_RETRYABLE_REASON_CODES = frozenset({
    "NETWORK_ERROR",         # requests.RequestException reaching creator_info/init/status
    "UPLOAD_NETWORK_ERROR",  # requests.RequestException during the upload PUT itself
    # Milestone 3.12: Supabase Storage unreachable while materializing hosted
    # media (publish_tiktok._materialization_publish_error). Same transport
    # class as NETWORK_ERROR; STORAGE_HTTP_ERROR is left to the http_status
    # fallback below, like HTTP_ERROR.
    "STORAGE_NETWORK_ERROR",
    # Milestone 3.13: another process held this connection's token-refresh
    # lock past the timeout (publishing/tiktok/credential_store.py) — it
    # will have finished by the next attempt.
    "CREDENTIAL_REFRESH_BUSY",
    # Milestone 4.2 (publishing/instagram/content_publishing.py): Meta hasn't
    # finished processing the container yet; a temporary/transient Meta
    # error; Meta couldn't fetch the signed media URL (a fresh URL is issued
    # on every attempt); the account's API publishing limit — a few spaced
    # retries before the post is marked FAILED.
    "INSTAGRAM_MEDIA_NOT_READY",
    "INSTAGRAM_TRANSIENT_ERROR",
    "INSTAGRAM_MEDIA_FETCH_FAILED",
    "INSTAGRAM_RATE_LIMITED",
})

# Always terminal regardless of http_status — local validation, account/
# config restrictions, or credential problems that time alone never
# resolves; retrying without a human/pipeline change would just fail the
# same way again.
_TERMINAL_REASON_CODES = frozenset({
    "LOCAL_FILE_MISSING",
    "CAPTION_TOO_LONG",
    "CORRUPT_MEDIA",
    "VIDEO_TOO_LONG",
    "SELF_ONLY_UNAVAILABLE",
    "UNSUPPORTED_PRIVACY_LEVEL",
    "UNAUDITED_CLIENT_PRIVACY_RESTRICTION",
    "AUTH_ERROR",
    # Milestone 2.1.8: tiktok_auth.TikTokReauthorizationRequiredError always
    # carries this code — the refresh token is expired/revoked/never
    # issued, or TikTok's token endpoint explicitly rejected a refresh
    # request. Listed explicitly (not left to the http_status fallback)
    # so it stays terminal even though the underlying failure may itself
    # carry a 4xx http_status that would otherwise route through the same
    # fallback path other unrecognized codes use.
    "REAUTHORIZATION_REQUIRED",
    # Milestone 4.2: Meta rejected the request for a reason time won't fix,
    # and the Instagram Reels media/caption checks
    # (publishing/instagram/media_requirements.py).
    "INSTAGRAM_REQUEST_REJECTED",
    "INSTAGRAM_MEDIA_UNSUPPORTED_FORMAT",
    "INSTAGRAM_MEDIA_UNSUPPORTED_CODEC",
    "INSTAGRAM_MEDIA_DURATION",
    "INSTAGRAM_MEDIA_FRAME_RATE",
    "INSTAGRAM_MEDIA_TOO_LARGE",
    "INSTAGRAM_MEDIA_RESOLUTION",
    "INSTAGRAM_CAPTION_TOO_MANY_HASHTAGS",
    "INSTAGRAM_CAPTION_TOO_MANY_MENTIONS",
    # Milestone 4.2.1: the same file fails the same way on every attempt.
    "INSTAGRAM_MEDIA_PREPARATION_FAILED",
    "INSTAGRAM_MEDIA_PREPARATION_TIMEOUT",
})


def is_retryable(reason_code: str, http_status: int | None = None) -> bool:
    """True if a failure with this reason_code/http_status should be
    retried later; False if it's terminal.

    reason_code is checked against the explicit allow/deny lists above
    first. If it matches neither (e.g. HTTP_ERROR, MALFORMED_RESPONSE, or
    a dynamic TikTok-reported API error code under TIKTOK_API_ERROR),
    http_status decides when one is available: 5xx is treated as a
    transient server-side problem (retryable), 4xx as a request TikTok
    explicitly rejected (terminal). If nothing resolves it either way,
    the result is terminal — fail closed rather than retry blindly.
    """
    if reason_code in _RETRYABLE_REASON_CODES:
        return True
    if reason_code in _TERMINAL_REASON_CODES:
        return False
    if http_status is not None:
        if http_status >= 500:
            return True
        if 400 <= http_status < 500:
            return False
    return False


def classify(error) -> bool:
    """Convenience wrapper: classify a publisher.PublishError instance
    directly, using its reason_code and http_status attributes."""
    return is_retryable(error.reason_code, getattr(error, "http_status", None))
