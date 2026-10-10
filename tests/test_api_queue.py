"""Tests for /api/queue (Milestone 3.9: Queue + Calendar Functionality):
listing slots with their occupying video/publish state, manual and
automatic (FIFO) video-to-slot assignment, and "Remove from schedule"
(unassignment). Real temp-file SQLite ContentStore via TestClient, matching
tests/test_api_cadence.py's/test_api_videos.py's own established pattern —
slots are created directly against the store (not through the cadence PUT
endpoint) so their scheduled_at values are deterministic regardless of
"today"."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from content_automation.api import app as app_module
from content_automation.api.dependencies import auth as auth_deps
from content_automation.api.dependencies import storage as storage_deps
from content_automation.persistence.content_store import ContentStore
from content_automation.storage.local import LocalStorage

NOW = datetime(2026, 9, 14, 8, 0, 0)


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

    def _override_get_storage():
        return LocalStorage(root=tmp_path / "objects")

    app_module.app.dependency_overrides[auth_deps.get_store] = _override_get_store
    app_module.app.dependency_overrides[storage_deps.get_storage] = _override_get_storage
    yield TestClient(app_module.app)
    app_module.app.dependency_overrides.clear()


def _act_as(user):
    app_module.app.dependency_overrides[auth_deps.get_current_user] = lambda: user


def _open_slot(db_path, user, scheduled_at):
    with ContentStore(db_path=db_path) as store:
        store.insert_slot_if_missing(scheduled_at, None, None, NOW.isoformat(), user_id=user.id)
        row = store._conn.execute(
            "SELECT id FROM content_slots WHERE user_id = ? AND scheduled_at = ?", (user.id, scheduled_at)
        ).fetchone()
        return row["id"]


def _video(db_path, user, name="v"):
    with ContentStore(db_path=db_path) as store:
        return store.insert_video(f"hash-{name}", f"{name}.mp4", f"/incoming/{name}.mp4", NOW.isoformat(), user_id=user.id).id


WIDE_WINDOW = {"from": "2000-01-01T00:00:00", "to": "2200-01-01T00:00:00"}


# ---------------------------------------------------------------------------
# GET /api/queue/slots
# ---------------------------------------------------------------------------

def test_list_queue_slots_requires_authentication(client, users):
    response = client.get("/api/queue/slots", params=WIDE_WINDOW)
    assert response.status_code == 401


def test_list_queue_slots_shows_open_slots(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    _open_slot(db_path, user_a, "2026-09-20T09:00:00")

    response = client.get("/api/queue/slots", params=WIDE_WINDOW)
    assert response.status_code == 200
    slots = response.json()["slots"]
    assert len(slots) == 1
    assert slots[0]["status"] == "OPEN"
    assert slots[0]["display_status"] == "OPEN"
    assert slots[0]["assigned_video"] is None


def test_list_queue_slots_only_shows_the_caller_own_slots(client, users, db_path):
    user_a, user_b = users
    _open_slot(db_path, user_a, "2026-09-20T09:00:00")
    _open_slot(db_path, user_b, "2026-09-21T09:00:00")

    _act_as(user_a)
    response = client.get("/api/queue/slots", params=WIDE_WINDOW)
    slots = response.json()["slots"]

    assert len(slots) == 1
    assert slots[0]["scheduled_at"] == "2026-09-20T09:00:00"


def test_list_queue_slots_shows_the_assigned_video(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    # Future slot: since Milestone 3.11 a PENDING post whose time has
    # already passed is NEEDS_ATTENTION (SCHEDULE_MISSED), not Scheduled —
    # covered separately in tests/test_api_queue_publish_status.py.
    slot_id = _open_slot(db_path, user_a, "2099-09-20T09:00:00")
    video_id = _video(db_path, user_a)
    client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id})

    response = client.get("/api/queue/slots", params=WIDE_WINDOW)
    slots = response.json()["slots"]

    assert len(slots) == 1
    assert slots[0]["status"] == "ASSIGNED"
    assert slots[0]["display_status"] == "SCHEDULED"  # 3.9's "ASSIGNED" display value, renamed in 3.11
    assert slots[0]["assigned_video"]["id"] == video_id
    assert slots[0]["assigned_video"]["original_filename"] == "v.mp4"
    assert slots[0]["platform_post_status"] == "PENDING"


def test_list_queue_slots_reflects_a_published_platform_post(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    slot_id = _open_slot(db_path, user_a, "2026-09-20T09:00:00")
    video_id = _video(db_path, user_a)
    client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id})

    with ContentStore(db_path=db_path) as store:
        post = store.get_platform_post(video_id, "tiktok")
        # A real PUBLISHED row always carries its platform post id (persisted
        # before polling — publish_tiktok.py); 3.11 shows PUBLISHED without
        # one as NEEDS_ATTENTION, tested in test_api_queue_publish_status.py.
        store.update_platform_post(
            post.id, updated_at="2026-09-15T00:00:00", status="PUBLISHED",
            platform_post_id="pub_1", published_at="2026-09-20T13:00:05+00:00",
        )

    response = client.get("/api/queue/slots", params=WIDE_WINDOW)
    slot = response.json()["slots"][0]
    assert slot["status"] == "ASSIGNED"  # content_slots.status itself never becomes "PUBLISHED"
    assert slot["display_status"] == "PUBLISHED"
    assert slot["platform_post_status"] == "PUBLISHED"


# ---------------------------------------------------------------------------
# POST /api/queue/slots/{slot_id}/assign (manual)
# ---------------------------------------------------------------------------

def test_manual_assign_requires_authentication(client, users, db_path):
    user_a, _ = users
    slot_id = _open_slot(db_path, user_a, "2026-09-20T09:00:00")
    response = client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": 1})
    assert response.status_code == 401


def test_manual_assign_rejects_another_users_slot(client, users, db_path):
    user_a, user_b = users
    slot_id = _open_slot(db_path, user_b, "2026-09-20T09:00:00")
    video_id = _video(db_path, user_a)

    _act_as(user_a)
    response = client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id})

    assert response.status_code == 404


def test_manual_assign_rejects_another_users_video(client, users, db_path):
    user_a, user_b = users
    slot_id = _open_slot(db_path, user_a, "2026-09-20T09:00:00")
    video_id = _video(db_path, user_b)

    _act_as(user_a)
    response = client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id})

    assert response.status_code == 404


def test_manual_assign_the_same_slot_twice_is_refused(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    slot_id = _open_slot(db_path, user_a, "2026-09-20T09:00:00")
    video_1 = _video(db_path, user_a, "one")
    video_2 = _video(db_path, user_a, "two")
    first = client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_1})
    assert first.status_code == 200

    second = client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_2})

    assert second.status_code == 409
    assert client.get("/api/queue/slots", params=WIDE_WINDOW).json()["slots"][0]["assigned_video"]["id"] == video_1


def test_manual_assign_a_video_already_scheduled_is_refused(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    slot_1 = _open_slot(db_path, user_a, "2026-09-20T09:00:00")
    slot_2 = _open_slot(db_path, user_a, "2026-09-21T09:00:00")
    video_id = _video(db_path, user_a)
    client.post(f"/api/queue/slots/{slot_1}/assign", json={"video_id": video_id})

    response = client.post(f"/api/queue/slots/{slot_2}/assign", json={"video_id": video_id})

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# POST /api/queue/assign-next (automatic/FIFO)
# ---------------------------------------------------------------------------

def test_assign_next_claims_the_earliest_open_slot(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    # Far-future dates so this test never depends on the real "today" —
    # assign-next uses the real wall clock (no `now` override, unlike
    # tests/test_queue_assignment.py's unit tests), so a slot dated in the
    # past relative to whenever this test actually runs must never be
    # picked, which a 2026-09-01 distractor slot would also prove, but
    # these two already do without needing one.
    later = _open_slot(db_path, user_a, "2100-01-02T09:00:00")
    earliest_future = _open_slot(db_path, user_a, "2100-01-01T09:00:00")
    video_id = _video(db_path, user_a)

    response = client.post("/api/queue/assign-next", json={"video_id": video_id})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == earliest_future
    assert body["assigned_video"]["id"] == video_id
    assert client.get("/api/queue/slots", params=WIDE_WINDOW).json()["slots"]
    later_slot = next(s for s in client.get("/api/queue/slots", params=WIDE_WINDOW).json()["slots"] if s["id"] == later)
    assert later_slot["status"] == "OPEN"


def test_assign_next_returns_409_when_no_open_slot_exists(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    video_id = _video(db_path, user_a)

    response = client.post("/api/queue/assign-next", json={"video_id": video_id})

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# POST /api/queue/slots/{slot_id}/unassign ("Remove from schedule")
# ---------------------------------------------------------------------------

def test_unassign_keeps_the_video_but_reopens_the_slot(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    slot_id = _open_slot(db_path, user_a, "2026-09-20T09:00:00")
    video_id = _video(db_path, user_a)
    client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id})

    response = client.post(f"/api/queue/slots/{slot_id}/unassign")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OPEN"
    assert body["assigned_video"] is None

    # the video itself is untouched — still exists, still listable
    library = client.get("/api/videos")
    assert library.status_code == 200
    assert any(v["id"] == video_id for v in library.json()["videos"])


def test_unassign_refuses_to_destroy_a_published_platform_post(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    slot_id = _open_slot(db_path, user_a, "2026-09-20T09:00:00")
    video_id = _video(db_path, user_a)
    client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id})
    with ContentStore(db_path=db_path) as store:
        post = store.get_platform_post(video_id, "tiktok")
        # A real PUBLISHED row always carries its platform post id (persisted
        # before polling — publish_tiktok.py); 3.11 shows PUBLISHED without
        # one as NEEDS_ATTENTION, tested in test_api_queue_publish_status.py.
        store.update_platform_post(
            post.id, updated_at="2026-09-15T00:00:00", status="PUBLISHED",
            platform_post_id="pub_1", published_at="2026-09-20T13:00:05+00:00",
        )

    response = client.post(f"/api/queue/slots/{slot_id}/unassign")

    assert response.status_code == 409
    unchanged = client.get("/api/queue/slots", params=WIDE_WINDOW).json()["slots"][0]
    assert unchanged["status"] == "ASSIGNED"
    assert unchanged["display_status"] == "PUBLISHED"


def test_unassign_an_already_open_slot_is_refused(client, users, db_path):
    user_a, _ = users
    _act_as(user_a)
    slot_id = _open_slot(db_path, user_a, "2026-09-20T09:00:00")

    response = client.post(f"/api/queue/slots/{slot_id}/unassign")

    assert response.status_code == 409


def test_unassign_rejects_another_users_slot(client, users, db_path):
    user_a, user_b = users
    slot_id = _open_slot(db_path, user_b, "2026-09-20T09:00:00")

    _act_as(user_a)
    response = client.post(f"/api/queue/slots/{slot_id}/unassign")

    assert response.status_code == 404


def test_removing_from_schedule_then_deleting_the_video_succeeds(client, users, db_path):
    """Regression proof for the Delete Video 409 guard (Milestone 3.7
    follow-up): once a video is cleanly removed from its schedule, the
    guard's two conditions (assigned_slot_id, any platform_posts row) are
    both actually cleared, so a delete that was previously blocked now
    succeeds."""
    user_a, _ = users
    _act_as(user_a)
    slot_id = _open_slot(db_path, user_a, "2026-09-20T09:00:00")
    video_id = _video(db_path, user_a)
    client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id})
    blocked = client.delete(f"/api/videos/{video_id}")
    assert blocked.status_code == 409

    client.post(f"/api/queue/slots/{slot_id}/unassign")
    allowed = client.delete(f"/api/videos/{video_id}")

    assert allowed.status_code == 204


# ---------------------------------------------------------------------------
# Milestone 4.2: choosing platforms when assigning
# ---------------------------------------------------------------------------

def _connect(db_path, user, platform):
    with ContentStore(db_path=db_path) as store:
        store.get_or_create_platform_connection(user.id, platform, external_account_id=f"{platform}-{user.id}")


def _platforms_for(db_path, video_id):
    with ContentStore(db_path=db_path) as store:
        return sorted(p for p in ("tiktok", "instagram") if store.get_platform_post(video_id, p) is not None)


def test_assigning_to_instagram_only_creates_just_an_instagram_post(client, users, db_path):
    user_a, _ = users
    _connect(db_path, user_a, "instagram")
    slot_id, video_id = _open_slot(db_path, user_a, "2099-01-05T09:00:00"), _video(db_path, user_a)
    _act_as(user_a)
    response = client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id, "platforms": ["instagram"]})
    assert response.status_code == 200
    assert _platforms_for(db_path, video_id) == ["instagram"]
    assert [p["platform"] for p in response.json()["publications"]] == ["instagram"]


def test_omitting_platforms_keeps_the_default(client, users, db_path):
    user_a, _ = users
    slot_id, video_id = _open_slot(db_path, user_a, "2099-01-05T09:00:00"), _video(db_path, user_a)
    _act_as(user_a)
    assert client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id}).status_code == 200
    assert _platforms_for(db_path, video_id) == ["tiktok"]


def test_assigning_to_an_unconnected_platform_is_refused_and_changes_nothing(client, users, db_path):
    user_a, user_b = users
    _connect(db_path, user_b, "instagram")  # someone else's connection doesn't count
    slot_id, video_id = _open_slot(db_path, user_a, "2099-01-05T09:00:00"), _video(db_path, user_a)
    _act_as(user_a)
    response = client.post(f"/api/queue/assign-next", json={"video_id": video_id, "platforms": ["instagram"]})
    assert response.status_code == 409 and "Connect Instagram" in response.json()["detail"]
    response = client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id, "platforms": ["instagram"]})
    assert response.status_code == 409
    assert _platforms_for(db_path, video_id) == []


def test_unknown_or_empty_platform_choices_are_bad_requests(client, users, db_path):
    user_a, _ = users
    slot_id, video_id = _open_slot(db_path, user_a, "2099-01-05T09:00:00"), _video(db_path, user_a)
    _act_as(user_a)
    for platforms in (["myspace"], []):
        response = client.post(f"/api/queue/slots/{slot_id}/assign", json={"video_id": video_id, "platforms": platforms})
        assert response.status_code == 400
