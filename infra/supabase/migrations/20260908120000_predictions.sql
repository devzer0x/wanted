-- WASTED prediction layer schema — CONTRACTS-PREDICTIONS.md v2.0 §2-§3. Companion to (and
-- additive over) CONTRACTS.md v1.14 / 20260825120000_schema.sql, which this migration never
-- alters, renames or drops. All writes to the tables below come from the harness/site's
-- service-role key or through the SECURITY DEFINER entry points at the bottom of this file; the
-- public reads via RLS (policies migration, 20260908120001).
--
-- Table creation order here is dependency-driven (reward_claims before reward_ledger, because
-- reward_ledger.claim_id references it), not the descriptive order in CONTRACTS-PREDICTIONS §2.

create type public.prediction_status as enum ('open', 'locked', 'resolving', 'settled', 'void');

-- `wallet_sessions` — EIP-4361 (Sign-In with Ethereum) nonce issuance and verification state.
-- Not Supabase Auth: the site's wallet identity lives entirely in this table plus an HttpOnly
-- session cookie the API routes read server-side; there is no auth.uid() to hang RLS off, which
-- is why this table (like reward_ledger/reward_claims/policy_flags) carries no public policy.
create table public.wallet_sessions (
  id uuid primary key default gen_random_uuid(),
  address text not null check (address = lower(address)),
  nonce text not null,
  chain_id integer not null,
  issued_at timestamptz not null default now(),
  expires_at timestamptz not null,
  verified_at timestamptz,
  ua_hash text,
  ip_hash text,
  check (issued_at < expires_at)
);
create index wallet_sessions_address_idx on public.wallet_sessions (address);

-- `predictions` — CONTRACTS-PREDICTIONS §2. A prediction always belongs to a real session and
-- its window is enforced at the row level (CHECK), not trusted from the caller.
create table public.predictions (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references public.sessions (id),
  question text not null,
  prediction_type text not null,
  state_context jsonb not null default '{}'::jsonb,
  created_from_event bigint references public.events (id),
  opened_at timestamptz not null default now(),
  locks_at timestamptz not null,
  resolves_at timestamptz not null,
  outcomes jsonb not null,
  telemetry_rule jsonb not null,
  status public.prediction_status not null default 'open',
  result text,
  resolution_evidence jsonb,
  reward_pool numeric(38, 18) not null check (reward_pool > 0),
  reward_asset text not null,
  entry_count integer not null default 0,
  correct_count integer not null default 0,
  is_event boolean not null default false,
  settled_at timestamptz,
  check (opened_at < locks_at and locks_at < resolves_at)
  -- `result`, once set, must be a real outcome key. Postgres CHECK constraints cannot contain a
  -- subquery (even a set-returning-function one over the row's own jsonb column), so this is
  -- enforced in `predictions_guard_status_transition` instead, at the point `result` is allowed
  -- to become non-null (the resolving -> settled transition), not here.
);
create index predictions_session_idx on public.predictions (session_id);
create index predictions_status_locks_idx on public.predictions (status, locks_at);
create index predictions_status_resolves_idx on public.predictions (status, resolves_at);
create index predictions_settled_at_idx on public.predictions (settled_at desc);

-- Status is monotonic. `settled` and `void` are terminal. Enforced below by
-- `predictions_guard_status_transition`, not by a CHECK (a CHECK can't see OLD).

-- `prediction_entries` — one row per (prediction, wallet). This UNIQUE constraint is the whole
-- point: it is what makes "did this wallet already predict?" a database fact, not an app-layer
-- promise.
create table public.prediction_entries (
  constraint prediction_entries_wallet_lowercase check (wallet = lower(wallet)),
  id bigint generated always as identity primary key,
  prediction_id uuid not null references public.predictions (id),
  wallet text not null,
  outcome text not null,
  created_at timestamptz not null default now(),
  constraint prediction_entries_prediction_wallet_key unique (prediction_id, wallet)
);
create index prediction_entries_wallet_idx on public.prediction_entries (wallet);

-- `reward_claims` — one on-chain payout of N ledger rows. Created before `reward_ledger` because
-- `reward_ledger.claim_id` references it.
create table public.reward_claims (
  constraint reward_claims_wallet_lowercase check (wallet = lower(wallet)),
  id uuid primary key default gen_random_uuid(),
  wallet text not null,
  amount numeric(38, 18) not null check (amount > 0),
  asset text not null,
  status text not null default 'pending' check (status in ('pending', 'submitted', 'confirmed', 'failed')),
  tx_hash text,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index reward_claims_wallet_idx on public.reward_claims (wallet, created_at desc);
-- Idempotency invariant 3: one in-flight claim per wallet; a replayed claim request cannot
-- double-spend.
create unique index reward_claims_one_inflight_per_wallet
  on public.reward_claims (wallet)
  where status in ('pending', 'submitted');

-- `reward_ledger` — one credit per (prediction, wallet). This UNIQUE constraint is idempotency
-- invariant 2: a prediction can never credit a wallet twice, even if settlement runs repeatedly.
-- `claim_id` is nullable; claimable balance is `sum(amount) where claim_id is null` (invariant 4).
create table public.reward_ledger (
  constraint reward_ledger_wallet_lowercase check (wallet = lower(wallet)),
  id bigint generated always as identity primary key,
  prediction_id uuid not null references public.predictions (id),
  wallet text not null,
  amount numeric(38, 18) not null check (amount > 0),
  asset text not null,
  claim_id uuid references public.reward_claims (id),
  clamped boolean not null default false,
  clamp_reason text,
  created_at timestamptz not null default now(),
  constraint reward_ledger_prediction_wallet_key unique (prediction_id, wallet)
);
create index reward_ledger_wallet_idx on public.reward_ledger (wallet);
create index reward_ledger_unclaimed_idx on public.reward_ledger (wallet) where claim_id is null;
create index reward_ledger_created_at_idx on public.reward_ledger (created_at);

-- `wallet_streaks` — current + best streak per wallet, maintained only by `settle_due_predictions`.
create table public.wallet_streaks (
  wallet text primary key check (wallet = lower(wallet)),
  current_streak integer not null default 0 check (current_streak >= 0),
  best_streak integer not null default 0 check (best_streak >= 0),
  updated_at timestamptz not null default now(),
  check (best_streak >= current_streak)
);

-- `policy_flags` — server-side eligibility, keyed by wallet. `web/src/lib/policy/`'s
-- `assessEligibility()` reads/writes this as its persistence layer (region checks, terms
-- acceptance, manual overrides); `settle_due_predictions` consults `blocked` before crediting a
-- wallet, per CONTRACTS-PREDICTIONS §5 ("predicting is always allowed; rewarding is not").
create table public.policy_flags (
  wallet text primary key check (wallet = lower(wallet)),
  blocked boolean not null default false,
  block_reason text,
  region text,
  terms_accepted_at timestamptz,
  updated_at timestamptz not null default now()
);

-- Realtime: the site subscribes to inserts on `predictions` so live/locked/settled cards update
-- without a poll. No other new table needs this — entries/ledger/claims/streaks are read through
-- authenticated routes or the public views/functions below, never streamed raw.
-- The supabase_realtime publication exists on Supabase; guard for plain-Postgres test runs (same
-- guard as 20260825120000_schema.sql).
do $$
begin
  if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    create publication supabase_realtime;
  end if;
end
$$;
alter publication supabase_realtime add table public.predictions;

-- ---------------------------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------------------------

-- Rejects any backwards (or sideways-into-a-different-branch) status transition. `settled` and
-- `void` are terminal — neither appears as an OLD status in the allowed-pairs list below, so no
-- transition out of them is ever accepted. Also stamps `settled_at` the moment a prediction
-- reaches either terminal state, so `settled_at` has exactly one writer.
create function public.predictions_guard_status_transition()
returns trigger
language plpgsql
as $$
begin
  if new.status = old.status then
    return new;
  end if;

  if (old.status, new.status) not in (
    ('open', 'locked'),
    ('locked', 'resolving'),
    ('resolving', 'settled'),
    ('resolving', 'void')
  ) then
    raise exception 'invalid prediction status transition % -> % for prediction %',
      old.status, new.status, old.id;
  end if;

  if new.status = 'settled' and not exists (
    select 1 from jsonb_array_elements(new.outcomes) o where o ->> 'key' = new.result
  ) then
    raise exception 'prediction % settled with result % which is not one of its outcome keys',
      new.id, new.result;
  end if;

  if new.status in ('settled', 'void') then
    new.settled_at := now();
  end if;

  return new;
end;
$$;

create trigger predictions_status_transition_guard
  before update of status on public.predictions
  for each row execute function public.predictions_guard_status_transition();

-- `entry_count` is maintained here, by trigger, never by a client and never by `enter_prediction`
-- itself — so it stays correct even if a row reaches `prediction_entries` by another path (tests,
-- an admin backfill) with the same idempotency guarantees.
create function public.predictions_bump_entry_count()
returns trigger
language plpgsql
as $$
begin
  update public.predictions set entry_count = entry_count + 1 where id = new.prediction_id;
  return new;
end;
$$;

create trigger prediction_entries_bump_count
  after insert on public.prediction_entries
  for each row execute function public.predictions_bump_entry_count();

-- ---------------------------------------------------------------------------------------------
-- SQL entry points (CONTRACTS-PREDICTIONS §2/§4/§3)
-- ---------------------------------------------------------------------------------------------

-- The server-side lock enforcement: `/api/predictions/[id]/enter` calls this after it has already
-- verified the caller owns `p_wallet` (the wallet-auth session cookie). This function does not
-- re-verify wallet ownership — only that the prediction is still open, in Postgres's own clock,
-- and that the outcome key is real. A client clock, a paused tab, or a replayed request cannot
-- beat the lock, because `now() < p.locks_at` is evaluated by the same statement that inserts.
create function public.enter_prediction(p_id uuid, p_wallet text, p_outcome text)
returns boolean
language sql
security definer
set search_path = public, pg_temp
as $$
  with ins as (
    insert into public.prediction_entries (prediction_id, wallet, outcome)
    -- lower(): wallet addresses are stored lower-cased everywhere, so that
    -- UNIQUE (prediction_id, wallet) is genuinely one-entry-per-ADDRESS. EIP-55 checksumming is a
    -- display concern; if the checksummed form were stored, two spellings of the same address would
    -- be two rows and the constraint would not bind. The CHECKs on these tables enforce it.
    select p_id, lower(trim(p_wallet)), p_outcome
    where exists (
      select 1
      from public.predictions p
      where p.id = p_id
        and p.status = 'open'
        and now() < p.locks_at
        and exists (select 1 from jsonb_array_elements(p.outcomes) o where o ->> 'key' = p_outcome)
    )
    on conflict (prediction_id, wallet) do nothing
    returning 1
  )
  select exists (select 1 from ins);
$$;

-- open -> locked where the lock time has passed. Called by /api/cron/tick.
create function public.lock_due_predictions()
returns integer
language sql
security definer
set search_path = public, pg_temp
as $$
  with upd as (
    update public.predictions
    set status = 'locked'
    where status = 'open' and now() >= locks_at
    returning 1
  )
  select count(*)::integer from upd;
$$;

-- The settlement engine — CONTRACTS-PREDICTIONS §3. Pure function of rows already in
-- `public.events`/`public.sessions`/`public.stats` inside [locks_at, resolves_at]; never trusts
-- client input, never infers. Safely re-runnable: a prediction only ever reaches `settled`/`void`
-- once (both terminal, guarded by the trigger above), and `reward_ledger`'s
-- UNIQUE (prediction_id, wallet) plus ON CONFLICT DO NOTHING means even a forced re-entry can
-- never double-credit. Returns the number of predictions moved to a terminal state this run
-- (settled + void) — predictions merely skipped for incomplete telemetry (see below) don't count,
-- since nothing happened to them yet.
--
-- KNOWN GAP, flagged rather than silently faked: CONTRACTS-PREDICTIONS §3 lists "a heartbeat gap
-- > 90s" alongside session_end/bridge_down as a mandatory void trigger. `stats.heartbeat_at`
-- (CONTRACTS.md §5) is a single upserted-in-place value per session, not a time series, and there
-- is no `heartbeat` row in the closed `events.type` enum (CONTRACTS.md §4) either — so a gap that
-- happened during a past window cannot be reconstructed from anything this function can query by
-- the time settlement runs. `decisions.ts`/`events.ts` cadence was considered as a proxy and
-- rejected: normal play has multi-minute gaps between decisions during a single long action (see
-- harness `ACTIVITY_GAP_S`), so using it here would false-void healthy predictions. This function
-- therefore enforces the two liveness signals that ARE real and queryable — `sessions.ended_at`
-- falling inside-or-before the window, and `bridge_down`/`bridge_up` interval overlap — and does
-- not claim to catch a silent harness crash with no corresponding event row. Needs either a
-- discrete heartbeat/gap event added to the CONTRACTS.md §4 enum, or a time-series heartbeat table,
-- before the 90s-gap condition can be implemented for real; flagged to the orchestrator.
create function public.settle_due_predictions()
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_daily_cap numeric(38, 18);
  v_max_per_prediction numeric(38, 18);
  v_max_per_wallet_day numeric(38, 18);
  v_settled_count integer := 0;
  p record;
  v_void_reason text;
  v_evidence jsonb;
  v_result text;
  v_kind text;
  v_params jsonb;
  v_outcome_true text;
  v_outcome_false text;
  v_session_ended_at timestamptz;
  v_bridge_overlap boolean;
  v_correct_count integer;
  v_pool_base numeric(38, 18);
  v_effective_pool_base numeric(38, 18);
  v_per_wallet_base numeric(38, 18);
  wallet_row record;
  v_daily_spent numeric(38, 18);
  v_wallet_day_spent numeric(38, 18);
  v_credit numeric(38, 18);
  v_clamped boolean;
  v_clamp_reason text;
  v_blocked boolean;
  v_settlement_grace_minutes numeric;
  v_settlement_grace interval;
  v_ready boolean;
  v_max_events_ts timestamptz;
  v_heartbeat_at timestamptz;
begin
  -- §6 caps live in `site_config` (the existing general operator-config table), key
  -- 'reward_caps', mirroring the "operator writes it, the app reads it with a fallback when the
  -- row is absent" pattern the schema migration already established for the 'stream' key. The
  -- fallbacks below are deliberately permissive placeholders, not a real business decision — an
  -- operator-inserted `site_config` row is expected to override them before go-live.
  select coalesce((value ->> 'daily_cap')::numeric, 1000000)
    into v_daily_cap from public.site_config where key = 'reward_caps';
  v_daily_cap := coalesce(v_daily_cap, 1000000);

  select coalesce((value ->> 'max_per_prediction')::numeric, 100000)
    into v_max_per_prediction from public.site_config where key = 'reward_caps';
  v_max_per_prediction := coalesce(v_max_per_prediction, 100000);

  select coalesce((value ->> 'max_per_wallet_day')::numeric, 10000)
    into v_max_per_wallet_day from public.site_config where key = 'reward_caps';
  v_max_per_wallet_day := coalesce(v_max_per_wallet_day, 10000);

  -- SETTLEMENT_GRACE — CONTRACTS-PREDICTIONS §3 "Settlement preconditions". Same site_config
  -- fallback pattern as the reward caps above.
  select (value ->> 'grace_minutes')::numeric
    into v_settlement_grace_minutes from public.site_config where key = 'settlement';
  v_settlement_grace := make_interval(mins => coalesce(v_settlement_grace_minutes, 10)::int);

  -- Candidates: due predictions still `locked` (claim them below) plus ones already `resolving`
  -- from a prior tick that skipped for incomplete telemetry (§3) — those must be reconsidered
  -- every run, which a plain `where status = 'locked'` claim would never do again.
  -- FOR UPDATE SKIP LOCKED makes a concurrent call safe without a separate claim statement.
  for p in
    select * from public.predictions
    where status in ('locked', 'resolving') and now() >= resolves_at
    order by resolves_at
    for update skip locked
  loop
    if p.status = 'locked' then
      update public.predictions set status = 'resolving' where id = p.id;
    end if;

    v_void_reason := null;
    v_evidence := '{}'::jsonb;
    v_result := null;

    if p.entry_count = 0 then
      v_void_reason := 'no_entries';
    end if;

    -- §3 "Settlement preconditions — telemetry completeness". The harness's offline queue can
    -- flush an in-window event AFTER resolves_at has already passed, carrying its original `ts`.
    -- Reading `events`/`sessions`/`stats` for this window is only trustworthy once we hold proof
    -- the writer has caught up past the end of the window; entry_count above is exempt because
    -- prediction_entries is written by the web API, not the harness's offline queue.
    if v_void_reason is null then
      select max(e.ts) into v_max_events_ts from public.events e where e.session_id = p.session_id;
      select s.heartbeat_at into v_heartbeat_at from public.stats s where s.session_id = p.session_id;
      v_ready := coalesce(v_max_events_ts, '-infinity'::timestamptz) >= p.resolves_at
              or coalesce(v_heartbeat_at, '-infinity'::timestamptz) >= p.resolves_at;

      if not v_ready then
        if now() < p.resolves_at + v_settlement_grace then
          -- Skip for now, free of charge. Stays in `resolving`; a later tick will look again.
          continue;
        end if;
        v_void_reason := 'telemetry_incomplete_after_grace';
        v_evidence := jsonb_build_object(
          'max_events_ts', v_max_events_ts, 'heartbeat_at', v_heartbeat_at,
          'grace_expired_at', p.resolves_at + v_settlement_grace
        );
      end if;
    end if;

    if v_void_reason is null then
      select ended_at into v_session_ended_at from public.sessions where id = p.session_id;
      if v_session_ended_at is not null and v_session_ended_at <= p.resolves_at then
        v_void_reason := 'session_ended_during_window';
        v_evidence := jsonb_build_object('session_ended_at', v_session_ended_at);
      end if;
    end if;

    if v_void_reason is null then
      select exists (
        select 1
        from public.events bd
        where bd.session_id = p.session_id
          and bd.type = 'bridge_down'
          and bd.ts <= p.resolves_at
          and (
            (select min(bu.ts) from public.events bu
             where bu.session_id = p.session_id and bu.type = 'bridge_up' and bu.ts > bd.ts) is null
            or
            (select min(bu.ts) from public.events bu
             where bu.session_id = p.session_id and bu.type = 'bridge_up' and bu.ts > bd.ts)
              >= p.locks_at
          )
      ) into v_bridge_overlap;
      if v_bridge_overlap then
        v_void_reason := 'bridge_down_overlap';
      end if;
    end if;

    if v_void_reason is null then
      v_kind := p.telemetry_rule ->> 'kind';
      v_params := p.telemetry_rule -> 'params';
      v_outcome_true := p.telemetry_rule ->> 'outcome_if_true';
      v_outcome_false := p.telemetry_rule ->> 'outcome_if_false';

      if v_kind = 'event_occurs' and coalesce(v_params ->> 'event_type', '') = '' then
        v_void_reason := 'malformed_rule';

      elsif v_kind = 'event_occurs' then
        declare
          v_ev record;
        begin
          select id, ts into v_ev from public.events
            where session_id = p.session_id and type = (v_params ->> 'event_type')
              and ts >= p.locks_at and ts <= p.resolves_at
            order by ts asc limit 1;
          if found then
            v_result := v_outcome_true;
            v_evidence := jsonb_build_object('event_id', v_ev.id, 'ts', v_ev.ts);
          else
            v_result := v_outcome_false;
          end if;
        end;

      elsif v_kind = 'wanted_reaches' and coalesce(v_params ->> 'level', '') = '' then
        v_void_reason := 'malformed_rule';

      elsif v_kind = 'wanted_reaches' then
        declare
          v_ev record;
          v_level numeric := (v_params ->> 'level')::numeric;
        begin
          select id, ts, (payload ->> 'to')::numeric as to_level into v_ev
            from public.events
            where session_id = p.session_id and type = 'wanted_change'
              and ts >= p.locks_at and ts <= p.resolves_at
              and (payload ->> 'to')::numeric >= v_level
            order by ts asc limit 1;
          if found then
            v_result := v_outcome_true;
            v_evidence := jsonb_build_object('event_id', v_ev.id, 'ts', v_ev.ts, 'to', v_ev.to_level);
          else
            v_result := v_outcome_false;
          end if;
        end;

      elsif v_kind = 'wanted_clears' then
        declare
          v_ev record;
        begin
          select id, ts into v_ev from public.events
            where session_id = p.session_id and type = 'wanted_change'
              and ts >= p.locks_at and ts <= p.resolves_at
              and (payload ->> 'to')::numeric = 0
            order by ts asc limit 1;
          if found then
            v_result := v_outcome_true;
            v_evidence := jsonb_build_object('event_id', v_ev.id, 'ts', v_ev.ts);
          else
            v_result := v_outcome_false;
          end if;
        end;

      elsif v_kind = 'wanted_gained' then
        declare
          v_ev record;
        begin
          select id, ts into v_ev from public.events
            where session_id = p.session_id and type = 'wanted_change'
              and ts >= p.locks_at and ts <= p.resolves_at
              and (payload ->> 'to')::numeric > (payload ->> 'from')::numeric
            order by ts asc limit 1;
          if found then
            v_result := v_outcome_true;
            v_evidence := jsonb_build_object('event_id', v_ev.id, 'ts', v_ev.ts);
          else
            v_result := v_outcome_false;
          end if;
        end;

      elsif v_kind = 'survives_window' then
        declare
          v_ev record;
        begin
          select id, ts, type into v_ev from public.events
            where session_id = p.session_id and type in ('death', 'busted')
              and ts >= p.locks_at and ts <= p.resolves_at
            order by ts asc limit 1;
          if found then
            v_result := v_outcome_false;
            v_evidence := jsonb_build_object('event_id', v_ev.id, 'ts', v_ev.ts, 'type', v_ev.type);
          else
            v_result := v_outcome_true;
          end if;
        end;

      elsif v_kind = 'mission_outcome' and coalesce(v_params ->> 'expect', '') = '' then
        v_void_reason := 'malformed_rule';

      elsif v_kind = 'mission_outcome' then
        declare
          v_ev record;
          v_name text := v_params ->> 'name';
          v_expect text := v_params ->> 'expect';
        begin
          select id, ts, type into v_ev from public.events
            where session_id = p.session_id and type in ('mission_end', 'mission_fail')
              and ts >= p.locks_at and ts <= p.resolves_at
              and (v_name is null or payload ->> 'name' = v_name)
            order by ts asc limit 1;
          if not found then
            v_void_reason := 'missing_telemetry';
          else
            if (v_expect = 'passed' and v_ev.type = 'mission_end')
               or (v_expect = 'failed' and v_ev.type = 'mission_fail') then
              v_result := v_outcome_true;
            else
              v_result := v_outcome_false;
            end if;
            v_evidence := jsonb_build_object('event_id', v_ev.id, 'ts', v_ev.ts, 'type', v_ev.type);
          end if;
        end;

      elsif v_kind = 'activity_outcome'
            and (coalesce(v_params ->> 'activity', '') = '' or coalesce(v_params ->> 'expect', '') = '') then
        v_void_reason := 'malformed_rule';

      elsif v_kind = 'activity_outcome' then
        declare
          v_ev record;
          v_activity text := v_params ->> 'activity';
          v_expect text := v_params ->> 'expect';
        begin
          select id, ts, payload into v_ev from public.events
            where session_id = p.session_id and type = 'activity_end'
              and ts >= p.locks_at and ts <= p.resolves_at
              and payload ->> 'activity' = v_activity
            order by ts asc limit 1;
          if not found then
            v_void_reason := 'missing_telemetry';
          else
            if v_ev.payload ->> 'outcome' = v_expect then
              v_result := v_outcome_true;
            else
              v_result := v_outcome_false;
            end if;
            v_evidence := jsonb_build_object(
              'event_id', v_ev.id, 'ts', v_ev.ts, 'outcome', v_ev.payload ->> 'outcome'
            );
          end if;
        end;

      elsif v_kind in ('vehicle_entered', 'vehicle_exited') then
        -- See the function-level comment: no discrete vehicle-transition event exists in the
        -- closed events enum, and `stats.hud.vehicle` is not time-versioned. Recognised kind,
        -- permanently un-settleable under the current telemetry schema -> always void.
        v_void_reason := 'missing_telemetry';
        v_evidence := jsonb_build_object(
          'note', 'no telemetry source for vehicle state transitions under CONTRACTS.md v1.14 §4/§5'
        );

      else
        v_void_reason := 'unknown_rule_kind';
        v_evidence := jsonb_build_object('kind', v_kind);
      end if;
    end if;

    if v_void_reason is not null then
      update public.predictions
      set status = 'void',
          resolution_evidence = jsonb_build_object('void_reason', v_void_reason) || v_evidence
      where id = p.id;
      v_settled_count := v_settled_count + 1;
      continue;
    end if;

    select count(*) into v_correct_count
      from public.prediction_entries where prediction_id = p.id and outcome = v_result;

    -- §7: floor(reward_pool / correct_count), in integer base-unit math (18dp for the reward
    -- asset). Remainder dust stays with the treasury.
    v_pool_base := floor(p.reward_pool * 1000000000000000000::numeric);
    v_effective_pool_base := least(v_pool_base, floor(v_max_per_prediction * 1000000000000000000::numeric));

    update public.predictions
    set status = 'settled', result = v_result, correct_count = v_correct_count,
        resolution_evidence = v_evidence
    where id = p.id;

    -- Streak: +1 and carry best forward for every entrant who matched the result, reset to 0 for
    -- everyone else. Runs once per prediction because this loop only ever sees a prediction once
    -- (status filter above), so re-running settlement cannot double-increment a streak.
    insert into public.wallet_streaks (wallet, current_streak, best_streak, updated_at)
    select pe.wallet,
           case when pe.outcome = v_result then 1 else 0 end,
           case when pe.outcome = v_result then 1 else 0 end,
           now()
    from public.prediction_entries pe
    where pe.prediction_id = p.id
    on conflict (wallet) do update set
      current_streak = case when excluded.current_streak = 1
                             then public.wallet_streaks.current_streak + 1 else 0 end,
      best_streak = greatest(
        public.wallet_streaks.best_streak,
        case when excluded.current_streak = 1
             then public.wallet_streaks.current_streak + 1 else 0 end
      ),
      updated_at = now();

    if v_correct_count > 0 then
      v_per_wallet_base := floor(v_effective_pool_base / v_correct_count);

      for wallet_row in
        select pe.wallet from public.prediction_entries pe
        where pe.prediction_id = p.id and pe.outcome = v_result
      loop
        -- §5: predicting is always allowed, rewarding is not. A blocked wallet still got counted
        -- above for correctness/streak/leaderboard; it just never reaches the ledger.
        select coalesce(blocked, false) into v_blocked
          from public.policy_flags where wallet = wallet_row.wallet;
        if coalesce(v_blocked, false) then
          continue;
        end if;

        select coalesce(sum(amount), 0) into v_daily_spent
          from public.reward_ledger
          where (created_at at time zone 'utc')::date = (now() at time zone 'utc')::date;
        select coalesce(sum(amount), 0) into v_wallet_day_spent
          from public.reward_ledger
          where wallet = wallet_row.wallet
            and (created_at at time zone 'utc')::date = (now() at time zone 'utc')::date;

        v_credit := v_per_wallet_base / 1000000000000000000::numeric;
        v_clamped := false;
        v_clamp_reason := null;

        if v_daily_spent + v_credit > v_daily_cap then
          v_credit := greatest(v_daily_cap - v_daily_spent, 0);
          v_clamped := true;
          v_clamp_reason := 'daily_cap';
        end if;

        if v_wallet_day_spent + v_credit > v_max_per_wallet_day then
          v_credit := greatest(v_max_per_wallet_day - v_wallet_day_spent, 0);
          v_clamped := true;
          v_clamp_reason := coalesce(v_clamp_reason || '+wallet_day_cap', 'wallet_day_cap');
        end if;

        if v_credit > 0 then
          insert into public.reward_ledger (prediction_id, wallet, amount, asset, clamped, clamp_reason)
          values (p.id, wallet_row.wallet, v_credit, p.reward_asset, v_clamped, v_clamp_reason)
          on conflict (prediction_id, wallet) do nothing;
        end if;
      end loop;
    end if;

    v_settled_count := v_settled_count + 1;
  end loop;

  return v_settled_count;
end;
$$;

-- Public leaderboard — matches `LeaderboardRow` in web/src/lib/prediction/types.ts exactly.
-- SECURITY DEFINER because it reads `prediction_entries` and `reward_ledger`, neither of which
-- has a public SELECT policy; the aggregate this returns (no raw entries, no wallet-level ledger
-- rows) is what CONTRACTS-PREDICTIONS §2 RLS means by "wallet_streaks, and the leaderboard view —
-- public select." Ranking: most correct first, then highest earned, then wallet for a stable order.
create function public.leaderboard(p_window text)
returns table (
  rank integer,
  wallet text,
  accuracy numeric,
  correct integer,
  total integer,
  current_streak integer,
  best_streak integer,
  earned numeric
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  with window_bounds as (
    select case p_window
      when 'today' then (date_trunc('day', now() at time zone 'utc') at time zone 'utc')
      when 'week' then (date_trunc('week', now() at time zone 'utc') at time zone 'utc')
      else '-infinity'::timestamptz
    end as since
  ),
  base as (
    select
      pe.wallet,
      count(*) filter (where pe.outcome = p.result) as correct,
      count(*) as total
    from public.prediction_entries pe
    join public.predictions p on p.id = pe.prediction_id
    cross join window_bounds w
    where p.status = 'settled'
      and p.settled_at is not null
      and p.settled_at >= w.since
    group by pe.wallet
  ),
  earnings as (
    select rl.wallet, sum(rl.amount) as earned
    from public.reward_ledger rl
    join public.predictions p on p.id = rl.prediction_id
    cross join window_bounds w
    where p.status = 'settled'
      and p.settled_at is not null
      and p.settled_at >= w.since
    group by rl.wallet
  )
  select
    (row_number() over (order by b.correct desc, coalesce(e.earned, 0) desc, b.wallet asc))::integer,
    b.wallet,
    round(b.correct::numeric / nullif(b.total, 0), 4),
    b.correct::integer,
    b.total::integer,
    coalesce(ws.current_streak, 0),
    coalesce(ws.best_streak, 0),
    coalesce(e.earned, 0)
  from base b
  left join earnings e on e.wallet = b.wallet
  left join public.wallet_streaks ws on ws.wallet = b.wallet
  order by 1;
$$;

-- Public participation split — matches `PredictionDistribution` in
-- web/src/lib/prediction/types.ts. `security_invoker = false` (the PG15+ default, spelled out
-- here so intent survives a future Postgres default change): this view runs as its owner, which
-- is how it can aggregate `prediction_entries` even though that table has no public SELECT
-- policy. It only ever exposes per-outcome counts, never a wallet or a raw entry row.
create view public.prediction_distribution
with (security_invoker = false)
as
select
  counts_by_outcome.prediction_id,
  jsonb_object_agg(counts_by_outcome.outcome, counts_by_outcome.cnt) as counts,
  sum(counts_by_outcome.cnt)::integer as total
from (
  select prediction_id, outcome, count(*) as cnt
  from public.prediction_entries
  group by prediction_id, outcome
) counts_by_outcome
group by counts_by_outcome.prediction_id;
