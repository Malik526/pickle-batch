"use client";

import { useState } from "react";
import { CaptionEditor } from "@/components/app/CaptionEditor";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import Link from "next/link";
import { PlatformDeliveryList } from "@/components/app/PlatformDeliveryList";
import { presentActionHint, presentQueueStatus } from "@/lib/status";
import type { CaptionResponse, QueueSlotResponse, VideoResponse } from "@/lib/api/types";

/** published_at is aware UTC — unlike scheduled_at, converting it to the
 * viewer's local time is correct here. */
function formatPublishedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function formatSlotDateTime(iso: string): string {
  // scheduled_at is naive-local (see lib/api/types.ts's own QueueSlotResponse
  // note) — new Date() on a bare ISO string with no trailing Z/offset
  // parses it as local wall-clock time, so this reformats the same numbers
  // rather than converting between timezones (same reasoning as the
  // now-retired UpcomingSchedulePreview it replaces).
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

/**
 * QueueSlotCard — full detail + actions for exactly one content_slot
 * (Milestone 3.9: Queue + Calendar Functionality). Used both as each row
 * in QueueList and as the selected-slot detail panel under
 * QueueCalendarMonth, so assign/remove logic lives in exactly one place.
 *
 * Props:
 *   slot — the real GET /api/queue/slots row.
 *   unassignedVideos — the caller's own videos with no assigned_slot_id
 *     yet (Library videos eligible for manual assignment to an OPEN slot).
 *   onAssign(videoId) — manual assignment; only rendered for an OPEN slot.
 *   onRemove() — "Remove from schedule"; only rendered when the backend
 *     reports can_unassign (every platform post still PENDING — the same
 *     guard as PlatformPostInProgressError, so the action is never offered
 *     once it would be refused).
 *
 * Milestone 3.11: the badge comes from lib/status.ts's presentQueueStatus
 * (shared with the calendar); slot.message (sanitized, server-written
 * copy) is shown under the status, plus an advisory action hint and the
 * publish time once published.
 *   onRetry(confirmNotPublished) — Milestone 3.14: shown only when the
 *     backend reports can_retry. When retry_requires_confirmation, the
 *     first click opens an inline confirmation (the post may already be
 *     live, and there's no platform id to check) and only the explicit
 *     confirm sends confirmNotPublished=true.
 *   busy — disables both actions while a request for this slot is in flight.
 *   onSaveCaption(videoId, text) / onGenerateCaption(videoId, overwrite) —
 *     Milestone 3.10: the assigned video's caption actions, rendered via
 *     CaptionEditor below the slot row.
 *
 * Milestone 4.2: when the slot's video goes to Instagram (or more than one
 * platform), PlatformDeliveryList shows each platform's state on its own
 * row in place of the single message; the badge stays the slot's overall
 * (most urgent) status.
 */
export function QueueSlotCard({
  slot,
  unassignedVideos,
  onAssign,
  onRemove,
  onRetry,
  onSaveCaption,
  onGenerateCaption,
  busy = false,
}: {
  slot: QueueSlotResponse;
  unassignedVideos: VideoResponse[];
  onAssign: (videoId: number) => void;
  onRemove: () => void;
  onRetry: (confirmNotPublished: boolean) => void;
  onSaveCaption: (videoId: number, text: string) => Promise<CaptionResponse>;
  onGenerateCaption: (videoId: number, overwrite: boolean) => Promise<CaptionResponse>;
  busy?: boolean;
}) {
  const assignedVideo = slot.assigned_video;
  const [pendingVideoId, setPendingVideoId] = useState<string>("");
  const [confirmingRetry, setConfirmingRetry] = useState(false);
  const presentation = presentQueueStatus(slot.display_status);
  // Milestone 4.2: list deliveries per platform once a slot goes anywhere
  // besides TikTok alone; a TikTok-only slot reads exactly as before.
  const showsDeliveries = slot.publications.some((publication) => publication.platform !== "tiktok") || slot.publications.length > 1;
  const hint = presentActionHint(slot.action_hint);

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink">{formatSlotDateTime(slot.scheduled_at)}</p>
          {slot.assigned_video ? (
            <p className="mt-0.5 truncate text-xs text-ink-muted">{slot.assigned_video.original_filename}</p>
          ) : null}
          {slot.published_at ? (
            <p className="mt-0.5 text-xs text-ink-muted">Published {formatPublishedAt(slot.published_at)}</p>
          ) : null}
          {showsDeliveries ? (
            <div className="mt-2">
              <PlatformDeliveryList publications={slot.publications} />
            </div>
          ) : slot.message ? (
            <p className="mt-1 text-xs text-ink">{slot.message}</p>
          ) : null}
          {hint ? (
            hint.href ? (
              <Link href={hint.href} className="tap-target mt-0.5 inline-block text-xs font-medium text-accent hover:underline">
                {hint.label}
              </Link>
            ) : (
              <p className="mt-0.5 text-xs text-ink-muted">{hint.label}</p>
            )
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-3">
          <Badge tone={presentation.tone}>{presentation.label}</Badge>

          {slot.status === "OPEN" ? (
            unassignedVideos.length === 0 ? (
              <p className="text-xs text-ink-muted">No unscheduled videos</p>
            ) : (
              <div className="flex items-center gap-2">
                <select
                  value={pendingVideoId}
                  onChange={(event) => setPendingVideoId(event.target.value)}
                  aria-label={`Video to assign to ${formatSlotDateTime(slot.scheduled_at)}`}
                  className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-ink pointer-coarse:min-h-11"
                >
                  <option value="">Choose a video…</option>
                  {unassignedVideos.map((video) => (
                    <option key={video.id} value={video.id}>
                      {video.original_filename}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={!pendingVideoId || busy}
                  onClick={() => onAssign(Number(pendingVideoId))}
                  className="tap-target rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium text-ink hover:border-accent/40 hover:text-accent disabled:opacity-60"
                >
                  Assign
                </button>
              </div>
            )
          ) : slot.status === "ASSIGNED" && slot.can_unassign ? (
            <button
              type="button"
              disabled={busy}
              onClick={onRemove}
              className="tap-target text-xs font-medium text-ink-muted hover:text-status-danger disabled:opacity-60"
            >
              Remove from schedule
            </button>
          ) : slot.can_retry && !confirmingRetry ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => (slot.retry_requires_confirmation ? setConfirmingRetry(true) : onRetry(false))}
              className="tap-target rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium text-ink hover:border-accent/40 hover:text-accent disabled:opacity-60"
            >
              {busy ? "Retrying…" : "Retry"}
            </button>
          ) : null}
        </div>
      </div>

      {slot.can_retry && confirmingRetry ? (
        <div role="group" aria-label="Confirm retry" className="flex flex-col gap-2 rounded-lg border border-border px-3 py-2">
          <p className="text-xs text-ink">
            We can&apos;t tell whether this was posted. Check your TikTok profile first — if it&apos;s already there,
            retrying will post it a second time.
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirmingRetry(false);
                onRetry(true);
              }}
              className="tap-target rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium text-ink hover:border-accent/40 hover:text-accent disabled:opacity-60"
            >
              It isn&apos;t posted — retry
            </button>
            <button
              type="button"
              onClick={() => setConfirmingRetry(false)}
              className="tap-target text-xs font-medium text-ink-muted hover:text-ink"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {assignedVideo ? (
        <CaptionEditor
          key={assignedVideo.id}
          caption={assignedVideo.caption}
          onSave={(text) => onSaveCaption(assignedVideo.id, text)}
          onGenerate={(overwrite) => onGenerateCaption(assignedVideo.id, overwrite)}
        />
      ) : null}
    </Card>
  );
}
