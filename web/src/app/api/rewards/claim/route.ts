// POST /api/rewards/claim — CONTRACTS-PREDICTIONS.md §4 + the task brief's security property #3.
//
// Order of operations, and why:
//   1. auth (session cookie) + rate limit
//   2. isRewardAssetConfigured() / isTreasuryConfigured() — checked BEFORE touching the ledger,
//      so a misconfigured treasury never creates a claim row it cannot fulfil.
//   3. assessEligibility() re-checked here (not just at credit time — CONTRACTS-PREDICTIONS §5:
//      "Called before any ledger credit and again before any claim").
//   4. claimable read fresh from `reward_ledger` server-side (never trusts a client-supplied
//      amount), clamped to rewardLimits().maxClaim, rejected below rewardLimits().minClaim.
//   5. INSERT the `reward_claims` row, THEN UPDATE the covered `reward_ledger` rows.
//   6. Only after (5) succeeds: call `sendReward` (the on-chain call).
//
// ATOMICITY — resolved by a SQL function, not by ordering.
//
// This route only reaches Postgres through PostgREST, which issues one statement per call and
// cannot hold a transaction open. "Create the claim row" and "mark the ledger rows as belonging to
// it" therefore cannot be two statements from here: a crash between them would leave a `pending`
// claim owning no ledger rows, while the balance it was meant to consume stayed claimable.
//
// Both writes now happen inside `public.create_reward_claim(...)`
// (infra/supabase/migrations/20260908120002_reward_claim_rpc.sql), which is a single statement to
// PostgREST and so a single transaction to Postgres. That function also re-reads and sums the
// wallet's unclaimed credits under a row lock, which is why this route no longer computes an
// amount at all — a client-supplied ledger id list is a client-supplied amount by another name,
// and even a server-computed one can go stale between the read and the write.
//
// Double-claim protection is unchanged and still enforced by the database: the partial unique
// index `UNIQUE (wallet) WHERE status IN ('pending','submitted')` means a concurrent or replayed
// claim fails with 23505, which this route maps to a real 409.
//
// Failure of the on-chain send is finalised through `finalize_reward_claim(...)`, which returns
// the credits to the claimable pool in the same transaction that marks the claim failed — so a
// failed transfer never burns a viewer's rewards.

import { readSession } from "@/lib/auth/session";
import { assessEligibility } from "@/lib/policy";
import { rewardLimits } from "@/lib/rewards/strategies";
import { isTreasuryConfigured, sendReward } from "@/lib/rewards/treasury";
import { rewardAsset, isRewardAssetConfigured } from "@/lib/chain/assets";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured, getClientIp } from "../../_lib/http";
import { rateLimit, RATE_LIMITS } from "../../_lib/rateLimit";
import { toBaseUnits, fromBaseUnits } from "../../_lib/amount";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  const session = await readSession();
  if (!session?.address) return jsonError(401, "not authenticated");
  const wallet = session.address;

  const ip = getClientIp(request);
  const limited = rateLimit(`claim:${wallet}:${ip}`, RATE_LIMITS.claim);
  if (!limited.allowed) {
    return jsonError(429, "too many requests", { retryAfterMs: limited.retryAfterMs });
  }

  if (!isRewardAssetConfigured()) return notConfigured("reward asset");
  if (!isTreasuryConfigured()) return notConfigured("treasury");

  const policy = await assessEligibility({ address: wallet });
  if (!policy.eligible) {
    return jsonError(403, "not eligible to claim", { reasons: policy.reasons });
  }

  // isRewardAssetConfigured() already passed, so this is non-null; typed narrowing for callers.
  const asset = rewardAsset();
  if (!asset) return notConfigured("reward asset");

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  // Throws on a present-but-unparseable CLAIM_MIN_AMOUNT / CLAIM_MAX_AMOUNT rather than treating
  // the rail as absent. Refusing the claim is the correct response to a treasury ceiling nobody
  // can read: the alternative is paying out with no ceiling at all.
  let limits;
  try {
    limits = rewardLimits();
  } catch (err) {
    return jsonError(500, "reward limits are misconfigured", {
      detail: err instanceof Error ? err.message : String(err),
    });
  }

  // The amount is NOT computed here. `create_reward_claim` locks this wallet's unclaimed ledger
  // rows, sums them, enforces the §6 min/max, inserts the claim and marks the rows — all in one
  // transaction. The limits travel as whole token units because that is what the numeric columns
  // and the operator-facing caps are denominated in.
  const { data: claimRow, error: claimErr } = await admin
    .rpc("create_reward_claim", {
      p_wallet: wallet,
      p_min_amount: fromBaseUnits(limits.minClaim, asset.decimals),
      p_max_amount:
        limits.maxClaim === null
          ? null
          : fromBaseUnits(limits.maxClaim, asset.decimals),
    })
    .single<{ id: string; amount: string; asset: string; status: string }>();

  if (claimErr) {
    const code = (claimErr as { code?: string }).code;
    // Postgres error codes raised deliberately by the function, mapped to honest HTTP status.
    if (code === "23505") return jsonError(409, "a claim is already in progress for this wallet");
    if (code === "P0002") return jsonError(400, "nothing to claim");
    if (code === "P0003") {
      return jsonError(400, "claimable balance is below the minimum claim amount", {
        minClaim: limits.minClaim.toString(),
      });
    }
    if (code === "P0004") {
      return jsonError(409, "claim exceeds the maximum automatic amount and needs manual release");
    }
    return jsonError(500, "failed to create claim", { detail: claimErr.message });
  }
  if (!claimRow) return jsonError(500, "failed to create claim");

  const amount = toBaseUnits(String(claimRow.amount), asset.decimals);

  // The on-chain call happens only after the claim has committed. `sendReward` re-resolves the
  // reward asset and checks `paused()` itself.
  try {
    const { hash } = await sendReward({ to: wallet as `0x${string}`, amount });
    await admin.rpc("finalize_reward_claim", {
      p_claim_id: claimRow.id,
      p_status: "submitted",
      p_tx_hash: hash,
    });
    return jsonOk({ claimId: claimRow.id, status: "submitted", txHash: hash });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    // Returns the credits to claimable in the same transaction — the viewer keeps their reward.
    await admin.rpc("finalize_reward_claim", {
      p_claim_id: claimRow.id,
      p_status: "failed",
      p_error: message,
    });
    return jsonError(502, "on-chain transfer failed", {
      claimId: claimRow.id,
      status: "failed",
      detail: message,
    });
  }
}
