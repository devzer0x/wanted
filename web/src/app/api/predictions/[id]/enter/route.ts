// POST /api/predictions/[id]/enter — CONTRACTS-PREDICTIONS.md §4.
// {outcome} -> 201, or 409 after locks_at.
//
// Security property #2: the lock check is entirely inside `enter_prediction`'s SQL (per
// CONTRACTS-PREDICTIONS.md §4's own INSERT ... WHERE now() < p.locks_at), called via `.rpc()`.
// This route never reads or compares `locks_at` itself, and never reads server or client
// timestamps to make that decision — a `false` return (zero rows inserted) is the only signal, and
// it maps directly to 409. A client clock, a paused tab, or a replayed request cannot beat it.
//
// Security property #1: the wallet comes from `readSession()` (the verified cookie) only — the
// request body only ever supplies `outcome`, never a wallet address.

import { readSession } from "@/lib/auth/session";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured, getClientIp, readJsonBody } from "../../../_lib/http";
import { rateLimit, RATE_LIMITS } from "../../../_lib/rateLimit";

export const dynamic = "force-dynamic";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface EnterBody {
  outcome?: unknown;
}

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function POST(request: Request, { params }: RouteContext): Promise<Response> {
  const { id } = await params;
  if (!UUID_RE.test(id)) return jsonError(400, "invalid prediction id");

  const session = await readSession();
  if (!session?.address) return jsonError(401, "not authenticated");
  const wallet = session.address;

  const ip = getClientIp(request);
  const limited = rateLimit(`enter:${wallet}:${ip}`, RATE_LIMITS.enter);
  if (!limited.allowed) {
    return jsonError(429, "too many requests", { retryAfterMs: limited.retryAfterMs });
  }

  const body = await readJsonBody<EnterBody>(request);
  const outcome = typeof body?.outcome === "string" ? body.outcome.trim() : "";
  if (!outcome) return jsonError(400, "outcome is required");

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  const { data, error } = await admin.rpc("enter_prediction", {
    p_id: id,
    p_wallet: wallet,
    p_outcome: outcome,
  });

  if (error) {
    // 23505 = unique_violation on (prediction_id, wallet) — already entered.
    if ((error as { code?: string }).code === "23505") {
      return jsonError(409, "already entered this prediction");
    }
    return jsonError(500, "enter_prediction failed", { detail: error.message });
  }

  if (data !== true) {
    return jsonError(409, "prediction is locked, unknown, or the outcome is invalid");
  }

  return jsonOk({ ok: true }, 201);
}
