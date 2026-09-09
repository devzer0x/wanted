import type { MyEntry, Prediction } from "@/lib/prediction/types";

export interface ViewerRecord {
  correct: number;
  total: number;
  /** Base-unit string. Summed with BigInt — never Number(), which loses precision above 2^53. */
  earned: string;
  asset: string;
}

/**
 * The viewer's own record across the settled predictions THIS RESPONSE contains — nothing wider.
 *
 * `GET /api/predictions/live` returns recently settled rows only (a bounded window, capped at 20
 * rows), so this is deliberately not "today" and not "all time": it is the record for the results
 * shown on the page, and the pill that renders it says so. Returns null when the viewer has no
 * settled entry among them, so the pills disappear rather than showing a zero that reads like a
 * score.
 */
export function viewerRecord(
  resolved: Prediction[],
  mine: Record<string, MyEntry>
): ViewerRecord | null {
  let correct = 0;
  let total = 0;
  let earned = BigInt(0);
  let asset = "";

  for (const prediction of resolved) {
    if (prediction.status !== "settled") continue;
    const entry = mine[prediction.id];
    if (!entry) continue;

    total += 1;
    if (entry.correct === true) correct += 1;
    if (!asset && prediction.reward_asset) asset = prediction.reward_asset;
    // Only add up amounts denominated in the one asset the pill will name.
    if (prediction.reward_asset === asset && /^\d+$/.test(entry.reward)) {
      earned += BigInt(entry.reward);
    }
  }

  if (total === 0) return null;
  return { correct, total, earned: earned.toString(), asset };
}
