import { defineConfig, devices } from "@playwright/test";

// Two run modes, selected by environment:
//
//   (default)                      build and serve the app on LOCAL_PORT with web/.env.local (the
//                                  real Supabase project) and run e2e/site.spec.ts against it.
//   PLAYWRIGHT_BASE_URL=https://…  run the same suite against a deployed site; no local server
//                                  is started at all.
//   PLAYWRIGHT_OFFLINE=1           serve locally with an unroutable Supabase URL and run
//                                  e2e/offline.spec.ts, proving the honest data-link-down states.
//
// The last two are mutually exclusive: a deployed site's backend cannot be made unreachable from
// here, so combining them would test nothing and silently pass.
//
// The local server is a production build, not `next dev`: dev overrides `metadataBase` with
// http://localhost:<port> and compiles routes lazily, so share-card URLs and first-hit timing
// would not be the ones that ship.

const LOCAL_PORT = Number(process.env.PLAYWRIGHT_PORT ?? 4311);
const externalBaseURL = (process.env.PLAYWRIGHT_BASE_URL ?? "").trim().replace(/\/+$/, "");
const offlineMode = process.env.PLAYWRIGHT_OFFLINE === "1";

if (externalBaseURL && offlineMode) {
  throw new Error(
    "PLAYWRIGHT_OFFLINE=1 cannot be combined with PLAYWRIGHT_BASE_URL: the offline suite needs a " +
      "local server pointed at an unreachable Supabase, which a deployed site cannot provide."
  );
}

const baseURL = externalBaseURL || `http://127.0.0.1:${LOCAL_PORT}`;

// Nothing listens here, so every Supabase request fails fast with ECONNREFUSED rather than
// hanging — the site must then render its honest offline/data-link-down states.
const UNREACHABLE_SUPABASE_URL = "http://127.0.0.1:54329";

// Next.js does not overwrite variables already present in process.env, so these win over
// .env.local for the spawned server.
//
// NEXT_PUBLIC_* values are inlined at build time, so NEITHER suite may build into `.next`:
//   - both suites bake NEXT_PUBLIC_SITE_URL=http://127.0.0.1:<port>, which would leave a later
//     `npm run start` serving canonical/og:url/og:image URLs pointing at the test port;
//   - the offline suite additionally bakes an unroutable NEXT_PUBLIC_SUPABASE_URL.
// `.next` therefore belongs exclusively to `npm run build` / `npm run start`, and each suite gets
// its own output directory (consumed by next.config.ts, honoured by both `next build` and
// `next start`). Both are gitignored, and both are pre-declared in tsconfig.json's `include` so
// that `next build` has no reason to rewrite that tracked file (it only edits tsconfig when
// `<distDir>/types/**/*.ts` is missing from `include`).
const DIST_DIR = offlineMode ? ".next-offline" : ".next-e2e";

const serverEnv: Record<string, string> = {
  NEXT_PUBLIC_SITE_URL: baseURL,
  NEXT_DIST_DIR: DIST_DIR,
  ...(offlineMode
    ? {
        NEXT_PUBLIC_SUPABASE_URL: UNREACHABLE_SUPABASE_URL,
        // The offline suite asserts the stream panel's "NO SIGNAL" state, which means BOTH
        // sources of a stream config are absent: the site_config row (unreachable here by
        // construction) and the server-side env fallback. That fallback is
        // STREAM_PROVIDER/STREAM_CHANNEL in .env.local — an operator file, gitignored, whose
        // contents vary by machine. Left to inherit, the suite passes or fails on whether the
        // operator happens to have configured a channel, which is not what it is testing.
        // Blanking them here makes the premise the assertion states actually hold.
        STREAM_PROVIDER: "",
        STREAM_CHANNEL: "",
      }
    : {}),
};

const testMatch = offlineMode ? /offline\.spec\.ts/ : /site\.spec\.ts/;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  // Every page in the live suite is `force-dynamic` and fans out into several Supabase queries
  // bounded by a 4 s deadline (src/lib/data.ts). One default worker per core aimed a dozen
  // concurrent request-time renders at a single Supabase project, and the queries started
  // aborting — the site then correctly reported "data link down", and tests asserting live data
  // failed for a reason that has nothing to do with the site. Capping concurrency fixes the
  // contention at its source; the offline suite has no backend to contend for and keeps the
  // default.
  workers: offlineMode ? undefined : 3,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      testMatch,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "mobile",
      testMatch,
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 390, height: 844 },
        deviceScaleFactor: 3,
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
  webServer: externalBaseURL
    ? undefined
    : {
        command: `npm run build && npm run start -- --port ${LOCAL_PORT}`,
        url: `http://127.0.0.1:${LOCAL_PORT}/api/health`,
        // A reused server would carry the wrong Supabase URL for the mode being tested.
        reuseExistingServer: false,
        env: serverEnv,
        timeout: 300_000,
      },
});
