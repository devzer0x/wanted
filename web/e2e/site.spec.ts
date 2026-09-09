import { expect, test, type Page } from "@playwright/test";

// Runs against a site whose Supabase IS reachable — a local production build wired to
// web/.env.local (built into .next-e2e, never .next), or a deployment via PLAYWRIGHT_BASE_URL.
// The database holds whatever it holds, so every assertion
// here is either structural (must hold at any data volume) or conditional on rows actually being
// present. Nothing asserts a fabricated row count.
//
// Tests tagged @current-build assert markup or metadata that exists in this working tree. They
// pass locally and against any deployment OF this tree; against an older deployment they fail by
// design, which is the signal that a redeploy is due. To health-check a deployment on its own
// terms, run with --grep-invert @current-build.

const ROUTES = ["/", "/predict", "/leaderboard", "/agent", "/missions", "/clips"];
const CONTRACT_MOODS = ["chill", "bored", "hyped", "scared", "smug"];

// Expected <title> === expected og:title for every route: they are produced from one string in
// src/lib/metadata.ts, and asserting them separately is what catches the two drifting apart.
const SHARE_ROUTES = [
  { path: "/", title: "WANTED — an AI plays GTA. Predict what happens next." },
  { path: "/predict", title: "Predict · WANTED" },
  { path: "/leaderboard", title: "Leaderboard · WANTED" },
  { path: "/agent", title: "Who's playing? · WANTED" },
  { path: "/missions", title: "Missions · WANTED" },
  { path: "/clips", title: "Clips · WANTED" },
];

test.describe.configure({ mode: "parallel" });
test.setTimeout(90_000);

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const el = document.scrollingElement ?? document.documentElement;
    return el.scrollWidth - el.clientWidth;
  });
}

interface HeadMetadata {
  title: string;
  ogTitle: string | null;
  ogUrl: string | null;
  canonical: string | null;
  ogImage: string | null;
}

async function headMetadata(page: Page): Promise<HeadMetadata> {
  return page.evaluate(() => {
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
}

// Origin + path with any trailing slash removed, so an assertion holds whether the host emits
// "https://site/clips" or "https://site/clips/".
function normalizeUrl(raw: string): string {
  const url = new URL(raw);
  const path = url.pathname.replace(/\/+$/, "");
  return `${url.origin}${path}`;
}

function expectedUrl(origin: string, path: string): string {
  return `${origin}${path === "/" ? "" : path.replace(/\/+$/, "")}`;
}

test("/api/health is honest about serving and configuration", async ({ request }) => {
  const res = await request.get("/api/health");
  expect(res.status()).toBe(200);
  expect(res.headers()["cache-control"]).toContain("no-store");
  const body = await res.json();
  expect(body.ok).toBe(true);
  expect(body.supabase_configured).toBe(true);
});

test("live page renders its shell with real rows or an honest empty state @current-build", async ({ page }) => {
  await page.goto("/");
  // The h1 is the proposition, not the wordmark: a screen-reader user and a search engine both
  // want to know what this page IS before they learn what it is called. The brand lives in the
  // nav landmark, so assert both in their own places rather than expecting one to carry the other.
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/AI plays GTA/i);
  await expect(
    page.locator("header").getByRole("link", { name: "WANTED home" })
  ).toBeVisible();

  const feed = page.getByRole("region", { name: "Live commentary feed" });
  await expect(feed).toBeVisible();

  const entries = feed.locator('[data-testid="feed-decision"], [data-testid="feed-event"]');
  const count = await entries.count();
  if (count === 0) {
    await expect(page.getByTestId("feed-empty")).toBeVisible();
  } else {
    // Every rendered entry must carry a parseable ISO timestamp from its row.
    const stamps = await feed.locator("time").evaluateAll((nodes) =>
      nodes.map((n) => n.getAttribute("datetime"))
    );
    expect(stamps.length).toBe(count);
    for (const stamp of stamps) {
      expect(stamp).toBeTruthy();
      expect(Number.isNaN(Date.parse(stamp as string))).toBe(false);
    }
  }
});

test("commentary entries expose the mood and the thought behind them @current-build", async ({ page }) => {
  await page.goto("/");
  const decisions = page.getByTestId("feed-decision");
  const count = await decisions.count();
  test.skip(count === 0, "no decision rows in this database yet");

  const first = decisions.first();
  // textContent, not innerText: the chip is rendered uppercase by CSS but the row value is not.
  const mood = ((await first.locator("span.ticker").first().textContent()) ?? "").trim();
  expect(CONTRACT_MOODS).toContain(mood);

  const toggle = first.getByRole("button", { name: /what he was thinking/i });
  if (await toggle.count()) {
    await toggle.click();
    await expect(first.getByText(/AI-generated/i)).toBeVisible();
  }
});

// Pausing the feed is exactly when a reader is dwelling on the commentary, so the standing
// "AI-generated" disclosure (CLAUDE.md §7) must survive the pause rather than be replaced by it.
test("the feed keeps its AI disclosure while paused @current-build", async ({ page }) => {
  await page.goto("/");
  const status = page.getByTestId("feed-status");
  await expect(status).toHaveAttribute("data-paused", "false");
  await expect(status).toContainText(/AI-generated/i);

  // Pause triggers on the feed's own scroll position, so the state needs a scrollable feed. How
  // many rows the database happens to hold is not ours to decide and must never be faked, so the
  // container is clamped instead — the same constraint a short window imposes. No row is added,
  // removed, or altered.
  const scroller = page.getByTestId("feed-scroll");
  await expect(scroller).toBeVisible();
  await scroller.evaluate((el) => {
    (el as HTMLElement).style.maxHeight = "60px";
  });
  // Polled, not measured once: under mobile emulation the element still reports 0x0 for a beat
  // after load, which would read as "not scrollable" and silently skip the assertion.
  await expect
    .poll(async () => scroller.evaluate((el) => el.scrollHeight - el.clientHeight), {
      message: "the clamped feed should become scrollable",
    })
    .toBeGreaterThan(24);

  await scroller.evaluate((el) => el.scrollTo({ top: 64 }));
  await expect(status).toHaveAttribute("data-paused", "true");
  await expect(status).toContainText(/AI-generated/i);
  await expect(status).toContainText(/paused/i);
});

// The public site does not publish what the agent's brain costs to run. Asserting on visible text
// alone would pass while the numbers still rode along inside the RSC payload, readable in
// view-source — so this checks the served bytes too. The site's queries name their columns
// (src/lib/columns.ts) precisely so the spend columns never reach the browser.
test("the live page publishes no running-cost figures @current-build", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.getByLabel("Brain running costs")).toHaveCount(0);
  await expect(page.getByText(/brain bill/i)).toHaveCount(0);
  await expect(page.getByText(/per hour/i)).toHaveCount(0);

  // The game-state panel replaced the old counters row. It must exist and must be built from
  // real telemetry, so assert the landmark rather than any particular number — the values change
  // with whatever the last real session wrote, and pinning one would make this test a fiction.
  await expect(page.getByRole("region", { name: "Agent game state" })).toBeVisible();

  const html = await (await request.get("/")).text();
  for (const column of ["cost_per_hour_usd", "cost_today_usd", "cost_usd", "cached_tokens"]) {
    expect(html, `${column} must not be shipped to the browser`).not.toContain(column);
  }
});

test("the agent page does not advertise running costs @current-build", async ({ request }) => {
  const html = await (await request.get("/agent")).text();
  expect(html).not.toMatch(/cost/i);
  expect(html).not.toMatch(/the meter is running/i);
});

// The feed and the offline banner show relative ages, which are a lie the moment they stop
// moving: a page left open must not still claim "12s ago" an hour later. The browser clock is
// moved forward (no row is invented or altered — the same real row is read from a later moment)
// and the rendered age must follow without a reload. setSystemTime jumps without firing the
// intervening timers, so this costs one tick, not a day of them.
test("relative ages keep ticking without a reload @current-build", async ({ page }) => {
  await page.clock.install({ time: new Date() });
  await page.goto("/");

  const stamps = page.locator('[data-testid="feed-decision"] time, [data-testid="feed-event"] time');
  test.skip((await stamps.count()) === 0, "no decision or event rows in this database yet");

  await page.clock.runFor(1200);
  const datetime = await stamps.first().getAttribute("datetime");
  const stamp = page.locator(`time[datetime="${datetime}"]`).first();
  const before = (await stamp.textContent())?.trim();

  // +24 h changes the label from any starting age (s→h, m→h/d, h→d, d→d+1).
  await page.clock.setSystemTime(new Date(Date.now() + 24 * 60 * 60 * 1000));
  await page.clock.runFor(1200);

  expect((await stamp.textContent())?.trim(), "the age must advance with the clock").not.toBe(before);

  // A heartbeat a day old is unambiguously stale, so the banner must now be up and saying so.
  const banner = page.getByTestId("offline-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(/off air|no data/i);
});

test("stream panel reports exactly one honest state @current-build", async ({ page }) => {
  await page.goto("/");
  const chip = page.getByTestId("stream-status");
  await expect(chip).toBeVisible();
  const status = await chip.getAttribute("data-status");
  expect(["no-signal", "off-air", "live"]).toContain(status);

  const player = page.getByTestId("stream-player");
  if (status === "no-signal") {
    await expect(page.getByText("NO SIGNAL").first()).toBeVisible();
    await expect(player).toHaveCount(0);
  } else {
    await expect(player).toHaveCount(1);
    const src = (await player.getAttribute("src")) ?? "";
    if (src.startsWith("https://player.twitch.tv/")) {
      // Twitch refuses to play unless `parent` is the exact embedding hostname.
      const parent = new URL(src).searchParams.get("parent");
      expect(parent).toBe(new URL(page.url()).hostname);
    } else {
      expect(src).toMatch(/^https:\/\/www\.youtube\.com\/embed\//);
    }
  }
});

test("the offline banner and the stream status agree @current-build", async ({ page }) => {
  await page.goto("/");
  const status = await page.getByTestId("stream-status").getAttribute("data-status");
  const bannerCount = await page.getByTestId("offline-banner").count();
  if (status === "live") {
    expect(bannerCount).toBe(0);
  } else if (bannerCount > 0) {
    await expect(page.getByTestId("offline-banner")).toContainText(/off air|no data/i);
  }
});

test("missions page renders rows or an honest empty state", async ({ page }) => {
  await page.goto("/missions");
  // The h1 text is what the DOM holds, not what CSS renders: these titles are set in the design
  // with `text-transform: uppercase`, so the visible sticker reads MISSIONS while the accessible
  // name — which is what assistive tech announces and what this asserts on — is "Story mode".
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Story mode");
  const empty = page.getByTestId("missions-empty");
  if (await empty.count()) {
    await expect(empty).toContainText(/nothing yet|data link down/i);
  } else {
    await expect(page.locator("article").first()).toBeVisible();
  }
});

test("clips page renders rows or an honest empty state", async ({ page }) => {
  await page.goto("/clips");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Clips");
  const empty = page.getByTestId("clips-empty");
  if (await empty.count()) {
    await expect(empty).toContainText(/no footage|data link down/i);
  } else {
    await expect(page.locator("article").first()).toBeVisible();
  }
});

test("agent page carries the AI disclosure and non-affiliation", async ({ page }) => {
  await page.goto("/agent");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Who's playing?");
  await expect(page.getByText("It's an AI", { exact: false }).first()).toBeVisible();
  await expect(
    page.getByText(/not affiliated with, endorsed by, or connected to/i).first()
  ).toBeVisible();
});

test("footer disclosure is on every page", async ({ page }) => {
  for (const path of ROUTES) {
    await page.goto(path);
    await expect(
      page
        .locator("footer")
        .getByText(/not affiliated with, endorsed by, or connected to Rockstar Games/i)
    ).toBeVisible();
    await expect(page.locator("footer").getByText(/The agent is an AI; all commentary/i)).toBeVisible();
  }
});

test("the navbar carries the wordmark and the primary routes on every page", async ({ page }) => {
  for (const path of ROUTES) {
    await page.goto(path);
    const nav = page.locator("header");
    await expect(nav.getByRole("link", { name: "WANTED home" })).toBeVisible();
    for (const label of ["Live", "Predict", "Leaderboard"]) {
      await expect(nav.getByRole("link", { name: label, exact: true })).toBeVisible();
    }
  }
});

// REMOVED: "the displayed contract address keeps its case".
//
// The navbar no longer displays a token contract address, so the test had nothing left to assert
// against. Keeping the finding, because it will apply again the moment $WANTED ships an address
// onto the page: the navbar's `.ticker` class sets `text-transform: uppercase`, which silently
// rendered a base58 address in caps. base58 is case-sensitive, so anyone reading it off the screen
// rather than clicking copy got a DIFFERENT address. Any future address display needs both a
// case-preserving style and a test that the visible text is not its own uppercase form.


for (const path of ROUTES) {
  test(`${path} renders without horizontal scroll`, async ({ page }) => {
    await page.goto(path);
    await expect(page.locator("footer")).toBeVisible();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(1);
  });
}

test("per-clip OG image renders", async ({ request }) => {
  const res = await request.get("/clips/1/opengraph-image");
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toContain("image/png");
});

test("site-wide OG image renders @current-build", async ({ request }) => {
  const res = await request.get("/opengraph-image");
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toContain("image/png");
});

// Next replaces `openGraph` and `alternates` per segment instead of merging them, so declaring
// them only in the root layout silently gives every route the homepage's og:title, the bare
// origin as og:url, and the origin as canonical. Asserting og:image's origin on "/" alone passes
// through that bug, so this walks every route and additionally requires the values to be
// DISTINCT — inheritance from a common ancestor cannot satisfy that.
test("share metadata is per-route, not inherited from the homepage @current-build", async ({
  page,
}) => {
  const seenUrls = new Set<string>();
  const seenTitles = new Set<string>();

  for (const route of SHARE_ROUTES) {
    await page.goto(route.path);
    const origin = new URL(page.url()).origin;
    const meta = await headMetadata(page);
    const want = expectedUrl(origin, route.path);

    expect(meta.title, `<title> on ${route.path}`).toBe(route.title);
    expect(meta.ogTitle, `og:title on ${route.path}`).toBe(route.title);

    expect(meta.ogUrl, `og:url must be present on ${route.path}`).toBeTruthy();
    expect(normalizeUrl(meta.ogUrl as string), `og:url on ${route.path}`).toBe(want);

    expect(meta.canonical, `canonical must be present on ${route.path}`).toBeTruthy();
    expect(normalizeUrl(meta.canonical as string), `canonical on ${route.path}`).toBe(want);

    expect(meta.ogImage, `og:image must be present on ${route.path}`).toBeTruthy();
    expect(new URL(meta.ogImage as string).origin, `og:image origin on ${route.path}`).toBe(origin);

    seenUrls.add(normalizeUrl(meta.ogUrl as string));
    seenTitles.add(meta.ogTitle as string);
  }

  expect(seenUrls.size, "each route needs its own og:url").toBe(SHARE_ROUTES.length);
  expect(seenTitles.size, "each route needs its own og:title").toBe(SHARE_ROUTES.length);
});

// Clip permalinks are the site's deliberate share surface and carry their own OG image, so their
// metadata must name the clip, not the homepage. This needs a real row; the offline suite covers
// the same route deterministically (an unreachable database still renders the permalink).
test("clip permalinks carry their own share metadata @current-build", async ({ page }) => {
  await page.goto("/clips");
  const links = page.locator('a[href^="/clips/"]');
  const count = await links.count();
  test.skip(count === 0, "no clip rows in this database yet");

  const href = (await links.first().getAttribute("href")) as string;
  await page.goto(href);
  const origin = new URL(page.url()).origin;
  const meta = await headMetadata(page);
  const want = expectedUrl(origin, href);

  expect(normalizeUrl(meta.ogUrl as string), "og:url on a clip permalink").toBe(want);
  expect(normalizeUrl(meta.canonical as string), "canonical on a clip permalink").toBe(want);
  expect(meta.ogTitle).toBe(meta.title);
  expect(meta.ogTitle, "a clip must not advertise the homepage's og:title").not.toBe(
    SHARE_ROUTES[0].title
  );
  // The per-clip OG image route, not the site-wide card.
  expect(new URL(meta.ogImage as string).pathname).toBe(`${href}/opengraph-image`);
});

test("unknown routes get the branded 404 @current-build", async ({ page }) => {
  const res = await page.goto("/clips/999999999");
  expect(res?.status()).toBe(404);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("WRONG TURN");
  await expect(page.locator("footer")).toBeVisible();
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(1);

  // A 404 must not hand crawlers another page's identity. Claiming nothing is the ideal answer
  // (the served HTML does exactly that, since notFound() resolves against the root layout alone);
  // a self-referential value that appears after hydration is harmless. Claiming the bare origin —
  // what the inherited root-layout canonical used to emit — is the failure this guards.
  const origin = new URL(page.url()).origin;
  const meta = await headMetadata(page);
  for (const [name, value] of [
    ["canonical", meta.canonical],
    ["og:url", meta.ogUrl],
  ] as const) {
    if (value === null) continue;
    expect(normalizeUrl(value), `${name} on a 404 must not claim another page`).toBe(
      expectedUrl(origin, "/clips/999999999")
    );
  }
});
