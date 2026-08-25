import { getSupabaseEnv } from "@/lib/env";

// Public-bucket object URL (CONTRACTS §5: clips/shots buckets are public read). Pure string
// construction — no network — so it is safe during server render even when Supabase is down.
export function publicObjectUrl(bucket: "clips" | "shots", storagePath: string): string | null {
  const env = getSupabaseEnv();
  if (!env) return null;
  const base = env.url.replace(/\/+$/, "");
  const path = storagePath
    .replace(/^\/+/, "")
    .split("/")
    .map(encodeURIComponent)
    .join("/");
  return `${base}/storage/v1/object/public/${bucket}/${path}`;
}
