#!/usr/bin/env node
// The treasury module — web/src/lib/rewards/treasury.ts, CONTRACTS-PREDICTIONS §10.4 — verified against
// the REAL Robinhood Chain mainnet (id 4663), as far as it can honestly go without one broadcast.
// Run: `npm run verify:treasury` (from web/).
//
// WHAT THIS COVERS. The module is the only code in the repo that reads TREASURY_PRIVATE_KEY or builds a
// signed transaction. Everything the payout worker relies on it for is exercised here through the REAL
// exported functions, compiled from the working tree with the project's own tsc:
//   * isTreasuryConfigured / treasuryAddress: the key must be 32 bytes of hex AND a scalar in [1, n) —
//     scalar 0 and the secp256k1 order n itself are refused before any library sees them (FM-17);
//   * RPC resolution: TREASURY_RPC_URL is read PER CALL and wins over the chain default; malformed means
//     treasury_not_configured, never a silent fall-back;
//   * readChainState / readLatestNonce against live mainnet, compared with raw JSON-RPC ground truth;
//   * simulateTransfer: a real chain revert from an unfunded treasury is `simulation_failed`;
//   * signTransfer: touches NO network; the raw tx decodes (viem parseTransaction) to chainId 4663, the
//     given nonce, to = token, value 0, data = transfer(recipient, exact base units), the given gas and
//     fees, recovers to the treasury address, and keccak256(raw) == the returned hash;
//   * broadcastRaw: a node refusal is `broadcast_rejected`, and the bytes offered are exactly the signed
//     bytes — observed at the interlock, which refuses them;
//   * getReceipt against a real mainnet transaction, an unknown hash (null) and a malformed one;
//   * every thrown error is a TreasuryError whose message is exactly its code, with no cause.
//
// THE ONE THING IT WILL NEVER DO: broadcast to mainnet. Two independent guarantees:
//   1. The treasury key is generated fresh in this process (never printed) and its ETH and TTWO balances
//      are read from the real chain and asserted ZERO before anything else runs: a transfer from it
//      cannot succeed anywhere.
//   2. Every RPC the product code can reach is an in-process interlock that forwards requests verbatim to
//      the real node and returns the real reply — except eth_sendRawTransaction / eth_sendTransaction /
//      eth_sendBundle, which it refuses and records. It never forwards one and never fabricates a success
//      or a hash. The script asserts at the end that no broadcast method was ever forwarded.
//
// The half of the payout path that needs a broadcast — confirm, revert, drop, crash-after-persist,
// ambiguous broadcast, lease exclusion — is proved on an anvil fork of this same chain by
// scripts/verify-payout.mjs, against the same compiled source.

import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { randomBytes } from "node:crypto";
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const webRoot = join(fileURLToPath(new URL(".", import.meta.url)), "..");

// The subject of this verification, re-read live below rather than trusted: TTWO at this address on
// chain 4663, 18 decimals, paused() == false (CONTRACTS-PREDICTIONS §1). The holder is a real address
// with a real TTWO balance — here so that "the fresh account holds zero" is proved by a reader that
// demonstrably returns non-zero for someone who does hold some.
const TTWO = "0x5e81213613b6b86eab4c6c50d718d34359459786";
const TTWO_HOLDER = "0x8366a39cc670b4001a1121b8f6a443a643e40951";
const MAINNET_RPC = "https://rpc.mainnet.chain.robinhood.com";
const CHAIN_ID = 4663;

// secp256k1 group order n (SEC 2 v2 §2.4.1). Public constants, not keys anyone holds.
const SECP256K1_N = BigInt("0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141");
const hex64 = (n) => `0x${n.toString(16).padStart(64, "0")}`;

// A settlement-shaped amount: 2.5 TTWO in whole units, i.e. what reward_claims.amount holds. On the
// wire it must be 2500000000000000000 base units and never 2, 25 or 2.5.
const SETTLEMENT_AMOUNT = "2.500000000000000000";
const SETTLEMENT_BASE_UNITS = BigInt("2500000000000000000");

const BROADCAST_METHODS = /^(eth_sendRawTransaction|eth_sendTransaction|eth_sendBundle)$/;
const READ_METHODS = new Set(["eth_chainId", "eth_getTransactionCount", "eth_getBalance", "eth_call", "eth_gasPrice", "eth_estimateGas", "eth_getTransactionReceipt", "eth_blockNumber"]);

let failures = 0;
const unproven = [];

function check(name, ok, detail) {
  if (!ok) failures += 1;
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

// --------------------------------------------------------------------------------------------
// Raw JSON-RPC straight at the real node: the independent ground truth. No project code, no interlock.
// --------------------------------------------------------------------------------------------

// The public endpoint sometimes answers a burst with an HTML page (a rate limiter) instead of JSON-RPC.
// A READ is retried a bounded number of times; a JSON-RPC error from the node is never retried and is
// returned as the node's own answer.
function rpcCaller(url) {
  let id = 0;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  return async function rpc(method, params) {
    let problem = "";
    for (let attempt = 1; attempt <= 5; attempt += 1) {
      let res;
      try {
        res = await fetch(url, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: ++id, method, params }),
        });
      } catch (err) {
        problem = `fetch failed: ${firstLine(errorText(err))}`;
        await sleep(2000 * 2 ** (attempt - 1));
        continue;
      }
      const text = await res.text();
      let body;
      try {
        body = JSON.parse(text);
      } catch {
        problem = `HTTP ${res.status} with a non-JSON body ("${text.slice(0, 24).replace(/\s+/g, " ")}...")`;
        await sleep(2000 * 2 ** (attempt - 1));
        continue;
      }
      if (body.error) {
        // A rate limiter speaking JSON-RPC is not the node's answer to the question; wait and ask again.
        if (res.status === 429 || /too many requests|rate limit/i.test(String(body.error.message))) {
          problem = `rate limited: ${body.error.message}`;
          await sleep(2000 * 2 ** (attempt - 1));
          continue;
        }
        const err = new Error(`${method}: ${body.error.message}`);
        err.rpcData = body.error.data;
        throw err;
      }
      return body.result;
    }
    throw new Error(`${method}: no JSON-RPC answer from ${url} after 5 attempts — last: ${problem}`);
  };
}

// --------------------------------------------------------------------------------------------
// The broadcast interlock: forwards everything verbatim, refuses every broadcast method.
// --------------------------------------------------------------------------------------------

async function startInterlock(upstream, label) {
  const forwarded = [];
  const blocked = [];
  const forwardedBroadcasts = []; // must stay empty forever; asserted at the end
  // Upstream replies that were NOT the node's answer — a rate limiter (429 / "Too Many Requests") or a
  // WAF page (e.g. HTTP 403 with HTML). Cumulative, never reset; used only as evidence for a bounded retry.
  const rateLimited = [];

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
      if (offenders.length > 0 || calls.length === 0) {
        for (const o of offenders) blocked.push({ method: o.method, params: o.params });
        const deny = (c) => ({
          jsonrpc: "2.0",
          id: c?.id ?? null,
          error: { code: -32000, message: "verify-treasury interlock: broadcast refused. This script never sends a transaction." },
        });
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify(Array.isArray(parsed) ? calls.map(deny) : deny(calls[0])));
        return;
      }
      for (const c of calls) {
        forwarded.push({ method: c?.method, params: c?.params });
        if (BROADCAST_METHODS.test(String(c?.method))) forwardedBroadcasts.push(c.method);
      }
      try {
        const up = await fetch(upstream, { method: "POST", headers: { "content-type": "application/json" }, body });
        const text = await up.text();
        let isJsonRpc = true;
        try {
          JSON.parse(text);
        } catch {
          isJsonRpc = false;
        }
        if (up.status === 429 || !isJsonRpc || /too many requests|rate limit/i.test(text)) {
          rateLimited.push({
            status: up.status,
            head: text.slice(0, 24).replace(/\s+/g, " "),
            methods: calls.map((c) => c?.method),
          });
        }
        res.writeHead(up.status, { "content-type": "application/json" });
        res.end(text);
      } catch (err) {
        res.writeHead(502, { "content-type": "application/json" });
        res.end(JSON.stringify({ jsonrpc: "2.0", id: null, error: { code: -32603, message: `interlock upstream failed: ${String(err)}` } }));
      }
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    label,
    url: `http://127.0.0.1:${server.address().port}`,
    forwarded,
    blocked,
    forwardedBroadcasts,
    rateLimited,
    reset() {
      forwarded.length = 0;
      blocked.length = 0;
    },
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

// --------------------------------------------------------------------------------------------
// Compile the real source with the project's own tsc, then resolve only module specifiers the way Next
// would at build time:
//   "@/lib/..."   -> the relative path to the same file in the compiled output (tsconfig paths)
//   "./x"         -> "./x.js" (Node ESM needs the extension; tsc does not rewrite specifiers)
//   "server-only" -> next/dist/compiled/server-only/empty.js, which is what next@15.5.23's own
//                    create-compiler-aliases.js maps `server-only$` to on the SERVER pass (the client
//                    pass gets index.js, which throws — the build-time guard treasury.ts relies on).
// --------------------------------------------------------------------------------------------

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
  const env = { ...process.env };
  delete env.TREASURY_PRIVATE_KEY;
  execFileSync("npx", ["tsc", "-p", tsconfigPath], { cwd: webRoot, stdio: "inherit", env });

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

// --------------------------------------------------------------------------------------------

async function main() {
  // This script never touches a funded treasury: an ambient key is dropped, and so is every ambient
  // RPC/asset override, so each result below is about the values set here.
  if (process.env.TREASURY_PRIVATE_KEY) {
    console.log("  NOTE  TREASURY_PRIVATE_KEY was present in the environment; it is ignored and replaced with a freshly generated, unfunded key.");
  }
  for (const k of [
    "TREASURY_PRIVATE_KEY",
    "TREASURY_RPC_URL",
    "NEXT_PUBLIC_RPC_URL",
    "NEXT_PUBLIC_CHAIN_RPC_URL",
    "NEXT_PUBLIC_CHAIN_ID",
    "NEXT_PUBLIC_TTWO_DECIMALS",
    "NEXT_PUBLIC_TTWO_SYMBOL",
  ]) {
    delete process.env[k];
  }

  const outRoot = mkdtempSync(join(webRoot, ".treasury-verify-"));
  const interlocks = [];

  try {
    const outDir = compileRealSource(outRoot, [
      "src/lib/rewards/treasury.ts",
      "src/lib/rewards/strategies.ts",
      "src/lib/chain/assets.ts",
      "src/lib/chain/config.ts",
      "src/app/api/_lib/amount.ts",
    ]);

    const {
      encodeFunctionData,
      decodeFunctionData,
      decodeFunctionResult,
      decodeErrorResult,
      isAddress,
      keccak256,
      parseTransaction,
      recoverTransactionAddress,
      fromRlp,
      toFunctionSelector,
    } = await import("viem");
    const { privateKeyToAccount, generatePrivateKey } = await import("viem/accounts");

    const ERC20 = [
      { type: "function", name: "decimals", stateMutability: "view", inputs: [], outputs: [{ type: "uint8" }] },
      { type: "function", name: "symbol", stateMutability: "view", inputs: [], outputs: [{ type: "string" }] },
      { type: "function", name: "paused", stateMutability: "view", inputs: [], outputs: [{ type: "bool" }] },
      { type: "function", name: "balanceOf", stateMutability: "view", inputs: [{ name: "a", type: "address" }], outputs: [{ type: "uint256" }] },
      { type: "function", name: "transfer", stateMutability: "nonpayable", inputs: [{ name: "to", type: "address" }, { name: "amount", type: "uint256" }], outputs: [{ type: "bool" }] },
    ];
    const ERC20_ERRORS = [
      { type: "error", name: "ERC20InsufficientBalance", inputs: [{ name: "sender", type: "address" }, { name: "balance", type: "uint256" }, { name: "needed", type: "uint256" }] },
    ];

    // The throwaway treasury. The key itself is never printed, logged or put in a check name.
    const treasuryKey = generatePrivateKey();
    const treasuryAddress = privateKeyToAccount(treasuryKey).address.toLowerCase();
    const recipient = privateKeyToAccount(generatePrivateKey()).address.toLowerCase();

    // ------------------------------------------------------------------------------------
    section(`[1] the real chain, read directly (raw JSON-RPC to ${MAINNET_RPC}, no project code)`);
    // ------------------------------------------------------------------------------------

    const rpc = rpcCaller(MAINNET_RPC);
    const readErc20 = async (fn, args, token = TTWO) =>
      decodeFunctionResult({
        abi: ERC20,
        functionName: fn,
        data: await rpc("eth_call", [{ to: token, data: encodeFunctionData({ abi: ERC20, functionName: fn, args }) }, "latest"]),
      });

    const chainIdHex = await rpc("eth_chainId", []);
    check(`chain id is ${CHAIN_ID} (Robinhood Chain mainnet)`, Number(BigInt(chainIdHex)) === CHAIN_ID, `eth_chainId -> ${chainIdHex}`);

    const onChainSymbol = await readErc20("symbol");
    const onChainDecimals = Number(await readErc20("decimals"));
    const onChainPaused = await readErc20("paused");
    check(
      `reward token at ${TTWO} answers symbol()/decimals()/paused()`,
      onChainSymbol === "TTWO" && onChainDecimals === 18 && onChainPaused === false,
      `symbol=${onChainSymbol}  decimals=${onChainDecimals}  paused=${onChainPaused}`,
    );

    const holderBalance = await readErc20("balanceOf", [TTWO_HOLDER]);
    check("the balance reader is alive: a real holder reads back non-zero", holderBalance > 0n, `balanceOf(${TTWO_HOLDER}) = ${holderBalance}`);

    const freshEth = BigInt(await rpc("eth_getBalance", [treasuryAddress, "latest"]));
    const freshTtwo = await readErc20("balanceOf", [treasuryAddress]);
    const freshNonce = BigInt(await rpc("eth_getTransactionCount", [treasuryAddress, "latest"]));
    const freshCode = await rpc("eth_getCode", [treasuryAddress, "latest"]);
    check(
      "a freshly generated treasury holds ZERO ETH and ZERO TTWO on mainnet, nonce 0, no code",
      freshEth === 0n && freshTtwo === 0n && freshNonce === 0n && freshCode === "0x",
      `${treasuryAddress}  eth=${freshEth}  ttwo=${freshTtwo}  nonce=${freshNonce}  code=${freshCode}`,
    );
    if (freshEth !== 0n || freshTtwo !== 0n) throw new Error("refusing to continue: the generated account is not empty");
    const rawGasPrice = BigInt(await rpc("eth_gasPrice", []));

    // ------------------------------------------------------------------------------------
    section("[2] the project's own chain readers, against the same chain, agreeing with [1]");
    // ------------------------------------------------------------------------------------

    // Two interlocks, both in front of the real node: one is the chain default (NEXT_PUBLIC_RPC_URL,
    // read by lib/chain/config at MODULE LOAD — so it is set before the import), the other is the
    // treasury override (TREASURY_RPC_URL, read by treasury.ts PER CALL). Both refuse every broadcast.
    const chainDefault = await startInterlock(MAINNET_RPC, "chain default");
    const override = await startInterlock(MAINNET_RPC, "TREASURY_RPC_URL");
    interlocks.push(chainDefault, override);
    process.env.NEXT_PUBLIC_RPC_URL = chainDefault.url;
    process.env.NEXT_PUBLIC_TTWO_TOKEN = TTWO;

    const url = (rel) => pathToFileURL(join(outDir, rel)).href;
    const chainConfig = await import(url("lib/chain/config.js"));
    const assets = await import(url("lib/chain/assets.js"));
    const amount = await import(url("app/api/_lib/amount.js"));
    const strategies = await import(url("lib/rewards/strategies.js"));
    const treasury = await import(url("lib/rewards/treasury.js"));
    note(`chain default RPC -> interlock ${chainDefault.url} -> ${MAINNET_RPC}`);

    const asset = assets.rewardAsset();
    check(
      "rewardAsset() resolves TTWO, and its decimals equal what the token reports on chain",
      asset !== null && asset.address.toLowerCase() === TTWO && asset.decimals === onChainDecimals && asset.symbol === "TTWO",
      asset ? `${asset.symbol} ${asset.address} decimals=${asset.decimals}` : "null",
    );
    check(
      "ROBINHOOD_CHAIN is id 4663 and its default RPC is the one set before import",
      chainConfig.isChainConfigured() === true && chainConfig.ROBINHOOD_CHAIN.id === CHAIN_ID && chainConfig.ROBINHOOD_CHAIN.rpcUrls.default.http[0] === chainDefault.url,
      `id=${chainConfig.ROBINHOOD_CHAIN.id} rpc=${chainConfig.ROBINHOOD_CHAIN.rpcUrls.default.http[0]}`,
    );
    // The holder is an active account — 204 TTWO transfers out and 158 in over 20,000 blocks (~34 min),
    // measured with `cast logs` on 2026-09-15 — so its balance moves between any two reads, and the public
    // node serves no historic state to pin a block with. The project's read is therefore bracketed by two
    // raw reads taken immediately around it rather than compared with a value from seconds earlier.
    const holderBefore = await readErc20("balanceOf", [TTWO_HOLDER]);
    const readVia = await assets.readErc20Balance(TTWO, TTWO_HOLDER);
    const holderAfter = await readErc20("balanceOf", [TTWO_HOLDER]);
    const lo = holderBefore < holderAfter ? holderBefore : holderAfter;
    const hi = holderBefore < holderAfter ? holderAfter : holderBefore;
    check(
      "readErc20Balance(holder) agrees with raw eth_call reads taken immediately before and after it",
      readVia >= lo && readVia <= hi,
      `raw before ${holderBefore}; project ${readVia}; raw after ${holderAfter} (the read in [1] was ${holderBalance})`,
    );
    const freshVia = await assets.readErc20Balance(TTWO, treasuryAddress);
    const pausedVia = await assets.isTokenPaused(TTWO);
    check(
      "readErc20Balance(fresh treasury) and isTokenPaused() agree exactly with [1]",
      freshVia === freshTtwo && pausedVia === onChainPaused,
      `treasury ${freshVia}; paused ${pausedVia}`,
    );

    // A TreasuryError must carry its code as its whole message, no cause, and nothing shaped like a key,
    // a hash or a raw transaction (principle 7 / FM-17).
    const leaks = (text, extra = []) => /0x[0-9a-fA-F]{64}/.test(text) || extra.some((s) => s && text.toLowerCase().includes(s.toLowerCase().replace(/^0x/, "")));
    async function expectCode(name, fn, code, extraSecrets = []) {
      let thrown = null;
      try {
        const v = await fn();
        check(name, false, `returned instead of throwing: ${JSON.stringify(v, (_k, x) => (typeof x === "bigint" ? x.toString() : x))}`);
        return null;
      } catch (err) {
        thrown = err;
      }
      const ok =
        thrown instanceof treasury.TreasuryError &&
        thrown.code === code &&
        thrown.message === code &&
        thrown.cause === undefined &&
        !leaks(String(thrown.stack ?? thrown.message), extraSecrets);
      check(name, ok, `${thrown?.name}: code=${thrown?.code} message="${thrown?.message}" cause=${thrown?.cause === undefined ? "none" : "PRESENT"}`);
      return thrown;
    }

    // The public endpoint rate-limits bursts. A product read that fails with rpc_unavailable is retried
    // ONLY when an interlock recorded a rate-limited upstream reply during that very attempt — evidence,
    // not hope — and every retry is printed. Any other failure is passed through exactly as it happened.
    async function transportRetry(label, fn) {
      const limitedSoFar = () => interlocks.reduce((n, i) => n + i.rateLimited.length, 0);
      for (let attempt = 1; ; attempt += 1) {
        const seen = limitedSoFar();
        try {
          return await fn();
        } catch (err) {
          const limited = limitedSoFar() > seen;
          if (attempt < 4 && limited && err instanceof treasury.TreasuryError && err.code === "rpc_unavailable") {
            const last = interlocks.flatMap((i) => i.rateLimited).pop();
            note(
              `${label}: the public RPC's reply was not the node's answer (HTTP ${last?.status} "${last?.head}", seen at the interlock); ` +
                `retry ${attempt} in ${attempt * 3}s`,
            );
            await new Promise((r) => setTimeout(r, attempt * 3000));
            continue;
          }
          throw err;
        }
      }
    }

    // ------------------------------------------------------------------------------------
    section("[3] isTreasuryConfigured() / treasuryAddress(): 32 bytes of hex AND a scalar in [1, n)");
    // ------------------------------------------------------------------------------------

    const keyCases = [
      ["no key set", undefined, false],
      ["whitespace only", "   ", false],
      ["present but malformed", "not-a-private-key", false],
      ["right alphabet, wrong length (20 bytes)", treasuryKey.slice(0, 42), false],
      ["scalar 0 (0x followed by 64 zeros)", hex64(0n), false],
      ["scalar n, the secp256k1 order itself", hex64(SECP256K1_N), false],
      ["scalar n + 1", hex64(SECP256K1_N + 1n), false],
      ["scalar 2^256 - 1", hex64((1n << 256n) - 1n), false],
      ["scalar n - 1 (the largest valid scalar)", hex64(SECP256K1_N - 1n), true],
      ["scalar 1 (the smallest valid scalar)", hex64(1n), true],
      ["the generated key, 0x-prefixed", treasuryKey, true],
      ["the generated key without 0x", treasuryKey.slice(2), true],
      ["the generated key in UPPERCASE hex", `0x${treasuryKey.slice(2).toUpperCase()}`, true],
    ];
    for (const [label, value, expected] of keyCases) {
      if (value === undefined) delete process.env.TREASURY_PRIVATE_KEY;
      else process.env.TREASURY_PRIVATE_KEY = value;
      const configured = treasury.isTreasuryConfigured();
      const addr = treasury.treasuryAddress();
      check(
        `${label.padEnd(44)} -> ${expected}`,
        configured === expected && (expected ? typeof addr === "string" && addr === addr.toLowerCase() && isAddress(addr) : addr === null),
        `isTreasuryConfigured()=${configured} treasuryAddress()=${addr === null ? "null" : "an address"}`,
      );
    }
    process.env.TREASURY_PRIVATE_KEY = treasuryKey;
    check("treasuryAddress() is the key's address, lowercase", treasury.treasuryAddress() === treasuryAddress, treasury.treasuryAddress());

    process.env.TREASURY_PRIVATE_KEY = hex64(SECP256K1_N);
    await expectCode("a key with scalar n is refused BEFORE any library sees it: treasury_key_invalid, no scalar in the error", () => treasury.readLatestNonce(), "treasury_key_invalid", [hex64(SECP256K1_N)]);
    process.env.TREASURY_PRIVATE_KEY = hex64(0n);
    await expectCode("a key with scalar 0 -> treasury_key_invalid", () => treasury.readLatestNonce(), "treasury_key_invalid");
    delete process.env.TREASURY_PRIVATE_KEY;
    await expectCode("no key -> treasury_not_configured", () => treasury.readLatestNonce(), "treasury_not_configured");
    process.env.TREASURY_PRIVATE_KEY = treasuryKey;

    // ------------------------------------------------------------------------------------
    section("[4] the reward math, end to end in base units (the 10^18 guard) and the claim rails");
    // ------------------------------------------------------------------------------------

    const converted = amount.toBaseUnits(SETTLEMENT_AMOUNT, onChainDecimals);
    check(`toBaseUnits("${SETTLEMENT_AMOUNT}", ${onChainDecimals}) === ${SETTLEMENT_BASE_UNITS}n`, converted === SETTLEMENT_BASE_UNITS, `got ${converted}`);
    check("fromBaseUnits() round-trips it back to the exact column text", amount.fromBaseUnits(converted, onChainDecimals) === SETTLEMENT_AMOUNT);
    check('regression witness: a whole-unit "2" is 2e18 base units, not 2', amount.toBaseUnits("2", onChainDecimals) === 2_000_000_000_000_000_000n);
    check("the smallest representable credit survives the shift", amount.toBaseUnits("0.000000000000000001", 18) === 1n && amount.fromBaseUnits(1n, 18) === "0.000000000000000001");
    check("sumBaseUnits() adds exactly (0.1 + 0.2 = 3e17)", amount.sumBaseUnits(["0.1", "0.2"], 18) === 300_000_000_000_000_000n);
    let overPrecise = null;
    try {
      amount.toBaseUnits("0.0000000000000000001", 18);
    } catch (err) {
      overPrecise = err;
    }
    check("more precision than the asset has is refused, never rounded away", overPrecise instanceof Error, overPrecise ? firstLine(errorText(overPrecise)) : "did not throw");
    check(
      "even_split floors in base units and leaves the dust with the treasury (§7)",
      strategies.computeRewardPerWallet({ pool: SETTLEMENT_BASE_UNITS, correctCount: 3 }) === 833_333_333_333_333_333n &&
        strategies.computeRewardPerWallet({ pool: SETTLEMENT_BASE_UNITS, correctCount: 0 }) === 0n,
    );
    delete process.env.CLAIM_MIN_AMOUNT;
    delete process.env.CLAIM_MAX_AMOUNT;
    let unsetRails = null;
    try {
      strategies.rewardLimits();
    } catch (err) {
      unsetRails = err;
    }
    check("rewardLimits(): unset rails are a refusal, not 'no limit' (§10.6)", unsetRails instanceof Error, unsetRails ? firstLine(errorText(unsetRails)) : "did not throw");
    process.env.CLAIM_MIN_AMOUNT = "2000000000000000";
    process.env.CLAIM_MAX_AMOUNT = "500000000000000000";
    const rails = strategies.rewardLimits();
    check(
      "rewardLimits(): the decided rails read as 0.002 / 0.5 TTWO",
      amount.fromBaseUnits(rails.minClaim, 18) === "0.002000000000000000" && amount.fromBaseUnits(rails.maxClaim, 18) === "0.500000000000000000",
    );
    process.env.CLAIM_MAX_AMOUNT = "0.5";
    let wholeUnitMistake = null;
    try {
      strategies.rewardLimits();
    } catch (err) {
      wholeUnitMistake = err;
    }
    check('rewardLimits(): CLAIM_MAX_AMOUNT="0.5" (whole units by mistake) is refused', wholeUnitMistake instanceof Error);
    delete process.env.CLAIM_MIN_AMOUNT;
    delete process.env.CLAIM_MAX_AMOUNT;

    // ------------------------------------------------------------------------------------
    section("[5] readLatestNonce() / readChainState() against mainnet; TREASURY_RPC_URL is read per call");
    // ------------------------------------------------------------------------------------

    chainDefault.reset();
    override.reset();
    delete process.env.TREASURY_RPC_URL;
    const nonceDefault = await transportRetry("readLatestNonce (chain default)", () => treasury.readLatestNonce());
    check(
      "TREASURY_RPC_URL unset -> the chain default RPC is used",
      nonceDefault === 0n && chainDefault.forwarded.some((c) => c.method === "eth_getTransactionCount") && override.forwarded.length === 0,
      `readLatestNonce()=${nonceDefault}; chain default saw ${chainDefault.forwarded.length} call(s), override saw ${override.forwarded.length}`,
    );

    chainDefault.reset();
    process.env.TREASURY_RPC_URL = override.url;
    const nonceOverride = await transportRetry("readLatestNonce (override)", () => treasury.readLatestNonce());
    const lastNonceCall = override.forwarded.find((c) => c.method === "eth_getTransactionCount");
    check(
      "TREASURY_RPC_URL set (no re-import) -> it wins on the very next call; 'latest', never 'pending'",
      nonceOverride === 0n && lastNonceCall !== undefined && lastNonceCall.params?.[1] === "latest" && chainDefault.forwarded.length === 0,
      `readLatestNonce()=${nonceOverride}; params=${JSON.stringify(lastNonceCall?.params)}; chain default saw ${chainDefault.forwarded.length}`,
    );

    // Malformed override: the property that matters is "refuse, never fall back to the public endpoint";
    // the code documented in treasury.ts's header for it is treasury_not_configured. Both are checked,
    // separately, on every reader that resolves the RPC.
    for (const [label, bad] of [["malformed", "not a url"], ["a non-http(s) scheme", "ftp://rpc.example"]]) {
      process.env.TREASURY_RPC_URL = bad;
      chainDefault.reset();
      override.reset();
      await expectCode(`TREASURY_RPC_URL ${label} -> readChainState() refuses: treasury_not_configured`, () => treasury.readChainState(TTWO), "treasury_not_configured");
      await expectCode(`TREASURY_RPC_URL ${label} -> simulateTransfer() refuses: treasury_not_configured`, () => treasury.simulateTransfer({ token: TTWO, to: recipient, amountBase: 1n }), "treasury_not_configured");
      await expectCode(`TREASURY_RPC_URL ${label} -> getReceipt() refuses: treasury_not_configured`, () => treasury.getReceipt(keccak256("0x01")), "treasury_not_configured");
      const nonceErr = await expectCode(`TREASURY_RPC_URL ${label} -> readLatestNonce() refuses: treasury_not_configured (the code treasury.ts documents)`, () => treasury.readLatestNonce(), "treasury_not_configured");
      if (nonceErr?.code === "rpc_unavailable") {
        note("readLatestNonce() calls rpc() INSIDE its try, so the catch relabels resolveRpcUrl()'s treasury_not_configured");
        note("as rpc_unavailable (src/lib/rewards/treasury.ts readLatestNonce). Still a refusal; wrong code. Reported, not fixed here.");
      }
      check(
        `TREASURY_RPC_URL ${label}: no silent fall-back — no request reached the chain default or any RPC`,
        chainDefault.forwarded.length + chainDefault.blocked.length + override.forwarded.length + override.blocked.length === 0,
        `chain default saw ${chainDefault.forwarded.length}, override saw ${override.forwarded.length}`,
      );
    }
    process.env.TREASURY_RPC_URL = override.url;

    override.reset();
    const state = await transportRetry("readChainState", () => treasury.readChainState(TTWO));
    check(
      "readChainState(TTWO) matches mainnet: chainId 4663, nonce 0, ETH 0, TTWO 0, paused false, gasPrice > 0",
      state.chainId === CHAIN_ID &&
        state.latestNonce === 0n &&
        state.ethBalance === 0n &&
        state.tokenBalance === freshTtwo &&
        state.paused === onChainPaused &&
        typeof state.gasPrice === "bigint" &&
        state.gasPrice > 0n,
      `chainId=${state.chainId} latestNonce=${state.latestNonce} eth=${state.ethBalance} ttwo=${state.tokenBalance} paused=${state.paused} gasPrice=${state.gasPrice} (raw eth_gasPrice in [1]: ${rawGasPrice})`,
    );
    note(`live mainnet gas price ${state.gasPrice} wei vs the worker's 5 gwei ceiling (MAX_FEE_CEILING) — observation, not a check`);
    const methods = [...new Set(override.forwarded.map((c) => c.method))];
    check(
      "readChainState issued only reads",
      methods.every((m) => READ_METHODS.has(m)) && override.blocked.length === 0,
      `methods: ${methods.join(", ")}`,
    );

    // ------------------------------------------------------------------------------------
    section("[6] simulateTransfer() from the unfunded treasury, against the real chain");
    // ------------------------------------------------------------------------------------

    override.reset();
    await expectCode("a zero amount is refused before any network call -> simulation_failed", () => treasury.simulateTransfer({ token: TTWO, to: recipient, amountBase: 0n }), "simulation_failed");
    await expectCode("a malformed recipient -> simulation_failed", () => treasury.simulateTransfer({ token: TTWO, to: "0xnot-an-address", amountBase: 1n }), "simulation_failed");
    check("...neither touched the network", override.forwarded.length === 0, `forwarded ${override.forwarded.length}`);

    override.reset();
    await expectCode(
      "a transfer the treasury cannot fund FAILS as simulation_failed (a real revert, not rpc_unavailable)",
      () => transportRetry("simulateTransfer", () => treasury.simulateTransfer({ token: TTWO, to: recipient, amountBase: SETTLEMENT_BASE_UNITS })),
      "simulation_failed",
    );
    const expectedCalldata = encodeFunctionData({ abi: ERC20, functionName: "transfer", args: [recipient, SETTLEMENT_BASE_UNITS] });
    const wireCall = override.forwarded.find(
      (c) => (c.method === "eth_call" || c.method === "eth_estimateGas") && String(c.params?.[0]?.data ?? "").startsWith(toFunctionSelector("transfer(address,uint256)")),
    );
    check(
      `the simulated call carried exactly ${SETTLEMENT_BASE_UNITS} base units, from the treasury, to TTWO`,
      wireCall !== undefined &&
        wireCall.params[0].data.toLowerCase() === expectedCalldata.toLowerCase() &&
        wireCall.params[0].to.toLowerCase() === TTWO &&
        wireCall.params[0].from.toLowerCase() === treasuryAddress,
      wireCall ? `${wireCall.method} to=${wireCall.params[0].to} data=${wireCall.params[0].data}` : "no transfer call observed",
    );
    check("nothing was broadcast", override.blocked.length === 0, `rpc issued: ${[...new Set(override.forwarded.map((c) => c.method))].join(", ")}`);

    let simError = null;
    try {
      await rpc("eth_call", [{ from: treasuryAddress, to: TTWO, data: expectedCalldata }, "latest"]);
    } catch (err) {
      simError = err;
    }
    let decoded = null;
    try {
      decoded = simError?.rpcData ? decodeErrorResult({ abi: ERC20_ERRORS, data: simError.rpcData }) : null;
    } catch {
      decoded = null;
    }
    check(
      "ground truth: the chain's own revert names the treasury, its zero balance and the amount (ERC20InsufficientBalance)",
      decoded?.errorName === "ERC20InsufficientBalance" &&
        decoded.args[0].toLowerCase() === treasuryAddress &&
        decoded.args[1] === 0n &&
        decoded.args[2] === SETTLEMENT_BASE_UNITS,
      decoded ? `${decoded.errorName}(sender=${decoded.args[0]}, balance=${decoded.args[1]}, needed=${decoded.args[2]})` : firstLine(errorText(simError)),
    );
    const holderSim = await rpc("eth_call", [{ from: TTWO_HOLDER, to: TTWO, data: expectedCalldata }, "latest"]);
    check(
      "the identical transfer from a FUNDED holder simulates true — only the balance is missing",
      decodeFunctionResult({ abi: ERC20, functionName: "transfer", data: holderSim }) === true,
      `eth_call from ${TTWO_HOLDER} -> true (simulation only; nothing was sent)`,
    );

    // ------------------------------------------------------------------------------------
    section("[7] signTransfer(): no network, and the bytes are exactly the transfer asked for");
    // ------------------------------------------------------------------------------------

    const params = {
      token: TTWO,
      to: recipient,
      amountBase: SETTLEMENT_BASE_UNITS,
      nonce: 7n,
      gasLimit: 101_000n,
      maxFeePerGas: 2_000_000_000n,
      maxPriorityFeePerGas: 0n,
    };
    chainDefault.reset();
    override.reset();
    const signed = await treasury.signTransfer(params);
    check(
      "signTransfer touched NO network (zero requests at either interlock)",
      chainDefault.forwarded.length + chainDefault.blocked.length + override.forwarded.length + override.blocked.length === 0,
      `chain default ${chainDefault.forwarded.length}/${chainDefault.blocked.length}, override ${override.forwarded.length}/${override.blocked.length}`,
    );

    const tx = parseTransaction(signed.raw);
    const fields = fromRlp(`0x${signed.raw.slice(4)}`, "hex");
    const call = decodeFunctionData({ abi: ERC20, data: tx.data });
    const signer = (await recoverTransactionAddress({ serializedTransaction: signed.raw })).toLowerCase();
    check("the raw tx is an EIP-1559 envelope (0x02), lowercase hex", signed.raw.startsWith("0x02") && signed.raw === signed.raw.toLowerCase() && tx.type === "eip1559", `${signed.raw.length} hex chars`);
    check("chainId 4663", tx.chainId === CHAIN_ID, `chainId=${tx.chainId}`);
    check("nonce is exactly the one given (7)", tx.nonce === 7, `nonce=${tx.nonce}`);
    check("to = the token", tx.to === TTWO, `to=${tx.to}`);
    check("value 0 (RLP value field is empty)", (tx.value ?? 0n) === 0n && fields[6] === "0x", `parsed value=${tx.value ?? "absent (= 0)"}; rlp=${fields[6]}`);
    check(
      `data = transfer(recipient, ${SETTLEMENT_BASE_UNITS}) — exact base units`,
      tx.data === expectedCalldata && call.functionName === "transfer" && call.args[0].toLowerCase() === recipient && call.args[1] === SETTLEMENT_BASE_UNITS,
      `transfer(${call.args[0]}, ${call.args[1]})`,
    );
    check(
      "gas limit and fees are exactly the ones given",
      tx.gas === params.gasLimit && tx.maxFeePerGas === params.maxFeePerGas && (tx.maxPriorityFeePerGas ?? 0n) === 0n,
      `gas=${tx.gas} maxFee=${tx.maxFeePerGas} prio=${tx.maxPriorityFeePerGas ?? 0n}`,
    );
    check("the signature recovers to the treasury address", signer === treasuryAddress, signer);
    check("keccak256(raw) == the returned hash", keccak256(signed.raw) === signed.hash, signed.hash);

    const again = await treasury.signTransfer(params);
    check("re-signing the same inputs yields the identical bytes (deterministic: a re-sign after a crash is the same tx)", again.raw === signed.raw && again.hash === signed.hash);
    const other = await treasury.signTransfer({ ...params, nonce: 8n });
    check("a different nonce is a different tx", other.hash !== signed.hash && parseTransaction(other.raw).nonce === 8);

    override.reset();
    await expectCode("amount 0 -> simulation_failed", () => treasury.signTransfer({ ...params, amountBase: 0n }), "simulation_failed");
    await expectCode("negative nonce -> simulation_failed", () => treasury.signTransfer({ ...params, nonce: -1n }), "simulation_failed");
    await expectCode("gas limit 0 -> simulation_failed", () => treasury.signTransfer({ ...params, gasLimit: 0n }), "simulation_failed");
    await expectCode("priority fee above max fee -> simulation_failed", () => treasury.signTransfer({ ...params, maxPriorityFeePerGas: params.maxFeePerGas + 1n }), "simulation_failed");
    await expectCode("malformed recipient -> simulation_failed", () => treasury.signTransfer({ ...params, to: "0x1234" }), "simulation_failed");
    delete process.env.TREASURY_PRIVATE_KEY;
    await expectCode("no key -> treasury_not_configured", () => treasury.signTransfer(params), "treasury_not_configured");
    process.env.TREASURY_PRIVATE_KEY = hex64(0n);
    await expectCode("scalar-0 key -> treasury_key_invalid", () => treasury.signTransfer(params), "treasury_key_invalid");
    process.env.TREASURY_PRIVATE_KEY = treasuryKey;
    check("...none of the refusals touched the network", override.forwarded.length + override.blocked.length === 0);

    // ------------------------------------------------------------------------------------
    section("[8] broadcastRaw(): refused at the interlock — the bytes offered are exactly the signed bytes");
    // ------------------------------------------------------------------------------------

    override.reset();
    const probe = await (
      await fetch(override.url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_sendRawTransaction", params: ["0xdeadbeef"] }),
      })
    ).json();
    check(
      "the interlock itself works: a broadcast is refused and recorded, never forwarded",
      override.blocked.length === 1 && override.forwarded.length === 0 && /interlock/.test(probe?.error?.message ?? ""),
      `blocked=${override.blocked.length} forwarded=${override.forwarded.length}`,
    );

    override.reset();
    await expectCode("broadcastRaw of malformed bytes -> broadcast_rejected, no network", () => treasury.broadcastRaw("0xzz"), "broadcast_rejected");
    check("...no request was made", override.forwarded.length + override.blocked.length === 0);

    override.reset();
    await expectCode(
      "broadcastRaw(signed.raw) when the node refuses -> broadcast_rejected; the error carries no raw tx and no hash",
      () => treasury.broadcastRaw(signed.raw),
      "broadcast_rejected",
      [signed.raw, signed.hash],
    );
    check(
      "exactly one broadcast was attempted, it carried exactly the signed bytes, and the interlock refused it",
      override.blocked.length === 1 && override.blocked[0].method === "eth_sendRawTransaction" && override.blocked[0].params?.[0] === signed.raw,
      `blocked=${override.blocked.length} (${override.blocked[0]?.method}); bytes identical: ${override.blocked[0]?.params?.[0] === signed.raw}`,
    );

    // ------------------------------------------------------------------------------------
    section("[9] getReceipt() against real mainnet transactions");
    // ------------------------------------------------------------------------------------

    let realHash = null;
    let block = await rpc("eth_getBlockByNumber", ["latest", false]);
    for (let i = 0; i < 50 && block && realHash === null; i += 1) {
      if (block.transactions.length > 0) realHash = block.transactions[block.transactions.length - 1];
      else block = await rpc("eth_getBlockByNumber", [`0x${(BigInt(block.number) - 1n).toString(16)}`, false]);
    }
    if (realHash) {
      const rawReceipt = await rpc("eth_getTransactionReceipt", [realHash]);
      override.reset();
      let r = null;
      let rErr = null;
      try {
        r = await transportRetry("getReceipt(real tx)", () => treasury.getReceipt(realHash));
      } catch (err) {
        rErr = err;
      }
      if (rErr) note(`getReceipt(real tx) threw ${errorText(rErr)}`);
      check(
        "getReceipt(a real mainnet tx) returns its real status and block number",
        rErr === null &&
          r !== null &&
          r.status === (rawReceipt.status === "0x1" ? "success" : "reverted") &&
          r.blockNumber === BigInt(rawReceipt.blockNumber),
        `${realHash}: ${r?.status} @ ${r?.blockNumber} (raw: status ${rawReceipt.status} block ${BigInt(rawReceipt.blockNumber)})`,
      );
    } else {
      check("found a real mainnet transaction in the last 50 blocks to read a receipt for", false);
    }
    const unknownHash = keccak256(`0x${randomBytes(32).toString("hex")}`);
    let none;
    try {
      none = await transportRetry("getReceipt(unknown hash)", () => treasury.getReceipt(unknownHash));
    } catch (err) {
      none = err;
    }
    const upstreamEvents = interlocks.flatMap((i) => i.rateLimited);
    check(
      "getReceipt(an unknown hash) returns null — 'no receipt', never 'failed'",
      none === null,
      `${unknownHash} -> ${none instanceof Error ? errorText(none) : none}` +
        (none instanceof Error && upstreamEvents.length
          ? `; non-node upstream replies so far: ${upstreamEvents.length} (last HTTP ${upstreamEvents.at(-1).status} "${upstreamEvents.at(-1).head}")`
          : ""),
    );
    override.reset();
    await expectCode("getReceipt(a malformed hash) -> rpc_unavailable, far from the 'no receipt' branch, no network", () => treasury.getReceipt("0x1234"), "rpc_unavailable");
    check("...no request was made", override.forwarded.length + override.blocked.length === 0);

    // ------------------------------------------------------------------------------------
    section("[10] the pause guard");
    // ------------------------------------------------------------------------------------

    check("the guard's input is a live read: TTWO reports paused() = false via readChainState", state.paused === false && onChainPaused === false);

    // A pause state that cannot be read must refuse. The treasury's own address is a real address on
    // the real chain with no code (eth_getCode "0x" in [1]), so the node genuinely returns empty data.
    override.reset();
    await expectCode(
      "pause state unreadable (token = a real code-less address) -> pause_unreadable",
      () => treasury.simulateTransfer({ token: treasuryAddress, to: recipient, amountBase: 1n }),
      "pause_unreadable",
    );
    check("...and it refused before broadcasting anything", override.blocked.length === 0);

    // ------------------------------------------------------------------------------------
    section("[11] nothing was ever broadcast to mainnet");
    // ------------------------------------------------------------------------------------

    const forwardedBroadcasts = interlocks.flatMap((i) => i.forwardedBroadcasts);
    check(
      "no eth_sendRawTransaction / eth_sendTransaction / eth_sendBundle was forwarded by either interlock, ever",
      forwardedBroadcasts.length === 0,
      `forwarded broadcasts: ${forwardedBroadcasts.length}`,
    );
    const nonceAfter = BigInt(await rpc("eth_getTransactionCount", [treasuryAddress, "latest"]));
    const pendingAfter = BigInt(await rpc("eth_getTransactionCount", [treasuryAddress, "pending"]));
    check("mainnet agrees: the treasury's nonce is still 0 ('latest' and 'pending')", nonceAfter === 0n && pendingAfter === 0n, `latest=${nonceAfter} pending=${pendingAfter}`);

    // ------------------------------------------------------------------------------------
    section("[12] the `token_paused` branch: a search of the chain for a real paused token (last: it is a burst of reads)");
    // ------------------------------------------------------------------------------------

    // A real search for a token on this chain that reports paused() == true, so the `token_paused`
    // branch can be driven by the chain rather than by a fabricated answer.
    let pausedFound = null;
    let scanned = 0;
    try {
      const listRes = await fetch("https://api.robinhood.com/rhj/assets", { signal: AbortSignal.timeout(20000) });
      const list = await listRes.json();
      const deployments = (list.assets ?? []).flatMap((a) =>
        (a.deployments ?? [])
          .filter((d) => d.chainId === CHAIN_ID && isAddress(d.contractAddress))
          .map((d) => ({ symbol: a.tokenSymbol, address: d.contractAddress })),
      );
      const pausedData = encodeFunctionData({ abi: ERC20, functionName: "paused" });
      for (let i = 0; i < deployments.length; i += 10) {
        const batch = deployments.slice(i, i + 10);
        const res = await Promise.all(batch.map((d) => rpc("eth_call", [{ to: d.address, data: pausedData }, "latest"]).catch(() => null)));
        res.forEach((raw, k) => {
          scanned += 1;
          if (raw && raw !== "0x" && BigInt(raw) === 1n) pausedFound = batch[k];
        });
      }
      note(`searched ${scanned} live token deployments on chain ${CHAIN_ID} for paused() == true`);
    } catch (err) {
      note(`could not complete the paused-token search: ${firstLine(errorText(err))}`);
    }
    if (pausedFound) {
      await expectCode(
        `paused() == true on a real token (${pausedFound.symbol}) -> token_paused`,
        () => treasury.simulateTransfer({ token: pausedFound.address.toLowerCase(), to: recipient, amountBase: 1n }),
        "token_paused",
      );
    } else {
      note("no contract on chain 4663 reports paused() == true, so the `token_paused` branch cannot be reached");
      note("without fabricating a chain response. It is NOT proved here.");
      unproven.push("simulateTransfer()'s `paused == true` -> token_paused branch: no contract found on chain 4663 that reports paused() == true.");
    }

    // The scan above reached the chain only through raw reads and, if it found a paused token, one
    // simulateTransfer through the interlock; re-assert that no broadcast was ever forwarded.
    check(
      "still no broadcast forwarded by either interlock after the scan",
      interlocks.flatMap((i) => i.forwardedBroadcasts).length === 0,
    );
    const limits = interlocks.flatMap((i) => i.rateLimited);
    note(`rate-limited upstream replies observed at the interlocks during this run: ${limits.length}${limits.length ? ` (${[...new Set(limits.flatMap((l) => l.methods))].join(", ")})` : ""}`);

    delete process.env.TREASURY_PRIVATE_KEY;
    delete process.env.TREASURY_RPC_URL;
  } finally {
    for (const i of interlocks) await i.close();
    rmSync(outRoot, { recursive: true, force: true });
  }
}

console.log("WANTED — treasury module (§10.4), verified against Robinhood Chain mainnet without a broadcast");

let fatal = null;
try {
  await main();
} catch (err) {
  fatal = err;
  failures += 1;
  console.log(`\n  ERROR  ${firstLine(errorText(err))}`);
}

unproven.push(
  "A mined payout on MAINNET. By design nothing here broadcasts; confirm / revert / drop / crash-after-persist / ambiguous broadcast / lease exclusion are proved on an anvil fork of chain 4663 by scripts/verify-payout.mjs, against the same compiled source.",
  "broadcastRaw()'s 'accepted' and 'known' outcomes against the real mainnet node (proved on the fork by verify-payout.mjs, drills c-e).",
);
section("UNPROVEN HERE — where the line actually is");
for (const item of unproven) console.log(`  ·  ${item}`);

console.log(failures === 0 ? "\n  The treasury module holds as far as an unfunded key on mainnet can prove it.\n" : `\n  ${failures} FAILED\n`);
if (fatal) console.error(firstLine(errorText(fatal)));
process.exit(failures === 0 ? 0 : 1);
