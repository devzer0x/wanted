import type { PredictionDistribution } from "@/lib/prediction/types";

/** Participation share for one outcome, 0–100, or null when there is nothing to show yet.
 *  Derived only from the public `prediction_distribution` aggregate (CONTRACTS-PREDICTIONS §2) —
 *  never from raw entry rows, which stay server-only. */
export function pctOf(distribution: PredictionDistribution | undefined, key: string): number | null {
  if (!distribution || distribution.total === 0) return null;
  return Math.round(((distribution.counts[key] ?? 0) / distribution.total) * 100);
}
