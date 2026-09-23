// GET /api/leaderboard?window=today|week|all — CONTRACTS-PREDICTIONS.md §4.
//
// Delegates entirely to the `leaderboard(p_window)` SQL function named in the task brief — no
// ranking/accuracy/streak math is reimplemented here.

import type { LeaderboardRow, LeaderboardWindow } from "@/lib/prediction/types";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured, internalError } from "../_lib/http";
import { rewardAsset } from "@/lib/chain/assets";
import { toBaseUnits } from "../_lib/amount";

export const dynamic = "force-dynamic";

const VALID_WINDOWS: LeaderboardWindow[] = ["today", "week", "all"];

export async function GET(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const windowParam = url.searchParams.get("window") ?? "all";
  if (!VALID_WINDOWS.includes(windowParam as LeaderboardWindow)) {
    return jsonError(400, `window must be one of: ${VALID_WINDOWS.join(", ")}`);
  }

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  // `leaderboard()` returns `earned` as a Postgres `numeric` in WHOLE token units. Left uncast,
  // PostgREST serialises that as a bare JSON number: postgrest-js's JSON.parse turns anything
  // below ~1e-6 into exponential notation ("5e-7", not "0.0000005") and anything past ~17
  // significant digits into a rounded double — neither of which `toBaseUnits` can read as the
  // exact decimal it is. `earned::text` (PostgREST supports a "::cast" on any selected column,
  // including from an RPC that returns a table) makes PostgREST emit the exact decimal as a JSON
  // STRING instead, which JSON.parse leaves untouched. Every other column is selected explicitly
  // alongside it because naming one column in `select` on an RPC restricts the response to the
  // named set (proved locally against a real postgrest/postgrest:v12.2.3 — see PR notes).
  const { data, error } = await admin
    .rpc("leaderboard", { p_window: windowParam })
    .select("rank, wallet, accuracy, correct, total, current_streak, best_streak, earned:earned::text");
  if (error) {
    return internalError("leaderboard: leaderboard failed", error, "leaderboard failed");
  }

  // `LeaderboardRow.earned` is a base-unit STRING for two reasons: the UI formats every amount the
  // same way, and a JS number cannot hold 1e18 without loss — a wallet that earned a large amount
  // would be shown a rounded figure on a public ranking. Convert here, at the boundary, using the
  // configured asset's scale rather than an assumed 18. `r.earned` is already an exact string
  // (the cast above), never a JS number, so String() below is just a type narrowing, not a
  // round trip through double precision.
  const decimals = rewardAsset()?.decimals ?? 18;
  const rows: LeaderboardRow[] = ((data ?? []) as Record<string, unknown>[]).map((r) => ({
    rank: Number(r.rank ?? 0),
    wallet: String(r.wallet ?? ""),
    accuracy: Number(r.accuracy ?? 0),
    correct: Number(r.correct ?? 0),
    total: Number(r.total ?? 0),
    current_streak: Number(r.current_streak ?? 0),
    best_streak: Number(r.best_streak ?? 0),
    earned: toBaseUnits(String(r.earned ?? "0"), decimals).toString(),
  }));

  return jsonOk({ window: windowParam, rows });
}
