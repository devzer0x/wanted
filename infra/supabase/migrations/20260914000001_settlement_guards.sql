-- Two guards on the CREDIT side of rewards, both found by the 2026-09-14 payout audit.
--
-- 1. SETTLEMENT RUNS ONE AT A TIME (audit FM-10).
--
-- settle_due_predictions() claims candidates with FOR UPDATE SKIP LOCKED, so two overlapping runs
-- — a slow tick overlapping the next minute's, or a manual POST during a cron run — settle
-- DIFFERENT predictions in parallel. Each computes "spent today" with sum() under READ COMMITTED
-- and cannot see the other's uncommitted credits, so each can spend the full remaining daily cap:
-- two runs, twice the cap. The caps are only atomic if settlement is serial.
--
-- The wrapper takes a TRANSACTION-scoped advisory lock and gives up at once if another run holds
-- it. PostgREST runs one RPC as one transaction, so the lock covers exactly the settlement call and
-- is released when it commits or aborts — a crashed caller cannot leave it held, which a
-- session-level lock on a pooled connection could. The loser returns 0; anything it did not reach
-- is picked up by the next tick, because settlement is idempotent and window-bounded.
--
-- EXECUTE on the raw function is then revoked from service_role, so the lock cannot be bypassed:
-- the only path from the cron to settlement goes through it.
--
-- 2. REWARDS CANNOT READ AS ON WITHOUT REAL CAPS (audit FM-11).
--
-- settle_due_predictions() falls back to 1,000,000 / 100,000 / 10,000 whole TTWO when the
-- reward_caps row is absent or a key is misspelled ("dailyCap"). Rather than carry a 380-line copy
-- of settlement here to change three literals, rewards_enabled() — which already gates every
-- credit through the BEFORE INSERT trigger — now also requires all three caps to be present,
-- numeric and positive. A missing or mistyped cap therefore stops credits instead of uncapping
-- them, and the permissive fallbacks become unreachable while rewards are on.
--
-- It also fixes a bug in 20260909010000's version: `(value ->> 'enabled')::boolean` does not
-- yield NULL for a value like "maybe", it RAISES — so a typo in the switch would have aborted every
-- settlement transaction, not read as off. CASE is used throughout because Postgres does not
-- guarantee evaluation order inside AND, and a cast must never run before its type check.
--
-- Idempotent.

create or replace function public.rewards_enabled()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select
    coalesce((
      select case when jsonb_typeof(value -> 'enabled') = 'boolean'
                  then (value ->> 'enabled')::boolean else false end
      from public.site_config where key = 'rewards'
    ), false)
    and coalesce((
      select
        case when jsonb_typeof(value -> 'daily_cap') = 'number'
             then (value ->> 'daily_cap')::numeric > 0 else false end
        and case when jsonb_typeof(value -> 'max_per_prediction') = 'number'
             then (value ->> 'max_per_prediction')::numeric > 0 else false end
        and case when jsonb_typeof(value -> 'max_per_wallet_day') = 'number'
             then (value ->> 'max_per_wallet_day')::numeric > 0 else false end
      from public.site_config where key = 'reward_caps'
    ), false);
$$;

comment on function public.rewards_enabled() is
  'Rewards master switch (CONTRACTS-PREDICTIONS §5). TRUE only when site_config ''rewards''.enabled is '
  'the JSON boolean true AND site_config ''reward_caps'' holds numeric, positive daily_cap, '
  'max_per_prediction and max_per_wallet_day. Anything else — absent, misspelled, mistyped — is FALSE.';

create or replace function public.settle_due_predictions_serialized()
returns integer
language plpgsql
security definer
set search_path = public
as $$
begin
  -- 7741300101 is this lock's arbitrary, fixed key; nothing else in the schema uses advisory locks.
  if not pg_try_advisory_xact_lock(7741300101) then
    return 0;
  end if;
  return public.settle_due_predictions();
end;
$$;

comment on function public.settle_due_predictions_serialized() is
  'The only entry point to settlement for the application. Serialises runs with a transaction-scoped '
  'advisory lock so the §6 daily and per-wallet caps are atomic; a concurrent call returns 0 at once.';

revoke execute on function public.settle_due_predictions_serialized() from public, anon, authenticated;
grant execute on function public.settle_due_predictions_serialized() to service_role;

-- Nothing in the application may reach settlement except through the lock.
revoke execute on function public.settle_due_predictions() from service_role;
