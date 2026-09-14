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
import type { ClaimStatus, InFlightClaim, RewardBalance } from "@/lib/prediction/types";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { getClientCountry, jsonError, jsonOk, notConfigured } from "../../_lib/http";
import { sumBaseUnits } from "../../_lib/amount";

export const dynamic = "force-dynamic";

interface LedgerRow {
  /** `amount::text` — a JSON string, never a JSON number (FM-08: 5e-7 as a double prints "5e-7"). */
  amount: string;
  claim_id: string | null;
}

interface InFlightRow {
  id: string;
  status: string;
  tx_hash: string | null;
}

const IN_FLIGHT: readonly ClaimStatus[] = ["queued", "signed", "broadcast", "needs_review"];

function sqlState(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code;
  return typeof code === "string" && /^[0-9A-Z]{5}$/.test(code) ? code : "unknown";
}

export async function GET(request: Request): Promise<Response> {
  const session = await readSession();
  if (!session?.address) return jsonError(401, "not authenticated");
  const wallet = session.address;

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  // `rewardAsset()` is null when NEXT_PUBLIC_TTWO_TOKEN is unset — reported honestly rather than
  // as a fake "TTWO". Its `decimals` is also what converts the ledger's whole-unit `numeric`
  // amounts into base units, so without a configured asset there is no correct scale to report a
  // balance in, and saying "0" would be a claim about reality we cannot make.
  const asset = rewardAsset();
  if (!asset) return notConfigured("reward asset");

  // FS5: only the configured asset's rows. Every amount below is scaled with THIS asset's decimals, so
  // a credit in another asset would be summed at the wrong scale; it is simply not this balance.
  const { data: rows, error } = await admin
    .from("reward_ledger")
    .select("amount:amount::text, claim_id")
    .eq("wallet", wallet)
    .eq("asset", asset.symbol);
  if (error) {
    // Fixed text out, SQLSTATE to the server log only (CONTRACTS §10.1 principle 7).
    console.error(`rewards/balance: reward_ledger ${sqlState(error)}`);
    return jsonError(500, "failed to read reward balance");
  }

  // The wallet's one non-terminal claim, if any. The partial unique index guarantees at most one, so
  // maybeSingle() is exact, not a guess. While it exists the panel disables Claim: a second click
  // could only ever 409.
  const { data: inFlightRow, error: inFlightErr } = await admin
    .from("reward_claims")
    .select("id, status, tx_hash")
    .eq("wallet", wallet)
    .eq("asset", asset.symbol)
    .in("status", IN_FLIGHT as ClaimStatus[])
    .maybeSingle<InFlightRow>();
  if (inFlightErr) {
    console.error(`rewards/balance: reward_claims ${sqlState(inFlightErr)}`);
    return jsonError(500, "failed to read reward balance");
  }
  let inFlight: InFlightClaim | null = null;
  if (inFlightRow) {
    const status = IN_FLIGHT.find((st) => st === inFlightRow.status);
    if (!status) {
      console.error("rewards/balance: unknown_claim_status");
      return jsonError(500, "failed to read reward balance");
    }
    inFlight = { id: inFlightRow.id, status, tx_hash: inFlightRow.tx_hash };
  }

  const ledgerRows = (rows ?? []) as LedgerRow[];


  const claimable = sumBaseUnits(
    ledgerRows.filter((r) => r.claim_id === null).map((r) => r.amount),
    asset.decimals,
  ).toString();
  const lifetime = sumBaseUnits(ledgerRows.map((r) => r.amount), asset.decimals).toString();

  const policy = await assessEligibility({
    address: wallet,
    country: getClientCountry(request),
    rulesVersion: session.rulesVersion,
  });

  // FS6: a wallet excluded under the rules' Fair play section is refused at claim time by the database
  // (create_reward_claim P0006, assign_claim_nonce). Say so here too, so the panel never offers a
  // Claim button that could only 403. Read here rather than inside assessEligibility, which has no
  // database access and should stay that way.
  const { data: flag, error: flagErr } = await admin
    .from("policy_flags")
    .select("blocked")
    .eq("wallet", wallet)
    .maybeSingle<{ blocked: boolean }>();
  if (flagErr) {
    console.error(`rewards/balance: policy_flags ${sqlState(flagErr)}`);
    return jsonError(500, "failed to read reward balance");
  }
  const walletBlocked = flag?.blocked === true;

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
    eligible: policy.eligible && limitsError === null && !walletBlocked,
    reasons: [
      ...policy.reasons,
      ...(walletBlocked ? ["wallet_blocked"] : []),
      ...(limitsError ? ["reward_limits_misconfigured"] : []),
    ],
    min_claim: minClaim,
    in_flight_claim: inFlight,
  };
  return jsonOk(body);
}
