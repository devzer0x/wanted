// GET /api/leaderboard?window=today|week|all — CONTRACTS-PREDICTIONS.md §4.
//
// Delegates entirely to the `leaderboard(p_window)` SQL function named in the task brief — no
// ranking/accuracy/streak math is reimplemented here.

import type { LeaderboardRow, LeaderboardWindow } from "@/lib/prediction/types";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured } from "../_lib/http";
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

  const { data, error } = await admin.rpc("leaderboard", { p_window: windowParam });
  if (error) {
    return jsonError(500, "leaderboard failed", { detail: error.message });
  }

  // `leaderboard()` returns `earned` as a Postgres numeric in WHOLE token units, which PostgREST
  // hands back as a JSON number. `LeaderboardRow.earned` is a base-unit STRING for two reasons: the
  // UI formats every amount the same way, and a JS number cannot hold 1e18 without loss — a wallet
  // that earned a large amount would be shown a rounded figure on a public ranking. Convert here,
  // at the boundary, using the configured asset's scale rather than an assumed 18.
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
