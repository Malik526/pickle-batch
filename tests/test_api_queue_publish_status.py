"""Tests for the publish-state fields on GET /api/queue/slots (Milestone
3.11: User-Facing Publish States + Errors) — sanitized failure explanations,
NEEDS_ATTENTION for uncertain rows, persistence across reloads, and tenant
isolation. Same TestClient + temp SQLite fixture pattern as test_api_queue.py."""

import json
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

RAW_FAILURE_REASON = (
    "TikTok HTTP 401: {'error': {'code': 'access_token_invalid', 'log_id': '20260930ABCDEF'}, "
    "'access_token': 'act.SECRET_TOKEN_VALUE'} Traceback (most recent call last)"
)


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


def _scheduled(client, db_path, user, scheduled_at=FUTURE, name="v", **post_fields):
    """An assigned slot (via the real assign endpoint) whose PENDING post is
    then moved to whatever state the test needs. Returns (slot_id, video_id)."""
    with ContentStore(db_path=db_path) as store:
        store.insert_slot_if_missing(scheduled_at, None, None, NOW.isoformat(), user_id=user.id)
        slot_id = store._conn.execute(
            "SELECT id FROM content_slots WHERE user_id = ? AND scheduled_at = ?", (user.id, scheduled_at)
        ).fetchone()["id"]
        video_id = store.insert_video(f"hash-{name}", f"{name}.mp4", f"/in/{name}.mp4", NOW.isoformat(), user_id=user.id).id
    _act_as(user)
    assert client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id}).status_code == 200
    if post_fields:
        with ContentStore(db_path=db_path) as store:
            post = store.get_platform_post(video_id, "tiktok")
            store.update_platform_post(post.id, updated_at=post_fields.pop("updated_at", NOW.isoformat()), **post_fields)
    return slot_id, video_id


def _slots(client):
    return client.get("/api/queue/slots", params=WINDOW).json()["slots"]


def test_scheduled_slot_shape(client, users, db_path):
    _scheduled(client, db_path, users[0])
    slot = _slots(client)[0]
    assert slot["display_status"] == "SCHEDULED"
    assert slot["reason_code"] is None and slot["message"] is None and slot["action_hint"] is None
    assert slot["published_at"] is None
    assert slot["can_unassign"] is True
    assert slot["publications"] == [{
        "platform": "tiktok", "display_status": "SCHEDULED", "platform_post_status": "PENDING",
        "published_at": None, "reason_code": None, "message": None, "action_hint": None,
        "stage": None,  # Milestone 4.2: only set for Instagram while PUBLISHING
    }]


def test_failed_slot_shows_only_sanitized_explanation(client, users, db_path):
    _scheduled(client, db_path, users[0], status="FAILED", failure_reason=RAW_FAILURE_REASON,
               failure_code="access_token_invalid")
    response = client.get("/api/queue/slots", params=WINDOW)
    slot = response.json()["slots"][0]

    assert slot["display_status"] == "FAILED"
    assert slot["reason_code"] == "AUTH_REQUIRED"
    assert slot["message"] == "TikTok connection needs to be renewed. Reconnect your account in Settings."
    assert slot["action_hint"] == "RECONNECT_ACCOUNT"
    assert slot["can_unassign"] is False

    body = response.text
    for leak in ("SECRET_TOKEN_VALUE", "log_id", "20260930ABCDEF", "Traceback", "HTTP 401", "access_token_invalid"):
        assert leak not in body
    assert "failure_reason" not in json.dumps(response.json())


def test_failed_row_from_before_3_11_has_generic_message(client, users, db_path):
    _scheduled(client, db_path, users[0], status="FAILED", failure_reason=RAW_FAILURE_REASON)  # no failure_code
    slot = _slots(client)[0]
    assert slot["reason_code"] == "UNKNOWN_ERROR"
    assert slot["message"] == "Publishing to TikTok failed for an unexpected reason."


def test_overdue_pending_slot_needs_attention_and_can_still_be_removed(client, users, db_path):
    slot_id, _ = _scheduled(client, db_path, users[0], scheduled_at="2026-01-05T09:00:00")
    slot = _slots(client)[0]
    assert slot["display_status"] == "NEEDS_ATTENTION"
    assert slot["reason_code"] == "SCHEDULE_MISSED"
    assert slot["can_unassign"] is True
    assert client.post(f"/api/queue/slots/{slot_id}/unassign").status_code == 200


def test_published_state_survives_reload(client, users, db_path):
    slot_id, _ = _scheduled(client, db_path, users[0], status="PUBLISHED", platform_post_id="pub_1",
                            published_at="2026-09-29T13:00:05+00:00")
    first = _slots(client)[0]
    second = _slots(client)[0]
    assert first == second
    assert first["display_status"] == "PUBLISHED"
    assert first["published_at"] == "2026-09-29T13:00:05+00:00"
    assert first["can_unassign"] is False
    # The existing unassign guard is unchanged.
    assert client.post(f"/api/queue/slots/{slot_id}/unassign").status_code == 409
    assert _slots(client)[0]["display_status"] == "PUBLISHED"


def test_published_without_platform_id_is_not_claimed_as_published(client, users, db_path):
    _scheduled(client, db_path, users[0], status="PUBLISHED", published_at="2026-09-29T13:00:05+00:00")
    slot = _slots(client)[0]
    assert slot["display_status"] == "NEEDS_ATTENTION"
    assert slot["reason_code"] == "STATE_INCONSISTENT"


def test_publish_state_is_isolated_per_user(client, users, db_path):
    user_a, user_b = users
    slot_id, _ = _scheduled(client, db_path, user_a, status="FAILED", failure_code="CAPTION_TOO_LONG")

    _act_as(user_b)
    assert _slots(client) == []
    assert client.post(f"/api/queue/slots/{slot_id}/unassign").status_code == 404

    _act_as(user_a)
    assert _slots(client)[0]["message"] == "Caption is too long for TikTok."
