// /api/cron/tick — CONTRACTS-PREDICTIONS.md §4 + §10.5: secret-gated; locks due predictions,
// settles due windows, then runs the payout worker. Calls `lock_due_predictions()` then
// `settle_due_predictions_serialized()` (settlement behind an advisory lock), then
// `runPayoutWorker(admin, {budgetMs: 40_000})`. No settlement logic lives here — an unknown
// `telemetry_rule.kind` or a heartbeat gap voiding a prediction is entirely `settle_due_predictions`'
// job (CONTRACTS-PREDICTIONS §3), not this route's — and no payout logic either: the worker in
// lib/rewards/payoutWorker.ts is the single signer, and this route is its clock.
//
// DURATION: maxDuration = 60 s. The payout lease TTL is 120 s (payoutWorker.LEASE_TTL_SECONDS), so a
// run killed at 60 s leaves a lease that expires before any other run can take it, and a zombie past
// its TTL is refused by every mutating SQL call (fencing, P0010). The worker gets 40 s of budget
// after lock + settle.
//
// A paused, held, halted or unconfigured payout step is a normal outcome, reported under `payouts`;
// it never fails the tick. Error responses carry fixed strings only — never Postgres text (§10.1
// principle 7); the SQLSTATE is logged server-side.
//
// METHOD NOTE (verified against Vercel's own docs, 2026-09-08, not guessed — CLAUDE.md rule 6):
// Vercel Cron Jobs always invoke the configured path with an HTTP GET
// (https://vercel.com/docs/cron-jobs — "Vercel makes an HTTP GET request... Vercel Functions
// triggered by a cron job... will always contain `vercel-cron/1.0` as the user agent"); there is no
// way to configure Vercel to send POST. Both are handled here, identically secret-gated, so the route
// is reachable the way Vercel will actually call it (GET, with `CRON_SECRET` sent automatically as
// `Authorization: Bearer <value>`) and by POST for manual/ops-triggered runs with the same secret.
//
// The secret check is the bearer token only — never the mere presence of an `x-vercel-cron-*`
// header, which is not a secret and could be sent by anyone.

import { timingSafeEqual } from "node:crypto";

import { runPayoutWorker, type PayoutSummary } from "@/lib/rewards/payoutWorker";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured } from "../../_lib/http";

export const dynamic = "force-dynamic";
export const maxDuration = 60;

const PAYOUT_BUDGET_MS = 40_000;
// FS2: the worker's budget is what is LEFT of this request, not a flat 40 s. lock + settle run first in
// the same 60 s function; if they took 30 s, a flat budget would put the worker's graceful stop after
// Vercel's hard kill. 55 s leaves 5 s for the response under maxDuration.
const REQUEST_DEADLINE_MS = 55_000;

function authorized(request: Request): boolean {
  const secret = process.env.CRON_SECRET;
  if (!secret) return false; // an unset secret must never mean "open"
  // FS8: constant-time comparison. timingSafeEqual needs equal lengths, and a length mismatch is
  // already a refusal, so it is checked first.
  const header = Buffer.from(request.headers.get("authorization") ?? "", "utf8");
  const expected = Buffer.from(`Bearer ${secret}`, "utf8");
  return header.length === expected.length && timingSafeEqual(header, expected);
}

function sqlState(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code;
  return typeof code === "string" && /^[0-9A-Z]{5}$/.test(code) ? code : "unknown";
}

async function handle(request: Request): Promise<Response> {
  const requestStartedAt = Date.now();
  if (!authorized(request)) return jsonError(401, "unauthorized");

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  const { data: locked, error: lockErr } = await admin.rpc("lock_due_predictions");
  if (lockErr) {
    console.error(`cron/tick: lock_due_predictions ${sqlState(lockErr)}`);
    return jsonError(500, "lock_due_predictions failed");
  }

  // Through the serialising wrapper, never the raw function (whose EXECUTE is revoked from
  // service_role): overlapping runs would each spend the full daily cap, because each sums "spent
  // today" without seeing the other's uncommitted credits. See 20260914000001_settlement_guards.sql.
  // A run that finds another in progress returns 0 immediately; the next tick picks up the rest.
  const { data: settled, error: settleErr } = await admin.rpc("settle_due_predictions_serialized");
  if (settleErr) {
    console.error(`cron/tick: settle_due_predictions_serialized ${sqlState(settleErr)}`);
    return jsonError(500, "settle_due_predictions_serialized failed", { locked: Number(locked ?? 0) });
  }

  let payouts: PayoutSummary;
  try {
    const budgetMs = Math.max(0, Math.min(PAYOUT_BUDGET_MS, REQUEST_DEADLINE_MS - (Date.now() - requestStartedAt)));
    payouts = await runPayoutWorker(admin, { budgetMs });
  } catch {
    // runPayoutWorker is written never to throw; if it ever does, settlement has already committed
    // and the tick still succeeds, with the payout step reported as having not run.
    console.error("cron/tick: payout worker threw");
    payouts = {
      enabled: false,
      reason: "worker_error",
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

  return jsonOk({ locked: Number(locked ?? 0), settled: Number(settled ?? 0), payouts });
}

export async function POST(request: Request): Promise<Response> {
  return handle(request);
}

export async function GET(request: Request): Promise<Response> {
  return handle(request);
}
