// GET /api/predictions/live — CONTRACTS-PREDICTIONS.md §4.
// -> open + locked + recently settled predictions, honest `session_live`, and (when a wallet
// session cookie is attached) the viewer's own entries.
//
// Security property #7: `session_live` is computed from `sessions`/`stats` (both existing tables,
// unaffected by this task) using this server's own clock (a Vercel Node Function's Date.now(),
// not anything client-supplied) — never invented, and the route must never fabricate a
// prediction. If there is no session with `ended_at is null` whose `stats.heartbeat_at` is within
// 90s, `session_live` is false and `live`/`resolved` simply reflect whatever `predictions` rows
// really exist (which today, verified below, is none — the table doesn't exist yet).
//
// UNVERIFIED AGAINST A REAL SCHEMA (flagged in the task report — infra migration not applied as
// of 2026-09-08, confirmed via `GET /rest/v1/predictions` -> PGRST205):
//   - `predictions` row shape here matches CONTRACTS-PREDICTIONS.md §2's column table exactly,
//     EXCEPT `settled_at`: the shared type `Prediction` (web/src/lib/prediction/types.ts, frozen,
//     read-only) declares `settled_at: string | null`, but §2's column table for `predictions`
//     does not list a `settled_at` column at all. This route reads it defensively
//     (`row.settled_at ?? null`) so it compiles and degrades to `null` rather than inventing a
//     value if the column turns out not to exist — see the report for the exact question this
//     raises for the orchestrator/infra workstream.
//   - `prediction_distribution` is documented only as "a security-definer view" with no column
//     list. This route assumes the standard shape for that kind of aggregate,
//     `(prediction_id, outcome, count)`, one row per outcome, and pivots it into the nested
//     `{counts, total}` shape `PredictionDistribution` requires. If the real view instead returns
//     one row per prediction with a jsonb `counts` column, only the mapping block below needs to
//     change.
//   - `prediction_entries`/`reward_ledger` column names for the `mine` section are inferred from
//     CONTRACTS-PREDICTIONS.md §2's prose the same way `wallet_sessions` is in the auth routes.

import { readSession } from "@/lib/auth/session";
import { HEARTBEAT_STALE_MS } from "@/lib/offline";
import { rewardAsset } from "@/lib/chain/assets";
import { toBaseUnits } from "../../_lib/amount";
import type {
  LivePredictionsResponse,
  MyEntry,
  Prediction,
  PredictionDistribution,
} from "@/lib/prediction/types";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { jsonOk, notConfigured, internalError } from "../../_lib/http";

export const dynamic = "force-dynamic";

// Deliberately the SAME constant the page's offline banner uses, imported rather than redeclared.
// It was 90_000 here while `offline.ts` used 60_000, so between 60 s and 90 s of staleness the
// banner said OFF AIR while this route still reported session_live — the viewer saw "the agent is
// not on the air" directly above a live prediction card. CONTRACTS.md §5 fixes the threshold at
// 60 s, and one exported constant is what keeps both consumers honest to it.
const HEARTBEAT_LIVE_WINDOW_MS = HEARTBEAT_STALE_MS;
// How far into the future a heartbeat may be stamped before we stop trusting it. Generous enough
// to absorb normal NTP drift between the game server and wherever this runs; far short of the kind
// of gap that means a clock is actually wrong.
const CLOCK_SKEW_TOLERANCE_MS = 120_000;
// "Recently settled" has no numeric definition in CONTRACTS-PREDICTIONS.md §4 — judgment call,
// documented in the task report: last 2 hours by `resolves_at`, capped at 20 rows.
const RECENTLY_SETTLED_WINDOW_MS = 2 * 60 * 60 * 1000;
const RECENTLY_SETTLED_LIMIT = 20;

// The database stores amounts in WHOLE token units (`numeric(38,18)`); `Prediction.reward_pool` is
// declared as a base-unit string because that is what the UI formats against, the same convention
// as `RewardBalance.claimable`. Passing the raw column through is a 10^18 error in the readable
// direction: a real 10 TTWO pool rendered as "Pool: 0 TTWO", which is a wrong number in front of
// viewers deciding whether an answer is worth giving. Convert at the boundary, like everywhere else.
function toPrediction(row: Record<string, unknown>, decimals: number): Prediction {
  return {
    id: row.id as string,
    session_id: row.session_id as string,
    question: row.question as string,
    prediction_type: row.prediction_type as string,
    state_context: (row.state_context as Record<string, unknown> | null) ?? null,
    opened_at: row.opened_at as string,
    locks_at: row.locks_at as string,
    resolves_at: row.resolves_at as string,
    outcomes: (row.outcomes as Prediction["outcomes"]) ?? [],
    status: row.status as Prediction["status"],
    result: (row.result as string | null) ?? null,
    reward_pool: toBaseUnits(String(row.reward_pool ?? "0"), decimals).toString(),
    reward_asset: (row.reward_asset as string) ?? "",
    entry_count: Number(row.entry_count ?? 0),
    correct_count: Number(row.correct_count ?? 0),
    is_event: Boolean(row.is_event),
    settled_at: (row.settled_at as string | null | undefined) ?? null,
  };
}

export async function GET(): Promise<Response> {
  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  // --- session_live: existing tables, honest, no invention ------------------------------------
  const { data: openSession, error: sessionErr } = await admin
    .from("sessions")
    .select("id")
    .is("ended_at", null)
    .order("started_at", { ascending: false })
    .limit(1)
    .maybeSingle<{ id: string }>();
  if (sessionErr) {
    return internalError("predictions/live: failed to read sessions", sessionErr, "failed to read sessions");
  }

  let sessionLive = false;
  if (openSession) {
    const { data: statsRow, error: statsErr } = await admin
      .from("stats")
      .select("heartbeat_at")
      .eq("session_id", openSession.id)
      .maybeSingle<{ heartbeat_at: string }>();
    if (statsErr) {
      return internalError("predictions/live: failed to read stats", statsErr, "failed to read stats");
    }
    if (statsRow?.heartbeat_at) {
      const age = Date.now() - new Date(statsRow.heartbeat_at).getTime();
      // A NEGATIVE age means the heartbeat is stamped slightly in this machine's future, which is
      // ordinary clock skew, not a fault: the harness writes `heartbeat_at` from the game server
      // and this code runs somewhere else entirely, so a second of NTP drift between them is
      // routine. Requiring `age >= 0` made the site report OFF AIR while the agent was playing,
      // intermittently, for no reason a viewer or an operator could see. Reproduced here with a
      // container clock only 0.14s ahead of the host.
      //
      // A heartbeat far in the future is still refused — that is a broken clock or a bad write,
      // and treating it as live would keep the site claiming the agent is on the air indefinitely.
      sessionLive = age <= HEARTBEAT_LIVE_WINDOW_MS && age >= -CLOCK_SKEW_TOLERANCE_MS;
    }
  }

  // --- predictions ------------------------------------------------------------------------------
  const { data: liveRows, error: liveErr } = await admin
    .from("predictions")
    .select("*")
    .in("status", ["open", "locked"])
    .order("opened_at", { ascending: false });
  if (liveErr) {
    return internalError("predictions/live: failed to read predictions", liveErr, "failed to read predictions");
  }

  const since = new Date(Date.now() - RECENTLY_SETTLED_WINDOW_MS).toISOString();
  const { data: resolvedRows, error: resolvedErr } = await admin
    .from("predictions")
    .select("*")
    .in("status", ["settled", "void"])
    .gte("resolves_at", since)
    .order("resolves_at", { ascending: false })
    .limit(RECENTLY_SETTLED_LIMIT);
  if (resolvedErr) {
    return internalError("predictions/live: failed to read resolved predictions", resolvedErr, "failed to read resolved predictions");
  }

  // Amount scale comes from the configured reward asset, never a hardcoded 18.
  const decimals = rewardAsset()?.decimals ?? 18;
  const live = (liveRows ?? []).map((r) => toPrediction(r, decimals));
  const resolved = (resolvedRows ?? []).map((r) => toPrediction(r, decimals));
  const allIds = [...live, ...resolved].map((p) => p.id);

  // --- distributions (public aggregate only, via the security-definer view) --------------------
  const distributions: Record<string, PredictionDistribution> = {};
  if (allIds.length > 0) {
    const { data: distRows, error: distErr } = await admin
      .from("prediction_distribution")
      .select("*")
      .in("prediction_id", allIds);
    if (distErr) {
      return internalError("predictions/live: failed to read prediction_distribution", distErr, "failed to read prediction_distribution");
    }
    for (const row of (distRows ?? []) as Record<string, unknown>[]) {
      const predictionId = row.prediction_id as string;
      const entry =
        distributions[predictionId] ?? { prediction_id: predictionId, counts: {}, total: 0 };
      if (typeof row.counts === "object" && row.counts !== null) {
        // View already returns the nested shape.
        Object.assign(entry.counts, row.counts as Record<string, number>);
        entry.total = Number(row.total ?? Object.values(entry.counts).reduce((a, b) => a + b, 0));
      } else if (typeof row.outcome === "string") {
        // View returns one row per outcome; pivot here.
        const count = Number(row.count ?? 0);
        entry.counts[row.outcome] = count;
        entry.total += count;
      }
      distributions[predictionId] = entry;
    }
  }

  // --- mine: only when a verified wallet session cookie is attached ----------------------------
  const mine: Record<string, MyEntry> = {};
  const session = await readSession();
  if (session?.address && allIds.length > 0) {
    const wallet = session.address;
    const [{ data: entryRows, error: entryErr }, { data: ledgerRows, error: ledgerErr }] =
      await Promise.all([
        admin
          .from("prediction_entries")
          .select("prediction_id, outcome, created_at")
          .eq("wallet", wallet)
          .in("prediction_id", allIds),
        admin
          .from("reward_ledger")
          // `amount::text`, never the bare column: `numeric(38,18)` can arrive as an
          // unquoted JSON number and round-trip through a JS double (FM-08 — a small
          // `even_split` share prints back as "5e-7"). Same cast as
          // `/api/rewards/balance` and `/api/rewards/claims`.
          .select("prediction_id, amount:amount::text")
          .eq("wallet", wallet)
          .in("prediction_id", allIds),
      ]);
    if (entryErr) {
      return internalError("predictions/live: failed to read prediction_entries", entryErr, "failed to read prediction_entries");
    }
    if (ledgerErr) {
      return internalError("predictions/live: failed to read reward_ledger", ledgerErr, "failed to read reward_ledger");
    }
    // `MyEntry.reward` is a BASE-UNIT string (lib/prediction/types.ts), but the ledger
    // stores WHOLE token units — the same convention `toPrediction` converts for
    // `reward_pool` twenty lines above. Passing the column straight through shipped a
    // whole-unit decimal like "0.02" into a field the UI only trusts as an integer:
    // `hasBaseUnits()` rejects anything with a ".", so `LivePredictionCard` and
    // `ResolvedPredictionCard` rendered "no reward was credited" to a viewer who HAD
    // been credited, and `summary.ts::viewerRecord` dropped the amount from their
    // earnings tally. Convert at the boundary, like everywhere else.
    const rewardByPrediction = new Map<string, string>();
    for (const row of (ledgerRows ?? []) as { prediction_id: string; amount: string | number }[]) {
      rewardByPrediction.set(
        row.prediction_id,
        toBaseUnits(String(row.amount ?? "0"), decimals).toString(),
      );
    }
    const byId = new Map(live.concat(resolved).map((p) => [p.id, p] as const));
    for (const row of (entryRows ?? []) as {
      prediction_id: string;
      outcome: string;
      created_at: string;
    }[]) {
      const prediction = byId.get(row.prediction_id);
      const settled = prediction?.status === "settled";
      mine[row.prediction_id] = {
        prediction_id: row.prediction_id,
        outcome: row.outcome,
        created_at: row.created_at,
        correct: settled && prediction ? prediction.result === row.outcome : null,
        reward: rewardByPrediction.get(row.prediction_id) ?? "0",
      };
    }
  }

  const body: LivePredictionsResponse = { live, resolved, distributions, mine, session_live: sessionLive };
  return jsonOk(body);
}
