import { expect, test, type Locator, type Page } from "@playwright/test";
import { hexToString, numberToHex, stringToHex, verifyMessage } from "viem";

import { installWallet } from "./walletHarness";
// The app's own constants, imported rather than retyped, so a change to either fails here.
import { WANTED_CHAIN_ID, WANTED_CHAIN_ID_HEX } from "../src/components/wallet/chain";
import { shortAddress } from "../src/lib/social";

// The wallet flow, executed end to end against the real app and the real API routes.
//
// The only stand-in is the browser extension itself: `./walletHarness.ts` injects a real EIP-1193
// provider (announced over EIP-6963, backed by a real secp256k1 account) because Playwright's
// Chromium has no wallet installed. Nothing of ours is stubbed — `/api/auth/nonce`,
// `/api/auth/verify` and `/api/auth/session` are hit for real and answer with whatever the real
// backend says today.
//
// TODAY'S REAL STATE, and why some tests below skip rather than pass:
// the prediction-layer tables are not in the Supabase project yet, so POST /api/auth/nonce
// answers 500 ("Could not find the table 'public.wallet_sessions' in the schema cache"). The
// specs observe the REAL response and decide from it: no nonce means no message to sign, so the
// signature tests skip with the server's own answer quoted in the reason. Faking a nonce to make
// them green would be a lie about a security property (CLAUDE.md §1, §2).
//
// Everything up to and including the network switch needs no database at all, and runs for real.
//
// VERIFIED, not assumed, that the two skipped tests pass once the table exists: they were run
// against a real Postgres 15 + PostgREST holding `public.wallet_sessions` created verbatim from
// infra/supabase/migrations/20260908120000_predictions.sql (a local container, never the cloud
// project). Both passed on both viewports, and `/api/auth/verify` really burned the nonce —
// `verified_at` was stamped — which means the harness's signature verified server-side through
// the real `verifySiwe`. When that migration reaches the Supabase project, these unskip and the
// nonce test below skips instead. Note that they then write real `wallet_sessions` rows (for
// throwaway addresses) into whatever project the run points at, the same way the rest of this
// suite reads real rows.
//
// Host page: /agent, which is fully static. The wallet button lives in the nav on every page
// (src/app/layout.tsx -> SiteNav), so this exercises the same component the live pages do without
// adding request-time Supabase fan-out that has nothing to do with what is under test.

const HOST_PAGE = "/agent";

// 4663 / 0x1237 — Robinhood Chain mainnet, per docs/CONTRACTS-PREDICTIONS.md §1 (verified there
// by a live eth_chainId) and src/lib/chain/config.ts's DEFAULT_CHAIN_ID of 4663. The wallet layer
// declares both forms in src/components/wallet/chain.ts; viem's switchChain puts
// `numberToHex(id)` on the wire, so all three must agree or the switch asks for the wrong chain.
const EXPECTED_CHAIN_ID = 4663;
const EXPECTED_CHAIN_ID_HEX = "0x1237";
const WRONG_CHAIN_ID_HEX = "0x1"; // Ethereum mainnet — a wallet's usual default

const RPC_URL = "https://rpc.mainnet.chain.robinhood.com";

test.setTimeout(60_000);

test("the chain id the app will ask a wallet to switch to is the verified one", () => {
  expect(WANTED_CHAIN_ID).toBe(EXPECTED_CHAIN_ID);
  expect(WANTED_CHAIN_ID_HEX).toBe(EXPECTED_CHAIN_ID_HEX);
  // What viem actually serialises for `wallet_switchEthereumChain`.
  expect(numberToHex(WANTED_CHAIN_ID)).toBe(EXPECTED_CHAIN_ID_HEX);
});

function walletButton(page: Page): Locator {
  return page.getByTestId("wallet-button");
}

function walletPanel(page: Page): Locator {
  return page.getByRole("dialog", { name: "Wallet" });
}

/** Click to connect, then click again to open the wallet panel. Returns the panel. */
async function connectAndOpenPanel(page: Page): Promise<Locator> {
  const button = walletButton(page);
  await button.click();
  await expect(button).toHaveAttribute("data-wallet-status", "connected");
  await button.click();
  const panel = walletPanel(page);
  await expect(panel).toBeVisible();
  return panel;
}

interface NonceOutcome {
  status: number;
  body: { message?: string; nonce?: string; error?: string; detail?: string };
  raw: string;
}

/** Reads the REAL /api/auth/nonce response the app just received. Never fabricates one. */
async function readNonceResponse(
  page: Page,
  act: () => Promise<void>
): Promise<NonceOutcome> {
  const waiting = page.waitForResponse(
    (res) => res.url().includes("/api/auth/nonce") && res.request().method() === "POST"
  );
  await act();
  const res = await waiting;
  const raw = await res.text();
  let body: NonceOutcome["body"] = {};
  try {
    body = JSON.parse(raw) as NonceOutcome["body"];
  } catch {
    // Not JSON — `raw` is still reported in the skip reason so the state is legible.
  }
  return { status: res.status(), body, raw };
}

// The skip reason quotes the server's REAL answer rather than asserting a cause, and only names
// the missing table when the server itself named it. A skip that explains itself with something
// that did not happen is worse than no skip at all.
function nonceSkipReason(outcome: NonceOutcome): string {
  const quoted = `POST /api/auth/nonce answered ${outcome.status}: ${outcome.raw.slice(0, 400)}`;
  const namesTable = /wallet_sessions/i.test(outcome.raw);
  const cause = namesTable
    ? "The prediction-layer table public.wallet_sessions is not in this Supabase project yet — " +
      "applying infra/supabase/migrations/20260908120000_predictions.sql is what unskips this."
    : "This test unskips as soon as that route issues a message.";
  return `${quoted} — there is no server-issued message to sign, so the signature path cannot run. ${cause} It must never be faked green.`;
}

// Read the session from INSIDE the page, not through `page.request`.
//
// `page.request` is a separate APIRequestContext, and in this suite's configuration it does not
// carry the document's cookies — so it reported `address: null` for a session that had in fact been
// established, and the final assertion of the sign-in test failed against a working app. Verified
// directly: driving the same UI and reading `/api/auth/session` via `page.evaluate` returns the
// connected address while `page.request` returns null for the same page.
async function sessionAddress(page: Page): Promise<string | null> {
  const body = await page.evaluate(async () => {
    const res = await fetch("/api/auth/session");
    if (res.status !== 200) return { address: null, status: res.status };
    return (await res.json()) as { address: string | null };
  });
  return body.address;
}

// ---------------------------------------------------------------------------------------------
// No wallet at all — the state every visitor without an extension sees. Nothing is installed
// here, which is exactly the condition the rest of the suite runs under today.
// ---------------------------------------------------------------------------------------------

test("with no wallet in the browser, the button says so and claims nothing", async ({ page }) => {
  await page.goto(HOST_PAGE);
  const button = walletButton(page);
  await expect(button).toHaveText(/connect wallet/i);

  await button.click();

  await expect(button).toHaveAttribute("data-wallet-status", "no-wallet");
  await expect(button).toHaveText(/no wallet found/i);
  await expect(page.getByText(/No EIP-6963 wallet was found in this browser/i)).toBeVisible();

  // No address is invented, and no session is claimed on the strength of a click.
  await expect(walletPanel(page)).toHaveCount(0);
  expect(await sessionAddress(page)).toBeNull();
});

// ---------------------------------------------------------------------------------------------
// Connecting
// ---------------------------------------------------------------------------------------------

test("an EIP-6963 wallet on Robinhood Chain connects and its address is shown", async ({ page }) => {
  const wallet = await installWallet(page, { chainIdHex: EXPECTED_CHAIN_ID_HEX });
  await page.goto(HOST_PAGE);

  const button = walletButton(page);
  await expect(button).toHaveText(/connect wallet/i);
  await button.click();

  await expect(button).toHaveAttribute("data-wallet-status", "connected");
  await expect(button).toHaveText(new RegExp(escapeRegExp(shortAddress(wallet.address))));

  const methods = (await wallet.calls()).map((call) => call.method);
  expect(methods, "connecting must prompt for accounts and read the chain").toContain(
    "eth_requestAccounts"
  );
  expect(methods).toContain("eth_chainId");
  // Connecting proves an address; it must not ask for a signature or a transaction.
  expect(methods).not.toContain("personal_sign");
  expect(methods).not.toContain("eth_sendTransaction");
});

test("a wallet that already authorised this site reattaches without prompting", async ({ page }) => {
  const wallet = await installWallet(page, {
    chainIdHex: EXPECTED_CHAIN_ID_HEX,
    preAuthorized: true,
  });
  await page.goto(HOST_PAGE);

  const button = walletButton(page);
  await expect(button).toHaveAttribute("data-wallet-status", "connected");
  await expect(button).toHaveText(new RegExp(escapeRegExp(shortAddress(wallet.address))));

  // The silent path must use eth_accounts, which never prompts — asking for
  // eth_requestAccounts unbidden would pop a wallet dialog nobody clicked.
  const methods = (await wallet.calls()).map((call) => call.method);
  expect(methods).toContain("eth_accounts");
  expect(methods, "a silent reconnect must not prompt").not.toContain("eth_requestAccounts");
});

test("a declined connection is surfaced as declined, not as connected", async ({ page }) => {
  const wallet = await installWallet(page, { rejectConnect: true });
  await page.goto(HOST_PAGE);

  const button = walletButton(page);
  await button.click();

  await expect(button).toHaveAttribute("data-wallet-status", "rejected");
  await expect(page.getByText("Connection request was declined.")).toBeVisible();
  await expect(button).toHaveText(/connect wallet/i);
  expect((await wallet.calls()).map((c) => c.method)).toContain("eth_requestAccounts");
  expect(await sessionAddress(page)).toBeNull();
});

// ---------------------------------------------------------------------------------------------
// Wrong network. THE assertion here is the hex chain id on the wire.
// ---------------------------------------------------------------------------------------------

test("a wallet on the wrong chain is reported, and the switch asks for 0x1237", async ({ page }) => {
  const wallet = await installWallet(page, { chainIdHex: WRONG_CHAIN_ID_HEX });
  await page.goto(HOST_PAGE);

  const button = walletButton(page);
  await button.click();

  await expect(button).toHaveAttribute("data-wallet-status", "wrong-network");
  await expect(button).toHaveText(/wrong network/i);

  // Clicking the wrong-network button asks the wallet to switch.
  await button.click();

  await expect
    .poll(async () => (await wallet.callsTo("wallet_switchEthereumChain")).length, {
      message: "the app should ask the wallet to switch chain",
    })
    .toBe(1);

  const [switchCall] = await wallet.callsTo("wallet_switchEthereumChain");
  const params = switchCall.params as Array<{ chainId?: string }>;
  expect(Array.isArray(params)).toBe(true);
  expect(params[0].chainId, "EIP-3326 takes the chain id as a hex string").toBe(
    EXPECTED_CHAIN_ID_HEX
  );

  // And the wallet reporting the new chain moves the UI to connected.
  await expect(button).toHaveAttribute("data-wallet-status", "connected");
  await expect(button).toHaveText(new RegExp(escapeRegExp(shortAddress(wallet.address))));
});

test("a wallet that does not know the chain is asked to add it, then to switch", async ({ page }) => {
  const wallet = await installWallet(page, {
    chainIdHex: WRONG_CHAIN_ID_HEX,
    chainUnknown: true,
  });
  await page.goto(HOST_PAGE);

  const button = walletButton(page);
  await button.click();
  await expect(button).toHaveAttribute("data-wallet-status", "wrong-network");
  await button.click();

  await expect
    .poll(async () => (await wallet.callsTo("wallet_addEthereumChain")).length, {
      message: "a 4902 from the wallet should be answered with wallet_addEthereumChain",
    })
    .toBe(1);

  const [addCall] = await wallet.callsTo("wallet_addEthereumChain");
  const added = (addCall.params as Array<{
    chainId?: string;
    chainName?: string;
    rpcUrls?: string[];
  }>)[0];
  expect(added.chainId).toBe(EXPECTED_CHAIN_ID_HEX);
  expect(added.chainName).toBe("Robinhood Chain");
  expect(added.rpcUrls).toContain(RPC_URL);

  // The add is followed by a second switch attempt, and only then is the app connected.
  const methods = (await wallet.calls()).map((call) => call.method);
  expect(methods.filter((m) => m === "wallet_switchEthereumChain")).toHaveLength(2);
  expect(methods.lastIndexOf("wallet_switchEthereumChain")).toBeGreaterThan(
    methods.indexOf("wallet_addEthereumChain")
  );
  await expect(button).toHaveAttribute("data-wallet-status", "connected");
});

test("a chain change in the wallet is reflected, and a declined switch says so", async ({ page }) => {
  const wallet = await installWallet(page, { chainIdHex: EXPECTED_CHAIN_ID_HEX });
  await page.goto(HOST_PAGE);

  const panel = await connectAndOpenPanel(page);

  // The user switches network inside the wallet while the panel is open — a real EIP-1193 event,
  // not a reload.
  await wallet.emit("chainChanged", WRONG_CHAIN_ID_HEX);

  await expect(walletButton(page)).toHaveAttribute("data-wallet-status", "wrong-network");
  await expect(panel).toContainText("This wallet is on chain 1, not Robinhood Chain (4663).");

  // Decline the switch: the app must say so rather than quietly leave the button looking fine.
  await wallet.setBehavior({ rejectSwitch: true });
  await panel.getByRole("button", { name: "Switch network" }).click();

  await expect(panel.getByText("Network switch was declined.")).toBeVisible();
  await expect(walletButton(page)).toHaveAttribute("data-wallet-status", "wrong-network");

  // Now the user accepts. Same button, no reload.
  await wallet.setBehavior({ rejectSwitch: false });
  await panel.getByRole("button", { name: "Switch network" }).click();
  await expect(walletButton(page)).toHaveAttribute("data-wallet-status", "connected");

  const switched = await wallet.callsTo("wallet_switchEthereumChain");
  expect(switched).toHaveLength(2);
  for (const call of switched) {
    expect((call.params as Array<{ chainId?: string }>)[0].chainId).toBe(EXPECTED_CHAIN_ID_HEX);
  }
});

// ---------------------------------------------------------------------------------------------
// Sign-in. These need a server-issued message, so they need public.wallet_sessions to exist.
// ---------------------------------------------------------------------------------------------

// The single most valuable assertion in this file. CONTRACTS-PREDICTIONS §4 pins the SIWE domain
// server-side and returns the exact message text precisely so the client cannot assemble its own;
// a client-built message (from window.location.host, or with one byte of difference) produces a
// signature /verify can never validate, and worse, a Host-derived domain would compare
// attacker-supplied input with itself. So: the bytes handed to personal_sign must be the bytes
// the server issued, and the signature over them must really recover to the connected address.
test("sign-in signs the server-issued message verbatim", async ({ page }) => {
  const wallet = await installWallet(page, { chainIdHex: EXPECTED_CHAIN_ID_HEX });
  await page.goto(HOST_PAGE);
  const panel = await connectAndOpenPanel(page);

  const nonce = await readNonceResponse(page, async () => {
    await panel.getByRole("button", { name: "Sign in to predict" }).click();
  });
  test.skip(nonce.status !== 201, nonceSkipReason(nonce));

  const serverMessage = nonce.body.message;
  expect(typeof serverMessage, "/api/auth/nonce must return the message to sign").toBe("string");
  const message = serverMessage as string;
  expect(message).toContain(nonce.body.nonce as string);

  await expect
    .poll(async () => (await wallet.callsTo("personal_sign")).length, {
      message: "the app should ask the wallet to sign",
    })
    .toBe(1);

  const [signCall] = await wallet.callsTo("personal_sign");
  const params = signCall.params as [string, string];

  // viem's signMessage puts `stringToHex(message)` on the wire (verified against viem 2.56.3),
  // so byte-identity is asserted in both directions: the hex must be the hex of the server's
  // string, and decoding it must give that string back with nothing added or trimmed.
  expect(params[0], "personal_sign must carry the server's message, byte for byte").toBe(
    stringToHex(message)
  );
  expect(hexToString(params[0] as `0x${string}`)).toBe(message);
  expect(params[1].toLowerCase()).toBe(wallet.address.toLowerCase());

  // And the signature is a real one over that message, not an opaque string.
  const signature = signCall.result as `0x${string}`;
  expect(signature).toMatch(/^0x[0-9a-f]{130}$/i);
  expect(
    await verifyMessage({ address: wallet.address, message, signature }),
    "the signature must recover to the connected address"
  ).toBe(true);

  expect((await wallet.calls()).map((c) => c.method)).not.toContain("eth_sendTransaction");

  // Whatever /verify then says, the UI must not claim a session the server did not grant.
  await expect
    .poll(async () => {
      const address = await sessionAddress(page);
      const errored = await panel.getByText(/declined|unavailable|failed|could not/i).count();
      return address !== null || errored > 0;
    })
    .toBe(true);
  const address = await sessionAddress(page);
  if (address !== null) {
    expect(address.toLowerCase()).toBe(wallet.address.toLowerCase());
  }
});

test("a declined signature is surfaced honestly and no session is claimed", async ({ page }) => {
  const wallet = await installWallet(page, {
    chainIdHex: EXPECTED_CHAIN_ID_HEX,
    rejectSign: true,
  });

  const verifyCalls: string[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/auth/verify")) verifyCalls.push(req.method());
  });

  await page.goto(HOST_PAGE);
  const panel = await connectAndOpenPanel(page);

  const nonce = await readNonceResponse(page, async () => {
    await panel.getByRole("button", { name: "Sign in to predict" }).click();
  });
  test.skip(nonce.status !== 201, nonceSkipReason(nonce));

  await expect
    .poll(async () => (await wallet.callsTo("personal_sign")).length)
    .toBe(1);

  await expect(panel.getByText("You declined the sign-in request.")).toBeVisible();
  // Still offered, not silently "signed in".
  await expect(panel.getByRole("button", { name: "Sign in to predict" })).toBeVisible();
  expect(verifyCalls, "nothing may be sent to /api/auth/verify without a signature").toHaveLength(
    0
  );
  expect(await sessionAddress(page)).toBeNull();
});

// The state this deployment is really in today: the nonce cannot be issued, so sign-in cannot
// start. The UI must say what the server said and must not pretend. Once public.wallet_sessions
// exists this test skips and the two above take over.
test("when the server cannot issue a nonce, sign-in fails out loud", async ({ page }) => {
  const wallet = await installWallet(page, { chainIdHex: EXPECTED_CHAIN_ID_HEX });
  await page.goto(HOST_PAGE);
  const panel = await connectAndOpenPanel(page);

  const nonce = await readNonceResponse(page, async () => {
    await panel.getByRole("button", { name: "Sign in to predict" }).click();
  });
  test.skip(
    nonce.status === 201,
    "/api/auth/nonce is issuing messages on this deployment, so the honest-failure state is not " +
      "reachable — the verbatim-message and declined-signature tests cover the working path."
  );

  const serverError = nonce.body.error;
  expect(typeof serverError, "a failed route must answer with {error}").toBe("string");

  // The reason shown is the server's own, not an invented one.
  await expect(panel.getByText(serverError as string)).toBeVisible();
  await expect(panel.getByRole("button", { name: "Sign in to predict" })).toBeVisible();

  // No message, so nothing was signed and nothing was sent to /verify.
  expect(await wallet.callsTo("personal_sign")).toHaveLength(0);
  expect(await sessionAddress(page)).toBeNull();

  // The wallet is still connected — a backend failure is not a wallet failure.
  await expect(walletButton(page)).toHaveAttribute("data-wallet-status", "connected");
});

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
