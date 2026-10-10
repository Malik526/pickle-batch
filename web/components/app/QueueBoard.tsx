"use client";

import { useState } from "react";
import { QueueCalendarMonth } from "@/components/app/QueueCalendarMonth";
import { QueueList } from "@/components/app/QueueList";
import { PlatformPicker } from "@/components/app/PlatformPicker";
import { QueueSlotCard } from "@/components/app/QueueSlotCard";
import { Card } from "@/components/ui/Card";
import { ErrorState } from "@/components/ui/ErrorState";
import { Spinner } from "@/components/ui/Spinner";
import { useQueueActions } from "@/hooks/useQueueActions";
import { useQueueSlots } from "@/hooks/useQueueSlots";
import { useVideos } from "@/hooks/useVideos";
import { ApiError } from "@/lib/api/client";
import { useInstagramConnection } from "@/hooks/useInstagramConnection";
import { useTikTokConnection } from "@/hooks/useTikTokConnection";
import { PlatformId } from "@/lib/domain/publishing";
import { usePersistentUiState } from "@/lib/ui-state";

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** A naive-local "YYYY-MM-DDTHH:mm:ss" string from a Date's own local
 * components — Date.toISOString() converts to UTC first, which is wrong
 * here: scheduled_at (and the window this compares it against) is
 * naive-local, matching every other slot-related timestamp in this
 * codebase (see AGENTS.md's timestamp-convention note). */
function toNaiveIso(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function monthWindow(monthCursor: Date): { from: string; to: string } {
  const year = monthCursor.getFullYear();
  const month = monthCursor.getMonth();
  return {
    from: toNaiveIso(new Date(year, month, 1, 0, 0, 0)),
    to: toNaiveIso(new Date(year, month + 1, 1, 0, 0, 0)),
  };
}

/**
 * QueueBoard — the real Queue: a list/calendar toggle over GET
 * /api/queue/slots for the currently displayed month, an "Unscheduled
 * videos" section for automatic (FIFO) assignment, and per-slot manual
 * assign/"Remove from schedule" actions (Milestone 3.9: Queue + Calendar
 * Functionality). Composes three pure presentational pieces:
 * QueueList/QueueCalendarMonth (the two view modes) and QueueSlotCard
 * (one slot's full detail + actions — used as every List row and as
 * Calendar mode's selected-slot detail panel, so assign/remove logic
 * lives in exactly one place).
 *
 * Milestone 3.10: each assigned slot also shows its video's caption
 * (CaptionEditor, inside QueueSlotCard). Caption saves patch the one
 * affected slot in place rather than reloading the whole board, so other
 * slots' unsaved caption drafts survive.
 *
 * No drag/drop, no per-slot editing beyond assign/remove, no manual
 * one-off slot creation — all explicitly deferred to a later milestone
 * per this milestone's own brief.
 *
 * Milestone 3.14:
 *   refreshKey — the board reloads whenever this changes (the page bumps it
 *     after a cadence save, which regenerates slots server-side).
 *   Retry — a FAILED/UNKNOWN slot's recovery action (POST .../retry); the
 *     confirmation step lives in QueueSlotCard.
 *
 * Milestone 3.15: slots (per month) and videos come from the shared cache
 * (useQueueSlots, useVideos — the same videos entry Library and Home
 * use), and every action goes through useQueueActions, which refreshes
 * the Queue and the Library's statuses together. The refreshKey prop is
 * gone: a cadence save invalidates the cached Queue itself. Revisits and
 * month changes keep the current slots on screen while fresh data loads;
 * a failed background refresh shows a Retry notice above them. The
 * list/calendar choice and the displayed month survive navigation.
 *
 * Milestone 4.2: with Instagram connected, a "Publish to" picker
 * (PlatformPicker) chooses the platforms each newly scheduled video goes to;
 * the choice survives navigation. Without Instagram, assignment sends no
 * platforms and the server keeps its default, exactly as before.
 */
export function QueueBoard() {
  const [viewMode, setViewMode] = usePersistentUiState<"list" | "calendar">("queue.viewMode", "list");
  const [monthCursor, setMonthCursor] = usePersistentUiState("queue.month", () => new Date());
  const { from, to } = monthWindow(monthCursor);
  const slotsQuery = useQueueSlots(from, to);
  const videosQuery = useVideos();
  const actions = useQueueActions();
  const slots = slotsQuery.data ?? null;
  const videos = videosQuery.data ?? [];
  const [selectedSlotId, setSelectedSlotId] = useState<number | null>(null);
  const [busySlotId, setBusySlotId] = useState<number | null>(null);
  const [busyVideoId, setBusyVideoId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const unassignedVideos = videos.filter((video) => video.assigned_slot_id === null);

  // Milestone 4.2: once Instagram is connected the user chooses where each
  // scheduled video goes; until then nothing changes (server default).
  const tiktokConnected = useTikTokConnection().data?.connected ?? false;
  const instagramConnected = useInstagramConnection().data?.connected ?? false;
  const platformOptions = [
    ...(tiktokConnected ? [{ id: PlatformId.TIKTOK, label: "TikTok" }] : []),
    { id: PlatformId.INSTAGRAM, label: "Instagram" },
  ];
  const [chosenPlatforms, setChosenPlatforms] = usePersistentUiState<string[]>("queue.platforms", [PlatformId.TIKTOK]);
  const platformChoice = instagramConnected
    ? chosenPlatforms.filter((id) => platformOptions.some((option) => option.id === id))
    : undefined;
  const nothingChosen = platformChoice !== undefined && platformChoice.length === 0;
  const selectedSlot = slots?.find((slot) => slot.id === selectedSlotId) ?? null;

  function retryLoad() {
    void slotsQuery.refetch();
    void videosQuery.refetch();
  }

  async function handleAssign(slotId: number, videoId: number) {
    setActionError(null);
    setBusySlotId(slotId);
    try {
      await actions.assign(slotId, videoId, platformChoice);
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : "Could not assign that video.");
    } finally {
      setBusySlotId(null);
    }
  }

  async function handleAssignNext(videoId: number) {
    setActionError(null);
    setBusyVideoId(videoId);
    try {
      await actions.assignNext(videoId, platformChoice);
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : "Could not assign that video to the next open slot.");
    } finally {
      setBusyVideoId(null);
    }
  }

  async function handleRemove(slotId: number) {
    setActionError(null);
    setBusySlotId(slotId);
    try {
      await actions.unassign(slotId);
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : "Could not remove this video from the schedule.");
    } finally {
      setBusySlotId(null);
    }
  }

  async function handleRetry(slotId: number, confirmNotPublished: boolean) {
    setActionError(null);
    setBusySlotId(slotId);
    try {
      await actions.retry(slotId, confirmNotPublished);
    } catch (error) {
      // The backend's 409 messages are written for users (scheduling/manual_recovery.py).
      setActionError(error instanceof ApiError ? error.message : "Could not retry this post.");
      if (error instanceof ApiError && error.reasonCode === "CONCURRENT_UPDATE") await actions.refresh();
    } finally {
      setBusySlotId(null);
    }
  }

  // Caption errors propagate to CaptionEditor, which shows them inline.
  const handleSaveCaption = actions.saveCaption;
  const handleGenerateCaption = actions.generateCaption;

  const loadFailed = slotsQuery.isError || videosQuery.isError;
  const loadError = slotsQuery.error ?? videosQuery.error;

  if (loadFailed && slots === null) {
    return (
      <ErrorState message={loadError instanceof ApiError ? loadError.message : "Could not load your queue."} onRetry={retryLoad} />
    );
  }

  if (slots === null) {
    return (
      <Card>
        <Spinner label="Loading your queue…" />
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {loadFailed ? (
        <ErrorState message="Couldn't refresh your queue. Showing the last schedule loaded." onRetry={retryLoad} />
      ) : null}
      {actionError ? <ErrorState message={actionError} /> : null}

      <section aria-labelledby="unscheduled-videos-heading">
        <h3 id="unscheduled-videos-heading" className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
          Unscheduled videos
        </h3>
        {platformChoice !== undefined ? (
          <div className="mb-3">
            <PlatformPicker options={platformOptions} selected={platformChoice} onChange={setChosenPlatforms} />
            {nothingChosen ? <p className="mt-1 text-xs text-status-danger">Choose at least one platform.</p> : null}
          </div>
        ) : null}
        {unassignedVideos.length === 0 ? (
          <p className="text-sm text-ink-muted">No unscheduled videos — upload one in Library.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {unassignedVideos.map((video) => (
              <li key={video.id} className="flex items-center justify-between gap-3 rounded-lg border border-border px-3 py-2">
                <p className="truncate text-sm text-ink">{video.original_filename}</p>
                <button
                  type="button"
                  disabled={busyVideoId === video.id || nothingChosen}
                  onClick={() => void handleAssignNext(video.id)}
                  className="tap-target shrink-0 rounded-lg border border-border bg-surface px-3 py-1.5 text-xs font-medium text-ink hover:border-accent/40 hover:text-accent disabled:opacity-60"
                >
                  {busyVideoId === video.id ? "Assigning…" : "Assign to next available slot"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setViewMode("list")}
          aria-pressed={viewMode === "list"}
          className={`tap-target rounded-lg border px-3 py-1.5 text-sm font-medium ${
            viewMode === "list" ? "border-accent text-accent" : "border-border text-ink-muted"
          }`}
        >
          List
        </button>
        <button
          type="button"
          onClick={() => setViewMode("calendar")}
          aria-pressed={viewMode === "calendar"}
          className={`tap-target rounded-lg border px-3 py-1.5 text-sm font-medium ${
            viewMode === "calendar" ? "border-accent text-accent" : "border-border text-ink-muted"
          }`}
        >
          Calendar
        </button>
      </div>

      {viewMode === "list" ? (
        <QueueList
          slots={slots}
          unassignedVideos={unassignedVideos}
          busySlotId={busySlotId}
          onAssign={(slotId, videoId) => void handleAssign(slotId, videoId)}
          onRemove={(slotId) => void handleRemove(slotId)}
          onRetry={(slotId, confirmNotPublished) => void handleRetry(slotId, confirmNotPublished)}
          onSaveCaption={handleSaveCaption}
          onGenerateCaption={handleGenerateCaption}
        />
      ) : (
        <div className="flex flex-col gap-4">
          <QueueCalendarMonth
            month={monthCursor}
            slots={slots}
            selectedSlotId={selectedSlotId}
            onSelectSlot={setSelectedSlotId}
            onPrevMonth={() => setMonthCursor((current) => new Date(current.getFullYear(), current.getMonth() - 1, 1))}
            onNextMonth={() => setMonthCursor((current) => new Date(current.getFullYear(), current.getMonth() + 1, 1))}
          />
          {selectedSlot ? (
            <QueueSlotCard
              slot={selectedSlot}
              unassignedVideos={unassignedVideos}
              busy={busySlotId === selectedSlot.id}
              onAssign={(videoId) => void handleAssign(selectedSlot.id, videoId)}
              onRemove={() => void handleRemove(selectedSlot.id)}
              onRetry={(confirmNotPublished) => void handleRetry(selectedSlot.id, confirmNotPublished)}
              onSaveCaption={handleSaveCaption}
              onGenerateCaption={handleGenerateCaption}
            />
          ) : (
            <p className="text-sm text-ink-muted">Select a date&apos;s slot to see its details.</p>
          )}
        </div>
      )}
    </div>
  );
}
