"""Forward compatibility of PostgresContentStore reads (Milestone 4.2): a
column added by a newer deployment's migration must not break records built
by code that doesn't know it yet. (Before 4.2, SELECT * rows were passed
straight to the dataclass, so applying 0012 under running older code made
every platform_posts read raise TypeError.) Postgres only; throwaway schema."""

import psycopg
import pytest

from content_automation.config import DATABASE_URL, POSTGRES_SCHEMA, POSTGRES_TEST_SCHEMA

PG_SCHEMA = f"{POSTGRES_TEST_SCHEMA}_forward_compat"
assert PG_SCHEMA != POSTGRES_SCHEMA
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not configured — Postgres tests skipped")


@pytest.fixture
def store():
    from content_automation.persistence.postgres_content_store import PostgresContentStore

    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA IF EXISTS "{PG_SCHEMA}" CASCADE')
        admin.execute(f'CREATE SCHEMA "{PG_SCHEMA}"')
    with PostgresContentStore(dsn=DATABASE_URL, schema=PG_SCHEMA) as s:
        yield s
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA IF EXISTS "{PG_SCHEMA}" CASCADE')


def test_reads_ignore_columns_added_by_a_newer_migration(store):
    user = store.create_user("a@example.com", None, "2026-10-10T00:00:00+00:00")
    video = store.insert_video("hash", "v.mp4", "/in/v.mp4", "2026-10-10T00:00:00", user_id=user.id)
    store.insert_platform_post_if_missing(video.id, "instagram", scheduled_at=None, created_at="2026-10-10T00:00:00", user_id=user.id)
    for table in ("platform_posts", "videos", "users"):
        store._conn.execute(f"ALTER TABLE {table} ADD COLUMN future_column TEXT DEFAULT 'from the future'")

    post = store.get_platform_post(video.id, "instagram")
    assert post.platform == "instagram" and not hasattr(post, "future_column")
    assert store.get_video(video.id).original_filename == "v.mp4"
    assert store.list_platform_posts_for_video(video.id)[0].id == post.id


def test_platform_media_id_round_trips(store):
    user = store.create_user("a@example.com", None, "2026-10-10T00:00:00+00:00")
    video = store.insert_video("hash", "v.mp4", "/in/v.mp4", "2026-10-10T00:00:00", user_id=user.id)
    store.insert_platform_post_if_missing(video.id, "instagram", scheduled_at=None, created_at="2026-10-10T00:00:00", user_id=user.id)
    post = store.get_platform_post(video.id, "instagram")
    assert post.platform_media_id is None  # 0012 applied, NULL by default
    store.update_platform_post(post.id, updated_at="2026-10-10T01:00:00+00:00", platform_post_id="container-1", platform_media_id="media-1")
    post = store.get_platform_post(video.id, "instagram")
    assert (post.platform_post_id, post.platform_media_id) == ("container-1", "media-1")
