#!/usr/bin/env node
// Drives a SCHEDULED ROUND (CONTRACTS-PREDICTIONS v2.4 §3) from the harness's own generator all the
// way to TTWO landing in a winner's wallet on a fork of Robinhood Chain mainnet — one command, no
// row anywhere invented by this script.
//
// Every other verifier in this directory starts from a prediction row a test inserted. This one
// starts one step earlier, at the only place production will ever start: the real
// PredictionGenerator, ranking the real CATALOG against the real RollingBaseRate, writing through
// the real PredictionWriter -> SupabaseWriter into a real PostgREST. Then the real
// lock_due_predictions() / settle_due_predictions_serialized() decide the result from the recorded
// telemetry, and the real payoutWorker signs the transfer.
//
// WHAT IS REAL:
//   * Postgres 16 with EVERY migration in infra/supabase/migrations (including
//     20260921000000_event_matches.sql), Supabase's roles and default privileges, a real PostgREST
//     v12 behind the shim that gives it Supabase's /rest/v1 URL shape, and an anvil fork of chain
//     4663 holding the real TTWO contract with real balances (web/scripts/payout-stack/up.sh).
//   * The telemetry is harness/tests/fixtures/real_session_2026-09-20.json — 230 events, 2 h 19 m,
//     a provenance-stamped pull from the production project. It is the recording whose measured
//     rates put the ambient templates inside §3's [0.20, 0.80] band.
//   * The questions are produced by the shipped harness code (web/scripts/rounds-generate.py imports
//     wasted_harness.predictions.{catalog,baserate,generator,writer} and wasted_harness.events).
//   * The settlement, the credit, the claim and the signature are the product's own SQL and
//     TypeScript, compiled from the working tree.
//
// THE ONE ADAPTATION, stated plainly: THE RECORDING IS REPLAYED ONTO NOW. Every event timestamp is
// moved by ONE constant offset, printed below as `offset_s`. Nothing else about any event changes —
// not its type, not its payload, not its order, not the gaps between them. That is what makes a
// settlement window that reads `[locks_at, resolves_at]` able to see real gameplay at all.
//
// The offset is chosen so that the round under test is OPEN RIGHT NOW: its 60 s entry window is
// still ahead, so the two wallets enter through the real `enter_prediction`, against Postgres's own
// clock, with nothing about the row touched. The recording therefore lands with its last few
// minutes still in the future — a session mid-flight, which is exactly the state production will be
// in when a viewer answers a card. (The alternative, landing the whole recording in the past, would
// have forced the entry window open by hand; `enter_prediction` refuses a locked row, and this
// script asserts that refusal rather than working around it.)
//
// Usage: cd web && node scripts/verify-rounds.mjs
//   env: HARNESS_PYTHON (default ../harness/.venv/bin/python) — needs the `supabase` package.
// Tears the stack down in `finally`. Only containers named wanted-payout-* are ever touched.

import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const webRoot = join(fileURLToPath(new URL(".", import.meta.url)), "..");
const repoRoot = join(webRoot, "..");
const STACK_DIR = join(webRoot, "scripts", "payout-stack");
const STACK_ENV = "/tmp/wanted-payout-stack/env";
const FIXTURE = join(repoRoot, "harness", "tests", "fixtures", "real_session_2026-09-20.json");
const ROUNDS_PY = join(webRoot, "scripts", "rounds-generate.py");
const LOADER = join(repoRoot, "scripts", "load-real-session.py");
const HARNESS_PY = process.env.HARNESS_PYTHON || join(repoRoot, "harness", ".venv", "bin", "python");

// Chain facts (CONTRACTS-PREDICTIONS §1), each re-read from the fork before it is relied on.
const TTWO = "0x5e81213613b6b86eab4c6c50d718d34359459786";
const TTWO_HOLDER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"; // a real holder, ON THE FORK only
const CHAIN_ID = 4663;
const FUND_TTWO = 10n ** 18n;
const FUND_ETH = 10n ** 18n;
// The decided §10.6 claim rails, in base units — what Vercel Production carries.
const CLAIM_MIN_AMOUNT = "2000000000000000"; // 0.002
const CLAIM_MAX_AMOUNT = "500000000000000000"; // 0.5

// §3's own numbers, restated here so a drift in the harness fails this script rather than passing it.
const ROUND_LOCK_S = 60;
const ROUND_WINDOW_S = 180;
const MIN_SAMPLES = 12;
const MIN_RATE = 0.2;
const MAX_RATE = 0.8;

// How far ahead of `now` the round under test opens its lock. It has to cover the shift, the
// generator replay and the assertions before the entry — measured at 1 s over two full runs, so
// this is deliberate slack, not a fitted number. The margin actually left is asserted and printed,
// never assumed; if a slower machine ever eats it, the run fails loudly instead of quietly
// entering a locked round.
const ENTRY_LEAD_S = 180;

let pass = 0;
let fail = 0;
let current = "setup";
const failures = [];
function check(name, ok, detail) {
  if (ok) pass += 1;
  else {
    fail += 1;
    failures.push(`[${current}] ${name}`);
  }
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}`);
  if (detail) for (const line of String(detail).split("\n")) console.log(`        ${line}`);
}
const note = (t) => console.log(`        ${t}`);
function section(title) {
  current = title;
  console.log(`\n${title}`);
}
const j = (v) => JSON.stringify(v, (_k, x) => (typeof x === "bigint" ? x.toString() : x));

// ------------------------------------------------------------------------------------------------
// Database access for SETUP and ASSERTIONS only. Every product action — generation, entry,
// settlement, the claim, the worker — goes through PostgREST with the service-role key.
// ------------------------------------------------------------------------------------------------
let PG = "wanted-payout-pg";
const lit = (v) => (v === null || v === undefined ? "null" : `'${String(v).replace(/'/g, "''")}'`);
function childEnv() {
  const env = { ...process.env };
  delete env.TREASURY_PRIVATE_KEY;
  delete env.SUPABASE_SECRET_KEY;
  return env;
}
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

// ------------------------------------------------------------------------------------------------
// Compile the real TypeScript with the project's own tsc (the technique verify-payout.mjs and
// verify-treasury.mjs both use), then resolve module specifiers the way Next would on the server.
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
// The harness half. Its stdout is JSON; its logs go to stderr and are shown.
// ------------------------------------------------------------------------------------------------
function runHarness(args, extraEnv = {}) {
  const env = { ...process.env, ...extraEnv };
  delete env.TREASURY_PRIVATE_KEY;
  const out = execFileSync(HARNESS_PY, [ROUNDS_PY, ...args], {
    cwd: repoRoot,
    env,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
    maxBuffer: 64 * 1024 * 1024,
  });
  return JSON.parse(out);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let stackUp = false;
let outRoot = null;

async function main() {
  for (const k of ["TREASURY_PRIVATE_KEY", "TREASURY_RPC_URL", "NEXT_PUBLIC_TREASURY_ADDRESS", "NEXT_PUBLIC_CHAIN_RPC_URL"]) {
    delete process.env[k];
  }
  if (!existsSync(HARNESS_PY)) throw new Error(`no harness interpreter at ${HARNESS_PY} (set HARNESS_PYTHON)`);

  // ============================================================================================
  section("[1] the stack, and the settlement branch this whole rehearsal depends on");
  // ============================================================================================
  execFileSync("bash", [join(STACK_DIR, "up.sh")], { env: childEnv(), encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], maxBuffer: 16 * 1024 * 1024 });
  stackUp = true;
  for (const line of readFileSync(STACK_ENV, "utf8").split("\n")) {
    const m = /^export ([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (m) process.env[m[1]] = m[2];
  }
  PG = process.env.PAYOUT_PG_CONTAINER || PG;
  ANVIL = process.env.PAYOUT_ANVIL_URL || ANVIL;
  const migrations = readdirSync(join(repoRoot, "infra/supabase/migrations")).filter((f) => f.endsWith(".sql"));
  check(
    `stack up: Postgres 16 + ${migrations.length} migrations, PostgREST v12 behind /rest/v1, anvil fork of 4663`,
    Boolean(process.env.SUPABASE_URL && process.env.SUPABASE_SECRET_KEY && process.env.NEXT_PUBLIC_RPC_URL),
    `SUPABASE_URL=${process.env.SUPABASE_URL}  fork=${ANVIL}`,
  );

  const settleDef = psql("select pg_get_functiondef('public.settle_due_predictions()'::regprocedure);");
  const hasGuard = settleDef.includes("jsonb_typeof(v_params -> 'payload_match') is distinct from 'object'");
  const hasMatch = settleDef.includes("payload @> (v_params -> 'payload_match')");
  const branchCount = (settleDef.match(/v_kind = 'event_matches'/g) || []).length;
  check(
    "the DEPLOYED settle_due_predictions() really carries the v2.4 event_matches branch",
    hasMatch && hasGuard && branchCount === 2,
    `pg_get_functiondef: ${settleDef.length} bytes; "v_kind = 'event_matches'" x${branchCount}; ` +
      `containment test present: ${hasMatch}; malformed_rule guard present: ${hasGuard}`,
  );
  if (!hasMatch) throw new Error("event_matches is not deployed; the rest of this rehearsal would be meaningless");

  // rewards + payouts ON in the LOCAL site_config. Set here, before settlement, because the credit
  // is written BY settlement and `reward_ledger_master_switch` refuses the insert while it is off.
  psql(`insert into public.site_config (key, value) values
          ('rewards', '{"enabled": true, "payouts": true}'::jsonb),
          ('reward_caps', '{"daily_cap": 100, "max_per_prediction": 100, "max_per_wallet_day": 100}'::jsonb)
        on conflict (key) do update set value = excluded.value;`);
  check(
    "local site_config: rewards {enabled, payouts} on, caps generous (this tests the path, not the clamps)",
    psql("select public.rewards_enabled()::text || ',' || public.payouts_enabled()::text;") === "true,true",
  );

  // ============================================================================================
  section("[2] the product's own TypeScript, compiled from the working tree with the project's tsc");
  // ============================================================================================
  // Compiled BEFORE the clock starts running on an open round: `npx tsc` takes seconds, and the
  // entry window this rehearsal opens is a real one that really closes.
  process.env.CLAIM_MIN_AMOUNT = CLAIM_MIN_AMOUNT;
  process.env.CLAIM_MAX_AMOUNT = CLAIM_MAX_AMOUNT;
  process.env.NEXT_PUBLIC_TTWO_TOKEN = process.env.NEXT_PUBLIC_TTWO_TOKEN || TTWO;
  outRoot = mkdtempSync(join(webRoot, ".rounds-verify-"));
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
  const { encodeFunctionData, keccak256 } = viem;
  const { generatePrivateKey, privateKeyToAccount } = await import("viem/accounts");
  const url = (rel) => pathToFileURL(join(outDir, rel)).href;
  const worker = await import(url("lib/rewards/payoutWorker.js"));
  const { createSupabaseAdmin } = await import(url("app/api/_lib/supabaseAdmin.js"));
  const { toBaseUnits, fromBaseUnits } = await import(url("app/api/_lib/amount.js"));
  const { rewardLimits } = await import(url("lib/rewards/strategies.js"));
  const { rewardAsset } = await import(url("lib/chain/assets.js"));
  const { ROBINHOOD_CHAIN } = await import(url("lib/chain/config.js"));
  const admin = createSupabaseAdmin();
  const asset = rewardAsset();
  if (!admin || !asset) throw new Error("cannot continue without the admin client and the reward asset");
  check(
    "the real worker, treasury, claim rails and asset config are the working tree's, pointed at the local stack",
    worker.LEASE_TTL_SECONDS === 120 && worker.MAX_PAYOUTS_PER_TICK === 5 &&
      asset.symbol === "TTWO" && asset.decimals === 18 && asset.address.toLowerCase() === TTWO &&
      ROBINHOOD_CHAIN.id === CHAIN_ID && ROBINHOOD_CHAIN.rpcUrls.default.http[0] === process.env.NEXT_PUBLIC_RPC_URL,
    `asset=${asset.symbol}/${asset.decimals}; chain=${ROBINHOOD_CHAIN.id} rpc=${ROBINHOOD_CHAIN.rpcUrls.default.http[0]}; ` +
      `claim rails ${rewardLimits().minClaim}..${rewardLimits().maxClaim} base units`,
  );

  // ============================================================================================
  section("[3] what the real generator WOULD ask, replayed in memory, before anything is loaded");
  // ============================================================================================
  const fixture = JSON.parse(readFileSync(FIXTURE, "utf8"));
  const SESSION_ID = fixture._provenance.session_id;
  const plan = runHarness(["plan", "--fixture", FIXTURE, "--session", SESSION_ID]);
  const rounds = plan.rounds;
  check(
    `the recording is the 09-20 production pull: ${fixture.events.length} events, ${(( plan.t1 - plan.t0) / 3600).toFixed(2)} h`,
    fixture.events.length === 230 && fixture._provenance.session_id === SESSION_ID,
    `session ${SESSION_ID}; span ${fixture._provenance.span[0]} .. ${fixture._provenance.span[1]}`,
  );
  check(
    `the real generator asks ${rounds.length} scheduled rounds over it (warm started from its first hour, ${plan.warm_start_rows} events)`,
    rounds.length >= 3,
    rounds.map((r, i) => `#${i} +${r.offset_from_t0_s.toFixed(0)}s ${r.prediction_type} cal=${r.calibration.yes}/${r.calibration.n} ${r.matching_event ? "window HAS a match" : "window has none"}`).join("\n"),
  );

  // The three rows this rehearsal is about, chosen from the recording, not arranged:
  //   A  the last round whose window contains a real matching event  -> entered, must settle YES
  //   B  the last round before A whose window contains none          -> ZERO entries, must void
  //   C  an earlier round whose window contains none                 -> entered, must settle NO
  // C is beyond the brief and proves the other half of the new branch: "absence is a definite
  // outcome_if_false". Its entries are inserted directly, because its entry window closed on the
  // shifted clock; the ENTRY PATH itself is proven on A, through the real enter_prediction.
  const idxA = rounds.map((r, i) => [r, i]).filter(([r]) => r.matching_event).map(([, i]) => i).pop();
  if (idxA === undefined) throw new Error("no round in this recording has a matching event in its window");
  const idxB = rounds.slice(0, idxA).map((r, i) => [r, i]).filter(([r]) => !r.matching_event).map(([, i]) => i).pop();
  if (idxB === undefined) throw new Error("no round without a matching event precedes the chosen one");
  const idxC = rounds.slice(0, idxB).map((r, i) => [r, i]).filter(([r]) => !r.matching_event).map(([, i]) => i).pop();
  if (idxC === undefined) throw new Error("no second round without a matching event");
  const A = rounds[idxA];
  const B = rounds[idxB];
  const C = rounds[idxC];
  note(`A = round #${idxA} ${A.prediction_type}: window holds a real ${A.matching_event.type} at ${A.matching_event.ts}`);
  note(`    payload ${j(A.matching_event.payload)}`);
  note(`    rule    ${j(A.telemetry_rule)}`);
  note(`B = round #${idxB} ${B.prediction_type}: no event matching ${j(B.telemetry_rule.params)} in its window -> stays empty`);
  note(`C = round #${idxC} ${C.prediction_type}: no event matching ${j(C.telemetry_rule.params)} in its window -> entered, must settle NO`);

  // ============================================================================================
  section("[4] the real recording, replayed onto now (one constant offset, nothing else changed)");
  // ============================================================================================
  const loadSql = execFileSync("python3", [LOADER, FIXTURE], { env: childEnv(), encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  writeFileSync(join(outRoot, "load.sql"), loadSql);
  execFileSync("docker", ["cp", join(outRoot, "load.sql"), `${PG}:/tmp/real_session_load.sql`], { env: childEnv() });
  execFileSync("docker", ["exec", PG, "psql", "-X", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-q", "-f", "/tmp/real_session_load.sql"], {
    env: childEnv(),
    stdio: ["ignore", "ignore", "pipe"],
  });
  const loaded = Number(psql(`select count(*) from public.events where session_id = ${lit(SESSION_ID)};`));
  check(
    "the recording is loaded verbatim by scripts/load-real-session.py (extended with a fixture argument)",
    loaded === fixture.events.length,
    `${loaded} events under session ${SESSION_ID}, ${fixture.events.length} in the file`,
  );

  // ONE constant offset, applied in ONE update over the loaded rows. Chosen so A's lock is
  // ENTRY_LEAD_S ahead of the database's own clock.
  const nowDb = Number(psql("select extract(epoch from now());"));
  const OFFSET = nowDb + ENTRY_LEAD_S - A.locks_epoch;
  const shifted = psql(
    `update public.events set ts = ts + make_interval(secs => ${OFFSET}) where session_id = ${lit(SESSION_ID)} returning 1;`,
  )
    .split("\n")
    .filter(Boolean).length;
  psql(`update public.sessions set started_at = started_at + make_interval(secs => ${OFFSET}) where id = ${lit(SESSION_ID)};
        insert into public.stats (session_id, heartbeat_at) values (${lit(SESSION_ID)}, now())
          on conflict (session_id) do update set heartbeat_at = now();`);
  const [span] = psqlJson(
    `select min(ts)::text as first, max(ts)::text as last,
            extract(epoch from (max(ts) - now())) as tail_s, count(*) as n
       from public.events where session_id = ${lit(SESSION_ID)}`,
  );
  check(
    `every event moved by one offset of ${OFFSET.toFixed(3)} s (${(OFFSET / 86400).toFixed(2)} days); payloads, types and order untouched`,
    shifted === loaded && Number(span.n) === loaded,
    `${shifted} rows shifted; recording now spans ${span.first} .. ${span.last} ` +
      `(its last event is ${Number(span.tail_s).toFixed(0)} s from now — the session is mid-flight, as production will be)`,
  );
  check(
    "the session reads LIVE: stats.heartbeat_at is fresh",
    psql(`select (now() - heartbeat_at < interval '5 seconds')::text from public.stats where session_id = ${lit(SESSION_ID)};`) === "true",
  );

  // ============================================================================================
  section("[5] the REAL harness generator, writing into the REAL PostgREST");
  // ============================================================================================
  const stateDir = join(outRoot, "harness-state");
  const run = runHarness(["run", "--fixture", FIXTURE, "--session", SESSION_ID, "--offset", String(OFFSET)], {
    SUPABASE_URL: process.env.SUPABASE_URL,
    SUPABASE_SECRET_KEY: process.env.SUPABASE_SECRET_KEY,
    WASTED_STATE_DIR: stateDir,
  });
  check(
    `RollingBaseRate warm started by READING ${run.warm_start_rows} real events back out of the database ` +
      `(the production restart path, main._warm_start_base_rate)`,
    run.warm_start_rows === plan.warm_start_rows && run.warm_start_span_s > 0,
    `${run.warm_start_rows} rows, ${run.warm_start_span_s}s of observed history`,
  );
  check(
    "every generated row reached Supabase through PredictionWriter -> SupabaseWriter.flush(); nothing fell to the offline queue",
    run.flushed === true && run.write_failures === 0 && !existsSync(join(stateDir, "queue.jsonl")),
    `flushed=${run.flushed} predictions_write_failures=${run.write_failures} last_error=${run.last_error ?? "none"}`,
  );

  const rows = psqlJson(
    `select id, prediction_type, question, status::text as status, is_event,
            state_context, telemetry_rule, outcomes, reward_pool::text as reward_pool, reward_asset,
            extract(epoch from (locks_at - opened_at)) as entry_s,
            extract(epoch from (resolves_at - locks_at)) as window_s,
            extract(epoch from opened_at) as opened_epoch, entry_count
       from public.predictions where session_id = ${lit(SESSION_ID)} order by opened_at`,
  );
  check(
    `the generator's own rows are in public.predictions: ${rows.length} scheduled rounds (>= 3 required)`,
    rows.length >= 3 && rows.length === run.written && rows.length === rounds.length,
    `${rows.length} rows; the generator produced ${run.generated}, wrote ${run.written}; the in-memory plan predicted ${rounds.length}`,
  );
  check(
    "the database rows are the rounds the in-memory replay predicted, in order and to the second",
    rows.every((r, i) => r.prediction_type === rounds[i].prediction_type && Math.abs(r.opened_epoch - (rounds[i].opened_epoch + OFFSET)) < 0.001),
    rows.map((r, i) => `${r.prediction_type} opened +${(r.opened_epoch - (plan.t0 + OFFSET)).toFixed(0)}s (planned +${rounds[i].offset_from_t0_s.toFixed(0)}s)`).slice(0, 4).join("\n") + "\n        ...",
  );

  const outcomeKeys = (r) => new Set(r.outcomes.map((o) => o.key));
  const badRule = rows.find((r) => {
    const t = r.telemetry_rule;
    const p = t?.params ?? {};
    return (
      t?.kind !== "event_matches" ||
      typeof p.event_type !== "string" ||
      p.event_type.length === 0 ||
      typeof p.payload_match !== "object" ||
      p.payload_match === null ||
      Array.isArray(p.payload_match) ||
      Object.keys(p.payload_match).length === 0 ||
      !outcomeKeys(r).has(t.outcome_if_true) ||
      !outcomeKeys(r).has(t.outcome_if_false)
    );
  });
  check(
    "every row is kind `event_matches` in the contract's literal shape (non-empty event_type, non-empty object payload_match, both outcomes real keys)",
    badRule === undefined,
    badRule ? `offender: ${j(badRule.telemetry_rule)}` : `e.g. ${j(rows[0].telemetry_rule)}`,
  );
  const badCal = rows.find((r) => {
    const c = r.state_context?.calibration;
    return !c || c.n < MIN_SAMPLES || c.yes / c.n < MIN_RATE || c.yes / c.n > MAX_RATE || c.window_s !== ROUND_WINDOW_S;
  });
  check(
    `every row carries the measurement that justified it: n >= ${MIN_SAMPLES}, rate in [${MIN_RATE}, ${MAX_RATE}], measured over the same ${ROUND_WINDOW_S}s window it settles on`,
    badCal === undefined,
    badCal
      ? `offender: ${j(badCal.state_context.calibration)}`
      : rows.map((r) => `${r.prediction_type} ${r.state_context.calibration.yes}/${r.state_context.calibration.n} = ${((r.state_context.calibration.yes / r.state_context.calibration.n) * 100).toFixed(0)}%`).join(", "),
  );
  check(
    `every row gives a viewer at least ${ROUND_LOCK_S}s to enter, then a ${ROUND_WINDOW_S}s window`,
    rows.every((r) => Number(r.entry_s) >= ROUND_LOCK_S && Number(r.window_s) === ROUND_WINDOW_S),
    `entry windows: ${[...new Set(rows.map((r) => `${Number(r.entry_s)}s`))].join(", ")}; resolve windows: ${[...new Set(rows.map((r) => `${Number(r.window_s)}s`))].join(", ")}`,
  );
  const repeats = rows.slice(1).filter((r, i) => r.prediction_type === rows[i].prediction_type);
  check(
    "no two consecutive rounds ask the same question",
    repeats.length === 0,
    `order: ${rows.map((r) => r.prediction_type).join(" -> ")}`,
  );

  // Tie the three chosen rounds to their database rows by their (shifted) open time.
  const rowFor = (planned) => {
    const want = planned.opened_epoch + OFFSET;
    const hit = rows.find((r) => Math.abs(r.opened_epoch - want) < 0.001);
    if (!hit) throw new Error(`no database row for planned round at ${planned.opened_at}`);
    return hit;
  };
  const rowA = rowFor(A);
  const rowB = rowFor(B);
  const rowC = rowFor(C);

  // ============================================================================================
  section("[6] the evidence for each chosen window, read back out of the shifted events");
  // ============================================================================================
  for (const [label, planned, row] of [["A", A, rowA], ["B", B, rowB], ["C", C, rowC]]) {
    const hits = psqlJson(
      `select e.id, e.ts::text as ts, e.type, e.payload
         from public.events e, public.predictions p
        where p.id = ${lit(row.id)} and e.session_id = p.session_id
          and e.type = (p.telemetry_rule -> 'params' ->> 'event_type')
          and e.ts >= p.locks_at and e.ts <= p.resolves_at
          and e.payload @> (p.telemetry_rule -> 'params' -> 'payload_match')
        order by e.ts`,
    );
    const all = Number(
      psql(`select count(*) from public.events e, public.predictions p
              where p.id = ${lit(row.id)} and e.session_id = p.session_id
                and e.type = (p.telemetry_rule -> 'params' ->> 'event_type')
                and e.ts >= p.locks_at and e.ts <= p.resolves_at;`),
    );
    const expected = planned.matching_event ? 1 : 0;
    check(
      `${label} (${row.prediction_type}): ${hits.length} event(s) in its window satisfy ${j(row.telemetry_rule.params.payload_match)}, out of ${all} ${row.telemetry_rule.params.event_type} row(s) there`,
      (hits.length > 0) === Boolean(planned.matching_event) && hits.length >= expected,
      hits.length
        ? `decided by events.id=${hits[0].id} ts=${hits[0].ts} payload=${j(hits[0].payload)}`
        : `nothing matches -> settlement must answer ${row.telemetry_rule.outcome_if_false}`,
    );
  }

  // ============================================================================================
  section("[7] two wallets enter the open round through the REAL enter_prediction");
  // ============================================================================================
  const winner = privateKeyToAccount(generatePrivateKey()).address.toLowerCase();
  const loser = privateKeyToAccount(generatePrivateKey()).address.toLowerCase();
  const cWinner = privateKeyToAccount(generatePrivateKey()).address.toLowerCase();
  const cLoser = privateKeyToAccount(generatePrivateKey()).address.toLowerCase();

  const [openNow] = psqlJson(
    `select status::text as status, extract(epoch from (locks_at - now())) as lead_s from public.predictions where id = ${lit(rowA.id)}`,
  );
  check(
    "A is genuinely OPEN right now — its entry window has not closed, by Postgres's own clock",
    openNow.status === "open" && Number(openNow.lead_s) > 0,
    `status=${openNow.status}, ${Number(openNow.lead_s).toFixed(0)}s left to enter (lead was ${ENTRY_LEAD_S}s; setup consumed ${(ENTRY_LEAD_S - Number(openNow.lead_s)).toFixed(0)}s)`,
  );
  const entryYes = await admin.rpc("enter_prediction", { p_id: rowA.id, p_wallet: winner, p_outcome: rowA.telemetry_rule.outcome_if_true });
  const entryNo = await admin.rpc("enter_prediction", { p_id: rowA.id, p_wallet: loser, p_outcome: rowA.telemetry_rule.outcome_if_false });
  const [countA] = psqlJson(`select entry_count from public.predictions where id = ${lit(rowA.id)}`);
  check(
    `both entries accepted by enter_prediction: ${winner.slice(0, 10)}.. on ${rowA.telemetry_rule.outcome_if_true}, ${loser.slice(0, 10)}.. on ${rowA.telemetry_rule.outcome_if_false}`,
    entryYes.data === true && entryNo.data === true && !entryYes.error && !entryNo.error && countA.entry_count === 2,
    `returns ${j(entryYes.data)}/${j(entryNo.data)}; entry_count=${countA.entry_count} (maintained by trigger)`,
  );
  const lateEntry = await admin.rpc("enter_prediction", { p_id: rowB.id, p_wallet: winner, p_outcome: "yes" });
  check(
    "the same call on B — whose entry window closed minutes ago on the shifted clock — is REFUSED",
    lateEntry.data === false && !lateEntry.error && Number(psql(`select entry_count from public.predictions where id = ${lit(rowB.id)};`)) === 0,
    "enter_prediction returned false; B keeps zero entries. Nothing in this script moves a lock to get around that.",
  );
  // C: entries inserted directly (setup), because its window is already in the past. The insert goes
  // through the same table and the same entry_count trigger; only the open-window check is skipped.
  psql(`insert into public.prediction_entries (prediction_id, wallet, outcome) values
          (${lit(rowC.id)}, ${lit(cWinner)}, ${lit(rowC.telemetry_rule.outcome_if_false)}),
          (${lit(rowC.id)}, ${lit(cLoser)}, ${lit(rowC.telemetry_rule.outcome_if_true)});`);
  check(
    "C has two entries (inserted directly — its window closed; the entry PATH is proven on A above)",
    Number(psql(`select entry_count from public.predictions where id = ${lit(rowC.id)};`)) === 2,
  );

  // ============================================================================================
  section("[8] waiting for A's window to close, then settling exactly the way /api/cron/tick does");
  // ============================================================================================
  const [times] = psqlJson(
    `select extract(epoch from (resolves_at - now())) as wait_s, locks_at::text as locks, resolves_at::text as resolves from public.predictions where id = ${lit(rowA.id)}`,
  );
  note(`A locks at ${times.locks} and resolves at ${times.resolves}: waiting ${Number(times.wait_s).toFixed(0)}s for real time to pass over the real window`);
  let waited = 0;
  while (psql(`select (now() > resolves_at)::text from public.predictions where id = ${lit(rowA.id)};`) !== "true") {
    await sleep(5000);
    waited += 5;
    if (waited % 60 === 0) note(`  ...${waited}s`);
  }
  check("A's window has closed in real time; nothing about the row was moved to get there", true, `waited ${waited}s`);
  const [lockedState] = psqlJson(`select status::text as status, entry_count from public.predictions where id = ${lit(rowA.id)}`);
  check(
    "A is still `open` until lock_due_predictions() runs — the lock is the function's job, not the clock's",
    lockedState.status === "open",
    `status=${lockedState.status}, entry_count=${lockedState.entry_count}`,
  );

  const locked = await admin.rpc("lock_due_predictions");
  const settled = await admin.rpc("settle_due_predictions_serialized");
  check(
    `lock_due_predictions() -> ${j(locked.data)} locked, settle_due_predictions_serialized() -> ${j(settled.data)} settled`,
    !locked.error && !settled.error && Number(locked.data) >= 1 && Number(settled.data) >= 1,
    `errors: ${j(locked.error)} / ${j(settled.error)}`,
  );

  // ---- A ---------------------------------------------------------------------------------------
  const [resA] = psqlJson(
    `select status::text as status, result, correct_count, entry_count, resolution_evidence, reward_pool::text as pool
       from public.predictions where id = ${lit(rowA.id)}`,
  );
  const evId = resA.resolution_evidence?.event_id;
  const [decider] = evId ? psqlJson(`select id, ts::text as ts, type, payload, extract(epoch from ts) as epoch from public.events where id = ${lit(evId)}`) : [null];
  const fixtureTs = decider ? new Date((decider.epoch - OFFSET) * 1000).toISOString() : null;
  const inFixture = fixtureTs ? fixture.events.find((e) => Math.abs(new Date(e.ts).getTime() - new Date(fixtureTs).getTime()) < 2) : null;
  check(
    `A settled ${j(resA.result)} — decided by a real recorded event, not by this script`,
    resA.status === "settled" && resA.result === rowA.telemetry_rule.outcome_if_true && resA.correct_count === 1,
    `status=${resA.status} result=${resA.result} correct_count=${resA.correct_count}\n` +
      `evidence=${j(resA.resolution_evidence)}`,
  );
  check(
    `the deciding event is in the recording: events.id=${evId} ${decider?.type} @ ${decider?.ts} (the file's own ${inFixture?.ts})`,
    Boolean(decider) && Boolean(inFixture) && decider.type === rowA.telemetry_rule.params.event_type && inFixture.type === decider.type,
    `payload in the database: ${j(decider?.payload)}\n` +
      `payload in real_session_2026-09-20.json: ${j(inFixture?.payload)}\n` +
      `the filter it satisfies: ${j(rowA.telemetry_rule.params.payload_match)}`,
  );
  const ledgerA = psqlJson(`select wallet, amount::text as amount, asset, clamped, claim_id from public.reward_ledger where prediction_id = ${lit(rowA.id)}`);
  check(
    `exactly one credit, to the wallet that answered ${rowA.telemetry_rule.outcome_if_true}: ${ledgerA[0]?.amount ?? "nothing"} ${ledgerA[0]?.asset ?? ""}`,
    ledgerA.length === 1 && ledgerA[0].wallet === winner && ledgerA[0].clamped === false && Number(ledgerA[0].amount) === Number(resA.pool),
    `pool ${resA.pool} / ${resA.correct_count} correct -> ${ledgerA[0]?.amount}; the other wallet (${loser.slice(0, 10)}..) has ${
      psqlJson(`select count(*) as n from public.reward_ledger where wallet = ${lit(loser)}`)[0].n
    } ledger rows`,
  );

  // ---- B ---------------------------------------------------------------------------------------
  const [resB] = psqlJson(`select status::text as status, result, entry_count, resolution_evidence from public.predictions where id = ${lit(rowB.id)}`);
  check(
    "B — the round nobody entered — VOIDS as `no_entries`, with no result and no ledger row",
    resB.status === "void" && resB.result === null && resB.resolution_evidence?.void_reason === "no_entries" &&
      Number(psql(`select count(*) from public.reward_ledger where prediction_id = ${lit(rowB.id)};`)) === 0,
    `status=${resB.status} entry_count=${resB.entry_count} evidence=${j(resB.resolution_evidence)} — "if nobody enters, nobody wins", before any credit code is reachable`,
  );

  // ---- C ---------------------------------------------------------------------------------------
  const [resC] = psqlJson(`select status::text as status, result, correct_count, resolution_evidence from public.predictions where id = ${lit(rowC.id)}`);
  const ledgerC = psqlJson(`select wallet, amount::text as amount from public.reward_ledger where prediction_id = ${lit(rowC.id)}`);
  check(
    `C settled ${j(resC.result)} — absence of a matching event is a definite ${rowC.telemetry_rule.outcome_if_false}, and it credits the wallet that said so`,
    resC.status === "settled" && resC.result === rowC.telemetry_rule.outcome_if_false && resC.correct_count === 1 &&
      ledgerC.length === 1 && ledgerC[0].wallet === cWinner,
    `status=${resC.status} result=${resC.result} evidence=${j(resC.resolution_evidence)}; credited ${ledgerC[0]?.amount} to ${cWinner.slice(0, 10)}..`,
  );

  const [tally] = psqlJson(
    `select count(*) as total,
            count(*) filter (where entry_count = 0 and now() > resolves_at) as due_empty,
            count(*) filter (where entry_count = 0 and now() > resolves_at and status = 'void'
                             and resolution_evidence ->> 'void_reason' = 'no_entries') as voided_empty,
            count(*) filter (where status = 'open') as still_open,
            count(*) filter (where status = 'settled') as settled
       from public.predictions where session_id = ${lit(SESSION_ID)}`,
  );
  check(
    `every round nobody entered voided the same way: ${tally.voided_empty} of ${tally.due_empty} due-and-empty rounds`,
    Number(tally.due_empty) >= 1 && Number(tally.voided_empty) === Number(tally.due_empty) &&
      Number(tally.settled) === 2 &&
      Number(psql(`select count(*) from public.reward_ledger;`)) === 2,
    `${tally.total} rounds: ${tally.settled} settled (A and C), ${tally.voided_empty} void no_entries, ${tally.still_open} still open. ` +
      `${psql("select count(*) from public.reward_ledger;")} ledger rows in the whole database.`,
  );

  // ============================================================================================
  section("[9] paying it, on the mainnet fork, with the real worker");
  // ============================================================================================
  process.env.TREASURY_PRIVATE_KEY = generatePrivateKey();
  const treasury = privateKeyToAccount(process.env.TREASURY_PRIVATE_KEY).address.toLowerCase();
  process.env.NEXT_PUBLIC_TREASURY_ADDRESS = treasury;
  const erc20 = [
    { type: "function", name: "balanceOf", stateMutability: "view", inputs: [{ name: "a", type: "address" }], outputs: [{ type: "uint256" }] },
    { type: "function", name: "transfer", stateMutability: "nonpayable", inputs: [{ name: "to", type: "address" }, { name: "amount", type: "uint256" }], outputs: [{ type: "bool" }] },
    { type: "function", name: "paused", stateMutability: "view", inputs: [], outputs: [{ type: "bool" }] },
    { type: "function", name: "decimals", stateMutability: "view", inputs: [], outputs: [{ type: "uint8" }] },
  ];
  const call = async (fn, args = []) => anvil("eth_call", [{ to: TTWO, data: encodeFunctionData({ abi: erc20, functionName: fn, args }) }, "latest"]);
  const balanceOf = async (a) => BigInt(await call("balanceOf", [a]));
  const waitReceipt = async (h, ms = 15_000) => {
    const end = Date.now() + ms;
    for (;;) {
      const rc = await anvil("eth_getTransactionReceipt", [h]);
      if (rc || Date.now() > end) return rc;
      await sleep(200);
    }
  };
  check(
    "the fork IS chain 4663 with the real TTWO: decimals 18, paused() false, the holder's real balance is there",
    Number(BigInt(await anvil("eth_chainId"))) === CHAIN_ID && Number(BigInt(await call("decimals"))) === 18 &&
      BigInt(await call("paused")) === 0n && (await balanceOf(TTWO_HOLDER)) > FUND_TTWO,
    `eth_chainId=${Number(BigInt(await anvil("eth_chainId")))} balanceOf(${TTWO_HOLDER.slice(0, 10)}..)=${await balanceOf(TTWO_HOLDER)}`,
  );
  await anvil("anvil_setBalance", [treasury, hex(FUND_ETH)]);
  await anvil("anvil_setBalance", [TTWO_HOLDER, hex(FUND_ETH)]);
  await anvil("anvil_impersonateAccount", [TTWO_HOLDER]);
  const fundHash = await anvil("eth_sendTransaction", [
    { from: TTWO_HOLDER, to: TTWO, data: encodeFunctionData({ abi: erc20, functionName: "transfer", args: [treasury, FUND_TTWO] }) },
  ]);
  await anvil("anvil_stopImpersonatingAccount", [TTWO_HOLDER]);
  const fundRc = await waitReceipt(fundHash);
  check(
    "throwaway treasury funded ON THE FORK by a real TTWO transfer from a real holder",
    fundRc?.status === "0x1" && (await balanceOf(treasury)) === FUND_TTWO,
    `treasury ${treasury}: ${await balanceOf(treasury)} TTWO base units; funding tx ${fundHash}`,
  );

  const limits = rewardLimits();
  const winnerBefore = await balanceOf(winner);
  const claim = await admin
    .rpc("create_reward_claim", {
      p_wallet: winner,
      p_asset: asset.symbol,
      p_min_amount: fromBaseUnits(limits.minClaim, asset.decimals),
      p_max_amount: fromBaseUnits(limits.maxClaim, asset.decimals),
    })
    .single();
  check(
    "create_reward_claim (what POST /api/rewards/claim calls) enqueues the credit",
    !claim.error && claim.data?.status === "queued" && claim.data?.amount_text === ledgerA[0].amount,
    claim.error ? `error=${j(claim.error)}` : `claim ${claim.data.id} amount_text="${claim.data.amount_text}" (rails ${limits.minClaim}..${limits.maxClaim} base units)`,
  );
  if (claim.error) throw new Error("claim could not be enqueued");
  const summary = await worker.runPayoutWorker(admin, { budgetMs: 40_000 });
  note(`runPayoutWorker: reason=${summary.reason} paid=${summary.paid} failed=${summary.failed} in_flight=${summary.in_flight} halted=${summary.halted} next_nonce=${summary.next_nonce} chain_nonce=${summary.chain_nonce}`);
  const [paid] = psqlJson(
    `select status, tx_hash, raw_tx, receipt_status, block_number::text as block_number, amount::text as amount,
            amount_base::text as amount_base, signer_address, to_address, chain_id, attempts, error
       from public.reward_claims where id = ${lit(claim.data.id)}`,
  );
  const receipt = paid.tx_hash ? await anvil("eth_getTransactionReceipt", [paid.tx_hash]) : null;
  const winnerAfter = await balanceOf(winner);
  check(
    `a real transaction was signed by the real worker and mined on the fork: ${paid.tx_hash}`,
    paid.status === "confirmed" && paid.receipt_status === 1 && receipt?.status === "0x1" &&
      receipt.transactionHash === paid.tx_hash && keccak256(paid.raw_tx) === paid.tx_hash &&
      paid.signer_address === treasury && paid.to_address === winner && paid.chain_id === CHAIN_ID,
    `status=${paid.status} receipt_status=${paid.receipt_status} block=${paid.block_number} attempts=${paid.attempts} error=${paid.error}\n` +
      `keccak256(raw_tx) == tx_hash == receipt.transactionHash; signer ${paid.signer_address}; recipient ${paid.to_address}; chain ${paid.chain_id}`,
  );
  check(
    `the winner's on-chain TTWO balance rose by exactly the credited amount (${paid.amount} ${asset.symbol})`,
    winnerAfter - winnerBefore === BigInt(paid.amount_base) && toBaseUnits(paid.amount, asset.decimals) === BigInt(paid.amount_base) &&
      BigInt(paid.amount_base) === toBaseUnits(ledgerA[0].amount, asset.decimals),
    `balanceOf(${winner.slice(0, 10)}..): ${winnerBefore} -> ${winnerAfter} (delta ${winnerAfter - winnerBefore} base units); ledger credit ${ledgerA[0].amount}`,
  );
  check(
    "the ledger row is attached to the confirmed claim, so it can never be claimed twice",
    Number(psql(`select count(*) from public.reward_ledger where prediction_id = ${lit(rowA.id)} and claim_id = ${lit(claim.data.id)};`)) === 1 &&
      Number(psql(`select count(*) from public.reward_ledger where wallet = ${lit(winner)} and claim_id is null;`)) === 0,
    `claimable balance for the winner is now ${psql(`select coalesce(sum(amount), 0)::text from public.reward_ledger where wallet = ${lit(winner)} and claim_id is null;`)}`,
  );

  // ============================================================================================
  section("=== SUMMARY: one scheduled round, from the generator to the chain ===");
  // ============================================================================================
  console.log(`  recording           : real_session_2026-09-20.json — ${fixture.events.length} real events, ${((plan.t1 - plan.t0) / 3600).toFixed(2)} h, session ${SESSION_ID}`);
  console.log(`  time shift          : +${OFFSET.toFixed(3)} s applied to every event (one constant; nothing else changed)`);
  console.log(`  rounds generated    : ${rows.length}, by the real PredictionGenerator + CATALOG + RollingBaseRate, written through the real writer`);
  for (const r of rows) {
    const c = r.state_context.calibration;
    console.log(`      ${r.prediction_type.padEnd(18)} ${String(c.yes).padStart(2)}/${String(c.n).padStart(2)} = ${((c.yes / c.n) * 100).toFixed(0).padStart(3)}%  pool ${r.reward_pool} ${r.reward_asset}  ${r.id === rowA.id ? "<- A" : r.id === rowB.id ? "<- B" : r.id === rowC.id ? "<- C" : ""}`);
  }
  console.log(`  A  ${rowA.prediction_type}: settled ${resA.result} on events.id=${evId} (${decider?.type} ${j(decider?.payload)}) at ${decider?.ts}`);
  console.log(`  B  ${rowB.prediction_type}: void ${resB.resolution_evidence?.void_reason} — 0 entries, 0 ledger rows`);
  console.log(`  C  ${rowC.prediction_type}: settled ${resC.result} — no matching event in the window`);
  console.log(`  credited            : ${ledgerA[0].amount} ${ledgerA[0].asset} to ${winner}`);
  console.log(`  tx                  : ${paid.tx_hash} (block ${paid.block_number}, receipt_status ${paid.receipt_status})`);
  console.log(`  balance             : ${winnerBefore} -> ${winnerAfter} base units (delta ${winnerAfter - winnerBefore} = ${paid.amount} ${asset.symbol})`);
}

let exitCode = 0;
try {
  await main();
} catch (err) {
  fail += 1;
  console.error(`\nFATAL: ${err instanceof Error ? `${err.name}: ${err.message}\n${err.stack}` : String(err)}`);
} finally {
  if (outRoot) rmSync(outRoot, { recursive: true, force: true });
  if (stackUp) {
    try {
      console.log(`\n${execFileSync("bash", [join(STACK_DIR, "down.sh")], { env: childEnv(), encoding: "utf8" }).trim()}`);
    } catch (err) {
      console.error(`teardown failed: ${String(err)}`);
    }
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  for (const f of failures) console.log(`  FAILED: ${f}`);
  exitCode = fail === 0 ? 0 : 1;
}
process.exit(exitCode);
