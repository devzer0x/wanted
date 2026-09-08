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
 * One deadline per client, shared by every attempt of the operation it serves.
 *
 * Same technique as `src/lib/supabase/server.ts` (that file's `createDeadlineSignal`, duplicated
 * here because this module intentionally does not import from `src/lib/**`): postgrest-js retries
 * network failures internally, and `AbortSignal.timeout()` aborts with a TimeoutError that
 * postgrest-js does not recognise as an abort, so it retries anyway and sleeps out the whole
 * backoff chain. A manual AbortController produces a genuine AbortError, which postgrest-js
 * rethrows immediately. The timer is unref'd so a resolved client never holds the event loop open.
 */
function createDeadlineSignal(): AbortSignal {
  const controller = new AbortController();
  const timer: unknown = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  if (typeof (timer as { unref?: () => void })?.unref === "function") {
    (timer as { unref: () => void }).unref();
  }
  return controller.signal;
}

export function isSupabaseAdminConfigured(): boolean {
  return Boolean(process.env.SUPABASE_URL && process.env.SUPABASE_SECRET_KEY);
}

/** Null when service-role credentials are absent. Callers must fail honestly (503), never fake data. */
export function createSupabaseAdmin(): SupabaseClient | null {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SECRET_KEY;
  if (!url || !key) return null;
  const deadline = createDeadlineSignal();
  return createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
    global: {
      fetch: (input: RequestInfo | URL, init?: RequestInit) =>
        fetch(input, {
          ...init,
          cache: "no-store",
          signal: init?.signal ? AbortSignal.any([init.signal, deadline]) : deadline,
        }),
    },
  });
}
