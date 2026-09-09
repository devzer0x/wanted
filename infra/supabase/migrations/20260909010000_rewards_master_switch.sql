-- The rewards master switch, enforced where the credit is actually written.
--
-- CONTRACTS-PREDICTIONS §5 says eligibility is "called before any ledger credit and again before
-- any claim". Only the second half was true. `REWARDS_ENABLED` is a Vercel environment variable
-- read by `assessEligibility()` in web/src/lib/policy/index.ts, and the only callers of that are
-- /api/auth/verify, /api/auth/session, /api/rewards/balance and /api/rewards/claim. The credit is
-- written by `settle_due_predictions()`, which is a Postgres function: it cannot read a Node
-- process's environment, and never could. So the flag blocked the withdrawal and not the deposit —
-- with rewards "off", settlement would still have written reward_ledger rows the moment the agent
-- produced its first resolvable prediction. A flag has to live where the code that obeys it runs.
--
-- WHY A TRIGGER RATHER THAN AN EDIT TO settle_due_predictions().
--
-- `insert into public.reward_ledger` appears exactly once in the entire schema, in settlement. The
-- two other statements that touch the table are UPDATEs inside the claim RPC. So a BEFORE INSERT
-- trigger is not one more check to keep in sync with the writer — it is a gate on the only door,
-- and it stays a gate on the only door when someone adds a second writer later. The alternative
-- was a `create or replace` carrying a 380-line copy of settlement into this file purely to add
-- two lines, leaving two definitions to drift apart.
--
-- BEFORE INSERT ONLY, DELIBERATELY. `finalize_reward_claim` sets `claim_id = null` to return
-- credits to the claimable pool when an on-chain payout fails. If the switch could block that
-- UPDATE, an operator who flipped rewards off during a failing payout would destroy a user's
-- balance — the switch causing the exact harm it exists to prevent. Rows that already exist are
-- never the switch's business.
--
-- SKIP, NOT RAISE. Returning NULL drops the credit and lets the rest of settlement commit, so
-- predictions still resolve, streaks still advance and the leaderboard still ranks. Raising would
-- abort the whole settlement transaction and the prediction would never leave `resolving`. This is
-- the same posture the payout loop already takes for a blocked wallet: "a blocked wallet still got
-- counted above for correctness/streak/leaderboard; it just never reaches the ledger."
--
-- DEFAULT CLOSED. `reward_caps` and `settlement` fall back to permissive placeholders when their
-- site_config row is absent, which is right for a tuning knob and wrong for a kill switch: an
-- absent row must never mean "pay out". Today the live project has no `rewards` row and no
-- `reward_caps` row, so before this migration a first settlement would have credited against a
-- 1,000,000/day placeholder cap. After it, an absent row means no money moves.
--
-- GOING LIVE is one statement, and it is the operator's to run — not this migration's:
--
--   insert into public.site_config (key, value) values ('rewards', '{"enabled": true}')
--   on conflict (key) do update set value = excluded.value;
--
-- Turning it off again is the same statement with false, and takes effect on the next insert with
-- no deploy. Keep `REWARDS_ENABLED` set in Vercel too: this switch stops credits being created,
-- that one stops existing credits being claimed, and an operator standing down wants both.

create or replace function public.rewards_enabled()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  -- Absent row, absent key, or a non-boolean value all read as false. `->>` yields NULL rather
  -- than erroring on a missing key, and `coalesce` turns every one of those into "off".
  select coalesce(
    (select (value ->> 'enabled')::boolean from public.site_config where key = 'rewards'),
    false
  );
$$;

comment on function public.rewards_enabled() is
  'Rewards master switch (CONTRACTS-PREDICTIONS §5). Reads site_config key ''rewards'', field '
  '''enabled''. Defaults to FALSE when the row is missing — an absent config must never mean pay out.';

create or replace function public.reward_ledger_master_switch()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if public.rewards_enabled() then
    return new;
  end if;

  -- Visible in the Postgres log, so a settlement that credits nobody is distinguishable from a
  -- settlement where nobody was right.
  raise notice 'reward_ledger: credit of % % for % suppressed — rewards master switch is off',
    new.amount, new.asset, new.wallet;
  return null;
end;
$$;

drop trigger if exists reward_ledger_master_switch_trg on public.reward_ledger;
create trigger reward_ledger_master_switch_trg
  before insert on public.reward_ledger
  for each row
  execute function public.reward_ledger_master_switch();

-- `rewards_enabled()` is a read of a row `site_config`'s own policy already publishes to the
-- browser, so exposing it costs nothing and lets the UI state honestly that rewards are not live
-- yet rather than deciding that itself (§5: "UI may reflect ineligibility; it may never decide it").
grant execute on function public.rewards_enabled() to anon, authenticated, service_role;

-- The trigger function is invoked by the trigger, never called directly. Nobody needs EXECUTE.
revoke execute on function public.reward_ledger_master_switch() from public, anon, authenticated;
