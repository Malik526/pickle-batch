"""Unit tests for Instagram Reels publishing (Milestone 4.2) below the
scheduling layer: the Meta HTTP client (requests mocked — no network), the
InstagramPublisher status mapping, hosted credential resolution, and the
Reels media/caption requirements."""

from pathlib import Path

import pytest
import requests

from content_automation import config
from content_automation.media.inspection import MediaInfo
from content_automation.publishing.instagram import content_publishing as api
from content_automation.publishing.instagram import media_requirements as reqs
from content_automation.publishing.instagram import publisher as ig_publisher
from content_automation.publishing.instagram.oauth import InstagramReauthorizationRequiredError
from content_automation.publishing.instagram.publisher import InstagramPublisher, hosted_credentials
from content_automation.publishing.publisher import (
    STATUS_FAILED,
    STATUS_PUBLISH_COMPLETE,
    STATUS_READY_TO_FINALIZE,
    PublishError,
)
from content_automation.persistence.content_store import ContentStore
from content_automation.scheduling import retry_classification

TOKEN = "IGAA-secret-access-token"
SIGNED = "https://proj.supabase.co/storage/v1/object/sign/media/u/2/v/9/source.mp4?token=eyJsigned.secret"


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Calls(list):
    """Recorded requests; `queue` holds the scripted responses/exceptions."""

    def __init__(self):
        super().__init__()
        self.queue = []


@pytest.fixture
def calls(monkeypatch):
    recorded = _Calls()

    def fake_request(method, url, data=None, params=None, timeout=None):
        recorded.append({"method": method, "url": url, "data": data, "params": params})
        item = recorded.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(api.requests, "request", fake_request)
    monkeypatch.setattr(config, "INSTAGRAM_GRAPH_BASE", "https://graph.instagram.com")
    monkeypatch.setattr(config, "INSTAGRAM_GRAPH_API_VERSION", "v25.0")
    return recorded


# --- HTTP client -------------------------------------------------------------------

def test_container_creation_posts_reels_with_video_url_and_caption(calls):
    calls.queue.append(_Response(200, {"id": "17890000000000001"}))
    assert api.create_reel_container("1784140000", TOKEN, SIGNED, "Hello #reels") == "17890000000000001"
    [call] = calls
    assert (call["method"], call["url"]) == ("POST", "https://graph.instagram.com/v25.0/1784140000/media")
    assert call["data"] == {"media_type": "REELS", "video_url": SIGNED, "caption": "Hello #reels", "access_token": TOKEN}
    assert TOKEN not in call["url"] and SIGNED not in call["url"]  # secrets travel in the body, not the URL


@pytest.mark.parametrize("caption", [None, ""])
def test_captionless_container_sends_no_caption_field(calls, caption):
    calls.queue.append(_Response(200, {"id": "1"}))
    api.create_reel_container("1784140000", TOKEN, SIGNED, caption)
    assert "caption" not in calls[0]["data"]


def test_status_and_publish_calls(calls):
    calls.queue += [_Response(200, {"status_code": "FINISHED", "status": "Finished: Media has been uploaded"}),
                    _Response(200, {"id": "18000000000000009"})]
    status = api.get_container_status("17890000000000001", TOKEN)
    assert (status.status_code, status.detail) == ("FINISHED", "Finished: Media has been uploaded")
    assert calls[0]["params"] == {"fields": "status_code,status", "access_token": TOKEN}
    assert api.publish_container("1784140000", TOKEN, "17890000000000001") == "18000000000000009"
    assert calls[1]["url"].endswith("/1784140000/media_publish")
    assert calls[1]["data"] == {"creation_id": "17890000000000001", "access_token": TOKEN}


@pytest.mark.parametrize(
    ("response", "reason", "retryable"),
    [
        (_Response(400, {"error": {"code": 190, "message": "Invalid OAuth access token"}}), "REAUTHORIZATION_REQUIRED", False),
        (_Response(400, {"error": {"code": 9007, "error_subcode": 2207027}}), "INSTAGRAM_MEDIA_NOT_READY", True),
        (_Response(400, {"error": {"code": 4, "error_subcode": 2207042}}), "INSTAGRAM_RATE_LIMITED", True),
        (_Response(400, {"error": {"code": 9004, "error_subcode": 2207052}}), "INSTAGRAM_MEDIA_FETCH_FAILED", True),
        (_Response(400, {"error": {"code": 100, "error_subcode": 2207026}}), "INSTAGRAM_REQUEST_REJECTED", False),
        (_Response(500, {"error": {"code": 2, "is_transient": True}}), "INSTAGRAM_TRANSIENT_ERROR", True),
        (_Response(502, None), "INSTAGRAM_TRANSIENT_ERROR", True),
        (_Response(200, ["not", "an", "object"]), "MALFORMED_RESPONSE", False),
    ],
)
def test_meta_errors_map_to_classified_reason_codes(calls, response, reason, retryable):
    calls.queue.append(response)
    with pytest.raises(api.InstagramPublishError) as raised:
        api.create_reel_container("1784140000", TOKEN, SIGNED, None)
    assert raised.value.reason_code == reason
    assert retry_classification.is_retryable(reason, raised.value.http_status) is retryable


@pytest.mark.parametrize("exc", [requests.Timeout("timed out " + SIGNED), requests.ConnectionError("boom " + TOKEN)])
def test_network_failures_are_retryable_and_never_leak_secrets(calls, exc):
    calls.queue.append(exc)
    with pytest.raises(api.InstagramPublishError) as raised:
        api.create_reel_container("1784140000", TOKEN, SIGNED, None)
    assert raised.value.reason_code == "NETWORK_ERROR"
    message = str(raised.value)
    assert TOKEN not in message and "eyJsigned" not in message and "supabase" not in message


def test_error_messages_carry_codes_not_meta_text(calls):
    calls.queue.append(_Response(400, {"error": {"code": 100, "message": "secret-ish body " + TOKEN}}))
    with pytest.raises(api.InstagramPublishError) as raised:
        api.publish_container("1784140000", TOKEN, "c1")
    assert "code=100" in str(raised.value) and TOKEN not in str(raised.value) and "body" not in str(raised.value)


def test_missing_ids_are_malformed(calls):
    calls.queue += [_Response(200, {}), _Response(200, {"status": "no code"})]
    with pytest.raises(api.InstagramPublishError, match="missing an id"):
        api.create_reel_container("1", TOKEN, SIGNED, None)
    with pytest.raises(api.InstagramPublishError, match="status_code"):
        api.get_container_status("c1", TOKEN)


# --- InstagramPublisher mapping ------------------------------------------------------------

class _FakeApi:
    def __init__(self, status="IN_PROGRESS", detail=None):
        self.status, self.detail, self.events = status, detail, []

    def create_reel_container(self, account_id, token, url, caption):
        self.events.append(("create", account_id, url, caption))
        return "c-1"

    def get_container_status(self, container_id, token):
        self.events.append(("status", container_id))
        return api.ContainerStatus(self.status, self.detail)

    def publish_container(self, account_id, token, container_id):
        self.events.append(("publish", container_id))
        return "m-1"


@pytest.fixture
def fake_api(monkeypatch):
    fake = _FakeApi()
    monkeypatch.setattr(ig_publisher, "api", type("A", (), {
        "create_reel_container": staticmethod(fake.create_reel_container),
        "get_container_status": staticmethod(fake.get_container_status),
        "publish_container": staticmethod(fake.publish_container),
        "ContainerStatus": api.ContainerStatus,
    }))
    return fake


def test_publish_creates_container_and_reports_id_before_returning(fake_api):
    seen = []
    publisher = InstagramPublisher(credentials=lambda: (TOKEN, "1784140000"))
    result = publisher.publish(Path("unused"), None, on_platform_post_id=lambda cid: seen.append((cid, list(fake_api.events))), media_url=SIGNED)
    assert result.platform_post_id == "c-1"
    assert seen == [("c-1", [("create", "1784140000", SIGNED, None)])]  # id reported right after creation
    assert publisher.requires_finalize and publisher.reports_platform_post_id_before_media_transfer


def test_publish_without_a_media_url_fails_before_any_request(fake_api):
    with pytest.raises(PublishError) as raised:
        InstagramPublisher(credentials=lambda: (TOKEN, "1")).publish(Path("x"), None)
    assert raised.value.reason_code == "STORAGE_UNAVAILABLE" and fake_api.events == []


@pytest.mark.parametrize(
    ("container", "status", "failure_code"),
    [
        ("IN_PROGRESS", "IN_PROGRESS", None),
        ("FINISHED", STATUS_READY_TO_FINALIZE, None),
        ("PUBLISHED", STATUS_PUBLISH_COMPLETE, None),
        ("ERROR", STATUS_FAILED, "INSTAGRAM_CONTAINER_ERROR"),
        ("EXPIRED", STATUS_FAILED, "INSTAGRAM_CONTAINER_EXPIRED"),
    ],
)
def test_container_status_maps_to_the_shared_vocabulary(fake_api, container, status, failure_code):
    fake_api.status = container
    result = InstagramPublisher(credentials=lambda: (TOKEN, "1")).get_status("c-1")
    assert (result.status, result.failure_code) == (status, failure_code)


def test_finalize_publishes_the_given_container(fake_api):
    assert InstagramPublisher(credentials=lambda: (TOKEN, "1")).finalize("c-9").platform_media_id == "m-1"
    assert fake_api.events == [("publish", "c-9")]


# --- hosted credentials ------------------------------------------------------------------

def test_hosted_credentials_use_only_the_users_own_active_connection(tmp_path, monkeypatch):
    with ContentStore(db_path=tmp_path / "t.db") as store:
        alice = store.create_user("a@example.com", None, "2026-10-10T00:00:00+00:00")
        bob = store.create_user("b@example.com", None, "2026-10-10T00:00:00+00:00")
        connection = store.get_or_create_platform_connection(alice.id, "instagram", external_account_id="111")
        tokens = {connection.id: ("alice-token", {"user_id": "111"})}
        monkeypatch.setattr(ig_publisher, "get_hosted_instagram_access_token", lambda s, cid: tokens[cid][0])
        monkeypatch.setattr(ig_publisher, "load_hosted_instagram_token", lambda s, cid: tokens[cid][1])

        assert hosted_credentials(store, alice.id)() == ("alice-token", "111")
        with pytest.raises(PublishError) as raised:
            hosted_credentials(store, bob.id)()
        assert raised.value.reason_code == "REAUTHORIZATION_REQUIRED"

        store.update_platform_connection_status(connection.id, "DISCONNECTED", "2026-10-10T01:00:00+00:00")
        with pytest.raises(PublishError):
            hosted_credentials(store, alice.id)()


def test_hosted_credentials_translate_auth_and_credential_failures(tmp_path, monkeypatch):
    from content_automation.publishing.credential_encryption import CredentialStoreError

    with ContentStore(db_path=tmp_path / "t.db") as store:
        user = store.create_user("a@example.com", None, "2026-10-10T00:00:00+00:00")
        store.get_or_create_platform_connection(user.id, "instagram", external_account_id="111")

        def expired(_s, _cid):
            raise InstagramReauthorizationRequiredError("expired")
        monkeypatch.setattr(ig_publisher, "get_hosted_instagram_access_token", expired)
        with pytest.raises(PublishError) as raised:
            hosted_credentials(store, user.id)()
        assert raised.value.reason_code == "REAUTHORIZATION_REQUIRED"

        def undecryptable(_s, _cid):
            raise CredentialStoreError("bad key")
        monkeypatch.setattr(ig_publisher, "get_hosted_instagram_access_token", undecryptable)
        with pytest.raises(PublishError) as raised:
            hosted_credentials(store, user.id)()
        assert raised.value.reason_code == "CREDENTIAL_UNAVAILABLE"


# --- Reels requirements ----------------------------------------------------------------------

def _info(**overrides):
    base = dict(path=Path("clip.mp4"), container="mov,mp4,m4a,3gp,3g2,mj2", video_codec="h264", audio_codec="aac",
                width=1080, height=1920, fps=30.0, duration_seconds=12.0, file_size_bytes=20 * 1024 * 1024)
    return MediaInfo(**{**base, **overrides})


def test_a_standard_1080p_reel_passes():
    assert reqs.check_reel_media(_info()) is None
    assert reqs.check_reel_media(_info(audio_codec=None, video_codec="hevc", path=Path("c.mov"), container="mov")) is None


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"container": "matroska,webm", "path": Path("c.mkv")}, "INSTAGRAM_MEDIA_UNSUPPORTED_FORMAT"),
        ({"video_codec": "vp9"}, "INSTAGRAM_MEDIA_UNSUPPORTED_CODEC"),
        ({"audio_codec": "opus"}, "INSTAGRAM_MEDIA_UNSUPPORTED_CODEC"),
        ({"duration_seconds": 2.0}, "INSTAGRAM_MEDIA_DURATION"),
        ({"duration_seconds": 16 * 60.0}, "INSTAGRAM_MEDIA_DURATION"),
        ({"fps": 15.0}, "INSTAGRAM_MEDIA_FRAME_RATE"),
        ({"fps": 120.0}, "INSTAGRAM_MEDIA_FRAME_RATE"),
        ({"file_size_bytes": 301 * 1024 * 1024}, "INSTAGRAM_MEDIA_TOO_LARGE"),
        ({"width": 3840, "height": 2160}, "INSTAGRAM_MEDIA_RESOLUTION"),  # the real 4K iPhone uploads
    ],
)
def test_unsupported_media_is_rejected_with_an_actionable_reason(overrides, code):
    problem = reqs.check_reel_media(_info(**overrides))
    assert problem is not None and problem.reason_code == code and problem.message


def test_caption_limits_are_instagram_specific():
    assert reqs.check_caption(None) is None and reqs.check_caption("") is None
    assert reqs.check_caption("x" * 2200) is None
    assert reqs.check_caption("x" * 2201).reason_code == "CAPTION_TOO_LONG"
    assert reqs.check_caption(" ".join(f"#t{i}" for i in range(30))) is None
    assert reqs.check_caption(" ".join(f"#t{i}" for i in range(31))).reason_code == "INSTAGRAM_CAPTION_TOO_MANY_HASHTAGS"
    assert reqs.check_caption(" ".join(f"@u{i}" for i in range(21))).reason_code == "INSTAGRAM_CAPTION_TOO_MANY_MENTIONS"
    assert reqs.check_caption("email me@example.com about #1") is None  # an email address isn't a mention
