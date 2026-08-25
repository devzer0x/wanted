import { defineConfig, devices } from "@playwright/test";

// E2E runs against `next dev` with web/.env.local pointing at an intentionally unreachable
// Supabase URL: with no backend reachable the site must show its honest offline/empty states.
// Realtime delivery cannot be exercised locally (no Realtime service exists here).
const PORT = 4311;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: `npm run dev -- --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
