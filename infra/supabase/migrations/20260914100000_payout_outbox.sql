-- WANTED — the payout outbox. CONTRACTS-PREDICTIONS.md §10 (v2.3), from the 2026-09-14 security
-- ruling on the payout audit (FM-01..FM-18). This file is the database half of that ruling; the
-- signer (web/src/lib/rewards/payoutWorker.ts) is the other half, and neither is safe alone.
--
-- WHAT WAS WRONG WITH THE OLD SHAPE.
--
-- The claim route created a `pending` claim, signed and sent a transfer inside the same HTTP
-- request, then called finalize_reward_claim('submitted' | 'failed'). Every failure the audit found
-- lives in that shape:
--   * a broadcast error was treated as "not paid" and released the credits, although an ambiguous
--     RPC error can follow an accepted transaction — so the viewer could claim, and be paid, twice
--     (FM-02, FM-07);
--   * nothing ever read a receipt, so `submitted` never ended and the one-in-flight index 409'd the
--     wallet for life after its first payout (FM-01, FM-05);
--   * a crash between "sent" and "recorded" left no record of whether money moved (FM-03, FM-04);
--   * nonces came from the RPC's 'pending' view on every instance at once (FM-09);
--   * the amount was summed in one snapshot and the rows attached in another (FM-15), and a claim
--     over the maximum could never be paid at all (FM-14).
--
-- THE NEW SHAPE, AND WHY EACH PART IS HERE RATHER THAN IN TYPESCRIPT.
--
-- The request path only ENQUEUES (create_reward_claim -> `queued`). One worker, run by the cron
-- while it holds a database lease, signs; it persists the signed bytes BEFORE any network call
-- carries them, broadcasts only persisted bytes, and decides every outcome from chain receipts.
-- PostgREST gives the web layer one statement per call and no open transaction, so every rule that
-- must hold across a crash, a lost response or a zombie instance has to be a database fact:
--
--   1. The status guard trigger is the state machine. A code path that tries an edge the design
--      does not have fails here, whoever wrote it and whichever role runs it.
--   2. Credits return to the viewer only with proof (§10.1 principle 4). The ledger guard trigger
--      makes that structural: `reward_ledger.claim_id` can go X -> null only while a transaction-
--      local flag names X AND claim X is already `failed` — and `failed` itself cannot be entered
--      without a proof the status trigger accepts. PostgREST cannot set that flag (it runs each
--      call in its own transaction and exposes no set_config), so from the service-role key the
--      only way to release a credit is to call one of the four functions that are allowed to.
--   3. Nonces come from `treasury_accounts`, allocated strictly one at a time; a unique index makes
--      reuse impossible and assign_claim_nonce refuses while anything else is in flight.
--   4. Every mutating worker call carries the lease holder id as a fencing token and re-reads the
--      lease row FOR UPDATE. A worker whose lease expired (an evicted instance that wakes up late)
--      cannot write a single row, and holding the row lock means the lease cannot change hands
--      mid-call either.
--   5. Payouts are off unless site_config 'rewards'.payouts is the JSON boolean true — one SQL
--      statement stops new claims and new signing within a tick, with no deploy (FM-12).
--
-- DEPLOY ORDER. This drops finalize_reward_claim and the 3-argument create_reward_claim. Between
-- applying it and deploying the route that calls the 4-argument version, the old route's claims
-- fail (PostgREST reports the old function as missing) — fail-closed, which is the right side to
-- fail on, and harmless while payouts are off. It must be applied BEFORE that deployment.
--
-- Legacy rows: `pending` -> `queued`, `submitted` -> `broadcast`, per the contract. Production has
-- zero reward_claims rows as of 2026-09-14. A legacy `broadcast` row has no nonce/raw_tx, so the
-- trigger will never let it reach `confirmed` (confirmed requires the full signed record); an
-- operator can move it to needs_review and resolve it `failed` only. That is deliberate: without
-- the signed bytes there is nothing on the row that proves what was paid.
--
-- Idempotent where Postgres allows (every step is `if not exists`, `or replace`, or drop-if-exists
-- followed by re-create) and wrapped in one transaction, because a half-applied outbox — the old
-- finalize dropped but the new functions absent, or the status CHECK dropped mid-rewrite — is worse
-- than either whole state.

begin;

-- ================================================================================================
-- 1. reward_claims — the outbox columns (§10.2)
-- ================================================================================================

alter table public.reward_claims add column if not exists nonce bigint;
alter table public.reward_claims add column if not exists raw_tx text;
alter table public.reward_claims add column if not exists gas_limit bigint;
alter table public.reward_claims add column if not exists max_fee_per_gas numeric(78, 0);
alter table public.reward_claims add column if not exists max_priority_fee_per_gas numeric(78, 0);
alter table public.reward_claims add column if not exists chain_id integer;
alter table public.reward_claims add column if not exists token_address text;
alter table public.reward_claims add column if not exists to_address text;
alter table public.reward_claims add column if not exists amount_base numeric(78, 0);
alter table public.reward_claims add column if not exists signer_address text;
alter table public.reward_claims add column if not exists signed_at timestamptz;
alter table public.reward_claims add column if not exists broadcast_at timestamptz;
alter table public.reward_claims add column if not exists confirmed_at timestamptz;
alter table public.reward_claims add column if not exists block_number bigint;
alter table public.reward_claims add column if not exists receipt_status smallint;
alter table public.reward_claims add column if not exists override_tx_hash text;
alter table public.reward_claims add column if not exists override_kind text;
alter table public.reward_claims add column if not exists attempts integer not null default 0;
alter table public.reward_claims add column if not exists stuck_since timestamptz;
alter table public.reward_claims add column if not exists proof text;
-- `tx_hash` and `error` already exist. `error` is reused for the last error CODE (principle 7); a
-- separate `last_error` column would have left the old free-text one sitting next to it.

-- ---- status vocabulary -------------------------------------------------------------------------
-- The old CHECK and the old one-in-flight index both name 'pending'/'submitted', so both go before
-- the rows are rewritten and come back afterwards over the new vocabulary. The CHECK was declared
-- inline in 20260908120000, so Postgres named it <table>_<column>_check.
alter table public.reward_claims drop constraint if exists reward_claims_status_check;
drop index if exists public.reward_claims_one_inflight_per_wallet;

update public.reward_claims set status = 'queued', updated_at = now() where status = 'pending';
update public.reward_claims set status = 'broadcast', updated_at = now() where status = 'submitted';

alter table public.reward_claims alter column status set default 'queued';
alter table public.reward_claims add constraint reward_claims_status_check
  check (status in ('queued', 'signed', 'broadcast', 'confirmed', 'failed', 'needs_review'));

-- Idempotency invariant 3, widened. `needs_review` is in the set on purpose: a claim a human has not
-- yet ruled on may still have paid, so the wallet must not be able to queue a second one against
-- the same credits' replacement. The FM-01 fix is not a narrower index — it is that confirmed and
-- failed are reached from receipts, so a claim actually leaves this set.
create unique index reward_claims_one_inflight_per_wallet
  on public.reward_claims (wallet)
  where status in ('queued', 'signed', 'broadcast', 'needs_review');

-- One nonce, one claim, ever. Together with strictly serial allocation this is what makes a
-- nonce gap impossible except as an unmined signed tx the worker re-broadcasts first (FM-09).
create unique index if not exists reward_claims_nonce_key
  on public.reward_claims (nonce)
  where nonce is not null;

-- For the worker's scans (in-flight by nonce, queued oldest first) — the table is tiny today, but
-- the reconciler runs every minute forever.
create index if not exists reward_claims_status_idx on public.reward_claims (status, created_at);

-- ---- row-shape CHECKs ---------------------------------------------------------------------------
-- Formats the trigger would otherwise have to re-derive on every write. Lowercase is enforced, not
-- normalised: every function below lower()s its inputs, so an uppercase value here means a direct
-- write went around them. `raw_tx` is an EIP-1559 typed envelope, so its first byte is 0x02.
alter table public.reward_claims drop constraint if exists reward_claims_hex_formats;
alter table public.reward_claims add constraint reward_claims_hex_formats check (
  (raw_tx is null or raw_tx ~ '^0x02([0-9a-f]{2})+$')
  and (override_tx_hash is null or override_tx_hash ~ '^0x[0-9a-f]{64}$')
  and (token_address is null or token_address ~ '^0x[0-9a-f]{40}$')
  and (to_address is null or to_address ~ '^0x[0-9a-f]{40}$')
  and (signer_address is null or signer_address ~ '^0x[0-9a-f]{40}$')
);
-- tx_hash predates this migration, so its format CHECK is NOT VALID: enforced on every new write,
-- not retro-applied to history. (Legacy rows that carry one are terminal or legacy-broadcast.)
alter table public.reward_claims drop constraint if exists reward_claims_tx_hash_format;
alter table public.reward_claims add constraint reward_claims_tx_hash_format
  check (tx_hash is null or tx_hash ~ '^0x[0-9a-f]{64}$') not valid;
alter table public.reward_claims drop constraint if exists reward_claims_to_is_wallet;
alter table public.reward_claims add constraint reward_claims_to_is_wallet
  check (to_address is null or to_address = wallet);
alter table public.reward_claims drop constraint if exists reward_claims_numeric_ranges;
alter table public.reward_claims add constraint reward_claims_numeric_ranges check (
  (nonce is null or nonce >= 0)
  and (gas_limit is null or gas_limit > 0)
  and (max_fee_per_gas is null or max_fee_per_gas > 0)
  and (max_priority_fee_per_gas is null or max_priority_fee_per_gas >= 0)
  and (max_priority_fee_per_gas is null or max_fee_per_gas is null
       or max_priority_fee_per_gas <= max_fee_per_gas)
  and (chain_id is null or chain_id > 0)
  and (amount_base is null or amount_base > 0)
  and (block_number is null or block_number >= 0)
  and (receipt_status is null or receipt_status in (0, 1))
  and attempts >= 0
);
alter table public.reward_claims drop constraint if exists reward_claims_override_shape;
alter table public.reward_claims add constraint reward_claims_override_shape check (
  (override_tx_hash is null) = (override_kind is null)
  and (override_kind is null or override_kind in ('pay', 'cancel'))
);
alter table public.reward_claims drop constraint if exists reward_claims_proof_kind;
alter table public.reward_claims add constraint reward_claims_proof_kind check (
  proof is null or proof in ('receipt_reverted', 'cancel_receipt', 'operator_review', 'never_signed')
);
-- Principle 7: `error` holds a code, never text. The old route wrote raw viem/Postgres messages
-- here (FM-17), and those carry RPC URLs, calldata and, on one noble path, key material. NOT VALID
-- for the same reason as tx_hash: history is not rewritten, every new write is checked.
alter table public.reward_claims drop constraint if exists reward_claims_error_is_code;
alter table public.reward_claims add constraint reward_claims_error_is_code
  check (error is null or error ~ '^[a-z_]{1,48}$') not valid;

comment on column public.reward_claims.nonce is
  'Treasury nonce, allocated by assign_claim_nonce from treasury_accounts. Unique where not null. '
  'A queued row with a nonce means "signing in progress — reuse this nonce", never re-allocated.';
comment on column public.reward_claims.raw_tx is
  'The fully signed EIP-1559 tx, persisted BEFORE any broadcast. The only bytes ever broadcast for '
  'this claim, re-broadcastable forever.';
comment on column public.reward_claims.proof is
  'Why a failed claim released its credits: receipt_reverted | cancel_receipt | operator_review | '
  'never_signed. There is no fifth reason.';
comment on column public.reward_claims.error is
  'Last error CODE only (^[a-z_]{1,48}$). Never a Postgres, viem or noble message.';

-- ================================================================================================
-- 2. New tables: the treasury account and the lease (§10.2)
-- ================================================================================================

-- One row per signing address. `next_nonce` is the ONLY source of nonces (never
-- eth_getTransactionCount('pending'), never viem's nonceManager). `halted` is the automatic form of
-- the payouts switch: set when the key is seen doing something this table did not authorise, and
-- cleared only by an operator (resume_payouts), never by code.
create table if not exists public.treasury_accounts (
  address text primary key check (address ~ '^0x[0-9a-f]{40}$'),
  next_nonce bigint not null check (next_nonce >= 0),
  halted boolean not null default false,
  halt_reason text,
  updated_at timestamptz not null default now()
);

-- Exactly one row, ever (the CHECK pins the key). A lease rather than an advisory lock because the
-- worker's unit of work spans many PostgREST calls, each its own transaction: a transaction-scoped
-- lock would be gone after the first call, and a session lock on a pooled connection belongs to
-- whoever gets that connection next. TTL 120 s > the cron's maxDuration 60 s, so a killed worker's
-- lease expires before the next one could have finished anyway.
create table if not exists public.payout_lease (
  id integer primary key check (id = 1),
  holder uuid,
  expires_at timestamptz
);
insert into public.payout_lease (id, holder, expires_at) values (1, null, null)
on conflict (id) do nothing;

alter table public.treasury_accounts enable row level security;
alter table public.payout_lease      enable row level security;

-- No policies: RLS default-denies, and the revokes are the privilege-layer half of the same rule.
-- Revoked from anon/authenticated BY NAME (Supabase's default privileges grant tables to them
-- directly, which `from public` would not touch — see 20260909000000).
--
-- service_role keeps SELECT only, not the ALL its default privileges hand out. Both tables are
-- written exclusively by the SECURITY DEFINER functions below. A PostgREST PATCH on payout_lease
-- would defeat the fencing token, and one on treasury_accounts could move next_nonce or clear a
-- halt from code — the two things the design says only the functions, and only an operator, do.
revoke all on public.treasury_accounts, public.payout_lease from public, anon, authenticated;
revoke insert, update, delete, truncate, references, trigger
  on public.treasury_accounts, public.payout_lease from service_role;
grant select on public.treasury_accounts, public.payout_lease to service_role;

-- B1 (FM-19, 2026-09-15 security review): the same narrowing for the two MONEY tables this migration
-- did not create. 20260908120001 granted service_role ALL on reward_claims and reward_ledger, and
-- service_role bypasses RLS, so one raw PostgREST INSERT created a `queued` claim with no credit
-- behind it — which the status trigger accepts (it checks a row's shape, not its backing) and the
-- worker would sign and pay, past CLAIM_MAX and past every cap; a raw ledger INSERT was just as good.
-- Every legitimate write already goes through a SECURITY DEFINER function running as its owner
-- (create_reward_claim, settlement, the worker and operator functions); the web only SELECTs these.
revoke insert, update, delete, truncate, references, trigger
  on public.reward_claims, public.reward_ledger from service_role;
grant select on public.reward_claims, public.reward_ledger to service_role;

-- FS12 (2026-09-15 review): service_role has held ALL on site_config since 20260825120001, so a leaked
-- service key could rewrite reward_caps or flip rewards.payouts — the very switches that bound what
-- that key can drain, which made B1 hardening rather than containment. Nothing in the product writes
-- site_config with the service key: the web and the harness only read it, treasury_status is written
-- by write_treasury_status (SECURITY DEFINER), and the operator changes switches in the SQL editor.
revoke insert, update, delete, truncate, references, trigger on public.site_config from service_role;
grant select on public.site_config to service_role;

-- ================================================================================================
-- 3. The status guard trigger — the state machine (§10.2)
-- ================================================================================================

create or replace function public.reward_claims_guard_status_transition()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  v_edge text;
begin
  if tg_op = 'INSERT' then
    -- A claim is born queued and empty. Anything else is a row written around create_reward_claim.
    if new.status <> 'queued' or new.nonce is not null or new.raw_tx is not null
       or new.tx_hash is not null or new.proof is not null or new.override_tx_hash is not null
       or new.receipt_status is not null or new.block_number is not null then
      raise exception 'reward_claims: a claim is inserted queued, with no nonce, raw_tx, tx_hash or outcome'
        using errcode = 'P0013';
    end if;
    return new;
  end if;

  -- Terminal means terminal. A late callback, a retried RPC or a hand edit cannot reopen money that
  -- has been paid, nor re-attach credits that have been released.
  if old.status in ('confirmed', 'failed') then
    raise exception 'reward_claims: % claims are immutable', old.status using errcode = 'P0013';
  end if;

  v_edge := old.status || '->' || new.status;
  if v_edge not in (
    'queued->queued', 'queued->signed', 'queued->failed',
    'signed->signed', 'signed->broadcast', 'signed->confirmed', 'signed->failed', 'signed->needs_review',
    'broadcast->broadcast', 'broadcast->confirmed', 'broadcast->failed', 'broadcast->needs_review',
    'needs_review->needs_review', 'needs_review->confirmed', 'needs_review->failed'
  ) then
    raise exception 'reward_claims: transition % is not in the state machine', v_edge
      using errcode = 'P0013';
  end if;

  -- What the claim IS never changes: the ledger rows attached to it sum to `amount` (FM-15), and
  -- the wallet and asset are what they were paid against.
  if new.id <> old.id or new.wallet <> old.wallet or new.amount <> old.amount or new.asset <> old.asset then
    raise exception 'reward_claims: id, wallet, amount and asset are fixed at creation' using errcode = 'P0013';
  end if;

  -- A queued row has no bytes. Its nonce may be assigned (null -> N) or rolled back by
  -- cancel_queued_claim (N -> null), never moved from one nonce to another.
  if old.status = 'queued' and new.status in ('queued', 'failed') then
    if new.raw_tx is not null or new.tx_hash is not null then
      raise exception 'reward_claims: a queued claim has no raw_tx or tx_hash' using errcode = 'P0013';
    end if;
    if old.nonce is not null and new.nonce is not null and new.nonce <> old.nonce then
      raise exception 'reward_claims: a queued claim''s nonce is never re-allocated' using errcode = 'P0013';
    end if;
  end if;

  -- Once bytes exist, the row must keep describing exactly those bytes. Everything the signature
  -- covers is frozen; the fee fields and the signer are included because they are encoded in raw_tx
  -- too, so a row that changed them would describe a transaction that was never signed.
  if old.raw_tx is not null and (
       new.nonce is distinct from old.nonce
    or new.raw_tx is distinct from old.raw_tx
    or new.tx_hash is distinct from old.tx_hash
    or new.amount_base is distinct from old.amount_base
    or new.to_address is distinct from old.to_address
    or new.token_address is distinct from old.token_address
    or new.chain_id is distinct from old.chain_id
    or new.gas_limit is distinct from old.gas_limit
    or new.max_fee_per_gas is distinct from old.max_fee_per_gas
    or new.max_priority_fee_per_gas is distinct from old.max_priority_fee_per_gas
    or new.signer_address is distinct from old.signer_address
  ) then
    raise exception 'reward_claims: the signed transaction record is immutable once raw_tx is set'
      using errcode = 'P0013';
  end if;

  -- An override is recorded once; the reconciler's reading of it must not shift underneath it.
  if old.override_tx_hash is not null and (
       new.override_tx_hash is distinct from old.override_tx_hash
    or new.override_kind is distinct from old.override_kind) then
    raise exception 'reward_claims: an override is recorded once per claim' using errcode = 'P0013';
  end if;

  -- Persist-before-broadcast (principle 2) as a row invariant: nothing is signed, broadcast or
  -- confirmed without the complete signed record on the row, paid to the claim's own wallet.
  if new.status in ('signed', 'broadcast', 'confirmed') then
    if new.nonce is null or new.raw_tx is null or new.tx_hash is null or new.gas_limit is null
       or new.max_fee_per_gas is null or new.max_priority_fee_per_gas is null or new.chain_id is null
       or new.token_address is null or new.to_address is null or new.amount_base is null
       or new.signer_address is null then
      raise exception 'reward_claims: % requires the complete signed transaction record', new.status
        using errcode = 'P0013';
    end if;
    if new.to_address <> new.wallet then
      raise exception 'reward_claims: to_address must equal the claim wallet' using errcode = 'P0013';
    end if;
  end if;

  -- Confirmed only from a status-1 receipt, and the block it landed in is kept for a later audit.
  if new.status = 'confirmed' and (new.block_number is null or new.receipt_status is distinct from 1) then
    raise exception 'reward_claims: confirmed requires block_number and receipt_status = 1'
      using errcode = 'P0013';
  end if;

  -- Failed only with a proof, and the proof has to fit the state it came from: a queued claim can
  -- only be "never signed"; a signed/broadcast claim only fails on a receipt; a claim in review only
  -- by an operator's ruling. That mapping is the four release cases of principle 4, nothing more.
  if new.status = 'failed' then
    if new.proof is null then
      raise exception 'reward_claims: failed requires a proof' using errcode = 'P0013';
    end if;
    if (old.status = 'queued' and new.proof <> 'never_signed')
       or (old.status in ('signed', 'broadcast') and new.proof not in ('receipt_reverted', 'cancel_receipt'))
       or (old.status = 'needs_review' and new.proof <> 'operator_review') then
      raise exception 'reward_claims: proof % is not valid from %', new.proof, old.status
        using errcode = 'P0013';
    end if;
    -- never_signed: nothing was ever persisted, and the nonce (if one was assigned) has been
    -- handed back, so the unique nonce index does not keep a dead claim holding it.
    if new.proof = 'never_signed' and (new.raw_tx is not null or new.nonce is not null) then
      raise exception 'reward_claims: never_signed requires raw_tx and nonce to be null'
        using errcode = 'P0013';
    end if;
    if new.proof = 'receipt_reverted' and new.receipt_status is distinct from 0 then
      raise exception 'reward_claims: receipt_reverted requires receipt_status = 0' using errcode = 'P0013';
    end if;
  elsif new.proof is not null then
    raise exception 'reward_claims: only a failed claim carries a proof' using errcode = 'P0013';
  end if;

  if new.status not in ('confirmed', 'failed') and new.receipt_status is not null then
    raise exception 'reward_claims: receipt_status is set only on a terminal claim' using errcode = 'P0013';
  end if;

  return new;
end;
$$;

drop trigger if exists reward_claims_guard_status_transition on public.reward_claims;
create trigger reward_claims_guard_status_transition
  before insert or update on public.reward_claims
  for each row
  execute function public.reward_claims_guard_status_transition();

revoke execute on function public.reward_claims_guard_status_transition() from public, anon, authenticated;

-- ================================================================================================
-- 4. The ledger guard trigger — credits move only through the claim functions (§10.2)
-- ================================================================================================
--
-- claim_id null -> X  only while wanted.attach_claim = X (set by create_reward_claim) and claim X is
--                     queued for the same wallet and asset;
-- claim_id X -> null  only while wanted.release_claim = X (set by fail_claim, resolve_claim_review,
--                     cancel_queued_claim, and assign_claim_nonce's blocked-wallet cancel) and
--                     claim X is already failed — which the status trigger admits only with proof;
-- claim_id X -> Y     never.
--
-- The flags are transaction-local (set_config(..., true)) and each function clears its own after
-- use. The status check makes the flag necessary but not sufficient: even a session that sets the
-- flag by hand cannot release the credits of a claim that is not failed with a valid proof.
--
-- Fires on INSERT and on every UPDATE, not only UPDATE OF claim_id: a credit is born claimable
-- (settlement never inserts one attached), and a ledger row's amount, wallet and asset are fixed —
-- otherwise "the attached rows sum to the claim amount" could be broken without touching claim_id.
create or replace function public.reward_ledger_guard_claim_id()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  v_status text;
  v_wallet text;
  v_asset text;
begin
  if tg_op = 'INSERT' then
    if new.claim_id is not null then
      raise exception 'reward_ledger: a credit is inserted unclaimed' using errcode = 'P0013';
    end if;
    return new;
  end if;

  if new.id <> old.id or new.prediction_id <> old.prediction_id or new.wallet <> old.wallet
     or new.amount <> old.amount or new.asset <> old.asset or new.created_at <> old.created_at then
    raise exception 'reward_ledger: a credit''s prediction, wallet, amount, asset and time are fixed'
      using errcode = 'P0013';
  end if;

  if new.claim_id is not distinct from old.claim_id then
    return new;
  end if;

  if old.claim_id is null then
    if coalesce(current_setting('wanted.attach_claim', true), '') <> new.claim_id::text then
      raise exception 'reward_ledger: credits are attached only by create_reward_claim' using errcode = 'P0013';
    end if;
    select c.status, c.wallet, c.asset into v_status, v_wallet, v_asset
      from public.reward_claims c where c.id = new.claim_id;
    if v_status is distinct from 'queued' or v_wallet <> new.wallet or v_asset <> new.asset then
      raise exception 'reward_ledger: a credit attaches only to a queued claim of its own wallet and asset'
        using errcode = 'P0013';
    end if;
    return new;
  end if;

  if new.claim_id is null then
    if coalesce(current_setting('wanted.release_claim', true), '') <> old.claim_id::text then
      raise exception 'reward_ledger: credits are released only with proof, by the claim functions'
        using errcode = 'P0013';
    end if;
    select c.status into v_status from public.reward_claims c where c.id = old.claim_id;
    if v_status is distinct from 'failed' then
      raise exception 'reward_ledger: only a failed claim releases its credits' using errcode = 'P0013';
    end if;
    return new;
  end if;

  raise exception 'reward_ledger: a credit never moves from one claim to another' using errcode = 'P0013';
end;
$$;

drop trigger if exists reward_ledger_guard_claim_id on public.reward_ledger;
create trigger reward_ledger_guard_claim_id
  before insert or update on public.reward_ledger
  for each row
  execute function public.reward_ledger_guard_claim_id();

revoke execute on function public.reward_ledger_guard_claim_id() from public, anon, authenticated;

-- ================================================================================================
-- 5. Old entry points out
-- ================================================================================================
-- finalize_reward_claim released credits for any pending/submitted claim with no proof (FM-07); a
-- reconciler built on it would double-pay the first time an RPC lagged. The 3-argument
-- create_reward_claim summed and attached in two snapshots (FM-15), ignored the asset (FM-14/16)
-- and had no kill switch (FM-12). Both are replaced, not kept alongside: PostgREST resolves an
-- overloaded name by argument names, and a surviving old signature is a second door.
drop function if exists public.finalize_reward_claim(uuid, text, text, text);
drop function if exists public.create_reward_claim(text, numeric, numeric);

-- ================================================================================================
-- 6. Viewer path (§10.3)
-- ================================================================================================

-- The payout switch. Same fail-closed shape as rewards_enabled() after 20260914000001: absent row,
-- absent key or a non-boolean value (including the STRING "true") all read as off, and the CASE
-- keeps a malformed value from raising instead. Independent of crediting on purpose: payouts can be
-- paused while credits accrue, and credits already earned can be paid after crediting stops.
create or replace function public.payouts_enabled()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce((
    select case when jsonb_typeof(value -> 'payouts') = 'boolean'
                then (value ->> 'payouts')::boolean else false end
    from public.site_config where key = 'rewards'
  ), false);
$$;

comment on function public.payouts_enabled() is
  'Payout kill switch (CONTRACTS-PREDICTIONS §10.1 principle 6). TRUE only when site_config ''rewards''.payouts '
  'is the JSON boolean true. Checked by create_reward_claim (P0005) and before any nonce is allocated or signed '
  'bytes are persisted.';

-- Enqueue a claim. Still one function for the reason 20260908120002 gave (PostgREST cannot hold a
-- transaction open, so "create the claim, attach the credits" must be one statement), with the
-- audit's fixes:
--   * p_asset: only that asset's credits are considered, so a second reward asset can never wedge a
--     wallet with "multiple assets pending" (FM-14) or be paid at the wrong token's scale (FM-16);
--   * the limits are REQUIRED — null used to mean "no limit" on the money path (FM-11/13);
--   * greedy PREFIX under p_max_amount, oldest first: a balance over the maximum is paid in
--     successive claims instead of being refused forever (FM-14). A prefix, not a best fit: the
--     order credits are paid in is then a fact about the ledger, not about the arithmetic;
--   * rows are locked and their ids collected in ONE statement, attached BY ID LIST with RETURNING
--     amount, and the returned sum must equal the claim amount (FM-15). A credit settled mid-call
--     is neither attached nor counted; it simply stays claimable;
--   * amount_text: amounts never cross into TypeScript as JSON numbers (FM-08). PostgREST renders
--     numeric as a JSON number, and 5e-7 as a double prints as "5e-7".
create or replace function public.create_reward_claim(
  p_wallet text,
  p_asset text,
  p_min_amount numeric,
  p_max_amount numeric
)
returns table (id uuid, wallet text, amount_text text, asset text, status text)
language plpgsql
security definer
set search_path = public
as $$
#variable_conflict use_column
declare
  v_wallet text := lower(trim(p_wallet));
  v_asset text := trim(p_asset);
  v_ids bigint[];
  v_amounts numeric[];
  v_take bigint[] := '{}';
  v_sum numeric(38, 18) := 0;
  v_claim_id uuid;
  v_attached numeric;
  v_attached_n integer;
  i integer;
begin
  if v_wallet is null or v_wallet = '' then
    raise exception 'wallet is required' using errcode = '22023';
  end if;
  if v_asset is null or v_asset = '' then
    raise exception 'asset is required' using errcode = '22023';
  end if;
  if p_min_amount is null or p_max_amount is null then
    raise exception 'minimum and maximum claim amounts are required' using errcode = '22023';
  end if;
  if p_min_amount <= 0 or p_max_amount < p_min_amount then
    raise exception 'claim limits must satisfy 0 < min <= max' using errcode = '22023';
  end if;

  if not public.payouts_enabled() then
    raise exception 'payouts are paused' using errcode = 'P0005';
  end if;

  -- §5, at claim time as well as credit time (FM-06): a wallet blocked after it was credited keeps
  -- its credits on the books but cannot withdraw them.
  if exists (select 1 from public.policy_flags f where f.wallet = v_wallet and f.blocked) then
    raise exception 'wallet is not eligible for payouts' using errcode = 'P0006';
  end if;

  -- Lock and read in one statement, oldest first. A concurrent claim for the same wallet waits here
  -- and then either finds nothing left (P0002) or hits the one-in-flight index (23505).
  select array_agg(l.id order by l.created_at, l.id), array_agg(l.amount order by l.created_at, l.id)
    into v_ids, v_amounts
  from (
    select rl.id, rl.amount, rl.created_at
    from public.reward_ledger rl
    where rl.wallet = v_wallet and rl.asset = v_asset and rl.claim_id is null
    order by rl.created_at, rl.id
    for update
  ) l;

  if v_ids is null then
    raise exception 'nothing to claim' using errcode = 'P0002';
  end if;
  if v_amounts[1] > p_max_amount then
    raise exception 'the oldest credit alone exceeds the maximum claim amount' using errcode = 'P0004';
  end if;

  for i in 1 .. array_length(v_ids, 1) loop
    exit when v_sum + v_amounts[i] > p_max_amount;
    v_sum := v_sum + v_amounts[i];
    v_take := v_take || v_ids[i];
  end loop;

  if v_sum < p_min_amount then
    raise exception 'below minimum claim amount' using errcode = 'P0003';
  end if;

  -- The partial unique index turns a concurrent or replayed claim into 23505 here.
  insert into public.reward_claims (wallet, amount, asset)
  values (v_wallet, v_sum, v_asset)
  returning reward_claims.id into v_claim_id;

  perform set_config('wanted.attach_claim', v_claim_id::text, true);
  with attached as (
    update public.reward_ledger rl
       set claim_id = v_claim_id
     where rl.id = any (v_take) and rl.claim_id is null
    returning rl.amount
  )
  select coalesce(sum(a.amount), 0), count(*) into v_attached, v_attached_n from attached a;
  perform set_config('wanted.attach_claim', '', true);

  if v_attached <> v_sum or v_attached_n <> cardinality(v_take) then
    raise exception 'attached credits do not equal the claim amount' using errcode = 'P0013';
  end if;

  return query
    select c.id, c.wallet, c.amount::text, c.asset, c.status
    from public.reward_claims c where c.id = v_claim_id;
end;
$$;

comment on function public.create_reward_claim(text, text, numeric, numeric) is
  'Enqueues one claim: the longest oldest-first prefix of the wallet''s unclaimed p_asset credits whose sum is '
  '<= p_max_amount, attached by id list in the same transaction. P0005 paused, P0006 blocked, P0002 nothing, '
  'P0004 oldest credit > max, P0003 below min, 23505 a claim already in flight. Returns amount as text.';

-- ================================================================================================
-- 7. Worker path (§10.3). Every mutating function takes p_holder FIRST and fences on the lease.
-- ================================================================================================

-- The fence. Reads the lease row FOR UPDATE, so for the rest of the caller's transaction nobody can
-- take the lease over (acquire_payout_lease's UPDATE waits on this row lock); and refuses unless
-- this holder has it and it has not expired. A worker that outlived its TTL gets P0010 on its next
-- write, which is the whole point of a fencing token. Internal: no API role may call it directly.
create or replace function public.assert_payout_lease(p_holder uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_holder uuid;
  v_expires timestamptz;
begin
  select l.holder, l.expires_at into v_holder, v_expires
    from public.payout_lease l where l.id = 1
    for update;
  if p_holder is null or v_holder is distinct from p_holder or v_expires is null or v_expires <= now() then
    raise exception 'payout lease not held by this holder' using errcode = 'P0010';
  end if;
end;
$$;

-- Take the lease if it is free, expired, or already ours (renewal). Two concurrent callers
-- serialise on the row: the loser re-evaluates the WHERE against the winner's row and gets false.
create or replace function public.acquire_payout_lease(p_holder uuid, p_ttl_seconds integer)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_holder is null then
    raise exception 'holder is required' using errcode = '22023';
  end if;
  if p_ttl_seconds is null or p_ttl_seconds < 1 or p_ttl_seconds > 3600 then
    raise exception 'ttl must be between 1 and 3600 seconds' using errcode = '22023';
  end if;

  update public.payout_lease l
     set holder = p_holder,
         expires_at = now() + make_interval(secs => p_ttl_seconds)
   where l.id = 1
     and (l.holder is null or l.expires_at is null or l.expires_at <= now() or l.holder = p_holder);
  if found then
    return true;
  end if;

  if not exists (select 1 from public.payout_lease where id = 1) then
    raise exception 'payout_lease singleton row is missing' using errcode = 'P0013';
  end if;
  return false;
end;
$$;

-- Clears the lease only if p_holder still has it; a zombie releasing late cannot free someone
-- else's lease.
create or replace function public.release_payout_lease(p_holder uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.payout_lease l
     set holder = null, expires_at = null
   where l.id = 1 and l.holder = p_holder;
end;
$$;

-- Everything the worker needs to decide a tick, in one read. No lease: reading is always safe, and
-- a tick that loses the lease still reports. Every bigint/numeric is a JSON STRING (next_nonce,
-- nonce, amount_text, liability_text) — base-unit arithmetic in TypeScript is BigInt and a JSON
-- number would pass through a double first. `needs_review` is a count and stays a number.
-- in_flight is not filtered by signer: a claim signed by a previous key is still in flight.
create or replace function public.payout_snapshot(p_address text)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_address text := lower(trim(p_address));
  v_account jsonb;
  v_in_flight jsonb;
  v_review integer;
  v_queued jsonb;
  v_liability numeric(38, 18);
begin
  if v_address is null or v_address !~ '^0x[0-9a-f]{40}$' then
    raise exception 'address must be a 0x-prefixed 20-byte hex address' using errcode = '22023';
  end if;

  select jsonb_build_object('next_nonce', a.next_nonce::text, 'halted', a.halted, 'halt_reason', a.halt_reason)
    into v_account
  from public.treasury_accounts a where a.address = v_address;

  select coalesce(jsonb_agg(jsonb_build_object(
           'id', c.id, 'wallet', c.wallet, 'status', c.status, 'nonce', c.nonce::text,
           'raw_tx', c.raw_tx, 'tx_hash', c.tx_hash,
           'override_tx_hash', c.override_tx_hash, 'override_kind', c.override_kind,
           'signer_address', c.signer_address,
           'signed_at', c.signed_at, 'broadcast_at', c.broadcast_at, 'stuck_since', c.stuck_since)
         order by c.nonce, c.id), '[]'::jsonb)
    into v_in_flight
  from public.reward_claims c
  where c.status in ('signed', 'broadcast');

  select count(*)::integer into v_review from public.reward_claims c where c.status = 'needs_review';

  -- A queued row that already has a nonce is a signing that was interrupted; it goes first, because
  -- nothing else can be assigned until it is signed or cancelled.
  select coalesce(jsonb_agg(jsonb_build_object(
           'id', q.id, 'wallet', q.wallet, 'asset', q.asset,
           'amount_text', q.amount::text, 'nonce', q.nonce::text)
         order by (q.nonce is null), q.created_at, q.id), '[]'::jsonb)
    into v_queued
  from (
    select c.id, c.wallet, c.asset, c.amount, c.nonce, c.created_at
    from public.reward_claims c
    where c.status = 'queued'
    order by (c.nonce is null), c.created_at, c.id
    limit 5
  ) q;

  -- What the treasury owes, in whole units: every credit not yet in a claim plus every claim not yet
  -- terminal. Summed across assets — there is one reward asset; a second would need its own figure.
  v_liability :=
      coalesce((select sum(rl.amount) from public.reward_ledger rl where rl.claim_id is null), 0)
    + coalesce((select sum(c.amount) from public.reward_claims c
                where c.status in ('queued', 'signed', 'broadcast', 'needs_review')), 0);

  return jsonb_build_object(
    'account', v_account,
    'in_flight', v_in_flight,
    'needs_review', v_review,
    'queued', v_queued,
    'liability_text', v_liability::text
  );
end;
$$;

-- First sight of a signing address: the worker reads eth_getTransactionCount(addr, 'latest') and
-- records it. Insert-only — an existing row's next_nonce is the database's truth and is never
-- overwritten from the chain. Refuses a starting nonce at or below one this signer already used on
-- a recorded claim (the row can only be missing then if someone removed it).
create or replace function public.init_treasury_account(p_holder uuid, p_address text, p_nonce bigint)
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
  v_address text := lower(trim(p_address));
  v_next bigint;
begin
  perform public.assert_payout_lease(p_holder);
  if v_address is null or v_address !~ '^0x[0-9a-f]{40}$' then
    raise exception 'address must be a 0x-prefixed 20-byte hex address' using errcode = '22023';
  end if;
  if p_nonce is null or p_nonce < 0 then
    raise exception 'nonce must be >= 0' using errcode = '22023';
  end if;

  if not exists (select 1 from public.treasury_accounts a where a.address = v_address) then
    if exists (select 1 from public.reward_claims c
               where c.signer_address = v_address and c.nonce >= p_nonce) then
      raise exception 'a recorded claim already used a nonce >= % for this signer', p_nonce
        using errcode = 'P0013';
    end if;
    insert into public.treasury_accounts (address, next_nonce) values (v_address, p_nonce)
    on conflict (address) do nothing;
  end if;

  select a.next_nonce into v_next from public.treasury_accounts a where a.address = v_address;
  return v_next;
end;
$$;

-- Give a queued claim its nonce. Strictly serial (FM-09): a new nonce is allocated only when no
-- other claim is signed, broadcast, in review or queued-with-nonce, so the only possible gap is a
-- signed tx not yet mined, which the worker re-broadcasts before it asks for another.
-- Re-checks policy_flags.blocked at the last moment before signing (FM-06): a wallet blocked since it
-- claimed is failed never_signed, its credits released (and still unclaimable, because
-- create_reward_claim refuses blocked wallets), and NULL is returned — the worker signs nothing.
-- Also refuses to allocate while payouts are paused (P0005), so flipping the switch mid-tick stops
-- the next signing, not just the next tick.
create or replace function public.assign_claim_nonce(p_holder uuid, p_claim_id uuid, p_address text)
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
  v_address text := lower(trim(p_address));
  v_claim public.reward_claims;
  v_account public.treasury_accounts;
  v_nonce bigint;
begin
  perform public.assert_payout_lease(p_holder);
  if p_claim_id is null or v_address is null or v_address !~ '^0x[0-9a-f]{40}$' then
    raise exception 'claim id and a 0x-prefixed 20-byte address are required' using errcode = '22023';
  end if;

  select * into v_claim from public.reward_claims c where c.id = p_claim_id for update;
  if not found then
    raise exception 'claim not found' using errcode = 'P0013';
  end if;
  if v_claim.status <> 'queued' then
    raise exception 'claim is % and cannot be assigned a nonce', v_claim.status using errcode = 'P0013';
  end if;

  select * into v_account from public.treasury_accounts a where a.address = v_address for update;
  if not found or v_account.halted then
    raise exception 'treasury account missing or halted' using errcode = 'P0011';
  end if;

  if exists (
    select 1 from public.reward_claims c
    where c.id <> p_claim_id
      and (c.status in ('signed', 'broadcast', 'needs_review') or (c.status = 'queued' and c.nonce is not null))
  ) then
    raise exception 'another claim is in flight' using errcode = 'P0012';
  end if;

  -- The blocked re-check runs BEFORE the interrupted-signing shortcut below (B2, 2026-09-15 review).
  -- With the shortcut first, a wallet blocked after its claim took a nonce was signed and paid. The
  -- cancel goes through cancel_queued_claim, which also hands the nonce back (a holding claim's nonce
  -- is always next_nonce - 1 under strict serial): failing the row in place with its nonce still set
  -- would violate the status trigger (never_signed requires nonce null) and wedge the whole queue.
  -- Both rows are already locked FOR UPDATE by this transaction, so the nested locks do not wait.
  if exists (select 1 from public.policy_flags f where f.wallet = v_claim.wallet and f.blocked) then
    perform public.cancel_queued_claim(p_claim_id);
    return null;
  end if;

  if not public.payouts_enabled() then
    raise exception 'payouts are paused' using errcode = 'P0005';
  end if;

  -- An interrupted signing: the nonce is already this claim's, and re-signing with it is safe because
  -- nothing was persisted, so nothing was broadcast. The LAST check before allocating, so a halt,
  -- another claim in flight, a block and a pause all still stop it.
  if v_claim.nonce is not null then
    return v_claim.nonce;
  end if;

  update public.treasury_accounts a
     set next_nonce = a.next_nonce + 1, updated_at = now()
   where a.address = v_address
  returning a.next_nonce - 1 into v_nonce;

  update public.reward_claims c set nonce = v_nonce, updated_at = now() where c.id = p_claim_id;
  return v_nonce;
end;
$$;

-- Persist the signed transaction BEFORE it goes anywhere (principle 2). Everything needed to prove
-- and re-broadcast the payment lands on the row in one transaction: the bytes, their hash, the
-- nonce, every fee field, chain, token, recipient, amount in base units and signer.
--
-- What SQL can and cannot check. It checks that the row agrees with the claim: the nonce is the one
-- allocated, the recipient is the claim's wallet, and p_amount_base = amount x 10^p_decimals exactly
-- (numeric arithmetic, no float anywhere). It CANNOT check that p_tx_hash = keccak256(p_raw_tx) or
-- that raw_tx decodes to those fields — Postgres has no keccak and no RLP decoder. That half is
-- proven against real signed bytes by the web verifier (invariant I5).
--
-- Refuses while payouts are paused (P0005) or the signer is missing/halted (P0011): bytes that are
-- not persisted are never broadcast, so refusing here is how a halt stops a signing already in
-- progress. The claim stays queued-with-nonce and is re-signed with the same nonce after resume.
create or replace function public.record_signed_claim(
  p_holder uuid,
  p_claim_id uuid,
  p_nonce bigint,
  p_raw_tx text,
  p_tx_hash text,
  p_gas_limit bigint,
  p_max_fee_per_gas numeric,
  p_max_priority_fee_per_gas numeric,
  p_chain_id integer,
  p_token_address text,
  p_to_address text,
  p_amount_base numeric,
  p_decimals integer,
  p_signer_address text
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_raw text := lower(trim(p_raw_tx));
  v_hash text := lower(trim(p_tx_hash));
  v_token text := lower(trim(p_token_address));
  v_to text := lower(trim(p_to_address));
  v_signer text := lower(trim(p_signer_address));
  v_claim public.reward_claims;
  v_account public.treasury_accounts;
begin
  perform public.assert_payout_lease(p_holder);

  if p_claim_id is null or p_nonce is null or p_nonce < 0
     or v_raw is null or v_raw !~ '^0x02([0-9a-f]{2})+$'
     or v_hash is null or v_hash !~ '^0x[0-9a-f]{64}$'
     or p_gas_limit is null or p_gas_limit <= 0
     or p_max_fee_per_gas is null or p_max_fee_per_gas <= 0 or p_max_fee_per_gas <> trunc(p_max_fee_per_gas)
     or p_max_priority_fee_per_gas is null or p_max_priority_fee_per_gas < 0
     or p_max_priority_fee_per_gas <> trunc(p_max_priority_fee_per_gas)
     or p_max_priority_fee_per_gas > p_max_fee_per_gas
     or p_chain_id is null or p_chain_id <= 0
     or v_token is null or v_token !~ '^0x[0-9a-f]{40}$'
     or v_to is null or v_to !~ '^0x[0-9a-f]{40}$'
     or v_signer is null or v_signer !~ '^0x[0-9a-f]{40}$'
     or p_amount_base is null or p_amount_base <= 0 or p_amount_base <> trunc(p_amount_base)
     or p_decimals is null or p_decimals < 0 or p_decimals > 36 then
    raise exception 'invalid signed-claim argument' using errcode = '22023';
  end if;

  select * into v_claim from public.reward_claims c where c.id = p_claim_id for update;
  if not found then
    raise exception 'claim not found' using errcode = 'P0013';
  end if;
  if v_claim.status <> 'queued' then
    raise exception 'claim is %, not queued', v_claim.status using errcode = 'P0013';
  end if;
  if v_claim.nonce is distinct from p_nonce then
    raise exception 'nonce does not match the nonce allocated to this claim' using errcode = 'P0013';
  end if;
  if v_to <> v_claim.wallet then
    raise exception 'recipient does not match the claim wallet' using errcode = 'P0013';
  end if;
  if p_amount_base <> v_claim.amount * power(10::numeric, p_decimals) then
    raise exception 'amount_base does not equal amount x 10^decimals' using errcode = 'P0013';
  end if;

  if not public.payouts_enabled() then
    raise exception 'payouts are paused' using errcode = 'P0005';
  end if;
  select * into v_account from public.treasury_accounts a where a.address = v_signer;
  if not found or v_account.halted then
    raise exception 'treasury account missing or halted' using errcode = 'P0011';
  end if;

  update public.reward_claims c
     set status = 'signed',
         raw_tx = v_raw,
         tx_hash = v_hash,
         gas_limit = p_gas_limit,
         max_fee_per_gas = p_max_fee_per_gas,
         max_priority_fee_per_gas = p_max_priority_fee_per_gas,
         chain_id = p_chain_id,
         token_address = v_token,
         to_address = v_to,
         amount_base = p_amount_base,
         signer_address = v_signer,
         signed_at = now(),
         updated_at = now()
   where c.id = p_claim_id;
end;
$$;

-- The persisted bytes were accepted by the RPC (or it already knew them). Re-callable: each
-- re-broadcast of the same bytes counts an attempt; broadcast_at keeps the FIRST acceptance. Also
-- applies the stuck rule, because a dropped tx is re-broadcast here, tick after tick.
create or replace function public.mark_claim_broadcast(p_holder uuid, p_claim_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_status text;
begin
  perform public.assert_payout_lease(p_holder);
  select c.status into v_status from public.reward_claims c where c.id = p_claim_id for update;
  if v_status is null or v_status not in ('signed', 'broadcast') then
    raise exception 'claim is not signed or broadcast' using errcode = 'P0013';
  end if;

  update public.reward_claims c
     set status = 'broadcast',
         broadcast_at = coalesce(c.broadcast_at, now()),
         attempts = c.attempts + 1,
         stuck_since = coalesce(c.stuck_since,
                                case when c.signed_at is not null and now() - c.signed_at > interval '30 minutes'
                                     then now() end),
         updated_at = now()
   where c.id = p_claim_id;
end;
$$;

-- Something went wrong and nothing was decided: count it, keep the CODE, change no status. This is
-- the path a broadcast error takes — it never fails a claim and never releases a credit (FM-02).
create or replace function public.record_claim_attempt(p_holder uuid, p_claim_id uuid, p_error_code text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_status text;
begin
  perform public.assert_payout_lease(p_holder);
  if p_error_code is null or p_error_code !~ '^[a-z_]{1,48}$' then
    raise exception 'error code must match ^[a-z_]{1,48}$' using errcode = '22023';
  end if;
  select c.status into v_status from public.reward_claims c where c.id = p_claim_id for update;
  if v_status is null or v_status not in ('queued', 'signed', 'broadcast', 'needs_review') then
    raise exception 'claim is not in flight' using errcode = 'P0013';
  end if;

  update public.reward_claims c
     set attempts = c.attempts + 1,
         error = p_error_code,
         stuck_since = coalesce(c.stuck_since,
                                case when c.signed_at is not null and now() - c.signed_at > interval '30 minutes'
                                     then now() end),
         updated_at = now()
   where c.id = p_claim_id;
end;
$$;

-- A status-1 receipt was read for a hash this claim recorded: its own tx_hash, or an operator-
-- recorded same-nonce replacement of kind `pay`. Any other hash is not proof of THIS payment.
-- Credits stay attached: they have been paid.
create or replace function public.confirm_claim(p_holder uuid, p_claim_id uuid, p_tx_hash text, p_block_number bigint)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_hash text := lower(trim(p_tx_hash));
  v_claim public.reward_claims;
begin
  perform public.assert_payout_lease(p_holder);
  if v_hash is null or v_hash !~ '^0x[0-9a-f]{64}$' or p_block_number is null or p_block_number < 0 then
    raise exception 'a 32-byte tx hash and a block number are required' using errcode = '22023';
  end if;
  select * into v_claim from public.reward_claims c where c.id = p_claim_id for update;
  if not found or v_claim.status not in ('signed', 'broadcast') then
    raise exception 'claim is not signed or broadcast' using errcode = 'P0013';
  end if;
  -- coalesce(..., false), not a bare NOT: with no override recorded, override_kind is NULL, so
  -- (hash = tx_hash OR (NULL = 'pay' AND ...)) is NULL for a WRONG hash, NOT NULL is NULL, and
  -- IF NULL does not fire — which let any hash confirm the claim. Found by verify-predictions §12.
  if not coalesce(v_hash = v_claim.tx_hash
                  or (v_claim.override_kind = 'pay' and v_hash = v_claim.override_tx_hash), false) then
    raise exception 'hash is not a recorded payment for this claim' using errcode = 'P0014';
  end if;

  update public.reward_claims c
     set status = 'confirmed', block_number = p_block_number, receipt_status = 1,
         confirmed_at = now(), updated_at = now()
   where c.id = p_claim_id;
end;
$$;

-- Fail a signed/broadcast claim and release its credits — only with proof the chain supplied:
--   receipt_reverted  a status-0 receipt for tx_hash or a `pay` override (the nonce is consumed by a
--                     tx that moved nothing, so neither tx can ever pay);
--   cancel_receipt    a receipt for the recorded `cancel` override (the operator's 0-value
--                     self-transfer consumed the nonce).
-- Anything else — a timeout, a missing receipt, a broadcast error — is P0014, and the claim and its
-- credits stay exactly where they were (principle 4, FM-02/07).
create or replace function public.fail_claim(p_holder uuid, p_claim_id uuid, p_proof text, p_tx_hash text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_hash text := lower(trim(p_tx_hash));
  v_claim public.reward_claims;
begin
  perform public.assert_payout_lease(p_holder);
  select * into v_claim from public.reward_claims c where c.id = p_claim_id for update;
  if not found or v_claim.status not in ('signed', 'broadcast') then
    raise exception 'claim is not signed or broadcast' using errcode = 'P0013';
  end if;

  if p_proof = 'receipt_reverted'
     and v_hash is not null
     and (v_hash = v_claim.tx_hash or (v_claim.override_kind = 'pay' and v_hash = v_claim.override_tx_hash)) then
    null;
  elsif p_proof = 'cancel_receipt'
     and v_hash is not null
     and v_claim.override_kind = 'cancel' and v_hash = v_claim.override_tx_hash then
    null;
  else
    raise exception 'proof not acceptable' using errcode = 'P0014';
  end if;

  update public.reward_claims c
     set status = 'failed', proof = p_proof,
         receipt_status = case when p_proof = 'receipt_reverted' then 0 end,
         updated_at = now()
   where c.id = p_claim_id;

  perform set_config('wanted.release_claim', p_claim_id::text, true);
  update public.reward_ledger rl set claim_id = null where rl.claim_id = p_claim_id;
  perform set_config('wanted.release_claim', '', true);
end;
$$;

-- The nonce was consumed and no recorded hash has a receipt: something this system did not record
-- used the key (compromise, or an off-runbook manual send). Only a human may decide whether the
-- viewer was paid, so the claim goes to review and the signer halts (principle 5). If the claim's
-- signer has no account row, every account halts — fail closed rather than guess which.
create or replace function public.flag_claim_review(p_holder uuid, p_claim_id uuid, p_reason text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_claim public.reward_claims;
begin
  perform public.assert_payout_lease(p_holder);
  if p_reason is null or p_reason !~ '^[a-z_]{1,48}$' then
    raise exception 'reason must be a code matching ^[a-z_]{1,48}$' using errcode = '22023';
  end if;
  select * into v_claim from public.reward_claims c where c.id = p_claim_id for update;
  if not found or v_claim.status not in ('signed', 'broadcast', 'needs_review') then
    raise exception 'only a signed or broadcast claim can be flagged for review' using errcode = 'P0013';
  end if;

  update public.reward_claims c
     set status = 'needs_review', error = p_reason, updated_at = now()
   where c.id = p_claim_id;

  -- Keep the FIRST halt reason: it names the cause; later flags are consequences of it.
  update public.treasury_accounts a
     set halted = true, halt_reason = case when a.halted then a.halt_reason else p_reason end, updated_at = now()
   where a.address = v_claim.signer_address;
  if not found then
    update public.treasury_accounts a
       set halted = true, halt_reason = case when a.halted then a.halt_reason else p_reason end, updated_at = now();
  end if;
end;
$$;

-- Halt an account outright — the idle nonce check's response to 'latest' running ahead of
-- next_nonce ('unrecorded_tx_from_treasury'). Clearing it is resume_payouts, an operator act.
create or replace function public.halt_treasury(p_holder uuid, p_address text, p_reason text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_address text := lower(trim(p_address));
begin
  perform public.assert_payout_lease(p_holder);
  if v_address is null or v_address !~ '^0x[0-9a-f]{40}$' then
    raise exception 'address must be a 0x-prefixed 20-byte hex address' using errcode = '22023';
  end if;
  if p_reason is null or p_reason !~ '^[a-z_]{1,48}$' then
    raise exception 'reason must be a code matching ^[a-z_]{1,48}$' using errcode = '22023';
  end if;
  update public.treasury_accounts a
     set halted = true, halt_reason = case when a.halted then a.halt_reason else p_reason end, updated_at = now()
   where a.address = v_address;
  if not found then
    raise exception 'treasury account missing' using errcode = 'P0011';
  end if;
end;
$$;

-- Publish the worker's summary as site_config 'treasury_status'. site_config is PUBLIC (anon may
-- select every row), so this is a publication, and it refuses anything shaped like a secret or an
-- error message rather than trusting the caller: the value must be a JSON object under 8 KB whose
-- strings are single-line, at most 128 characters, and never 64+ hex digits (a private key, a hash
-- or a raw transaction). A PayoutSummary — codes, an address, decimal base-unit strings — always
-- passes; a viem message or key material never does.
create or replace function public.write_treasury_status(p_holder uuid, p_status jsonb)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  perform public.assert_payout_lease(p_holder);
  if p_status is null or jsonb_typeof(p_status) <> 'object' or octet_length(p_status::text) > 8192 then
    raise exception 'status must be a JSON object under 8 KB' using errcode = '22023';
  end if;
  if jsonb_path_exists(p_status,
       '$.** ? (@.type() == "string" && (@ like_regex "^(0x)?[0-9a-fA-F]{64,}$" || @ like_regex "[\n\r]"))')
     or exists (
       select 1 from jsonb_path_query(p_status, '$.** ? (@.type() == "string")') s(v)
       where length(s.v #>> '{}') > 128) then
    raise exception 'status carries a value that is not publishable' using errcode = '22023';
  end if;

  insert into public.site_config (key, value, updated_at) values ('treasury_status', p_status, now())
  on conflict (key) do update set value = excluded.value, updated_at = now();
end;
$$;

-- ================================================================================================
-- 8. Operator path (§10.3) — no lease; each call is a deliberate human act in the SQL editor.
-- ================================================================================================

-- Rule on a claim in review. `confirmed` needs the hash that paid (its own tx_hash or a `pay`
-- override) and the block; `failed` is the operator's proof (`operator_review`) and releases.
create or replace function public.resolve_claim_review(
  p_claim_id uuid, p_outcome text, p_tx_hash text, p_block_number bigint)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_hash text := lower(trim(p_tx_hash));
  v_claim public.reward_claims;
begin
  select * into v_claim from public.reward_claims c where c.id = p_claim_id for update;
  if not found or v_claim.status <> 'needs_review' then
    raise exception 'claim is not in needs_review' using errcode = 'P0013';
  end if;

  if p_outcome = 'confirmed' then
    if v_hash is null or v_hash !~ '^0x[0-9a-f]{64}$' or p_block_number is null or p_block_number < 0 then
      raise exception 'confirmed needs the paying tx hash and its block number' using errcode = '22023';
    end if;
    -- coalesce(..., false), not a bare NOT: with no override recorded, override_kind is NULL, so
  -- (hash = tx_hash OR (NULL = 'pay' AND ...)) is NULL for a WRONG hash, NOT NULL is NULL, and
  -- IF NULL does not fire — which let any hash confirm the claim. Found by verify-predictions §12.
  if not coalesce(v_hash = v_claim.tx_hash
                  or (v_claim.override_kind = 'pay' and v_hash = v_claim.override_tx_hash), false) then
      raise exception 'hash is not a recorded payment for this claim' using errcode = 'P0014';
    end if;
    update public.reward_claims c
       set status = 'confirmed', block_number = p_block_number, receipt_status = 1,
           confirmed_at = now(), updated_at = now()
     where c.id = p_claim_id;
  elsif p_outcome = 'failed' then
    update public.reward_claims c
       set status = 'failed', proof = 'operator_review', updated_at = now()
     where c.id = p_claim_id;
    perform set_config('wanted.release_claim', p_claim_id::text, true);
    update public.reward_ledger rl set claim_id = null where rl.claim_id = p_claim_id;
    perform set_config('wanted.release_claim', '', true);
  else
    raise exception 'outcome must be confirmed or failed' using errcode = '22023';
  end if;
end;
$$;

-- Clear a halt. Refused while any claim is still in review: resuming is the last step of an
-- incident, after every claim it touched has been ruled on. If the halt came from the idle nonce
-- check, next_nonce must first be corrected by hand to the chain's 'latest' count — otherwise the
-- next tick sees the same mismatch and halts again, which is the correct behaviour.
create or replace function public.resume_payouts(p_address text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_address text := lower(trim(p_address));
begin
  perform 1 from public.treasury_accounts a where a.address = v_address for update;
  if not found then
    raise exception 'treasury account missing' using errcode = 'P0011';
  end if;
  if exists (select 1 from public.reward_claims c where c.status = 'needs_review') then
    raise exception 'resolve every needs_review claim before resuming' using errcode = 'P0013';
  end if;
  update public.treasury_accounts a
     set halted = false, halt_reason = null, updated_at = now()
   where a.address = v_address;
end;
$$;

-- Cancel a claim that provably never had bytes persisted (raw_tx null) — release case (d). If a
-- nonce was assigned it must be the most recent allocation (next_nonce - 1), and it is handed back:
-- the bytes signed in memory for it (if any) were never persisted, so never broadcast, so the nonce
-- is still unused on chain. Exactly one account must match, or nothing is changed.
create or replace function public.cancel_queued_claim(p_claim_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_claim public.reward_claims;
  v_matches integer;
begin
  select * into v_claim from public.reward_claims c where c.id = p_claim_id for update;
  if not found or v_claim.status <> 'queued' or v_claim.raw_tx is not null then
    raise exception 'only a queued claim with no signed bytes can be cancelled' using errcode = 'P0013';
  end if;

  if v_claim.nonce is not null then
    select count(*) into v_matches from (
      select 1 from public.treasury_accounts a where a.next_nonce = v_claim.nonce + 1 for update
    ) m;
    if v_matches <> 1 then
      raise exception 'claim nonce is not the most recent allocation of exactly one account'
        using errcode = 'P0013';
    end if;
    update public.treasury_accounts a
       set next_nonce = a.next_nonce - 1, updated_at = now()
     where a.next_nonce = v_claim.nonce + 1;
  end if;

  update public.reward_claims c
     set status = 'failed', proof = 'never_signed', nonce = null, updated_at = now()
   where c.id = p_claim_id;
  perform set_config('wanted.release_claim', p_claim_id::text, true);
  update public.reward_ledger rl set claim_id = null where rl.claim_id = p_claim_id;
  perform set_config('wanted.release_claim', '', true);
end;
$$;

-- Record a manual same-nonce transaction the operator sent from the keystore: `pay` (a replacement
-- that pays the same transfer) or `cancel` (a 0-value self-transfer that burns the nonce). Once per
-- claim. The reconciler then reads this hash's receipt too, so a replacement is always a KNOWN hash
-- and never the "unrecorded consumer" that halts the treasury.
create or replace function public.record_claim_override(p_claim_id uuid, p_tx_hash text, p_kind text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_hash text := lower(trim(p_tx_hash));
  v_claim public.reward_claims;
begin
  if p_kind is null or p_kind not in ('pay', 'cancel') then
    raise exception 'kind must be pay or cancel' using errcode = '22023';
  end if;
  if v_hash is null or v_hash !~ '^0x[0-9a-f]{64}$' then
    raise exception 'a 32-byte tx hash is required' using errcode = '22023';
  end if;
  select * into v_claim from public.reward_claims c where c.id = p_claim_id for update;
  if not found or v_claim.status not in ('signed', 'broadcast', 'needs_review') then
    raise exception 'claim is not signed, broadcast or in review' using errcode = 'P0013';
  end if;
  if v_claim.override_tx_hash is not null then
    raise exception 'an override is already recorded for this claim' using errcode = 'P0013';
  end if;
  if v_hash = v_claim.tx_hash then
    raise exception 'the override must be a different transaction' using errcode = '22023';
  end if;

  update public.reward_claims c
     set override_tx_hash = v_hash, override_kind = p_kind, updated_at = now()
   where c.id = p_claim_id;
end;
$$;

-- ================================================================================================
-- 9. Grants — BY NAME, from anon and authenticated too (see 20260909000000 for why `from public`
--    alone leaves Supabase's direct grants in place). Every §10.3 function is service_role only.
--    PostgREST offers no finer role than service_role, so the harness's service key could call
--    these too; that is why the fencing, the proofs and the state machine live in the functions
--    rather than in trust in the caller.
-- ================================================================================================

revoke execute on function public.payouts_enabled() from public, anon, authenticated;
revoke execute on function public.create_reward_claim(text, text, numeric, numeric) from public, anon, authenticated;
revoke execute on function public.acquire_payout_lease(uuid, integer) from public, anon, authenticated;
revoke execute on function public.release_payout_lease(uuid) from public, anon, authenticated;
revoke execute on function public.payout_snapshot(text) from public, anon, authenticated;
revoke execute on function public.init_treasury_account(uuid, text, bigint) from public, anon, authenticated;
revoke execute on function public.assign_claim_nonce(uuid, uuid, text) from public, anon, authenticated;
revoke execute on function public.record_signed_claim(uuid, uuid, bigint, text, text, bigint, numeric, numeric, integer, text, text, numeric, integer, text) from public, anon, authenticated;
revoke execute on function public.mark_claim_broadcast(uuid, uuid) from public, anon, authenticated;
revoke execute on function public.record_claim_attempt(uuid, uuid, text) from public, anon, authenticated;
revoke execute on function public.confirm_claim(uuid, uuid, text, bigint) from public, anon, authenticated;
revoke execute on function public.fail_claim(uuid, uuid, text, text) from public, anon, authenticated;
revoke execute on function public.flag_claim_review(uuid, uuid, text) from public, anon, authenticated;
revoke execute on function public.halt_treasury(uuid, text, text) from public, anon, authenticated;
revoke execute on function public.write_treasury_status(uuid, jsonb) from public, anon, authenticated;
revoke execute on function public.resolve_claim_review(uuid, text, text, bigint) from public, anon, authenticated;
revoke execute on function public.resume_payouts(text) from public, anon, authenticated;
revoke execute on function public.cancel_queued_claim(uuid) from public, anon, authenticated;
revoke execute on function public.record_claim_override(uuid, text, text) from public, anon, authenticated;

grant execute on function public.payouts_enabled() to service_role;
grant execute on function public.create_reward_claim(text, text, numeric, numeric) to service_role;
grant execute on function public.acquire_payout_lease(uuid, integer) to service_role;
grant execute on function public.release_payout_lease(uuid) to service_role;
grant execute on function public.payout_snapshot(text) to service_role;
grant execute on function public.init_treasury_account(uuid, text, bigint) to service_role;
grant execute on function public.assign_claim_nonce(uuid, uuid, text) to service_role;
grant execute on function public.record_signed_claim(uuid, uuid, bigint, text, text, bigint, numeric, numeric, integer, text, text, numeric, integer, text) to service_role;
grant execute on function public.mark_claim_broadcast(uuid, uuid) to service_role;
grant execute on function public.record_claim_attempt(uuid, uuid, text) to service_role;
grant execute on function public.confirm_claim(uuid, uuid, text, bigint) to service_role;
grant execute on function public.fail_claim(uuid, uuid, text, text) to service_role;
grant execute on function public.flag_claim_review(uuid, uuid, text) to service_role;
grant execute on function public.halt_treasury(uuid, text, text) to service_role;
grant execute on function public.write_treasury_status(uuid, jsonb) to service_role;
grant execute on function public.resolve_claim_review(uuid, text, text, bigint) to service_role;
grant execute on function public.resume_payouts(text) to service_role;
grant execute on function public.cancel_queued_claim(uuid) to service_role;
grant execute on function public.record_claim_override(uuid, text, text) to service_role;

-- The fence is called only from inside the functions above (which run as their owner). Nobody
-- calls it directly — not even service_role, since a bare "am I the holder?" RPC is nothing the
-- worker needs and one more surface it does not.
revoke execute on function public.assert_payout_lease(uuid) from public, anon, authenticated, service_role;

commit;
