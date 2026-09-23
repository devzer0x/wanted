import { execFileSync } from "node:child_process";
import { expect, test, type Locator, type Page } from "@playwright/test";

import { installWallet } from "./walletHarness";
import { WANTED_CHAIN_ID_HEX } from "../src/components/wallet/chain";

// e2e/predictLocal.spec.ts — CONTRACTS-PREDICTIONS.md v2.4.
//
// Runs against web/scripts/predict-verify's real Postgres 16 + real PostgREST v12.2.3 stack
// (up.sh: every infra/supabase migration applied, seeded with the fixed-id rows this file reads),
// never against production (CLAUDE.md rule 1/2). `web/playwright.config.ts` only selects this file
// when `PLAYWRIGHT_PREDICT_LOCAL=1`; it never runs under `npm run test:e2e` or
// `npm run test:e2e:offline`, and up.sh must already be running (its own error message says so).
//
// Nothing is intercepted: every request the page makes goes to the real Next.js route handler,
// which reaches the real local Postgres through the real createSupabaseAdmin() — same convention
// as e2e/wallet.spec.ts and e2e/walletHarness.ts's own "no route under src/app/api/** is stubbed"
// rule. The one thing this file does directly against the database is exactly what
// web/scripts/predict-verify/up.sh already does to seed its own fixed rows (a `docker exec ...
// psql` insert) — used here only for ground truth up.sh has no way to know ahead of time: a
// specific reward_ledger amount attached to a wallet that does not exist until a test has actually
// signed in with a real EIP-1193 signature (./walletHarness.ts), or a second wallet whose only
// purpose is a leaderboard precision check.

const PG_CONTAINER = "web-f5-pg";

// Fixed ids web/scripts/predict-verify/up.sh seeds — keep these in sync with that file.
// LOCK_GUARD_ID (44444444-…) is not referenced by id below: the MobilePredictBar test finds it by
// question text through pickPrimary()'s own selection, the same way a viewer would.
const DUP_GUARD_ID = "55555555-5555-5555-5555-555555555555";
const TINY_REWARD_ID = "66666666-6666-6666-6666-666666666666";

test.setTimeout(60_000);

/** Runs SQL against the same local Postgres container up.sh seeded, the same way up.sh does. */
function runSql(sql: string): void {
  execFileSync(
    "docker",
    ["exec", "-i", PG_CONTAINER, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-q"],
    { input: sql, stdio: ["pipe", "pipe", "pipe"] }
  );
}

/**
 * Credits `wallet` for `predictionId` with `amountWhole` TTWO (a whole-token decimal literal,
 * e.g. "0.0000005") by inserting the same two rows `settle_due_predictions()` would have written —
 * done here by hand because the ground truth each scenario below needs (an exact tiny or huge
 * amount, for a wallet that only exists once a test has signed in) is not something settlement
 * ever produces on its own. `on conflict do nothing` so re-running this against an already-seeded
 * wallet (or the other viewport's project hitting the same fixed wallet) is a no-op, not an error.
 */
function creditWallet(predictionId: string, wallet: string, outcome: string, amountWhole: string): void {
  runSql(`
    insert into public.prediction_entries (prediction_id, wallet, outcome)
    values ('${predictionId}', '${wallet}', '${outcome}')
    on conflict (prediction_id, wallet) do nothing;
    insert into public.reward_ledger (prediction_id, wallet, amount, asset)
    values ('${predictionId}', '${wallet}', ${amountWhole}, 'TTWO')
    on conflict (prediction_id, wallet) do nothing;
  `);
}

function walletButton(page: Page): Locator {
  return page.getByTestId("wallet-button");
}

function walletPanel(page: Page): Locator {
  return page.getByRole("dialog", { name: "Wallet" });
}

/** Click to connect, then click again to open the wallet panel. Returns the panel. Same flow as
 *  e2e/wallet.spec.ts's own helper of the same shape. */
async function connectAndOpenPanel(page: Page): Promise<Locator> {
  const button = walletButton(page);
  await button.click();
  await expect(button).toHaveAttribute("data-wallet-status", "connected");
  await button.click();
  const panel = walletPanel(page);
  await expect(panel).toBeVisible();
  return panel;
}

/** Reads the session from INSIDE the page (not `page.request`, which does not carry the page's
 *  cookies — see e2e/wallet.spec.ts's sessionAddress for the same note). */
async function sessionAddress(page: Page): Promise<string | null> {
  const body = await page.evaluate(async () => {
    const res = await fetch("/api/auth/session");
    if (res.status !== 200) return { address: null as string | null };
    return (await res.json()) as { address: string | null };
  });
  return body.address;
}

/**
 * Installs a fresh harness wallet, navigates to `path`, connects, and signs in for real:
 * POST /api/auth/nonce -> personal_sign in the harness's own account -> POST /api/auth/verify. The
 * same flow e2e/wallet.spec.ts's "sign-in signs the server-issued message verbatim" test drives,
 * run to completion (not skipped) here because this stack's `wallet_sessions` table is real,
 * unlike that spec's production-pointing runs. Returns the signed-in address, lower-cased (the
 * storage form every wallet column uses — see src/app/api/_lib/wallet.ts).
 */
async function signIn(page: Page, path: string): Promise<string> {
  const wallet = await installWallet(page, { chainIdHex: WANTED_CHAIN_ID_HEX });
  await page.goto(path);
  const panel = await connectAndOpenPanel(page);

  const nonceResponse = page.waitForResponse(
    (res) => res.url().includes("/api/auth/nonce") && res.request().method() === "POST"
  );
  await panel.getByRole("button", { name: "Sign in to predict" }).click();
  const nonce = await nonceResponse;
  expect(nonce.status(), `POST /api/auth/nonce answered ${nonce.status()}: ${await nonce.text()}`).toBe(
    201
  );

  await expect
    .poll(async () => sessionAddress(page), { message: "waiting for /api/auth/verify to set the session cookie" })
    .toBe(wallet.address.toLowerCase());

  return wallet.address.toLowerCase();
}

/** POSTs JSON from inside the page (so the session cookie rides along), same pattern as the app's
 *  own fetch calls in src/components/predict/*.tsx. */
async function postJson(
  page: Page,
  url: string,
  body: unknown
): Promise<{ status: number; body: Record<string, unknown> }> {
  return page.evaluate(
    async ({ url, body }) => {
      const res = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(body),
      });
      let parsed: Record<string, unknown> = {};
      try {
        parsed = (await res.json()) as Record<string, unknown>;
      } catch {
        // No JSON body — parsed stays {}.
      }
      return { status: res.status, body: parsed };
    },
    { url, body }
  );
}

// -------------------------------------------------------------------------------------------
// (a), the static half: a wallet with far more significant digits than a JS double can hold.
// Independent of sign-in — leaderboard() has no notion of "whose session this is" — so this is
// seeded and read without ever opening a page.
// -------------------------------------------------------------------------------------------

const MANY_DIGITS_WALLET = "0x" + "9".repeat(40);
// 12 whole digits + 18 fractional digits = 30 significant digits — Number holds ~15-17 exactly.
const MANY_DIGITS_AMOUNT = "123456789012.123456789012345678";
const MANY_DIGITS_BASE_UNITS = "123456789012123456789012345678";

test("leaderboard reports the exact base-unit earned for a wallet with many significant digits", async ({
  request,
}) => {
  creditWallet(TINY_REWARD_ID, MANY_DIGITS_WALLET, "yes", MANY_DIGITS_AMOUNT);

  const res = await request.get("/api/leaderboard");
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { rows: Array<{ wallet: string; earned: string }> };

  const row = body.rows.find((r) => r.wallet === MANY_DIGITS_WALLET);
  expect(row, `no leaderboard row for ${MANY_DIGITS_WALLET} in ${JSON.stringify(body.rows)}`).toBeTruthy();
  // Pre-fix this is a JS double round-tripped through JSON.parse (postgrest-js), which cannot hold
  // 30 significant digits exactly — the assertion below is what catches that, not just "some value".
  expect(row?.earned).toBe(MANY_DIGITS_BASE_UNITS);
});

// -------------------------------------------------------------------------------------------
// (a, continued) + (b): a REAL signed-in wallet's own sub-1e-6 credit — exact on the leaderboard,
// and rendered honestly (never "0 TTWO credited") on the settled prediction it won. One flow
// because both facts come from the same credit: up.sh's own seed comment explains why this one
// needs a real session rather than a static row.
// -------------------------------------------------------------------------------------------

const TINY_AMOUNT = "0.0000005"; // 5e-7 TTWO
const TINY_BASE_UNITS = "500000000000"; // 0.0000005 * 10^18, exactly

test("a signed-in wallet's sub-1e-6 credit is exact on the leaderboard and never reads as 0 TTWO credited", async ({
  page,
  request,
}) => {
  const address = await signIn(page, "/predict");
  creditWallet(TINY_REWARD_ID, address, "yes", TINY_AMOUNT);

  const leaderboardRes = await request.get("/api/leaderboard");
  expect(leaderboardRes.status(), await leaderboardRes.text()).toBe(200);
  const body = (await leaderboardRes.json()) as { rows: Array<{ wallet: string; earned: string }> };
  const row = body.rows.find((r) => r.wallet === address);
  expect(row, `no leaderboard row for ${address} in ${JSON.stringify(body.rows)}`).toBeTruthy();
  expect(row?.earned).toBe(TINY_BASE_UNITS);

  // A fresh load picks the credit up through /api/predictions/live's `mine` map — no need to wait
  // out usePredictions()'s 8 s poll.
  await page.reload();
  const card = page.getByTestId("resolved-prediction").filter({ hasText: "TINY REWARD PROOF" });
  await expect(card).toBeVisible();
  const pickedLine = card.locator("p", { hasText: "You picked" });
  await expect(pickedLine).toHaveText("You picked YES — correct, <0.0001 TTWO credited.");
});

// -------------------------------------------------------------------------------------------
// (c): on a mobile viewport, MobilePredictBar's outcome buttons disable/disappear at `locks_at`
// the instant the page knows about it — not up to 8 s later, at usePredictions()'s next poll.
// LOCK_GUARD_ID is seeded `status = 'open'` with `locks_at` already 5 s in the past and staying
// there (nothing in this stack calls lock_due_predictions()), so the server-reported status alone
// would keep the pre-fix bar offering a vote forever.
// -------------------------------------------------------------------------------------------

test("MobilePredictBar disables its outcome buttons at locks_at without waiting for a poll", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "the sticky bar is lg:hidden on the desktop viewport");

  await signIn(page, "/");

  const bar = page.getByTestId("mobile-predict-bar");
  await expect(bar).toBeVisible();
  await expect(bar).toContainText("LOCK GUARD PROOF");
  await expect(bar.getByText("Locked — waiting for the result.")).toBeVisible();
  await expect(bar.getByRole("button", { name: "YES" })).toHaveCount(0);
  await expect(bar.getByRole("button", { name: "NO" })).toHaveCount(0);
});

// -------------------------------------------------------------------------------------------
// (d): a second entry from the same signed-in wallet is refused as a duplicate, not folded into
// the generic "locked, unknown, or invalid outcome" 409 — DUP_GUARD_ID's locks_at is 30 minutes
// out, so there is no ambiguity with an actual lock here.
// -------------------------------------------------------------------------------------------

test("a second POST /enter from the same wallet is refused as a duplicate", async ({ page }) => {
  await signIn(page, "/predict");

  const first = await postJson(page, `/api/predictions/${DUP_GUARD_ID}/enter`, { outcome: "yes" });
  expect(first.status, `first /enter: ${JSON.stringify(first.body)}`).toBe(201);

  const second = await postJson(page, `/api/predictions/${DUP_GUARD_ID}/enter`, { outcome: "yes" });
  expect(second.status, `second /enter: ${JSON.stringify(second.body)}`).toBe(409);
  expect(second.body.error).toBe("already entered this prediction");
});
