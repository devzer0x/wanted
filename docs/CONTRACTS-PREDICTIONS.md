# WANTED — PREDICTION LAYER CONTRACTS v2.5

Frozen. Executors treat this as read-only; changes go through the orchestrator and bump the
version. If code and contract disagree, the contract wins. Companion to `docs/CONTRACTS.md`
(bridge/decision/event/DB contracts v1.14), which is unchanged by this document.

Research backing every external claim is in §8. Nothing here is guessed.

**v2.1** — §5 now states where the credit-side and claim-side eligibility gates each run, and names
the `site_config` key `rewards` as the credit-side master switch (v2.0 named only `REWARDS_ENABLED`,
which cannot reach the credit). §6 records that the two environment rails are baked into a
deployment and do not apply retroactively, and that an unparseable value is now an error rather
than an absent limit. No table, function signature or HTTP shape changed.

**v2.2** — §5: reward eligibility now has real inputs for both conditions it enforces. Rules
acceptance is the SIWE signature itself (the statement names the /rules version), carried in the
signed session cookie and recorded in `wallet_sessions.rules_version`; country comes from Vercel's
`x-vercel-ip-country`. Before this, `POLICY_REQUIRE_TERMS` and `POLICY_BLOCKED_REGIONS` were both set
in production with no caller supplying either signal, so every wallet was ineligible. §5 also:
`rewards_enabled()` requires valid caps. §6: settlement is serialised behind an advisory lock and
reachable by `service_role` only through `settle_due_predictions_serialized()`. Changed HTTP
behaviour: the SIWE statement text; `/api/auth/*` and `/api/rewards/balance` read the country header.

**v2.3** — the payout path is replaced by an outbox (§10), per the 2026-09-14 security ruling.
`POST /api/rewards/claim` only enqueues; one lease-holding cron worker signs. Supersedes every
statement elsewhere in this document about `finalize_reward_claim`, `pending`/`submitted`, or
`sendReward` — where §2/§4/§6 disagree with §10, §10 wins.

**v2.4** — a steady cadence, and time to answer. §3 gains one rule kind, `event_matches`
(`event_occurs` plus a payload filter), which is what makes a fair question askable during ordinary
free roam; it is a NEW kind rather than a new parameter on `event_occurs` so that a harness ahead of
the database fails closed (an unknown kind voids) instead of settling every such question YES. §3
also gains "Cadence and entry windows": an enforced floor on the time a viewer has to enter, and the
rule that an always-available question is only asked while its own measured recent odds are fair.
Migration `20260921000000_event_matches.sql`. No table, column, HTTP shape or payout rule changed.

**v2.5** — four defects found by the 2026-09-22 review, each reproduced before it was fixed. §3: one
candidate whose rule or events raise a data error (SQLSTATE class 22) now voids alone as
`settlement_error` instead of aborting settlement for every due prediction, every tick; and the
harness no longer publishes a row whose entry window closed while it sat in the offline queue. §6:
the `max_per_prediction` clamp is recorded on the ledger row like the other two, and a credit
clamped to exactly zero is stated for what it is (no ledger row). §4: a duplicate entry answers
`already entered this prediction`, and `/api/leaderboard` reads `earned` as text. Migration
`20260922000000_settle_row_isolation.sql`. No table, column, function signature or payout rule
changed.

---

## 0. What this layer is

An autonomous agent plays the game (existing `bridge/` + `harness/`, untouched). Its real
telemetry lands in Supabase (existing `events`, `decisions`, `stats`, `sessions`). This layer
adds: predictions generated from that telemetry, entries from authenticated wallets, deterministic
settlement from the same telemetry, an off-chain reward ledger, and an on-chain claim.

Free to play. No wager, no deposit, no loss. Rewards come from a treasury we fund.

---

## 1. Chain configuration (verified 2026-09-08)

| Key | Mainnet | Testnet |
|---|---|---|
| Chain ID | `4663` (`0x1237`) | `46630` (`0xb626`) |
| RPC | `https://rpc.mainnet.chain.robinhood.com` | `https://rpc.testnet.chain.robinhood.com` |
| Explorer | `https://robinhoodchain.blockscout.com` | `https://explorer.testnet.chain.robinhood.com` |
| Gas | ETH (no native chain token) | ETH |
| Stack | Arbitrum Orbit, EVM-equivalent | same |

Verified by direct `eth_chainId` against both RPCs (returned `0x1237` / `0xb626`) and by
`docs.robinhood.com/chain`. Sources in §8.

### Reward asset — TTWO

- Contract `0x5e81213613b6B86EaB4c6c50d718d34359459786` on chain 4663.
- `symbol()` → `TTWO`, `decimals()` → `18`, `name()` → `Take-Two Interactive Software • Robinhood Token`.
- ERC-20 behind a **beacon proxy** (beacon `0xe10b6f6b275de231345c20d14ab812db62151b00`); the
  implementation is upgradeable by Robinhood. Treat the ABI as ERC-20 only.
- `paused()` exists and returns `false`. **A paused token must fail claims loudly, never silently.**
- `transfer()` to a fresh, never-funded address simulates successfully (`eth_call` → `true`), so
  arbitrary recipient wallets are permitted at the contract level. There is no on-chain allowlist.
- Restrictions are **regulatory, not contract-level**: Stock Tokens are tokenized debt securities
  issued by Robinhood Assets (Jersey) Limited, restricted or prohibited in the US, Canada, UK and
  Switzerland; only KYC'd EU customers may mint or redeem. §5 eligibility exists because of this.

**The reward asset is configurable.** Nothing outside `web/src/lib/chain/assets.ts` and the
`asset` columns may hardcode `TTWO`.

**Units — stated precisely, because an earlier ambiguity here cost a 10^18 error.** Amount columns
are `numeric(38,18)` and hold **whole token units**: a 2.5 TTWO credit is the value
`2.500000000000000000`. Settlement does its division in integer base-unit math and divides back
down before inserting, and the §6 operator caps are denominated the same way (a cap of `25` means
25 tokens). Application code works in **base units as `bigint`**, because that is what ERC-20
`transfer()` takes. The boundary between the two is a decimal shift by the asset's `decimals`
(`web/src/app/api/_lib/amount.ts`), never a reinterpretation of the same digits. Never
JavaScript `number` anywhere: it cannot hold 1e18 without loss.

---

## 2. Database objects (schema `public`)

Additive only. No existing table is altered, renamed or dropped. Existing rows keep working.

```
wallet_sessions      auth: address, nonce, chain_id, issued/expires/verified, hashed UA+IP
predictions          the question, its window, its telemetry rule, its result
prediction_entries   one row per (prediction, wallet)      UNIQUE (prediction_id, wallet)
reward_ledger        one credit per (prediction, wallet)   UNIQUE (prediction_id, wallet)
reward_claims        one on-chain payout of N ledger rows
wallet_streaks       current + best streak per wallet
policy_flags         server-side eligibility, keyed by wallet
```

### `predictions`

| column | type | notes |
|---|---|---|
| `id` | `uuid pk` | |
| `session_id` | `uuid → sessions(id)` | a prediction always belongs to a real session |
| `question` | `text` | display copy, e.g. `WILL WANTED LOSE THE COPS?` |
| `prediction_type` | `text` | key into the rule registry, §3 |
| `state_context` | `jsonb` | HUD snapshot at creation; what made it interesting |
| `created_from_event` | `bigint → events(id)` | nullable; the trigger event |
| `opened_at` `locks_at` `resolves_at` | `timestamptz` | `opened_at < locks_at < resolves_at` (CHECK) |
| `outcomes` | `jsonb` | `[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]` |
| `telemetry_rule` | `jsonb` | machine-readable, §3. **Never free text.** |
| `status` | `prediction_status` | `open → locked → resolving → settled` \| `void` |
| `result` | `text` | an `outcomes[].key`; null until settled |
| `resolution_evidence` | `jsonb` | the event ids/values that decided it |
| `reward_pool` | `numeric(38,18)` | |
| `reward_asset` | `text` | |
| `entry_count` `correct_count` | `integer` | maintained by trigger/settlement, never by a client |
| `is_event` | `boolean` | a highlighted WANTED EVENT (§13 of the brief) |
| `settled_at` | `timestamptz` | when settlement actually ran; not `resolves_at`, because the cron tick may settle late or after a completeness wait |

Status is monotonic; a CHECK-backed trigger rejects any backwards transition. `settled` and `void`
are terminal.

### Idempotency invariants (these are the load-bearing ones)

1. `prediction_entries` — `UNIQUE (prediction_id, wallet)`. One entry per wallet per prediction.
2. `reward_ledger` — `UNIQUE (prediction_id, wallet)`. **A prediction can never credit a wallet
   twice**, even if settlement runs repeatedly. Settlement is therefore safely re-runnable.
3. `reward_claims` — partial `UNIQUE (wallet) WHERE status IN ('pending','submitted')`. One
   in-flight claim per wallet; a replayed claim request cannot double-spend.
4. Ledger rows carry `claim_id`; a claim marks its rows in the same transaction that creates it.
   Claimable balance is `sum(amount) WHERE claim_id IS NULL`.

### RLS

Every new table is `enable row level security` with **no** public policy except:
- `predictions` — public `select` where `status <> 'open'` OR `opened_at <= now()`.
- `prediction_entries` — public `select` of aggregate counts only, via a `security definer` view
  (`prediction_distribution`). Raw entry rows are never publicly readable.
- `wallet_streaks`, and the `leaderboard` view — public `select`.

`reward_ledger`, `reward_claims`, `wallet_sessions`, `policy_flags`: **no public policy at all.**
Reachable only with the service-role key, server-side. The browser never reads them directly; it
reads its own balance through an authenticated route that scopes by the session cookie.

---

## 3. Telemetry rules — the settlement registry

A rule is a JSON object with a `kind`. The settlement function understands exactly these kinds and
**voids anything it does not recognise**. Every rule resolves purely from rows in `events` and
`stats` inside `[locks_at, resolves_at]` — never from client input, never from a model.

| kind | params | settles from |
|---|---|---|
| `event_occurs` | `event_type`, `outcome_if_true`, `outcome_if_false` | an `events` row of that type in the window |
| `event_matches` | `event_type`, `payload_match` (non-empty JSON object), `outcome_if_true`, `outcome_if_false` | an `events` row of that type in the window whose `payload` contains every key/value of `payload_match` (`payload @> payload_match`). Absence is a definite `outcome_if_false`, exactly as for `event_occurs`. A blank `event_type`, or a `payload_match` that is missing, not an object, or `{}`, voids `malformed_rule` — an empty filter would be `event_occurs` under another name |
| `wanted_reaches` | `level`, … | `wanted_change` payload `to >= level` |
| `wanted_clears` | — | `wanted_change` payload `to = 0` |
| `wanted_gained` | — | any `wanted_change` with `to > from` |
| `survives_window` | — | absence of `death`/`busted` in the window |
| `vehicle_entered` | — | `activity_*` / HUD `vehicle` transition null → non-null |
| `vehicle_exited` | — | HUD `vehicle` transition non-null → null |
| `mission_outcome` | `expect: passed\|failed` | `mission_end` / `mission_fail` |
| `activity_outcome` | `activity`, `expect` | `activity_end` payload `outcome` |

### Cadence and entry windows (v2.4)

The shape of a rule, in full, because the harness writes it and SQL reads it:

```json
{"kind": "event_matches",
 "params": {"event_type": "activity_end", "payload_match": {"outcome": "completed"}},
 "outcome_if_true": "yes", "outcome_if_false": "no"}
```

- **Entry window floor.** `locks_at - opened_at` is never less than **30 s** for any prediction, and
  is **60 s** for a scheduled round (below). Enforced where the window is constructed
  (`harness/wasted_harness/predictions/catalog.py`), not by convention. Measured need: the page polls
  every 8 s and the broadcast runs seconds behind the game, so the 10–20 s the first catalogue
  allowed was mostly gone before a viewer saw the card. `opened_at < locks_at < resolves_at` and the
  300 s ceiling on the whole window are unchanged.
- **Scheduled rounds.** While the agent is live and nothing situational has been asked for
  `round_interval_s` (default 300 s), the generator asks one *always-available* question: 60 s to
  enter, then a 180 s window. Situational questions (a wanted star, a fight, a mission) still fire
  on their own triggers and count as that interval's question.
- **An always-available question must earn its place on every ask.** Its YES-rate is re-measured by
  the harness from the agent's own recent telemetry (the same `events` rows settlement will read),
  over the same window shape, and it is offered only while that rate is inside **[0.20, 0.80]** on
  at least **12** sampled windows. Measured 2026-09-20: "pulls off a goal within 3 minutes" was 8 %
  in the 2026-09-04 recording and 50 % in that day's live session — a fixed base rate would have
  been a giveaway in one of them. When nothing is in band, nothing is asked; an empty card is
  honest and a foregone conclusion is not.
- **Nobody enters, nobody wins** is unchanged and already structural: `entry_count = 0` voids as
  `no_entries` before any credit code is reachable ("Voiding is mandatory", below).
- **A row that missed its own entry window is not published** (v2.5). The window is fixed when the
  harness generates the row, and the row can then wait in the writer's buffer or on-disk offline
  queue through a Supabase outage. At the moment of insertion, a `predictions` row with less than
  `MIN_ENTRY_REMAINING_S` (10 s — the page polls every 8 s) left before `locks_at` is dropped, logged
  once, and neither requeued nor counted as a write failure (`harness/wasted_harness/events.py`).
  Before this, such a row was inserted `open` with `locks_at` already past: a card nobody could
  enter, which then voided `no_entries`.

### Settlement preconditions — telemetry completeness

The harness has an **offline queue** (`harness/wasted_harness/events.py`): when Supabase is
unreachable, event rows are held on disk and flushed later, carrying their ORIGINAL `ts`. A row
belonging to the window can therefore land in the table *after* `resolves_at` has passed.

This creates a settlement hazard that ordinary "read the events in the window" logic gets wrong:
settle too early, see no `death` row, credit everyone who said "survives" — and then the real
`death` row arrives, timestamped inside the window. The evidence would contradict the payout, and
the ledger is already written.

Settlement therefore **must not run on a window until telemetry for that window is demonstrably
complete.** A prediction is eligible to settle only when, for its session, either

- `max(events.ts) >= resolves_at`, or
- `stats.heartbeat_at >= resolves_at`

i.e. we hold at least one signal recorded at or after the end of the window, which means the
writer had caught up past it. Until then the prediction sits in `resolving` and settlement skips
it — skipping is free, and a late settlement is always better than a wrong one.

If neither condition becomes true within `SETTLEMENT_GRACE` (default 10 minutes) after
`resolves_at`, the prediction **voids**. A window we cannot prove we observed is a window we do not
settle.

#### Why there is no heartbeat-gap rule, and why that is safe

An earlier draft of this contract listed "heartbeat gap > 90 s" as a void trigger. It is not
implementable and has been withdrawn: `stats.heartbeat_at` is upserted in place, so it carries no
history, and `events.type` is a closed enum with no heartbeat row. A past gap cannot be
reconstructed. Using decision cadence as a proxy was considered and rejected — normal play has
multi-minute gaps between decisions during a single long action (`ACTIVITY_GAP_S` is 150–420 s), so
the proxy would void healthy predictions.

The failure it was meant to catch — the harness dying mid-window, recording no `death`, and
`survives_window` then settling YES on a window nobody observed — is instead caught by the
completeness precondition above, because of how sessions work:

`main.py` assigns `session_id = str(uuid.uuid4())` per run, so a harness restart begins a NEW
session. A prediction is bound to the session that created it. When that harness dies, that
session's telemetry stops permanently: `max(events.ts)` and `heartbeat_at` for that `session_id`
never advance past `resolves_at`, the completeness gate never opens, and the prediction voids once
`SETTLEMENT_GRACE` expires. The production `sessions` table shows this pattern plainly — many rows
with `ended_at` still null, one per crashed run.

The completeness gate must therefore stay **scoped by `session_id`**. Widening it to "any recent
telemetry" would reintroduce the hole by letting a fresh session vouch for a dead one's window.

### Voiding is mandatory, not optional

Settlement **must** produce `void` (and credit nobody) when:

- a `session_end` or `bridge_down` overlaps the window — the agent stopped playing, so no outcome
  is observable. (A heartbeat-gap rule was considered and withdrawn; see below for why, and for why
  the completeness precondition covers the case it was meant to catch.)
- the rule `kind` is unknown to the settling code;
- the window closed but the required telemetry is absent *and* the rule is not an absence rule
  (`survives_window` is the only rule that treats absence as a positive result, and it still
  requires a live session throughout);
- `entry_count = 0` (nothing to settle).
- processing the candidate raises a **data error** (SQLSTATE class 22 — e.g. a numeric param that is
  not a number): that candidate alone voids with evidence exactly
  `{"void_reason": "settlement_error", "sqlstate": "<code>"}`, and the other due predictions in the
  same call settle normally (v2.5). The error MESSAGE is never written, because
  `resolution_evidence` is public and the message quotes the offending input (§10.1 principle 7);
  the operator's log gets a WARNING with the id and code. Every other error class (serialization,
  deadlock, cancellation, lock timeout, resources, the status guard) still aborts the call and is
  retried on the next tick — voiding on a transient failure would cancel a winnable round for good.
  The harness also refuses to construct a template whose numeric params are not numbers.

Never infer an outcome. Never let a model decide one. A void returns nothing to nobody and costs
the treasury nothing — it is always the safe branch.

---

## 4. HTTP API (Next.js route handlers, `web/src/app/api`)

All mutations are `POST`. All amounts are computed server-side. No route trusts a client-supplied
wallet, amount, outcome-correctness, or timestamp.

| route | method | body → returns |
|---|---|---|
| `/api/auth/nonce` | POST | `{address}` → `{nonce, message, expiresAt, issuedAt}`; stores an unverified `wallet_sessions` row. **`message` is signed verbatim by the client** |
| `/api/auth/verify` | POST | `{address, signature}` → sets `HttpOnly` session cookie; verifies EIP-4361 |
| `/api/auth/session` | GET | → `{address \| null, eligible}` |
| `/api/auth/logout` | POST | clears cookie |
| `/api/predictions/live` | GET | → open + locked + recently settled |
| `/api/predictions/[id]/enter` | POST | `{outcome}` → 201, or **409 after `locks_at`**; 409 `already entered this prediction` for a wallet's second entry (v2.5: `enter_prediction` returns `false` for both, so the route reads `prediction_entries` to tell them apart) |
| `/api/rewards/balance` | GET | → `{claimable, lifetime, asset}` for the cookie's wallet |
| `/api/rewards/claim` | POST | → creates a claim, submits the transfer, returns `{claimId, status}` |
| `/api/leaderboard` | GET | `?window=today\|week\|all`. `earned` is selected as `earned::text` (v2.5): as a bare JSON number it became a JS double, so an amount under 1e-6 turned into `"5e-7"`, which the base-unit conversion rejects — one tiny credit made the route fail for every viewer |
| `/api/cron/tick` | GET or POST | **secret-gated**; locks due predictions, settles due windows. Vercel Cron always invokes with **GET** (`vercel-cron/1.0`) and cannot be configured otherwise, so both verbs are implemented and gated identically |

### `session_live` uses one threshold, shared with the page

`/api/predictions/live` reports `session_live` from `stats.heartbeat_at`, using the SAME exported
constant as the page's offline banner (`HEARTBEAT_STALE_MS`, 60 s — the figure `docs/CONTRACTS.md`
§5 fixes). It was 90 s here while the banner used 60 s, so between those two figures a viewer saw
"the agent is not on the air" directly above a live prediction card.

It also tolerates up to two minutes of *forward* skew. The harness stamps that timestamp on the game
server and this check runs somewhere else entirely, so a heartbeat a second in the reader's future
is ordinary NTP drift, not a fault — requiring a non-negative age made the site report OFF AIR while
The agent was playing. A heartbeat further ahead than the tolerance is still refused, because that
means a clock is genuinely wrong and trusting it would keep the site claiming the agent is on air
indefinitely.

### What actually drives the lifecycle

A prediction only moves `open → locked → resolving → settled|void` when something calls
`lock_due_predictions()` and `settle_due_predictions()`. Two things do, deliberately:

1. **The harness, while the agent is live — the primary driver.** It already runs a 2–4 Hz loop on
   the game machine and already holds the service-role key, so it can tick every few seconds. That
   matters because windows are as short as 30 s: a prediction resolving at `T` should settle at
   `T`, not up to a minute later.
2. **A Vercel cron every minute — the backstop** (`web/vercel.json`). Its job is the case the
   harness cannot cover: the harness itself died. Predictions left behind by a dead session must
   still reach a terminal state, and they do — the completeness gate never opens for that session,
   so they void once `SETTLEMENT_GRACE` expires. Without a driver that outlives the harness, those
   predictions would sit in `resolving` forever and the audience would never be told.

Note for whoever deploys: Vercel Cron invokes with **GET**, and minute-level schedules require a
Pro plan — on Hobby the schedule is coerced to once a day, which is fine for the backstop role but
would be useless as the primary driver. That is the other reason the harness owns the fast path.

Neither driver decides anything. Both call the same two SQL functions, which are idempotent, so a
double tick from both at once is harmless.

### The SIWE domain is pinned server-side

`domain` and `uri` come from this deployment's configured origin (`_lib/siweDomain.ts`, derived
from `NEXT_PUBLIC_SITE_URL` / `VERCEL_PROJECT_PRODUCTION_URL`), **never from the request's `Host`
header**. Building the message from `Host` and then comparing the parsed domain against that same
header compares attacker-supplied input with itself, which defeats the binding EIP-4361's `domain`
field exists to provide: an attacker could take a nonce issued for a victim's address, collect a
signature on their own origin, and replay it here with a matching `Host`.

For the same reason `/api/auth/nonce` returns the **exact message text** to sign. The client signs
it verbatim rather than assembling its own, so a preview-deployment host, a www/non-www difference
or a trailing newline cannot produce a signature that will never verify. The message is not a
secret, and `/verify` still re-derives it server-side rather than trusting anything sent back.

The nonce is single-use: `/verify` observes that its burn actually updated one row and returns
**409** if another request burned it first. Zero rows updated is not an error from PostgREST, so
without that check two concurrent requests would both be issued a session from one nonce.

### Wallet addresses are stored lower-cased

Every wallet column stores the lower-cased address, enforced by `check (wallet = lower(wallet))` on
`prediction_entries`, `reward_ledger`, `reward_claims`, `wallet_streaks`, `policy_flags` and
`wallet_sessions.address`. `enter_prediction` lower-cases its argument rather than trusting the
caller.

This is not cosmetic. `UNIQUE (prediction_id, wallet)` only means *one entry per address* if there
is exactly one spelling of an address in the table; with the EIP-55 checksummed form stored, two
spellings would be two rows and the constraint would not bind at all.

It also broke claiming outright. `create_reward_claim` looks up `lower(wallet)`; against
checksummed rows it matched nothing and raised `nothing to claim` for every wallet, so credited
rewards were permanently unreachable. The CHECK constraints exist so that this cannot drift back:
a writer that inserts a checksummed address now fails loudly at the database.

EIP-55 checksumming remains a **display** concern (`displayAddress()`), and the checksum is still
validated on input — a mixed-case address whose checksum is wrong is a typo and is rejected rather
than lower-cased into something that looks valid.

### The signed message is built in exactly one place, and canonicalises its inputs

`buildSiweMessage()` normalises the address to EIP-55 and both timestamps to ISO-8601 milliseconds
with `Z`. Both `/nonce` and `/verify` call it, so neither can produce bytes the other cannot
reproduce.

The normalisation is there because its absence was a total sign-in outage. `/nonce` built the
message from `new Date().toISOString()` (`2026-09-08T18:43:13.595Z`) and stored the instant in a
`timestamptz`; `/verify` rebuilt the message from the value it read back, which PostgREST
serialises as `2026-09-08T18:43:13.595+00:00`. Same instant, different string, different signing
hash — every signature recovered a stranger's address and every sign-in failed with
`invalid_signature`. Neither half looked wrong on its own, and no unit test caught it, because the
bug lives only in the round trip through the database.

### The SIWE origin refuses to be a loopback address in production

`siweOrigin()` throws if the resolved origin is `localhost`/`127.0.0.1` in a production build,
unless `ALLOW_LOOPBACK_SIWE` is set (which exists only for running a production build locally to
verify it). `getSiteUrl()` falls back to `http://localhost:3000` when neither
`NEXT_PUBLIC_SITE_URL` nor `VERCEL_PROJECT_PRODUCTION_URL` is set — harmless for metadata, dangerous
here, because the domain in this message is what the user reads in their wallet before approving.
A deployment quietly signing `localhost:3000` while the user is on the real site shows them exactly
the mismatch that the pinned domain exists to make meaningful. `/nonce` and `/verify` surface the
refusal as an honest 503 rather than an opaque 500.

### Claim creation is a single SQL function, not a sequence of route steps

`POST /api/rewards/claim` delegates to `public.create_reward_claim(p_wallet, p_min, p_max)` and
finalises through `public.finalize_reward_claim(p_claim_id, p_status, p_tx_hash, p_error)`
(`infra/supabase/migrations/20260908120002_reward_claim_rpc.sql`).

The route reaches Postgres only through PostgREST, which issues one statement per call and cannot
hold a transaction open. Creating the claim row and marking the ledger rows as belonging to it must
commit together; as two statements they cannot, and a crash between them strands a `pending` claim
that owns no ledger rows while the balance stays claimable.

`create_reward_claim` also **sums the amount itself**, under a row lock on the wallet's unclaimed
credits. The route never computes or passes an amount: a client-supplied ledger id list is a
client-supplied amount by another name, and even a server-computed one can go stale between the
read and the write.

`finalize_reward_claim` returns the credits to the claimable pool in the same transaction that
marks a claim `failed`, so a failed transfer never burns a viewer's rewards.

### The lock check is server-side and uses server time

`/enter` compares `now()` **in Postgres** against `locks_at`, inside the inserting statement:

```sql
insert into prediction_entries (prediction_id, wallet, outcome)
select $1, $2, $3
from predictions p
where p.id = $1 and p.status = 'open' and now() < p.locks_at
```

Zero rows inserted ⇒ 409. A client clock, a paused tab, or a replayed request cannot beat the lock.

---

## 5. Eligibility (policy hooks)

Because the reward asset is a tokenized security, eligibility is **server-side and separate from
prediction logic**. `web/src/lib/policy/` exports one function:

```ts
assessEligibility(ctx): Promise<{ eligible: boolean; reasons: PolicyReason[] }>
```

Predicting is always allowed; **rewarding is not.** If eligibility cannot be confirmed, no reward
is issued — the ledger row is simply not written, and the entry still counts for accuracy, streak
and leaderboard.

Policy inputs are config-driven (`POLICY_BLOCKED_REGIONS`, `POLICY_REQUIRE_TERMS`,
`REWARDS_ENABLED`), never hardcoded in UI. UI may *reflect* ineligibility; it may never *decide* it.

**"Before any ledger credit and again before any claim" is enforced in two different places,
because the two moments run on two different machines.** This clause used to name only
`assessEligibility()`, which was reachable in one of them:

| Moment | Runs in | Gate |
|---|---|---|
| Credit — a `reward_ledger` row is written | Postgres, inside `settle_due_predictions()` | `public.rewards_enabled()`, read by a `before insert` trigger on `reward_ledger`; plus the existing per-wallet `policy_flags.blocked` check |
| Claim — credits are converted to an on-chain transfer | Node, in `/api/rewards/claim` | `assessEligibility()`, which reads `REWARDS_ENABLED` and the policy env |

A Postgres function cannot read a Vercel environment variable, so `REWARDS_ENABLED` never gated
the credit and never could; it gated only the claim. The credit-side switch therefore lives in
`site_config` under the key `rewards`:

```sql
insert into public.site_config (key, value) values ('rewards', '{"enabled": true}')
on conflict (key) do update set value = excluded.value;
```

It **defaults to off when the row is absent** — unlike the §6 caps, whose absent-row fallbacks are
permissive placeholders. An absent tuning knob may guess; an absent kill switch may not.

**It is also off unless the caps are real** (v2.2, `20260914000001`). `rewards_enabled()` returns
true only when `rewards.enabled` is the JSON boolean `true` AND `reward_caps` holds `daily_cap`,
`max_per_prediction` and `max_per_wallet_day` as positive JSON numbers. A missing, misspelled,
string-typed or zero cap therefore stops credits instead of falling through to settlement's
1,000,000/day placeholder, and a non-boolean switch value reads as off rather than raising (the v2.1
function raised on `"maybe"`, which would have aborted every settlement).

**The two eligibility signals, and where each comes from** (v2.2):

| Condition | Enabled by | Signal | Source |
|---|---|---|---|
| Rules accepted | `POLICY_REQUIRE_TERMS=true` | `rulesVersion >= RULES_VERSION` | The SIWE statement names the version (`web/src/lib/auth/siwe.ts`); `/verify` only accepts a signature over the message it rebuilt with the current version, then writes it into the HMAC-signed session cookie and `wallet_sessions.rules_version` |
| Not in a restricted region | `POLICY_BLOCKED_REGIONS=US,CA,GB,CH` | ISO 3166-1 alpha-2 country | `x-vercel-ip-country`, set by Vercel's edge from the client IP; absent or malformed reads as `region_unconfirmed`, never as allowed |

The signature is the consent record: a wallet cannot hold a session claiming rules vN without having
signed bytes that name vN. Bumping `RULES_VERSION` makes every existing session read as not accepted
until the wallet signs in again — the re-consent a change of rules needs — and never blocks
predicting. The country is IP geolocation, which a VPN defeats; /rules forbids that, and the operator
accepts it as a residual risk.

The switch **drops** a credit, it does not defer one. Settlement never revisits a settled
prediction, so turning rewards on later does not backfill anything suppressed while they were off.
That is the intended meaning of off: no invisible liability accrues against an unfunded treasury.

The switch is `before insert` **only**. `finalize_reward_claim` returns credits to the claimable
pool by setting `claim_id = null` on rows that already exist; if the switch blocked that update, an
operator standing down mid-payout would destroy a viewer's balance — the switch causing the exact
harm it exists to prevent.

---

## 6. Treasury limits (enforced server-side, in SQL where possible)

The five rails live in **two** places, and which is which is not a style choice — it follows from
where each is enforced.

| rail | set in | units | enforced by |
|---|---|---|---|
| `daily_cap` | `site_config` key `reward_caps` | whole tokens | `settle_due_predictions()` |
| `max_per_prediction` | `site_config` key `reward_caps` | whole tokens | `settle_due_predictions()` |
| `max_per_wallet_day` | `site_config` key `reward_caps` | whole tokens | `settle_due_predictions()` |
| `CLAIM_MIN_AMOUNT` | environment | base units | `/api/rewards/claim` |
| `CLAIM_MAX_AMOUNT` | environment | base units | `/api/rewards/claim` |

Settlement is a Postgres function; it cannot read the deployment's environment, so its three caps
must come from a table. The claim path is TypeScript, so its two come from the environment.

**Settlement is serial** (v2.2, `20260914000001`). The caps above are computed with `sum()` over
`reward_ledger` under READ COMMITTED, and `settle_due_predictions()` claims candidates with
`FOR UPDATE SKIP LOCKED` — so two overlapping runs settle different predictions in parallel, neither
sees the other's uncommitted credits, and each can spend the full remaining cap. The caps are only
atomic if one settlement runs at a time. `settle_due_predictions_serialized()` takes the
transaction-scoped advisory lock `7741300101` and returns 0 at once if another run holds it; EXECUTE
on the raw function is revoked from `service_role`, so the cron cannot bypass the lock. Verified with
a genuinely contended lock held by a second backend (`infra/verify-predictions.sh`).

**The two environment rails are baked into a deployment, and setting them changes nothing until the
next one.** Vercel applies environment variables at build time: "Any change you make to environment
variables are not applied to previous deployments, they only apply to new deployments." Adding
`CLAIM_MIN_AMOUNT` to the project after the serving deployment was built leaves that deployment
running with `minClaim = 0` and `maxClaim = null` while the dashboard shows both as set. Setting
either rail is therefore a two-step operation — set it, then redeploy — and the three `site_config`
caps have no such property: they are read per settlement and take effect immediately.

**A rail that is present but unparseable is an error, not an absent rail.** These two are in BASE
units while the three caps above are in whole tokens, so `CLAIM_MAX_AMOUNT=0.002` is the obvious
mistake to make. It used to be caught and turned into `null` — no maximum claim at all, reported
nowhere. `/api/rewards/claim` now refuses the claim and says why. Leaving a variable unset is still
a valid configuration and still means "no limit"; only a value nobody can read is fatal.

```sql
insert into public.site_config (key, value) values
  ('reward_caps', '{"daily_cap": 250, "max_per_prediction": 25, "max_per_wallet_day": 10}')
on conflict (key) do update set value = excluded.value;
```

**There is exactly one place to set each rail, deliberately.** An earlier version also parsed
`REWARDS_DAILY_CAP`, `REWARDS_MAX_PER_PREDICTION` and `REWARDS_MAX_PER_WALLET_DAY` from the
environment into `REWARD_LIMITS`, where nothing ever read them. An operator who set a daily treasury
cap would have been told nothing and capped nothing — the worst available failure mode for a
spending limit — so that half was deleted rather than wired up, because the enforcement point can
only ever be the SQL.

A credit that would breach a cap is **clamped, and the clamp is recorded on the ledger row**; it is
never silently exceeded. All three caps record themselves (v2.5 — `max_per_prediction` did not):
`clamp_reason` is `max_per_prediction`, `daily_cap` or `wallet_day_cap`, joined with `+` when more
than one applied. A pool EQUAL to `max_per_prediction` is not reduced and is not marked.

**A credit clamped to exactly zero writes no ledger row** (`reward_ledger.amount` is `CHECK (> 0)`).
That is the one case with no clamp record, and it is not a lost fact: the entry row and the
prediction's `result` are the record that the wallet was right, and the card says "Correct — no
reward was credited". It happens once a UTC day's `daily_cap` or a wallet's `max_per_wallet_day` is
used up. Caps are checked inside the settlement
transaction against `sum()` over the same table — not in application memory, which cannot be
atomic across concurrent settlements.

The treasury private key lives only in the server environment (`TREASURY_PRIVATE_KEY`). It is
never imported by any file under a `"use client"` boundary, never inlined into a
`NEXT_PUBLIC_*` var, and never logged. A build that can reach it from client code is a failed build.

---

## 7. Reward formula

Configurable strategy, default `even_split`:

```
reward_per_correct_wallet = floor(reward_pool / correct_count)   # in base units, integer math
```

Remainder dust stays with the treasury. `correct_count = 0` ⇒ nothing is distributed, and the
prediction settles normally (the pool is simply not spent). Strategies live in
`web/src/lib/rewards/strategies.ts`; adding one must not require touching settlement SQL.

---

## 8. Sources (fetched 2026-09-08)

- Robinhood Chain network details, chain IDs, RPC, explorer — `https://docs.robinhood.com/chain/`,
  cross-checked against a live `eth_chainId` on both RPCs.
- Stock Tokens are standard ERC-20s, 18 decimals, issued by Robinhood Assets (Jersey) Limited;
  jurisdictional restrictions — `https://docs.robinhood.com/chain/stock-tokens/`.
- TTWO deployment address and decimals — `https://api.robinhood.com/rhj/assets` (public, unauth),
  re-verified on-chain with `symbol()`, `name()`, `decimals()`, `totalSupply()`.
- Transferability to a non-whitelisted address — `eth_call` simulation of `transfer()` from a
  holder with balance to `0x1111…1111`, returned `true`.
- EIP-4361 (Sign-In with Ethereum) for wallet authentication.


---

## 10. Payout outbox (v2.3) — FROZEN INTERFACE

Source: the 2026-09-14 security ruling (Opus audit FM-01..FM-18, Fable design). Three executors
build against this section in parallel — `infra/` (SQL), `web/src/` (runtime), `web/scripts/`
(custody + verifiers). Nothing here may be changed by an executor; a needed change comes back to the
orchestrator.

### 10.1 Principles (each is tested)

1. **One signer.** Only `web/src/lib/rewards/payoutWorker.ts`, run by `GET /api/cron/tick` while
   holding the payout lease, signs. It is the only importer of `web/src/lib/rewards/treasury.ts`. No
   request path signs.
2. **Persist before broadcast.** The fully signed raw tx, its hash (keccak256 of the raw bytes,
   computed locally), nonce and every fee field are committed to `reward_claims` BEFORE any network
   call carries them. Only persisted bytes are ever broadcast, and they may be re-broadcast forever.
3. **Nonces from the database**, strictly increasing, one in flight at a time. Never
   `eth_getTransactionCount('pending')`, never viem's nonceManager.
4. **Credits return to the viewer only with proof**, in exactly four cases: (a) a status-0 receipt
   for a hash recorded on the claim; (b) a receipt for a recorded `cancel` override; (c) an operator
   resolving `needs_review` to `failed`; (d) cancelling a `queued` claim that never had bytes signed.
   NEVER from a broadcast error, a timeout, an aborted PostgREST call, a missing receipt or a timer.
5. **An unrecorded use of the key halts everything.** A nonce consumed by a tx the system did not
   record sets the treasury `halted`; only an operator, in SQL, resumes.
6. **Fail closed.** Payouts are off unless `site_config.rewards.payouts` is the JSON boolean `true`.
7. **No secret text leaves the server.** Routes return no `detail` carrying Postgres/viem/noble
   text; `reward_claims.error` holds only a code matching `^[a-z_]{1,48}$`.

### 10.2 Schema (migration `infra/supabase/migrations/20260914100000_payout_outbox.sql`)

`reward_claims` — status becomes one of `queued | signed | broadcast | confirmed | failed |
needs_review`; default `queued`. Existing `pending` rows → `queued`, `submitted` → `broadcast`
(production has zero rows as of 2026-09-14). Added columns:

| column | type | meaning |
|---|---|---|
| nonce | bigint | treasury nonce; unique where not null |
| raw_tx | text | signed EIP-1559 tx, `0x` lowercase hex |
| tx_hash | text (exists) | keccak256(raw_tx), `0x` + 64 lowercase hex |
| gas_limit | bigint | |
| max_fee_per_gas | numeric(78,0) | wei |
| max_priority_fee_per_gas | numeric(78,0) | wei |
| chain_id | integer | must be 4663 in production |
| token_address | text | lowercase |
| to_address | text | lowercase; must equal `wallet` |
| amount_base | numeric(78,0) | must equal `amount * 10^decimals` |
| signer_address | text | lowercase treasury address |
| signed_at, broadcast_at, confirmed_at | timestamptz | |
| block_number | bigint | set on confirmed |
| receipt_status | smallint | 1 on confirmed, 0 on receipt_reverted |
| override_tx_hash | text | an operator-recorded replacement/cancel tx |
| override_kind | text | `pay` or `cancel` |
| attempts | integer not null default 0 | broadcast/reconcile attempts |
| stuck_since | timestamptz | set when signed/broadcast > 30 min with no receipt |
| proof | text | on failed: `receipt_reverted`, `cancel_receipt`, `operator_review`, `never_signed` |
| error (exists) | text | last error CODE only (principle 7) |

Indexes: one in-flight claim per wallet — partial unique `(wallet) where status in ('queued',
'signed','broadcast','needs_review')` (replaces the old index); unique `(nonce) where nonce is not
null`.

New tables (RLS on, no policies, ALL revoked from anon/authenticated):
- `treasury_accounts(address text pk lowercase, next_nonce bigint not null >= 0, halted boolean not
  null default false, halt_reason text, updated_at timestamptz)`
- `payout_lease(id integer pk check (id = 1), holder uuid, expires_at timestamptz)` with its
  singleton row inserted by the migration.

**Status guard trigger** `reward_claims_guard_status_transition` (BEFORE INSERT OR UPDATE) enforces:
- INSERT: status `queued`, nonce/raw_tx/tx_hash null.
- Allowed edges: `queued→queued` (nonce assign or operator roll-back; raw_tx stays null),
  `queued→signed`, `queued→failed` (proof `never_signed`, raw_tx null), `signed→signed`,
  `signed→broadcast`, `signed→confirmed`, `signed→failed`, `signed→needs_review`,
  `broadcast→broadcast`, `broadcast→confirmed`, `broadcast→failed`, `broadcast→needs_review`,
  `needs_review→needs_review`, `needs_review→confirmed`, `needs_review→failed`. Everything else raises.
- `confirmed` and `failed` rows are immutable (any UPDATE raises).
- Entering `signed`/`broadcast`/`confirmed` requires non-null nonce, raw_tx, tx_hash, gas_limit,
  max_fee_per_gas, max_priority_fee_per_gas, chain_id, token_address, to_address, amount_base,
  signer_address, and `to_address = wallet`.
- Once raw_tx is set, none of nonce, raw_tx, tx_hash, amount, amount_base, wallet, asset, to_address,
  token_address, chain_id may change.
- `confirmed` requires block_number and receipt_status = 1. `failed` requires a valid `proof`;
  `never_signed` requires raw_tx null.

**Ledger guard trigger** on `reward_ledger` (BEFORE UPDATE OF claim_id): `null→X` only when
`current_setting('wanted.attach_claim', true) = X::text` (set by `create_reward_claim`); `X→null` only
when `current_setting('wanted.release_claim', true) = X::text` (set by `fail_claim`,
`resolve_claim_review`, `cancel_queued_claim`, `assign_claim_nonce`'s blocked-wallet cancel); `X→Y`
never. Settings are transaction-local (`set_config(..., true)`).

`finalize_reward_claim` and the 3-argument `create_reward_claim` are DROPPED.

### 10.3 SQL functions

All `security definer`, `set search_path = public`, EXECUTE revoked from `public, anon,
authenticated` BY NAME and granted to `service_role`. Custom SQLSTATEs:

| code | meaning |
|---|---|
| P0002 | nothing to claim |
| P0003 | below the minimum claim amount |
| P0004 | not even the oldest credit fits under the maximum |
| P0005 | payouts are paused |
| P0006 | wallet is blocked |
| P0010 | lease not held by this holder (fencing) |
| P0011 | treasury account missing or halted |
| P0012 | another claim is in flight (strict serial) |
| P0013 | precondition / state mismatch |
| P0014 | proof not acceptable |
| 23505 | one in-flight claim per wallet (unique index) |
| 22023 | invalid argument |

**Viewer path (called by `POST /api/rewards/claim`):**

`create_reward_claim(p_wallet text, p_asset text, p_min_amount numeric, p_max_amount numeric)
returns table (id uuid, wallet text, amount_text text, asset text, status text)`
- `p_min_amount`/`p_max_amount` are REQUIRED (whole token units); null → 22023. `p_min_amount > 0`.
- `payouts_enabled()` false → P0005. `policy_flags.blocked` for the wallet → P0006.
- Locks the wallet's unclaimed ledger rows for `p_asset` only (`FOR UPDATE`, ordered by
  `created_at, id`), takes the longest prefix whose running sum ≤ `p_max_amount` (the rest stays
  claimable for a later claim); empty → P0002; oldest row alone > max → P0004; sum < min → P0003.
- Inserts the `queued` claim, sets `wanted.attach_claim`, attaches ledger rows BY ID LIST with
  `RETURNING amount`, and raises P0013 unless the returned sum equals the claim amount (FM-15).
- `amount_text` is `amount::text` — amounts never cross into TypeScript as JSON numbers (FM-08).

`payouts_enabled() returns boolean` — `site_config` key `rewards`, field `payouts`, JSON boolean
`true`; anything else false. Independent of `rewards_enabled()` (crediting): payouts can be paused
while credits accrue, and existing credits can be paid after crediting stops.

**Worker path (every mutating function takes `p_holder uuid` FIRST and raises P0010 unless the lease
row, read `FOR UPDATE`, has `holder = p_holder and expires_at > now()`):**

| function | returns | effect |
|---|---|---|
| `acquire_payout_lease(p_holder uuid, p_ttl_seconds integer)` | boolean | takes the lease if free, expired, or already this holder's |
| `release_payout_lease(p_holder uuid)` | void | clears it only if held by p_holder |
| `payout_snapshot(p_address text)` | jsonb | read-only, no lease: `{account: {next_nonce, halted, halt_reason} \| null, in_flight: [{id, wallet, status, nonce, raw_tx, tx_hash, override_tx_hash, override_kind, signed_at, broadcast_at, stuck_since}] ordered by nonce, needs_review: int, queued: [{id, wallet, asset, amount_text, nonce}] oldest first (queued-with-nonce first), limit 5, liability_text: text}`. `in_flight` = signed/broadcast. `liability_text` = unclaimed ledger + non-terminal claims, whole units |
| `init_treasury_account(p_holder, p_address text, p_nonce bigint)` | bigint | inserts only if absent; returns next_nonce |
| `assign_claim_nonce(p_holder, p_claim_id uuid, p_address text)` | bigint | queued claim; returns its existing nonce if set; else P0011 if account missing/halted, P0012 if any other claim is signed/broadcast/needs_review or queued-with-nonce; if the wallet is now blocked: fails the claim `never_signed`, releases credits, returns NULL; else allocates `next_nonce` and returns it |
| `record_signed_claim(p_holder, p_claim_id, p_nonce bigint, p_raw_tx text, p_tx_hash text, p_gas_limit bigint, p_max_fee_per_gas numeric, p_max_priority_fee_per_gas numeric, p_chain_id integer, p_token_address text, p_to_address text, p_amount_base numeric, p_decimals integer, p_signer_address text)` | void | queued→signed; P0013 unless claim.nonce = p_nonce, p_to_address = wallet, and `p_amount_base = amount * 10^p_decimals` |
| `mark_claim_broadcast(p_holder, p_claim_id)` | void | signed→broadcast or broadcast→broadcast; sets broadcast_at once; attempts+1 |
| `record_claim_attempt(p_holder, p_claim_id, p_error_code text)` | void | attempts+1, error = code (format-checked), stuck_since set when > 30 min since signed_at with no receipt |
| `confirm_claim(p_holder, p_claim_id, p_tx_hash text, p_block_number bigint)` | void | signed/broadcast→confirmed; p_tx_hash must be tx_hash, or override_tx_hash with kind `pay` |
| `fail_claim(p_holder, p_claim_id, p_proof text, p_tx_hash text)` | void | signed/broadcast→failed with release; proof `receipt_reverted` (p_tx_hash is tx_hash or a `pay` override) or `cancel_receipt` (p_tx_hash is the `cancel` override); else P0014 |
| `flag_claim_review(p_holder, p_claim_id, p_reason text)` | void | →needs_review and halts the claim's signer account |
| `halt_treasury(p_holder, p_address text, p_reason text)` | void | halts the account |
| `write_treasury_status(p_holder, p_status jsonb)` | void | upserts `site_config` key `treasury_status` (public: balances, counts, halted — never errors, raw txs or keys) |

**Operator path (service_role / SQL editor; no lease; every call is a deliberate human act):**
`resolve_claim_review(p_claim_id uuid, p_outcome text, p_tx_hash text, p_block_number bigint)`
(`confirmed` needs a hash + block; `failed` sets proof `operator_review` and releases),
`resume_payouts(p_address text)`, `cancel_queued_claim(p_claim_id uuid)` (queued with raw_tx null;
if a nonce was assigned it must equal `next_nonce - 1`, which is rolled back), and
`record_claim_override(p_claim_id uuid, p_tx_hash text, p_kind text)` (once per claim, on
signed/broadcast/needs_review).

### 10.4 TypeScript interfaces

`web/src/lib/rewards/treasury.ts` — `import "server-only"`; imported ONLY by payoutWorker.ts:
```ts
export type TreasuryErrorCode = "treasury_not_configured" | "treasury_key_invalid" | "rpc_unavailable"
  | "wrong_chain" | "token_paused" | "pause_unreadable" | "simulation_failed" | "broadcast_rejected";
export class TreasuryError extends Error { readonly code: TreasuryErrorCode }   // message === code
export function isTreasuryConfigured(): boolean;               // key present AND scalar in [1, n)
export function treasuryAddress(): `0x${string}` | null;       // lowercase
export async function readChainState(token: `0x${string}`): Promise<{ chainId: number;
  latestNonce: bigint; ethBalance: bigint; tokenBalance: bigint; paused: boolean; gasPrice: bigint }>;
export async function readLatestNonce(): Promise<bigint>;       // eth_getTransactionCount(addr,'latest')
export async function simulateTransfer(p: { token: `0x${string}`; to: `0x${string}`;
  amountBase: bigint }): Promise<{ gasEstimate: bigint }>;      // simulateContract must return true
export async function signTransfer(p: { token: `0x${string}`; to: `0x${string}`; amountBase: bigint;
  nonce: bigint; gasLimit: bigint; maxFeePerGas: bigint; maxPriorityFeePerGas: bigint })
  : Promise<{ raw: `0x${string}`; hash: `0x${string}` }>;      // NO network; hash = keccak256(raw)
export async function broadcastRaw(raw: `0x${string}`): Promise<"accepted" | "known">;
export async function getReceipt(hash: `0x${string}`)
  : Promise<{ status: "success" | "reverted"; blockNumber: bigint } | null>;
```
RPC: `TREASURY_RPC_URL` (server-only, optional) else the chain default. Every thrown error is a
`TreasuryError` with a code; no viem/noble message is ever rethrown or logged. Keys with scalar 0 or
≥ the secp256k1 order are rejected before `privateKeyToAccount`.

`web/src/lib/rewards/payoutWorker.ts` — `import "server-only"`:
```ts
export const LEASE_TTL_SECONDS = 120;          // > the cron's maxDuration (60)
export const MAX_FEE_CEILING = 5_000_000_000n; // 5 gwei: above this, wait + report, never pay
export const FEE_FLOOR = 500_000_000n;         // maxFeePerGas = clamp(2 x gasPrice, floor, ceiling)
export const GAS_LIMIT_CEILING = 150_000n;     // gas_limit = min(ceil(estimate * 1.3), ceiling)
export const MAX_PAYOUTS_PER_TICK = 5;
export const RECEIPT_POLL_MS = 20_000;         // in-tick wait for a fresh tx's receipt
export const SAFETY_MARGIN_MS = 8_000;         // stop starting new work with less than this left
export interface PayoutSummary {
  enabled: boolean; reason: string | null;     // why nothing ran: payouts_paused, lease_held,
                                               // treasury_not_configured, halted, ...
  treasury: string | null; halted: boolean; halt_reason: string | null;
  in_flight: number; needs_review: number; stuck: number;
  paid: number; failed: number; queued_remaining: number;
  eth_balance: string | null; token_balance: string | null;   // base units
  liability: string | null;                                    // base units
  runway_days: number | null; chain_nonce: string | null; next_nonce: string | null;
}
export async function runPayoutWorker(admin: SupabaseClient, opts: { budgetMs: number })
  : Promise<PayoutSummary>;
```
Per tick: acquire lease → reconcile `in_flight` in nonce order (receipts for override then tx_hash;
none + chain nonce ≤ claim nonce → re-broadcast persisted raw_tx only if payouts enabled and not
halted; none + chain nonce > claim nonce → re-read receipts once after a short wait, then
`flag_claim_review`) → idle nonce sanity when nothing is in flight (chain 'latest' > next_nonce on
two reads → `halt_treasury('unrecorded_tx_from_treasury')`; < next_nonce → skip signing this tick,
report `chain_nonce_behind`, no halt) → if payouts enabled, not halted, no needs_review, nothing in
flight: up to `MAX_PAYOUTS_PER_TICK` claims, each: preflight (asset symbol = configured, strict
decimals, amount_base from amount_text, chainId 4663, not paused, gasPrice ≤ ceiling, token balance
≥ amount, ETH ≥ 3 × gas_limit × maxFee, simulate) → `assign_claim_nonce` → `signTransfer` →
`record_signed_claim` → `broadcastRaw` → `mark_claim_broadcast` (or `record_claim_attempt` and stop)
→ poll receipt ≤ RECEIPT_POLL_MS → confirm/fail, or leave in flight and stop → write
`treasury_status` → release lease (finally). Payouts paused = nothing new reaches the chain (no
signing, no re-broadcast); reading receipts continues.

### 10.5 HTTP

| route | change |
|---|---|
| `POST /api/rewards/claim` | enqueue only; `maxDuration = 15`; passes country + rulesVersion to policy; `create_reward_claim(wallet, asset.symbol, min, max)`; **202** `{claimId, status: "queued"}`; 23505→409, P0002→400, P0003→400 `{minClaim}`, P0004→409, P0005→503 `payouts are paused`, P0006→403; no `detail` anywhere |
| `GET /api/rewards/claims` | new: the session wallet's last 10 claims `{id, amount (base units string), asset, status, tx_hash, explorer_url, created_at, confirmed_at}` — tx_hash is the override `pay` hash when that is what confirmed; never raw_tx, nonce, error text |
| `GET /api/rewards/balance` | selects `amount::text`; adds `in_flight_claim: {id, status, tx_hash} \| null` |
| `GET /api/cron/tick` | `maxDuration = 60`; after lock + serialized settle, `runPayoutWorker(admin, {budgetMs: 40_000})`; response adds `payouts: PayoutSummary`; a paused/held/unconfigured payout step never fails the tick |

`supabaseAdmin.ts`: one fresh deadline per fetch (default 8 s), not one per client (FM-04).

### 10.6 Rails (decided; env values are base units, site_config values whole tokens)

`reward_caps = {"daily_cap": 0.25, "max_per_prediction": 0.02, "max_per_wallet_day": 0.05}`;
`CLAIM_MIN_AMOUNT=2000000000000000` (0.002), `CLAIM_MAX_AMOUNT=500000000000000000` (0.5), both
REQUIRED — unset is a refusal, not "no limit". Hot wallet ceiling 1.75 TTWO / 0.02 ETH; refill at
0.75 TTWO / 0.005 ETH. `site_config.rewards = {"enabled": bool, "payouts": bool}`.

### 10.7 Custody (procedure names only — never values)

The treasury key is generated by the operator in their own terminal with `cast wallet new
~/.foundry/keystores wanted-treasury` (scrypt keystore v3, hidden passphrase prompt), never through
an AI session. `web/scripts/treasury-push-key.mjs --keystore <path> --expect <address>` decrypts it
in-process (hidden prompt; MAC and address checked) and pipes it to `vercel env add
TREASURY_PRIVATE_KEY production --sensitive` on stdin; nothing prints but the address. The key exists
in exactly two places: Vercel Production (Sensitive) and the encrypted keystore (backed up with its
passphrase as two separate password-manager items). Never Preview/Development, `.env.local`, the
repo, the game server, `harness/.env`, `infra/.env.cloud`, a command line, or a transcript.


### 10.8 Recorded from the build (2026-09-15)

These are facts the build established. They refine §10.1–§10.7; none loosens it.

- **Strict serial is not first-in-first-out.** The database refuses a new nonce while any claim is
  signed, broadcast, in review or queued-with-nonce (P0012). With nothing in flight it lets ANY queued
  claim take the next nonce; the worker's order — queued-with-nonce first, then oldest — is what picks
  it. A test that expected FIFO from the database was wrong and was removed.
- **Every hash-membership guard is `coalesce(..., false)`.** `if not (hash = tx_hash or
  (override_kind = 'pay' and hash = override_tx_hash))` is NULL for a wrong hash when no override
  exists, and `IF NULL` does not fire — so `confirm_claim` and `resolve_claim_review` accepted any
  hash. Fixed in `20260914100000` before it was ever applied, and asserted in
  `infra/verify-predictions.sql` §12. The worker only passes receipt-backed hashes, so no payment was
  exposed; the rule is the database's defence in depth and must not depend on its caller.
- **`treasury_status` is a publication, not a log.** `write_treasury_status` accepts only a JSON
  object under 8 KB whose strings are single-line, at most 128 characters and never 64+ hex digits.
  `service_role` holds SELECT only on `treasury_accounts` and `payout_lease`; every write goes
  through the functions.
- **Custody binding.** `cast` 1.6 keystores record no `address` field, so the keystore cannot vouch
  for itself. The binding is `--expect`: the address `cast wallet new` printed, re-derived
  independently by `cast wallet address --keystore`. `treasury-push-key.mjs` pushes
  `NEXT_PUBLIC_TREASURY_ADDRESS` with `--no-sensitive` (the CLI refuses a `NEXT_PUBLIC_` Secret) and
  both variables with `--force`; stdout is only the address. The Vercel CLI reads a piped value from
  the FIRST stdin chunk or gives up after 500 ms, so the tool writes the key in one chunk.
- **Known gap.** A terminal claim with a recorded `pay` override does not record WHICH of its two
  hashes confirmed it. `GET /api/rewards/claims` shows no receipt link for such a claim rather than
  guess. Fix when it matters: a `confirmed_tx_hash` column set by `confirm_claim`. It can only arise
  after an operator's manual same-nonce replacement (RUNBOOK §5.6).

### 10.9 The 2026-09-15 security review (Sonnet finders, Sonnet skeptics, Fable sign-off)

Fable's ruling: **conditional GO for the first tranche** (0.05 TTWO + 0.005 ETH), after two blocking
fixes; the rest before funding to the ceiling. All of it landed before any migration was applied:

- **B1 (FM-19, critical).** `service_role` held ALL on `reward_claims` and `reward_ledger` (from
  `20260908120001`) and bypasses RLS, so one raw PostgREST INSERT made a `queued` claim with no credit
  behind it, which the worker would pay. Now SELECT only; every write goes through a SECURITY DEFINER
  function. **FS12**: the same for `site_config`, so a leaked service key can no longer rewrite the
  caps or flip the payouts switch. `service_role` now writes none of `reward_claims`, `reward_ledger`,
  `site_config`, `treasury_accounts`, `payout_lease` directly.
- **B2.** `assign_claim_nonce` returned a queued-with-nonce claim's nonce before re-checking
  `policy_flags.blocked`. Order is now: halted (P0011) → another claim in flight (P0012) → blocked
  (via `cancel_queued_claim`, which hands the nonce back) → paused (P0005) → the interrupted-signing
  shortcut → allocate. (The reviewers' proposed fix would have wedged the queue; the status trigger
  requires `nonce is null` for `never_signed`.)
- **FS9.** `payout_snapshot.in_flight[]` carries `signer_address`. A claim signed by a key other than
  the current one is judged by receipts only; with none it goes to `needs_review`
  (`signer_rotated_in_flight`) — the current key's nonce says nothing about the old key's.
- **FS10.** `treasury.ts` gains `getTransactionMeta(hash) → {from, nonce} | null` (an addition to
  §10.4's frozen exports). An override's receipt is honoured only when the tx is from the claim's
  signer at the claim's nonce; otherwise `needs_review` (`override_mismatch`).
- **FS1** the payouts switch is re-read immediately before any re-broadcast. **FS2** the worker's
  budget is `min(40 s, 55 s − time already spent in the request)`. **FS8** the cron secret is compared
  in constant time. **FS5** balance and claim history are scoped to the configured asset. **FS6** a
  blocked wallet reads `wallet_blocked` in the balance, so the panel never offers a Claim that 403s.
- **FS3/FS4** the custody tool pins `vercel@59.10.0`, proves after the push that the key is a
  Production Secret absent from Preview and Development, and wipes held secrets on SIGINT/TERM/HUP.
- **FS7** principle 7 now holds on every route: fourteen `detail: <message>` sites in auth,
  leaderboard and predictions return fixed text and log only the SQLSTATE (`internalError`).
- **FS11** RUNBOOK §5.5 carries the `next_nonce` correction an `unrecorded_tx_from_treasury` halt needs.
- **Refuted with live evidence (H1):** a client-supplied `x-vercel-ip-country` is overwritten by
  Vercel's edge; the header cannot be spoofed on Vercel. It remains IP geolocation (VPNs), as §5 says.
