/**
 * queue.ts — typed calls to the real /api/queue endpoints Milestone 3.9
 * (Queue + Calendar Functionality) added
 * (src/content_automation/api/routes/queue.py). Same pattern as
 * lib/api/cadence.ts/videos.ts: every function takes the caller's current
 * accessToken explicitly rather than reading it itself.
 */

import { apiRequest } from "@/lib/api/client";
import type { QueueSlotListResponse, QueueSlotResponse } from "@/lib/api/types";

/** fromIso/toIso are plain "YYYY-MM-DDTHH:MM:SS"-style bounds, matching
 * the naive-local scheduled_at convention this window is compared against
 * server-side (see api/routes/queue.py) — the caller (QueueBoard) computes
 * these from whatever month/window it's currently displaying. */
export function listQueueSlots(accessToken: string | null, fromIso: string, toIso: string): Promise<QueueSlotListResponse> {
  const params = new URLSearchParams({ from: fromIso, to: toIso });
  return apiRequest<QueueSlotListResponse>(`/api/queue/slots?${params.toString()}`, { accessToken });
}

/** Manual assignment: claim a specific OPEN slot for a specific owned video. */
/** Milestone 4.2: `platforms` chooses where to publish; omitted keeps the
 * server's default (TikTok). Sent only when given. */
function assignBody(videoId: number, platforms?: string[]) {
  return platforms ? { video_id: videoId, platforms } : { video_id: videoId };
}

export function assignVideoToSlot(
  accessToken: string | null, slotId: number, videoId: number, platforms?: string[],
): Promise<QueueSlotResponse> {
  return apiRequest<QueueSlotResponse>(`/api/queue/slots/${slotId}/assign`, {
    method: "POST", body: assignBody(videoId, platforms), accessToken,
  });
}

/** Automatic/FIFO assignment: claim the caller's earliest eligible OPEN slot. */
export function assignNextOpenSlot(accessToken: string | null, videoId: number, platforms?: string[]): Promise<QueueSlotResponse> {
  return apiRequest<QueueSlotResponse>("/api/queue/assign-next", {
    method: "POST", body: assignBody(videoId, platforms), accessToken,
  });
}

/** "Remove from schedule" — reopens the slot without deleting the video. */
export function unassignSlot(accessToken: string | null, slotId: number): Promise<QueueSlotResponse> {
  return apiRequest<QueueSlotResponse>(`/api/queue/slots/${slotId}/unassign`, { method: "POST", accessToken });
}

/** Milestone 3.13 recovery: retry a FAILED post, re-check an UNKNOWN one
 * that has a platform id, or — only with confirmNotPublished — resubmit an
 * UNKNOWN one that doesn't. The backend decides which; 409 when refused. */
export function retrySlotPublication(
  accessToken: string | null, slotId: number, { confirmNotPublished = false }: { confirmNotPublished?: boolean } = {},
): Promise<QueueSlotResponse> {
  return apiRequest<QueueSlotResponse>(`/api/queue/slots/${slotId}/retry`, {
    method: "POST", body: { confirm_not_published: confirmNotPublished }, accessToken,
  });
}
