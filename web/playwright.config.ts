import { readFileSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";

// Three run modes, selected by environment:
//
//   (default)                      build and serve the app on LOCAL_PORT with web/.env.local (the
//                                  real Supabase project) and run e2e/site.spec.ts against it.
//   PLAYWRIGHT_BASE_URL=https://…  run the same suite against a deployed site; no local server
//                                  is started at all.
//   PLAYWRIGHT_OFFLINE=1           serve locally with an unroutable Supabase URL and run
//                                  e2e/offline.spec.ts, proving the honest data-link-down states.
//   PLAYWRIGHT_PREDICT_LOCAL=1     serve locally against web/scripts/predict-verify's real
//                                  Postgres + PostgREST stack (which must already be up) and run
//                                  e2e/predictLocal.spec.ts.
//
// The last three are mutually exclusive: a deployed site's backend cannot be made unreachable or
// repointed at a local docker stack from here, so combining any two of them would test nothing and
// silently pass.
//
// The local server is a production build, not `next dev`: dev overrides `metadataBase` with
// http://localhost:<port> and compiles routes lazily, so share-card URLs and first-hit timing
// would not be the ones that ship.

const LOCAL_PORT = Number(process.env.PLAYWRIGHT_PORT ?? 4311);
const externalBaseURL = (process.env.PLAYWRIGHT_BASE_URL ?? "").trim().replace(/\/+$/, "");
const offlineMode = process.env.PLAYWRIGHT_OFFLINE === "1";
const predictLocalMode = process.env.PLAYWRIGHT_PREDICT_LOCAL === "1";

if ([externalBaseURL !== "", offlineMode, predictLocalMode].filter(Boolean).length > 1) {
  throw new Error(
    "PLAYWRIGHT_BASE_URL, PLAYWRIGHT_OFFLINE=1 and PLAYWRIGHT_PREDICT_LOCAL=1 are mutually " +
      "exclusive: a deployed site's backend cannot be made unreachable, and cannot be repointed " +
      "at a local docker stack, from here."
  );
}

/**
 * The env file web/scripts/predict-verify/up.sh writes once its stack is ready: the service-role
 * SUPABASE_URL/SUPABASE_SECRET_KEY pair for THIS run's throwaway PostgREST JWT secret, which only
 * up.sh knows (it mints the JWT). Fixed path, same convention as
 * web/scripts/verify-payout.mjs's STACK_ENV for web/scripts/payout-stack.
 */
function readPredictVerifyEnv(): Record<string, string> {
  const path = "/tmp/web-f5-predict-verify/env";
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    throw new Error(
      `PLAYWRIGHT_PREDICT_LOCAL=1 requires ${path}, which does not exist. Run ` +
        "web/scripts/predict-verify/up.sh first (and web/scripts/predict-verify/down.sh when done)."
    );
  }
  const env: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    const match = /^export\s+([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (match) env[match[1]] = match[2];
  }
  // Fail closed. Next.js fills any variable still undefined from web/.env.local, which holds the
  // PRODUCTION service-role pair; a file missing either line would silently point this suite's
  // writes (POST /enter) at production. Refuse unless both are present and the URL is loopback.
  const url = env.SUPABASE_URL ?? "";
  if (!env.SUPABASE_SECRET_KEY || !/^http:\/\/(127\.0\.0\.1|localhost):\d+\/?$/.test(url)) {
    throw new Error(
      `${path} must export SUPABASE_URL=http://127.0.0.1:<port> and SUPABASE_SECRET_KEY; got ` +
        `SUPABASE_URL=${JSON.stringify(url)}. Re-run web/scripts/predict-verify/up.sh.`
    );
  }
  return env;
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
// `next start`). All are gitignored, and all are pre-declared in tsconfig.json's `include` so
// that `next build` has no reason to rewrite that tracked file (it only edits tsconfig when
// `<distDir>/types/**/*.ts` is missing from `include`).
const DIST_DIR = predictLocalMode ? ".next-predict-local" : offlineMode ? ".next-offline" : ".next-e2e";

const serverEnv: Record<string, string> = {
  NEXT_PUBLIC_SITE_URL: baseURL,
  NEXT_DIST_DIR: DIST_DIR,
  // The SIWE origin is pinned server-side and a production build refuses to sign a loopback
  // domain (src/app/api/_lib/siweDomain.ts). Both suites serve a production build on 127.0.0.1,
  // so without this every /api/auth/nonce here answers 503 "sign-in is not configured" and the
  // wallet suite could never reach the signature path at all — it would be skipping for a reason
  // that has nothing to do with what it is testing. `.env.example` documents this variable as
  // exactly that: "Set this ONLY to run a production build locally for verification." It applies
  // to this spawned local server only; a PLAYWRIGHT_BASE_URL run starts no server and is
  // unaffected, and no deployment sets it.
  ALLOW_LOOPBACK_SIWE: "1",
  ...(offlineMode
    ? {
        NEXT_PUBLIC_SUPABASE_URL: UNREACHABLE_SUPABASE_URL,
        // The SERVER-side pair too. Left undefined, Next.js fills both from web/.env.local — the
        // production service-role key — and /api/predictions/live and /api/leaderboard, which
        // offline.spec.ts visits, would read production while this suite claims to be offline.
        SUPABASE_URL: UNREACHABLE_SUPABASE_URL,
        SUPABASE_SECRET_KEY: "offline-suite-not-a-key",
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
    : predictLocalMode
      ? {
          // web/scripts/predict-verify/up.sh: real Postgres 16 + real PostgREST v12.2.3, every
          // infra/supabase/migrations applied, seeded with the fixed-id predictions
          // e2e/predictLocal.spec.ts drives. Supplies SUPABASE_URL + SUPABASE_SECRET_KEY, the only
          // two names web/src/app/api/_lib/supabaseAdmin.ts reads.
          ...readPredictVerifyEnv(),
          // src/lib/auth/session.ts refuses to sign a wallet session cookie without this. The
          // stack binds 127.0.0.1 only and predict-verify/down.sh destroys it after every run,
          // so a fixed dev value costs nothing.
          SESSION_SECRET: "wanted-web-f5-predict-verify-session-secret-local-only",
          // The verified TTWO deployment (docs/CONTRACTS-PREDICTIONS.md §1) — needed so
          // rewardAsset() resolves and the tiny/large reward_ledger credits
          // e2e/predictLocal.spec.ts seeds convert to base units at the real 18-decimal scale, the
          // same as production. No chain call is ever made against it from this suite: nothing
          // here reads a balance or sends a transaction.
          NEXT_PUBLIC_TTWO_TOKEN: "0x5e81213613b6B86EaB4c6c50d718d34359459786",
          // The browser/SSR client is pointed at nothing, explicitly: left undefined, Next.js
          // would fill NEXT_PUBLIC_SUPABASE_URL and the publishable key from web/.env.local, i.e.
          // the production project. src/lib/data.ts and src/components/live/LiveDashboard.tsx
          // then show their honest "data link down" state (as in offline.spec.ts) instead of a
          // Realtime server this stack does not run; predictLocal.spec.ts asserts only on the
          // service-role-backed prediction routes and pages.
          NEXT_PUBLIC_SUPABASE_URL: UNREACHABLE_SUPABASE_URL,
        }
      : {}),
};

// The default suite is site.spec.ts (pages against a reachable Supabase) plus wallet.spec.ts
// (the connect/switch-network/sign-in flow, driven by the injected EIP-1193 provider in
// e2e/walletHarness.ts). wallet.spec.ts stays out of the offline run on purpose: it asserts what
// the REAL auth routes answer, and with Supabase unroutable every one of them would report the
// same 503/500 regardless of what the wallet did, so it would prove nothing there.
// predictLocal.spec.ts stays out of both — it needs the local docker stack's fixed-id rows, which
// neither the real project nor an unroutable URL provides.
const testMatch = predictLocalMode
  ? /predictLocal\.spec\.ts/
  : offlineMode
    ? /offline\.spec\.ts/
    : /(site|wallet)\.spec\.ts/;

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
