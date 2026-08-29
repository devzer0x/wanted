import { createServerClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";
import { getSupabaseEnv } from "@/lib/env";

// Server-side read-only client. There is no auth session on this site (public read-only data
// under RLS), so the cookie adapter is a no-op. Every request is no-store with a short deadline:
// live pages must render their honest offline state quickly when Supabase is unreachable,
// never hang or serve stale data.
const FETCH_TIMEOUT_MS = 4000;

/**
 * One deadline per client, shared by every attempt of the operation it serves.
 *
 * postgrest-js retries network failures internally (3 retries with backoff — measured at 7.0 s
 * against a refused connection, @supabase/postgrest-js 2.109.0). Two details make the obvious
 * implementation useless there: a signal created per attempt restarts the clock on every retry,
 * and `AbortSignal.timeout()` aborts with a **TimeoutError**, which postgrest-js does not
 * recognise as an abort — it retries anyway and sleeps out the whole backoff chain. An
 * AbortController produces a genuine AbortError, which postgrest-js rethrows immediately.
 *
 * The timer is unref'd so a client whose query already resolved never holds the event loop open.
 */
function createDeadlineSignal(): AbortSignal {
  const controller = new AbortController();
  const timer: unknown = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  if (typeof (timer as { unref?: () => void })?.unref === "function") {
    (timer as { unref: () => void }).unref();
  }
  return controller.signal;
}

export function createServerSupabase(): SupabaseClient | null {
  const env = getSupabaseEnv();
  if (!env) return null;
  const deadline = createDeadlineSignal();
  return createServerClient(env.url, env.key, {
    cookies: {
      getAll: () => [],
      setAll: () => {},
    },
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
