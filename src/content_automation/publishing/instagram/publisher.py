"""
publisher.py — InstagramPublisher: the Publisher for Instagram Reels
(Milestone 4.2), and the hosted per-user factory the worker uses.

What it does:
  Maps Meta's two-step container lifecycle onto the shared Publisher
  contract (publishing/publisher.py) so the existing claim / checkpoint /
  reconciliation / crash-recovery / manual-retry machinery drives Instagram
  without a second state machine:

    publish(media_url=...)  create the Reels container from a short-lived
                            signed URL, then hand the container id to
                            on_platform_post_id. Creating a container never
                            posts, so "no id persisted" means "nothing can
                            exist on Instagram" — the same guarantee
                            reports_platform_post_id_before_media_transfer
                            gives for TikTok.
    get_status(container)   IN_PROGRESS → still processing; FINISHED →
                            STATUS_READY_TO_FINALIZE; PUBLISHED →
                            STATUS_PUBLISH_COMPLETE; ERROR / EXPIRED →
                            STATUS_FAILED (nothing was posted, so a
                            deliberate retry may create a new container).
    finalize(container)     media_publish → the Instagram media id. Called
                            only by scheduling/finalization.py, after its
                            PUBLISH_REQUESTED checkpoint.

  Credentials: every call gets a token from the user's own Instagram
  connection through credential_store.get_hosted_instagram_access_token
  (which refreshes it when due); the Instagram account id comes from the
  same stored credential, so the token and account always match. Auth
  problems surface as PublishError(REAUTHORIZATION_REQUIRED /
  CREDENTIAL_UNAVAILABLE), handled like TikTok's.

  Logging: event names, internal row ids and Meta's container/media ids
  only — never tokens or the signed video URL.

Dependencies:
  publishing.instagram.{content_publishing, credential_store, oauth},
  publishing.publisher, persistence.protocol.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from content_automation.persistence.protocol import ContentStoreProtocol
from content_automation.publishing.credential_encryption import CredentialStoreError
from content_automation.publishing.instagram import content_publishing as api
from content_automation.publishing.instagram.credential_store import (
    get_hosted_instagram_access_token,
    load_hosted_instagram_token,
)
from content_automation.publishing.instagram.oauth import InstagramAuthError
from content_automation.publishing.platforms import INSTAGRAM
from content_automation.publishing.publisher import (
    STATUS_FAILED,
    STATUS_PUBLISH_COMPLETE,
    STATUS_READY_TO_FINALIZE,
    FinalizeResult,
    PublishError,
    Publisher,
    PublishResult,
    PublishStatusResult,
)

logger = logging.getLogger(__name__)

_CONTAINER_FAILURE_CODES = {"ERROR": "INSTAGRAM_CONTAINER_ERROR", "EXPIRED": "INSTAGRAM_CONTAINER_EXPIRED"}


class InstagramPublisher(Publisher):
    reports_platform_post_id_before_media_transfer = True
    requires_finalize = True

    def __init__(self, credentials: Callable[[], tuple[str, str]]):
        """credentials() returns (access_token, instagram_account_id) for
        the connection this publisher acts for, or raises PublishError."""
        self._credentials = credentials

    def publish(self, video_path: Path, caption: str | None, on_platform_post_id: Callable[[str], None] | None = None,
                *, media_url: str | None = None) -> PublishResult:
        if not media_url:
            raise PublishError("Instagram needs a media URL to fetch the video from.", reason_code="STORAGE_UNAVAILABLE")
        access_token, account_id = self._credentials()
        _log("instagram_container_create_started")
        container_id = api.create_reel_container(account_id, access_token, media_url, caption or None)
        _log("instagram_container_created", container_id=container_id)
        if on_platform_post_id is not None:
            on_platform_post_id(container_id)
        return PublishResult(platform_post_id=container_id, status="IN_PROGRESS")

    def get_status(self, platform_post_id: str) -> PublishStatusResult:
        access_token, _ = self._credentials()
        container = api.get_container_status(platform_post_id, access_token)
        if container.status_code == "FINISHED":
            return PublishStatusResult(status=STATUS_READY_TO_FINALIZE)
        if container.status_code == "PUBLISHED":
            return PublishStatusResult(status=STATUS_PUBLISH_COMPLETE)
        if container.status_code in _CONTAINER_FAILURE_CODES:
            return PublishStatusResult(
                status=STATUS_FAILED, failure_code=_CONTAINER_FAILURE_CODES[container.status_code],
                failure_reason=container.detail or f"Instagram container {container.status_code}",
            )
        return PublishStatusResult(status=container.status_code)

    def finalize(self, platform_post_id: str) -> FinalizeResult:
        access_token, account_id = self._credentials()
        media_id = api.publish_container(account_id, access_token, platform_post_id)
        return FinalizeResult(platform_media_id=media_id)


def hosted_credentials(store: ContentStoreProtocol, user_id: int) -> Callable[[], tuple[str, str]]:
    """(access_token, instagram_account_id) for user_id's own ACTIVE
    Instagram connection, refreshed when due; PublishError otherwise."""

    def provide() -> tuple[str, str]:
        connection = store.get_platform_connection(user_id, INSTAGRAM)
        if connection is None or connection.status != "ACTIVE":
            raise PublishError("Instagram is not connected for this account. Connect Instagram again.",
                               reason_code="REAUTHORIZATION_REQUIRED")
        try:
            access_token = get_hosted_instagram_access_token(store, connection.id)
            account_id = (load_hosted_instagram_token(store, connection.id) or {}).get("user_id")
        except InstagramAuthError as exc:
            raise PublishError(str(exc), reason_code=exc.reason_code, http_status=exc.http_status) from None
        except CredentialStoreError:
            raise PublishError("The stored Instagram credential could not be used. Reconnect Instagram.",
                               reason_code="CREDENTIAL_UNAVAILABLE") from None
        if not account_id:
            raise PublishError("The stored Instagram credential has no account id. Reconnect Instagram.",
                               reason_code="CREDENTIAL_UNAVAILABLE")
        return access_token, str(account_id)

    return provide


def build_hosted_instagram_publisher(store: ContentStoreProtocol, user_id: int) -> InstagramPublisher:
    return InstagramPublisher(credentials=hosted_credentials(store, user_id))


def _log(event: str, **fields) -> None:
    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("event=%s platform=instagram %s", event, detail)
