-- CONTRACTS-PREDICTIONS v2.4 §3 — one new settlement rule kind: `event_matches`.
--
-- `event_occurs` plus a payload filter: an `events` row of `event_type` inside [locks_at,
-- resolves_at] whose `payload` CONTAINS every key/value of `payload_match` (`payload @>
-- payload_match`). Found -> outcome_if_true, with the event id, its ts and the filter recorded as
-- evidence; not found -> outcome_if_false, exactly as absence works for `event_occurs`.
--
-- WHY A NEW KIND RATHER THAN A NEW PARAM ON `event_occurs`.
--
-- The harness deploys separately from this database, so a harness ahead of the database is normal
-- for minutes at a time (and permanent if this migration is never applied). Whatever the new rule
-- looks like, old SQL will read it. Those are the only two options:
--
--   * a new PARAM on `event_occurs`: old SQL matches `v_kind = 'event_occurs'`, ignores the
--     unknown `payload_match` key, and settles on ANY event of that type. "Will he finish this
--     job?" then pays YES to everyone the moment any `activity_end` lands — and 2,474 of the 2,668
--     `activity_end` rows in the 2026-09-04 recording (93 %) are NOT `completed`. Wrong, paid, and
--     irreversible: settlement never revisits a settled prediction, and the ledger row is written.
--   * a new KIND: old SQL falls through every `elsif` to `unknown_rule_kind` and VOIDS. Nobody is
--     paid, nobody is told a falsehood, and the operator sees a void reason naming the kind.
--
-- Fail closed is the whole reason this is `event_matches` and not `event_occurs` with a filter.
--
-- WHY THIS FILE IS A VERBATIM COPY PLUS ONE BRANCH.
--
-- Postgres has no "add a branch" DDL: the only way to change a plpgsql function is to submit its
-- whole source again. Everything below except the added branch is byte-identical to the current
-- definition in 20260908120000_predictions.sql (its own long comment block on the heartbeat-gap
-- gap and the liveness signals still stands and is not repeated here). The diff that proves it is
-- in the verification output for this change.
--
-- GRANTS ARE NOT RESTATED, DELIBERATELY. `create or replace function` keeps the existing owner and
-- ACL, and the ACL on this function is the product of three earlier migrations: 20260908120001 and
-- 20260909000000 revoked EXECUTE from public/anon/authenticated, and 20260914000001 revoked it from
-- service_role so settlement is only reachable through settle_due_predictions_serialized(). Adding
-- a `grant` here would hand one of those back. infra/verify-predictions.sql §11 and §13 re-assert
-- all three AFTER this migration has been applied, which is what proves the ACL survived the
-- replace; apply-predictions-to-cloud.sh checks the same thing against the cloud project (step 7).
--
-- Signature, return type, language, SECURITY DEFINER and `search_path` are unchanged; this file is
-- re-runnable (`create or replace` only), so it belongs in apply-predictions-to-cloud.sh's REPAIR
-- list.

create or replace function public.settle_due_predictions()
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

      elsif v_kind = 'event_matches'
            and (coalesce(v_params ->> 'event_type', '') = ''
                 or jsonb_typeof(v_params -> 'payload_match') is distinct from 'object'
                 or v_params -> 'payload_match' = '{}'::jsonb) then
        -- An absent, non-object or EMPTY filter is `event_occurs` wearing this kind's name, and
        -- would settle every such question YES on the first event of that type. Void instead.
        v_void_reason := 'malformed_rule';

      elsif v_kind = 'event_matches' then
        declare
          v_ev record;
        begin
          -- `payload @> payload_match` is jsonb containment: every key/value of the filter must be
          -- present in the recorded payload; extra keys in the payload are ignored. Absence is a
          -- definite outcome_if_false, exactly as for `event_occurs`.
          select id, ts into v_ev from public.events
            where session_id = p.session_id and type = (v_params ->> 'event_type')
              and ts >= p.locks_at and ts <= p.resolves_at
              and payload @> (v_params -> 'payload_match')
            order by ts asc limit 1;
          if found then
            v_result := v_outcome_true;
            v_evidence := jsonb_build_object('event_id', v_ev.id, 'ts', v_ev.ts,
                                             'payload_match', v_params -> 'payload_match');
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
