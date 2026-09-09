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

  // With the database unreachable we do not know what the agent is doing — claiming it is off
  // the air would be a guess. The two cases get different headlines for that reason.
  if (linkDown) {
    return (
      <Banner state="no-data" headline="NO DATA">
        We can&apos;t reach the telemetry database, so we can&apos;t tell you what the agent is
        doing right now. We would rather say that than guess.
      </Banner>
    );
  }

  let detail: string;
  if (!stats?.heartbeat_at) {
    detail = "No heartbeat on record yet.";
  } else if (mounted) {
    detail = `Last heartbeat ${formatAge(stats.heartbeat_at, nowMs)}.`;
  } else {
    detail = "The heartbeat is stale.";
  }

  return (
    <Banner state="offline" headline="OFF AIR">
      The agent is not on the air right now. {detail} Nothing on this page is a replay.
    </Banner>
  );
}

function Banner({
  state,
  headline,
  children,
}: {
  state: "offline" | "no-data";
  headline: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role="status"
      data-testid="offline-banner"
      data-state={state}
      className="banner-throb panel relative overflow-hidden"
    >
      <div className="stripes h-2 w-full" aria-hidden="true" />
      <div className="flex flex-col gap-2 px-4 py-3.5 sm:flex-row sm:items-center sm:gap-4">
        <span className="wordmark shrink-0 self-start text-2xl">{headline}</span>
        <p className="text-[0.82rem] leading-relaxed text-dim">{children}</p>
      </div>
    </div>
  );
}
