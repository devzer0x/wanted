// GET /api/rewards/claims — CONTRACTS-PREDICTIONS.md §10.5: the session wallet's last 10 claims.
//
// -> { claims: [{ id, amount, asset, status, tx_hash, explorer_url, created_at, confirmed_at }] }
//
// The wallet comes only from the signed session cookie, never from a query string. The response
// never carries raw_tx, nonce, fees or error text — only what a viewer needs to watch a claim go
// queued -> paid and open the receipt on the explorer.
//
// AMOUNTS: selected as `amount::text` (PostgREST cast syntax) so the numeric never becomes a JSON
// number, then shifted to base units with the configured asset's decimals (FM-08).
//
// WHICH HASH: `tx_hash` is the hash that proves the claim's outcome, and only when the row says which
// hash that is:
//   * no override recorded           -> the claim's own tx_hash (null while queued)
//   * a `cancel` override recorded   -> none for a failed claim (the signed transfer never landed; the
//                                       operator's cancel did); the claim's own hash while in flight
//   * a `pay` override recorded      -> while in flight, the claim's own hash. Once terminal, EITHER
//                                       the original or the replacement may be the one that mined, and
//                                       reward_claims has no column recording which one confirm_claim
//                                       or fail_claim accepted — so none is named rather than a guess.
//                                       (Reported to the orchestrator as a contract gap.)

import { readSession } from "@/lib/auth/session";
import { rewardAsset } from "@/lib/chain/assets";
import { explorerTxUrl } from "@/lib/chain/config";
import type { Claim, ClaimStatus, ClaimsResponse } from "@/lib/prediction/types";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured } from "../../_lib/http";
import { toBaseUnits } from "../../_lib/amount";

export const dynamic = "force-dynamic";

const CLAIM_STATUSES: readonly ClaimStatus[] = [
  "queued",
  "signed",
  "broadcast",
  "confirmed",
  "failed",
  "needs_review",
];
const HASH_RE = /^0x[0-9a-f]{64}$/i;

interface ClaimRow {
  id: string;
  amount: string;
  asset: string;
  status: string;
  tx_hash: string | null;
  override_tx_hash: string | null;
  override_kind: string | null;
  created_at: string;
  confirmed_at: string | null;
}

function isClaimStatus(v: string): v is ClaimStatus {
  return (CLAIM_STATUSES as readonly string[]).includes(v);
}

function provingHash(row: ClaimRow, status: ClaimStatus): string | null {
  const own = row.tx_hash && HASH_RE.test(row.tx_hash) ? row.tx_hash.toLowerCase() : null;
  const terminal = status === "confirmed" || status === "failed";
  if (row.override_kind === null) return own;
  if (!terminal) return own;
  return null;
}

export async function GET(): Promise<Response> {
  const session = await readSession();
  if (!session?.address) return jsonError(401, "not authenticated");
  const wallet = session.address;

  const asset = rewardAsset();
  if (!asset) return notConfigured("reward asset");

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  const { data, error } = await admin
    .from("reward_claims")
    .select("id, amount:amount::text, asset, status, tx_hash, override_tx_hash, override_kind, created_at, confirmed_at")
    .eq("wallet", wallet)
    // FS5: only the configured asset's claims — each row is scaled with THIS asset's decimals below,
    // and a claim in another asset rendered at this scale would be wrong by orders of magnitude.
    .eq("asset", asset.symbol)
    .order("created_at", { ascending: false })
    .limit(10);
  if (error) {
    console.error(`rewards/claims: select ${typeof error.code === "string" ? error.code : "unknown"}`);
    return jsonError(500, "failed to read claims");
  }

  const claims: Claim[] = [];
  for (const row of (data ?? []) as ClaimRow[]) {
    if (!isClaimStatus(row.status)) {
      console.error("rewards/claims: unknown_claim_status");
      return jsonError(500, "failed to read claims");
    }
    let amount: string;
    try {
      amount = toBaseUnits(row.amount, asset.decimals).toString();
    } catch {
      console.error("rewards/claims: amount_unrepresentable");
      return jsonError(500, "failed to read claims");
    }
    const txHash = provingHash(row, row.status);
    claims.push({
      id: row.id,
      amount,
      asset: row.asset,
      status: row.status,
      tx_hash: txHash,
      explorer_url: txHash ? explorerTxUrl(txHash) : null,
      created_at: row.created_at,
      confirmed_at: row.confirmed_at,
    });
  }

  const body: ClaimsResponse = { claims };
  return jsonOk(body);
}
