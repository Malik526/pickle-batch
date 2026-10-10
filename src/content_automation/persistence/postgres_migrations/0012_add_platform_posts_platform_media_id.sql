-- 0012_add_platform_posts_platform_media_id.sql — Milestone 4.2 (Instagram
-- Reels publishing). Purely additive: one nullable column, NULL for every
-- existing row.
--
-- platform_media_id is the platform's id for the published post when it
-- differs from the submission handle kept in platform_post_id. Instagram
-- publishes in two steps: a media container (platform_post_id — what status
-- checks and crash recovery use, never overwritten) and, after
-- media_publish, a separate Instagram media id (platform_media_id). It stays
-- NULL until published, and also when publication was only confirmed from
-- the container's status. TikTok leaves it NULL. Same column as
-- persistence/content_store.py's SQLite _PLATFORM_POSTS_MIGRATION_COLUMNS.
--
-- platform_posts.submission_state gains one value, PUBLISH_REQUESTED (no
-- constraint lists the allowed values, so no DDL is needed): written,
-- compare-and-swap, immediately before Instagram's media_publish call and
-- cleared once the outcome is known. See scheduling/finalization.py.

ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS platform_media_id TEXT;
