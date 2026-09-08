// /api/cron/tick — CONTRACTS-PREDICTIONS.md §4: "secret-gated; locks due predictions, settles due
// windows." Calls `lock_due_predictions()` then `settle_due_predictions()`, the two SQL entry
// points named for this in the task brief. No settlement logic lives here — an unknown
// `telemetry_rule.kind` or a heartbeat gap voiding a prediction is entirely `settle_due_predictions`'
// job (CONTRACTS-PREDICTIONS §3), not this route's.
//
// METHOD NOTE (verified against Vercel's own docs, 2026-09-08, not guessed — CLAUDE.md rule 6):
// the task's contract table lists this route as POST, but Vercel Cron Jobs always invoke the
// configured path with an HTTP GET (https://vercel.com/docs/cron-jobs — "Vercel makes an HTTP GET
// request... Vercel Functions triggered by a cron job... will always contain `vercel-cron/1.0` as
// the user agent"); there is no way to configure Vercel to send POST. Both are handled here,
// identically secret-gated, so the route is reachable the way Vercel will actually call it (GET,
// with `CRON_SECRET` sent automatically as `Authorization: Bearer <value>` — Vercel's own
// documented behaviour) and the way the contract documents it (POST, for manual/ops-triggered
// runs with the same bearer secret). Whoever wires up `vercel.json` (outside this owned directory)
// should point its cron schedule at this path; Vercel's GET request will authenticate correctly.
//
// The secret check is the bearer token only — never the mere presence of an `x-vercel-cron-*`
// header, which is not a secret and could be sent by anyone.

import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured } from "../../_lib/http";

export const dynamic = "force-dynamic";

function authorized(request: Request): boolean {
  const secret = process.env.CRON_SECRET;
  if (!secret) return false; // an unset secret must never mean "open"
  const header = request.headers.get("authorization");
  return header === `Bearer ${secret}`;
}

async function handle(request: Request): Promise<Response> {
  if (!authorized(request)) return jsonError(401, "unauthorized");

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  const { data: locked, error: lockErr } = await admin.rpc("lock_due_predictions");
  if (lockErr) {
    return jsonError(500, "lock_due_predictions failed", { detail: lockErr.message });
  }

  const { data: settled, error: settleErr } = await admin.rpc("settle_due_predictions");
  if (settleErr) {
    return jsonError(500, "settle_due_predictions failed", {
      detail: settleErr.message,
      locked,
    });
  }

  return jsonOk({ locked: Number(locked ?? 0), settled: Number(settled ?? 0) });
}

export async function POST(request: Request): Promise<Response> {
  return handle(request);
}

export async function GET(request: Request): Promise<Response> {
  return handle(request);
}
