-- WANTED prediction layer — assertions against REAL recorded gameplay.
--
-- Every window below was chosen from ground truth computed off the recording, not invented:
--   * 2026-09-04T06:20:00Z .. 06:23:00Z  contains NO death and NO arrest  -> survival = yes
--   * 2026-09-04T08:41:00Z .. 08:45:00Z  contains TWO real deaths
--                                        (08:42:33.680272Z, 08:44:38.543808Z) -> survival = no
--   * 2026-09-04T01:03:00Z .. 01:06:00Z  contains a real wanted_change {"from":2,"to":0}
--                                        at 01:04:37.719774Z -> cops cleared = yes
--
-- If settlement disagrees with any of these, it disagrees with something that actually happened.
\set ON_ERROR_STOP on
\timing off

\echo ''
\echo '################ SETUP ################'

-- Generous caps so the reward assertions test the §7 formula, not the §6 clamps.
insert into public.site_config (key, value) values
  ('reward_caps', '{"daily_cap": 100000, "max_per_prediction": 100000, "max_per_wallet_day": 100000}'::jsonb),
  ('settlement',  '{"grace_minutes": 10}'::jsonb)
on conflict (key) do update set value = excluded.value;

create temporary table t (name text primary key, id uuid);

-- Three predictions over the real recording. reward_pool is in WHOLE token units.
with s as (select id from public.sessions limit 1)
insert into public.predictions
  (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
   outcomes, telemetry_rule, reward_pool, reward_asset)
select s.id, v.q, v.ptype, v.o, v.l, v.r,
       '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
       v.rule::jsonb, 10, 'TTWO'
from s, (values
  ('survive_yes', 'WILL WANTED SURVIVE THE NEXT 3 MINUTES?', 'survives',
   timestamptz '2026-09-04T06:19:00Z', timestamptz '2026-09-04T06:20:00Z', timestamptz '2026-09-04T06:23:00Z',
   '{"kind":"survives_window","outcome_if_true":"yes","outcome_if_false":"no"}'),
  ('survive_no',  'WILL WANTED SURVIVE THE NEXT 4 MINUTES?', 'survives',
   timestamptz '2026-09-04T08:40:00Z', timestamptz '2026-09-04T08:41:00Z', timestamptz '2026-09-04T08:45:00Z',
   '{"kind":"survives_window","outcome_if_true":"yes","outcome_if_false":"no"}'),
  ('clears_cops', 'WILL WANTED LOSE THE COPS?', 'wanted_clear',
   timestamptz '2026-09-04T01:02:00Z', timestamptz '2026-09-04T01:03:00Z', timestamptz '2026-09-04T01:06:00Z',
   '{"kind":"wanted_clears","outcome_if_true":"yes","outcome_if_false":"no"}')
) as v(name, q, ptype, o, l, r, rule);

insert into t(name, id)
select p.prediction_type || '|' || p.locks_at::text, p.id from public.predictions p;

-- Name them for readability.
create temporary view px as
  select case
    when locks_at = timestamptz '2026-09-04T06:20:00Z' then 'survive_yes'
    when locks_at = timestamptz '2026-09-04T08:41:00Z' then 'survive_no'
    when locks_at = timestamptz '2026-09-04T01:03:00Z' then 'clears_cops'
  end as name, *
  from public.predictions;

\echo ''
\echo '################ 1. ENTRIES + THE SERVER-SIDE LOCK ################'
\echo '-- NOTE: the three predictions above sit over REAL gameplay from 2026-09-04, so their'
\echo '--       locks_at is already in the past. enter_prediction() therefore refuses them, which'
\echo '--       is the lock behaving correctly. Entries for the settlement tests are inserted'
\echo '--       directly (settlement is what is under test there); enter_prediction() itself is'
\echo '--       tested separately below against a prediction whose window is still open.'

insert into public.prediction_entries (prediction_id, wallet, outcome)
select p.id, w.wallet, w.outcome
from px p, (values ('0xaaa1','yes'), ('0xbbb2','no'), ('0xccc3','yes')) as w(wallet, outcome);

\echo '-- EXPECT 3 per prediction (maintained by trigger, never by a client)'
select p.name, p.entry_count from px p order by p.name;

\echo '-- EXPECT ERROR: duplicate (prediction_id, wallet) — idempotency invariant 1'
\set ON_ERROR_STOP off
insert into public.prediction_entries (prediction_id, wallet, outcome)
select id, '0xaaa1', 'no' from px where name = 'survive_yes';
\set ON_ERROR_STOP on

-- A prediction that is genuinely OPEN right now, to exercise enter_prediction() properly.
with s as (select id from public.sessions limit 1)
insert into public.predictions
  (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
   outcomes, telemetry_rule, reward_pool, reward_asset)
select s.id, 'LIVE LOCK TEST', 'locktest',
  now() - interval '10 seconds', now() + interval '1 hour', now() + interval '2 hours',
  '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
  '{"kind":"survives_window","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb, 10, 'TTWO'
from s;

\echo '-- EXPECT t: first entry into a still-open prediction succeeds'
select public.enter_prediction(id, '0xaaa1', 'yes') as must_be_true
from public.predictions where prediction_type = 'locktest';
\echo '-- EXPECT f: the same wallet twice'
select public.enter_prediction(id, '0xaaa1', 'no') as must_be_false
from public.predictions where prediction_type = 'locktest';
\echo '-- EXPECT f: an outcome key that is not in `outcomes`'
select public.enter_prediction(id, '0xddd4', 'maybe') as must_be_false
from public.predictions where prediction_type = 'locktest';

\echo '-- Now slam the lock shut and retry. EXPECT f: no entry is possible after locks_at,'
\echo '--       decided by Postgres now(), not by any client clock.'
update public.predictions set locks_at = now() - interval '1 second',
                              resolves_at = now() + interval '1 hour'
 where prediction_type = 'locktest';
select public.enter_prediction(id, '0xeee9', 'yes') as must_be_false
from public.predictions where prediction_type = 'locktest';

\echo ''
\echo '################ 2. SETTLEMENT AGAINST REAL GAMEPLAY ################'

select public.lock_due_predictions() as locked;
select public.settle_due_predictions() as settled;

\echo '-- EXPECT: survive_yes = yes | survive_no = no | clears_cops = yes'
select p.name, p.status, p.result, p.correct_count from px p order by p.name;

\echo '-- EXPECT: survive_no cites a REAL death event, and its ts is one of the two known deaths'
select p.name,
       p.resolution_evidence ->> 'type' as evidence_type,
       p.resolution_evidence ->> 'ts'   as evidence_ts,
       (p.resolution_evidence ->> 'ts')::timestamptz
         in (timestamptz '2026-09-04T08:42:33.680272Z',
             timestamptz '2026-09-04T08:44:38.543808Z') as ts_is_a_real_recorded_death
from px p where p.name = 'survive_no';

\echo '-- EXPECT: clears_cops cites the real wanted_change at 01:04:37.719774Z'
select p.name, p.resolution_evidence from px p where p.name = 'clears_cops';

\echo ''
\echo '################ 3. REWARDS — §7 formula, whole token units ################'
\echo '-- EXPECT: pool 10 / 2 correct = 5.000000000000000000 each for survive_yes (aaa1, ccc3)'
\echo '--         and a single credit of 10 for survive_no (bbb2 alone was right)'
select l.wallet, l.amount, l.asset, p.name
from public.reward_ledger l join px p on p.id = l.prediction_id
order by p.name, l.wallet;

\echo ''
\echo '################ 4. IDEMPOTENCY — settlement is safely re-runnable ################'
select count(*) as ledger_rows_before from public.reward_ledger;
select public.settle_due_predictions() as second_run_should_settle_0;
select public.settle_due_predictions() as third_run_should_settle_0;
\echo '-- EXPECT: identical count, and never more than one ledger row per (prediction, wallet)'
select count(*) as ledger_rows_after from public.reward_ledger;
select coalesce(max(c), 0) as max_rows_for_any_prediction_wallet_pair_must_be_1
from (select count(*) c from public.reward_ledger group by prediction_id, wallet) z;

\echo ''
\echo '################ 5. STREAKS ################'
\echo '-- EXPECT: aaa1 and ccc3 correct once then wrong once; bbb2 wrong then correct'
select wallet, current_streak, best_streak from public.wallet_streaks order by wallet;

\echo ''
\echo '################ 6. VOID PATHS ################'
\echo '-- EXPECT: a prediction with zero entries voids as no_entries and credits nobody'
with s as (select id from public.sessions limit 1)
insert into public.predictions
  (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
   outcomes, telemetry_rule, reward_pool, reward_asset)
select s.id, 'NOBODY ANSWERED THIS', 'survives',
  timestamptz '2026-09-04T06:19:00Z', timestamptz '2026-09-04T06:20:00Z', timestamptz '2026-09-04T06:23:00Z',
  '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
  '{"kind":"survives_window","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb, 10, 'TTWO'
from s;

\echo '-- EXPECT: an unrecognised rule kind voids rather than guessing'
with s as (select id from public.sessions limit 1)
insert into public.predictions
  (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
   outcomes, telemetry_rule, reward_pool, reward_asset)
select s.id, 'WILL SOMETHING UNMODELLED HAPPEN?', 'bogus',
  timestamptz '2026-09-04T06:19:00Z', timestamptz '2026-09-04T06:20:00Z', timestamptz '2026-09-04T06:23:00Z',
  '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
  '{"kind":"reads_his_mind","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb, 10, 'TTWO'
from s;

-- Direct insert: the window is in the past, so enter_prediction would (correctly) refuse, and
-- a zero-entry prediction voids as `no_entries` BEFORE the rule kind is ever inspected — which
-- would have made this assertion silently vacuous.
insert into public.prediction_entries (prediction_id, wallet, outcome)
select id, '0xeee5', 'yes' from public.predictions where prediction_type = 'bogus';
select public.lock_due_predictions() as locked;
select public.settle_due_predictions() as settled;
\echo '-- EXPECT: no_entries for the unanswered one, and an UNKNOWN-RULE void (not a guessed'
\echo '--         outcome) for the one whose kind the engine does not recognise'
select question, status, result, entry_count,
       resolution_evidence ->> 'void_reason' as void_reason
from public.predictions where prediction_type in ('bogus') or question = 'NOBODY ANSWERED THIS';

\echo ''
\echo '################ 7. CLAIMS — atomic, capped, replay-safe ################'
\echo '-- EXPECT: one claim for aaa1 covering its whole unclaimed balance, ledger rows attached'
select id, wallet, amount, asset, status from public.create_reward_claim('0xaaa1');
select count(*) as aaa1_rows_still_claimable
from public.reward_ledger where wallet = '0xaaa1' and claim_id is null;

\echo '-- A claim is now in flight for aaa1. Credit it again so there IS a fresh balance —'
\echo '-- otherwise the next call would fail with "nothing to claim" and never reach the'
\echo '-- partial unique index, which is the actual double-spend guard under test.'
insert into public.reward_ledger (prediction_id, wallet, amount, asset)
select id, '0xaaa1', 4, 'TTWO' from public.predictions where prediction_type = 'locktest';
\echo '-- EXPECT: ERROR 23505 — one in-flight claim per wallet (idempotency invariant 3)'
\set ON_ERROR_STOP off
select public.create_reward_claim('0xaaa1');
\set ON_ERROR_STOP on

\echo '-- EXPECT: ERROR P0002 — nothing to claim for a wallet with no credits'
\set ON_ERROR_STOP off
select public.create_reward_claim('0xnobody');
\set ON_ERROR_STOP on

\echo '-- EXPECT: ERROR P0003 — below the minimum claim amount'
\set ON_ERROR_STOP off
select public.create_reward_claim('0xbbb2', 999999, null);
\set ON_ERROR_STOP on

\echo '-- EXPECT: a FAILED claim returns the credits to the claimable pool (nothing is burned)'
select public.finalize_reward_claim(
  (select id from public.reward_claims where wallet = '0xaaa1'), 'failed', null, 'simulated rpc failure');
select count(*) as aaa1_rows_claimable_again
from public.reward_ledger where wallet = '0xaaa1' and claim_id is null;

\echo '-- EXPECT: aaa1 can now claim again, and confirming is terminal'
select id, amount, status from public.create_reward_claim('0xaaa1');
select status, tx_hash from public.finalize_reward_claim(
  (select id from public.reward_claims where wallet='0xaaa1' and status='pending'),
  'confirmed', '0xdeadbeef', null);
\echo '-- EXPECT: ERROR P0002 — a confirmed claim cannot be reopened by a late callback'
\set ON_ERROR_STOP off
select public.finalize_reward_claim(
  (select id from public.reward_claims where wallet='0xaaa1' and status='confirmed'),
  'failed', null, 'late callback');
\set ON_ERROR_STOP on

\echo ''
\echo '################ 8. LEADERBOARD + DISTRIBUTION ################'
select * from public.leaderboard('all') order by rank limit 5;
\echo '-- EXPECT: counts only, no wallet addresses exposed'
select * from public.prediction_distribution limit 5;

\echo ''
\echo '################ 9. RLS — what the browser can and cannot reach ################'
set role anon;
\echo '-- EXPECT: readable'
select count(*) as anon_can_read_predictions from public.predictions;
select count(*) as anon_can_read_distribution from public.prediction_distribution;
\echo '-- EXPECT: permission denied on every reward/auth table'
\set ON_ERROR_STOP off
select count(*) from public.reward_ledger;
select count(*) from public.reward_claims;
select count(*) from public.wallet_sessions;
select public.enter_prediction('00000000-0000-0000-0000-000000000000'::uuid, '0xaaa1', 'yes');
select public.create_reward_claim('0xaaa1');
select public.settle_due_predictions();
\set ON_ERROR_STOP on
reset role;

\echo ''
\echo '################ DONE ################'
