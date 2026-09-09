// GET /api/rewards/balance — CONTRACTS-PREDICTIONS.md §4. -> {claimable, lifetime, asset} for the
// cookie's wallet.
//
// Security property #1: wallet comes only from `readSession()`, never from a query string.
//
// AGGREGATE NOTE: this Supabase project has PostgREST computed aggregates (`.select("amount.sum()")`)
// disabled — PGRST123, documented already in web/.env.local's own comment about an unrelated page.
// So `claimable`/`lifetime` are summed here in TypeScript with BigInt over the raw ledger rows
// (`_lib/amount.ts`), never with a PostgREST aggregate and never with `Number`.

import { readSession } from "@/lib/auth/session";
import { assessEligibility } from "@/lib/policy";
import { rewardLimits } from "@/lib/rewards/strategies";
import { rewardAsset } from "@/lib/chain/assets";
import type { RewardBalance } from "@/lib/prediction/types";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured } from "../../_lib/http";
import { sumBaseUnits } from "../../_lib/amount";

export const dynamic = "force-dynamic";

interface LedgerRow {
  amount: string | number;
  claim_id: string | null;
}

export async function GET(): Promise<Response> {
  const session = await readSession();
  if (!session?.address) return jsonError(401, "not authenticated");
  const wallet = session.address;

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  const { data: rows, error } = await admin
    .from("reward_ledger")
    .select("amount, claim_id")
    .eq("wallet", wallet);
  if (error) {
    return jsonError(500, "failed to read reward_ledger", { detail: error.message });
  }

  const ledgerRows = (rows ?? []) as LedgerRow[];

  // `rewardAsset()` is null when NEXT_PUBLIC_TTWO_TOKEN is unset — reported honestly rather than
  // as a fake "TTWO". Its `decimals` is also what converts the ledger's whole-unit `numeric`
  // amounts into base units, so without a configured asset there is no correct scale to report a
  // balance in, and saying "0" would be a claim about reality we cannot make.
  const asset = rewardAsset();
  if (!asset) return notConfigured("reward asset");

  const claimable = sumBaseUnits(
    ledgerRows.filter((r) => r.claim_id === null).map((r) => r.amount),
    asset.decimals,
  ).toString();
  const lifetime = sumBaseUnits(ledgerRows.map((r) => r.amount), asset.decimals).toString();

  const policy = await assessEligibility({ address: wallet });

  // `min_claim` is advisory display data, so a misconfigured limit must not blank the balance the
  // viewer is entitled to see. It reports the failure as an ineligibility reason instead — which
  // is honest, because a claim WOULD be refused in this state (the claim route 500s on the same
  // parse), and §5 already routes "cannot confirm" to ineligible rather than to a silent success.
  let minClaim: string;
  let limitsError: string | null = null;
  try {
    minClaim = rewardLimits().minClaim.toString();
  } catch (err) {
    minClaim = "0";
    limitsError = err instanceof Error ? err.message : String(err);
  }

  const body: RewardBalance = {
    asset: asset.symbol,
    claimable,
    lifetime,
    eligible: policy.eligible && limitsError === null,
    reasons: limitsError ? [...policy.reasons, "reward_limits_misconfigured"] : policy.reasons,
    min_claim: minClaim,
  };
  return jsonOk(body);
}
