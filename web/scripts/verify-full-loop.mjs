#!/usr/bin/env node
// Drives the ENTIRE prediction loop end-to-end against a running app, over real HTTP, with a real
// secp256k1 wallet signature and real recorded gameplay telemetry.
//
// This is the closest thing to the operator's §37 acceptance list that can be executed without the
// game running. It covers steps 3-18 of that list: prediction creation, wallet connect, outcome
// selection, server-side storage, locking at the right time, deterministic resolution from
// telemetry, ledger credit for the correct wallet and none for the incorrect one, claimable balance,
// the claim flow, leaderboard, and streaks.
//
// WHAT IS REAL HERE, and what is not:
//   * Real Postgres 16 with the real migrations, real PostgREST enforcing real RLS over HTTP, the
//     real Next.js server, the real API route handlers, a real secp256k1 keypair and signature.
//   * The telemetry that decides the outcome is REAL RECORDED GAMEPLAY — 5,649 events captured from
//     an actual session. Settlement is asserted against a death that genuinely happened at
//     2026-09-04T08:42:33.680272Z.
//   * The only harness action is moving a prediction's window onto that recorded death AFTER the
//     entry has been accepted. Entry and the lock are therefore exercised against a genuinely open
//     window through the real API, and settlement against genuinely real telemetry. No event is
//     invented, and no API response is stubbed.
//   * NOT covered here: the game being live (it is not), and paying a claim on chain — the payout
//     worker is the only signer and is proven separately against a mainnet fork by
//     scripts/verify-payout.mjs. Neither is simulated here.
//
// Usage: node scripts/verify-full-loop.mjs
//   env: LOOP_BASE (default http://127.0.0.1:4500), LOOP_PG (docker container name of the database)

import { execFileSync } from "node:child_process";
import { privateKeyToAccount, generatePrivateKey } from "viem/accounts";

const BASE = process.env.LOOP_BASE || "http://127.0.0.1:4500";
const PG = process.env.LOOP_PG || "wanted-e2e-pg";

// Required, not defaulted: a secret-shaped literal does not belong in a committed file, even a
// throwaway one for a local harness. It must match the CRON_SECRET the server under test was
// started with, or the tick assertions would fail for the wrong reason.
const CRON_SECRET = process.env.CRON_SECRET;
if (!CRON_SECRET) {
  console.error("CRON_SECRET must be set, and must match the server under test.");
  process.exit(2);
}

// A real recorded death from the fixture. Settlement must land on this exact event.
const REAL_DEATH = "2026-09-04T08:42:33.680272+00:00";
const WINDOW_START = "2026-09-04T08:41:00+00:00";
const WINDOW_END = "2026-09-04T08:45:00+00:00";

let pass = 0;
let fail = 0;
const check = (name, ok, detail = "") => {
  if (ok) { pass += 1; console.log(`  PASS  ${name}${detail ? `  ${detail}` : ""}`); }
  else { fail += 1; console.log(`  FAIL  ${name}${detail ? `  ${detail}` : ""}`); }
};

function sql(statement) {
  return execFileSync(
    "docker",
    ["exec", "-i", PG, "psql", "-U", "postgres", "-tAq", "-v", "ON_ERROR_STOP=1", "-c", statement],
    { encoding: "utf8" },
  ).trim();
}

async function api(path, opts = {}) {
  const res = await fetch(`${BASE}${path}`, opts);
  const text = await res.text();
  let body;
  try { body = JSON.parse(text); } catch { body = text; }
  return { status: res.status, body, headers: res.headers };
}

console.log("\n=========== WANTED — full prediction loop ===========\n");

// The rewards master switch (migration 20260909010000) defaults to OFF, so the settlement
// assertions below would correctly find an empty ledger without this. It is turned on here rather
// than defaulted on in the schema because an absent config must never mean "pay out" — the switch
// exists precisely because REWARDS_ENABLED, a Vercel env var, could never reach the Postgres
// function that writes the credit. infra/verify-predictions.sql §10 asserts the off case.
// rewards_enabled() also requires real caps (20260914000001) — generous ones, so the assertions
// below test the reward formula and not the clamps.
sql(`insert into public.site_config (key, value) values
       ('rewards', '{"enabled": true}'::jsonb),
       ('reward_caps', '{"daily_cap": 100000, "max_per_prediction": 100000, "max_per_wallet_day": 100000}'::jsonb)
     on conflict (key) do update set value = excluded.value;`);
check("rewards master switch is on for this run", sql("select public.rewards_enabled();") === "t");

// ---------------------------------------------------------------------------------------------
console.log("--- 0. The agent's session, and its real telemetry ---");
const sessionId = sql("select id from public.sessions order by started_at desc limit 1;");
const eventCount = Number(sql("select count(*) from public.events;"));
check("a real recorded session is loaded", Boolean(sessionId), sessionId);
check("its real telemetry is present", eventCount === 5649, `${eventCount} events`);
check(
  "the death the outcome will hinge on is genuinely in the recording",
  Number(sql(`select count(*) from public.events where type='death' and ts='${REAL_DEATH}';`)) === 1,
  REAL_DEATH,
);

// Make the session look live, as it would be with the game running, so /api/predictions/live
// reports session_live truthfully rather than refusing to show anything.
sql(`insert into public.stats (session_id, heartbeat_at) values ('${sessionId}', now())
     on conflict (session_id) do update set heartbeat_at = now();`);
const live0 = await api("/api/predictions/live");
check("session_live is true once the heartbeat is fresh", live0.body.session_live === true);

// Clock skew: the heartbeat is written by the game server, and this check runs elsewhere, so a
// heartbeat stamped slightly in the reader's future is ordinary NTP drift rather than a fault.
// Requiring a non-negative age made the site report OFF AIR while the agent was playing.
sql(`update public.stats set heartbeat_at = now() + interval '30 seconds' where session_id='${sessionId}';`);
const skewed = await api("/api/predictions/live");
check("a heartbeat slightly in the future still reads as live (clock skew)",
  skewed.body.session_live === true);
sql(`update public.stats set heartbeat_at = now() + interval '2 hours' where session_id='${sessionId}';`);
const wayOff = await api("/api/predictions/live");
check("a heartbeat far in the future is NOT trusted", wayOff.body.session_live === false);
sql(`update public.stats set heartbeat_at = now() where session_id='${sessionId}';`);

// ---------------------------------------------------------------------------------------------
console.log("\n--- 1. a prediction is created from that session ---");
const predId = sql(`
  insert into public.predictions
    (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
     outcomes, telemetry_rule, reward_pool, reward_asset)
  values ('${sessionId}', 'WILL WANTED SURVIVE THE NEXT 4 MINUTES?', 'survives',
     now() - interval '5 seconds', now() + interval '1 hour', now() + interval '2 hours',
     '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
     '{"kind":"survives_window","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb,
     10, 'TTWO')
  returning id;`);
check("prediction row created", Boolean(predId), predId);

const live1 = await api("/api/predictions/live");
check(
  "it appears on /api/predictions/live",
  Array.isArray(live1.body.live) && live1.body.live.some((p) => p.id === predId),
  `${live1.body.live?.length ?? 0} live`,
);
// The pool is 10 whole TTWO in the database. The contract declares reward_pool as a BASE-UNIT
// string, because that is what the UI formats against. Passing the raw column through rendered a
// real 10 TTWO pool as "Pool: 0 TTWO" on the live card — a wrong number in front of viewers
// deciding whether to answer.
const servedPool = live1.body.live?.find((p) => p.id === predId)?.reward_pool;
check("reward_pool is served in base units, not raw whole units",
  servedPool === "10000000000000000000", String(servedPool));

// ---------------------------------------------------------------------------------------------
console.log("\n--- 2. a viewer connects a wallet and signs in (real signature) ---");
const winner = privateKeyToAccount(generatePrivateKey());
const loser = privateKeyToAccount(generatePrivateKey());

async function signIn(account) {
  const nonceRes = await api("/api/auth/nonce", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ address: account.address }),
  });
  if (nonceRes.status !== 201 && nonceRes.status !== 200) {
    return { ok: false, detail: `nonce ${nonceRes.status} ${JSON.stringify(nonceRes.body)}` };
  }
  const { message } = nonceRes.body;
  if (!message) return { ok: false, detail: "no message returned to sign" };
  // Sign the SERVER'S message verbatim — the server pins the domain and rebuilds this text itself.
  const signature = await account.signMessage({ message });
  const verifyRes = await api("/api/auth/verify", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ address: account.address, signature }),
  });
  const cookie = verifyRes.headers.get("set-cookie");
  return {
    ok: verifyRes.status === 200,
    cookie: cookie ? cookie.split(";")[0] : null,
    message,
    detail: `${verifyRes.status} ${JSON.stringify(verifyRes.body)}`,
  };
}

const winnerAuth = await signIn(winner);
check("nonce issued and signature verified", winnerAuth.ok, winnerAuth.detail);
check("a session cookie was set", Boolean(winnerAuth.cookie));
check(
  "the signed message names this deployment's own origin, not a caller-supplied host",
  typeof winnerAuth.message === "string" && winnerAuth.message.includes("127.0.0.1:4500"),
);

// Replay the same signature: the nonce is single-use, so it must be refused.
if (winnerAuth.message) {
  const replay = await api("/api/auth/verify", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ address: winner.address, signature: await winner.signMessage({ message: winnerAuth.message }) }),
  });
  check("replaying a used nonce is refused", replay.status === 401 || replay.status === 409, `status ${replay.status}`);
} else {
  check("replaying a used nonce is refused", false, "skipped — sign-in did not succeed");
}

const loserAuth = await signIn(loser);
check("a second wallet can sign in independently", loserAuth.ok, loserAuth.detail);

// ---------------------------------------------------------------------------------------------
console.log("\n--- 3. entries, and the server-side lock ---");
async function enter(auth, outcome) {
  return api(`/api/predictions/${predId}/enter`, {
    method: "POST",
    headers: { "content-type": "application/json", cookie: auth.cookie },
    body: JSON.stringify({ outcome }),
  });
}

const e1 = await enter(winnerAuth, "no");   // correct: he really did die in that window
const e2 = await enter(loserAuth, "yes");   // incorrect
check("winner's entry accepted", e1.status === 200 || e1.status === 201, `status ${e1.status}`);
check("loser's entry accepted", e2.status === 200 || e2.status === 201, `status ${e2.status}`);

const dup = await enter(winnerAuth, "yes");
check("the same wallet cannot enter twice", dup.status === 409, `status ${dup.status}`);

const anon = await api(`/api/predictions/${predId}/enter`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ outcome: "yes" }),
});
check("an unauthenticated entry is refused", anon.status === 401, `status ${anon.status}`);

check("entry_count reflects exactly the two real entries",
  sql(`select entry_count from public.predictions where id='${predId}';`) === "2");

// ---------------------------------------------------------------------------------------------
console.log("\n--- 4. the window closes over REAL recorded gameplay ---");
// Harness step, and the only one: move the window onto the recorded death so that settlement is
// decided by telemetry that actually happened. Entry and the lock were already exercised above
// against a genuinely open window.
sql(`update public.predictions
       set locks_at='${WINDOW_START}', resolves_at='${WINDOW_END}',
           opened_at='${WINDOW_START}'::timestamptz - interval '1 minute'
     where id='${predId}';`);

const late = await enter(winnerAuth, "yes");
check("no entry is possible once locks_at has passed", late.status === 409, `status ${late.status}`);

// ---------------------------------------------------------------------------------------------
console.log("\n--- 5. the cron tick locks and settles ---");
const unauth = await api("/api/cron/tick", { method: "POST" });
check("the tick refuses an unauthenticated caller", unauth.status === 401, `status ${unauth.status}`);

const tick = await api("/api/cron/tick", {
  method: "POST",
  headers: { authorization: `Bearer ${CRON_SECRET}` },
});
check("the authenticated tick runs", tick.status === 200, JSON.stringify(tick.body));

const settled = sql(`select status || '|' || coalesce(result,'-') from public.predictions where id='${predId}';`);
check("the prediction settled", settled.startsWith("settled"), settled);
check("it settled NO — he really did die in that window", settled.endsWith("|no"), settled);

const evidence = sql(`select resolution_evidence->>'ts' from public.predictions where id='${predId}';`);
check("the evidence cites the real recorded death", evidence === REAL_DEATH, evidence);

// ---------------------------------------------------------------------------------------------
console.log("\n--- 6. rewards: the correct wallet is credited, the incorrect one is not ---");
const winnerRows = Number(sql(`select count(*) from public.reward_ledger where wallet='${winner.address.toLowerCase()}';`));
const loserRows = Number(sql(`select count(*) from public.reward_ledger where wallet='${loser.address.toLowerCase()}';`));
check("the correct wallet has exactly one ledger credit", winnerRows === 1, `${winnerRows} rows`);
check("the incorrect wallet has none", loserRows === 0, `${loserRows} rows`);

check(
  "every stored wallet is lower-cased, so UNIQUE(prediction_id, wallet) binds per ADDRESS",
  sql("select coalesce(bool_and(wallet = lower(wallet)), true) from public.prediction_entries;") === "t" &&
  sql("select coalesce(bool_and(wallet = lower(wallet)), true) from public.reward_ledger;") === "t",
);
const amount = sql(`select amount from public.reward_ledger where wallet='${winner.address.toLowerCase()}';`);
check("the whole 10 TTWO pool went to the sole correct wallet", amount === "10.000000000000000000", amount);

// Settlement must be safely re-runnable.
await api("/api/cron/tick", { method: "POST", headers: { authorization: `Bearer ${CRON_SECRET}` } });
await api("/api/cron/tick", { method: "POST", headers: { authorization: `Bearer ${CRON_SECRET}` } });
check("re-running settlement does not double-credit",
  Number(sql(`select count(*) from public.reward_ledger where wallet='${winner.address.toLowerCase()}';`)) === 1);

// ---------------------------------------------------------------------------------------------
console.log("\n--- 7. claimable balance ---");
const bal = await api("/api/rewards/balance", { headers: { cookie: winnerAuth.cookie } });
check("the winner's balance is readable", bal.status === 200, JSON.stringify(bal.body).slice(0, 160));
check("claimable is 10 TTWO in base units",
  bal.body?.claimable === "10000000000000000000", String(bal.body?.claimable));
check("the asset is reported as TTWO", bal.body?.asset === "TTWO", String(bal.body?.asset));

const loserBal = await api("/api/rewards/balance", { headers: { cookie: loserAuth.cookie } });
check("the incorrect wallet has nothing claimable", loserBal.body?.claimable === "0", String(loserBal.body?.claimable));

const noAuthBal = await api("/api/rewards/balance");
check("balance requires authentication", noAuthBal.status === 401, `status ${noAuthBal.status}`);

// ---------------------------------------------------------------------------------------------
console.log("\n--- 8. the claim (the outbox: enqueue only — CONTRACTS-PREDICTIONS §10) ---");
// The claim route no longer signs anything. It enqueues a `queued` claim and returns 202; the payout
// worker, run by the cron under a lease, is the only signer — and it is proven against a fork of
// Robinhood Chain mainnet by scripts/verify-payout.mjs. What this loop proves is the viewer's half,
// through the real routes: the payouts switch, the enqueue, the credit moving onto the claim, the
// one-in-flight rule, and the balance and history the panel renders from.
//
// The server under test must be started with CLAIM_MIN_AMOUNT and CLAIM_MAX_AMOUNT set (they are
// required now; unset refuses every claim) and REWARDS_ENABLED=true. It needs no treasury key.
const winnerWallet = winner.address.toLowerCase();
sql(`insert into public.site_config (key, value) values ('rewards', '{"enabled": true, "payouts": false}'::jsonb)
     on conflict (key) do update set value = excluded.value;`);
const paused = await api("/api/rewards/claim", { method: "POST", headers: { cookie: winnerAuth.cookie } });
check("with payouts OFF the claim is refused as paused (503) and nothing is enqueued",
  paused.status === 503 && sql("select count(*) from public.reward_claims;") === "0",
  `status ${paused.status} ${JSON.stringify(paused.body).slice(0, 120)}`);

sql(`update public.site_config set value = value || '{"payouts": true}'::jsonb where key = 'rewards';`);
const claim = await api("/api/rewards/claim", { method: "POST", headers: { cookie: winnerAuth.cookie } });
console.log(`       claim -> ${claim.status} ${JSON.stringify(claim.body).slice(0, 220)}`);
check("the claim is QUEUED (202) — accepted, never reported as paid",
  claim.status === 202 && claim.body?.status === "queued" && typeof claim.body?.claimId === "string",
  `status ${claim.status}`);
check("the response carries no transaction hash (nothing was signed)",
  !/0x[a-f0-9]{64}/i.test(JSON.stringify(claim.body)));
check("the queued claim owns the winner's credit, in the same transaction that created it",
  sql(`select count(*) from public.reward_ledger where wallet='${winnerWallet}' and claim_id is not null;`) === "1" &&
  sql(`select status from public.reward_claims where wallet='${winnerWallet}';`) === "queued");
check("no request signed anything: no raw_tx, no nonce anywhere",
  sql("select count(*) from public.reward_claims where raw_tx is not null or nonce is not null;") === "0");

const again = await api("/api/rewards/claim", { method: "POST", headers: { cookie: winnerAuth.cookie } });
check("a second claim while one is in flight is refused (409), not double-queued",
  again.status === 409 && sql(`select count(*) from public.reward_claims where wallet='${winnerWallet}';`) === "1",
  `status ${again.status}`);

const balAfter = await api("/api/rewards/balance", { headers: { cookie: winnerAuth.cookie } });
check("the balance shows nothing claimable and names the in-flight claim",
  balAfter.body?.claimable === "0" && balAfter.body?.in_flight_claim?.status === "queued",
  JSON.stringify(balAfter.body).slice(0, 200));
const history = await api("/api/rewards/claims", { headers: { cookie: winnerAuth.cookie } });
const latest = history.body?.claims?.[0];
check("the claim history shows it queued, 10 TTWO in base units, with no receipt yet",
  history.status === 200 && latest?.status === "queued" && latest?.amount === "10000000000000000000" &&
    latest?.tx_hash === null && latest?.explorer_url === null,
  JSON.stringify(latest));
check("the claim history is private to the session",
  (await api("/api/rewards/claims")).status === 401);

console.log("\n--- 9. leaderboard and streaks ---");
const lb = await api("/api/leaderboard?window=all");
const rows = lb.body?.rows || [];
const winnerRow = rows.find((r) => r.wallet?.toLowerCase() === winner.address.toLowerCase());
const loserRow = rows.find((r) => r.wallet?.toLowerCase() === loser.address.toLowerCase());
check("the leaderboard lists both wallets", rows.length === 2, `${rows.length} rows`);
check("the correct wallet shows 1/1 accuracy", winnerRow && winnerRow.correct === 1 && winnerRow.total === 1,
  JSON.stringify(winnerRow));
// `earned` must be a base-unit STRING: a JS number cannot hold 1e18 without loss, so a large
// earner would be shown a rounded total on a public ranking.
check("earned is a base-unit string, not a lossy number",
  typeof winnerRow?.earned === "string" && winnerRow.earned === "10000000000000000000",
  `${typeof winnerRow?.earned} ${winnerRow?.earned}`);
check("the incorrect wallet shows 0/1", loserRow && loserRow.correct === 0 && loserRow.total === 1,
  JSON.stringify(loserRow));
check("the correct wallet's streak is 1", winnerRow && winnerRow.current_streak === 1, String(winnerRow?.current_streak));
check("the incorrect wallet's streak is 0", loserRow && loserRow.current_streak === 0, String(loserRow?.current_streak));

// ---------------------------------------------------------------------------------------------
console.log("\n--- 10. the next prediction can appear ---");
const nextId = sql(`
  insert into public.predictions
    (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
     outcomes, telemetry_rule, reward_pool, reward_asset)
  values ('${sessionId}', 'WILL WANTED LOSE THE COPS?', 'wanted_clear',
     now() - interval '5 seconds', now() + interval '2 minutes', now() + interval '5 minutes',
     '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
     '{"kind":"wanted_clears","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb, 10, 'TTWO')
  returning id;`);
const live2 = await api("/api/predictions/live");
check("a new prediction is served as live", live2.body.live?.some((p) => p.id === nextId));
// The settled one resolved on 2026-09-04, which is outside the route's "recently settled" window.
// Excluding it is correct — the page shows recent results, not the whole archive — so assert that,
// and separately prove a freshly-resolved prediction IS surfaced.
check("a long-past settled prediction is NOT in the recent-results list",
  !live2.body.resolved?.some((p) => p.id === predId));
const recentId = sql(`
  insert into public.predictions
    (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
     outcomes, telemetry_rule, reward_pool, reward_asset, status, result, settled_at)
  values ('${sessionId}', 'DID WANTED SURVIVE?', 'survives',
     now() - interval '10 minutes', now() - interval '9 minutes', now() - interval '1 minute',
     '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
     '{"kind":"survives_window","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb,
     10, 'TTWO', 'settled', 'yes', now())
  returning id;`);
const live3 = await api("/api/predictions/live");
check("a just-resolved prediction IS surfaced as a recent result",
  live3.body.resolved?.some((p) => p.id === recentId));

console.log(`\n=========== ${pass} passed, ${fail} failed ===========\n`);
process.exit(fail === 0 ? 0 : 1);
