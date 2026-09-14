// Service-role Supabase client for API routes only.
//
// This module lives under `app/api/_lib/` — never under `src/lib/` (owned by a parallel
// workstream) and never importable from a "use client" boundary — so the secret key it reads
// can never be bundled into browser code (CONTRACTS-PREDICTIONS.md §6 / §2 RLS notes:
// wallet_sessions, reward_ledger, reward_claims, policy_flags have NO public policy at all and
// are reachable only with the service-role key, server-side).
//
// Env var names match the rest of the repo (harness/.env, infra/.env.cloud): `SUPABASE_URL` +
// `SUPABASE_SECRET_KEY` — not the legacy `SUPABASE_SERVICE_ROLE_KEY` name, and not the
// `NEXT_PUBLIC_*` browser pair read by src/lib/env.ts (getSupabaseEnv). Verified against
// harness/.env.example and infra/.env.cloud, both of which use SUPABASE_URL + SUPABASE_SECRET_KEY
// for the same "server-side writes bypass RLS" role this file needs.
//
// NOTE (found during verification, 2026-09-08): as of this writing `web/.env.local` — which the
// task brief that produced this file said already held "the real Supabase project" credentials —
// contains only the browser-safe NEXT_PUBLIC_* pair. SUPABASE_URL / SUPABASE_SECRET_KEY are not
// present there; they exist in `infra/.env.cloud` (a directory this workstream does not own).
// Every route below fails honestly (503, see `adminUnavailable()`) rather than fake success when
// these are unset, which is the correct behaviour whether the cause is "not deployed yet" or
// "briefly misconfigured".

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

const FETCH_TIMEOUT_MS = 8000;

/**
 * A fetch whose EVERY call gets its own fresh deadline (CONTRACTS-PREDICTIONS §10.5, FM-04).
 *
 * The old client armed one deadline at construction and shared it with every later call, so a
 * request that spent 8 s on anything — lock, settle, a chain read — found every write after that
 * pre-aborted. postgrest-js turns an abort into a returned `{error}` rather than a throw, so a lost
 * write looked exactly like a slow one. Now each fetch starts its own clock.
 *
 * Same technique as `src/lib/supabase/server.ts` (duplicated here because this module intentionally
 * does not import from `src/lib/**`): a MANUAL AbortController, never `AbortSignal.timeout()`.
 * postgrest-js 2.109 rethrows a genuine AbortError immediately, while `AbortSignal.timeout()` aborts
 * with a TimeoutError it does not recognise as an abort, so for GETs it would retry and sleep out the
 * whole backoff chain (node_modules/@supabase/postgrest-js/dist/index.mjs, executeWithRetry: only
 * GET/HEAD/OPTIONS are ever retried, and never after an AbortError — so an RPC POST is one attempt
 * with one deadline). The timer is left to run out rather than cleared when headers arrive, because
 * the body is read after fetch() resolves and must stay under the same deadline; aborting an
 * already-consumed response is a no-op, and the timer is unref'd so it never holds the event loop.
 */
function fetchWithDeadline(timeoutMs: number) {
  return (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const controller = new AbortController();
    const timer: unknown = setTimeout(() => controller.abort(), timeoutMs);
    if (typeof (timer as { unref?: () => void })?.unref === "function") {
      (timer as { unref: () => void }).unref();
    }
    return fetch(input, {
      ...init,
      cache: "no-store",
      signal: init?.signal ? AbortSignal.any([init.signal, controller.signal]) : controller.signal,
    });
  };
}

export function isSupabaseAdminConfigured(): boolean {
  return Boolean(process.env.SUPABASE_URL && process.env.SUPABASE_SECRET_KEY);
}

/**
 * Null when service-role credentials are absent. Callers must fail honestly (503), never fake data.
 * `timeoutMs` is the per-fetch deadline (default 8 s).
 */
export function createSupabaseAdmin(opts?: { timeoutMs?: number }): SupabaseClient | null {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SECRET_KEY;
  if (!url || !key) return null;
  const timeoutMs =
    opts?.timeoutMs !== undefined && Number.isFinite(opts.timeoutMs) && opts.timeoutMs > 0
      ? opts.timeoutMs
      : FETCH_TIMEOUT_MS;
  return createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
    global: { fetch: fetchWithDeadline(timeoutMs) },
  });
}
