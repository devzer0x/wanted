import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // NEXT_PUBLIC_* values are baked into the client bundle at build time, so neither Playwright
  // suite may share an output directory with the normal build: both bake the test origin into
  // canonical/og:url/og:image, and the offline suite also bakes an unroutable Supabase URL.
  // `.next` stays reserved for `npm run build`/`npm run start`; the suites use `.next-e2e` and
  // `.next-offline`. The env var is set only by playwright.config.ts, and every value it can
  // take is pre-declared in tsconfig.json's `include` so builds never rewrite that tracked file.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  poweredByHeader: false,
};

export default nextConfig;
