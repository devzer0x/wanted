// POST /api/rewards/claim — CONTRACTS-PREDICTIONS.md §10.5: ENQUEUE ONLY.
//
// This route does one committed database call and returns 202. It never signs, never broadcasts and
// never reads the chain: the key is reachable only from the payout worker (lib/rewards/payoutWorker.ts,
// run by /api/cron/tick under the payout lease), which picks the queued claim up within a tick or two.
// That is what closes the request-time kill windows (FM-02/03/04): there is no longer anything to
// lose between "the claim committed" and "the response arrived". A response lost after the commit is
// now harmless — the row is queued and will be paid; the panel shows it on the next load.
//
// Order of operations:
//   1. auth (session cookie) + rate limit
//   2. reward asset configured (its symbol pins which credits this claim may take — FM-14/16)
//   3. assessEligibility() with the Vercel geo country and the SIGNED session's rules version
//      (FM-06) — the same §5 gate as crediting, re-checked at claim time
//   4. rewardLimits() — CLAIM_MIN_AMOUNT / CLAIM_MAX_AMOUNT are REQUIRED (§10.6); unset or malformed
//      refuses the claim rather than meaning "no limit"
//   5. create_reward_claim(wallet, asset.symbol, min, max) — one transaction: payouts switch (P0005),
//      policy_flags.blocked (P0006), lock this wallet's unclaimed credits for this asset, take the
//      longest prefix under the max, insert the queued claim, attach the credits by id and prove the
//      attached sum equals the claim amount.
//
// The amount is never computed or even parsed here: a client-supplied amount or ledger id list is not
// accepted, and the server-computed one is decided inside the locking transaction.
//
// Errors are fixed strings (§10.1 principle 7). Postgres and parse text never reaches the response;
// the SQLSTATE alone is logged server-side.

import { readSession } from "@/lib/auth/session";
import { assessEligibility } from "@/lib/policy";
import { rewardLimits } from "@/lib/rewards/strategies";
import { rewardAsset } from "@/lib/chain/assets";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { getClientCountry, getClientIp, jsonError, jsonOk, notConfigured } from "../../_lib/http";
import { rateLimit, RATE_LIMITS } from "../../_lib/rateLimit";
import { fromBaseUnits } from "../../_lib/amount";

export const dynamic = "force-dynamic";
export const maxDuration = 15;

function sqlState(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code;
  return typeof code === "string" && /^[0-9A-Z]{5}$/.test(code) ? code : "unknown";
}

export async function POST(request: Request): Promise<Response> {
  const session = await readSession();
  if (!session?.address) return jsonError(401, "not authenticated");
  const wallet = session.address;

  const ip = getClientIp(request);
  const limited = rateLimit(`claim:${wallet}:${ip}`, RATE_LIMITS.claim);
  if (!limited.allowed) {
    return jsonError(429, "too many requests", { retryAfterMs: limited.retryAfterMs });
  }

  const asset = rewardAsset();
  if (!asset) return notConfigured("reward asset");

  const policy = await assessEligibility({
    address: wallet,
    country: getClientCountry(request),
    rulesVersion: session.rulesVersion,
  });
  if (!policy.eligible) {
    return jsonError(403, "not eligible to claim", { reasons: policy.reasons });
  }

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  let limits;
  try {
    limits = rewardLimits();
  } catch {
    console.error("rewards/claim: reward_limits_misconfigured");
    return jsonError(500, "reward limits are misconfigured");
  }

  // Limits travel as WHOLE token units in JSON strings (the numeric columns' unit); PostgREST hands
  // a JSON string to a numeric parameter exactly, where a JSON number would pass through a double.
  const { data: claimRow, error: claimErr } = await admin
    .rpc("create_reward_claim", {
      p_wallet: wallet,
      p_asset: asset.symbol,
      p_min_amount: fromBaseUnits(limits.minClaim, asset.decimals),
      p_max_amount: fromBaseUnits(limits.maxClaim, asset.decimals),
    })
    .single<{ id: string; wallet: string; amount_text: string; asset: string; status: string }>();

  if (claimErr) {
    const code = sqlState(claimErr);
    // SQLSTATEs raised deliberately by create_reward_claim (§10.3), mapped per §10.5.
    if (code === "23505") return jsonError(409, "a claim is already in progress for this wallet");
    if (code === "P0002") return jsonError(400, "nothing to claim");
    if (code === "P0003") {
      return jsonError(400, "claimable balance is below the minimum claim amount", {
        minClaim: limits.minClaim.toString(),
      });
    }
    if (code === "P0004") {
      return jsonError(409, "a single credit is larger than the maximum claim amount");
    }
    if (code === "P0005") return jsonError(503, "payouts are paused");
    if (code === "P0006") return jsonError(403, "this wallet is not eligible to claim");
    console.error(`rewards/claim: create_reward_claim ${code}`);
    return jsonError(500, "failed to create claim");
  }
  if (!claimRow || typeof claimRow.id !== "string") {
    console.error("rewards/claim: create_reward_claim returned no row");
    return jsonError(500, "failed to create claim");
  }

  return jsonOk({ claimId: claimRow.id, status: "queued" }, 202);
}
