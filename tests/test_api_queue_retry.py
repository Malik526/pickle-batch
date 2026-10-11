"""Tests for POST /api/queue/slots/{id}/retry and the can_retry /
retry_requires_confirmation fields on Queue slots (Milestone 3.13:
Reconciliation + Recovery). Same TestClient + temp SQLite fixture pattern as
test_api_queue_publish_status.py; the transition rules themselves are covered
on both backends in test_hosted_recovery.py."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from content_automation.api import app as app_module
from content_automation.api.dependencies import auth as auth_deps
from content_automation.api.dependencies import storage as storage_deps
from content_automation.persistence.content_store import ContentStore
from content_automation.storage.local import LocalStorage

NOW = datetime(2026, 9, 30, 8, 0, 0)
WINDOW = {"from": "2000-01-01T00:00:00", "to": "2200-01-01T00:00:00"}
FUTURE = "2099-01-05T09:00:00"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def users(db_path):
    with ContentStore(db_path=db_path) as store:
        user_a = store.create_user("a@example.com", "User A", "2026-01-01T00:00:00+00:00")
        user_b = store.create_user("b@example.com", "User B", "2026-01-01T00:00:00+00:00")
    return user_a, user_b


@pytest.fixture
def client(db_path, tmp_path):
    def _override_get_store():
        with ContentStore(db_path=db_path) as s:
            yield s

    app_module.app.dependency_overrides[auth_deps.get_store] = _override_get_store
    app_module.app.dependency_overrides[storage_deps.get_storage] = lambda: LocalStorage(root=tmp_path / "objects")
    yield TestClient(app_module.app)
    app_module.app.dependency_overrides.clear()


def _act_as(user):
    app_module.app.dependency_overrides[auth_deps.get_current_user] = lambda: user


def _scheduled(client, db_path, user, **post_fields):
    """An assigned slot whose PENDING post is moved to the state under test."""
    with ContentStore(db_path=db_path) as store:
        store.insert_slot_if_missing(FUTURE, None, None, NOW.isoformat(), user_id=user.id)
        slot_id = store._conn.execute(
            "SELECT id FROM content_slots WHERE user_id = ? AND scheduled_at = ?", (user.id, FUTURE)
        ).fetchone()["id"]
        video_id = store.insert_video("hash-v", "v.mp4", "/in/v.mp4", NOW.isoformat(), user_id=user.id).id
    _act_as(user)
    assert client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id}).status_code == 200
    if post_fields:
        with ContentStore(db_path=db_path) as store:
            post = store.get_platform_post(video_id, "tiktok")
            store.update_platform_post(post.id, updated_at=NOW.isoformat(), **post_fields)
    return slot_id, video_id


def _post(db_path, video_id):
    with ContentStore(db_path=db_path) as store:
        return store.get_platform_post(video_id, "tiktok")


def test_retry_failed_post_requeues_it(client, users, db_path):
    slot_id, video_id = _scheduled(client, db_path, users[0], status="FAILED", failure_code="NETWORK_ERROR",
                                   retry_count=4)
    listed = client.get("/api/queue/slots", params=WINDOW).json()["slots"][0]
    assert (listed["can_retry"], listed["retry_requires_confirmation"]) == (True, False)

    response = client.post(f"/api/queue/slots/{slot_id}/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["display_status"] == "SCHEDULED"
    assert body["can_retry"] is False
    post = _post(db_path, video_id)
    assert (post.status, post.retry_count, post.failure_code) == ("PENDING", 0, "NETWORK_ERROR")


@pytest.mark.parametrize("status,platform_post_id", [("PUBLISHED", "pub_1"), ("PUBLISHING", None),
                                                     ("PUBLISHING", "pub_1"), ("PENDING", None)])
def test_retry_rejects_published_publishing_and_pending(client, users, db_path, status, platform_post_id):
    slot_id, video_id = _scheduled(client, db_path, users[0], status=status, platform_post_id=platform_post_id)
    before = _post(db_path, video_id)

    response = client.post(f"/api/queue/slots/{slot_id}/retry", json={"confirm_not_published": True})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "NOT_RETRYABLE"
    assert _post(db_path, video_id) == before


def test_unknown_outcome_requires_confirmation(client, users, db_path):
    slot_id, video_id = _scheduled(client, db_path, users[0], status="UNKNOWN",
                                   failure_code="SUBMISSION_OUTCOME_UNKNOWN")
    listed = client.get("/api/queue/slots", params=WINDOW).json()["slots"][0]
    assert listed["display_status"] == "NEEDS_ATTENTION"
    assert listed["reason_code"] == "OUTCOME_UNKNOWN"
    assert (listed["can_retry"], listed["retry_requires_confirmation"]) == (True, True)

    refused = client.post(f"/api/queue/slots/{slot_id}/retry", json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"
    assert _post(db_path, video_id).status == "UNKNOWN"

    confirmed = client.post(f"/api/queue/slots/{slot_id}/retry", json={"confirm_not_published": True})
    assert confirmed.status_code == 200
    assert _post(db_path, video_id).status == "PENDING"


def test_unknown_after_auth_failure_suggests_reconnect_and_rechecks(client, users, db_path):
    slot_id, video_id = _scheduled(client, db_path, users[0], status="UNKNOWN", platform_post_id="pub_1",
                                   failure_code="REAUTHORIZATION_REQUIRED")
    listed = client.get("/api/queue/slots", params=WINDOW).json()["slots"][0]
    assert listed["action_hint"] == "RECONNECT_ACCOUNT"
    assert listed["retry_requires_confirmation"] is False

    response = client.post(f"/api/queue/slots/{slot_id}/retry")

    assert response.status_code == 200
    post = _post(db_path, video_id)
    assert (post.status, post.platform_post_id) == ("PUBLISHING", "pub_1")


def test_retry_is_owner_only(client, users, db_path):
    slot_id, video_id = _scheduled(client, db_path, users[0], status="FAILED")
    _act_as(users[1])

    response = client.post(f"/api/queue/slots/{slot_id}/retry")

    assert response.status_code == 404
    assert _post(db_path, video_id).status == "FAILED"


def test_retry_open_slot_is_404(client, users, db_path):
    with ContentStore(db_path=db_path) as store:
        store.insert_slot_if_missing(FUTURE, None, None, NOW.isoformat(), user_id=users[0].id)
    _act_as(users[0])
    slot_id = client.get("/api/queue/slots", params=WINDOW).json()["slots"][0]["id"]

    assert client.post(f"/api/queue/slots/{slot_id}/retry").status_code == 404


def test_retry_requires_authentication(client, users, db_path):
    slot_id, _ = _scheduled(client, db_path, users[0], status="FAILED")
    app_module.app.dependency_overrides.pop(auth_deps.get_current_user)

    assert client.post(f"/api/queue/slots/{slot_id}/retry").status_code == 401


# --- Milestone 4.2.2: Retry for Instagram slots ------------------------------------------

def _instagram_slot(client, db_path, user, **post_fields):
    """An Instagram-only slot (the Queue's "Publish to → Instagram" choice)."""
    with ContentStore(db_path=db_path) as store:
        store.get_or_create_platform_connection(user.id, "instagram", external_account_id=f"ig-{user.id}")
        store.insert_slot_if_missing(FUTURE, None, None, NOW.isoformat(), user_id=user.id)
        slot_id = store._conn.execute(
            "SELECT id FROM content_slots WHERE user_id = ? AND scheduled_at = ?", (user.id, FUTURE)
        ).fetchone()["id"]
        video_id = store.insert_video("hash-ig", "reel.mov", "/in/reel.mov", NOW.isoformat(), user_id=user.id).id
    _act_as(user)
    response = client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id, "platforms": ["instagram"]})
    assert response.status_code == 200
    with ContentStore(db_path=db_path) as store:
        post = store.get_platform_post(video_id, "instagram")
        store.update_platform_post(post.id, updated_at=NOW.isoformat(), **post_fields)
    return slot_id, video_id


def test_retry_with_no_platform_requeues_an_instagram_only_slot(client, users, db_path):
    # The production bug: the body names no platform (as the Queue UI sends it), and the
    # endpoint used to look for a TikTok post → 404, leaving the Instagram row FAILED.
    slot_id, video_id = _instagram_slot(client, db_path, users[0], status="FAILED", failure_code="INSTAGRAM_MEDIA_RESOLUTION")

    response = client.post(f"/api/queue/slots/{slot_id}/retry", json={"confirm_not_published": False})

    assert response.status_code == 200
    body = response.json()
    assert body["display_status"] == "SCHEDULED" and body["message"] is None  # old failure no longer shown
    with ContentStore(db_path=db_path) as store:
        assert store.get_platform_post(video_id, "instagram").status == "PENDING"


def test_retry_of_an_instagram_duration_failure_is_refused_with_a_clear_reason(client, users, db_path):
    slot_id, video_id = _instagram_slot(client, db_path, users[0], status="FAILED", failure_code="INSTAGRAM_MEDIA_DURATION")
    listed = client.get("/api/queue/slots", params=WINDOW).json()["slots"][0]
    assert listed["can_retry"] is False

    response = client.post(f"/api/queue/slots/{slot_id}/retry")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "NOT_RETRYABLE"
    with ContentStore(db_path=db_path) as store:
        assert store.get_platform_post(video_id, "instagram").status == "FAILED"


def test_naming_a_platform_with_no_post_is_still_404(client, users, db_path):
    slot_id, _ = _instagram_slot(client, db_path, users[0], status="FAILED", failure_code="NETWORK_ERROR")
    assert client.post(f"/api/queue/slots/{slot_id}/retry", json={"platform": "tiktok"}).status_code == 404
