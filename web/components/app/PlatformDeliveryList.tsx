/**
 * PlatformDeliveryList — one row per platform a slot's video is going to
 * (Milestone 4.2), so TikTok and Instagram delivery read separately:
 * "Instagram · Processing — Instagram is processing the video…".
 *
 * Props:
 *   publications — the slot's per-platform states from GET /api/queue/slots
 *                  (QueueSlotResponse.publications), labelled through
 *                  lib/status.ts's presentDelivery.
 */

import { Badge } from "@/components/ui/Badge";
import type { PublicationStatus } from "@/lib/api/types";
import { presentDelivery } from "@/lib/status";

const PLATFORM_LABELS: Record<string, string> = { tiktok: "TikTok", instagram: "Instagram" };

export function PlatformDeliveryList({ publications }: { publications: PublicationStatus[] }) {
  return (
    <ul className="flex flex-col gap-1.5" aria-label="Delivery by platform">
      {publications.map((publication) => {
        const { label, tone } = presentDelivery(publication);
        return (
          <li key={publication.platform} className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
            <span className="font-medium text-ink">{PLATFORM_LABELS[publication.platform] ?? publication.platform}</span>
            <Badge tone={tone}>{label}</Badge>
            {publication.message ? <span>{publication.message}</span> : null}
          </li>
        );
      })}
    </ul>
  );
}
