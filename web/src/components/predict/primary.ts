import type { Prediction } from "@/lib/prediction/types";

/**
 * The one prediction the home page's single LIVE PREDICTION slot shows: the open prediction
 * closest to locking, or — if none are open — the locked/resolving one closest to resolving.
 * The API does not promise an order on `live` (CONTRACTS-PREDICTIONS §4), so this is a
 * deterministic pick over whatever it returns, never an invented one.
 */
export function pickPrimary(live: Prediction[]): Prediction | undefined {
  const open = live
    .filter((p) => p.status === "open")
    .sort((a, b) => new Date(a.locks_at).getTime() - new Date(b.locks_at).getTime());
  if (open.length > 0) return open[0];

  const inFlight = live
    .filter((p) => p.status === "locked" || p.status === "resolving")
    .sort((a, b) => new Date(a.resolves_at).getTime() - new Date(b.resolves_at).getTime());
  return inFlight[0];
}
