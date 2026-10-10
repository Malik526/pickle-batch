import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import QueuePage from "@/app/app/queue/page";
import { presentDelivery } from "@/lib/status";
import { renderWithProviders } from "@/tests/test-utils";

/**
 * Milestone 4.2 — Instagram in the Queue: the "Publish to" picker (shown only
 * once Instagram is connected) and per-platform delivery rows that tell
 * TikTok and Instagram apart, including Instagram's Processing step.
 */

const mockSession = vi.hoisted(() => ({
  user: { id: "u1", email: "creator@example.com", displayName: "Creator" },
  accessToken: "real-token",
  status: "authenticated" as const,
  signOut: vi.fn(),
}));
vi.mock("@/lib/session", () => ({ useSession: () => mockSession }));

const cadenceApi = vi.hoisted(() => ({ getCadence: vi.fn(), saveCadence: vi.fn(), getUpcomingSlots: vi.fn() }));
vi.mock("@/lib/api/cadence", () => cadenceApi);
const queueApi = vi.hoisted(() => ({
  listQueueSlots: vi.fn(), assignVideoToSlot: vi.fn(), assignNextOpenSlot: vi.fn(), unassignSlot: vi.fn(),
  retrySlotPublication: vi.fn(),
}));
vi.mock("@/lib/api/queue", () => queueApi);
const videosApi = vi.hoisted(() => ({ listVideos: vi.fn(), uploadVideos: vi.fn(), deleteVideo: vi.fn() }));
vi.mock("@/lib/api/videos", () => videosApi);
const platformsApi = vi.hoisted(() => ({
  getMe: vi.fn(), getTikTokConnection: vi.fn(), connectTikTok: vi.fn(), disconnectTikTok: vi.fn(),
  getInstagramConnection: vi.fn(), connectInstagram: vi.fn(), disconnectInstagram: vi.fn(),
}));
vi.mock("@/lib/api/platforms", () => platformsApi);

const caption = { video_id: 5, caption_text: null, provenance: "NONE", can_generate: false, editable: true, hashtags: [] };
const publication = (platform: string, display_status: string, extra: Record<string, unknown> = {}) => ({
  platform, display_status, platform_post_status: "PUBLISHING", published_at: null, reason_code: null, message: null,
  action_hint: null, stage: null, ...extra,
});
const slot = (publications: unknown[], display_status = "PUBLISHING") => ({
  id: 2, scheduled_at: "2026-10-06T09:00:00", timezone: "America/New_York", status: "ASSIGNED", display_status,
  reason_code: null, message: null, action_hint: null, published_at: null, can_unassign: false, can_retry: false,
  retry_requires_confirmation: false, platform_post_status: "PUBLISHING", publications,
  assigned_video: { id: 5, original_filename: "reel.mp4", caption },
});
const UNASSIGNED = {
  id: 10, original_filename: "raw.mp4", status: "DISCOVERED", file_size_bytes: 100, created_at: "2026-01-01T00:00:00",
  assigned_slot_id: null, publish_status: "UNSCHEDULED",
};
const connection = (platform: string, connected: boolean) => ({
  platform, connected, status: connected ? "ACTIVE" : "DISCONNECTED", account_label: null, connect_available: true,
});

beforeEach(() => {
  cadenceApi.getCadence.mockResolvedValue({ configured: false, timezone: null, is_active: false, posting_times: [] });
  videosApi.listVideos.mockResolvedValue({ videos: [UNASSIGNED] });
  queueApi.listQueueSlots.mockResolvedValue({ slots: [] });
  platformsApi.getTikTokConnection.mockResolvedValue(connection("tiktok", true));
  platformsApi.getInstagramConnection.mockResolvedValue(connection("instagram", true));
});

afterEach(() => {
  vi.clearAllMocks();
});

const assignButton = () => screen.findByRole("button", { name: "Assign to next available slot" });

describe("Publish to picker", () => {
  it("is hidden and sends no platforms while Instagram isn't connected", async () => {
    platformsApi.getInstagramConnection.mockResolvedValue(connection("instagram", false));
    queueApi.assignNextOpenSlot.mockResolvedValue(slot([]));
    renderWithProviders(<QueuePage />);
    await userEvent.setup().click(await assignButton());
    await waitFor(() => expect(queueApi.assignNextOpenSlot).toHaveBeenCalledWith("real-token", 10));
    expect(screen.queryByRole("group", { name: "Publish to" })).not.toBeInTheDocument();
  });

  it("lets the user send a video to Instagram only", async () => {
    queueApi.assignNextOpenSlot.mockResolvedValue(slot([]));
    renderWithProviders(<QueuePage />);
    const picker = within(await screen.findByRole("group", { name: "Publish to" }));
    const user = userEvent.setup();
    expect(picker.getByRole("checkbox", { name: "TikTok" })).toBeChecked();  // default unchanged
    await user.click(picker.getByRole("checkbox", { name: "TikTok" }));
    await user.click(picker.getByRole("checkbox", { name: "Instagram" }));
    await user.click(await assignButton());
    await waitFor(() => expect(queueApi.assignNextOpenSlot).toHaveBeenCalledWith("real-token", 10, ["instagram"]));
  });

  it("blocks assigning when no platform is chosen", async () => {
    renderWithProviders(<QueuePage />);
    const picker = within(await screen.findByRole("group", { name: "Publish to" }));
    await userEvent.setup().click(picker.getByRole("checkbox", { name: "TikTok" }));
    expect(await screen.findByText("Choose at least one platform.")).toBeInTheDocument();
    expect(await assignButton()).toBeDisabled();
  });
});

describe("per-platform delivery", () => {
  it("shows TikTok and Instagram separately, with Instagram's processing step", async () => {
    queueApi.listQueueSlots.mockResolvedValue({
      slots: [slot([
        publication("tiktok", "PUBLISHED", { platform_post_status: "PUBLISHED" }),
        publication("instagram", "PUBLISHING", { stage: "PROCESSING", message: "Instagram is processing the video…" }),
      ])],
    });
    renderWithProviders(<QueuePage />);
    const deliveries = within(await screen.findByRole("list", { name: "Delivery by platform" }));
    const [tiktokRow, instagramRow] = deliveries.getAllByRole("listitem");
    expect(within(tiktokRow).getByText("TikTok")).toBeInTheDocument();
    expect(within(tiktokRow).getByText("Published")).toBeInTheDocument();
    expect(within(instagramRow).getByText("Processing")).toBeInTheDocument();
    expect(within(instagramRow).getByText("Instagram is processing the video…")).toBeInTheDocument();
  });

  it("keeps a TikTok-only slot looking exactly as before", async () => {
    queueApi.listQueueSlots.mockResolvedValue({ slots: [{ ...slot([publication("tiktok", "SCHEDULED")], "SCHEDULED"), message: null }] });
    renderWithProviders(<QueuePage />);
    await screen.findByText("reel.mp4");
    expect(screen.queryByRole("list", { name: "Delivery by platform" })).not.toBeInTheDocument();
  });
});

describe("presentDelivery", () => {
  it("labels each delivery state", () => {
    expect(presentDelivery({ display_status: "SCHEDULED", platform_post_status: "PENDING" }).label).toBe("Scheduled");
    expect(presentDelivery({ display_status: "PUBLISHING", platform_post_status: "PUBLISHING", stage: "PROCESSING" }).label).toBe("Processing");
    expect(presentDelivery({ display_status: "PUBLISHING", platform_post_status: "PUBLISHING", stage: "PUBLISHING" }).label).toBe("Publishing");
    expect(presentDelivery({ display_status: "PUBLISHED", platform_post_status: "PUBLISHED" }).label).toBe("Published");
    expect(presentDelivery({ display_status: "FAILED", platform_post_status: "FAILED" }).label).toBe("Failed");
    expect(presentDelivery({ display_status: "NEEDS_ATTENTION", platform_post_status: "UNKNOWN" }).label).toBe("Unknown");
  });
});
