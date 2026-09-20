# WANTED — STATUS

Single source of truth. Nothing appears in "Works / verified" without evidence (command output,
run log, or URL) noted next to it. Last updated: 2026-09-20.

## 2026-09-08 — WANTED prediction layer (public product name: WANTED; $WANTED / TTWO)

A prediction layer was built on top of the existing agent, which is unchanged: `bridge/` and the
harness's perception/brain/behaviour stack were not modified. Viewers predict what the agent will
do next; predictions settle from the same telemetry the agent already writes.

**Verified — real Postgres 16, real recorded gameplay.** `infra/verify-predictions.sh` boots a
throwaway `postgres:16-alpine`, applies all five migrations, replays **5,649 events captured from
the real session `2c135a5f`** (`harness/tests/fixtures/real_session_2026-09-04.json`, pulled from
the production project, provenance stamped) and asserts settlement against outcomes that actually
happened:

- `survives_window` over `06:20:00Z–06:23:00Z` (a genuinely death-free stretch) → `yes`.
- `survives_window` over `08:41:00Z–08:45:00Z` → `no`, citing the **real death at
  `2026-09-04T08:42:33.680272Z`**; the assertion checks that timestamp is one of the two deaths the
  recording actually contains.
- `wanted_clears` over `01:03:00Z–01:06:00Z` → `yes`, citing the real `wanted_change {from:2,to:0}`
  at `01:04:37.719774Z` (event id 23).
- Rewards: pool 10 ÷ 2 correct = `5.000000000000000000` each; a lone correct wallet takes `10`.
- **Idempotency:** `settle_due_predictions()` run three times → 5 ledger rows, unchanged, and
  `max(rows per (prediction, wallet)) = 1`.
- **Lock:** `enter_prediction` returns false after `locks_at`, decided by Postgres `now()`. Proven
  on a live-window prediction: first entry `t`, duplicate `f`, unknown outcome key `f`, and `f`
  again once the lock is moved into the past.
- **Voids:** `no_entries` and `unknown_rule_kind` (the latter with a real entry present, so the
  rule dispatch is genuinely reached rather than short-circuited).
- **Claims:** atomic via `create_reward_claim`; a second in-flight claim hits
  `reward_claims_one_inflight_per_wallet` (**23505**) — the actual double-spend guard; below-minimum
  → `P0003`; a failed claim returns its credits to the claimable pool; a confirmed claim cannot be
  reopened.
- **RLS as `anon`:** `predictions` and `prediction_distribution` readable; `permission denied` on
  `reward_ledger`, `reward_claims`, `wallet_sessions`, and on `enter_prediction`,
  `create_reward_claim`, `settle_due_predictions`.

**Verified — the whole loop, end to end, over real HTTP.** `web/scripts/verify-full-loop.mjs`
(`npm run verify:loop`) drives the operator's §37 acceptance list, steps 3-18, against a full local
stack: real Postgres 16 with the real migrations, real PostgREST enforcing real RLS over HTTP, the
real Next.js production build, the real route handlers, real secp256k1 keypairs, and the real
recorded gameplay. **50 assertions, 0 failures.** It proves, in one run:

- a prediction is created from a live session and served by `/api/predictions/live`;
- a wallet signs in with a real signature over the server's own message, and a replayed nonce is
  refused;
- two wallets enter opposite outcomes; a duplicate entry is a 409; an unauthenticated entry is a
  401; `entry_count` reflects only the real entries;
- once `locks_at` passes, no entry is possible;
- the cron tick refuses an unauthenticated caller, then locks and settles;
- settlement lands on `no` and cites the **real recorded death at 2026-09-04T08:42:33.680272Z**;
- the correct wallet gets exactly one ledger credit of the whole 10 TTWO pool, the incorrect wallet
  gets none, and re-running settlement does not double-credit;
- claimable balance reads `10000000000000000000` base units as TTWO, and requires authentication;
- **the claim runs all the way to a real broadcast attempt against Robinhood Chain**, fails on gas
  with the chain's own error, is recorded `failed`, and **returns the credit to claimable** — a
  failed transfer never burns a reward;
- the leaderboard shows 1/1 and 0/1 with correct streaks;
- a new prediction appears, and the recent-results window behaves correctly at both ends.

**Six bugs were found by that run and fixed.** Every one was invisible to unit tests, because every
one lived in a seam between two individually-correct halves:

1. **Nobody could have signed in.** `/nonce` signed `Issued At: …595Z`; `/verify` rebuilt the message
   from what Postgres returned, `…595+00:00`. Same instant, different bytes, so every signature
   recovered a different address. `buildSiweMessage()` now canonicalises its inputs, which is the one
   place both halves share.
2. **`/api/predictions/live` was dead.** `prediction_distribution` was granted to `anon` and
   `authenticated` but not to `service_role` — the role the server actually uses. An `anon` smoke
   test passed while the real route returned "permission denied for view". A full grant audit across
   every new object confirmed this was the only omission.
3. **Rewards were permanently unclaimable.** Wallets were stored EIP-55 checksummed but
   `create_reward_claim` looked up `lower(wallet)`, so it matched nothing and raised "nothing to
   claim" for every wallet forever. Storage is now lower-cased and enforced by CHECK constraints on
   every wallet column, so it cannot drift back.

4. **The reward pool rendered as zero.** `/api/predictions/live` passed `reward_pool` straight
   through from the database's whole-unit `numeric`, while the contract and the UI treat it as base
   units — so a real 12 TTWO pool displayed as "Pool: 0 TTWO" on the live card, in front of viewers
   deciding whether an answer was worth giving.
5. **The leaderboard's `earned` was a lossy number.** It came back as a JSON number in whole units
   rather than a base-unit string. A JS number cannot hold 1e18, so a large earner would have been
   shown a rounded total on a public ranking.
6. **The site would have claimed OFF AIR while the agent was playing.** `session_live` required
   `age >= 0` against `stats.heartbeat_at`, so a heartbeat stamped even milliseconds in the reader's
   future read as offline. The harness writes that timestamp on the game server and the check runs
   elsewhere, so a second of NTP drift is routine — reproduced here with a container clock only
   0.14 s ahead of the host. It now tolerates two minutes of forward skew and still refuses a
   heartbeat far enough ahead to mean a broken clock.

**A config audit found three more traps, all now closed.** Comparing every `process.env` read in
the code against `.env.example`:

- **A treasury cap that silently did nothing.** `REWARDS_DAILY_CAP`, `REWARDS_MAX_PER_PREDICTION`
  and `REWARDS_MAX_PER_WALLET_DAY` were parsed from the environment into `REWARD_LIMITS`, and
  nothing read them — settlement enforces those caps from `site_config`, because it is a Postgres
  function and cannot see the deployment's environment. An operator who set a daily spending cap
  would have been told nothing and capped nothing. The dead half is deleted; there is now exactly
  one place to set each rail, and CONTRACTS-PREDICTIONS §6 says which and why.
- **RPC and explorer overrides that could not take effect.** The code read
  `NEXT_PUBLIC_CHAIN_RPC_URL` / `NEXT_PUBLIC_CHAIN_EXPLORER_URL` while the brief and
  `.env.example` documented `NEXT_PUBLIC_RPC_URL` / `NEXT_PUBLIC_EXPLORER_URL`. Pointing a deploy at
  a paid RPC or at testnet would have quietly kept using the default mainnet endpoint. Both
  spellings are now accepted.
- **A latent client/server split.** `NEXT_PUBLIC_TTWO_TOKEN` and `NEXT_PUBLIC_WANTED_TOKEN` were
  read through a dynamic `process.env[name]` lookup, which Next.js does not inline into the browser
  bundle. Nothing calls those from a client component today, so nothing was broken — but the first
  one that did would have found a reward asset that exists on the server and vanishes in the
  browser. Now read by static member access.
- `SUPABASE_URL` — required by every API route that touches the prediction schema — was missing
  from `.env.example` entirely.

**Two consistency bugs in what the page shows.** `/api/predictions/live` used a 90 s heartbeat
window while the page banner used the contract's 60 s, so between those two figures a viewer would
have seen "the agent is not on the air" directly above a live prediction card; both now share one
exported constant. And the leaderboard rendered `0.0%` beside `100%` in the same column.

Also hardened: `siweOrigin()` now refuses to sign a loopback domain in a production build, because
the domain in that message is what a user reads in their wallet before approving.

**Verified — live chain.** Robinhood Chain mainnet is real and reachable: `eth_chainId` → `0x1237`
(4663). TTWO at `0x5e81213613b6B86EaB4c6c50d718d34359459786` reads back `symbol()` = `TTWO`,
`decimals()` = 18; a real holder balance reads 2591.75 TTWO; `paused()` = false; and a `transfer()`
to a never-funded address simulates `true`, so arbitrary recipient wallets are permitted at the
contract level. SIWE round-trip verified, including three negative cases (tampered nonce, wrong
expected address, signature from the wrong key).

**Two integration bugs were found and fixed during assembly, both silent-money-loss class:**
1. The API read the ledger's `numeric(38,18)` amounts as base units while settlement writes whole
   token units — a 10^18 undercount on every payout, and an outright throw on any fractional
   reward. The unit convention is now stated in CONTRACTS-PREDICTIONS §1 and the conversion is a
   decimal shift by the asset's `decimals`.
2. Claim creation was two PostgREST statements, which cannot be one transaction; a crash between
   them stranded a `pending` claim owning no ledger rows. It is now a single SQL function that
   also sums the amount itself under a row lock.

**The harness prediction wiring was reviewed adversarially, and four defects were found by
RUNNING it — one of which would have stopped the show.** All four are fixed, each with a
regression test that was checked for vacuity by reverting the fix and confirming the test fails:

1. **BLOCKER — every prediction the harness opened would have been rejected forever.**
   `predictions.reward_pool` is `numeric(38,18) NOT NULL CHECK (> 0)` and `reward_asset` is
   `NOT NULL`, both with no default and nothing backfilling them; the generator emitted neither.
   A rejected insert is not dropped — `events.py` requeues it and re-POSTs the whole growing
   backlog on the game-loop thread every 2 s, so the 2–4 Hz loop degrades until the agent stops
   playing, and the queue eventually evicts real event history. The layer's own integration test
   passed because it read the row back out of the in-process buffer, which does not know what the
   schema requires. Now the generator funds the pool (exact `Decimal`, whole token units, with the
   §13 event multiplier applied where the pool is funded), and the row is proven to INSERT against
   the real schema.
2. **The layer shipped ON by default**, against a project whose prediction tables do not exist.
   Now opt-in per deployment (`WASTED_PREDICTIONS_ENABLED`, default false).
3. **Predictions were opened from a frozen screen snapshot.** `_offer_prediction` had no
   `_believe_state()` gate, though `_heartbeat` on the very next line does — measured at 24 of 25
   offers during a blocking-screen freeze, one of which wrote a `state_context` read off a snapshot
   frozen hundreds of ticks earlier. Because a frozen game emits no events, §3's completeness gate
   never opens for that window, so the question voids: the audience is shown a question built from
   data this process has already decided is a lie, answers it, and gets nothing back.
4. **No back-off on persistent write failure.** A circuit breaker now stops generation after five
   consecutive failures and says so once, rather than every tick.

**One more brand leak, caught by that same insert.** The catalogue's seven question strings still
named the agent by its old character name — text that renders on the live prediction card. Renamed.
The knowledge base still refers to the character that way in ~800 places, but that is model INPUT
and it is contained by a gate on model OUTPUT: the old name is in the validator's hard-banned list,
and a line containing it is rejected in `say` or `thought`, in any casing. Proven, and covered by a
regression test — it is not hypothetical, because the production `decisions` table already contains
two lines where the agent addressed itself by that name, and those render on the public site.

## 2026-09-20 — pre-funding review: four blockers found and fixed

Fable's review before the first tranche. Three Sonnet reviewers in parallel (harness generator,
settlement SQL, web + treasury), one Opus skeptic on the single finding that could pay the wrong
people. The treasury and payout code came through clean — persist-before-broadcast, DB-sourced
nonces, no error path that both fails a claim and could still land on chain, eligibility enforced
server-side on every money route. The four blockers were all elsewhere.

**B1 — two of eight shipped templates could never resolve.** `enters_vehicle` and `exits_vehicle`
passed validation because `vehicle_entered`/`vehicle_exited` are in `TELEMETRY_RULE_KINDS`, but
`settle_due_predictions()` (`20260908120000_predictions.sql:584`) matches those kinds and
unconditionally voids with `missing_telemetry`. They would have taken real entries and settled void
100% of the time — a card that never has a winner. `land_the_helicopter` had already been rejected
for exactly this reason, so the standard existed and simply was not applied. Both carried
`reliability=0.75`, above `mission_outcome`'s honest `0.5`, so ranking actively preferred them.
Fixed: both moved to `REJECTED_TEMPLATES` with the measured reason; `UNSETTLEABLE_RULE_KINDS` added
and enforced in `_build_catalog()` at import time; a test re-derives the always-void set from the
migration by parsing which dispatch branches can assign `v_result`, so the constant cannot drift
from the SQL. Proven non-vacuous: re-shipping such a template raises at import and collection fails.

**Consequence worth an operator decision:** those two were the only templates that fired on an
ordinary quiet moment. Every survivor needs a real trigger (wanted gain, fight, mission), so
predictions now open only on events. That matches the calibration finding that ambient questions
are 99%+ giveaways, but it does lower prediction cadence, and nothing ambient replaces them yet.

**B2 — a paid winner was told they had won nothing.** `/api/predictions/live` built `mine[].reward`
from the raw `reward_ledger.amount` (whole units) while `MyEntry.reward` is declared a base-unit
string. `hasBaseUnits()` requires a plain integer, so a real credit of `"10.000000000000000000"`
rendered as "no reward was credited" on both cards and was dropped from the viewer's earnings
tally. Claimable balance was always correct, which is why it went unseen. Fixed with the
`amount::text` cast and `toBaseUnits()` at the boundary, matching `/api/rewards/balance`.

**B3 — the harness settled nothing, silently.** `ticker.py` called `settle_due_predictions()`, which
`20260914000001_settlement_guards.sql:89` revokes from `service_role`; the granted function is the
advisory-lock wrapper `settle_due_predictions_serialized()`. Every tick raised `42501`, was
swallowed by `_call_one`'s deliberately broad `except`, and reported as `settled: None`. So the
contract's named PRIMARY driver locked predictions and never settled one, leaving the 1-minute
Vercel cron as the only driver and silently breaking this package's own 30 s windows. Fixed, plus a
test that replays every migration's grants in filename order and asserts each RPC the ticker names
is still EXECUTE-granted to `service_role` — the cross-artifact check whose absence caused it.

**B4 — settlement could resolve on evidence that had not landed.** CONFIRMED by an Opus skeptic that
tried to falsify it on five fronts. The completeness gate opens on
`max(events.ts) >= resolves_at OR stats.heartbeat_at >= resolves_at`, treating the heartbeat as
proof the writer caught up. The writer is not one ordered channel: every `(table, op)` run is a
separate HTTP request and a failed run does not abort the flush, so a failed `events` insert
followed by a successful `stats` upsert publishes the proof without the evidence. `SETTLEMENT_GRACE`
does not help — it is only consulted when the gate is shut. Two corrections to the original report,
both making it worse: `survives_window` is NOT safe (`insert_event_now` only bypasses the buffer
when the clip pipeline is up, so with OBS down a queued `death` resolves the window as SURVIVED —
failing toward paying the wrong people), and a deterministically-rejected row blocks its run's head
of line indefinitely while the 1-row heartbeat keeps succeeding, making it a steady state rather
than a coincidence. Fixed at both layers, no migration: `stats` runs last in a flush and is skipped
entirely once anything has failed, and `flush()` records `unflushed` which `_heartbeat` reads to
withhold a heartbeat over a backlog. Cost, accepted: the site reads OFF AIR during a write backlog.

**Verification.** Harness: full suite passes, ruff back to the same 8 pre-existing findings as HEAD.
Every fix has a regression test proven non-vacuous by reverting the fix and confirming failure — B4's
drives the real `SupabaseWriter` against a real HTTP server that accepts `stats` and refuses
`events`, and with the fix reverted it prints the actual POST trace showing the heartbeat published
behind the failed event. Web: typecheck and lint clean.

**NOT verified, and why:** B2 is typecheck-clean only. The assertion that proves it
(`mine[].reward` is base units and a plain integer string) has been added to
`web/scripts/verify-full-loop.mjs`, but that harness expects an already-running Postgres container
and Next server which nothing in this repo scripts, so it was not executed here. Run
`npm run verify:loop` against the local stack before funding. Note the existing 50 assertions
checked `claimable` and never `mine[]`, which is exactly how B2 survived them.

**Still open, not blocking funding but before `rewards.enabled`:** the four operator-only outbox
functions (`resolve_claim_review`, `resume_payouts`, `cancel_queued_claim`, `record_claim_override`)
are granted to the same `service_role` the app uses, with no lease or fencing, so "only an operator,
in SQL" is a convention rather than something Postgres enforces; settlement's three caps are not
scoped by `asset` (`20260908120000_predictions.sql:656-662`), harmless while TTWO is the only asset;
the prediction circuit breaker cannot catch a rejected INSERT (the write is buffered and
`events.py:flush()` swallows and requeues), so it guards a generator bug, not a broken table;
`CONTRACTS-PREDICTIONS` §6 and §10.6 give opposite defaults for unset `CLAIM_MIN_AMOUNT` /
`CLAIM_MAX_AMOUNT` (§10 wins by the doc's own precedence rule, but §6 is not marked superseded) —
and both must be set in Vercel, since unset is a refusal, not "no limit".

**Unchanged and still true:** prediction generation has never run against live game state. The live
project holds no predictions and the newest heartbeat in `stats` is 2026-08-25.

## 2026-09-15 — TTWO payouts rebuilt; NOT funded, NOT deployed

**Not funded.** No treasury key exists, and no transfer has ever been broadcast to mainnet from
this codebase.

*(Corrected 2026-09-20. This paragraph used to read "the work is in the working tree, uncommitted,
and none of the four new migrations is applied to the cloud project." Both halves had gone stale:
the work is committed as `cf51a1f`, and the migrations ARE applied — probed directly against the
live project, `predictions` / `reward_ledger` / `reward_claims` / `treasury_accounts` all answer,
and `payout_lease` holds its singleton row. `treasury_accounts` is empty, which is what actually
proves nothing has ever been signed. Both switches read off: `site_config.rewards` is
`{"enabled": false, "payouts": false}`, and all three `reward_caps` are present and positive.
Same failure mode as the 2026-09-09 grant paragraph below — a prose claim about the database
outliving the database. A probe settles it; prose does not.)*

**Why it was rebuilt.** An Opus audit of the old claim path (18 failure modes, 2026-09-14) found two
critical defects: a wallet could claim exactly once in its life (a `submitted` claim never left the
one-in-flight index, because nothing ever read a receipt), and an ambiguous broadcast error released
the credits of a transfer that may have landed — a double payment. Fable ruled FM-01..FM-14 must be
fixed before funding and specified the replacement: an outbox (CONTRACTS-PREDICTIONS §10).

**What exists and how it is verified:**
- **Eligibility — every wallet was being refused.** Production has `POLICY_BLOCKED_REGIONS` and
  `POLICY_REQUIRE_TERMS=true`, and no caller passed a country or recorded rules acceptance. Now the
  SIWE statement names the /rules version, the signed session carries it, `wallet_sessions.rules_version`
  records it (`20260914000000`), and routes pass Vercel's `x-vercel-ip-country`. New public page
  `/rules` (v1) renders the restricted list from the same env var policy enforces. Verified:
  `npm run verify:siwe` 11/11, including a signature over the pre-rules statement being rejected; the
  `/verify` burn write checked through real PostgREST.
- **Settlement guards (`20260914000001`).** Settlement runs behind a transaction-scoped advisory lock,
  reachable by `service_role` only through `settle_due_predictions_serialized()` (FM-10: overlapping
  runs could each spend the full daily cap). `rewards_enabled()` is false unless all three caps are
  present, numeric and positive (FM-11: a missing cap meant 1,000,000 TTWO/day), and no longer raises
  on a non-boolean switch value — the 2026-09-09 version did, which would have aborted every settlement.
  Verified with a genuinely contended lock: a second backend holds the key, the run returns 0 AND
  leaves a due prediction unsettled, and the next run settles exactly it.
- **Payout outbox (`20260914100000`, `web/src/lib/rewards/{payoutWorker,treasury}.ts`).** Claims only
  enqueue; one lease-holding cron worker signs; signed bytes are persisted before broadcast; nonces
  come from the database; credits return only with on-chain proof; an unrecorded use of the key halts
  the treasury. `infra/verify-predictions.sh` exits 0: 35 deliberate refusals all raised as expected,
  zero unexpected errors, every §12 witness correct (lease fencing incl. an expired zombie lease,
  strict nonce order 7→11 with a rollback, all four release proofs, halt/resume, publication filter)
  and invariants I1/I2/I4/I6. **That suite found a real bug**, fixed before anything was applied:
  `confirm_claim` accepted ANY hash when no override existed (SQL NULL logic — §10.8).
- **Custody tool** `web/scripts/treasury-push-key.mjs`: 6/6 required checks + 17 extra against real
  throwaway `cast` keystores (wrong passphrase refused by MAC, wrong/missing `--expect` refused, zero
  64-hex runs in any output); its real Vercel push has never run.
- Web typecheck and lint clean.

**Fault drills — PASS, 179/179, three full runs** (`npm run verify:payout`, 2026-09-15). Real Postgres
16 with every migration, real PostgREST with a real service-role JWT, the product's own compiled
worker, and an anvil fork of Robinhood Chain mainnet (real TTWO bytecode). Credits come only from
real settlement over the real recorded session (5,649 events). Drills: happy path (receipt hash =
tx_hash = keccak256(raw_tx), recipient delta exact to the wei); the same wallet paid again (FM-01);
crash after persist, before broadcast; ambiguous broadcast — the node took the tx, the caller got a
502 — paid exactly once (FM-02); dropped tx re-broadcast; reverted tx failed with credits returned;
an unknown use of the treasury's nonce halts everything until an operator rules; operator cancel;
three concurrent workers produce exactly one signer; the kill switch; preflight refusals; invariants
I1–I14. Gas per payout on the fork: 77,659.

**Security review — Fable: conditional GO for the first tranche.** Five Sonnet finders, a Sonnet
skeptic per finding, Fable's ruling (CONTRACTS-PREDICTIONS §10.9). Two blocking fixes, both landed
and proven before anything was applied: **B1** — `service_role` could INSERT a claim with no credit
behind it and the worker would have paid it (now refused: `permission denied for table
reward_claims`, asserted by running the exploit); **B2** — a wallet blocked mid-signing was still
paid (now cancelled, nonce handed back). All twelve fix-soon items landed too (FS1–FS12), including
`service_role` losing write access to `site_config`, override transactions checked for sender and
nonce, claims signed by a rotated key held for review, and fixed error text on every route. The SQL
suite exits 0 with 37 deliberate refusals and none unexpected.

**Found by verification, fixed before it mattered:** `confirm_claim` accepted any hash when no override
existed (SQL NULL logic); `readLatestNonce` mislabelled a bad RPC URL; the public treasury status
published pre-payout balances.

**Still unproven, and why:** a payout on real mainnet (by design nothing here broadcasts to mainnet;
first real payout is operator gate G-I); `token_paused` against a real paused token (none of the 194
tokens on chain 4663 is paused — it is a fail-closed boolean read). `npm run verify:treasury` passes
every check of our code but the public RPC (`rpc.mainnet.chain.robinhood.com`) answers repeated runs
from one machine with HTTP 403 pages; the worker treats that as `rpc_unavailable` (never a failed
claim), and a private `TREASURY_RPC_URL` is recommended before funding to the ceiling.

**Operator gates before any money moves** (docs/RUNBOOK.md §5): apply the migrations, deploy, generate
the key in your own terminal and push it, redeploy, verify the tick reports the right treasury, then
fund the first tranche only (0.05 TTWO + 0.005 ETH). Hot ceiling 1.75 TTWO + 0.02 ETH.

**LIVE at wanted.money since 2026-09-09**, serving the deployment built from commit `8e6ea09`.
Domain registered through Vercel, nameservers verified, 17 environment variables set in production
and preview, rewards deliberately OFF.

"Rewards off" now means two independent switches, because it used to mean only one and the one it
meant was the wrong half. `REWARDS_ENABLED=false` in the Vercel environment blocks CLAIMING: it is
read by `assessEligibility()`, whose only callers are four API routes. Nothing read it at CREDIT
time and nothing could — the credit is written by `settle_due_predictions()`, a Postgres function
with no access to a Node process's environment — so with rewards "off", settlement would still have
written `reward_ledger` rows as soon as the agent produced a resolvable prediction.
`20260909010000_rewards_master_switch.sql` closes that with a `before insert` trigger on
`reward_ledger` reading `site_config` key `rewards`, defaulting to OFF when the row is absent.
Verified in `infra/verify-predictions.sql` §10: the same window, pool and correct wallet that pay
5 TTWO in §3 produce zero ledger rows with the switch off, while the prediction still settles and
the streak still advances. Nothing was ever paid out under the old behaviour — the live project has
no predictions, no ledger rows, and its last heartbeat is `2026-09-07T15:54:57Z`.

Deployments were BLOCKED three times first, with `TEAM_ACCESS_REQUIRED`: Vercel attributes a CLI
deploy to the git commit author, and `agent@wanted.run` holds no seat on the team. Resolved by the
operator's decision to author the commit as the Vercel account owner instead.

**A production-only security bug was found immediately after going live, and is fixed in
`20260909000000_fix_function_grants.sql`, which IS now applied to the live project.** Re-probed
2026-09-09 with the browser's own publishable key: `enter_prediction` and `lock_due_predictions`
both return `42501 permission denied`. Before the fix they returned `false` and `0` — anyone
holding that key, which ships to every browser, could submit prediction entries for a wallet they
did not own, bypassing sign-in and the API's rate limits.

An earlier revision of this paragraph said the migration was not yet applied, and stayed that way
after it had been. A later review read that sentence and reported the vulnerability as live; the
single probe above settles it and is cheap enough to prefer over any prose, this paragraph
included. The migration is also listed in `infra/apply-predictions-to-cloud.sh` now, so a project
brought up by that script can no longer be left in the state the fix describes — it was missing
from the array, which is how the doc and the database came apart in the first place.

The cause is Supabase-specific and unreachable by local testing as it stood. Supabase ships
`alter default privileges in schema public grant all on functions to anon, authenticated,
service_role`, so every new function is granted to those roles DIRECTLY. The policies migration
revoked only `from public`, which clears the PUBLIC pseudo-role grant and leaves the direct one
intact. A plain Postgres has no such default, so every local run passed. `create_reward_claim` and
`finalize_reward_claim` were never exposed — their migration happened to name `anon, authenticated`
in its revoke, which is now the pattern everywhere.

`infra/verify-predictions.sh` now installs the same default privileges before applying migrations,
so a local run reproduces Supabase. Proven non-vacuous: with the old `from public` revoke the check
reports **0 of 3** functions denied to anon; with the fix, **3 of 3**.

Everything else on the live project was verified correct at the same time: every direct table write
by `anon` is denied, `reward_ledger`/`reward_claims`/`wallet_sessions` are unreadable, and both
money functions are denied.

**NOT verified, and cannot be from here:**
- **The agent was not playing.** Last heartbeat `2026-09-07T15:54:57Z`, ~25 h stale at time of
  writing; the session had ended. Steps 1–2 of the end-to-end flow (agent live → telemetry
  produces an eligible event) are unproven, and prediction *generation* has therefore only been
  exercised against the recording, never against live state.
- **No on-chain claim has ever been executed.** There is no treasury wallet, no funding and no
  signing key. `sendReward` is gated behind `isTreasuryConfigured()` and returns an honest
  "not configured" error today.
- **`$WANTED` does not exist.** No contract, no address. Every surface depending on it is hidden
  rather than zero-filled.
- **Migrations are not applied to the cloud Supabase project.** Confirmed against the live project:
  `GET /rest/v1/predictions` → `PGRST205`. Every route touching new schema fails honestly with the
  real PostgREST error rather than a fabricated 200.

**Base rates measured from the recording, which changed the product.** The recording covers
**86.93 h of continuous play** — the largest gap between consecutive events anywhere in it is 7.8
minutes, so there is no dead air to correct for. Sampling every 30 s across it (n = 10,426
windows), unconditional questions are nearly all giveaways:

| question (asked ambiently) | YES |
|---|---|
| survives 3 min (no death, no arrest) | **99.1%** |
| dies or is arrested in 3 min | 0.9% |
| gains a wanted star in 2 min | 1.1% |
| reaches 3+ stars in 3 min | **0.0%** — it never happened once in 86.93 h |

Triggering the same questions on a real wanted-star gain is what makes them worth asking (n = 39
real gains):

| question (triggered on a wanted-star gain) | YES |
|---|---|
| clears the cops within 90 s | **53.8%** |
| survives the chase, 150 s | **48.7%** |
| dies within 240 s | **43.6%** |

So the trigger and the window are both load-bearing, and neither could be guessed. The catalogue
ships 7 templates calibrated against these numbers and rejects `wanted_reaches_3` and `wanted_gain`
outright, recording the measured reason in `REJECTED_TEMPLATES` — the same pattern
`events.py::UNPRODUCED_EVENT_REASONS` uses for telemetry that does not exist. The calibration test
re-derives every number from the fixture on each run rather than trusting a comment.

*(An earlier draft of this entry said "~6.5 h of true playing time" and quoted 97.9% / 2.1%. That
was wrong: the 6.5 h came from a block analysis of mine that discarded blocks under 50 events and
then misread the surviving boundaries as dead air. The figures above are the correct ones, computed
twice by different methods that now agree.)*

## 2026-09-03 — "make the agent fun to watch" program (stream PAUSED, brain key DISABLED)

Operator brief: five phases, autonomous, stop at READY TO GO LIVE. Fun is defined as automated
checks F1–F6 (idle < 5 %, something new every 60 s, no line without an event, ≥ 60 % goal
completion, < 6 roam deaths/h, moves within 3 s of control). Everything is built and verified
OFFLINE against a fake brain and recorded/synthesised `/state` streams — no live brain calls.

- **PHASE 0 done:** six subagent definitions in `.claude/agents/` (registry picks them up next
  session; this session maps them to executor/verifier with the same model overrides). Safety
  net (fake brain, 5 Hz `/state` recorder, replayer through the REAL selection code, F1–F6
  `funcheck.py`) in progress.
- **PHASE 1 done — `docs/findings.md`:** R1 no drive-start sequence + `cleared_by_game` (111 in
  2 h); R2 a line on every decision, decisions on every poll; R3 mission goal leaking through
  missions-off (fixed `788956b`, undeployed); R4 model goal id never reached the plugin (22/22
  picks fell back; prompt fixed, undeployed); R5 stranded livelock (fixed, deployed); R6 phone;
  R7 **401 — the key on the box and on the dev machine are both invalid**; R8 weapons/aimed
  fire/drive-by/airtime/taxi not expressible. Tickets T1–T11.
- **PHASE 2 — fix-opus-b DONE** (T6 weapons/shoot_at/drive_by/in_air/seat, T7 chaos ladder L1–L3 with F5 step-down, cadence config, 95 roam tests; bridge 1.7.0; CONTRACTS v1.14 written by Fable, loadout default `ammunation` as an operator-requested, bounded exception). It also found the live bug behind "it can't cut the call": the phone tasks were missing from `bridge_client.BRIDGE_TASK_TYPES`, so `post_task` raised before I/O — routed to fix-sonnet-d.
- **PHASE 2 — fix-sonnet-c DONE** (T3 say only on an event, forced "" in code; T4 validator — names ⊂ STATE, mission-name mismatch, banned phrases from `commentary_style.md`, dedupe ≥ 0.6, ONE regenerate then drop, action always kept; T5 dashboard goal plugin-written, mood tracked not model-written; 51 tests; found and fixed `gate_say("")` letting an empty line through on an empty history).
- **PHASE 2 running:** fix-opus-a (T1 drive-start + verify, T2 wheel-only movement),
  fix-opus-b (T6 weapons + attack/shoot/drive-by/airtime/taxi tasks, T7 chaos ladder L1–L3 with
  F5 step-down + mission cadence config), fix-sonnet-c (T3 line-only-on-event in code, T4
  validator with one regenerate, T5 plugin-written dashboard goal), fix-sonnet-d (T8 phone
  answer/hang-up-25 s/destroy-stuck-UI, T9 reflexes + F6 on every control edge), fix-sonnet-e
  (T10 safety net). Merge order a → c → b → d, then `verify`.
- **sshd throttle fixed on the box** (`MaxStartups 60:30:200`, `LoginGraceTime 30`, inserted
  before the Match block, `sshd -t` clean, restarted, reconnect verified) — the 3-minute waits
  between connections are over.
- **Site:** the production site shows "N days, H hours since the agent was born" in place of the counters.


## 2026-09-03 — live: missions off, the stranded livelock, and the phone

**the agent is live on bridge 1.5.0, deployed by HOT RELOAD** — the DLL swapped 1.2.0 → 1.5.0 with the
game running and the stream never dropping. First time it has worked; every bridge fix from here is
a hot swap, not a game restart. `ReloadKeyBinding=Insert` in `ScriptHookVDotNet.ini` is what made
it possible.

**Missions switched OFF** by operator call ("not ready yet"): `WASTED_MISSIONS_ENABLED=false` on the
server. `start_nearest_mission` is never offered, never forced at 3 goals / 15 min, and the day
planner never schedules or accepts a mission block. Free roam is the whole show; 14 goals on the
menu, `pick_a_fight` and `gang_trouble` included now that `fight_ped{handle}` exists.

**Livelock found by the new wheel log, minutes after deploy, and fixed:** `stranded` is
reflex-class, so it outranked roam and preempted every goal ~2 s after it was picked — including
`roam_the_block`, whose own plan IS `enter_nearest_vehicle`. It preempted a goal to do the thing
the goal was already doing, forever. A live roam goal now stands the stranded ladder down; the goal
has its own stuck watchdog. The log that exposed it:
`wheel preempted owner=roam by=stranded` / `roam goal ended outcome=preempted duration_s=0.3`.

**`goal_fallback` fired on every pick:** the model wrote prose in the `goal` field ("cruise
around, find a bike, aim for a hill"). The prompt-file fix was not enough; the menu line itself now
names the field and shows the bare ids. Behaviour was never wrong (the fallback takes the top
offer), but his one genuine free-roam decision was being discarded. Deploys with the phone build.

**Phone calls — in progress (bridge 1.6.0, CONTRACTS v1.13).** Simeon called on stream; answering a
story call starts a mission. Researched, not guessed: there is NO native for "ringing" (it lives in
a build-specific script global), so the proxy is `IS_PED_RINGTONE_PLAYING(player) AND NOT
IS_MOBILE_PHONE_CALL_ONGOING()`. Answer/reject inject `Control.PhoneSelect` (176) /
`Control.PhoneCancel` (177) via `SET_CONTROL_VALUE_NEXT_FRAME` — keybinding-independent, names
verified against the pinned DLL. Policy: missions off → the reflex layer rejects; missions on → the
brain gets a `PHONE: ringing` line and chooses. Live-only unknowns, stated as such: whether control
group 0 or 2 registers, whether the ringtone proxy is clean on this build, how un-rejectable story
calls behave.

**Free roam variety + three new goals (17 total).** Novelty memory: the same goal id is never
offered twice running, and anything from the last four picks sinks to the back of the menu while a
triggered offer keeps the front. New and genuinely expressible: `chase_that_car` (follow_entity on
a moving fast car), `jack_a_driver` (enter_nearest_vehicle on an OCCUPIED car within 12 m is a jack
by construction), `honk_run` (wander + the horn primitive). Still impossible, unchanged:
rob_store, buy_gun, taxi_ride, big_jump.

**The retry storm, root-caused from the bridge log:** `enter_nearest_vehicle` started,
`failed: cleared_by_game` ~1 s later, re-posted within 300 ms by whichever owner got the wheel
next, with an empty Prairie 2.8 m away, for minutes. The game clears ped tasks for reasons the
harness cannot see — very likely the ringing phone taking the ped. `ClearedByGameBackoff` (per
task TYPE, 4 s doubling to 20 s, other types unaffected) is written and tested; the funnel wiring
lands with the phone build.

**Web: the counters are gone.** Operator: "statistics are not accurate". Replaced by one line —
how long since the first session ever recorded (`min(sessions.started_at)`, anon-readable, written
once by the harness and never touched). **Verified on production:** the live site renders
"8 days, 21 hours since the agent was born". Playwright assertion updated to match.

**715 tests passing, ruff clean.**


## 2026-09-03 — movement ownership, goal ids, and the interior escape

**Fix 1 — MovementWheel now GATES movement instead of recording it.** It was advisory: `claim()`
returned a bool callers could ignore and `force()` only logged a WARNING when two layers posted in
the same tick. That is the deadlock class behind "follow Lamar while standing next to the objective
car". Now `acquire(owner, reason)` returns a token or `None` on a fixed ladder
(reflex 3 > mission 2 > roam 1 > idle 0), and the gate sits at the single funnel every action
already passed through — `main._execute_action` refuses any of the bridge tasks whose token is not
the current holder's. The 8 primitives are exempt on purpose (`look_around` is a mouse sweep,
`wait` is a quiet period; neither moves anyone). Preemption cancels roam's task, clears
`roam.current` and emits `roam_goal_failed{preempted}`.
Verified: refusal, exclusivity, release, preemption and `force_idle` (death/cutscene) all confirmed
against the real class.
**Deliberately NOT done:** the operator's spec asked for a token assertion in the bridge. The
harness is the bridge's only client and `_execute_action` is its only path there, so the guarantee
is identical without a contract change or a bridge deploy.

**Fix 3 — the model can finally choose a roam goal.** The plugin side was already strict
(`model_choice` exact-token matches against the ids actually offered, returns None on zero or two,
never fuzzy-matches). The bug was that **two prompts contradicted each other**: `situations.md` said
"Pick exactly ONE, by id" while `decision_guide.md` said "your `goal` field is an echo, not an edit,
repeat the GOAL line unchanged". The echo rule won, `model_choice` almost never matched, the engine
silently took `available[0]`, and every opportunistic trigger was decoration. The roam case is now
stated first and the echo rule scoped to everything else. Added the two log lines the spec asked
for: `goal_fallback` (named no offered id) and `goal_switch_ignored` (a goal is locked and it named
another) — a RUN of either means prompt and menu have drifted, which is invisible from the stream.

**`steal_cop_car` could never complete**, the same never-completes bug `earn_two_stars` had:
sitting in a police car reliably earns a star, so the wanted-override killed the goal at the exact
moment it started working. Exempted via `wants_heat`.

**Withdrawn: `pick_a_fight` and `gang_trouble` are buildable after all.** I ruled them out because
nothing could initiate violence against a peaceful ped. That was true of the 19-action catalog; it
is not true now — **`fight_ped{handle}` exists** (the catalog is 20) and `TaskEngine.StartFightPed`
attacks any named ped regardless of relationship. To be added.

**679 tests passing, ruff clean.** Nothing deployed: the operator's API key is off and he deploys
himself.


## 2026-09-02 (night) — CONTRACTS v1.11 / bridge 1.4.0: built and verified locally, NOT live-verified

The operator asked whether a GTA mod would make the agent a pro. Three sourced research passes say no:
**the mod is Script Hook V + SHVDN, which we already run** — the gap was native API we were never
calling. Trainers (Menyoo, ENT) are cheats and were refused. Research also found **no strong
open-source prior art** for an AI playing GTA missions via native tasks; the whole self-driving-GTA
cluster is abandoned CNN keypress bots, the approach this project already rejects. Briefs:
`docs/research/brief-driving-natives.json`, `brief-combat-natives.json`,
`brief-mission-comprehension.json`.

**Three defects root-caused from the live run + operator screenshots:**

1. **He could not fight back because the fight command never ran.**
   `combat_hated_targets_around` requires a nearby ped whose relationship is Neutral/Dislike/Hate
   or the engine task **exits immediately**. A civilian whose car he stole is plausibly still
   Respect/Like — so his only retaliation action has been a silent no-op. Fixed with `fight_ped`
   (target-explicit, no relationship setup); the melee path is the one R*'s own
   `player_scene_t_bbfight` calls on `PLAYER_PED_ID`. He now also learns he is being attacked
   **before the punch lands** (the game reports a ped's melee target while the swing animation
   plays), instead of waiting to accumulate damage, and no longer leaves a working car to brawl.
2. **He searched at random for a crewmate the game was drawing on the minimap.**
   `nearby.peds` reaches ~50 m; once the crewmate drove off he was gone from `/state` entirely.
   `mission.entity_blips[]` (blips pinned to a ped/vehicle, route or not, carrying the game's own
   label) is now both a normal follow target and a recovery rung that outranks driving to a stale
   last-seen position. He names who he is tailing.
3. **He drove badly for a documented reason.** `avoid_traffic` was `786468` — the RECKLESS preset,
   documented as *"doesn't use the brakes at ALL to help with steering"*. Retuned. In-vehicle
   `follow_entity` now uses the engine's mission-follow task with a straight-line-to-target
   distance (the fix for losing a target at junctions) and explicit `driveAgainstTraffic: false`.
   Wedged cars recover with reverse / reverse-and-turn nudges and a task re-issue — never a
   teleport.

Also: `player.switch_in_progress` and `mission.retry_in_flight` now gate tasks and commentary (the
real signal behind him narrating "wrong body / waiting for the switch"), and commentary grounding
accepts names the game itself attached to a blip.

**Declared assist, on the record:** the bridge sets the engine's own driver ability (0.8) and
aggressiveness (0.5–0.8) for the player ped. Engine-clamped, and they change AI *competence*, not
vehicle physics. **Refused as cheats and verified absent by grep:** perfect-accuracy and
shoot-through-walls attributes, accuracy/shoot-rate above human range, giving weapons or ammo,
the teleport-out vehicle-exit flag, wanted-level clearing, police-ignore, self-righting a car.

**Evidence (local only):** bridge `dotnet build -c Release` 0 warnings / 0 errors;
`bridge/tools/offline-checks/run.sh` **187 passed / 0 failed**; harness **674 passed**,
`ruff` clean. An independent verifier re-ran all four, found **no cheat-list hits**, confirmed the
declared assist values, and cross-checked every wire name between the C# DTOs and the Python models
(a mismatch there would silently drop fields in production) — all agree, all backward-compatible
defaults, so a pre-v1.11 bridge payload still parses.

**NOT VERIFIED — this is the honest line.** None of this has run against the real game. The code
itself flags the specific unknowns for the live smoke test: whether the mission-follow task really
beats the old follow at junctions, whether `TASK_COMBAT_PED` does anything on a *player* ped (the
melee path is the confirmed one; the ranged path has zero R* precedent), whether the
combat-attribute and driver-competence setters take effect on a player ped, and whether the stuck
ladder's timings feel right. Do not read "verified locally" as "he plays well".

**Concurrent work, flagged:** `harness/wasted_harness/behavior/roam.py` + `tests/test_roam.py`
(~1800 lines, a free-roam goal engine) were written by a DIFFERENT session, are wired into
`main.py`, and rode along in commit `3362f73` to keep the tree consistent. They route through the
same `_execute_action` choke point so the new gating applies to them, and they touch none of the
v1.11 fields — but they are **not** covered by the verification above and want their own review.

## 2026-09-02 (evening) — CONTRACTS v1.10 deployed to the real machine; six real bugs found BY the live run

Bridge **v1.2.0** and a rebuilt harness went onto the server and the agent played on them. The run is
what produced this section: every item below was found by watching him, not by reading code.

**Deployed and confirmed live on the real game:**
- `bridge /health` → `version 1.2.0`, `tick_hz` 47-54, edition legacy.
- **`mission.script` works**: `/state` returned `script=Armenian1` during play. The agent now gets his
  mission identity from the engine's own running script thread instead of guessing.
- All three model tiers verified against the real API at startup, including the new
  **`tactical_mission` (Sonnet 5)** tier: static prefix 23671 tok, cacheable.
- The new **target-lost recovery ladder fired in a real mission** (`rung=reacquire`).
- The **commentary similarity gate fired** ("repeated commentary line" suppressed).

**BUGS THE LIVE RUN FOUND (all fixed, 599 tests green, ruff clean):**

1. **Wrong mission card — the cause of every "random" thing he said.** The identification ladder
   fell back to a ZONE guess even when the engine had named the script. Logged live:
   `mission identified: title_read=None zone="Pacific Bluffs" script=Armenian1 source=zone
   mission="The Wrap Up"`. He was in Franklin-and-Lamar and was handed The Wrap Up's walkthrough,
   so he narrated Dave Norton, a sniping Trevor and an ambush that did not exist (operator
   screenshots). Fix: a zone may only answer when the engine has NOT named the script; an
   unlearned script name yields "unknown" rather than a contradicting guess. Learned-pair lookup
   is now case-insensitive (`Armenian1` vs `armenian1`).
2. **`drive_to` without `speed_mps` — every recovery drive was rejected.** Live:
   `bridge rejected action type=drive_to status=400 invalid_params "drive_to requires numeric
   speed_mps"`. Both hand-built bodies were missing it, so the mutual-stall deadlock breaker has
   been **silently 400ing since it was written** — a hidden cause of the historical thrash loop.
   Fixed both; a structural test now pins the whole class.
3. **`dxcam` import could kill the harness.** `ScreenGrabber.__init__` guarded only `ImportError`,
   but dxcam builds its DXGI factory AT IMPORT and raises `COMError` (seen twice:
   `COMError(-2005270494)`). That escaped the constructor and bypassed `Harness.__init__`'s
   existing "run without screenshots" path, killing the process before its first tick. Now it
   degrades, loudly, as designed. (`dxcam.create()` had the same exposure; also closed.)
   **Root cause of the COMError itself: session.** dxcam can only enumerate outputs from the
   interactive console session; an SSH-launched harness has no desktop. Started from the
   `WASTED-Harness` scheduled task (Interactive, session 1) capture works — no "screenshots
   disabled" line, verified.
4. **Director token cap too small for the grown prompt.** First call of the session:
   `response hit max_tokens (1200) and the decision JSON is truncated`. The 1200 figure was
   measured against the pre-v1.10 prompt; the dynamic context has since grown. Raised to 3000
   (a ceiling, not a spend: billing is per token generated).
5. **Retaliation was too slow to matter.** He had to lose 10 HP inside 4 s — two or three punches —
   before the reflex would consider hitting back; a GTA melee is decided in about that many.
   `DAMAGE_ATTACK_HP` 10 → 4 (one clean punch). The 8 m attacker-proximity gate still prevents
   scrapes from starting fistfights, and a new test pins that.
6. **He drove too slowly to keep up.** Cruise 18 m/s (65 km/h) against mission NPCs that do 30 m/s.
   Cruise 18→24, rushed 26→34, rush-style distance 150→60 m; follow escalation now triggers on a
   12 m gap over 2.5 s (was 25 m over 6 s). All still human-attainable speeds — no cheats.

**New: commentary grounding (`brain/characters.py`).** A line naming a story character who is not
in `nearby.peds` (and is not the protagonist) is not published. Keyed off the game's own ped model
names, so streets, zones and car names are never touched. Tested against the exact lines he said on
stream ("keep Dave alive", "Trevor's got the rifle").

**Process note worth keeping:** the SHVDN research brief's tentative hashes for two tasks were
WRONG, and the bridge executor caught both by disassembling the pinned DLL rather than trusting the
brief — `flee_police` polls `SmartFleePoint` (it issues `TASK_SMART_FLEE_COORD`), and `wander_drive`
has its own `VehicleDriveWander` hash. The verifier then re-derived both from the same bytecode.

**Not verified (the operator disabled the API key at this point, deliberately):** whether these
changes make him play WELL. Everything above is "deployed, started, and observed", plus a green
599-test suite. The 20-minute unattended behavioural sample and measured $/hour still owe.

## 2026-09-02 — WANTED IS LIVE. First verified play session on the real game.

The brain was deployed to the server and started while the stream was running. **This is the
first time anything in this project has been verified against the real game rather than a test
suite** (CLAUDE.md non-negotiable 2).

**Verified live, from the harness log and `/health`:**
- Deploy gates all passed: 15 knowledge files unpacked, **632 items across 13 domains loaded**,
  prompt audit passed against the real API, game still ticking before and after (`tick_hz` 46 -> 50).
- `obs connected obs=32.2.2 ws=5.7.4`; Supabase taking `sessions`, `events`, `decisions`, `stats`.
- **The follow-the-blue-dot fix fired on a real mission within 20 seconds of starting:**
  `no objective marker; tailing the friendly blue dot instead handle=1282 distance_m=23.9`.
  That is exactly the failure that produced "Franklin lost Lamar" — he now recognises a
  marker-less follow phase and tails the crew instead of standing still waiting for a marker.
- Day planner running a mission block; lifetime totals seeded from previously published rows
  (deaths=4, busted=0, missions_passed=0).

**BUG FOUND AND FIXED IN THE SAME SESSION — the director was dead on arrival.**
Every director call failed twice and the reflex layer kept control, so the agent played with no
strategic layer at all. The logged error ("model returned no parseable decision object") was
misleading: the text was PRESENT, just cut off. Reproduced against the real API:

    stop_reason: max_tokens · output_tokens: 500 · block types: ['thinking', 'text']

**Sonnet 5 emits a thinking block, and it is billed against the same `max_tokens` budget.** At the
shared 500-token cap the thinking consumed the whole allowance and the decision JSON was truncated
mid-`params`. Haiku does not think, which is precisely why the tactical tier never showed the bug
and why 500 had looked fine for months. Measured at the same prompt: cap 1200 -> 443 tokens,
`end_turn`, parses; cap 2000 -> 599 tokens (the model spends more thinking when offered more).

Fix: `DIRECTOR_MAX_DECISION_TOKENS = 1200`, passed explicitly at the director call site, plus the
error now names a `max_tokens` stop instead of claiming the response was empty. Cost: 443 output
tokens at Sonnet 5's $10/MTok = **$0.0044/director call, ~$0.05/hour**. Three regression tests
added, including one asserting the call site actually passes the constant — the constant existing
was never the bug, not passing it would have been.

**Still not verified:** the 20-minute unattended behavioural sample, and whether he plays *well*
(as opposed to *at all*). Do not read "he is running" as "he is good".

## 2026-09-02 (late) — the agent is TAUGHT the game: 651-item knowledge base + 72 mission state machines

**Built offline, on purpose.** The operator disabled the API key and the Windows Administrator
account is locked out (error 0xd07, caused by my own SSH retry storm). Nothing in this section
touched the server, and nothing in it is proven in the running game.

- **Knowledge base — `harness/wasted_harness/brain/knowledge/`.** 13 domain files, **651 items**
  (hud_icons, map_markers, police_system, driving, combat, npc_entities, random_events,
  activities_freeroam, controls_interactions, vehicles, aircraft_water, failure_recovery,
  world_common_sense), each item carrying cue / context / meaning / suggested_action / avoid /
  urgency / confidence / exceptions / sources. Researched by 13 Opus agents against gta.wiki,
  gtabase, IGN and PCGamingWiki. Then **three adversarial Opus critics** reviewed the combined
  result and found **55 problems, 11 of them blockers**, which a repair pass then fixed
  (see "Knowledge quality" below). Not invented, not a walkthrough dump: sourced and reviewed.
- **`mission_states.json` — 72 story missions as state machines, 557 states.** Each state carries
  what he would SEE, what to do, the success and failure signals, a recovery path, and
  `has_marker`. **116 states are flagged `has_marker: false`** — the follow/escort phases where the
  game never draws a marker and he used to stand still waiting for one.
- **Retrieval, not injection (`brain/knowledge_base.py`).** The encyclopedia stays on disk; a
  per-tick `select()` returns only the items that match the situation — police knowledge when
  wanted > 0, aircraft knowledge in a helicopter, combat when something is hitting him, only
  locked-control items during a cutscene, and rotated free-roam knowledge otherwise. Verified
  against the real 651-item base: a Buzzard retrieves rotor-contact and Fort Zancudo airspace; four
  stars retrieves "break line of sight, not speed"; a cutscene retrieves 3 items and nothing else.
  Ranking is urgency, then situational preference, then confidence — preference breaks ties INSIDE
  an urgency tier so a four-star police warning is never pushed out by a map note.
- **Measured cost of the knowledge block:** ~450 uncached input tokens per call =
  **$0.119/hour** ($2.85/day at 24/7). The static prompt grew 13.8k → 15.7k tokens, which is
  cached and therefore ~10% of that per read.
- **Measured latency:** `select + render + mission_state_hint` = **0.28 ms per decision** over 500
  runs against the real files, which are read from disk once and then served from an `lru_cache`
  (7 misses, 3500 hits). Against an 8–25 s decision cadence this is free. Retrieval runs only
  inside `_dynamic_context`, i.e. once per model call, never per tick.
- **Integrity check:** all 15 knowledge files parse, no duplicate ids, every item carries all 11
  required keys, and all 72 mission names match `missions.json` exactly, so the state machines
  actually resolve for the mission the vision call identifies.

### Knowledge quality — what the critics caught

Worth recording because it is the reason this is not just a pile of scraped text:

- **Capability contradiction (blocker).** Five domains built survival policy on pressing Caps Lock
  for special abilities; the combat domain said he has no such key. The frozen action catalog
  (CONTRACTS §2) settles it — **he has no aim, no fire, no weapon select, no special ability, and
  no arbitrary keypress.** Knowledge that tells him otherwise makes him narrate actions that never
  happened, which is the worst possible failure on a live stream.
- **Friendly-fire risk (blocker).** Two mission states called for `combat_hated_targets_around` —
  an AREA task — with Amanda and Tracey, and Franklin and Chop, inside the radius.
- **"Never fight police" stated absolutely (blocker).** Eight scripted missions require exactly
  that; the rule needed scoping to free roam.
- **Wrong facts (blocker).** Two domains carried two different, both wrong, weapon-key tables.
  The critics also reported The Third Way's protagonist assignments as rotated, and **I passed that
  on to the repair pass as fact without checking it — I was wrong.** The repair agent refused the
  instruction, re-verified, and kept the original. Confirmed afterwards against gta.wiki: it is
  **Michael → Stretch** (B.J. Smith Recreation Center, "Michael decides to eliminate him for
  Franklin"), **Trevor → Haines** (Del Perro Pier), **Franklin → Cheng** (Pacific Bluffs, "since
  Franklin is unknown to the Triads"). The dataset's own notes already recorded that 3 of 4
  sources agree with this and one outlier does not. Recorded here because the process working —
  an executor rejecting an orchestrator's unverified claim — is the part worth keeping.
- **Unbounded waits (blocker).** Seven mission states told him to hold position and explicitly
  suppressed stall detection, with no maximum and no escalation — an infinite wait on a live show.

## 2026-09-02 (late) — behaviour and bridge fixes from the live stream failures

- **CONTRACTS v1.9 frozen** (and v1.8 written down retroactively — the bridge had been emitting
  `mission.route_blips` for a day with no contract entry and nothing on the harness side reading it).
- **Follow missions were unwinnable at the bridge level.** `StartFollowEntity` tailed with
  `DrivingStyles.Normal` and a hard-coded 15 m/s cap: it stopped at red lights and topped out at
  54 km/h while the NPC being tailed did neither. This is "Franklin lost Lamar", and no prompt
  change could ever have fixed it. Now `ignore_lights` at 30 m/s by default, `speed_mps` settable,
  clamped 1–60 m/s. `MissionFollower` escalates to 40 m/s once the gap has been widening for 6 s.
- **`style` is deliberately NOT exposed to the brain on `follow_entity`.** It is a non-nullable
  wire key (v1.4), so listing it would put `style: "normal"` on every follow and re-create the bug.
- **The GPS route is now readable.** A `route_blips[]` entry with `kind: "entity"` beats guessing
  at the nearest blue dot: it is the game's own answer, and it can name the car rather than the
  driver.
- **Mission failure was invisible to him.** `MissionTracker` decided failure with
  `player_dead or player_arrested`, so a follow mission that fails because the target escaped —
  killing nobody — emitted **no event at all** and the brain was never told. That is the operator's
  "it cant understand if mission is failed it keeps on sayin random things". Now the mission-end
  screen is read with one cheap vision call (the same mechanism CONTRACTS v1.3 established for
  mission titles), `mission_end` is armed, and `missions_passed` — displayed on the public site and
  the social preview image, and never incremented by anything — increments **only** on a confirmed
  pass. An unreadable screen emits nothing rather than guessing.
- **Animals can no longer be combat targets.** `NearestHostile`/`CountHatedTargets` now exclude
  them, reusing `SnapshotBuilder.IsAnimal`. This is the cat that held him at 0.2 m of movement for
  20 seconds mid-mission while he said "cat" more than any other word that session.
- **New reasoning-discipline prompt (`prompts/thinking.md`)**: hypothesis-then-check, action memory,
  a six-rung anti-loop ladder, anti-hallucination, and urgency tiers — written from the observed
  failures, not from generic advice.
- **Verified locally:** `481 passed`, `ruff check .` clean, both bridge projects `dotnet build`
  succeeded (0 warnings, 0 errors) against the pinned ScriptHookVDotNet reference.
- **NOT verified:** any of it, in the running game. See the blocker below.

### Second critic pass (after repair) — what it caught and what was done

The repaired base was re-audited by three more Opus critics. Two of their blockers were real and
are now fixed:

- **47 mission states told him to call actions that do not exist.** `enter_vehicle(...)` (the real
  name is `enter_nearest_vehicle`) in 28 states, plus `fly_to`, `swim_to` and `use_phone`. The hint
  renderer emits `expected_action` verbatim as `do: …`, so this was actively teaching a vocabulary
  the schema rejects. Renamed 59 fields; **0 executable states now name a non-existent action**
  (checked programmatically against `schemas.ACTION_TYPES`).
- **Whole capability classes were unflagged.** `drive_to` issues the engine's GROUND driving task
  (`Task.DriveTo` with road-node pathing), so it cannot pilot anything: every `kind: "fly"` state
  (34) is now `executable: false`, as are the 6 swim states and the phone one. **67 of 557 states
  (12%) are now honestly flagged as beyond his capability**, with the reason, instead of issuing
  instructions that silently do nothing.
- **"Never initiate combat with police" was stated absolutely** in both `rules.md` and
  `action_catalog.md`, and eight scripted missions require exactly that (Prologue, Blitz Play, The
  Paleto Score, The Bureau Raid, The Third Way...). Now scoped: free roam never, scripted police
  assault yes, with `mission.active` named as the flag that decides. The catalog also now warns
  that area combat cannot choose its target, so when a crewmate or hostage is inside the radius
  there is no right action at all.

Still open, recorded rather than quietly dropped: some Caps-Lock / weapon-key policy remains in
domains other than `combat` and `controls_interactions`; `police_system` carries several
mutually-exclusive standing instructions about accepting arrest; and `mission_state_hint` only ever
surfaces two states per mission (the first, and the first no-marker one), so most per-state work is
latent until state tracking gets better. None of these can loop him or make him shoot a friendly.

### Blocked on the operator (physical-world only)

1. The Windows Administrator account is locked out (0xd07) **because of my SSH retry storm**. It
   needs the lockout window to expire, or a console/rescue reset. Until then nothing deploys.
2. The Anthropic API key is disabled by the operator, so the agent's brain is stopped.
3. Once both are back: raise the lockout threshold so a retry can never lock the operator out
   again, deploy the harness package + bridge DLL, switch OBS to window capture, relaunch the game,
   and run the **20-minute behavioural sample that has still never completed**.

## 2026-09-02 — harness/bridge: full fix set built and unit-verified, live verification PENDING

- Working tree (uncommitted): 349 tests pass, ruff clean, `--prompt-audit` passes (tactical prefix
  14,484 tok, director 15,144 tok; warm tactical call ≈ $0.0026).
- Contents since the last server deploy: typed `action.params` (v1.4 — the "F F F F" root cause, verified
  against the real API and in live decisions), combat latch, think-time slow-motion removed + timescale
  guard, unstick rate-limit, backoff fix, mission_start vision wiring, mission knowledge base (72 missions,
  Opus-QA'd), radar legend + mechanics briefs, `friendly` crew + entity `pos` (v1.5/1.6), `player.protagonist`
  + `mission.starts[]` (v1.7, bridge built sha a9d50b03, staged, needs a game restart), day planner.
- **Server state right now:** an OLDER harness (params/combat/timescale fixes) is deployed; the newest
  package (206 KB) and the v1.7 bridge are staged but NOT swapped in; sshd is throttling connections
  (see RUNBOOK ops notes). VB-CABLE installed (Rockstar launcher requires an active playback endpoint).
- **Not yet proven live (CLAUDE.md rules 2/3):** the 20-minute unattended behavioural sample (drives,
  fights, follows crew, holds timescale 1.0, no stalls) and the watchability review. Do not read the
  test count as evidence that he plays well; only the live sample is.

## 2026-09-02 — web: production verified, domain live, cost display removed

- **Production deploy** aliased to the project's production domain
  (domain attached to the project, certificate issued for the apex + wildcard, `www` 307→apex).
- **Verified against the live production URL:** Playwright **42 passed, 2 skipped** (the skips are "no clip
  rows exist yet" — honest), served HTML contains **0** cost strings (`Brain bill`, `cost_per_hour_usd`,
  `per hour`), `/missions` has no Tokens column. `npm run lint` / `typecheck` / `build` clean.
- **Cost display removed** by operator request: `BrainBill.tsx` deleted, the `/agent` cost section and
  "cost transparency" wording removed, and the spend columns are no longer *fetched* (explicit column
  lists in `web/src/lib/columns.ts`; previously `select("*")` shipped them in the RSC payload).
  `docs/STATUS.md` remains the place where measured $/hour is recorded (CLAUDE.md §9).
- **Decision-feed freshness — root cause fixed:** Supabase Realtime was correctly enabled; the bug was a
  socket that reports `SUBSCRIBED` then delivers nothing and never re-fires, with only `stats` having a
  fallback poll. Now: 15 s incremental poll for `decisions`/`events`, refetch on `visibilitychange` /
  `focus` / `online`, coalesced so realtime + poll never double-post, and `stats` listens to `*` (a new
  session's first heartbeat is an INSERT and was being missed). End-to-end realtime delivery through the
  new code is still to be observed on the next live harness run.
- **Online/offline timing:** threshold stays 60 s (CONTRACTS §5); age text ticks every 1 s and resyncs
  on tab return; stats poll 10 s so ON/OFF flips within 10 s of a heartbeat resuming. Boundary proven
  against the real heartbeat row: on-air at +48 s, off-air at +62 s.
- Vercel preview URLs are behind Deployment Protection with no automation-bypass secret, so previews
  cannot be tested from outside; verification is done against the public production URL after promote.

## Delivery-day readiness pass — 2026-08-29 (commit `71140c0`)

Server ordered (Hetzner auction i5-12500). While
waiting, a full readiness pass ran: 2 recon agents + 4 hardening agents + 4 verifiers + 4 fixers
+ 1 confirmer. **Confirmer verdict: SAFE TO COMMIT, 15/15 checks PASS**, each falsification-tested.

**The machine changed under us** — we bought an Intel-iGPU auction box, not the planned
NVIDIA GEX44, so scripts/ were pointed at hardware we do not own. Retargeted; new findings in
docs/RESEARCH.md §7b (D11–D15) and the delivery-day sequence in docs/RUNBOOK.md §0.

**Defects caught before delivery day (all fixed + confirmed):**
- 🔴 The harness would have **crashed on its first poll of every session** — the bridge legitimately
  emits `last_task.id: null` before any task; the pydantic model required `str`. Nobody had ever
  validated the bridge's real output against the harness's model. Fixed, contract clarified
  (v1.2), and a **cross-package conformance test** now validates real bridge serializations
  against the harness models (proven to reject renamed/dropped/retyped fields).
- 🔴 `server-setup.ps1` would have downloaded the VDD **audio** driver instead of the display
  driver (ambiguous asset pattern + `Select-Object -First 1`) — and that audio driver is rejected
  on Server 2025 (Code 52). Ambiguous patterns now fail loudly.
- 🟠 Operator-skipped bootstrap phases were recorded as complete; nefcon's reboot-required exit
  code (3010) was treated as failure; the OBS profile was never selected by `run.ps1`.
- 🟠 Governor L3 issued `stop` wherever the agent was (possibly mid-freeway) instead of parking
  somewhere scenic; activity task-tracking raced against the 3 Hz poll; `stunt`/`mission_end`/
  `mission_fail` were wired but never emitted (now honestly declared as Phase 3/4 gaps).
- 🟠 web: per-route `og:title`/`og:url`/`canonical` were wrong (a regression introduced and fixed
  within the same pass); test runs poisoned `.next` and `tsconfig.json`.

**Verified locally after fixes:** harness **151 tests** green (was 42) + ruff clean; bridge Release
build 0 warnings/0 errors + 185 offline checks; 10 PowerShell files 0 parse + 0 analyzer errors;
web build/tsc/lint clean, 36 Playwright passed. Everything game-adjacent remains BLOCKED on the
undelivered server — nothing about the game is claimed as working.

⚠ **Anthropic key is at its configured spend limit** — verified today: `You have reached your
specified API usage limits. You will regain access on 2026-09-01`. Raise the limit in the console
before the live checks, or the agent cannot think.

## Current phase

**Phase 0 done · wait-work build-out done · CLOUD LAYER LIVE (2026-08-25 evening).** With the
human's credentials, the entire non-game stack is now verified against real services and the site
is publicly live. **Remaining work needs only: the Windows game server, the game purchase, and a
Twitch channel name.**

## Cloud layer — verified against REAL services (2026-08-25, workflow wf_04a3dc63-84d + inline)

- **Site LIVE (production)** — `/api/health` `{"ok":true,
  "supabase_configured":true}`; renders the honest OFFLINE banner *and* the first real decision
  rows server-side. Vercel project `wasted`; env vars set for
  preview+production. ⚠ Production went live via Vercel CLI v53's changed default (plain
  `vercel deploy` now targets production); brand-new project, nothing
  overwritten. Preview URLs are SSO-protected (Vercel Authentication) — disable in Project
  Settings → Deployment Protection if preview access is wanted.
- **Cloud Supabase schema applied + verified** (project wwluuzkboosvtupcexsp, empty pre-apply):
  full RLS suite passed on the production DB (anon read-only ×7, writes denied ×3, service-role
  writes ok, constraint rejections ×2), publication = exactly decisions/events/stats, buckets
  clips+shots public. All verification rows cleaned up. The earlier "Secret API key required"
  gate on publishable keys is **gone** — anon REST reads return 200; anon writes correctly denied.
- **Brain verified vs real Claude API**: both model IDs valid (1-token calls); tactical static
  prefix **7617 tokens** (≥4096 Haiku cache min), director 8532; two real structured decisions via
  `messages.parse` — valid DecisionModels, word limits enforced; **prompt caching proven**
  (call 2: `cache_read_input_tokens=8373`); measured cost **$0.011331 cold / $0.001714 warm** per
  tactical call → ≈ **$0.41/h at full 240-calls/h cadence** (design estimate was $0.37 — confirmed).
  Total verification spend ≈ $0.013.
- **First honest data**: session `297839d7-c0cb-4907-bba1-fed9f85d8140` (harness_version
  `api-verify`) with session_start event, 2 real decisions, stats heartbeat — left in place as the
  project's first rows; the live site renders them.
- **Realtime delivery measured**: postgres_changes INSERT → subscriber in **574–857 ms**
  (Phase 5 bar: < 2 s). First-ever subscription failed silently during replication-slot warm-up —
  the site's refetch-on-SUBSCRIBED covers this; test rows cleaned up.
- `tools/post_event` online path verified (wrote → confirmed → test row deleted).

Phase 5 DoD progress: Realtime latency ✔ measured; mobile ✔ (local Playwright); Playwright against
the deployed URL still open — `web/playwright.config.ts` needs a BASE_URL env override (backlog,
web executor next pass). `NEXT_PUBLIC_SITE_URL` env to set once the final domain is chosen.

## Works / verified locally (real commands, re-run independently by a verifier)

| Package | Verified here (evidence) | Deferred to real env |
|---|---|---|
| infra/ | Migrations apply clean on real Postgres 16 (`infra/verify-local.sh` reproduces): anon SELECT on all 7 tables; anon INSERT/UPDATE/DELETE denied ×3; service_role writes ok (incl. identity sequences); enum CHECKs reject bad values; publication = exactly decisions/events/stats | Apply to cloud project; PostgREST/Realtime/Storage behavior (needs keys) |
| bridge/ | `dotnet build -c Release` clean (0 warn/0 err); SHVDN v3.7.0-nightly.189 pinned + hash-verified; **38/38 real HTTP transport checks** by loading the compiled HttpServer under .NET 8 (state/health shapes, all 11 task types → 202 + `t-` ids, param/unknown-type 400s, unstick 409, online kill-switch 503 on every endpoint) | Everything touching natives: Phase 1 smoke with the game (`scripts/bridge-smoke.ps1`) |
| harness/ | 42/42 pytest green (py 3.13); `--check` honest (exit ≠ 0 listing missing prereqs); post_event queues offline for real; all five writer row shapes proven against the real schema as service_role; pricing.yaml byte-exact to D4 with source URL + date; product-code grep clean | PostgREST flush success path; real Claude API calls (startup model check, prefix ≥4096 check, live decisions); OBS replay pipeline; everything game-adjacent; 20-min live check (Phase 2) |
| web/ | build/tsc/lint clean; **12/12 Playwright** incl. offline banner with unreachable backend, honest empty states, per-clip OG image, 390×844 no horizontal scroll; no fabricated data anywhere | Realtime delivery + real rows; Vercel preview + Playwright there; live Twitch/YouTube embed (needs HTTPS host + channel) |
| scripts/ | PowerShell AST parse 0 errors ×8 files; PSScriptAnalyzer 0 errors; env guards abort loudly on non-server hosts (proven); bridge-smoke honest-FAIL run vs dead port (22 checks, exit 1); live checks of every external URL/API the scripts rely on (dev-c Referer gating, SHVDN release asset, winget IDs, VB-CABLE, Autologon) | Real execution on Windows Server 2025 (Phase 0a) |

Commits: `50d1cd9` (bridge), `ecc39ff` (harness), `0e02430` (web), `64ce38d` (scripts),
`22a3a53`/`a1aa924` (infra + contracts v1.1). Full executor/verifier reports: workflow
wf_29b91fcf-c97 journal (session transcript dir).

Notable implementation decisions accepted from executor reports (contract-conforming):
governor L1/L2/L3 at 70/90/100% of the configurable hourly cap; director screenshots allowed only
on §4 screenshot-bearing events; §2 word limits via pydantic validators (retry once → reflex keeps
control); watchdog posts only `bridge_down` (harness owns `bridge_up`/downtime accounting);
`stop` → `last_task.status: idle`; online-session latch is permanent until restart;
supabase-js pinned 2.109.0 until Node ≥22 baseline; drive/walk arrival = planar (XY) distance.

## Broken / known gaps

- **Nothing further is verifiable on the development machine.** The remaining work requires: the Windows GPU
  server (Phases 0a,1,2,3,4,6,7), an Anthropic API key (brain verification), Supabase keys
  (cloud migration apply + Realtime), Vercel access (Phase 5 previews), a stream channel (0a/6).
- vgamepad/ViGEmBus on Server 2025 untested (known Code-28 risk) — SendInput is the plan of record.
- Curated landmark/stunt coordinates in `behavior/activities.py` need live tuning in Phase 3.
- `-scofflineonly` durability is best-effort (periodic Rockstar revalidation reported).

## Blockers on the human (updated 2026-08-25 evening — keys DONE)

1. **The server** — cheapest plan: hourly cloud GPU (TensorDock/AWS, ~$10 total) to test first,
   then a monthly box (Hetzner EX44-class ~€70/mo or budget GPU host ~€90–130/mo). Human decides
   and provides IP + admin password.
2. **Buy "Grand Theft Auto V Enhanced" on Steam** (app 3240220; includes Legacy 271590 which we run).
3. ~~Keys~~ ✔ DONE — Anthropic + Supabase + Vercel all provided and verified 2026-08-25.
   (Anthropic org spend-cap tier still to confirm before 24/7.)
4. **Twitch channel name** (free) — then it goes into `site_config` and OBS.
5. Later, on the server: one-time Steam + Rockstar logins, offline args, BattlEye off.

## Cost

- Brain (design model, replace with measurement in Phase 2): ≈ **$0.70/streamed hour**
  (tactical Haiku ~240 calls/h ≈ $0.37 + director Sonnet 5 ~40 calls/h ≈ $0.33) — under the
  $1.50 target; ≈ $500/month at 24/7 → needs Build tier.
- Server ≈ €184/mo + ~€28 Windows + €1.10 HDMI emulator + €79 setup. VB-CABLE pro license TBD.
- Supabase Realtime bills per recipient (≈$1,900/mo at 300 viewers × 1 msg/s) — v1 fine at launch
  scale; digest migration path is contracted (CONTRACTS §5 / D6).

## Environment / operations notes (2026-08-25)

- Running the full local Supabase stack (~6 GB of images) is not required to develop here; SQL and
  RLS verification runs via `infra/verify-local.sh` against a throwaway `postgres:16-alpine`
  container, which is both faster and reproducible on any machine.
- Local toolchain is installed user-locally and is removable: .NET SDK 8, PowerShell 7.4.6.
- Game target: GTA V **Legacy** (271590) via Enhanced purchase; SHV 1.0.3889.0/1158.13; SHVDN
  **v3.7.0-nightly.189 pinned** (recorded in bridge/README); .NET Framework 4.8; `-nobattleye`.
- Paste-corruption note: master brief arrived with minor copy damage; reconstructed spots flagged
  in CONTRACTS.md §2 (mood enum — `scared` restored).


## Fun-to-watch program — PHASE 3/4 done, PHASE 5 written (2026-09-03, HEAD)

Five subagents' work integrated in one tree; my follow-ups on top (see `docs/findings.md`,
"Integration findings" I1–I9). Verification, all real output on this machine:

- `cd harness && .venv/bin/python -m pytest -p no:warnings` → **893 passed** (was 728 at the
  start of the program); `ruff check .` → clean.
- `~/.dotnet/dotnet build bridge/WastedBridge.csproj -c Release` → **0 warnings, 0 errors**
  (bridge **1.7.0**); `bridge/tools/offline-checks/run.sh` → 187/187.
- `verify` (read-only whole-tree audit): one movement owner per tick PASS; goal box
  plugin-written PASS; no live brain call from tests/tools PASS; two event-less lines found and
  removed.
- `harness/tools/soak.py` (fake brain, real tick, `docs/FUNCHECK.md`): **F1–F6 all PASS** on 12 min
  seed 3, 12 min seed 7 and 20 min seed 3 — idle 3.6–3.9 %, longest gap 46–51 s, 0 commentary
  offenders, 95–97 % goals completed, 3–5 deaths/h (the one scripted death), 2/2 F6 edges in 3 s.

**NOT VERIFIED — needs the game (listed in `docs/go-live.md`):** every native the five reports
list (drive-start seat/engine/cruise timing, one bridge-side re-issue surviving a story call,
`TASK_SMART_FLEE_PED`/`TASK_DRIVE_BY`/`TASK_SHOOT_AT_ENTITY` on the player ped, phone control group
0 vs 2, `DESTROY_MOBILE_PHONE`, `IS_ENTITY_IN_AIR` on a real ramp, the `ammunation` loadout), F1
on a real `/state` stream (the fake brain cannot echo a task back), and every soak grant the real
game may refuse. **Nothing in this program has been deployed to the box** — the harness there
dies on the 401 (T11) and the operator has not supplied the key.
