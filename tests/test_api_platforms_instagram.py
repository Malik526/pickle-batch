"""Tests for GET /api/platforms/instagram/status (Milestone 4.0): the real
connection state from the shared platform tables, owner isolation, no
credential leakage, and connect_available (offered only once configured,
since Milestone 4.1 — the connect/callback flow itself is covered by
test_api_platforms_instagram_oauth.py). Same TestClient + temp SQLite
pattern as test_api_platforms_tiktok.py."""

import pytest
from fastapi.testclient import TestClient

from content_automation import config
from content_automation.api import app as app_module
from content_automation.api.dependencies import auth as auth_deps
from content_automation.persistence.content_store import ContentStore

NOW = "2026-10-04T00:00:00+00:00"


@pytest.fixture(autouse=True)
def instagram_unconfigured(monkeypatch):
    """Tests decide whether Instagram is configured; the developer's .env
    must not (it made connect_available depend on the machine)."""
    for name in ("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET", "INSTAGRAM_REDIRECT_URI"):
        monkeypatch.setattr(config, name, "")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def users(db_path):
    with ContentStore(db_path=db_path) as store:
        return (
            store.create_user("a@example.com", "User A", NOW),
            store.create_user("b@example.com", "User B", NOW),
        )


@pytest.fixture
def client(db_path):
    def _override_get_store():
        with ContentStore(db_path=db_path) as s:
            yield s

    app_module.app.dependency_overrides[auth_deps.get_store] = _override_get_store
    yield TestClient(app_module.app)
    app_module.app.dependency_overrides.clear()


def _act_as(user):
    app_module.app.dependency_overrides[auth_deps.get_current_user] = lambda: user


def _connect(db_path, user, *, with_credential=True, status="ACTIVE"):
    with ContentStore(db_path=db_path) as store:
        connection = store.get_or_create_platform_connection(user.id, "instagram", external_account_id="17841400000000000")
        if status != "ACTIVE":
            store.update_platform_connection_status(connection.id, status, NOW)
        if with_credential:
            store.upsert_platform_credential(connection.id, "encrypted-payload-placeholder", NOW)


def test_requires_authentication(client, users):
    assert client.get("/api/platforms/instagram/status").status_code == 401


def test_a_user_with_no_instagram_connection_is_not_connected(client, users):
    _act_as(users[0])
    assert client.get("/api/platforms/instagram/status").json() == {
        "platform": "instagram", "connected": False, "status": "DISCONNECTED", "account_label": None,
        "connect_available": False,
    }


def test_an_active_connection_with_a_credential_is_connected(client, users, db_path):
    _connect(db_path, users[0])
    _act_as(users[0])
    body = client.get("/api/platforms/instagram/status").json()
    assert (body["connected"], body["status"]) == (True, "ACTIVE")


def test_an_active_connection_without_a_credential_is_not_connected(client, users, db_path):
    _connect(db_path, users[0], with_credential=False)
    _act_as(users[0])
    body = client.get("/api/platforms/instagram/status").json()
    assert (body["connected"], body["status"]) == (False, "DISCONNECTED")


def test_a_disconnected_connection_reports_its_status(client, users, db_path):
    _connect(db_path, users[0], with_credential=False, status="DISCONNECTED")
    _act_as(users[0])
    assert client.get("/api/platforms/instagram/status").json()["connected"] is False


def test_one_users_instagram_connection_is_invisible_to_another(client, users, db_path):
    _connect(db_path, users[0])
    _act_as(users[1])
    assert client.get("/api/platforms/instagram/status").json()["connected"] is False


def test_response_never_carries_credential_or_platform_account_identifiers(client, users, db_path):
    _connect(db_path, users[0])
    _act_as(users[0])
    raw = client.get("/api/platforms/instagram/status").text
    assert "encrypted-payload-placeholder" not in raw and "17841400000000000" not in raw
    assert "token" not in raw.lower()


def test_connect_is_offered_once_the_server_is_configured(client, users, monkeypatch):
    # Milestone 4.0 asserted False here (no connect flow yet); 4.1 ships it.
    monkeypatch.setattr(config, "INSTAGRAM_APP_ID", "1234567890")
    monkeypatch.setattr(config, "INSTAGRAM_APP_SECRET", "x")
    monkeypatch.setattr(config, "INSTAGRAM_REDIRECT_URI", "https://api.example.com/api/platforms/instagram/callback")
    monkeypatch.setattr(config, "FRONTEND_BASE_URL", "https://app.example.com")
    _act_as(users[0])
    assert client.get("/api/platforms/instagram/status").json()["connect_available"] is True


def test_instagram_does_not_change_tiktok_status(client, users, db_path):
    _connect(db_path, users[0])
    _act_as(users[0])
    body = client.get("/api/platforms/tiktok/status").json()
    assert body["platform"] == "tiktok" and body["connected"] is False
