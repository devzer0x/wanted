"use client";

import { formatAge } from "@/lib/format";
import { isAgentOffline } from "@/lib/offline";
import type { StatsRow } from "@/lib/types";

export function OfflineBanner({
  stats,
  nowMs,
  mounted,
  linkDown,
}: {
  stats: StatsRow | null;
  nowMs: number;
  /** Relative ages depend on the client clock; suppressed until after hydration. */
  mounted: boolean;
  linkDown: boolean;
}) {
  if (!isAgentOffline(stats, nowMs)) return null;

  let detail: string;
  if (linkDown) {
    detail = "Data link down — the telemetry database is unreachable from here.";
  } else if (!stats?.heartbeat_at) {
    detail = "No heartbeat on record.";
  } else if (mounted) {
    detail = `Last heartbeat ${formatAge(stats.heartbeat_at, nowMs)}.`;
  } else {
    detail = "Heartbeat is stale.";
  }

  return (
    <div
      role="status"
      data-testid="offline-banner"
      className="banner-throb panel relative overflow-hidden border-blood"
    >
      <div className="stripes h-1.5 w-full" aria-hidden="true" />
      <div className="flex flex-col gap-1 px-4 py-3 sm:flex-row sm:items-baseline sm:gap-4">
        <span className="wordmark text-2xl text-ember" data-text="OFFLINE">
          OFFLINE
        </span>
        <p className="text-xs text-smoke">
          the agent is not on the air right now. {detail}
        </p>
      </div>
    </div>
  );
}
