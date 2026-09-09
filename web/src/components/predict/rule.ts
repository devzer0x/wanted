import type { Prediction } from "@/lib/prediction/types";

/**
 * The "how this settles" pill.
 *
 * The design shows the machine rule — "Rule: wanted_clears within 120s". The settlement rule
 * itself (`predictions.telemetry_rule`, a jsonb column per CONTRACTS-PREDICTIONS §2/§3) is NOT
 * in `Prediction`, the frozen shared type, and `GET /api/predictions/live` does not select it, so
 * this builds the pill from the two real fields that ARE on the payload and stops there:
 *
 *   - `prediction_type`, the catalogue key the harness generated the row from — the thing that
 *     picks the telemetry rule (harness/wasted_harness/predictions/catalog.py), and
 *   - `resolves_at - locks_at`, the real gap between the lock and the settlement pass.
 *
 * Nothing is inferred about the rule's kind or parameters. If either timestamp is missing or the
 * window is not positive, the pill simply does not render.
 */
export function settlementRule(prediction: Prediction): { text: string; title: string } | null {
  const type = prediction.prediction_type;
  if (!type) return null;

  const locks = new Date(prediction.locks_at).getTime();
  const resolves = new Date(prediction.resolves_at).getTime();
  if (!Number.isFinite(locks) || !Number.isFinite(resolves) || resolves <= locks) return null;

  const seconds = Math.round((resolves - locks) / 1000);
  return {
    text: `Rule: ${type} · settles ${seconds}s after lock`,
    title: `Settled from the game's own telemetry for "${type}", ${seconds} seconds after entries lock.`,
  };
}
