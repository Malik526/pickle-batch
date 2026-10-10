"""
content_publishing.py — the Instagram Content Publishing API calls Pickle
Batch makes for Reels (Milestone 4.2). The only module that talks to Meta's
publishing endpoints; InstagramPublisher (publisher.py) composes these.

What it does (graph.instagram.com, Instagram API with Instagram Login,
verified against Meta's docs 2026-10-04 — ADR-0018):
  create_reel_container  POST /<IG_ID>/media  media_type=REELS, video_url,
                         caption (only when non-empty) → container id.
                         Never posts anything by itself.
  get_container_status   GET /<container_id>?fields=status_code,status →
                         IN_PROGRESS | FINISHED | PUBLISHED | ERROR | EXPIRED
  publish_container      POST /<IG_ID>/media_publish creation_id=<container>
                         → Instagram media id. The only call that creates a
                         post.

Errors are InstagramPublishError (a PublishError) with a reason_code the
shared retry classifier and failure taxonomy understand, the HTTP status, and
Meta's numeric error code/subcode only. Messages never include the request
URL (the access token and, for container creation, the signed video_url are
request parameters), the token, or Meta's response body.

Dependencies:
  requests, content_automation.config, publishing.publisher.
"""

from dataclasses import dataclass

import requests

from content_automation import config
from content_automation.publishing.publisher import PublishError

_TIMEOUT_SECONDS = 60

# Meta error codes (Graph API error handling + content publishing errors).
_TOKEN_ERROR_CODES = frozenset({102, 190})
_RATE_LIMIT_CODES = frozenset({4, 17, 32, 613})
_PUBLISH_LIMIT_SUBCODE = 2207042  # account reached its 24-hour API publishing limit
_MEDIA_NOT_READY_CODE = 9007
_MEDIA_NOT_READY_SUBCODE = 2207027
_MEDIA_FETCH_FAILED_CODE = 9004


class InstagramPublishError(PublishError):
    def __init__(self, message: str, reason_code: str, http_status: int | None = None,
                 provider_error_code: int | None = None, provider_error_subcode: int | None = None):
        super().__init__(message, reason_code=reason_code, http_status=http_status)
        self.provider_error_code = provider_error_code
        self.provider_error_subcode = provider_error_subcode


@dataclass(frozen=True)
class ContainerStatus:
    status_code: str
    # Meta's human-readable status ("Error: …"), kept for internal
    # diagnostics only (platform_posts.failure_reason), never shown to users.
    detail: str | None


def _url(path: str) -> str:
    return f"{config.INSTAGRAM_GRAPH_BASE}/{config.INSTAGRAM_GRAPH_API_VERSION}/{path}"


def _error_from(step: str, status: int, payload: dict) -> InstagramPublishError:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = error.get("code") if isinstance(error.get("code"), int) else None
    subcode = error.get("error_subcode") if isinstance(error.get("error_subcode"), int) else None
    if code in _TOKEN_ERROR_CODES:
        reason = "REAUTHORIZATION_REQUIRED"
    elif subcode == _PUBLISH_LIMIT_SUBCODE or code in _RATE_LIMIT_CODES:
        reason = "INSTAGRAM_RATE_LIMITED"
    elif code == _MEDIA_NOT_READY_CODE or subcode == _MEDIA_NOT_READY_SUBCODE:
        reason = "INSTAGRAM_MEDIA_NOT_READY"
    elif code == _MEDIA_FETCH_FAILED_CODE:
        reason = "INSTAGRAM_MEDIA_FETCH_FAILED"
    elif error.get("is_transient") is True or status >= 500:
        reason = "INSTAGRAM_TRANSIENT_ERROR"
    else:
        reason = "INSTAGRAM_REQUEST_REJECTED"
    detail = f"code={code}" + (f" subcode={subcode}" if subcode is not None else "")
    return InstagramPublishError(
        f"Instagram rejected the request ({step}, HTTP {status}, {detail}).", reason_code=reason,
        http_status=status, provider_error_code=code, provider_error_subcode=subcode,
    )


def _request(step: str, method: str, path: str, *, access_token: str, data: dict | None = None,
             params: dict | None = None) -> dict:
    if method == "GET":
        params = {**(params or {}), "access_token": access_token}
    else:
        data = {**(data or {}), "access_token": access_token}
    try:
        response = requests.request(method, _url(path), data=data, params=params, timeout=_TIMEOUT_SECONDS)
    except requests.Timeout:
        raise InstagramPublishError(f"Instagram did not respond in time ({step}).", reason_code="NETWORK_ERROR") from None
    except requests.RequestException as exc:
        # No str(exc): requests' message can include the URL and parameters.
        raise InstagramPublishError(
            f"Could not reach Instagram ({step}): {type(exc).__name__}.", reason_code="NETWORK_ERROR",
        ) from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        if response.status_code >= 400:
            raise _error_from(step, response.status_code, {})
        raise InstagramPublishError(
            f"Instagram returned an unexpected response ({step}, HTTP {response.status_code}).",
            reason_code="MALFORMED_RESPONSE", http_status=response.status_code,
        )
    if response.status_code >= 400 or "error" in payload:
        raise _error_from(step, response.status_code, payload)
    return payload


def _required_id(payload: dict, step: str) -> str:
    value = payload.get("id")
    if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
        return str(value).strip()
    raise InstagramPublishError(f"Instagram {step} response is missing an id.", reason_code="MALFORMED_RESPONSE")


def create_reel_container(ig_user_id: str, access_token: str, video_url: str, caption: str | None) -> str:
    data = {"media_type": "REELS", "video_url": video_url}
    if caption:
        data["caption"] = caption
    payload = _request("container creation", "POST", f"{ig_user_id}/media", access_token=access_token, data=data)
    return _required_id(payload, "container creation")


def get_container_status(container_id: str, access_token: str) -> ContainerStatus:
    payload = _request(
        "container status", "GET", container_id, access_token=access_token, params={"fields": "status_code,status"},
    )
    status_code = payload.get("status_code")
    if not isinstance(status_code, str) or not status_code:
        raise InstagramPublishError("Instagram container status response is missing status_code.", reason_code="MALFORMED_RESPONSE")
    detail = payload.get("status")
    return ContainerStatus(status_code=status_code, detail=detail[:300] if isinstance(detail, str) else None)


def publish_container(ig_user_id: str, access_token: str, container_id: str) -> str:
    payload = _request(
        "media publish", "POST", f"{ig_user_id}/media_publish", access_token=access_token,
        data={"creation_id": container_id},
    )
    return _required_id(payload, "media publish")
