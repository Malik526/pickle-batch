/**
 * types.ts — frontend domain types (Milestone 3.5, Phase 13).
 *
 * These represent API/product concepts, not raw database columns — the UI
 * never imports or assumes a Postgres/SQLite schema shape directly (see
 * docs/decisions/0010-frontend-app-shell.md "Shared Types"). Field names
 * and shapes here are deliberately looser/friendlier than the backend's
 * own `videos`/`platform_posts` tables (e.g. `PlatformPostStatus` is
 * lowercase and only carries the four states the UI actually needs to
 * distinguish, not every backend column like retry_count or
 * next_status_check_at).
 */

import type { PlatformId, PlatformIdValue } from "@/lib/domain/publishing";

export type PlatformPostStatus = "pending" | "publishing" | "published" | "failed";

/** Milestone 3.15: an alias of the domain platform id (lib/domain/publishing.ts). */
export type Platform = PlatformIdValue;

export type VideoStatus = "processing" | "ready" | "needs_review" | "failed";

export interface VideoSummary {
  id: string;
  title: string;
  status: VideoStatus;
  durationSeconds: number | null;
  createdAt: string;
}

export interface PlatformPostSummary {
  id: string;
  videoId: string;
  platform: Platform;
  status: PlatformPostStatus;
  scheduledAt: string | null;
  publishedAt: string | null;
}

export interface QueueItem {
  id: string;
  videoTitle: string;
  platform: Platform;
  status: PlatformPostStatus;
  scheduledAt: string | null;
}

export interface PlatformConnectionSummary {
  id: string;
  platform: Platform;
  accountLabel: string | null;
  status: "connected" | "disconnected" | "needs_attention";
}

/**
 * The real GET /api/me and /api/platforms/tiktok/status response shapes
 * (Milestone 3.6) — see api/schemas/me.py and api/schemas/platforms.py on
 * the backend. Deliberately mirrors those Pydantic models' field names
 * (snake_case, matching the backend's actual JSON) rather than
 * reformatting to camelCase, since these are read directly from the wire
 * with no transform layer in between — unlike PlatformConnectionSummary
 * above, which is a UI-only shape for still-mocked data.
 */
export interface CurrentUser {
  id: number;
  email: string;
  display_name: string | null;
}

/**
 * Milestone 4.0 — the platform-neutral connection shape
 * (api/schemas/platforms.py PlatformConnectionStatus), first used by
 * Instagram. connect_available is false unless that platform's connect
 * flow exists and is configured on the server; never offer Connect
 * otherwise. Milestone 4.1: account_label is "@username" for a connected
 * Instagram account when Instagram reported one, else null — never the
 * numeric account id.
 */
export interface PlatformConnectionStatus {
  platform: PlatformIdValue;
  connected: boolean;
  status: string;
  account_label: string | null;
  connect_available: boolean;
}

/** Milestone 4.1 — a connect request's response (api/schemas/platforms.py
 * ConnectStartResponse): the platform's authorization URL, nothing else. */
export interface ConnectStartResponse {
  authorization_url: string;
}

export interface TikTokConnectionStatus {
  platform: typeof PlatformId.TIKTOK;
  connected: boolean;
  status: string;
  account_label: string | null;
  /** Milestone 3.14 follow-up — from TikTok's creator_info; null when
   * unknown. Optional so older API responses still type-check. */
  creator_username?: string | null;
  creator_nickname?: string | null;
  creator_avatar_url?: string | null;
}

/**
 * The real GET/POST /api/videos response shapes (Milestone 3.7) — see
 * api/schemas/videos.py. Same snake_case-mirrors-the-wire convention as
 * CurrentUser/TikTokConnectionStatus above; VideoSummary (top of this
 * file) stays the separate, still-mock-only shape Queue's UI uses.
 */
export interface VideoResponse {
  id: number;
  original_filename: string;
  status: string;
  file_size_bytes: number | null;
  created_at: string;
  /** Milestone 3.9 (Queue + Calendar Functionality) — non-null means this
   * video already occupies a content_slot; the Queue's "unscheduled
   * videos" list filters on this rather than re-deriving it. */
  assigned_slot_id: number | null;
  /** Milestone 3.14 final follow-up — "UNSCHEDULED", or the same
   * display_status the Queue shows for this video's slot (SCHEDULED,
   * PUBLISHING, PUBLISHED, FAILED, NEEDS_ATTENTION). Resolved server-side
   * by publish_status.resolve_video_publish_status; never derive it here.
   * Optional only because an older deployed API omits it — read it through
   * lib/status.ts's libraryPublishStatus. */
  publish_status?: string;
}

export interface VideoListResponse {
  videos: VideoResponse[];
}

export interface VideoUploadResult {
  filename: string;
  success: boolean;
  video: VideoResponse | null;
  error: string | null;
}

export interface VideoUploadBatchResponse {
  results: VideoUploadResult[];
}

/**
 * The real GET/PUT /api/cadence and GET /api/cadence/slots response
 * shapes (Milestone 3.8) — see api/schemas/cadence.py. Same
 * snake_case-mirrors-the-wire convention as VideoResponse above.
 */
export interface PostingTime {
  weekday: string;
  posting_time: string;
}

export interface CadenceResponse {
  configured: boolean;
  timezone: string | null;
  is_active: boolean;
  posting_times: PostingTime[];
}

export interface CadenceRequest {
  timezone: string;
  is_active: boolean;
  posting_times: PostingTime[];
}

export interface SlotResponse {
  id: number;
  scheduled_at: string;
  status: string;
  timezone: string | null;
}

export interface SlotListResponse {
  slots: SlotResponse[];
}

/**
 * The real GET /api/queue/slots and assign/unassign response shapes
 * (Milestone 3.9: Queue + Calendar Functionality) — see
 * api/schemas/queue.py. Same snake_case-mirrors-the-wire convention as
 * VideoResponse/SlotResponse above. Deliberately a separate shape from
 * SlotResponse (Milestone 3.8's simpler cadence-preview slot) rather than
 * widening it — this one carries the occupying video and a derived
 * publish-state label that Milestone 3.8's cadence preview never needed.
 */
export interface QueueVideoSummary {
  id: number;
  original_filename: string;
  caption: CaptionResponse;
}

/** One platform's resolved publish state (Milestone 3.11). */
export interface PublicationStatus {
  platform: string;
  display_status: string;
  platform_post_status: string;
  published_at: string | null;
  reason_code: string | null;
  message: string | null;
  action_hint: string | null;
  /** Milestone 4.2 — "PROCESSING" (the platform is processing the upload)
   * or "PUBLISHING" (the post is being made) for Instagram while
   * PUBLISHING; null/absent otherwise or from an older API. */
  stage?: string | null;
}

export interface QueueSlotResponse {
  id: number;
  scheduled_at: string;
  timezone: string | null;
  status: string;
  /** OPEN | SCHEDULED | PUBLISHING | PUBLISHED | FAILED | NEEDS_ATTENTION
   * (Milestone 3.11 — resolved server-side; present via lib/status.ts). */
  display_status: string;
  /** Sanitized, user-facing — never raw backend error text. */
  reason_code: string | null;
  message: string | null;
  action_hint: string | null;
  /** Aware UTC; set only when PUBLISHED. */
  published_at: string | null;
  can_unassign: boolean;
  /** Milestone 3.13: a FAILED or UNKNOWN post that POST .../retry accepts. */
  can_retry: boolean;
  /** Retrying could duplicate a post that may already be live — the user
   * must confirm it isn't on the platform first (confirm_not_published). */
  retry_requires_confirmation: boolean;
  assigned_video: QueueVideoSummary | null;
  platform_post_status: string | null;
  publications: PublicationStatus[];
}

export interface QueueSlotListResponse {
  slots: QueueSlotResponse[];
}

/**
 * The real GET/PUT /api/videos/{id}/caption and POST .../caption/generate
 * response shape (Milestone 3.10: Caption Generation + Editing) — see
 * api/schemas/captions.py. Also carried on every assigned
 * QueueVideoSummary so the Queue needs no extra request per slot.
 */
export type CaptionProvenance = "NONE" | "MANUAL" | "GENERATED" | "GENERATED_EDITED";

export interface CaptionResponse {
  video_id: number;
  caption_text: string | null;
  provenance: CaptionProvenance;
  /** A usable transcript exists, so generation can succeed. */
  can_generate: boolean;
  /** False once the video has been submitted to a platform. */
  editable: boolean;
  /** Milestone 3.10.1 — read-only hashtags derived from caption_text, in
   * caption order. Not rendered or editable separately: the caption
   * textarea stays the one place hashtags are written. */
  hashtags: string[];
}
