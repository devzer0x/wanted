-- Settlement row isolation, and the max_per_prediction clamp recorded — two changes to
-- settle_due_predictions(), both inside its body. CONTRACTS-PREDICTIONS v2.4 §3 (voids), §6 (caps),
-- §10.1 principle 7 (no server text leaves the server).
--
-- (a) ONE BAD ROW NO LONGER STOPS SETTLEMENT.
--
-- Before this, any error while processing one candidate aborted the whole call. Reproduced against
-- the 20260921000000 definition: a `wanted_reaches` rule with `params.level = "two"` raises 22P02
-- (invalid_text_representation) on the numeric cast; the call rolls back; the next tick picks the
-- same row first and raises again. No other due prediction settles or credits, every tick, until
-- an operator deletes the row by hand. One malformed question halted settlement for everyone.
--
-- Now each candidate, after it is claimed, runs in its own block with `exception when
-- data_exception`. PL/pgSQL runs such a block as a subtransaction: on a class-22 error its writes —
-- the `settled` status, the streak upserts, any ledger rows already inserted — are rolled back,
-- and the handler voids that one prediction as `settlement_error` and moves on.
--
--   * CLASS 22 ONLY. A category name matches every code in its class (PostgreSQL docs, "Trapping
--     Errors"). Class 22 is bad DATA — the row's own rule, or the events it reads — and settling
--     the same data again can only fail the same way, so voiding is the only way forward and is the
--     safe branch §3 always prefers. Everything else propagates exactly as before: 40001
--     serialization_failure, 40P01 deadlock, 57014 query_canceled, 55P03 lock_not_available, 53xxx
--     resources, the status guard's P0001. The RPC's transaction aborts, nothing it did commits,
--     and the next tick retries. Voiding on a transient error would cancel a winnable round for
--     good; that is why this is not `when others`.
--   * THE STATUS GUARD. predictions_guard_status_transition() allows exactly locked -> resolving,
--     resolving -> settled and resolving -> void. The claim (locked -> resolving) stays OUTSIDE the
--     block, and the block's own terminal writes are undone by its rollback, so the handler always
--     finds the row `resolving`, and resolving -> void is allowed. The guard stamps settled_at, as
--     it does for every void; it stays settled_at's only writer.
--   * THE EVIDENCE IS A CODE. `{"void_reason": "settlement_error", "sqlstate": "22P02"}` and
--     nothing else. resolution_evidence is publicly readable (predictions RLS), and the server's
--     message quotes the offending input (`invalid input syntax for type numeric: "two"`), so the
--     message never goes in it. The operator gets a WARNING carrying the prediction id and the
--     same code; the row's own telemetry_rule reproduces the rest.
--   * THE CURSOR. The loop's `FOR UPDATE SKIP LOCKED` query fetches at the head of the loop,
--     outside every per-candidate block, so its row locks belong to the enclosing transaction and
--     survive a candidate's rollback; since 9.3 Postgres also keeps a FOR UPDATE lock across a
--     rolled-back savepoint that updated the row (SELECT docs, "The Locking Clause"). A `continue`
--     inside the block leaves it normally and continues the loop. verify-predictions.sh proves both
--     with a row held FOR UPDATE by a second backend beside a poisoned row, in one run.
--   * COST. One subtransaction per candidate per tick — a handful a minute.
--
-- NOT covered, on purpose: a non-class-22 error that is nonetheless permanent. The known one is a
-- rule whose outcome_if_true/outcome_if_false is not one of the prediction's outcome keys: the
-- status guard raises P0001 on the `settled` write, and that still aborts the call every tick.
--
-- (b) THE max_per_prediction CLAMP IS RECORDED.
--
-- `v_effective_pool_base := least(v_pool_base, max_per_prediction)` already reduced the pool, but
-- the ledger rows it paid said clamped = false, clamp_reason NULL, while the daily_cap and
-- max_per_wallet_day clamps recorded themselves. §6: a clamp "is recorded on the ledger row". Now
-- every credit paid from a reduced pool carries clamped = true and clamp_reason
-- 'max_per_prediction', and the two later caps append to it with the existing pattern
-- ('max_per_prediction+daily_cap', 'max_per_prediction+wallet_day_cap', ...). The daily_cap line
-- appends instead of overwriting, which changes nothing when no pool clamp came first. A pool EQUAL
-- to the cap is not reduced, so it is not marked.
--
-- WHY THIS FILE IS A VERBATIM COPY PLUS FOUR HUNKS.
--
-- Postgres has no "patch a function" DDL. Everything below is byte-identical to the definition in
-- 20260921000000_event_matches.sql except four hunks, which
-- infra/verify-settle-row-isolation-verbatim.sh declares and requires exactly, with negative
-- controls. The body inside the new block is deliberately NOT re-indented, so that diff stays small.
--
-- GRANTS ARE NOT RESTATED, DELIBERATELY. `create or replace function` keeps the owner and ACL, and
-- this function's ACL is the product of 20260908120001, 20260909000000 (EXECUTE revoked from
-- public/anon/authenticated) and 20260914000001 (revoked from service_role, so settlement is only
-- reachable through settle_due_predictions_serialized(), which this file does not touch). A grant
-- here would hand one back. infra/verify-predictions.sql §14.5 re-asserts all of it after this
-- migration; apply-predictions-to-cloud.sh step 7 checks the same on the cloud project.
--
-- Signature, return type, language, SECURITY DEFINER and search_path are unchanged; the file is
-- re-runnable (`create or replace` only), so it belongs in apply-predictions-to-cloud.sh's REPAIR list.

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

    -- 20260922000000 — row isolation. From here to the `exception` clause just above `end loop`, a
    -- candidate runs in its own subtransaction: a DATA error (SQLSTATE class 22) in one prediction
    -- voids that prediction and the loop moves on, instead of aborting the call for every row.
    -- The claim above stays OUTSIDE the block on purpose: it is what guarantees the row is
    -- `resolving` when the handler runs, the one state the status guard lets become `void`. The
    -- lines below are deliberately not re-indented, so that the diff against 20260921000000 is
    -- exactly the hunks infra/verify-settle-row-isolation-verbatim.sh declares.
    begin

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

        -- §6, 20260922000000: the pool was already cut to max_per_prediction above, before the
        -- split, so every credit paid from a cut pool is a clamped credit and is recorded as one.
        -- The two caps below append their reasons to this one.
        if v_effective_pool_base < v_pool_base then
          v_clamped := true;
          v_clamp_reason := 'max_per_prediction';
        end if;

        if v_daily_spent + v_credit > v_daily_cap then
          v_credit := greatest(v_daily_cap - v_daily_spent, 0);
          v_clamped := true;
          v_clamp_reason := coalesce(v_clamp_reason || '+daily_cap', 'daily_cap');
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

    exception when data_exception then
      -- Class 22 ONLY (a category name matches every code in its class): bad data in this row's
      -- rule or in the events it reads, which no retry can fix. Every other class — 40001, 40P01,
      -- 57014, 55P03, 53xxx, the status guard's P0001 — still propagates and aborts the call, so a
      -- transient failure is retried by the next tick instead of voiding a winnable round.
      --
      -- The subtransaction has already undone this candidate's partial work: its ledger rows, its
      -- streak updates and any `settled` write. The row is therefore `resolving` again, and
      -- resolving -> void is allowed; the status guard stamps settled_at, as for every void.
      --
      -- The evidence carries the error CODE and nothing else. resolution_evidence is public
      -- (predictions RLS) and the server's message quotes the offending input, so it stays out
      -- (CONTRACTS-PREDICTIONS §10.1 principle 7). The warning is for the operator's log, and
      -- carries the same two facts: which prediction, which code.
      update public.predictions
      set status = 'void',
          resolution_evidence = jsonb_build_object('void_reason', 'settlement_error', 'sqlstate', sqlstate)
      where id = p.id;
      raise warning 'settle_due_predictions: prediction % voided as settlement_error, SQLSTATE %',
        p.id, sqlstate;
      v_settled_count := v_settled_count + 1;
    end;
  end loop;

  return v_settled_count;
end;
$$;
