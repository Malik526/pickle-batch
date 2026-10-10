/**
 * useQueueActions — the Queue's slot mutations (assign, assign-next,
 * remove from schedule, retry) and caption edits, each followed by the
 * cache update it needs (Milestone 3.15).
 *
 * Slot changes invalidate every cached Queue window and the videos list,
 * since both the slot's state and the video's Library status
 * (publish_status) move together. Caption edits patch the one assigned
 * video's caption inside the cached Queue windows instead of refetching.
 * Each function returns the API result and throws ApiError like the API
 * client does; callers keep their own busy/error UI state.
 */

import { useQueryClient } from "@tanstack/react-query";
import { generateCaption, saveCaption } from "@/lib/api/captions";
import { assignNextOpenSlot, assignVideoToSlot, retrySlotPublication, unassignSlot } from "@/lib/api/queue";
import type { CaptionResponse, QueueSlotResponse } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { useApiAuth } from "@/hooks/useApiAuth";

export function useQueueActions() {
  const queryClient = useQueryClient();
  const { userId, accessToken } = useApiAuth();

  async function refreshAfterSlotChange(): Promise<void> {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.queueSlotsAll(userId) }),
      queryClient.invalidateQueries({ queryKey: queryKeys.videos(userId) }),
    ]);
  }

  async function slotChange<T>(request: Promise<T>): Promise<T> {
    const result = await request;
    await refreshAfterSlotChange();
    return result;
  }

  function applyCaption(caption: CaptionResponse): CaptionResponse {
    queryClient.setQueriesData<QueueSlotResponse[]>({ queryKey: queryKeys.queueSlotsAll(userId) }, (slots) =>
      slots?.map((slot) =>
        slot.assigned_video?.id === caption.video_id ? { ...slot, assigned_video: { ...slot.assigned_video, caption } } : slot,
      ),
    );
    return caption;
  }

  return {
    /** platforms (Milestone 4.2): where to publish; omitted keeps the server default. */
    assign: (slotId: number, videoId: number, platforms?: string[]) =>
      slotChange(
        platforms ? assignVideoToSlot(accessToken, slotId, videoId, platforms) : assignVideoToSlot(accessToken, slotId, videoId),
      ),
    assignNext: (videoId: number, platforms?: string[]) =>
      slotChange(platforms ? assignNextOpenSlot(accessToken, videoId, platforms) : assignNextOpenSlot(accessToken, videoId)),
    unassign: (slotId: number) => slotChange(unassignSlot(accessToken, slotId)),
    retry: (slotId: number, confirmNotPublished: boolean) =>
      slotChange(retrySlotPublication(accessToken, slotId, { confirmNotPublished })),
    /** Refetch Queue + videos without a mutation (e.g. after a CONCURRENT_UPDATE refusal). */
    refresh: refreshAfterSlotChange,
    saveCaption: async (videoId: number, text: string) => applyCaption(await saveCaption(accessToken, videoId, text)),
    generateCaption: async (videoId: number, overwrite: boolean) =>
      applyCaption(await generateCaption(accessToken, videoId, overwrite)),
  };
}
