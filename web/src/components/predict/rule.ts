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

/**
 * The "recent odds" pill, for a scheduled round.
 *
 * An always-available question is only asked while its YES-rate, re-measured by the harness from
 * the agent's own recent telemetry, is fair (CONTRACTS-PREDICTIONS §3, "Cadence and entry windows"),
 * and the measurement that justified asking rides on the row as `state_context.calibration`
 * (`{yes, n, window_s, history_s}`). This prints exactly that and nothing derived from it beyond
 * the percentage: the odds a viewer is shown are the odds that were measured. Any other shape —
 * every situational question, which carries no calibration — renders no pill.
 */
export function measuredOdds(prediction: Prediction): { text: string; title: string } | null {
  const calibration = prediction.state_context?.calibration;
  if (typeof calibration !== "object" || calibration === null) return null;
  const { yes, n, history_s } = calibration as { yes?: unknown; n?: unknown; history_s?: unknown };
  if (typeof yes !== "number" || typeof n !== "number") return null;
  if (!Number.isInteger(yes) || !Number.isInteger(n) || n <= 0 || yes < 0 || yes > n) return null;

  const hours = typeof history_s === "number" && history_s > 0 ? Math.round(history_s / 360) / 10 : null;
  return {
    text: `Recent odds: YES ${Math.round((yes / n) * 100)}% (${yes}/${n})`,
    title:
      `Measured, not estimated: YES in ${yes} of the last ${n} windows of this exact length` +
      (hours ? `, over about the last ${hours} h of play.` : "."),
  };
}
