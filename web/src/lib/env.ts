// Supabase browser credentials. Both current-generation (PUBLISHABLE_KEY) and legacy (ANON_KEY)
// names are accepted; the publishable name wins when both are set. NEXT_PUBLIC_* values are
// inlined at build time only via static member access, so each variable is referenced literally.

export interface SupabaseEnv {
  url: string;
  key: string;
}

export function getSupabaseEnv(): SupabaseEnv | null {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key =
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ||
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) return null;
  return { url, key };
}

const LOCAL_SITE_URL = "http://localhost:3000";

/**
 * Canonical origin for absolute URLs (metadataBase, share cards).
 *
 * Falls back to Vercel's `VERCEL_PROJECT_PRODUCTION_URL` — a system env var set on every Vercel
 * deployment (build and runtime), holding the shortest production domain without a scheme
 * (https://vercel.com/docs/environment-variables/system-environment-variables, fetched
 * 2026-08-29). Without that fallback an unset NEXT_PUBLIC_SITE_URL makes Next's metadataBase
 * default to localhost, and every og:image URL served from production points at localhost.
 */
export function getSiteUrl(): string {
  const explicit = process.env.NEXT_PUBLIC_SITE_URL?.trim();
  if (explicit) return explicit.replace(/\/+$/, "");
  const vercelDomain = process.env.VERCEL_PROJECT_PRODUCTION_URL?.trim();
  if (vercelDomain) return `https://${vercelDomain.replace(/\/+$/, "")}`;
  return LOCAL_SITE_URL;
}
