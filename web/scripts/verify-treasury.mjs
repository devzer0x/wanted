#!/usr/bin/env node
// The treasury payout path, exercised as far as it can honestly go without a funded wallet.
// Run: `npm run verify:treasury` (from web/).
//
// WHY THIS EXISTS
//
// `POST /api/rewards/claim` ends in three lines that have never run against the chain:
//
//     const amount = toBaseUnits(String(claimRow.amount), asset.decimals);
//     const { hash } = await sendReward({ to: wallet, amount });
//
// Everything before them is covered by SQL and route tests. Those two lines are where a viewer's
// reward becomes an ERC-20 transfer, and they are exactly where the two failure modes that matter
// live: a 10^18 unit error (already found and fixed once in this repo — see the header of
// src/app/api/_lib/amount.ts) and a payout that fails silently or, worse, reports a transaction
// hash for a transfer that never happened.
//
// This script runs the REAL compiled source against the REAL Robinhood Chain mainnet (id 4663).
// No mocks, no stubs, no invented chain responses.
//
// THE ONE THING IT WILL NOT DO: broadcast. A transfer is never signed and sent. Two independent
// mechanisms guarantee it:
//
//   1. The treasury it uses is a private key generated fresh in this process, whose ETH and TTWO
//      balances are read from the real chain and asserted to be zero before anything else runs.
//      An ERC-20 transfer from it cannot succeed, and gas estimation against the real node
//      reverts before viem ever reaches the signing step.
//   2. A local JSON-RPC interlock. The project's chain RPC is pointed at an in-process HTTP
//      server that forwards every request verbatim to the real upstream RPC and returns the real
//      response — except `eth_sendRawTransaction`, which it refuses and records. Nothing is
//      fabricated: reads are real chain data, and the one blocked method returns a loud error,
//      never a fake success and never a fake transaction hash. The interlock doubles as the
//      observation point for what the product code actually put on the wire.
//
// Like scripts/verify-siwe.mjs: no test runner, no new dependency. It compiles the real
// TypeScript with the project's own tsc and calls the real exported functions.

import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const webRoot = join(fileURLToPath(new URL(".", import.meta.url)), "..");

// The subject of this verification, not a product constant. docs/CONTRACTS-PREDICTIONS.md §1
// pins these on chain 4663 and this script re-reads every one of them live rather than trusting
// the table: TTWO at this address, 18 decimals, paused() == false. The holder is a real address
// carrying a real TTWO balance — it is here so that "the fresh account holds zero" is proved by a
// reader that demonstrably returns non-zero for someone who does hold some.
const VERIFIED_TTWO = "0x5e81213613b6B86EaB4c6c50d718d34359459786";
const VERIFIED_TTWO_HOLDER = "0x8366a39cc670b4001a1121b8f6a443a643e40951";
const DEFAULT_RPC_URL = "https://rpc.mainnet.chain.robinhood.com";
const EXPECTED_CHAIN_ID = 4663;

// A settlement-shaped amount: what create_reward_claim() writes into reward_claims.amount, a
// numeric(38,18) holding WHOLE TOKEN UNITS. 2.5 TTWO. The whole point of steps [4] and [6] is
// that this becomes 2500000000000000000 base units on the wire and never 2, 25 or 2.5.
const SETTLEMENT_AMOUNT = "2.500000000000000000";
const SETTLEMENT_BASE_UNITS = BigInt("2500000000000000000");

let failures = 0;
let unproven = [];

function check(name, ok, detail) {
  if (ok) {
    console.log(`  PASS  ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL  ${name}`);
  }
  if (detail) console.log(`        ${detail}`);
}

function note(text) {
  console.log(`        ${text}`);
}

function section(title) {
  console.log(`\n${title}`);
}

// --------------------------------------------------------------------------------------------
// Raw JSON-RPC, straight at the real node. No project code and no interlock in the path, so that
// step [2] has an independent ground truth to compare the project's own readers against.
// --------------------------------------------------------------------------------------------

function rpcCaller(url) {
  let id = 0;
  return async function rpc(method, params) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: ++id, method, params }),
    });
    const body = await res.json();
    if (body.error) {
      const err = new Error(`${method}: ${body.error.message}`);
      err.rpcData = body.error.data;
      err.rpcError = body.error;
      throw err;
    }
    return body.result;
  };
}

// --------------------------------------------------------------------------------------------
// The broadcast interlock.
// --------------------------------------------------------------------------------------------

const BROADCAST_METHODS = /^(eth_sendRawTransaction|eth_sendTransaction|eth_sendBundle)$/;

async function startInterlock(upstream) {
  const forwarded = [];
  const blocked = [];

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
      const offenders = calls.filter((c) => BROADCAST_METHODS.test(String(c?.method)));

      if (offenders.length > 0) {
        for (const o of offenders) blocked.push({ method: o.method, params: o.params });
        const deny = (c) => ({
          jsonrpc: "2.0",
          id: c?.id ?? null,
          error: {
            code: -32000,
            message:
              "verify-treasury interlock: broadcast refused. This script never sends a transaction.",
          },
        });
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify(Array.isArray(parsed) ? calls.map(deny) : deny(calls[0])));
        return;
      }

      for (const c of calls) forwarded.push({ method: c?.method, params: c?.params });

      try {
        const up = await fetch(upstream, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body,
        });
        const text = await up.text();
        res.writeHead(up.status, { "content-type": "application/json" });
        res.end(text);
      } catch (err) {
        res.writeHead(502, { "content-type": "application/json" });
        res.end(
          JSON.stringify({
            jsonrpc: "2.0",
            id: null,
            error: { code: -32603, message: `interlock upstream failed: ${String(err)}` },
          }),
        );
      }
    });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });

  return {
    url: `http://127.0.0.1:${server.address().port}`,
    forwarded,
    blocked,
    reset() {
      forwarded.length = 0;
      blocked.length = 0;
    },
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

// --------------------------------------------------------------------------------------------
// Compile the real source with the project's own tsc, then resolve the specifiers Next would
// have resolved at build time. Nothing here rewrites logic — only module specifiers:
//   "@/lib/..."   -> the relative path to the same file in the compiled output (tsconfig paths)
//   "./x"         -> "./x.js"                        (Node ESM wants the extension; tsc does not
//                                                     rewrite specifiers, bundlers do)
//   "server-only" -> next/dist/compiled/server-only/empty.js, which is what next's own
//                    create-compiler-aliases.js maps `server-only$` to on the SERVER compilation
//                    pass (the client pass gets index.js, which throws — that is the build-time
//                    guard treasury.ts relies on, and it is unaffected by running the server copy
//                    here). Verified in the installed next@15.5.23; the file is zero bytes.
// --------------------------------------------------------------------------------------------

function compileRealSource(outRoot) {
  const tsconfigPath = join(outRoot, "tsconfig.json");
  writeFileSync(
    tsconfigPath,
    JSON.stringify(
      {
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
        files: [
          "../next-env.d.ts",
          "../src/lib/rewards/treasury.ts",
          "../src/lib/rewards/strategies.ts",
          "../src/lib/chain/assets.ts",
          "../src/lib/chain/config.ts",
          "../src/app/api/_lib/amount.ts",
        ],
      },
      null,
      2,
    ),
  );

  execFileSync("npx", ["tsc", "-p", tsconfigPath], { cwd: webRoot, stdio: "inherit" });

  const outDir = join(outRoot, "out");
  // tsc emitted ES modules into .js. web/package.json has no "type", so without this Node would
  // fall back on syntax detection and warn on every file. Bare specifiers still resolve out to
  // web/node_modules, which is how the compiled code gets the project's own viem.
  writeFileSync(join(outDir, "package.json"), JSON.stringify({ type: "module" }));
  const serverOnlyEmpty = join(webRoot, "node_modules/next/dist/compiled/server-only/empty.js");

  const walk = (dir) =>
    readdirSync(dir).flatMap((entry) => {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) return walk(full);
      return full.endsWith(".js") ? [full] : [];
    });

  const toRelative = (fromDir, target) => {
    const r = relative(fromDir, target);
    return r.startsWith(".") ? r : `./${r}`;
  };

  for (const file of walk(outDir)) {
    const fromDir = dirname(file);
    const resolveSpecifier = (spec) => {
      if (spec === "server-only") return toRelative(fromDir, serverOnlyEmpty);
      if (spec.startsWith("@/")) return toRelative(fromDir, `${join(outDir, spec.slice(2))}.js`);
      if (spec.startsWith("./") || spec.startsWith("../")) {
        return spec.endsWith(".js") ? spec : `${spec}.js`;
      }
      return spec; // a real package; Node resolves it from web/node_modules
    };

    const rewritten = readFileSync(file, "utf8")
      .replace(/(\bfrom\s*)(["'])([^"']+)\2/g, (_m, p, q, s) => `${p}${q}${resolveSpecifier(s)}${q}`)
      .replace(/(\bimport\s*\(\s*)(["'])([^"']+)\2/g, (_m, p, q, s) => `${p}${q}${resolveSpecifier(s)}${q}`)
      .replace(/(\bimport\s+)(["'])([^"']+)\2/g, (_m, p, q, s) => `${p}${q}${resolveSpecifier(s)}${q}`);

    writeFileSync(file, rewritten);
  }

  return outDir;
}

// --------------------------------------------------------------------------------------------

function errorText(err) {
  return err instanceof Error ? `${err.name}: ${err.message}` : String(err);
}

function firstLine(text) {
  return String(text).split("\n")[0].trim();
}

/** A payout that lies would put a plausible tx hash in front of a caller. Nothing may. */
function containsTxHash(text) {
  return /0x[0-9a-fA-F]{64}\b/.test(String(text));
}

async function expectThrow(name, fn, matcher) {
  let thrown;
  try {
    const value = await fn();
    check(name, false, `returned instead of throwing: ${JSON.stringify(value, (_k, v) => (typeof v === "bigint" ? v.toString() : v))}`);
    return null;
  } catch (err) {
    thrown = err;
  }
  const text = errorText(thrown);
  const ok = matcher ? matcher(text) : true;
  check(name, ok && !containsTxHash(text), firstLine(text));
  if (containsTxHash(text)) note("!! the error text carries something shaped like a transaction hash");
  return thrown;
}

// --------------------------------------------------------------------------------------------

async function main() {
  const ambientRpc = process.env.NEXT_PUBLIC_CHAIN_RPC_URL?.trim() || DEFAULT_RPC_URL;

  // This script must never touch a funded treasury. If the caller's environment holds a real key,
  // it is dropped here and replaced with one generated in-process.
  if (process.env.TREASURY_PRIVATE_KEY) {
    console.log(
      "  NOTE  TREASURY_PRIVATE_KEY was present in the environment; it is ignored and replaced\n" +
        "        with a freshly generated, unfunded key. This script never spends a real treasury.",
    );
    delete process.env.TREASURY_PRIVATE_KEY;
  }

  const outRoot = mkdtempSync(join(webRoot, ".treasury-verify-"));
  let interlock = null;

  try {
    const outDir = compileRealSource(outRoot);

    const {
      encodeFunctionData,
      decodeFunctionResult,
      decodeErrorResult,
      isAddress,
      toFunctionSelector,
    } = await import("viem");
    const { privateKeyToAccount, generatePrivateKey } = await import("viem/accounts");

    // ABI used only by this script for raw encoding/decoding of the direct RPC calls. The product
    // ABIs live in src/lib/chain/assets.ts and src/lib/rewards/treasury.ts and are exercised
    // through the real exported functions in steps [2] and [6].
    const ERC20 = [
      { type: "function", name: "decimals", stateMutability: "view", inputs: [], outputs: [{ type: "uint8" }] },
      { type: "function", name: "symbol", stateMutability: "view", inputs: [], outputs: [{ type: "string" }] },
      { type: "function", name: "paused", stateMutability: "view", inputs: [], outputs: [{ type: "bool" }] },
      { type: "function", name: "balanceOf", stateMutability: "view", inputs: [{ name: "a", type: "address" }], outputs: [{ type: "uint256" }] },
      { type: "function", name: "transfer", stateMutability: "nonpayable", inputs: [{ name: "to", type: "address" }, { name: "amount", type: "uint256" }], outputs: [{ type: "bool" }] },
    ];
    const ERC20_ERRORS = [
      {
        type: "error",
        name: "ERC20InsufficientBalance",
        inputs: [
          { name: "sender", type: "address" },
          { name: "balance", type: "uint256" },
          { name: "needed", type: "uint256" },
        ],
      },
    ];

    const treasuryKey = generatePrivateKey();
    const treasuryAccount = privateKeyToAccount(treasuryKey);
    const treasuryAddress = treasuryAccount.address; // the key itself is never printed or logged

    // ------------------------------------------------------------------------------------
    section(`[1] the real chain, read directly (raw JSON-RPC to ${ambientRpc}, no project code)`);
    // ------------------------------------------------------------------------------------

    const rpc = rpcCaller(ambientRpc);
    const readErc20 = async (fn, args) =>
      decodeFunctionResult({
        abi: ERC20,
        functionName: fn,
        data: await rpc("eth_call", [{ to: VERIFIED_TTWO, data: encodeFunctionData({ abi: ERC20, functionName: fn, args }) }, "latest"]),
      });

    const chainIdHex = await rpc("eth_chainId", []);
    check(
      `chain id is ${EXPECTED_CHAIN_ID} (Robinhood Chain mainnet)`,
      Number(BigInt(chainIdHex)) === EXPECTED_CHAIN_ID,
      `eth_chainId -> ${chainIdHex} (${Number(BigInt(chainIdHex))})`,
    );

    const onChainSymbol = await readErc20("symbol");
    const onChainDecimals = Number(await readErc20("decimals"));
    const onChainPaused = await readErc20("paused");
    check(
      `reward token at ${VERIFIED_TTWO} answers symbol()/decimals()/paused()`,
      onChainSymbol === "TTWO" && onChainDecimals === 18 && typeof onChainPaused === "boolean",
      `symbol=${onChainSymbol}  decimals=${onChainDecimals}  paused=${onChainPaused}`,
    );

    const holderBalance = await readErc20("balanceOf", [VERIFIED_TTWO_HOLDER]);
    check(
      "the balance reader is alive: a real holder reads back non-zero",
      holderBalance > BigInt(0),
      `balanceOf(${VERIFIED_TTWO_HOLDER}) = ${holderBalance} base units`,
    );

    const freshEth = BigInt(await rpc("eth_getBalance", [treasuryAddress, "latest"]));
    const freshTtwo = await readErc20("balanceOf", [treasuryAddress]);
    const freshCode = await rpc("eth_getCode", [treasuryAddress, "latest"]);
    check(
      "a freshly generated treasury account holds ZERO ETH and ZERO TTWO on the real chain",
      freshEth === BigInt(0) && freshTtwo === BigInt(0),
      `${treasuryAddress}  eth=${freshEth}  ttwo=${freshTtwo}  code=${freshCode}`,
    );
    note("this is what an unfunded treasury actually looks like — every check below runs from it");

    if (freshEth !== BigInt(0) || freshTtwo !== BigInt(0)) {
      throw new Error("refusing to continue: the generated account is not empty");
    }

    // ------------------------------------------------------------------------------------
    section("[2] the project's own readers, against the same chain, agreeing with [1]");
    // ------------------------------------------------------------------------------------

    interlock = await startInterlock(ambientRpc);
    process.env.NEXT_PUBLIC_CHAIN_RPC_URL = interlock.url;
    process.env.NEXT_PUBLIC_TTWO_TOKEN = VERIFIED_TTWO;
    delete process.env.NEXT_PUBLIC_TTWO_DECIMALS; // exercise the default and compare it to chain
    delete process.env.NEXT_PUBLIC_TTWO_SYMBOL;

    const url = (rel) => pathToFileURL(join(outDir, rel)).href;
    const chainConfig = await import(url("lib/chain/config.js"));
    const assets = await import(url("lib/chain/assets.js"));
    const amount = await import(url("app/api/_lib/amount.js"));
    const strategies = await import(url("lib/rewards/strategies.js"));
    // A fresh module instance per scenario: treasury.ts memoises the resolved account in
    // `cachedAccount`, so each TREASURY_PRIVATE_KEY scenario needs its own copy of the module.
    const freshTreasury = (scenario) => import(`${url("lib/rewards/treasury.js")}?scenario=${scenario}`);

    note(`project RPC pointed at the interlock ${interlock.url} -> ${ambientRpc}`);

    const asset = assets.rewardAsset();
    check(
      "rewardAsset() resolves the configured reward asset",
      asset !== null && asset.address.toLowerCase() === VERIFIED_TTWO.toLowerCase(),
      asset ? `${asset.symbol} ${asset.address} decimals=${asset.decimals}` : "null",
    );
    check(
      "the decimals the app assumes match the decimals the token reports on chain",
      asset !== null && asset.decimals === onChainDecimals,
      `config=${asset?.decimals}  chain=${onChainDecimals}`,
    );
    check(
      "isChainConfigured() true, chain id matches the live node",
      chainConfig.isChainConfigured() === true && chainConfig.ROBINHOOD_CHAIN.id === Number(BigInt(chainIdHex)),
      `ROBINHOOD_CHAIN.id=${chainConfig.ROBINHOOD_CHAIN.id}`,
    );

    const readVia = await assets.readErc20Balance(VERIFIED_TTWO, VERIFIED_TTWO_HOLDER);
    const freshVia = await assets.readErc20Balance(VERIFIED_TTWO, treasuryAddress);
    check(
      "readErc20Balance() agrees exactly with the raw eth_call in [1]",
      readVia === holderBalance && freshVia === freshTtwo,
      `holder ${readVia} === ${holderBalance}; treasury ${freshVia} === ${freshTtwo}`,
    );

    const pausedVia = await assets.isTokenPaused(VERIFIED_TTWO);
    check(
      "isTokenPaused() reads the real paused() and agrees with [1]",
      pausedVia === onChainPaused,
      `isTokenPaused() = ${pausedVia}`,
    );

    // ------------------------------------------------------------------------------------
    section("[3] isTreasuryConfigured() — src/lib/rewards/treasury.ts, TREASURY_PRIVATE_KEY");
    // ------------------------------------------------------------------------------------

    const treasuryProbe = await freshTreasury("probe");
    delete process.env.TREASURY_PRIVATE_KEY;
    check("no key set                       -> false", treasuryProbe.isTreasuryConfigured() === false);

    process.env.TREASURY_PRIVATE_KEY = "   ";
    check("whitespace only                  -> false", treasuryProbe.isTreasuryConfigured() === false);

    process.env.TREASURY_PRIVATE_KEY = "not-a-private-key";
    check("present but malformed            -> false", treasuryProbe.isTreasuryConfigured() === false);
    note("presence alone is not enough: the value has to be 32 bytes of hex");

    process.env.TREASURY_PRIVATE_KEY = treasuryKey.slice(0, 40);
    check("right alphabet, wrong length     -> false", treasuryProbe.isTreasuryConfigured() === false);

    process.env.TREASURY_PRIVATE_KEY = treasuryKey;
    check("a real 0x-prefixed key           -> true", treasuryProbe.isTreasuryConfigured() === true);

    process.env.TREASURY_PRIVATE_KEY = treasuryKey.slice(2);
    check("the same key without the 0x      -> true", treasuryProbe.isTreasuryConfigured() === true);

    // ------------------------------------------------------------------------------------
    section("[4] the reward math, end to end in base units (the 10^18 guard)");
    // ------------------------------------------------------------------------------------

    const converted = amount.toBaseUnits(SETTLEMENT_AMOUNT, onChainDecimals);
    check(
      `toBaseUnits("${SETTLEMENT_AMOUNT}", ${onChainDecimals}) === ${SETTLEMENT_BASE_UNITS}n`,
      typeof converted === "bigint" && converted === SETTLEMENT_BASE_UNITS,
      `got ${converted} (${typeof converted}), decimals read live from the token in [1]`,
    );

    const roundTripped = amount.fromBaseUnits(converted, onChainDecimals);
    check(
      "fromBaseUnits() round-trips it back to the exact column text",
      roundTripped === SETTLEMENT_AMOUNT && amount.toBaseUnits(roundTripped, onChainDecimals) === converted,
      `"${roundTripped}"`,
    );

    check(
      "regression witness: a whole-unit \"2\" is 2e18 base units, not 2",
      amount.toBaseUnits("2", onChainDecimals) === BigInt("2000000000000000000"),
      `toBaseUnits("2", 18) = ${amount.toBaseUnits("2", onChainDecimals)} — reading the column as if it` +
        " already held base units is the 10^18 bug",
    );

    check(
      "the smallest representable credit survives the shift",
      amount.toBaseUnits("0.000000000000000001", onChainDecimals) === BigInt(1) &&
        amount.fromBaseUnits(BigInt(1), onChainDecimals) === "0.000000000000000001",
    );

    check(
      "sumBaseUnits() adds ledger rows exactly (no float, no rounding)",
      amount.sumBaseUnits(["0.1", "0.2"], onChainDecimals) === BigInt("300000000000000000") &&
        amount.sumBaseUnits(["2.500000000000000000", "2.500000000000000000"], onChainDecimals) === BigInt("5000000000000000000"),
      `0.1 + 0.2 = ${amount.sumBaseUnits(["0.1", "0.2"], onChainDecimals)} base units`,
    );

    let overPrecise = null;
    try {
      amount.toBaseUnits("0.0000000000000000001", onChainDecimals);
    } catch (err) {
      overPrecise = err;
    }
    check(
      "more precision than the asset has is refused, never rounded away",
      overPrecise instanceof Error,
      overPrecise ? firstLine(errorText(overPrecise)) : "did not throw",
    );

    check(
      "even_split floors in base units and leaves the dust with the treasury (§7)",
      strategies.computeRewardPerWallet({ pool: SETTLEMENT_BASE_UNITS, correctCount: 3 }) === BigInt("833333333333333333") &&
        strategies.computeRewardPerWallet({ pool: SETTLEMENT_BASE_UNITS, correctCount: 0 }) === BigInt(0),
      `2.5 TTWO / 3 = ${strategies.computeRewardPerWallet({ pool: SETTLEMENT_BASE_UNITS, correctCount: 3 })} base units`,
    );

    // ------------------------------------------------------------------------------------
    section("[5] sendReward() refuses before it can reach the chain");
    // ------------------------------------------------------------------------------------

    const recipient = privateKeyToAccount(generatePrivateKey()).address;

    interlock.reset(); // forget the reads [2] made; [5] must add nothing to this
    delete process.env.TREASURY_PRIVATE_KEY;
    const unconfigured = await freshTreasury("unconfigured");
    await expectThrow(
      "no TREASURY_PRIVATE_KEY -> throws, does not return a hash",
      () => unconfigured.sendReward({ to: recipient, amount: SETTLEMENT_BASE_UNITS }),
      (t) => /treasury is not configured/.test(t),
    );

    process.env.TREASURY_PRIVATE_KEY = treasuryKey;
    const guarded = await freshTreasury("guards");
    await expectThrow(
      "a zero amount is refused",
      () => guarded.sendReward({ to: recipient, amount: BigInt(0) }),
      (t) => /positive number of base units/.test(t),
    );
    await expectThrow(
      "a negative amount is refused",
      () => guarded.sendReward({ to: recipient, amount: BigInt(-1) }),
      (t) => /positive number of base units/.test(t),
    );
    await expectThrow(
      "a malformed recipient is refused",
      () => guarded.sendReward({ to: "0xnot-an-address", amount: SETTLEMENT_BASE_UNITS }),
      (t) => /not a valid address/.test(t),
    );

    const savedToken = process.env.NEXT_PUBLIC_TTWO_TOKEN;
    delete process.env.NEXT_PUBLIC_TTWO_TOKEN;
    await expectThrow(
      "no reward asset configured -> refuses",
      () => guarded.sendReward({ to: recipient, amount: SETTLEMENT_BASE_UNITS }),
      (t) => /reward asset is not configured/.test(t),
    );
    process.env.NEXT_PUBLIC_TTWO_TOKEN = savedToken;

    check(
      "none of [5] touched the network",
      interlock.forwarded.length === 0 && interlock.blocked.length === 0,
      `rpc calls forwarded: ${interlock.forwarded.length}, broadcasts blocked: ${interlock.blocked.length}`,
    );

    // ------------------------------------------------------------------------------------
    section("[6] sendReward() from an unfunded treasury, against the real chain");
    // ------------------------------------------------------------------------------------

    // Before trusting "nothing was broadcast", prove the interlock would have caught it. A raw
    // eth_sendRawTransaction is posted straight at it with a payload that is not a transaction —
    // no transaction is ever built or signed here, and the request never leaves this process.
    interlock.reset();
    const interlockProbe = await (
      await fetch(interlock.url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_sendRawTransaction", params: ["0xdeadbeef"] }),
      })
    ).json();
    check(
      "the interlock itself works: a broadcast attempt is refused and recorded, never forwarded",
      interlock.blocked.length === 1 && interlock.forwarded.length === 0 && /interlock/.test(interlockProbe?.error?.message ?? ""),
      `blocked=${interlock.blocked.length} forwarded=${interlock.forwarded.length} — "${interlockProbe?.error?.message ?? ""}"`,
    );

    interlock.reset();
    process.env.TREASURY_PRIVATE_KEY = treasuryKey;
    const live = await freshTreasury("unfunded-live");

    const thrown = await expectThrow(
      "an unfunded treasury FAILS LOUDLY — it throws, and returns no transaction hash",
      () => live.sendReward({ to: recipient, amount: SETTLEMENT_BASE_UNITS }),
    );
    note(`recipient ${recipient}`);
    note(`rpc methods the product code actually issued: ${interlock.forwarded.map((c) => c.method).join(", ") || "(none)"}`);

    check(
      "NOTHING WAS BROADCAST — no eth_sendRawTransaction reached, or was allowed past, the interlock",
      interlock.blocked.length === 0,
      interlock.blocked.length === 0
        ? "viem never got past gas estimation; the interlock had nothing to refuse"
        : `interlock REFUSED ${interlock.blocked.length} broadcast attempt(s)`,
    );
    check(
      "it did talk to the real chain rather than failing locally",
      interlock.forwarded.length > 0 && interlock.forwarded.some((c) => c.method === "eth_call"),
      `${interlock.forwarded.length} real JSON-RPC calls forwarded upstream`,
    );
    check(
      "the failure is a real chain rejection, not a fabricated one",
      thrown !== null && /revert|insufficient|gas/i.test(errorText(thrown)),
      firstLine(errorText(thrown)),
    );

    // What the product code put on the wire, observed at the interlock: this is where the
    // settlement value, the conversion in [4] and the ERC-20 call meet.
    const wireCall = interlock.forwarded.find(
      (c) => (c.method === "eth_estimateGas" || c.method === "eth_call") && typeof c.params?.[0]?.data === "string" && c.params[0].data.startsWith(toFunctionSelector("transfer(address,uint256)")),
    );
    const expectedCalldata = encodeFunctionData({
      abi: ERC20,
      functionName: "transfer",
      args: [recipient, SETTLEMENT_BASE_UNITS],
    });
    check(
      `the calldata sendReward() built carries exactly ${SETTLEMENT_BASE_UNITS} base units`,
      wireCall !== undefined &&
        wireCall.params[0].data.toLowerCase() === expectedCalldata.toLowerCase() &&
        wireCall.params[0].to.toLowerCase() === VERIFIED_TTWO.toLowerCase() &&
        wireCall.params[0].from.toLowerCase() === treasuryAddress.toLowerCase(),
      wireCall ? `${wireCall.method} to=${wireCall.params[0].to} data=${wireCall.params[0].data}` : "no transfer call observed",
    );

    // The same transfer simulated with eth_call straight at the real node — no interlock, no
    // project code, nothing broadcast. This is the independent proof that the payout cannot
    // succeed from this address, and the chain says why in its own words.
    let simError = null;
    try {
      await rpc("eth_call", [{ from: treasuryAddress, to: VERIFIED_TTWO, data: expectedCalldata }, "latest"]);
    } catch (err) {
      simError = err;
    }
    check(
      "eth_call simulation of the same transfer REVERTS on the real chain",
      simError !== null,
      simError ? firstLine(errorText(simError)) : "the simulation SUCCEEDED, which contradicts a zero balance",
    );
    if (simError?.rpcData) {
      let decoded = null;
      try {
        decoded = decodeErrorResult({ abi: ERC20_ERRORS, data: simError.rpcData });
      } catch {
        decoded = null;
      }
      if (decoded) {
        const [sender, balance, needed] = decoded.args;
        check(
          "the chain's own revert reason names the treasury, its zero balance and the amount owed",
          decoded.errorName === "ERC20InsufficientBalance" &&
            sender.toLowerCase() === treasuryAddress.toLowerCase() &&
            balance === BigInt(0) &&
            needed === SETTLEMENT_BASE_UNITS,
          `${decoded.errorName}(sender=${sender}, balance=${balance}, needed=${needed})`,
        );
      } else {
        note(`revert data (undecodable with this script's error ABI): ${simError.rpcData}`);
      }
    }

    // The same call from a real holder returns true, so the revert above is about the empty
    // treasury and not about the token refusing this recipient.
    const holderSim = await rpc("eth_call", [
      { from: VERIFIED_TTWO_HOLDER, to: VERIFIED_TTWO, data: expectedCalldata },
      "latest",
    ]);
    check(
      "the identical transfer from a FUNDED holder simulates true — only the balance is missing",
      decodeFunctionResult({ abi: ERC20, functionName: "transfer", data: holderSim }) === true,
      `eth_call from ${VERIFIED_TTWO_HOLDER} -> true (simulation only; nothing was sent)`,
    );

    // ------------------------------------------------------------------------------------
    section("[7] the pause guard");
    // ------------------------------------------------------------------------------------

    check(
      "the guard's input is a live read, not an assumption: TTWO reports paused() = false",
      onChainPaused === false && pausedVia === false,
      "so the branch that stops a payout is the one that cannot be triggered by pointing at TTWO",
    );

    // A real search for a token on this chain that would trigger `paused == true`. Nothing is
    // fabricated: this walks Robinhood's public asset list (CONTRACTS-PREDICTIONS §8) and calls
    // paused() on every mainnet deployment.
    let pausedFound = null;
    let scanned = 0;
    try {
      const listRes = await fetch("https://api.robinhood.com/rhj/assets", { signal: AbortSignal.timeout(20000) });
      const list = await listRes.json();
      const deployments = (list.assets ?? []).flatMap((a) =>
        (a.deployments ?? [])
          .filter((d) => d.chainId === EXPECTED_CHAIN_ID && isAddress(d.contractAddress))
          .map((d) => ({ symbol: a.tokenSymbol, address: d.contractAddress })),
      );
      const pausedData = encodeFunctionData({ abi: ERC20, functionName: "paused" });
      for (let i = 0; i < deployments.length; i += 25) {
        const batch = deployments.slice(i, i + 25);
        const results = await Promise.all(
          batch.map((d) =>
            rpc("eth_call", [{ to: d.address, data: pausedData }, "latest"]).catch(() => null),
          ),
        );
        results.forEach((raw, k) => {
          scanned += 1;
          if (raw && BigInt(raw) === BigInt(1)) pausedFound = batch[k];
        });
      }
      note(`searched ${scanned} live token deployments on chain ${EXPECTED_CHAIN_ID} for paused() == true`);
    } catch (err) {
      note(`could not complete the paused-token search: ${firstLine(errorText(err))}`);
    }

    if (pausedFound) {
      process.env.NEXT_PUBLIC_TTWO_TOKEN = pausedFound.address;
      process.env.TREASURY_PRIVATE_KEY = treasuryKey;
      const pausedRun = await freshTreasury("paused-token");
      await expectThrow(
        `paused() == true on a real token (${pausedFound.symbol}) -> sendReward refuses`,
        () => pausedRun.sendReward({ to: recipient, amount: SETTLEMENT_BASE_UNITS }),
        (t) => /reward token is paused/.test(t),
      );
      process.env.NEXT_PUBLIC_TTWO_TOKEN = VERIFIED_TTWO;
    } else {
      note(`no contract on chain ${EXPECTED_CHAIN_ID} reports paused() == true, so the`);
      note("`if (paused) throw` branch cannot be reached without fabricating a chain response.");
      note("It is NOT proved here, and this script will not pretend otherwise.");
      unproven.push(
        "sendReward()'s `paused == true` branch. No contract found on chain 4663 that reports " +
          "paused() == true, and faking the RPC answer would prove nothing about the real chain.",
      );
    }

    // What IS reachable, and matters just as much: a pause state that cannot be read must stop
    // the payout too. Pointing the reward asset at the treasury's own address — a real address on
    // the real chain with no code (eth_getCode returned "0x" in [1]) — makes the real node return
    // empty data for paused(), which is a genuine chain response, not a mock.
    process.env.NEXT_PUBLIC_TTWO_TOKEN = treasuryAddress;
    process.env.TREASURY_PRIVATE_KEY = treasuryKey;
    const unreadablePause = await freshTreasury("pause-unreadable");
    interlock.reset();
    await expectThrow(
      "pause state unreadable (asset points at a real code-less address) -> refuses to send",
      () => unreadablePause.sendReward({ to: recipient, amount: SETTLEMENT_BASE_UNITS }),
      (t) => /could not confirm reward token pause state, refusing to send/.test(t),
    );
    check(
      "...and it refused before broadcasting anything",
      interlock.blocked.length === 0 && !interlock.forwarded.some((c) => BROADCAST_METHODS.test(String(c.method))),
      `rpc issued: ${interlock.forwarded.map((c) => c.method).join(", ") || "(none)"}`,
    );
    process.env.NEXT_PUBLIC_TTWO_TOKEN = VERIFIED_TTWO;

    // ------------------------------------------------------------------------------------
    section("[8] an observation about the module cache, not a pass/fail");
    // ------------------------------------------------------------------------------------

    // treasury.ts memoises `cachedAccount` on first use, INCLUDING the null. A process that
    // called sendReward() once before TREASURY_PRIVATE_KEY was populated keeps refusing forever,
    // while isTreasuryConfigured() — which re-reads the env each call — says the treasury is fine.
    // Demonstrated here against the real module because it is part of the payout path.
    delete process.env.TREASURY_PRIVATE_KEY;
    const sticky = await freshTreasury("sticky-cache");
    await sticky.sendReward({ to: recipient, amount: SETTLEMENT_BASE_UNITS }).catch(() => {});
    process.env.TREASURY_PRIVATE_KEY = treasuryKey;
    let stickyMessage = "";
    try {
      await sticky.sendReward({ to: recipient, amount: SETTLEMENT_BASE_UNITS });
    } catch (err) {
      stickyMessage = errorText(err);
    }
    const stickyNull = /treasury is not configured/.test(stickyMessage);
    note(`a key was added AFTER a first sendReward() call in the same module instance`);
    note(`isTreasuryConfigured() now reports ${sticky.isTreasuryConfigured()}`);
    if (stickyNull) {
      note('sendReward() still says "treasury is not configured" — cachedAccount memoised the null.');
      note("Harmless on Vercel, where the env is populated before the first cold-start request,");
      note("but the two functions can disagree, and only a new process clears it.");
    } else {
      note("sendReward() picked the key up: the null is not memoised.");
    }

    delete process.env.TREASURY_PRIVATE_KEY;
  } finally {
    if (interlock) await interlock.close();
    rmSync(outRoot, { recursive: true, force: true });
  }
}

console.log("WANTED — treasury payout path, verified against Robinhood Chain mainnet");

let fatal = null;
try {
  await main();
} catch (err) {
  fatal = err;
  failures += 1;
  console.log(`\n  ERROR  ${errorText(err)}`);
}

// --------------------------------------------------------------------------------------------

unproven = unproven.concat([
  "A SUCCESSFUL payout. No transfer has ever been broadcast from this repo, by design: the " +
    "treasury holds no ETH for gas and no TTWO to send, and this script blocks broadcasting " +
    "outright. Everything up to and including the signed-transaction boundary is proved; the " +
    "boundary itself is not.",
  "eth_sendRawTransaction / receipt handling. viem fails at gas estimation before it signs, so " +
    "the sign-and-send path, the tx hash that `POST /api/rewards/claim` writes into " +
    "reward_claims.tx_hash via finalize_reward_claim, and any receipt or reorg handling are all " +
    "unexercised.",
  "Gas. Robinhood Chain charges ETH and the treasury has none, so fee estimation, the ETH " +
    "balance a payout needs, and the cost per claim are unmeasured.",
  "The route around sendReward(): create_reward_claim / finalize_reward_claim against a real " +
    "Supabase project. This script covers src/lib/rewards and src/app/api/_lib only.",
]);

section("UNPROVEN — where the line actually is");
for (const item of unproven) {
  console.log(`  ·  ${item}`);
}

console.log(
  failures === 0
    ? "\n  The payout path holds as far as an unfunded treasury can prove it.\n"
    : `\n  ${failures} FAILED\n`,
);

if (fatal) console.error(fatal);
process.exit(failures === 0 ? 0 : 1);
