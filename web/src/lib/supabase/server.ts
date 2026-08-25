import { createServerClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";
import { getSupabaseEnv } from "@/lib/env";

// Server-side read-only client. There is no auth session on this site (public read-only data
// under RLS), so the cookie adapter is a no-op. Every request is no-store with a short timeout:
// live pages must render their honest offline state quickly when Supabase is unreachable,
// never hang or serve stale data.
const FETCH_TIMEOUT_MS = 4000;

export function createServerSupabase(): SupabaseClient | null {
  const env = getSupabaseEnv();
  if (!env) return null;
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
          signal: init?.signal ?? AbortSignal.timeout(FETCH_TIMEOUT_MS),
        }),
    },
  });
}
