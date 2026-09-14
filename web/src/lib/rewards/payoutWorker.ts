// The payout worker — the single signer and the reconciler. docs/CONTRACTS-PREDICTIONS.md §10.4.
//
// Run only by GET /api/cron/tick, only while holding the payout lease. It is the ONLY importer of
// ./treasury (§10.1 principle 1); no request path can reach the key.
//
// One run, in order (§10.4):
//   1. acquire the lease (holder = a fresh UUID; every mutating SQL call carries it as a fencing
//      token and the database refuses a holder whose lease is gone — P0010)
//   2. reconcile every signed/broadcast claim in nonce order, from chain truth:
//        receipt (override first, then tx_hash)  -> confirm_claim / fail_claim with that receipt
//        no receipt, chain nonce <= claim nonce  -> the tx can still land: re-broadcast the PERSISTED
//                                                  raw_tx (only if payouts are on and not halted)
//        no receipt, chain nonce >  claim nonce  -> wait, re-read receipts once, then flag_claim_review
//                                                  (which halts the treasury) — never release
//   3. idle nonce sanity when nothing is in flight: chain 'latest' ahead of the database on two reads
//      halts the treasury (a tx this system did not record used the key); behind skips signing
//   4. if payouts are on, not halted, nothing needs review and nothing is in flight: up to
//      MAX_PAYOUTS_PER_TICK claims, each preflight -> assign_claim_nonce -> signTransfer (no
//      network) -> record_signed_claim (PERSIST) -> broadcastRaw -> mark_claim_broadcast -> short
//      receipt poll -> confirm/fail, or leave it in flight and stop
//   5. write the public treasury_status, release the lease (finally)
//
// What this file never does (refuse_list): mark a claim failed without a receipt-backed proof;
// broadcast bytes that are not already persisted; pick a nonce from the chain; fee-bump or replace;
// sign above the fee ceiling; carry on after a failed database write as if it had succeeded; put
// Postgres, viem or noble text anywhere (reasons and error codes are fixed snake_case strings).

import "server-only";

import { randomUUID } from "node:crypto";
import type { SupabaseClient } from "@supabase/supabase-js";
import { keccak256 } from "viem";

import { rewardAsset } from "@/lib/chain/assets";
import { toBaseUnits } from "@/app/api/_lib/amount";
import {
  TreasuryError,
  broadcastRaw,
  getReceipt,
  getTransactionMeta,
  isTreasuryConfigured,
  readChainState,
  readLatestNonce,
  signTransfer,
  simulateTransfer,
  treasuryAddress,
} from "./treasury";

// BigInt(...) rather than `5_000_000_000n`: web/tsconfig.json targets ES2017, where a BigInt
// literal is a compile error (TS2737). The values are exactly the frozen ones.
export const LEASE_TTL_SECONDS = 120; // > the cron's maxDuration (60)
export const MAX_FEE_CEILING = BigInt("5000000000"); // 5 gwei: above this, wait + report, never pay
export const FEE_FLOOR = BigInt("500000000"); // maxFeePerGas = clamp(2 x gasPrice, floor, ceiling)
export const GAS_LIMIT_CEILING = BigInt("150000"); // gas_limit = min(ceil(estimate * 1.3), ceiling)
export const MAX_PAYOUTS_PER_TICK = 5;
export const RECEIPT_POLL_MS = 20_000; // in-tick wait for a fresh tx's receipt
export const SAFETY_MARGIN_MS = 8_000; // stop starting new work with less than this left

export interface PayoutSummary {
  enabled: boolean;
  reason: string | null; // why nothing (more) ran: payouts_paused, lease_held, treasury_not_configured, halted, ...
  treasury: string | null;
  halted: boolean;
  halt_reason: string | null;
  in_flight: number;
  needs_review: number;
  stuck: number;
  paid: number;
  failed: number;
  queued_remaining: number;
  eth_balance: string | null; // base units
  token_balance: string | null; // base units
  liability: string | null; // base units
  runway_days: number | null;
  chain_nonce: string | null;
  next_nonce: string | null;
}

/** Robinhood Chain mainnet; §10.4 preflight "chainId 4663". */
const PAYOUT_CHAIN_ID = 4663;
/** Short wait before the SECOND read that a halt or a review depends on — the public RPC is
 * load-balanced and one node's receipt index can lag its nonce view (design: reconciliation). */
const RECHECK_WAIT_MS = 2_000;
const RECEIPT_POLL_INTERVAL_MS = 1_000;
/** §10.2 stuck_since: signed/broadcast for more than 30 minutes with no receipt. */
const STUCK_AFTER_MS = 30 * 60 * 1000;

const ZERO = BigInt(0);
const TWO = BigInt(2);
const THREE = BigInt(3);

// ---------------------------------------------------------------------------------------------
// Snapshot parsing (payout_snapshot jsonb). Per the orchestrator's clarification every bigint or
// numeric in it is a JSON STRING; nonces are additionally accepted as safe integers because a nonce
// can never approach 2^53. Amounts are accepted ONLY as strings.
// ---------------------------------------------------------------------------------------------

type Hex = `0x${string}`;

interface InFlightClaim {
  id: string;
  wallet: Hex;
  /** The treasury address that signed raw_tx — not necessarily the CURRENT key after a rotation. */
  signer: Hex;
  status: "signed" | "broadcast";
  nonce: bigint;
  raw_tx: Hex;
  tx_hash: Hex;
  override_tx_hash: Hex | null;
  override_kind: "pay" | "cancel" | null;
  signed_at: string | null;
  stuck_since: string | null;
}

interface QueuedClaim {
  id: string;
  wallet: Hex;
  asset: string;
  amount_text: string;
  nonce: bigint | null;
}

interface Snapshot {
  account: { next_nonce: bigint; halted: boolean; halt_reason: string | null } | null;
  in_flight: InFlightClaim[];
  needs_review: number;
  queued: QueuedClaim[];
  liability_text: string;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const WALLET_RE = /^0x[0-9a-f]{40}$/;
const HASH_RE = /^0x[0-9a-f]{64}$/i;
const RAW_RE = /^0x(?:[0-9a-f]{2})+$/i;
const DECIMAL_TEXT_RE = /^\d+(\.\d+)?$/;
const REASON_CODE_RE = /^[a-z_]{1,48}$/;

class SnapshotInvalid extends Error {}

function obj(v: unknown): Record<string, unknown> {
  if (typeof v !== "object" || v === null || Array.isArray(v)) throw new SnapshotInvalid();
  return v as Record<string, unknown>;
}

function uintStringOrSafeInt(v: unknown): bigint {
  if (typeof v === "string" && /^\d+$/.test(v)) return BigInt(v);
  if (typeof v === "number" && Number.isSafeInteger(v) && v >= 0) return BigInt(v);
  throw new SnapshotInvalid();
}

function countValue(v: unknown): number {
  if (typeof v === "number" && Number.isSafeInteger(v) && v >= 0) return v;
  if (typeof v === "string" && /^\d{1,15}$/.test(v)) return Number(v);
  throw new SnapshotInvalid();
}

function hashValue(v: unknown): Hex {
  if (typeof v !== "string" || !HASH_RE.test(v)) throw new SnapshotInvalid();
  return v.toLowerCase() as Hex;
}

function optHash(v: unknown): Hex | null {
  return v === null || v === undefined ? null : hashValue(v);
}

function optText(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v !== "string") throw new SnapshotInvalid();
  return v;
}

function uuidValue(v: unknown): string {
  if (typeof v !== "string" || !UUID_RE.test(v)) throw new SnapshotInvalid();
  return v;
}

function walletValue(v: unknown): Hex {
  if (typeof v !== "string" || !WALLET_RE.test(v)) throw new SnapshotInvalid();
  return v as Hex;
}

function parseSnapshot(raw: unknown): Snapshot {
  const root = obj(raw);

  let account: Snapshot["account"] = null;
  if (root.account !== null && root.account !== undefined) {
    const a = obj(root.account);
    if (typeof a.halted !== "boolean") throw new SnapshotInvalid();
    account = {
      next_nonce: uintStringOrSafeInt(a.next_nonce),
      halted: a.halted,
      halt_reason: optText(a.halt_reason),
    };
  }

  if (!Array.isArray(root.in_flight) || !Array.isArray(root.queued)) throw new SnapshotInvalid();

  const in_flight = root.in_flight.map((item): InFlightClaim => {
    const c = obj(item);
    if (c.status !== "signed" && c.status !== "broadcast") throw new SnapshotInvalid();
    if (typeof c.raw_tx !== "string" || !RAW_RE.test(c.raw_tx)) throw new SnapshotInvalid();
    const kind = c.override_kind ?? null;
    if (kind !== null && kind !== "pay" && kind !== "cancel") throw new SnapshotInvalid();
    const overrideHash = optHash(c.override_tx_hash);
    if ((kind === null) !== (overrideHash === null)) throw new SnapshotInvalid();
    return {
      id: uuidValue(c.id),
      wallet: walletValue(c.wallet),
      signer: walletValue(c.signer_address),
      status: c.status,
      nonce: uintStringOrSafeInt(c.nonce),
      raw_tx: c.raw_tx.toLowerCase() as Hex,
      tx_hash: hashValue(c.tx_hash),
      override_tx_hash: overrideHash,
      override_kind: kind,
      signed_at: optText(c.signed_at),
      stuck_since: optText(c.stuck_since),
    };
  });
  in_flight.sort((x, y) => (x.nonce < y.nonce ? -1 : x.nonce > y.nonce ? 1 : 0));

  const queued = root.queued.map((item): QueuedClaim => {
    const q = obj(item);
    if (typeof q.asset !== "string" || q.asset.length === 0) throw new SnapshotInvalid();
    if (typeof q.amount_text !== "string" || !DECIMAL_TEXT_RE.test(q.amount_text)) {
      throw new SnapshotInvalid();
    }
    return {
      id: uuidValue(q.id),
      wallet: walletValue(q.wallet),
      asset: q.asset,
      amount_text: q.amount_text,
      nonce: q.nonce === null || q.nonce === undefined ? null : uintStringOrSafeInt(q.nonce),
    };
  });

  if (typeof root.liability_text !== "string" || !DECIMAL_TEXT_RE.test(root.liability_text)) {
    throw new SnapshotInvalid();
  }

  return {
    account,
    in_flight,
    needs_review: countValue(root.needs_review),
    queued,
    liability_text: root.liability_text,
  };
}

// ---------------------------------------------------------------------------------------------
// Run context
// ---------------------------------------------------------------------------------------------

/** Ends the run with a fixed reason code. Never carries library text. */
class StopRun extends Error {
  readonly reason: string;
  constructor(reason: string) {
    super(reason);
    this.reason = reason;
  }
}

interface Ctx {
  admin: SupabaseClient;
  holder: string;
  address: Hex;
  deadline: number;
  summary: PayoutSummary;
  enabled: boolean;
  halted: boolean;
  lastChainState: { ethBalance: bigint; tokenBalance: bigint } | null;
}

function remaining(ctx: Ctx): number {
  return ctx.deadline - Date.now();
}

function ensureBudget(ctx: Ctx): void {
  if (remaining(ctx) < SAFETY_MARGIN_MS) throw new StopRun("budget_exhausted");
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}

type RpcResult<T> = { ok: true; data: T } | { ok: false; code: string };

/**
 * One PostgREST RPC. postgrest-js returns `{error}` instead of throwing — including when the fetch
 * deadline aborts — so EVERY result goes through here and is checked. A throw is folded into the same
 * failure shape. Only the SQLSTATE survives; the message never leaves this function.
 */
async function rpc<T>(ctx: Pick<Ctx, "admin">, fn: string, args: Record<string, unknown>): Promise<RpcResult<T>> {
  try {
    const { data, error } = await ctx.admin.rpc(fn, args);
    if (error) return { ok: false, code: typeof error.code === "string" ? error.code : "" };
    return { ok: true, data: data as T };
  } catch {
    return { ok: false, code: "" };
  }
}

function stopFor(fn: string, code: string): StopRun {
  if (code === "P0010") return new StopRun("lease_lost");
  if (code === "P0011") return new StopRun("treasury_account_unavailable");
  if (code === "P0012") return new StopRun("claim_in_flight");
  return new StopRun(`${fn}_failed`);
}

/** A mutating worker call whose failure must stop the run — never proceed as if it succeeded. */
async function write<T = unknown>(ctx: Ctx, fn: string, args: Record<string, unknown>): Promise<T> {
  const res = await rpc<T>(ctx, fn, { p_holder: ctx.holder, ...args });
  if (!res.ok) throw stopFor(fn, res.code);
  return res.data;
}

/** Treasury calls: a TreasuryError becomes a stop with its (allowlisted) code. */
async function chain<T>(op: () => Promise<T>): Promise<T> {
  try {
    return await op();
  } catch (err) {
    if (err instanceof TreasuryError) throw new StopRun(err.code);
    throw new StopRun("treasury_error");
  }
}

function emptySummary(): PayoutSummary {
  return {
    enabled: false,
    reason: null,
    treasury: null,
    halted: false,
    halt_reason: null,
    in_flight: 0,
    needs_review: 0,
    stuck: 0,
    paid: 0,
    failed: 0,
    queued_remaining: 0,
    eth_balance: null,
    token_balance: null,
    liability: null,
    runway_days: null,
    chain_nonce: null,
    next_nonce: null,
  };
}

function isStuck(c: InFlightClaim, now: number): boolean {
  if (c.stuck_since) return true;
  if (!c.signed_at) return false;
  const t = Date.parse(c.signed_at);
  return Number.isFinite(t) && now - t > STUCK_AFTER_MS;
}

async function readSnapshot(ctx: Ctx): Promise<Snapshot> {
  const res = await rpc<unknown>(ctx, "payout_snapshot", { p_address: ctx.address });
  if (!res.ok) throw new StopRun("payout_snapshot_failed");
  try {
    return parseSnapshot(res.data);
  } catch {
    throw new StopRun("payout_snapshot_invalid");
  }
}

function applySnapshot(ctx: Ctx, snap: Snapshot): void {
  const s = ctx.summary;
  const now = Date.now();
  s.in_flight = snap.in_flight.length;
  s.needs_review = snap.needs_review;
  s.stuck = snap.in_flight.filter((c) => isStuck(c, now)).length;
  s.queued_remaining = snap.queued.length;
  if (snap.account) {
    s.next_nonce = snap.account.next_nonce.toString();
    if (snap.account.halted) {
      s.halted = true;
      s.halt_reason = snap.account.halt_reason;
    }
  }
  const asset = rewardAsset();
  if (asset) {
    try {
      s.liability = toBaseUnits(snap.liability_text, asset.decimals).toString();
    } catch {
      s.liability = null;
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Reconciliation
// ---------------------------------------------------------------------------------------------

type Found = {
  hash: Hex;
  kind: "tx" | "pay" | "cancel";
  receipt: { status: "success" | "reverted"; blockNumber: bigint };
};

/**
 * Receipts for the recorded override (if any) first, then the claim's own hash.
 *
 * An override is honoured only when it really is the claim signer's own transaction at the claim's
 * nonce (FS10): a receipt proves a transaction mined, not which nonce it consumed, and a `cancel`
 * recorded with a transaction at any other nonce would release the viewer's credits while the
 * original signed transfer could still land. A mismatch is for a human, never guessed at. The claim's
 * own tx_hash needs no such check — its bytes are ours, and they fix both the sender and the nonce.
 */
async function findReceipt(c: InFlightClaim): Promise<Found | "override_mismatch" | null> {
  if (c.override_tx_hash && c.override_kind) {
    const hash = c.override_tx_hash;
    const kind = c.override_kind;
    const r = await chain(() => getReceipt(hash));
    if (r) {
      const meta = await chain(() => getTransactionMeta(hash));
      if (!meta || meta.from !== c.signer || meta.nonce !== c.nonce) return "override_mismatch";
      return { hash, kind, receipt: r };
    }
  }
  const r = await chain(() => getReceipt(c.tx_hash));
  return r ? { hash: c.tx_hash, kind: "tx", receipt: r } : null;
}

/** Applies a receipt: the ONLY paths from in-flight to a terminal state, each carrying its proof. */
async function settle(ctx: Ctx, claimId: string, found: Found): Promise<"confirmed" | "failed"> {
  if (found.kind === "cancel") {
    // The operator's recorded 0-value self-transfer consumed the nonce; the transfer cannot land.
    await write(ctx, "fail_claim", { p_claim_id: claimId, p_proof: "cancel_receipt", p_tx_hash: found.hash });
    ctx.summary.failed += 1;
    return "failed";
  }
  if (found.receipt.status === "success") {
    await write(ctx, "confirm_claim", {
      p_claim_id: claimId,
      p_tx_hash: found.hash,
      p_block_number: found.receipt.blockNumber.toString(),
    });
    ctx.summary.paid += 1;
    return "confirmed";
  }
  await write(ctx, "fail_claim", { p_claim_id: claimId, p_proof: "receipt_reverted", p_tx_hash: found.hash });
  ctx.summary.failed += 1;
  return "failed";
}

async function flagReview(ctx: Ctx, claimId: string, reason: string): Promise<void> {
  await write(ctx, "flag_claim_review", { p_claim_id: claimId, p_reason: reason });
  ctx.halted = true;
  ctx.summary.halted = true;
  ctx.summary.halt_reason = reason;
}

async function reconcileOne(
  ctx: Ctx,
  c: InFlightClaim,
): Promise<"confirmed" | "failed" | "in_flight" | "review"> {
  const found = await findReceipt(c);
  if (found === "override_mismatch") {
    await flagReview(ctx, c.id, "override_mismatch");
    return "review";
  }
  if (found) return settle(ctx, c.id, found);

  // FS9: signed by a PREVIOUS key (a rotation with this claim still in flight). Every nonce read below
  // is the CURRENT key's count, which says nothing about the old key's nonce: comparing them would
  // re-broadcast the old bytes forever and swallow the signal that something else used that nonce.
  // Receipts (above) are the only safe evidence here; with none, a human decides.
  if (c.signer !== ctx.address) {
    await flagReview(ctx, c.id, "signer_rotated_in_flight");
    return "review";
  }

  const chainNonce = await chain(() => readLatestNonce());
  ctx.summary.chain_nonce = chainNonce.toString();

  if (chainNonce <= c.nonce) {
    // The nonce is unconsumed: the tx is pending or was dropped, and it can still land. Never
    // release here. Re-broadcast the persisted bytes — and only those — if allowed to.
    //
    // FS1: the switch is re-read right before bytes go back on the wire. The run's start-of-tick
    // value is up to a minute old, and "payouts off" must mean nothing new reaches the chain from the
    // moment it is set, not from the next tick.
    const live = await rpc<unknown>(ctx, "payouts_enabled", {});
    ctx.enabled = live.ok && live.data === true;
    ctx.summary.enabled = ctx.enabled;
    if (ctx.enabled && !ctx.halted) {
      if (keccak256(c.raw_tx) !== c.tx_hash) {
        // The stored bytes are not the recorded tx. Nothing about that is safe to automate.
        await flagReview(ctx, c.id, "raw_tx_hash_mismatch");
        return "review";
      }
      let failure: string | null = null;
      try {
        await broadcastRaw(c.raw_tx);
      } catch (err) {
        failure = err instanceof TreasuryError ? err.code : "treasury_error";
      }
      if (failure) {
        await write(ctx, "record_claim_attempt", { p_claim_id: c.id, p_error_code: failure });
      } else {
        await write(ctx, "mark_claim_broadcast", { p_claim_id: c.id });
        if (!c.stuck_since && isStuck(c, Date.now())) {
          // Out for 30+ minutes with no receipt: record_claim_attempt is what sets stuck_since.
          await write(ctx, "record_claim_attempt", { p_claim_id: c.id, p_error_code: "not_mined" });
        }
      }
    }
    return "in_flight";
  }

  // The nonce is consumed and no recorded hash has a receipt. Read again after a short wait before
  // concluding anything; if there is no budget for the wait, leave it for the next tick.
  if (remaining(ctx) - RECHECK_WAIT_MS < SAFETY_MARGIN_MS) throw new StopRun("budget_exhausted");
  await sleep(RECHECK_WAIT_MS);
  const again = await findReceipt(c);
  if (again === "override_mismatch") {
    await flagReview(ctx, c.id, "override_mismatch");
    return "review";
  }
  if (again) return settle(ctx, c.id, again);
  await flagReview(ctx, c.id, "nonce_consumed_without_receipt");
  return "review";
}

// ---------------------------------------------------------------------------------------------
// Signing one claim
// ---------------------------------------------------------------------------------------------

function clamp(v: bigint, lo: bigint, hi: bigint): bigint {
  return v < lo ? lo : v > hi ? hi : v;
}

function nonceFrom(v: unknown): bigint | null {
  if (typeof v === "number" && Number.isSafeInteger(v) && v >= 0) return BigInt(v);
  if (typeof v === "string" && /^\d+$/.test(v)) return BigInt(v);
  return null;
}

async function pollReceipt(
  hash: Hex,
  windowMs: number,
): Promise<{ status: "success" | "reverted"; blockNumber: bigint } | null> {
  const end = Date.now() + Math.max(0, windowMs);
  for (;;) {
    try {
      const r = await getReceipt(hash);
      if (r) return r;
    } catch {
      // Unreachable node mid-poll: keep polling until the window closes; a missing receipt is
      // never a failure, it just leaves the claim in flight for the next tick.
    }
    if (Date.now() + RECEIPT_POLL_INTERVAL_MS > end) return null;
    await sleep(RECEIPT_POLL_INTERVAL_MS);
  }
}

async function payOne(ctx: Ctx, q: QueuedClaim): Promise<"confirmed" | "failed"> {
  // Preflight (§10.4). Every refusal stops the run BEFORE anything is signed.
  const asset = rewardAsset();
  if (!asset) throw new StopRun("reward_asset_not_configured");
  if (q.asset !== asset.symbol) throw new StopRun("claim_asset_mismatch");
  let amountBase: bigint;
  try {
    amountBase = toBaseUnits(q.amount_text, asset.decimals);
  } catch {
    throw new StopRun("claim_amount_invalid");
  }
  if (amountBase <= ZERO) throw new StopRun("claim_amount_invalid");

  const token = asset.address.toLowerCase() as Hex;
  const state = await chain(() => readChainState(token));
  ctx.lastChainState = { ethBalance: state.ethBalance, tokenBalance: state.tokenBalance };
  ctx.summary.eth_balance = state.ethBalance.toString();
  ctx.summary.token_balance = state.tokenBalance.toString();
  ctx.summary.chain_nonce = state.latestNonce.toString();

  if (state.chainId !== PAYOUT_CHAIN_ID) throw new StopRun("wrong_chain");
  if (state.paused) throw new StopRun("token_paused");
  if (state.gasPrice > MAX_FEE_CEILING) throw new StopRun("gas_price_above_ceiling");
  const maxFeePerGas = clamp(state.gasPrice * TWO, FEE_FLOOR, MAX_FEE_CEILING);
  const maxPriorityFeePerGas = ZERO;
  if (state.tokenBalance < amountBase) throw new StopRun("insufficient_token_balance");

  const { gasEstimate } = await chain(() => simulateTransfer({ token, to: q.wallet, amountBase }));
  if (gasEstimate > GAS_LIMIT_CEILING) throw new StopRun("gas_estimate_above_ceiling");
  const scaled = (gasEstimate * BigInt(13) + BigInt(9)) / BigInt(10); // ceil(estimate * 1.3)
  const gasLimit = scaled < GAS_LIMIT_CEILING ? scaled : GAS_LIMIT_CEILING;
  if (state.ethBalance < THREE * gasLimit * maxFeePerGas) throw new StopRun("insufficient_eth");

  // Nonce from the database (returns the claim's existing nonce if one was already assigned).
  const assigned = await write<unknown>(ctx, "assign_claim_nonce", {
    p_claim_id: q.id,
    p_address: ctx.address,
  });
  if (assigned === null) {
    // The wallet is now blocked: the function failed the claim `never_signed` and released its
    // credits in the same transaction. Nothing was signed.
    ctx.summary.failed += 1;
    return "failed";
  }
  const nonce = nonceFrom(assigned);
  if (nonce === null) throw new StopRun("assign_claim_nonce_invalid");
  if (q.nonce !== null && nonce !== q.nonce) throw new StopRun("nonce_mismatch");
  // Strict serial: with nothing in flight, the nonce we sign must be the one the chain expects next.
  // Otherwise leave the claim queued-with-nonce; the next tick's sanity check judges the gap.
  if (nonce !== state.latestNonce) throw new StopRun("nonce_mismatch");

  // Sign locally — no network — then PERSIST before any node sees the bytes (§10.1 principle 2).
  const signed = await chain(() =>
    signTransfer({ token, to: q.wallet, amountBase, nonce, gasLimit, maxFeePerGas, maxPriorityFeePerGas }),
  );
  await write(ctx, "record_signed_claim", {
    p_claim_id: q.id,
    p_nonce: nonce.toString(),
    p_raw_tx: signed.raw,
    p_tx_hash: signed.hash,
    p_gas_limit: gasLimit.toString(),
    p_max_fee_per_gas: maxFeePerGas.toString(),
    p_max_priority_fee_per_gas: maxPriorityFeePerGas.toString(),
    p_chain_id: PAYOUT_CHAIN_ID,
    p_token_address: token,
    p_to_address: q.wallet,
    p_amount_base: amountBase.toString(),
    p_decimals: asset.decimals,
    p_signer_address: ctx.address,
  });

  // Broadcast exactly the persisted bytes. A failure is recorded as an attempt and the claim stays
  // `signed` — it is re-broadcast by reconciliation next tick, never failed.
  let failure: string | null = null;
  try {
    await broadcastRaw(signed.raw);
  } catch (err) {
    failure = err instanceof TreasuryError ? err.code : "treasury_error";
  }
  if (failure) {
    await write(ctx, "record_claim_attempt", { p_claim_id: q.id, p_error_code: failure });
    throw new StopRun(failure);
  }
  await write(ctx, "mark_claim_broadcast", { p_claim_id: q.id });

  const receipt = await pollReceipt(
    signed.hash,
    Math.min(RECEIPT_POLL_MS, remaining(ctx) - SAFETY_MARGIN_MS),
  );
  if (!receipt) throw new StopRun("awaiting_receipt"); // in flight; the next tick reconciles it
  return settle(ctx, q.id, { hash: signed.hash, kind: "tx", receipt });
}

// ---------------------------------------------------------------------------------------------
// The run
// ---------------------------------------------------------------------------------------------

async function work(ctx: Ctx): Promise<void> {
  const snap = await readSnapshot(ctx);
  applySnapshot(ctx, snap);
  ctx.halted = snap.account?.halted === true;

  // 2. Reconcile in nonce order. Receipts are read even while paused or halted.
  let stillInFlight = 0;
  for (const c of snap.in_flight) {
    ensureBudget(ctx);
    const outcome = await reconcileOne(ctx, c);
    if (outcome === "in_flight") stillInFlight += 1;
  }
  if (ctx.halted) throw new StopRun("halted");
  if (stillInFlight > 0) throw new StopRun("in_flight");

  // 3. Idle nonce sanity (and first-time account initialisation).
  let account = snap.account;
  if (!account) {
    const latest = await chain(() => readLatestNonce());
    ctx.summary.chain_nonce = latest.toString();
    const inserted = await write<unknown>(ctx, "init_treasury_account", {
      p_address: ctx.address,
      p_nonce: latest.toString(),
    });
    const next = nonceFrom(inserted);
    if (next === null) throw new StopRun("init_treasury_account_invalid");
    account = { next_nonce: next, halted: false, halt_reason: null };
    ctx.summary.next_nonce = next.toString();
  }

  // A queued claim that already holds a nonce (assigned, then the run died before persisting a
  // signature) has taken next_nonce - 1 without any tx existing for it; the chain should be there.
  const holding = snap.queued.find((q) => q.nonce !== null) ?? null;
  if (holding && holding.nonce !== account.next_nonce - BigInt(1)) {
    throw new StopRun("nonce_state_inconsistent");
  }
  const expected = holding ? (holding.nonce as bigint) : account.next_nonce;

  ensureBudget(ctx);
  let latest = await chain(() => readLatestNonce());
  ctx.summary.chain_nonce = latest.toString();
  if (latest > expected) {
    if (remaining(ctx) - RECHECK_WAIT_MS < SAFETY_MARGIN_MS) throw new StopRun("budget_exhausted");
    await sleep(RECHECK_WAIT_MS);
    latest = await chain(() => readLatestNonce());
    ctx.summary.chain_nonce = latest.toString();
    if (latest > expected) {
      await write(ctx, "halt_treasury", { p_address: ctx.address, p_reason: "unrecorded_tx_from_treasury" });
      ctx.halted = true;
      ctx.summary.halted = true;
      ctx.summary.halt_reason = "unrecorded_tx_from_treasury";
      throw new StopRun("halted");
    }
  }
  if (latest < expected) throw new StopRun("chain_nonce_behind");

  // 4. New payouts.
  if (!ctx.enabled) throw new StopRun("payouts_paused");
  if (snap.needs_review > 0) throw new StopRun("needs_review");

  let started = 0;
  for (const q of snap.queued) {
    if (started >= MAX_PAYOUTS_PER_TICK) break;
    ensureBudget(ctx);
    started += 1;
    await payOne(ctx, q);
  }
}

async function readDailyCapBase(ctx: Ctx, decimals: number): Promise<bigint | null> {
  try {
    // ->> returns the jsonb number as TEXT, so the cap never passes through a JS number.
    const { data, error } = await ctx.admin
      .from("site_config")
      .select("daily_cap:value->>daily_cap")
      .eq("key", "reward_caps")
      .maybeSingle();
    if (error || !data) return null;
    const text = (data as { daily_cap?: unknown }).daily_cap;
    if (typeof text !== "string") return null;
    const cap = toBaseUnits(text, decimals);
    return cap > ZERO ? cap : null;
  } catch {
    return null;
  }
}

/** Step 5: end-of-run counts, balances and the public treasury_status. Never throws. */
async function finish(ctx: Ctx): Promise<void> {
  const s = ctx.summary;
  try {
    const res = await rpc<unknown>(ctx, "payout_snapshot", { p_address: ctx.address });
    if (res.ok) applySnapshot(ctx, parseSnapshot(res.data));
  } catch {
    // Keep the start-of-run counts; the reason already says why the run ended.
  }

  // Re-read when nothing was read this run, AND whenever this run moved money: the preflight read
  // happened BEFORE the transfer, so publishing it would show the public treasury_status (and the
  // runway computed from it) the pre-payout balance. Found by verify-payout.mjs.
  const asset = rewardAsset();
  const movedMoney = s.paid > 0 || s.failed > 0;
  if (asset && (!ctx.lastChainState || movedMoney) && remaining(ctx) > 0) {
    try {
      const st = await readChainState(asset.address.toLowerCase() as Hex);
      ctx.lastChainState = { ethBalance: st.ethBalance, tokenBalance: st.tokenBalance };
      s.eth_balance = st.ethBalance.toString();
      s.token_balance = st.tokenBalance.toString();
      s.chain_nonce = st.latestNonce.toString();
    } catch {
      // Balances stay null: unknown, not zero.
    }
  }

  if (asset && ctx.lastChainState && s.liability !== null) {
    const cap = await readDailyCapBase(ctx, asset.decimals);
    if (cap !== null) {
      const free = ctx.lastChainState.tokenBalance - BigInt(s.liability);
      s.runway_days = Number((free * BigInt(100)) / cap) / 100;
    }
  }

  // Public (site_config is browser-readable): balances, counts, halted. Never reasons, errors,
  // raw txs, nonces or keys.
  const status = {
    updated_at: new Date().toISOString(),
    treasury: ctx.address,
    payouts_enabled: ctx.enabled,
    halted: s.halted,
    in_flight: s.in_flight,
    needs_review: s.needs_review,
    stuck: s.stuck,
    queued: s.queued_remaining,
    eth_balance: s.eth_balance,
    token_balance: s.token_balance,
    liability: s.liability,
    runway_days: s.runway_days,
  };
  const res = await rpc(ctx, "write_treasury_status", { p_holder: ctx.holder, p_status: status });
  if (!res.ok && s.reason === null) s.reason = res.code === "P0010" ? "lease_lost" : "write_treasury_status_failed";
}

/**
 * One payout run. Never throws: every outcome, including "did nothing", is a PayoutSummary whose
 * `reason` is a fixed snake_case code. A paused, held or unconfigured treasury is a normal result.
 */
export async function runPayoutWorker(
  admin: SupabaseClient,
  opts: { budgetMs: number },
): Promise<PayoutSummary> {
  const startedAt = Date.now();
  const budgetMs = Number.isFinite(opts.budgetMs) && opts.budgetMs > 0 ? opts.budgetMs : 0;
  const summary = emptySummary();

  // The kill switch (§10.1 principle 6): only the JSON boolean true turns payouts on. Unreadable
  // counts as off.
  const flag = await rpc<unknown>({ admin }, "payouts_enabled", {});
  const enabled = flag.ok && flag.data === true;
  summary.enabled = enabled;

  if (!isTreasuryConfigured()) {
    summary.reason = "treasury_not_configured";
    return summary;
  }
  const address = treasuryAddress();
  if (!address) {
    summary.reason = "treasury_not_configured";
    return summary;
  }
  summary.treasury = address;

  // The published treasury address, when set, must be the key's address: a mismatch means the
  // wrong key reached this deployment. Refuse before touching the lease, the chain or any claim.
  const published = process.env.NEXT_PUBLIC_TREASURY_ADDRESS?.trim().toLowerCase();
  if (published && published !== address) {
    summary.reason = "treasury_address_mismatch";
    return summary;
  }

  const holder = randomUUID();
  const lease = await rpc<unknown>({ admin }, "acquire_payout_lease", {
    p_holder: holder,
    p_ttl_seconds: LEASE_TTL_SECONDS,
  });
  if (!lease.ok) {
    summary.reason = "acquire_payout_lease_failed";
    return summary;
  }
  if (lease.data !== true) {
    summary.reason = "lease_held";
    return summary;
  }

  const ctx: Ctx = {
    admin,
    holder,
    address,
    deadline: startedAt + budgetMs,
    summary,
    enabled,
    halted: false,
    lastChainState: null,
  };

  try {
    try {
      await work(ctx);
      if (!flag.ok && summary.reason === null) summary.reason = "payouts_flag_unreadable";
    } catch (err) {
      summary.reason = err instanceof StopRun ? err.reason : "worker_error";
    }
    if (summary.reason !== "lease_lost") await finish(ctx);
  } catch {
    if (summary.reason === null) summary.reason = "worker_error";
  } finally {
    const released = await rpc({ admin }, "release_payout_lease", { p_holder: holder });
    if (!released.ok && summary.reason === null) summary.reason = "release_payout_lease_failed";
  }

  if (summary.reason !== null && !REASON_CODE_RE.test(summary.reason)) summary.reason = "worker_error";
  if (summary.reason !== null) console.error(`payout worker: ${summary.reason}`);
  return summary;
}
