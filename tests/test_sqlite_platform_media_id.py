"""SQLite side of Milestone 4.2's platform_posts.platform_media_id: present
on a fresh database, added to an existing pre-4.2 database on open, NULL by
default, and round-trips."""

import sqlite3

from content_automation.persistence.content_store import ContentStore


def _round_trip(store):
    user = store.create_user("a@example.com", None, "2026-10-10T00:00:00+00:00")
    video = store.insert_video("hash", "v.mp4", "/in/v.mp4", "2026-10-10T00:00:00", user_id=user.id)
    store.insert_platform_post_if_missing(video.id, "instagram", scheduled_at=None, created_at="2026-10-10T00:00:00", user_id=user.id)
    post = store.get_platform_post(video.id, "instagram")
    assert post.platform_media_id is None
    store.update_platform_post(post.id, updated_at="2026-10-10T01:00:00+00:00", platform_media_id="media-1")
    assert store.get_platform_post(video.id, "instagram").platform_media_id == "media-1"


def test_fresh_database_has_platform_media_id(tmp_path):
    with ContentStore(db_path=tmp_path / "fresh.db") as store:
        _round_trip(store)


def test_existing_database_gains_platform_media_id_on_open(tmp_path):
    path = tmp_path / "existing.db"
    with ContentStore(db_path=path):
        pass
    with sqlite3.connect(path) as conn:  # simulate a pre-4.2 database
        conn.execute("ALTER TABLE platform_posts DROP COLUMN platform_media_id")
        assert "platform_media_id" not in {row[1] for row in conn.execute("PRAGMA table_info(platform_posts)")}
    with ContentStore(db_path=path) as store:
        _round_trip(store)
