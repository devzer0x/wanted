import { expect, test } from "@playwright/test";

// Supabase is intentionally unreachable in this environment (.env.local → 127.0.0.1:54329).
// The site must render honest offline/empty states everywhere — no crash, no fabricated data.

test.describe("honest offline state (Supabase unreachable)", () => {
  test("/api/health reports ok and configured honestly", async ({ request }) => {
    const res = await request.get("/api/health");
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.ok).toBe(true);
    // env IS configured (to an unreachable host) — health reports configuration, not reachability
    expect(body.supabase_configured).toBe(true);
  });

  test("live page shows the offline banner and honest empty feed", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("offline-banner")).toBeVisible();
    await expect(page.getByTestId("offline-banner")).toContainText(/offline/i);
    await expect(page.getByTestId("feed-empty")).toBeVisible();
    await expect(page.getByTestId("feed-empty")).toContainText(/data link down/i);
    // stream is unconfigured here → honest no-signal panel instead of a player
    await expect(page.getByText("NO SIGNAL").first()).toBeVisible();
  });

  test("missions page renders with honest empty state", async ({ page }) => {
    await page.goto("/missions");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("MISSIONS");
    await expect(page.getByTestId("missions-empty")).toContainText(/data link down/i);
  });

  test("clips page renders with honest empty state", async ({ page }) => {
    await page.goto("/clips");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("CLIPS");
    await expect(page.getByTestId("clips-empty")).toContainText(/data link down/i);
  });

  test("per-clip OG image renders even with the database unreachable", async ({ request }) => {
    const res = await request.get("/clips/1/opengraph-image");
    expect(res.status()).toBe(200);
    expect(res.headers()["content-type"]).toContain("image/png");
  });

  test("the agent page carries the AI disclosure and non-affiliation", async ({ page }) => {
    await page.goto("/agent");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("WANTED");
    await expect(page.getByText("the agent is an AI.", { exact: false }).first()).toBeVisible();
    await expect(
      page.getByText(/not affiliated with, endorsed by, or connected to/i).first()
    ).toBeVisible();
  });

  test("footer disclosure is on every page", async ({ page }) => {
    // Visits four routes in one test; allow for cold dev-server compiles.
    test.setTimeout(120_000);
    for (const path of ["/", "/missions", "/clips", "/agent"]) {
      await page.goto(path);
      await expect(
        page
          .locator("footer")
          .getByText(/not affiliated with, endorsed by, or connected to Rockstar Games/i)
      ).toBeVisible();
      await expect(
        page.locator("footer").getByText(/the agent is an AI; all commentary/i)
      ).toBeVisible();
    }
  });
});

test.describe("mobile viewport 390x844", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  for (const path of ["/", "/missions", "/clips", "/agent"]) {
    test(`${path} renders without horizontal scroll`, async ({ page }) => {
      await page.goto(path);
      await expect(page.locator("footer")).toBeVisible();
      const overflow = await page.evaluate(() => {
        const el = document.scrollingElement ?? document.documentElement;
        return el.scrollWidth - el.clientWidth;
      });
      expect(overflow).toBeLessThanOrEqual(1);
    });
  }

  test("offline banner is visible on mobile", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("offline-banner")).toBeVisible();
  });
});
