import { expect, test } from "@playwright/test";

// PLAYWRIGHT_OFFLINE=1 boots the app against an unroutable Supabase URL (see playwright.config).
// Every request to the database fails for real — this is the honest-degradation contract:
// the site must still serve, must say what it does not know, and must never invent a feed.

const ROUTES = ["/", "/missions", "/clips", "/agent"];

test.setTimeout(90_000);

test("/api/health still serves and reports configuration, not reachability", async ({
  request,
}) => {
  const res = await request.get("/api/health");
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.ok).toBe(true);
  // Credentials ARE configured (at a host nothing answers on); health reports configuration.
  expect(body.supabase_configured).toBe(true);
});

test("live page says NO DATA and shows an empty feed, not stale content", async ({ page }) => {
  await page.goto("/");
  const banner = page.getByTestId("offline-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toHaveAttribute("data-state", "no-data");
  await expect(banner).toContainText(/can't reach the telemetry database/i);

  await expect(page.getByTestId("feed-empty")).toBeVisible();
  await expect(page.getByTestId("feed-empty")).toContainText(/data link down/i);
  await expect(page.getByTestId("feed-decision")).toHaveCount(0);
  await expect(page.getByTestId("feed-event")).toHaveCount(0);

  // The AI disclosure is standing, not conditional on data being available.
  await expect(page.getByTestId("feed-status")).toContainText(/AI-generated/i);

  // With no site_config row reachable and no env fallback, the stream panel is honest.
  await expect(page.getByTestId("stream-status")).toHaveAttribute("data-status", "no-signal");
  await expect(page.getByText("NO SIGNAL").first()).toBeVisible();
  await expect(page.getByTestId("stream-player")).toHaveCount(0);
});

test("counters and panels show em dashes rather than zeros they cannot verify", async ({
  page,
}) => {
  await page.goto("/");
  const counters = page.getByRole("region", { name: "Session counters" });
  await expect(counters).toContainText("—");
  await expect(page.getByRole("region", { name: "the agent's in-game status" })).toContainText(
    /no telemetry on record/i
  );
  await expect(page.getByRole("region", { name: "Current goal" })).toContainText(
    /no goal on record/i
  );
});

test("missions page renders with an honest data-link-down state", async ({ page }) => {
  await page.goto("/missions");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("MISSIONS");
  await expect(page.getByTestId("missions-empty")).toContainText(/data link down/i);
});

test("clips page renders with an honest data-link-down state", async ({ page }) => {
  await page.goto("/clips");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("CLIPS");
  await expect(page.getByTestId("clips-empty")).toContainText(/data link down/i);
});

test("per-clip page and OG image survive an unreachable database", async ({ page, request }) => {
  await page.goto("/clips/1");
  await expect(page.getByText(/data link down/i).first()).toBeVisible();

  const res = await request.get("/clips/1/opengraph-image");
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toContain("image/png");
});

// The live suite can only assert clip-permalink metadata when the database happens to hold a
// clip. Here the database is unreachable by construction, and /clips/1 still renders (honest
// data-link-down state rather than a 404) — so this is the deterministic guard that a clip
// permalink advertises ITSELF and not the homepage, which is what Next's per-segment
// replacement of `openGraph`/`alternates` silently breaks.
test("a clip permalink advertises itself, not the homepage", async ({ page }) => {
  await page.goto("/clips/1");
  const origin = new URL(page.url()).origin;

  const meta = await page.evaluate(() => {
    const attr = (selector: string, name: string) =>
      document.querySelector(selector)?.getAttribute(name) ?? null;
    return {
      title: document.title,
      ogTitle: attr('meta[property="og:title"]', "content"),
      ogUrl: attr('meta[property="og:url"]', "content"),
      canonical: attr('link[rel="canonical"]', "href"),
      ogImage: attr('meta[property="og:image"]', "content"),
    };
  });

  const want = `${origin}/clips/1`;
  expect(meta.title).toBe("Clip #1 · WASTED");
  expect(meta.ogTitle).toBe("Clip #1 · WASTED");
  expect((meta.ogUrl ?? "").replace(/\/+$/, "")).toBe(want);
  expect((meta.canonical ?? "").replace(/\/+$/, "")).toBe(want);
  expect(new URL(meta.ogImage as string).pathname).toBe("/clips/1/opengraph-image");
});

test("static routes each carry their own og:url and canonical while degraded", async ({ page }) => {
  const seen = new Set<string>();
  for (const path of ROUTES) {
    await page.goto(path);
    const origin = new URL(page.url()).origin;
    const meta = await page.evaluate(() => ({
      ogUrl: document.querySelector('meta[property="og:url"]')?.getAttribute("content") ?? null,
      canonical: document.querySelector('link[rel="canonical"]')?.getAttribute("href") ?? null,
    }));
    const want = `${origin}${path === "/" ? "" : path}`;
    expect((meta.ogUrl ?? "").replace(/\/+$/, ""), `og:url on ${path}`).toBe(want);
    expect((meta.canonical ?? "").replace(/\/+$/, ""), `canonical on ${path}`).toBe(want);
    seen.add(want);
  }
  expect(seen.size).toBe(ROUTES.length);
});

test("site-wide OG image survives an unreachable database", async ({ request }) => {
  const res = await request.get("/opengraph-image");
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toContain("image/png");
});

for (const path of ROUTES) {
  test(`${path} renders without horizontal scroll while degraded`, async ({ page }) => {
    await page.goto(path);
    await expect(page.locator("footer")).toBeVisible();
    const overflow = await page.evaluate(() => {
      const el = document.scrollingElement ?? document.documentElement;
      return el.scrollWidth - el.clientWidth;
    });
    expect(overflow).toBeLessThanOrEqual(1);
  });
}

test("footer disclosure survives degradation", async ({ page }) => {
  for (const path of ROUTES) {
    await page.goto(path);
    await expect(
      page
        .locator("footer")
        .getByText(/not affiliated with, endorsed by, or connected to Rockstar Games/i)
    ).toBeVisible();
  }
});
