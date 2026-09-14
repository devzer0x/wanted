#!/usr/bin/env node
// The payout outbox (CONTRACTS-PREDICTIONS §10), driven end to end through every failure mode the
// 2026-09-14 audit named. Run: `npm run verify:payout` (from web/). Needs Docker and Foundry.
//
// WHAT IS REAL — everything that decides money:
//   * THE CODE. web/src/lib/rewards/payoutWorker.ts and treasury.ts, with their imports
//     (lib/chain/{assets,config}.ts, app/api/_lib/{amount,supabaseAdmin}.ts, lib/rewards/strategies.ts),
//     compiled from the working tree with the project's own tsc. runPayoutWorker() here is the same
//     function GET /api/cron/tick calls; nothing in it is replaced, wrapped or stubbed.
//   * THE DATABASE. web/scripts/payout-stack/up.sh: Postgres 16 with EVERY migration in
//     infra/supabase/migrations (the outbox included), Supabase's roles and default privileges, and a
//     real PostgREST v12 verifying a real HS256 service-role JWT, reached through the product's own
//     createSupabaseAdmin() — the same supabase-js client and per-fetch deadline production uses.
//   * THE CREDITS. harness/tests/fixtures/real_session_2026-09-04.json (a recording of a real session)
//     is loaded unmodified with scripts/load-real-session.py. Every credit comes from a prediction over
//     a window whose outcome that recording decides (ground truth re-derived from the loaded events
//     before use), settled by the real settle_due_predictions_serialized() through PostgREST. No
//     reward_ledger row is ever inserted by hand.
//   * THE CLAIMS. The real create_reward_claim through PostgREST, with the decided §10.6 rails read by
//     the real rewardLimits() and converted by the real fromBaseUnits(), exactly as the claim route does.
//   * THE CHAIN STATE. The real TTWO bytecode and storage, real ECDSA signatures, real mempool, real
//     receipts, real reverts.
//
// WHAT IS NOT:
//   * The chain is a LOCAL anvil fork of Robinhood Chain mainnet (chain id 4663), not mainnet itself.
//     Nothing this script does can reach the real chain. Fork-only controls set each scene, and each is
//     named where it is used: anvil_setBalance (gas money), anvil_impersonateAccount (fund the treasury
//     from a real TTWO holder), evm_setAutomine / anvil_dropTransaction / evm_mine (pool control),
//     anvil_setStorageAt (make a mined transfer revert; starve the treasury), and
//     anvil_setNextBlockBaseFeePerGas (a fee spike).
//   * The treasury key and every recipient are throwaway keys generated in this process. The key lives
//     in process.env only, is stripped from every child process's environment, and is never printed.
//   * The "crash" and the "ambiguous broadcast" are an in-process JSON-RPC interlock placed between the
//     worker and the fork via TREASURY_RPC_URL. It never fabricates a chain answer: it forwards every
//     request verbatim and returns the real reply, except that for eth_sendRawTransaction it either
//     refuses outright (the bytes never left: a crash after persist) or forwards the bytes and then
//     drops the node's reply behind an HTTP 502 (the bytes landed, the caller cannot know).
//
// The stack is brought up at the start (up.sh, which first tears down any previous one) and torn down
// in `finally` (down.sh). Only containers named wanted-payout-* are touched.

import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const webRoot = join(fileURLToPath(new URL(".", import.meta.url)), "..");
const repoRoot = join(webRoot, "..");
const STACK_DIR = join(webRoot, "scripts", "payout-stack");
const STACK_ENV = "/tmp/wanted-payout-stack/env";
const FIXTURE = join(repoRoot, "harness", "tests", "fixtures", "real_session_2026-09-04.json");

// Chain facts (CONTRACTS-PREDICTIONS §1), each re-read from the fork before it is relied on.
const TTWO = "0x5e81213613b6b86eab4c6c50d718d34359459786";
const TTWO_HOLDER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"; // a real holder, used ON THE FORK only
const CHAIN_ID = 4663;
const FUND_TTWO = 10n ** 18n; // 1 TTWO moved from the real holder to the throwaway treasury, on the fork
const FUND_ETH = 10n ** 18n;

// The decided §10.6 claim rails, in base units — what Vercel Production carries.
const CLAIM_MIN_AMOUNT = "2000000000000000"; // 0.002
const CLAIM_MAX_AMOUNT = "500000000000000000"; // 0.5

// Real windows from the recording. The outcome is what the recording contains (the same ground truth
// infra/verify-predictions.sql uses); setup re-derives it from the loaded events before any drill runs.
const OUTCOMES = '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]';
const WINDOWS = {
  no_death: {
    name: "2026-09-04 06:20:00Z..06:23:00Z (no death, no arrest)",
    opened: "2026-09-04T06:19:00Z", locks: "2026-09-04T06:20:00Z", resolves: "2026-09-04T06:23:00Z",
    rule: { kind: "survives_window", outcome_if_true: "yes", outcome_if_false: "no" }, result: "yes",
  },
  two_deaths: {
    name: "2026-09-04 08:41:00Z..08:45:00Z (two real deaths)",
    opened: "2026-09-04T08:40:00Z", locks: "2026-09-04T08:41:00Z", resolves: "2026-09-04T08:45:00Z",
    rule: { kind: "survives_window", outcome_if_true: "yes", outcome_if_false: "no" }, result: "no",
  },
  cops_cleared: {
    name: "2026-09-04 01:03:00Z..01:06:00Z (a real wanted 2->0)",
    opened: "2026-09-04T01:02:00Z", locks: "2026-09-04T01:03:00Z", resolves: "2026-09-04T01:06:00Z",
    rule: { kind: "wanted_clears", outcome_if_true: "yes", outcome_if_false: "no" }, result: "yes",
  },
};

// ------------------------------------------------------------------------------------------------
// Reporting
// ------------------------------------------------------------------------------------------------

const results = [];
let currentDrill = "setup";

function check(name, ok, detail) {
  results.push({ drill: currentDrill, name, ok: Boolean(ok) });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}`);
  if (detail) for (const line of String(detail).split("\n")) console.log(`        ${line}`);
}
function note(text) {
  console.log(`        ${text}`);
}
function section(title) {
  console.log(`\n${title}`);
}
function errorText(err) {
  return err instanceof Error ? `${err.name}: ${err.message}` : String(err);
}
function firstLine(text) {
  return String(text).split("\n")[0].trim();
}
const j = (v) => JSON.stringify(v, (_k, x) => (typeof x === "bigint" ? x.toString() : x));

// ------------------------------------------------------------------------------------------------
// Child processes never inherit the treasury key or the service JWT.
// ------------------------------------------------------------------------------------------------

function childEnv() {
  const env = { ...process.env };
  delete env.TREASURY_PRIVATE_KEY;
  delete env.SUPABASE_SECRET_KEY;
  return env;
}

// ------------------------------------------------------------------------------------------------
// Compile the real source (same technique as scripts/verify-treasury.mjs): the project's own tsc,
// then only module specifiers are resolved the way Next would at build time —
//   "@/x"         -> relative path to the compiled file (tsconfig paths)
//   "./x"         -> "./x.js" (Node ESM needs the extension)
//   "server-only" -> next/dist/compiled/server-only/empty.js, what next@15.5.23's
//                    create-compiler-aliases.js maps `server-only$` to on the SERVER pass.
// ------------------------------------------------------------------------------------------------

function compileRealSource(outRoot, files) {
  const tsconfigPath = join(outRoot, "tsconfig.json");
  writeFileSync(
    tsconfigPath,
    JSON.stringify({
      compilerOptions: {
        target: "es2020",
        module: "esnext",
        moduleResolution: "bundler",
        strict: true,
        skipLibCheck: true,
        esModuleInterop: true,
        types: ["node"],
        baseUrl: "..",
        paths: { "@/*": ["./src/*"] },
        rootDir: "../src",
        outDir: "./out",
      },
      files: ["../next-env.d.ts", ...files.map((f) => `../${f}`)],
    }),
  );
  execFileSync("npx", ["tsc", "-p", tsconfigPath], { cwd: webRoot, stdio: "inherit", env: childEnv() });

  const outDir = join(outRoot, "out");
  writeFileSync(join(outDir, "package.json"), JSON.stringify({ type: "module" }));
  const serverOnlyEmpty = join(webRoot, "node_modules/next/dist/compiled/server-only/empty.js");
  const walk = (dir) =>
    readdirSync(dir).flatMap((e) => {
      const full = join(dir, e);
      return statSync(full).isDirectory() ? walk(full) : full.endsWith(".js") ? [full] : [];
    });
  const toRelative = (fromDir, target) => {
    const r = relative(fromDir, target);
    return r.startsWith(".") ? r : `./${r}`;
  };
  for (const file of walk(outDir)) {
    const fromDir = dirname(file);
    const resolve = (spec) => {
      if (spec === "server-only") return toRelative(fromDir, serverOnlyEmpty);
      if (spec.startsWith("@/")) return toRelative(fromDir, `${join(outDir, spec.slice(2))}.js`);
      if (spec.startsWith("./") || spec.startsWith("../")) return spec.endsWith(".js") ? spec : `${spec}.js`;
      return spec;
    };
    const src = readFileSync(file, "utf8")
      .replace(/(\bfrom\s*)(["'])([^"']+)\2/g, (_m, p, q, s) => `${p}${q}${resolve(s)}${q}`)
      .replace(/(\bimport\s*\(\s*)(["'])([^"']+)\2/g, (_m, p, q, s) => `${p}${q}${resolve(s)}${q}`)
      .replace(/(\bimport\s+)(["'])([^"']+)\2/g, (_m, p, q, s) => `${p}${q}${resolve(s)}${q}`);
    writeFileSync(file, src);
  }
  return outDir;
}

// ------------------------------------------------------------------------------------------------
// Database access for SETUP and ASSERTIONS only (psql as postgres inside the stack container). Every
// product action — settlement, claims, the worker — goes through PostgREST with the service-role key.
// ------------------------------------------------------------------------------------------------

let PG = "wanted-payout-pg";
const lit = (v) => (v === null || v === undefined ? "null" : `'${String(v).replace(/'/g, "''")}'`);

function psql(query) {
  return execFileSync("docker", ["exec", "-i", PG, "psql", "-X", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-tAq"], {
    input: query,
    env: childEnv(),
    encoding: "utf8",
    stdio: ["pipe", "pipe", "pipe"],
    maxBuffer: 64 * 1024 * 1024,
  }).trim();
}
function psqlJson(query) {
  return JSON.parse(psql(`select coalesce(json_agg(t), '[]'::json) from (${query}) t;`));
}
function psqlError(query) {
  try {
    psql(query);
    return null;
  } catch (err) {
    return firstLine(String(err.stderr || err.message));
  }
}

// ------------------------------------------------------------------------------------------------
// Raw JSON-RPC to the fork — the independent observer of chain truth (no project code in the path).
// ------------------------------------------------------------------------------------------------

let ANVIL = "http://127.0.0.1:8599";
let rpcId = 0;
async function anvil(method, params = []) {
  const res = await fetch(ANVIL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++rpcId, method, params }),
  });
  const body = await res.json();
  if (body.error) throw new Error(`${method}: ${body.error.message}`);
  return body.result;
}
const hex = (n) => `0x${BigInt(n).toString(16)}`;
const pad32 = (n) => `0x${BigInt(n).toString(16).padStart(64, "0")}`;

// ------------------------------------------------------------------------------------------------
// The interlock between the worker and the fork (TREASURY_RPC_URL).
//   pass              forward everything verbatim, return the real reply
//   refuse            eth_sendRawTransaction is refused and never forwarded (a crash after persist)
//   forward_then_fail eth_sendRawTransaction IS forwarded, the node's real reply is dropped and the
//                     caller gets an HTTP 502 (the bytes landed; the worker cannot know)
// ------------------------------------------------------------------------------------------------

async function startInterlock(upstream) {
  const state = { mode: "pass", sendRaw: [], droppedReplies: [] };
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", async () => {
      const body = Buffer.concat(chunks).toString("utf8");
      let parsed = null;
      try {
        parsed = JSON.parse(body);
      } catch {
        parsed = null;
      }
      const calls = Array.isArray(parsed) ? parsed : parsed ? [parsed] : [];
      const raws = calls.filter((c) => c?.method === "eth_sendRawTransaction");
      for (const c of raws) state.sendRaw.push({ raw: String(c.params?.[0] ?? "").toLowerCase(), mode: state.mode });

      if (raws.length > 0 && state.mode === "refuse") {
        const deny = (c) => ({
          jsonrpc: "2.0",
          id: c?.id ?? null,
          error: { code: -32000, message: "verify-payout interlock: broadcast refused, bytes never left this process" },
        });
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify(Array.isArray(parsed) ? calls.map(deny) : deny(calls[0])));
        return;
      }
      try {
        const up = await fetch(upstream, { method: "POST", headers: { "content-type": "application/json" }, body });
        const text = await up.text();
        if (raws.length > 0 && state.mode === "forward_then_fail") {
          state.droppedReplies.push(text);
          res.writeHead(502, { "content-type": "text/plain" });
          res.end("bad gateway");
          return;
        }
        res.writeHead(up.status, { "content-type": "application/json" });
        res.end(text);
      } catch (err) {
        res.writeHead(502, { "content-type": "text/plain" });
        res.end(`interlock upstream failed: ${String(err)}`);
      }
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    state,
    reset(mode) {
      state.mode = mode;
      state.sendRaw.length = 0;
      state.droppedReplies.length = 0;
    },
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

// ------------------------------------------------------------------------------------------------

let stackUp = false;
let interlock = null;
let outRoot = null;

async function main() {
  // The ambient environment never chooses the key, the RPC or the treasury address here.
  for (const k of [
    "TREASURY_PRIVATE_KEY",
    "TREASURY_RPC_URL",
    "NEXT_PUBLIC_TREASURY_ADDRESS",
    "NEXT_PUBLIC_CHAIN_RPC_URL",
    "NEXT_PUBLIC_TTWO_SYMBOL",
    "NEXT_PUBLIC_EXPLORER_URL",
    "NEXT_PUBLIC_CHAIN_EXPLORER_URL",
  ]) {
    delete process.env[k];
  }

  section("[setup] stack, real source, real recording, funded throwaway treasury on the fork");

  // ---- stack ----------------------------------------------------------------------------------
  const upOut = execFileSync("bash", [join(STACK_DIR, "up.sh")], {
    env: childEnv(),
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: 16 * 1024 * 1024,
  });
  stackUp = true;
  for (const line of readFileSync(STACK_ENV, "utf8").split("\n")) {
    const m = /^export ([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (m) process.env[m[1]] = m[2];
  }
  PG = process.env.PAYOUT_PG_CONTAINER || PG;
  ANVIL = process.env.PAYOUT_ANVIL_URL || ANVIL;
  check(
    "payout stack up: Postgres 16 + every migration, PostgREST v12 behind /rest/v1, anvil fork of 4663",
    Boolean(process.env.SUPABASE_URL && process.env.SUPABASE_SECRET_KEY && process.env.NEXT_PUBLIC_RPC_URL),
    `${firstLine(upOut.trim().split("\n").pop() ?? "")}; SUPABASE_URL=${process.env.SUPABASE_URL}; fork=${ANVIL}`,
  );
  const migrations = readdirSync(join(repoRoot, "infra/supabase/migrations")).filter((f) => f.endsWith(".sql"));
  const outboxFns = psql(
    "select count(*) from pg_proc where pronamespace = 'public'::regnamespace and proname in " +
      "('create_reward_claim','acquire_payout_lease','assign_claim_nonce','record_signed_claim','confirm_claim','fail_claim'," +
      "'flag_claim_review','halt_treasury','resolve_claim_review','resume_payouts','record_claim_override','payout_snapshot');",
  );
  const finalizeGone = psql("select count(*) from pg_proc where proname = 'finalize_reward_claim';");
  check(
    `the outbox migration is live (${migrations.length} migrations applied; §10.3 functions present; finalize_reward_claim dropped)`,
    outboxFns === "12" && finalizeGone === "0",
    `outbox functions found: ${outboxFns}/12, finalize_reward_claim: ${finalizeGone}`,
  );

  // ---- env the product reads (set BEFORE import: lib/chain/config reads NEXT_PUBLIC_RPC_URL at load)
  process.env.CLAIM_MIN_AMOUNT = CLAIM_MIN_AMOUNT;
  process.env.CLAIM_MAX_AMOUNT = CLAIM_MAX_AMOUNT;
  process.env.NEXT_PUBLIC_TTWO_TOKEN = process.env.NEXT_PUBLIC_TTWO_TOKEN || TTWO;

  // ---- compile the real source -------------------------------------------------------------------
  outRoot = mkdtempSync(join(webRoot, ".payout-verify-"));
  const outDir = compileRealSource(outRoot, [
    "src/lib/rewards/payoutWorker.ts",
    "src/lib/rewards/treasury.ts",
    "src/lib/rewards/strategies.ts",
    "src/lib/chain/assets.ts",
    "src/lib/chain/config.ts",
    "src/app/api/_lib/amount.ts",
    "src/app/api/_lib/supabaseAdmin.ts",
  ]);

  const viem = await import("viem");
  const { keccak256, encodeFunctionData, encodeAbiParameters, stringToHex, parseTransaction, recoverTransactionAddress, fromRlp } = viem;
  const { generatePrivateKey, privateKeyToAccount } = await import("viem/accounts");

  // The throwaway treasury key. process.env only; never printed; stripped from child processes.
  process.env.TREASURY_PRIVATE_KEY = generatePrivateKey();
  const treasury = privateKeyToAccount(process.env.TREASURY_PRIVATE_KEY).address.toLowerCase();
  process.env.NEXT_PUBLIC_TREASURY_ADDRESS = treasury; // exercises the worker's published-address match

  const url = (rel) => pathToFileURL(join(outDir, rel)).href;
  const worker = await import(url("lib/rewards/payoutWorker.js"));
  const { createSupabaseAdmin } = await import(url("app/api/_lib/supabaseAdmin.js"));
  const { toBaseUnits, fromBaseUnits } = await import(url("app/api/_lib/amount.js"));
  const { rewardLimits } = await import(url("lib/rewards/strategies.js"));
  const { rewardAsset } = await import(url("lib/chain/assets.js"));
  const { ROBINHOOD_CHAIN } = await import(url("lib/chain/config.js"));

  check(
    "real source compiled with the project's tsc; the frozen §10.4 constants are what the worker exports",
    worker.LEASE_TTL_SECONDS === 120 &&
      worker.MAX_FEE_CEILING === 5_000_000_000n &&
      worker.FEE_FLOOR === 500_000_000n &&
      worker.GAS_LIMIT_CEILING === 150_000n &&
      worker.MAX_PAYOUTS_PER_TICK === 5 &&
      worker.RECEIPT_POLL_MS === 20_000 &&
      worker.SAFETY_MARGIN_MS === 8_000,
    `LEASE_TTL_SECONDS=${worker.LEASE_TTL_SECONDS} MAX_FEE_CEILING=${worker.MAX_FEE_CEILING} FEE_FLOOR=${worker.FEE_FLOOR} ` +
      `GAS_LIMIT_CEILING=${worker.GAS_LIMIT_CEILING} MAX_PAYOUTS_PER_TICK=${worker.MAX_PAYOUTS_PER_TICK}`,
  );

  const asset = rewardAsset();
  const admin = createSupabaseAdmin();
  check(
    "createSupabaseAdmin() builds the product client; rewardAsset() is TTWO/18; chain config is 4663 -> the fork",
    admin !== null &&
      asset?.symbol === "TTWO" &&
      asset.decimals === 18 &&
      asset.address.toLowerCase() === TTWO &&
      ROBINHOOD_CHAIN.id === CHAIN_ID &&
      ROBINHOOD_CHAIN.rpcUrls.default.http[0] === process.env.NEXT_PUBLIC_RPC_URL,
    `asset=${asset?.symbol}/${asset?.decimals} ${asset?.address}; chain=${ROBINHOOD_CHAIN.id} rpc=${ROBINHOOD_CHAIN.rpcUrls.default.http[0]}`,
  );
  if (!admin || !asset) throw new Error("cannot continue without the admin client and the reward asset");

  // ---- the fork is Robinhood Chain mainnet state --------------------------------------------------
  const forkChain = Number(BigInt(await anvil("eth_chainId")));
  const erc20 = [
    { type: "function", name: "balanceOf", stateMutability: "view", inputs: [{ name: "a", type: "address" }], outputs: [{ type: "uint256" }] },
    { type: "function", name: "transfer", stateMutability: "nonpayable", inputs: [{ name: "to", type: "address" }, { name: "amount", type: "uint256" }], outputs: [{ type: "bool" }] },
    { type: "function", name: "paused", stateMutability: "view", inputs: [], outputs: [{ type: "bool" }] },
    { type: "function", name: "symbol", stateMutability: "view", inputs: [], outputs: [{ type: "string" }] },
    { type: "function", name: "decimals", stateMutability: "view", inputs: [], outputs: [{ type: "uint8" }] },
  ];
  const call = async (fn, args = []) =>
    anvil("eth_call", [{ to: TTWO, data: encodeFunctionData({ abi: erc20, functionName: fn, args }) }, "latest"]);
  const balanceOf = async (a) => BigInt(await call("balanceOf", [a]));
  const nonceOf = async (a) => BigInt(await anvil("eth_getTransactionCount", [a, "latest"]));
  const receiptOf = async (h) => anvil("eth_getTransactionReceipt", [h]);
  /** anvil may return a tx hash before automine has sealed its block; wait (bounded) for the receipt. */
  const waitReceipt = async (h, ms = 10_000) => {
    const end = Date.now() + ms;
    for (;;) {
      const rc = await receiptOf(h);
      if (rc || Date.now() > end) return rc;
      await new Promise((r) => setTimeout(r, 200));
    }
  };
  const symbolRaw = await call("symbol");
  const decimals = Number(BigInt(await call("decimals")));
  const paused = BigInt(await call("paused")) === 1n;
  const holderBal = await balanceOf(TTWO_HOLDER);
  check(
    "the fork IS chain 4663 with the real TTWO: decimals 18, paused() false, real holder balance present",
    forkChain === CHAIN_ID && decimals === 18 && paused === false && holderBal > FUND_TTWO && symbolRaw.length > 2,
    `eth_chainId=${forkChain} decimals=${decimals} paused=${paused} balanceOf(holder)=${holderBal}`,
  );

  // ---- the real recording --------------------------------------------------------------------------
  const fixture = JSON.parse(readFileSync(FIXTURE, "utf8"));
  const SESSION_ID = fixture._provenance.session_id;
  const loadSql = execFileSync("python3", [join(repoRoot, "scripts/load-real-session.py")], {
    env: childEnv(),
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  writeFileSync(join(outRoot, "load.sql"), loadSql);
  execFileSync("docker", ["cp", join(outRoot, "load.sql"), `${PG}:/tmp/real_session_load.sql`], { env: childEnv() });
  execFileSync("docker", ["exec", PG, "psql", "-X", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-q", "-f", "/tmp/real_session_load.sql"], {
    env: childEnv(),
    stdio: ["ignore", "ignore", "pipe"],
  });
  const loaded = Number(psql(`select count(*) from public.events where session_id = ${lit(SESSION_ID)};`));
  check(
    "the real recorded session is loaded unmodified (scripts/load-real-session.py)",
    loaded === fixture.events.length,
    `session ${SESSION_ID}: ${loaded} events in the database, ${fixture.events.length} in the fixture`,
  );

  for (const [key, w] of Object.entries(WINDOWS)) {
    const [g] = psqlJson(
      `select count(*) filter (where type in ('death','busted')) as deaths,
              count(*) filter (where type = 'wanted_change' and (payload->>'to')::numeric = 0) as clears,
              (select max(ts) from public.events where session_id = ${lit(SESSION_ID)}) >= ${lit(w.resolves)}::timestamptz as caught_up
         from public.events
        where session_id = ${lit(SESSION_ID)} and ts >= ${lit(w.locks)}::timestamptz and ts <= ${lit(w.resolves)}::timestamptz`,
    );
    const truth =
      w.rule.kind === "survives_window" ? (Number(g.deaths) === 0 ? "yes" : "no") : Number(g.clears) > 0 ? "yes" : "no";
    check(
      `ground truth re-derived from the loaded events: ${w.name} -> ${w.rule.kind} = ${truth}`,
      truth === w.result && g.caught_up === true,
      `deaths/busted in window: ${g.deaths}; wanted->0 in window: ${g.clears}; telemetry caught up past the window: ${g.caught_up} (key ${key})`,
    );
  }

  psql(`insert into public.site_config (key, value) values
          ('rewards', '{"enabled": true, "payouts": true}'::jsonb),
          ('reward_caps', '{"daily_cap": 100, "max_per_prediction": 100, "max_per_wallet_day": 100}'::jsonb)
        on conflict (key) do update set value = excluded.value;`);
  const flags = psql("select public.rewards_enabled()::text || ',' || public.payouts_enabled()::text;");
  check("site_config: rewards {enabled:true, payouts:true}, generous reward_caps (drills test payouts, not clamps)", flags === "true,true", `rewards_enabled(),payouts_enabled() = ${flags}`);

  // ---- fund the throwaway treasury ON THE FORK -------------------------------------------------------
  await anvil("anvil_setBalance", [treasury, hex(FUND_ETH)]);
  await anvil("anvil_setBalance", [TTWO_HOLDER, hex(FUND_ETH)]);
  await anvil("anvil_impersonateAccount", [TTWO_HOLDER]);
  const fundHash = await anvil("eth_sendTransaction", [
    { from: TTWO_HOLDER, to: TTWO, data: encodeFunctionData({ abi: erc20, functionName: "transfer", args: [treasury, FUND_TTWO] }) },
  ]);
  await anvil("anvil_stopImpersonatingAccount", [TTWO_HOLDER]);
  const fundRc = await waitReceipt(fundHash);
  const treasuryStart = await balanceOf(treasury);
  const treasuryNonce0 = await nonceOf(treasury);
  check(
    "treasury (throwaway key) funded on the fork: 1 ETH via anvil_setBalance, 1 TTWO by a real transfer from the holder",
    fundRc?.status === "0x1" && treasuryStart === FUND_TTWO && treasuryNonce0 === 0n,
    `treasury ${treasury}: TTWO ${treasuryStart}, ETH ${BigInt(await anvil("eth_getBalance", [treasury, "latest"]))}, nonce ${treasuryNonce0}; funding tx ${fundHash} status ${fundRc?.status}`,
  );

  // ---- TTWO's balance slot, found EMPIRICALLY ------------------------------------------------------
  const ns = BigInt(keccak256(encodeAbiParameters([{ type: "uint256" }], [BigInt(keccak256(stringToHex("openzeppelin.storage.ERC20"))) - 1n]))) & ~0xffn;
  const mapSlot = (addr, base) => keccak256(encodeAbiParameters([{ type: "address" }, { type: "uint256" }], [addr, base]));
  const candidates = [{ name: `OZ v5 ERC-7201 "openzeppelin.storage.ERC20" (namespace ${pad32(ns)})`, base: ns }];
  for (let i = 0n; i <= 10n; i++) candidates.push({ name: `plain mapping at slot ${i}`, base: i });
  const hits = [];
  for (const c of candidates) {
    const v = BigInt(await anvil("eth_getStorageAt", [TTWO, mapSlot(treasury, c.base), "latest"]));
    if (v === treasuryStart) hits.push(c);
  }
  let balanceBase = null;
  for (const h of hits) {
    const slot = mapSlot(treasury, h.base);
    await anvil("anvil_setStorageAt", [TTWO, slot, pad32(0n)]);
    const zeroed = await balanceOf(treasury);
    await anvil("anvil_setStorageAt", [TTWO, slot, pad32(treasuryStart)]);
    const back = await balanceOf(treasury);
    if (zeroed === 0n && back === treasuryStart) {
      balanceBase = h;
      break;
    }
  }
  check(
    "TTWO balance slot found empirically and confirmed with balanceOf (write 0 -> 0, restore -> original)",
    balanceBase !== null,
    `${candidates.length} candidates (ERC-7201 + plain slots 0..10); storage == balanceOf for: ${hits.map((h) => h.name).join("; ") || "none"}; confirmed: ${balanceBase?.name ?? "none"}`,
  );
  const setTreasuryTokens = async (value) => {
    await anvil("anvil_setStorageAt", [TTWO, mapSlot(treasury, balanceBase.base), pad32(value)]);
    return balanceOf(treasury);
  };

  interlock = await startInterlock(ANVIL);

  // ================================================================================================
  // Helpers shared by the drills
  // ================================================================================================

  const freshWallet = () => privateKeyToAccount(generatePrivateKey()).address.toLowerCase();
  const sameAmount = (a, b) => toBaseUnits(String(a), 18) === toBaseUnits(String(b), 18);
  let predictionSeq = 0;

  /** A credit, the real way: a prediction over a real window, the recipient on the right side and a
   * second throwaway on the wrong side, then lock + settle through PostgREST like the cron. */
  async function credit(label, wallet, pool, windowKey) {
    const w = WINDOWS[windowKey];
    const loser = freshWallet();
    predictionSeq += 1;
    const pid = psql(
      `with ins as (
         insert into public.predictions (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
                                         outcomes, telemetry_rule, reward_pool, reward_asset)
         values (${lit(SESSION_ID)}, ${lit(`PAYOUT DRILL ${label} #${predictionSeq}`)}, 'payout_drill',
                 ${lit(w.opened)}, ${lit(w.locks)}, ${lit(w.resolves)}, ${lit(OUTCOMES)}::jsonb,
                 ${lit(JSON.stringify(w.rule))}::jsonb, ${lit(pool)}, ${lit(asset.symbol)})
         returning id)
       select id from ins;`,
    );
    psql(
      `insert into public.prediction_entries (prediction_id, wallet, outcome) values
         (${lit(pid)}, ${lit(wallet)}, ${lit(w.result)}),
         (${lit(pid)}, ${lit(loser)}, ${lit(w.result === "yes" ? "no" : "yes")});`,
    );
    const locked = await admin.rpc("lock_due_predictions");
    const settled = await admin.rpc("settle_due_predictions_serialized");
    const [p] = psqlJson(`select status::text as status, result, correct_count from public.predictions where id = ${lit(pid)}`);
    const ledger = psqlJson(
      `select id, wallet, amount::text as amount, claim_id, clamped from public.reward_ledger where prediction_id = ${lit(pid)}`,
    );
    const ok =
      !locked.error &&
      !settled.error &&
      p.status === "settled" &&
      p.result === w.result &&
      p.correct_count === 1 &&
      ledger.length === 1 &&
      ledger[0].wallet === wallet &&
      sameAmount(ledger[0].amount, pool) &&
      ledger[0].claim_id === null &&
      ledger[0].clamped === false;
    check(
      `credit via REAL settlement: ${w.name} settles '${p.result}', ${wallet.slice(0, 10)}.. credited ${ledger[0]?.amount ?? "nothing"}`,
      ok,
      ok ? null : `lock=${j(locked.error ?? locked.data)} settle=${j(settled.error ?? settled.data)} prediction=${j(p)} ledger=${j(ledger)}`,
    );
    return ledger[0] ?? null;
  }

  function defaultRails() {
    const l = rewardLimits();
    return { min: fromBaseUnits(l.minClaim, asset.decimals), max: fromBaseUnits(l.maxClaim, asset.decimals) };
  }

  /** Exactly what POST /api/rewards/claim sends. */
  async function claim(wallet, rails = defaultRails()) {
    return admin
      .rpc("create_reward_claim", { p_wallet: wallet, p_asset: asset.symbol, p_min_amount: rails.min, p_max_amount: rails.max })
      .single();
  }

  async function enqueue(label, wallet) {
    const c = await claim(wallet);
    const ok = !c.error && c.data?.status === "queued" && typeof c.data?.amount_text === "string";
    check(
      `create_reward_claim via PostgREST (the product's supabase-js) enqueues: queued, amount_text a string`,
      ok,
      ok ? `claim ${c.data.id} amount_text="${c.data.amount_text}"` : `error=${j(c.error)} data=${j(c.data)}`,
    );
    if (!ok) throw new Error(`${label}: could not enqueue`);
    return c.data;
  }

  async function run(label, budgetMs = 40_000) {
    const t0 = Date.now();
    const s = await worker.runPayoutWorker(admin, { budgetMs });
    note(
      `run ${label}: reason=${s.reason} paid=${s.paid} failed=${s.failed} in_flight=${s.in_flight} needs_review=${s.needs_review} ` +
        `halted=${s.halted}${s.halt_reason ? `(${s.halt_reason})` : ""} next_nonce=${s.next_nonce} chain_nonce=${s.chain_nonce} ` +
        `(${Date.now() - t0} ms)`,
    );
    return s;
  }

  function row(id) {
    const [r] = psqlJson(
      `select id, wallet, status, nonce::text as nonce, raw_tx, tx_hash, attempts, error, proof, receipt_status,
              block_number::text as block_number, amount::text as amount, amount_base::text as amount_base,
              override_tx_hash, override_kind, signer_address, to_address, token_address, chain_id,
              gas_limit::text as gas_limit, max_fee_per_gas::text as max_fee, max_priority_fee_per_gas::text as max_prio
         from public.reward_claims where id = ${lit(id)}`,
    );
    return r;
  }
  const attached = (id) => Number(psql(`select count(*) from public.reward_ledger where claim_id = ${lit(id)};`));
  const unclaimed = (wallet) =>
    psqlJson(`select id, amount::text as amount from public.reward_ledger where wallet = ${lit(wallet)} and claim_id is null order by id`);
  function account() {
    const [a] = psqlJson(`select next_nonce::text as next_nonce, halted, halt_reason from public.treasury_accounts where address = ${lit(treasury)}`);
    return a ?? null;
  }
  const leaseFree = () => psql("select (holder is null)::text from public.payout_lease where id = 1;") === "true";

  function expectClean() {
    const open = psqlJson(`select id, wallet, status from public.reward_claims where status in ('queued','signed','broadcast','needs_review')`);
    const a = account();
    check(
      "start state: no claim queued/signed/broadcast/needs_review, treasury not halted, lease free",
      open.length === 0 && !(a?.halted) && leaseFree(),
      open.length || a?.halted ? `open=${j(open)} account=${j(a)}` : null,
    );
  }

  /** Every assertion a confirmed payout must satisfy, DB and chain. */
  async function assertConfirmed(id, before, label = "claim") {
    const r = row(id);
    const rc = r.tx_hash ? await receiptOf(r.tx_hash) : null;
    const onChainRaw = r.tx_hash ? await anvil("eth_getRawTransactionByHash", [r.tx_hash]) : null;
    const recipientNow = await balanceOf(r.wallet);
    const amountBase = r.amount_base === null ? null : BigInt(r.amount_base);
    check(
      `${label}: DB confirmed, receipt_status 1, block_number recorded`,
      r.status === "confirmed" && r.receipt_status === 1 && r.block_number !== null,
      `status=${r.status} receipt_status=${r.receipt_status} block=${r.block_number} nonce=${r.nonce} attempts=${r.attempts} error=${r.error}`,
    );
    check(
      `${label}: receipt.transactionHash == claim.tx_hash == keccak256(raw_tx); chain holds exactly the persisted bytes`,
      rc !== null &&
        rc.status === "0x1" &&
        rc.transactionHash === r.tx_hash &&
        keccak256(r.raw_tx) === r.tx_hash &&
        onChainRaw?.toLowerCase() === r.raw_tx &&
        BigInt(rc.blockNumber) === BigInt(r.block_number),
      `tx ${r.tx_hash} status ${rc?.status} block ${rc ? BigInt(rc.blockNumber) : "-"} gasUsed ${rc ? BigInt(rc.gasUsed) : "-"}; ` +
        `eth_getRawTransactionByHash == raw_tx: ${onChainRaw?.toLowerCase() === r.raw_tx}`,
    );
    check(
      `${label}: recipient balance delta == amount_base exactly (amount_base == amount x 10^18)`,
      amountBase !== null && recipientNow - before.recipient === amountBase && toBaseUnits(r.amount, 18) === amountBase,
      `amount ${r.amount} -> amount_base ${r.amount_base}; recipient ${before.recipient} -> ${recipientNow} (delta ${recipientNow - before.recipient})`,
    );
    check(`${label}: its credits stay attached (paid)`, attached(id) > 0, `attached ledger rows: ${attached(id)}`);
    return r;
  }

  async function snapshotChain(wallet) {
    return { recipient: await balanceOf(wallet), treasury: await balanceOf(treasury), nonce: await nonceOf(treasury) };
  }

  async function runUntilTerminal(label, id, max = 4) {
    const summaries = [];
    for (let i = 1; i <= max; i += 1) {
      summaries.push(await run(`${label}#${i}`));
      const s = row(id).status;
      if (s === "confirmed" || s === "failed" || s === "needs_review") return { runs: i, summaries };
    }
    return { runs: max, summaries };
  }

  /** A transaction from the treasury KEY that the system did not make: a 0-value self-transfer. */
  async function sendFromTreasuryKey(nonce) {
    const acct = privateKeyToAccount(process.env.TREASURY_PRIVATE_KEY);
    const raw = await acct.signTransaction({
      type: "eip1559",
      chainId: CHAIN_ID,
      nonce: Number(nonce),
      to: acct.address,
      value: 0n,
      gas: 21_000n,
      maxFeePerGas: 2_000_000_000n,
      maxPriorityFeePerGas: 0n,
    });
    const hash = await anvil("eth_sendRawTransaction", [raw]);
    return { hash, rc: await waitReceipt(hash) };
  }

  async function drill(id, name, fn) {
    currentDrill = id;
    section(`[${id}] ${name}`);
    try {
      await fn();
    } catch (err) {
      check(`drill ${id} ran to completion`, false, firstLine(errorText(err)));
    } finally {
      delete process.env.TREASURY_RPC_URL;
      interlock.reset("pass");
      try {
        await anvil("evm_setAutomine", [true]);
      } catch {
        /* reported by the next drill's clean-state check */
      }
    }
  }

  const confirmedIds = [];

  // ================================================================================================
  // a. Happy path
  // ================================================================================================
  let walletA = null;
  await drill("a", "happy path: queued -> confirmed", async () => {
    expectClean();
    walletA = freshWallet();
    await credit("a", walletA, "0.01", "no_death");
    const c = await enqueue("a", walletA);
    const before = await snapshotChain(walletA);
    check("no treasury_accounts row before the first run (the worker initialises it from 'latest')", account() === null);
    const s = await run("a#1");
    check("the run pays exactly one claim and reports no stop reason", s.paid === 1 && s.failed === 0 && s.reason === null, `summary.paid=${s.paid} reason=${s.reason}`);
    await assertConfirmed(c.id, before);
    const afterNonce = await nonceOf(treasury);
    const a = account();
    check(
      "treasury nonce +1 on chain; treasury_accounts.next_nonce == chain nonce",
      afterNonce === before.nonce + 1n && a !== null && BigInt(a.next_nonce) === afterNonce,
      `chain nonce ${before.nonce} -> ${afterNonce}; next_nonce ${a?.next_nonce}`,
    );
    const r = row(c.id);
    const tx = parseTransaction(r.raw_tx);
    check(
      "raw_tx decodes to chainId 4663, nonce = claim.nonce, to = TTWO, value 0, data = transfer(wallet, amount_base)",
      tx.chainId === CHAIN_ID &&
        BigInt(tx.nonce) === BigInt(r.nonce) &&
        tx.to === TTWO &&
        (tx.value ?? 0n) === 0n &&
        tx.data === encodeFunctionData({ abi: erc20, functionName: "transfer", args: [walletA, BigInt(r.amount_base)] }),
      `type=${tx.type} chainId=${tx.chainId} nonce=${tx.nonce} to=${tx.to} gas=${tx.gas} maxFee=${tx.maxFeePerGas} prio=${tx.maxPriorityFeePerGas ?? 0n}`,
    );
    const treasuryDelta = before.treasury - (await balanceOf(treasury));
    check("treasury TTWO delta == -amount_base exactly", treasuryDelta === BigInt(r.amount_base), `treasury paid out ${treasuryDelta}`);
    const [ts] = psqlJson("select value from public.site_config where key = 'treasury_status'");
    const tsText = j(ts?.value ?? null);
    check(
      "the public treasury_status was written, and carries no hash, raw tx or key (principle 7)",
      ts !== undefined && !/0x[0-9a-fA-F]{64}/.test(tsText) && ts.value.halted === false,
      tsText,
    );
    confirmedIds.push(c.id);
  });

  // ================================================================================================
  // b. FM-01 regression: a wallet whose last claim confirmed can claim and be paid again
  // ================================================================================================
  await drill("b", "FM-01 regression: the same wallet is credited, claims and is paid again", async () => {
    expectClean();
    if (!walletA) throw new Error("drill a did not run");
    await credit("b", walletA, "0.02", "two_deaths");
    const c = await enqueue("b", walletA);
    const before = await snapshotChain(walletA);
    const s = await run("b#1");
    check("the second claim is paid in one run", s.paid === 1 && s.reason === null, `paid=${s.paid} reason=${s.reason}`);
    const r = await assertConfirmed(c.id, before, "second claim");
    const claims = psqlJson(`select id, status, nonce::text as nonce from public.reward_claims where wallet = ${lit(walletA)} order by created_at`);
    check(
      "the wallet now has two confirmed claims at consecutive nonces (it never 409s for life)",
      claims.length === 2 && claims.every((x) => x.status === "confirmed") && BigInt(claims[1].nonce) === BigInt(claims[0].nonce) + 1n,
      j(claims),
    );
    confirmedIds.push(r.id);
  });

  // ================================================================================================
  // c. Crash after persist, before broadcast
  // ================================================================================================
  await drill("c", "crash after persist, before broadcast -> re-broadcast of exactly the persisted bytes", async () => {
    expectClean();
    const W = freshWallet();
    await credit("c", W, "0.01", "cops_cleared");
    const c = await enqueue("c", W);

    // I3 while this claim is queued: a fresh credit exists, and a second claim must hit the index.
    await credit("c-second", W, "0.01", "no_death");
    const dup = await claim(W);
    check(
      "I3: a second claim for a wallet with one in flight is refused by the unique index (23505), even with fresh credit",
      dup.error?.code === "23505",
      `error.code=${dup.error?.code ?? "none"}`,
    );

    const before = await snapshotChain(W);
    interlock.reset("refuse");
    process.env.TREASURY_RPC_URL = interlock.url;
    const s = await run("c#1 (interlock refuses eth_sendRawTransaction)");
    const r = row(c.id);
    check(
      "the worker stops on the refused broadcast and records it as an attempt (a code, never text)",
      s.reason === "broadcast_rejected" && r.attempts === 1 && r.error === "broadcast_rejected",
      `reason=${s.reason} attempts=${r.attempts} error=${r.error}`,
    );
    check(
      "claim ends 'signed' with the full signed record persisted; keccak256(raw_tx) == tx_hash",
      r.status === "signed" && r.raw_tx !== null && keccak256(r.raw_tx) === r.tx_hash && r.nonce !== null && r.signer_address === treasury,
      `status=${r.status} nonce=${r.nonce} tx_hash=${r.tx_hash} raw_tx=${r.raw_tx?.length ?? 0} hex chars`,
    );
    check(
      "the bytes the worker tried to send are exactly the persisted raw_tx (persist BEFORE broadcast)",
      interlock.state.sendRaw.length === 1 && interlock.state.sendRaw[0].raw === r.raw_tx,
      `broadcast attempts seen at the interlock: ${interlock.state.sendRaw.length}`,
    );
    const pending = await anvil("eth_getTransactionByHash", [r.tx_hash]);
    check(
      "nothing on chain: no such tx, treasury nonce and recipient balance unchanged, credits still attached",
      pending === null && (await nonceOf(treasury)) === before.nonce && (await balanceOf(W)) === before.recipient && attached(c.id) === 1,
      `eth_getTransactionByHash=${pending}; nonce ${await nonceOf(treasury)}; attached=${attached(c.id)}`,
    );

    delete process.env.TREASURY_RPC_URL; // the next runs talk to the fork directly
    const { runs, summaries } = await runUntilTerminal("c", c.id);
    check(
      "the next run on the direct fork re-broadcasts the persisted raw_tx; the run after reconciles it",
      summaries[0].reason === "in_flight" && row(c.id).status === "confirmed",
      `runs to terminal: ${runs}; first re-run reason=${summaries[0].reason}`,
    );
    const r2 = await assertConfirmed(c.id, before, "after re-broadcast");
    check("same hash as persisted before the crash; attempts counted", r2.tx_hash === r.tx_hash && r2.attempts >= 2, `tx_hash unchanged: ${r2.tx_hash === r.tx_hash}; attempts=${r2.attempts}`);
    check("treasury nonce +1 exactly", (await nonceOf(treasury)) === before.nonce + 1n);
    confirmedIds.push(c.id);
    note(`the second credit (${unclaimed(W).map((x) => x.amount).join(", ")}) stays claimable for ${W.slice(0, 10)}..`);
  });

  // ================================================================================================
  // d. FM-02: ambiguous broadcast
  // ================================================================================================
  await drill("d", "FM-02 ambiguous broadcast: the tx lands, the reply is lost -> never failed, paid exactly once", async () => {
    expectClean();
    const W = freshWallet();
    await credit("d", W, "0.01", "no_death");
    const c = await enqueue("d", W);
    const before = await snapshotChain(W);
    interlock.reset("forward_then_fail");
    process.env.TREASURY_RPC_URL = interlock.url;
    const s = await run("d#1 (interlock forwards the bytes, then answers 502)");
    const r = row(c.id);
    const nodeReply = interlock.state.droppedReplies[0] ?? "";
    check(
      "the bytes reached the node and the node accepted them (reply observed at the interlock, withheld from the worker)",
      interlock.state.sendRaw.length >= 1 && nodeReply.includes(r.tx_hash ?? "no-hash"),
      `forwarded eth_sendRawTransaction: ${interlock.state.sendRaw.length}; node's withheld reply: ${nodeReply.slice(0, 120)}`,
    );
    check(
      "the worker saw a transport failure, recorded it as an attempt and stopped",
      s.reason === "rpc_unavailable" && r.error === "rpc_unavailable" && r.attempts === 1,
      `reason=${s.reason} error=${r.error} attempts=${r.attempts}`,
    );
    check(
      "the claim is NOT failed and its credits are NOT released",
      r.status === "signed" && r.proof === null && attached(c.id) === 1 && unclaimed(W).length === 0,
      `status=${r.status} proof=${r.proof} attached=${attached(c.id)} claimable rows=${unclaimed(W).length}`,
    );
    const rcEarly = await receiptOf(r.tx_hash);
    check("meanwhile the chain has a status-1 receipt for the claim's hash (the viewer WAS paid)", rcEarly?.status === "0x1", `receipt status ${rcEarly?.status}`);

    // The reconcile run goes through the interlock in PASS mode (every request forwarded verbatim to
    // the fork, every reply returned) purely so that "it did not send again" is observed, not assumed.
    interlock.reset("pass");
    const s2 = await run("d#2 (interlock in pass-through mode)");
    delete process.env.TREASURY_RPC_URL;
    check(
      "the next run reconciles it to confirmed from the receipt, and sends nothing (0 eth_sendRawTransaction)",
      s2.paid === 1 && interlock.state.sendRaw.length === 0,
      `paid=${s2.paid} reason=${s2.reason}; eth_sendRawTransaction calls during the run: ${interlock.state.sendRaw.length}`,
    );
    await assertConfirmed(c.id, before, "ambiguous claim");
    const nonceAfter = await nonceOf(treasury);
    const s3 = await run("d#3 (idempotence)");
    check(
      "paid exactly ONCE: one nonce consumed, recipient delta == amount once, a further run changes nothing",
      nonceAfter === before.nonce + 1n && (await balanceOf(W)) - before.recipient === BigInt(r.amount_base) && s3.paid === 0 && (await nonceOf(treasury)) === nonceAfter,
      `nonce ${before.nonce} -> ${nonceAfter}; delta ${(await balanceOf(W)) - before.recipient}; next run paid=${s3.paid}`,
    );
    confirmedIds.push(c.id);
  });

  // ================================================================================================
  // e. Dropped tx
  // ================================================================================================
  await drill("e", "dropped tx: removed from the pool -> the persisted raw_tx is re-broadcast and confirms", async () => {
    expectClean();
    const W = freshWallet();
    await credit("e", W, "0.01", "two_deaths");
    const c = await enqueue("e", W);
    const before = await snapshotChain(W);
    await anvil("evm_setAutomine", [false]);
    const s = await run("e#1 (automine off)", 14_000);
    const r = row(c.id);
    const inPool = await anvil("eth_getTransactionByHash", [r.tx_hash]);
    check(
      "the worker broadcast it and left it in flight (no receipt inside the poll window)",
      s.reason === "awaiting_receipt" && r.status === "broadcast" && inPool !== null && inPool.blockNumber === null,
      `reason=${s.reason} status=${r.status} in pool: ${inPool !== null} (blockNumber ${inPool?.blockNumber})`,
    );
    const dropped = await anvil("anvil_dropTransaction", [r.tx_hash]);
    const gone = await anvil("eth_getTransactionByHash", [r.tx_hash]);
    check("anvil_dropTransaction removed it from the pool (method exists on this anvil)", gone === null, `anvil_dropTransaction -> ${dropped}; eth_getTransactionByHash -> ${gone}`);
    await anvil("evm_setAutomine", [true]);
    check("after the drop: claim still 'broadcast', credits attached, nonce unconsumed", row(c.id).status === "broadcast" && attached(c.id) === 1 && (await nonceOf(treasury)) === before.nonce);

    const { runs, summaries } = await runUntilTerminal("e", c.id);
    check(
      "the next run re-broadcasts the persisted raw_tx (chain nonce <= claim nonce) and a later run confirms it",
      summaries[0].reason === "in_flight" && row(c.id).status === "confirmed",
      `runs to terminal: ${runs}; first re-run reason=${summaries[0].reason}`,
    );
    const r2 = await assertConfirmed(c.id, before, "re-broadcast after drop");
    check("same hash, attempts counted, nonce +1", r2.tx_hash === r.tx_hash && r2.attempts >= 2 && (await nonceOf(treasury)) === before.nonce + 1n, `attempts=${r2.attempts}`);
    confirmedIds.push(c.id);
  });

  // ================================================================================================
  // f. Reverted tx
  // ================================================================================================
  await drill("f", "reverted tx: status-0 receipt -> failed receipt_reverted, credits claimable again", async () => {
    expectClean();
    const W = freshWallet();
    const credited = await credit("f", W, "0.01", "cops_cleared");
    const c = await enqueue("f", W);
    const before = await snapshotChain(W);
    await anvil("evm_setAutomine", [false]);
    const s = await run("f#1 (automine off)", 14_000);
    const r = row(c.id);
    check("broadcast and pending", s.reason === "awaiting_receipt" && r.status === "broadcast", `reason=${s.reason} status=${r.status}`);

    const saved = await balanceOf(treasury);
    const zero = await setTreasuryTokens(0n);
    check("treasury TTWO zeroed with anvil_setStorageAt before the block is mined (balanceOf reads 0)", zero === 0n, `balanceOf(treasury) ${saved} -> ${zero}`);
    await anvil("evm_mine");
    const rc = await receiptOf(r.tx_hash);
    const restored = await setTreasuryTokens(saved);
    await anvil("evm_setAutomine", [true]);
    check(
      "the mined transfer REVERTED: status-0 receipt, nonce consumed, recipient got nothing; treasury balance restored",
      rc?.status === "0x0" && (await nonceOf(treasury)) === before.nonce + 1n && (await balanceOf(W)) === before.recipient && restored === saved,
      `receipt status ${rc?.status} gasUsed ${rc ? BigInt(rc.gasUsed) : "-"}; nonce ${await nonceOf(treasury)}; balance restored ${restored === saved}`,
    );

    const s2 = await run("f#2");
    const r2 = row(c.id);
    check(
      "claim failed with proof receipt_reverted, receipt_status 0 (credits released only with that proof)",
      s2.failed === 1 && r2.status === "failed" && r2.proof === "receipt_reverted" && r2.receipt_status === 0,
      `failed=${s2.failed} status=${r2.status} proof=${r2.proof} receipt_status=${r2.receipt_status}`,
    );
    const free = unclaimed(W);
    check(
      "credits released: the same ledger row is claimable again, nothing attached to the failed claim",
      attached(c.id) === 0 && free.length === 1 && free[0].id === credited.id,
      `attached=${attached(c.id)} claimable=${j(free)}`,
    );
    check("treasury_accounts.next_nonce == chain nonce (the revert consumed the nonce)", BigInt(account().next_nonce) === (await nonceOf(treasury)));

    const again = await enqueue("f-again", W);
    const attachedIds = psqlJson(`select id from public.reward_ledger where claim_id = ${lit(again.id)}`).map((x) => x.id);
    check("a new claim takes exactly the released credit", attachedIds.length === 1 && attachedIds[0] === credited.id, `attached ids ${j(attachedIds)}`);
    const before2 = await snapshotChain(W);
    await run("f#3");
    await assertConfirmed(again.id, before2, "re-claim after revert");
    confirmedIds.push(again.id);
  });

  // ================================================================================================
  // g. Unknown nonce consumer
  // ================================================================================================
  await drill("g", "unknown nonce consumer: needs_review + halt; no further signing; operator restores service", async () => {
    expectClean();
    const W1 = freshWallet();
    await credit("g1", W1, "0.01", "no_death");
    const c1 = await enqueue("g1", W1);
    interlock.reset("refuse");
    process.env.TREASURY_RPC_URL = interlock.url;
    await run("g#1 (interlock refuses: claim left signed, not broadcast)");
    delete process.env.TREASURY_RPC_URL;
    const r1 = row(c1.id);
    check("claim signed, not broadcast", r1.status === "signed" && (await anvil("eth_getTransactionByHash", [r1.tx_hash])) === null, `status=${r1.status} nonce=${r1.nonce}`);

    const ext = await sendFromTreasuryKey(BigInt(r1.nonce));
    check(
      "a 0-value self-transfer from the treasury KEY consumes that nonce outside the system",
      ext.rc?.status === "0x1" && (await nonceOf(treasury)) === BigInt(r1.nonce) + 1n,
      `external tx ${ext.hash} at nonce ${r1.nonce}, status ${ext.rc?.status}`,
    );
    const s = await run("g#2");
    const r1b = row(c1.id);
    const a = account();
    check(
      "next run: claim -> needs_review, treasury halted (nonce_consumed_without_receipt)",
      r1b.status === "needs_review" && a.halted === true && a.halt_reason === "nonce_consumed_without_receipt" && s.reason === "halted",
      `status=${r1b.status} halted=${a.halted} halt_reason=${a.halt_reason} reason=${s.reason}`,
    );
    check("its credits are NOT released by the halt", attached(c1.id) === 1 && unclaimed(W1).length === 0);

    const W2 = freshWallet();
    await credit("g2", W2, "0.01", "two_deaths");
    const c2 = await enqueue("g2", W2);
    const nonceBefore = await nonceOf(treasury);
    const s2 = await run("g#3 (halted)");
    const r2 = row(c2.id);
    check(
      "a further claim is NOT signed while halted: stays queued, no nonce, no bytes, nothing on chain",
      s2.reason === "halted" && r2.status === "queued" && r2.nonce === null && r2.raw_tx === null && (await nonceOf(treasury)) === nonceBefore,
      `reason=${s2.reason} status=${r2.status} nonce=${r2.nonce}`,
    );
    const resumeTooEarly = psqlError(`select public.resume_payouts(${lit(treasury)});`);
    check("resume_payouts refuses while a claim is still in needs_review", resumeTooEarly !== null, resumeTooEarly);

    // The operator path, as the operator would type it (SQL editor / postgres).
    psql(`select public.resolve_claim_review(${lit(c1.id)}, 'failed', null, null);`);
    psql(`select public.resume_payouts(${lit(treasury)});`);
    const r1c = row(c1.id);
    check(
      "resolve_claim_review(id,'failed') -> failed/operator_review, credits released; resume_payouts clears the halt",
      r1c.status === "failed" && r1c.proof === "operator_review" && attached(c1.id) === 0 && unclaimed(W1).length === 1 && account().halted === false,
      `status=${r1c.status} proof=${r1c.proof} claimable rows for W1=${unclaimed(W1).length} halted=${account().halted}`,
    );
    const before2 = await snapshotChain(W2);
    await run("g#4 (resumed)");
    await assertConfirmed(c2.id, before2, "the held claim after resume");
    confirmedIds.push(c2.id);

    // Idle variant: nothing in flight, a queued claim waiting, an unrecorded tx from the key.
    const W3 = freshWallet();
    await credit("g3", W3, "0.01", "cops_cleared");
    const c3 = await enqueue("g3", W3);
    const chainN = await nonceOf(treasury);
    const idleExt = await sendFromTreasuryKey(chainN);
    const s3 = await run("g#5 (idle, unrecorded tx from the key)");
    const a3 = account();
    const r3 = row(c3.id);
    check(
      "idle variant: next run halts with halt_reason unrecorded_tx_from_treasury and signs nothing",
      idleExt.rc?.status === "0x1" && a3.halted === true && a3.halt_reason === "unrecorded_tx_from_treasury" && s3.reason === "halted" && r3.status === "queued" && r3.nonce === null,
      `external tx at nonce ${chainN} status ${idleExt.rc?.status}; halted=${a3.halted} (${a3.halt_reason}); claim ${r3.status} nonce=${r3.nonce}`,
    );
    // Runbook: correct next_nonce by hand to the chain's 'latest' count, then resume (resume_payouts' own comment).
    const latest = await nonceOf(treasury);
    psql(`update public.treasury_accounts set next_nonce = ${latest}, updated_at = now() where address = ${lit(treasury)};
          select public.resume_payouts(${lit(treasury)});`);
    const before3 = await snapshotChain(W3);
    await run("g#6 (after operator correction + resume)");
    await assertConfirmed(c3.id, before3, "queued claim after the idle halt");
    confirmedIds.push(c3.id);
    note(`W1's released credit (${unclaimed(W1).map((x) => x.amount).join(", ")}) stays claimable — the operator ruled the claim failed`);
  });

  // ================================================================================================
  // h. Cancel override
  // ================================================================================================
  await drill("h", "cancel override: recorded 0-value self-transfer at the nonce -> failed cancel_receipt, released", async () => {
    expectClean();
    const W = freshWallet();
    const credited = await credit("h", W, "0.01", "no_death");
    const c = await enqueue("h", W);
    interlock.reset("refuse");
    process.env.TREASURY_RPC_URL = interlock.url;
    await run("h#1 (interlock refuses: signed, not broadcast)");
    delete process.env.TREASURY_RPC_URL;
    const r = row(c.id);
    const cancel = await sendFromTreasuryKey(BigInt(r.nonce));
    psql(`select public.record_claim_override(${lit(c.id)}, ${lit(cancel.hash)}, 'cancel');`);
    const rOv = row(c.id);
    check(
      "operator's 0-value self-transfer mined at the claim's nonce; record_claim_override stored it",
      cancel.rc?.status === "0x1" && rOv.override_tx_hash === cancel.hash && rOv.override_kind === "cancel",
      `cancel tx ${cancel.hash} at nonce ${r.nonce}; override=${rOv.override_kind}`,
    );
    const s = await run("h#2");
    const r2 = row(c.id);
    check(
      "next run: failed with proof cancel_receipt, credits released, treasury NOT halted",
      s.failed === 1 && r2.status === "failed" && r2.proof === "cancel_receipt" && attached(c.id) === 0 && account().halted === false,
      `failed=${s.failed} status=${r2.status} proof=${r2.proof} halted=${account().halted}`,
    );
    const free = unclaimed(W);
    check(
      "the released credit is claimable again; the recipient received nothing; next_nonce == chain nonce",
      free.length === 1 && free[0].id === credited.id && (await balanceOf(W)) === 0n && BigInt(account().next_nonce) === (await nonceOf(treasury)),
      `claimable=${j(free)}`,
    );
    const again = await enqueue("h-again", W);
    const before = await snapshotChain(W);
    await run("h#3");
    await assertConfirmed(again.id, before, "re-claim after cancel");
    confirmedIds.push(again.id);
  });

  // ================================================================================================
  // i. Lease exclusion
  // ================================================================================================
  await drill("i", "lease exclusion: 3 concurrent workers, one queued claim -> one run works, one tx", async () => {
    expectClean();
    const W = freshWallet();
    await credit("i", W, "0.01", "two_deaths");
    const c = await enqueue("i", W);
    const before = await snapshotChain(W);
    const admins = [admin, createSupabaseAdmin(), createSupabaseAdmin()];
    const sums = await Promise.all(admins.map((a) => worker.runPayoutWorker(a, { budgetMs: 40_000 })));
    for (const [k, s] of sums.entries()) note(`concurrent run ${k + 1}: reason=${s.reason} paid=${s.paid}`);
    const workers = sums.filter((s) => s.paid === 1);
    const held = sums.filter((s) => s.reason === "lease_held");
    check("exactly one run reports the payout; the other two report lease_held", workers.length === 1 && held.length === 2, sums.map((s) => `${s.reason}/${s.paid}`).join(" | "));
    check("exactly one tx on chain: treasury nonce +1", (await nonceOf(treasury)) === before.nonce + 1n, `nonce ${before.nonce} -> ${await nonceOf(treasury)}`);
    await assertConfirmed(c.id, before, "leased claim");
    const fenced = await admin.rpc("mark_claim_broadcast", { p_holder: randomUUID(), p_claim_id: c.id });
    check("fencing: a mutating call from a holder that does not hold the lease is refused (P0010)", fenced.error?.code === "P0010", `error.code=${fenced.error?.code}`);
    check("the lease is released after the runs", leaseFree());
    confirmedIds.push(c.id);
  });

  // ================================================================================================
  // j. Kill switch
  // ================================================================================================
  await drill("j", "kill switch: rewards.payouts false -> nothing signed; true -> paid", async () => {
    expectClean();
    const W = freshWallet();
    await credit("j", W, "0.01", "cops_cleared");
    const c = await enqueue("j", W);
    const W2 = freshWallet();
    await credit("j2", W2, "0.01", "no_death");
    psql(`update public.site_config set value = '{"enabled": true, "payouts": false}'::jsonb where key = 'rewards';`);
    const refused = await claim(W2);
    check("I11: create_reward_claim raises P0005 while payouts are paused", refused.error?.code === "P0005", `error.code=${refused.error?.code}`);
    const before = await snapshotChain(W);
    const s = await run("j#1 (payouts false)");
    const r = row(c.id);
    check(
      "the run signs nothing: reason payouts_paused, claim queued with no nonce, no tx",
      s.enabled === false && s.reason === "payouts_paused" && r.status === "queued" && r.nonce === null && (await nonceOf(treasury)) === before.nonce,
      `enabled=${s.enabled} reason=${s.reason} status=${r.status} nonce=${r.nonce}`,
    );
    psql(`update public.site_config set value = '{"enabled": true, "payouts": true}'::jsonb where key = 'rewards';`);
    const s2 = await run("j#2 (payouts true)");
    check("flipped back without a deploy: the same queued claim is paid", s2.enabled === true && s2.paid === 1, `enabled=${s2.enabled} paid=${s2.paid}`);
    await assertConfirmed(c.id, before, "claim after the switch");
    confirmedIds.push(c.id);
    note(`W2's credit (${unclaimed(W2).map((x) => x.amount).join(", ")}) stays on the books, unclaimed`);
  });

  // ================================================================================================
  // k. Preflight refusals
  // ================================================================================================
  await drill("k", "preflight refusals: nothing is signed on a short treasury, a fee spike, a wrong or missing key", async () => {
    expectClean();
    const W = freshWallet();
    await credit("k", W, "0.05", "two_deaths");
    const c = await enqueue("k", W);
    const amountBase = toBaseUnits(c.amount_text, 18);
    const nextBefore = account().next_nonce;
    const nothingSigned = async () => {
      const r = row(c.id);
      return r.status === "queued" && r.nonce === null && r.raw_tx === null && account().next_nonce === nextBefore;
    };

    const saved = await balanceOf(treasury);
    const short = await setTreasuryTokens(amountBase - 1n);
    const s1 = await run("k#1 (treasury holds amount - 1)");
    check(
      "token balance below the claim -> reason insufficient_token_balance, claim stays queued, nothing signed",
      short === amountBase - 1n && s1.reason === "insufficient_token_balance" && (await nothingSigned()),
      `treasury balanceOf=${short} < amount_base=${amountBase}; reason=${s1.reason}`,
    );
    check("treasury balance restored", (await setTreasuryTokens(saved)) === saved);

    const blk = await anvil("eth_getBlockByNumber", ["latest", false]);
    await anvil("anvil_setNextBlockBaseFeePerGas", [hex(6_000_000_000n)]);
    const spiked = BigInt(await anvil("eth_gasPrice"));
    const s2 = await run("k#2 (next base fee 6 gwei)");
    check(
      "gas price above the 5 gwei ceiling -> reason gas_price_above_ceiling, nothing signed",
      spiked > worker.MAX_FEE_CEILING && s2.reason === "gas_price_above_ceiling" && (await nothingSigned()),
      `eth_gasPrice=${spiked} (anvil_setNextBlockBaseFeePerGas; anvil_setMinGasPrice is refused by this anvil under EIP-1559); reason=${s2.reason}`,
    );
    await anvil("anvil_setNextBlockBaseFeePerGas", [blk.baseFeePerGas]);
    const calm = BigInt(await anvil("eth_gasPrice"));
    check("fee restored below the ceiling", calm <= worker.MAX_FEE_CEILING, `eth_gasPrice=${calm}`);

    const savedAddr = process.env.NEXT_PUBLIC_TREASURY_ADDRESS;
    process.env.NEXT_PUBLIC_TREASURY_ADDRESS = freshWallet();
    const s3 = await run("k#3 (published treasury address != key)");
    process.env.NEXT_PUBLIC_TREASURY_ADDRESS = savedAddr;
    check("a key that is not the published treasury -> treasury_address_mismatch, lease never taken, nothing signed", s3.reason === "treasury_address_mismatch" && leaseFree() && (await nothingSigned()), `reason=${s3.reason}`);

    const savedKey = process.env.TREASURY_PRIVATE_KEY;
    delete process.env.TREASURY_PRIVATE_KEY;
    const s4 = await run("k#4 (no key)");
    process.env.TREASURY_PRIVATE_KEY = savedKey;
    check("no key -> treasury_not_configured, nothing signed", s4.reason === "treasury_not_configured" && (await nothingSigned()), `reason=${s4.reason}`);

    const before = await snapshotChain(W);
    await run("k#5 (all clear)");
    await assertConfirmed(c.id, before, "claim after the refusals");
    confirmedIds.push(c.id);
  });

  // ================================================================================================
  // l. Invariants
  // ================================================================================================
  await drill("l", "invariants I1-I14 over everything the drills left behind", async () => {
    // I13 first, so its claim is part of what the other invariants examine.
    const T = freshWallet();
    await credit("I13", T, "0.0000005", "no_death");
    const rawNum = await admin.from("reward_ledger").select("amount").eq("wallet", T).single();
    note(`for contrast: selecting the numeric column directly, supabase-js receives ${typeof rawNum.data?.amount} ${rawNum.data?.amount}`);
    const underRails = await claim(T);
    check("I13: under the decided rails (min 0.002) a 0.0000005 balance is refused P0003, not a 500", underRails.error?.code === "P0003", `error.code=${underRails.error?.code}`);
    const tiny = await claim(T, { min: "0.0000005", max: defaultRails().max });
    const tinyOk = !tiny.error && typeof tiny.data?.amount_text === "string" && tiny.data.amount_text === "0.000000500000000000";
    check(
      "I13: with min = the amount, create_reward_claim returns amount_text as the STRING \"0.000000500000000000\"",
      tinyOk,
      `amount_text: ${typeof tiny.data?.amount_text} ${j(tiny.data?.amount_text)}`,
    );
    if (tinyOk) {
      check("I13: toBaseUnits(amount_text) is exactly 500000000000n — no JS number in the path", toBaseUnits(tiny.data.amount_text, 18) === 500_000_000_000n);
      const before = await snapshotChain(T);
      await run("I13#1");
      await assertConfirmed(tiny.data.id, before, "0.0000005 claim");
      confirmedIds.push(tiny.data.id);
    }

    // I1
    const i1 = psql("select count(*) from public.reward_ledger l join public.reward_claims c on c.id = l.claim_id where c.status = 'failed';");
    check("I1: no failed claim owns a ledger row", i1 === "0", `rows attached to failed claims: ${i1}`);
    // I2
    const i2 = psqlJson(
      `select c.id, c.amount::text as amount, coalesce(sum(l.amount), 0)::text as attached
         from public.reward_claims c left join public.reward_ledger l on l.claim_id = c.id
        where c.status <> 'failed' group by c.id, c.amount having coalesce(sum(l.amount), 0) <> c.amount`,
    );
    const nonFailed = psql("select count(*) from public.reward_claims where status <> 'failed';");
    check("I2: sum(attached credits) == amount for every non-failed claim", i2.length === 0, `${nonFailed} non-failed claims checked; violations ${j(i2)}`);
    // I4
    const [i4] = psqlJson(
      `select count(nonce) as with_nonce, count(distinct nonce) as distinct_nonces, max(nonce)::text as max_nonce,
              (select next_nonce::text from public.treasury_accounts where address = ${lit(treasury)}) as next_nonce,
              (select count(*) from public.reward_claims where status in ('signed','broadcast') or (status = 'queued' and nonce is not null)) as open_nonces
         from public.reward_claims`,
    );
    const chainNonce = await nonceOf(treasury);
    check(
      "I4: nonces unique; max(nonce)+1 == next_nonce == chain nonce; nothing left holding a nonce",
      i4.with_nonce === i4.distinct_nonces && BigInt(i4.max_nonce) + 1n === BigInt(i4.next_nonce) && BigInt(i4.next_nonce) === chainNonce && i4.open_nonces === 0,
      `${i4.with_nonce} claims with a nonce (${i4.distinct_nonces} distinct), max ${i4.max_nonce}, next_nonce ${i4.next_nonce}, chain ${chainNonce}; ` +
        "chain nonces also consumed by 3 recorded-or-flagged external txs (g, g-idle, h)",
    );
    // I5
    const signedRows = psqlJson(
      `select id, status, nonce::text as nonce, raw_tx, tx_hash, chain_id, token_address, to_address, amount_base::text as amount_base,
              gas_limit::text as gas_limit, max_fee_per_gas::text as max_fee, max_priority_fee_per_gas::text as max_prio, signer_address
         from public.reward_claims where raw_tx is not null order by nonce`,
    );
    const bad = [];
    for (const r of signedRows) {
      const tx = parseTransaction(r.raw_tx);
      const fields = fromRlp(`0x${r.raw_tx.slice(4)}`, "hex");
      const signer = (await recoverTransactionAddress({ serializedTransaction: r.raw_tx })).toLowerCase();
      const ok =
        keccak256(r.raw_tx) === r.tx_hash &&
        tx.type === "eip1559" &&
        tx.chainId === CHAIN_ID &&
        r.chain_id === CHAIN_ID &&
        BigInt(tx.nonce) === BigInt(r.nonce) &&
        tx.to === r.token_address &&
        r.token_address === TTWO &&
        fields[6] === "0x" &&
        tx.data === encodeFunctionData({ abi: erc20, functionName: "transfer", args: [r.to_address, BigInt(r.amount_base)] }) &&
        tx.gas === BigInt(r.gas_limit) &&
        tx.maxFeePerGas === BigInt(r.max_fee) &&
        (tx.maxPriorityFeePerGas ?? 0n) === BigInt(r.max_prio) &&
        signer === r.signer_address &&
        signer === treasury &&
        tx.gas <= worker.GAS_LIMIT_CEILING &&
        tx.maxFeePerGas <= worker.MAX_FEE_CEILING &&
        tx.maxFeePerGas >= worker.FEE_FLOOR;
      if (!ok) bad.push(r.id);
    }
    check(
      "I5: for every row with raw_tx, keccak256(raw_tx) == tx_hash, and the bytes decode to exactly the row (chain, nonce, token, value 0, transfer(to, amount_base), gas, fees, signer)",
      signedRows.length > 0 && bad.length === 0,
      `${signedRows.length} signed rows decoded and recovered; mismatches ${j(bad)}`,
    );
    const incomplete = psql(
      `select count(*) from public.reward_claims where status in ('signed','broadcast','confirmed')
         and (nonce is null or raw_tx is null or tx_hash is null or gas_limit is null or max_fee_per_gas is null
              or chain_id is null or token_address is null or amount_base is null or signer_address is null);`,
    );
    check("I5: every signed/broadcast/confirmed claim carries the complete signed record", incomplete === "0", `incomplete rows: ${incomplete}`);

    // Chain truth for every terminal outcome, and the money adds up.
    const confirmed = psqlJson(`select id, wallet, tx_hash, block_number::text as block_number, amount_base::text as amount_base from public.reward_claims where status = 'confirmed'`);
    let receiptsOk = true;
    for (const c of confirmed) {
      const rc = await receiptOf(c.tx_hash);
      if (!rc || rc.status !== "0x1" || BigInt(rc.blockNumber) !== BigInt(c.block_number)) receiptsOk = false;
    }
    const reverted = psqlJson(`select tx_hash from public.reward_claims where proof = 'receipt_reverted'`);
    for (const c of reverted) {
      const rc = await receiptOf(c.tx_hash);
      if (!rc || rc.status !== "0x0") receiptsOk = false;
    }
    check(
      "every confirmed claim has a status-1 receipt at its recorded block; every receipt_reverted claim a status-0 one",
      receiptsOk && confirmed.length === confirmedIds.length,
      `${confirmed.length} confirmed (drills expected ${confirmedIds.length}), ${reverted.length} reverted`,
    );
    const paidOut = confirmed.reduce((acc, c) => acc + BigInt(c.amount_base), 0n);
    const treasuryOut = treasuryStart - (await balanceOf(treasury));
    let recipientsIn = 0n;
    for (const w of [...new Set(confirmed.map((c) => c.wallet))]) recipientsIn += await balanceOf(w);
    check(
      "the money adds up: treasury TTWO out == recipients' TTWO in == sum(amount_base of confirmed claims)",
      treasuryOut === paidOut && recipientsIn === paidOut,
      `treasury out ${treasuryOut}; recipients in ${recipientsIn}; confirmed amount_base sum ${paidOut}`,
    );

    // I6 / I7 from the service-role key — the product's own client, not superuser SQL.
    const target = confirmedIds[0];
    const reopen = await admin.from("reward_claims").update({ status: "failed", proof: "receipt_reverted" }).eq("id", target).select();
    check("I6: a confirmed claim cannot be reopened, even by the service-role key", reopen.error !== null && row(target).status === "confirmed", `error.code=${reopen.error?.code}`);
    const release = await admin.from("reward_ledger").update({ claim_id: null }).eq("claim_id", target).select();
    check("I7: the service-role key cannot release a credit by UPDATE (only the proof-carrying functions can)", release.error !== null && attached(target) > 0, `error.code=${release.error?.code}`);
    const nonceEdit = await admin.from("treasury_accounts").update({ next_nonce: 0 }).eq("address", treasury).select();
    check("the service-role key cannot move next_nonce or clear a halt by UPDATE (privilege revoked)", nonceEdit.error !== null, `error.code=${nonceEdit.error?.code}`);

    // I14
    const errs = psqlJson(`select distinct error from public.reward_claims where error is not null`).map((x) => x.error);
    check("I14: reward_claims.error holds only codes matching ^[a-z_]{1,48}$", errs.every((e) => /^[a-z_]{1,48}$/.test(e)), `distinct codes: ${j(errs)}`);
    const routeFiles = [];
    const walkRoutes = (dir) => {
      for (const e of readdirSync(dir)) {
        const full = join(dir, e);
        if (statSync(full).isDirectory()) walkRoutes(full);
        else if (/\.(ts|tsx)$/.test(e)) routeFiles.push(full);
      }
    };
    walkRoutes(join(webRoot, "src/app/api/rewards"));
    routeFiles.push(join(webRoot, "src/app/api/cron/tick/route.ts"));
    const hitsI14 = [];
    for (const f of routeFiles) {
      readFileSync(f, "utf8")
        .split("\n")
        .forEach((line, i) => {
          if (/"detail"|\bdetail\s*:/.test(line)) hitsI14.push(`${relative(repoRoot, f)}:${i + 1}: ${line.trim()}`);
        });
    }
    check(
      `I14: no '"detail"' / 'detail:' in the rewards routes or the cron route (${routeFiles.length} files scanned)`,
      hitsI14.length === 0,
      hitsI14.length ? `REPORTED, NOT FIXED (outside this script's ownership):\n${hitsI14.join("\n")}` : "none",
    );
  });
}

// ------------------------------------------------------------------------------------------------

console.log("WANTED — payout outbox verification: real code, real Postgres + PostgREST, real recording, anvil fork of chain 4663");

let fatal = null;
try {
  await main();
} catch (err) {
  fatal = err;
  results.push({ drill: currentDrill, name: "script ran to completion", ok: false });
  console.log(`\n  ERROR  ${errorText(err)}`);
} finally {
  if (interlock) await interlock.close();
  if (stackUp) {
    try {
      console.log(`\n${execFileSync("bash", [join(STACK_DIR, "down.sh")], { env: childEnv(), encoding: "utf8" }).trim()}`);
    } catch (err) {
      console.log(`down.sh failed: ${firstLine(errorText(err))}`);
    }
  }
  if (outRoot) rmSync(outRoot, { recursive: true, force: true });
}

section("RESULT");
const drills = [...new Set(results.map((r) => r.drill))];
for (const d of drills) {
  const rs = results.filter((r) => r.drill === d);
  const failed = rs.filter((r) => !r.ok);
  console.log(`  ${failed.length === 0 ? "PASS" : "FAIL"}  [${d}]  ${rs.length - failed.length}/${rs.length} checks`);
  for (const f of failed) console.log(`          FAIL: ${f.name}`);
}
const failures = results.filter((r) => !r.ok).length;
console.log(failures === 0 ? `\n  all ${results.length} checks passed\n` : `\n  ${failures} of ${results.length} checks FAILED\n`);
if (fatal) console.error(firstLine(errorText(fatal)));
process.exit(failures === 0 ? 0 : 1);
