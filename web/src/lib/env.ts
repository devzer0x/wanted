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
