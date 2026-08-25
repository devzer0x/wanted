import type { StatsRow } from "@/lib/types";

// The ONLY offline signal (CONTRACTS §5): stats.heartbeat_at stale > 60 s or absent.
// The site never infers offline from feed silence or socket state.
export const HEARTBEAT_STALE_MS = 60_000;

export function isAgentOffline(stats: StatsRow | null, nowMs: number): boolean {
  if (!stats?.heartbeat_at) return true;
  const beat = new Date(stats.heartbeat_at).getTime();
  if (Number.isNaN(beat)) return true;
  return nowMs - beat > HEARTBEAT_STALE_MS;
}
