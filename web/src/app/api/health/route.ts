import { getSupabaseEnv } from "@/lib/env";

export const dynamic = "force-dynamic";

// Honest health: ok = this app is serving; supabase_configured = browser credentials are
// present in the environment. Reachability of Supabase itself is a separate concern surfaced
// in the UI (offline/data-link states) — this endpoint reports only what it knows.
export function GET(): Response {
  return Response.json(
    { ok: true, supabase_configured: getSupabaseEnv() !== null },
    { headers: { "cache-control": "no-store" } }
  );
}
