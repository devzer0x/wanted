# WANTED — PREDICTION LAYER CONTRACTS v2.0

Frozen. Executors treat this as read-only; changes go through the orchestrator and bump the
version. If code and contract disagree, the contract wins. Companion to `docs/CONTRACTS.md`
(bridge/decision/event/DB contracts v1.14), which is unchanged by this document.

Research backing every external claim is in §8. Nothing here is guessed.

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
| `wanted_reaches` | `level`, … | `wanted_change` payload `to >= level` |
| `wanted_clears` | — | `wanted_change` payload `to = 0` |
| `wanted_gained` | — | any `wanted_change` with `to > from` |
| `survives_window` | — | absence of `death`/`busted` in the window |
| `vehicle_entered` | — | `activity_*` / HUD `vehicle` transition null → non-null |
| `vehicle_exited` | — | HUD `vehicle` transition non-null → null |
| `mission_outcome` | `expect: passed\|failed` | `mission_end` / `mission_fail` |
| `activity_outcome` | `activity`, `expect` | `activity_end` payload `outcome` |

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
| `/api/predictions/[id]/enter` | POST | `{outcome}` → 201, or **409 after `locks_at`** |
| `/api/rewards/balance` | GET | → `{claimable, lifetime, asset}` for the cookie's wallet |
| `/api/rewards/claim` | POST | → creates a claim, submits the transfer, returns `{claimId, status}` |
| `/api/leaderboard` | GET | `?window=today\|week\|all` |
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

Called before any ledger credit and again before any claim. Predicting is always allowed;
**rewarding is not.** If eligibility cannot be confirmed, no reward is issued — the ledger row is
simply not written, and the entry still counts for accuracy, streak and leaderboard.

Policy inputs are config-driven (`POLICY_BLOCKED_REGIONS`, `POLICY_REQUIRE_TERMS`,
`REWARDS_ENABLED`), never hardcoded in UI. UI may *reflect* ineligibility; it may never *decide* it.

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
never silently dropped and never silently exceeded. Caps are checked inside the settlement
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
