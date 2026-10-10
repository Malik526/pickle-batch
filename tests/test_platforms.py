"""Tests for the publishing platform registry (Milestone 4.0) and the
places that rely on it: failure-taxonomy labels and the platform_posts
materializer's guard against platforms with no hosted publisher."""

import pytest

from content_automation.persistence.content_store import ContentStore
from content_automation.publishing import failure_taxonomy
from content_automation.publishing.platforms import (
    INSTAGRAM,
    PLATFORMS,
    PULL_URL,
    PUSH_FILE,
    TIKTOK,
    UnknownPlatformError,
    get_platform,
    publishable,
)
from content_automation.scheduling import platform_post_materializer as ppm


def test_registry_knows_tiktok_and_instagram_with_their_real_differences():
    tiktok, instagram = get_platform(TIKTOK), get_platform(INSTAGRAM)
    assert (tiktok.label, tiktok.media_delivery, tiktok.requires_finalize_step) == ("TikTok", PUSH_FILE, False)
    assert (instagram.label, instagram.media_delivery, instagram.requires_finalize_step) == ("Instagram", PULL_URL, True)
    assert instagram.caption_max_chars == 2200


def test_instagram_is_connectable_and_publishable_since_4_2():
    # Milestone 4.1 shipped the connect flow; Milestone 4.2 Reels publishing.
    assert get_platform(TIKTOK).connection_available and get_platform(TIKTOK).publishing_available
    assert get_platform(INSTAGRAM).connection_available and get_platform(INSTAGRAM).publishing_available


@pytest.fixture
def unpublishable_platform(monkeypatch):
    """A registered platform with no hosted publisher yet — what Instagram
    was before Milestone 4.2 — so the publishable() guard stays tested."""
    from dataclasses import replace

    from content_automation.publishing import platforms

    monkeypatch.setitem(platforms.PLATFORMS, "futuregram", replace(PLATFORMS[INSTAGRAM], id="futuregram", label="Futuregram", publishing_available=False))
    return "futuregram"


def test_unknown_platform_raises():
    with pytest.raises(UnknownPlatformError):
        get_platform("myspace")


def test_publishable_keeps_order_and_drops_unknown_or_unpublishable_platforms(unpublishable_platform):
    assert publishable(["tiktok"]) == ["tiktok"]
    assert publishable(["instagram", unpublishable_platform, "tiktok", "myspace"]) == ["instagram", "tiktok"]
    assert publishable([]) == []


def test_failure_labels_come_from_the_registry():
    assert set(failure_taxonomy.PLATFORM_LABELS) == set(PLATFORMS)
    assert failure_taxonomy.platform_label(INSTAGRAM) == "Instagram"
    assert failure_taxonomy.platform_label(TIKTOK) == "TikTok"


def test_materializer_creates_no_posts_for_a_platform_without_a_publisher(tmp_path, monkeypatch, unpublishable_platform):
    monkeypatch.setattr(ppm, "TARGET_PUBLISHING_PLATFORMS", ["tiktok", unpublishable_platform])
    with ContentStore(db_path=tmp_path / "t.db") as store:
        store.insert_slot_if_missing("2099-01-05T09:00:00", None, None, "2026-10-04T00:00:00")
        slot = store.list_slots_by_status(["OPEN"])[0]
        video = store.insert_video("hash", "v.mp4", "/in/v.mp4", "2026-10-04T00:00:00")
        store.assign_slot(video.id, slot.id)

        ppm.materialize_platform_posts_for_assignment(store, video.id, slot.id, "2026-10-04T00:00:00")

        assert store.get_platform_post(video.id, "tiktok") is not None
        assert store.get_platform_post(video.id, unpublishable_platform) is None
