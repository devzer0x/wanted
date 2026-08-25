"use client";

import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";
import { getSupabaseEnv } from "@/lib/env";

let browserClient: SupabaseClient | null | undefined;

/** Singleton browser client; null when env is not configured (site renders offline state). */
export function getBrowserSupabase(): SupabaseClient | null {
  if (browserClient !== undefined) return browserClient;
  const env = getSupabaseEnv();
  browserClient = env ? createBrowserClient(env.url, env.key) : null;
  return browserClient;
}
