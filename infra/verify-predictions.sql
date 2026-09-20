-- WANTED prediction layer — assertions against REAL recorded gameplay.
--
-- Every window below was chosen from ground truth computed off the recording, not invented:
--   * 2026-09-04T06:20:00Z .. 06:23:00Z  contains NO death and NO arrest  -> survival = yes
--   * 2026-09-04T08:41:00Z .. 08:45:00Z  contains TWO real deaths
--                                        (08:42:33.680272Z, 08:44:38.543808Z) -> survival = no
--   * 2026-09-04T01:03:00Z .. 01:06:00Z  contains a real wanted_change {"from":2,"to":0}
--                                        at 01:04:37.719774Z -> cops cleared = yes
--   * 2026-09-04T02:52:00Z .. 02:55:00Z  three real activity_end rows: timeout, then TWO completed
--                                        (02:53:16.913902Z, 02:54:21.002800Z)
--                                        -> event_matches {"outcome":"completed"} = yes, citing the
--                                        SECOND row in the window, not the first
--   * 2026-09-04T01:15:00Z .. 01:18:00Z  three real activity_end rows, NONE completed (gave_up,
--                                        preempted, timeout) -> the same rule = no
--
-- If settlement disagrees with any of these, it disagrees with something that actually happened.
\set ON_ERROR_STOP on
\timing off

\echo ''
\echo '################ SETUP ################'

-- Generous caps so the reward assertions test the §7 formula, not the §6 clamps.
--
-- `rewards` is the master switch (20260909010000). It defaults to OFF when the row is absent, so
-- without this line every reward assertion below would correctly find an empty ledger. Section 10
-- turns it back off on purpose and proves that is what happens.
insert into public.site_config (key, value) values
  ('reward_caps', '{"daily_cap": 100000, "max_per_prediction": 100000, "max_per_wallet_day": 100000}'::jsonb),
  ('settlement',  '{"grace_minutes": 10}'::jsonb),
  ('rewards',     '{"enabled": true}'::jsonb)
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
select id, '0xaaa1', 'no' from public.predictions where question = 'WILL WANTED SURVIVE THE NEXT 3 MINUTES?';
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
\echo '################ 7. CLAIMS — enqueue only (20260914100000, CONTRACTS §10.3) ################'
\echo '-- EXPECT ERROR P0005 — payouts default to OFF: with no rewards.payouts, nothing can be claimed'
\set ON_ERROR_STOP off
select * from public.create_reward_claim('0xaaa1', 'TTWO', 0.001, 1000);
\set ON_ERROR_STOP on

update public.site_config set value = value || '{"payouts": true}'::jsonb where key = 'rewards';
select public.payouts_enabled() as payouts_on_must_be_t;

\echo '-- EXPECT: one QUEUED claim for aaa1 covering its whole unclaimed TTWO balance (5 + 5 = 10),'
\echo '--         the amount returned as TEXT (FM-08), its credits attached in the same transaction'
select wallet, amount_text, pg_typeof(amount_text) as amount_type_must_be_text, asset, status
from public.create_reward_claim('0xaaa1', 'TTWO', 0.001, 1000);
select count(*) as aaa1_rows_still_claimable_must_be_0
from public.reward_ledger where wallet = '0xaaa1' and claim_id is null;
select c.amount = sum(l.amount) as attached_sum_equals_claim_amount_must_be_t
from public.reward_claims c join public.reward_ledger l on l.claim_id = c.id
where c.wallet = '0xaaa1' group by c.amount;

\echo '-- A claim is now in flight for aaa1. Credit it again so there IS a fresh balance —'
\echo '-- otherwise the next call would fail with "nothing to claim" and never reach the'
\echo '-- partial unique index, which is the actual double-spend guard under test.'
insert into public.reward_ledger (prediction_id, wallet, amount, asset)
select id, '0xaaa1', 4, 'TTWO' from public.predictions where prediction_type = 'locktest';
\echo '-- EXPECT ERROR 23505 — one in-flight claim per wallet (idempotency invariant 3)'
\set ON_ERROR_STOP off
select * from public.create_reward_claim('0xaaa1', 'TTWO', 0.001, 1000);
\set ON_ERROR_STOP on

\echo '-- EXPECT ERROR P0002 — nothing to claim for a wallet with no credits'
\set ON_ERROR_STOP off
select * from public.create_reward_claim('0xnobody', 'TTWO', 0.001, 1000);
\set ON_ERROR_STOP on

\echo '-- EXPECT ERROR P0003 — below the minimum claim amount'
\set ON_ERROR_STOP off
select * from public.create_reward_claim('0xbbb2', 'TTWO', 999999, 9999999);
\set ON_ERROR_STOP on

\echo '-- EXPECT ERROR 22023 — the limits are REQUIRED now; null used to mean "no limit" (FM-11/13)'
\set ON_ERROR_STOP off
select * from public.create_reward_claim('0xbbb2', 'TTWO', null, null);
\set ON_ERROR_STOP on

\echo '-- EXPECT ERROR P0004 — bbb2''s only credit (10) is larger than a maximum of 5'
\set ON_ERROR_STOP off
select * from public.create_reward_claim('0xbbb2', 'TTWO', 0.001, 5);
\set ON_ERROR_STOP on

\echo '-- EXPECT ERROR P0006 — a blocked wallet keeps its credits on the books but cannot withdraw (FM-06)'
insert into public.policy_flags (wallet, blocked) values ('0xbbb2', true);
\set ON_ERROR_STOP off
select * from public.create_reward_claim('0xbbb2', 'TTWO', 0.001, 1000);
\set ON_ERROR_STOP on
delete from public.policy_flags where wallet = '0xbbb2';

-- Credits below are keyed on the SETUP predictions' question text, which is unique. Not on px.name:
-- px maps every prediction sharing a window to one name, so px keys insert one credit PER match.
\echo '-- Greedy PREFIX under the maximum (FM-14): credits of 3, 4, 5, oldest first, and a max of 7.5.'
\echo '-- EXPECT the claim to take 3 + 4 = 7 and leave the 5 claimable for a later claim — not refuse all.'
insert into public.reward_ledger (prediction_id, wallet, amount, asset) select id, '0xddd4', 3, 'TTWO' from public.predictions where question = 'WILL WANTED SURVIVE THE NEXT 3 MINUTES?';
insert into public.reward_ledger (prediction_id, wallet, amount, asset) select id, '0xddd4', 4, 'TTWO' from public.predictions where question = 'WILL WANTED SURVIVE THE NEXT 4 MINUTES?';
insert into public.reward_ledger (prediction_id, wallet, amount, asset) select id, '0xddd4', 5, 'TTWO' from public.predictions where question = 'WILL WANTED LOSE THE COPS?';
select amount_text as ddd4_claim_must_be_7 from public.create_reward_claim('0xddd4', 'TTWO', 0.001, 7.5);
select count(*) as ddd4_rows_attached_must_be_2 from public.reward_ledger where wallet = '0xddd4' and claim_id is not null;
select amount as ddd4_left_claimable_must_be_5 from public.reward_ledger where wallet = '0xddd4' and claim_id is null;

\echo '-- Only the asked-for asset is claimable (FM-14/16). EXPECT ERROR P0002: fff8 holds only OTHER.'
insert into public.reward_ledger (prediction_id, wallet, amount, asset) select id, '0xfff8', 1, 'OTHER' from public.predictions where question = 'WILL WANTED SURVIVE THE NEXT 3 MINUTES?';
\set ON_ERROR_STOP off
select * from public.create_reward_claim('0xfff8', 'TTWO', 0.001, 1000);
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
select * from public.create_reward_claim('0xaaa1', 'TTWO', 0.001, 1000);
select public.settle_due_predictions();
\set ON_ERROR_STOP on
reset role;

\echo ''
\echo '################ 10. REWARDS MASTER SWITCH — the credit gate, not just the claim gate ####'
\echo '-- The regression this section exists for: REWARDS_ENABLED is a Vercel env var, and'
\echo '--   settle_due_predictions() is a Postgres function that cannot read it. Rewards being'
\echo '--   "off" blocked claiming and did nothing whatsoever to crediting.'

\echo '-- EXPECT t: on, because SETUP turned it on'
select public.rewards_enabled() as must_be_true;

\echo '-- EXPECT f: an ABSENT row means off. A missing config must never mean pay out.'
delete from public.site_config where key = 'rewards';
select public.rewards_enabled() as must_be_false_when_row_absent;

\echo '-- EXPECT f: and an explicit false means off'
insert into public.site_config (key, value) values ('rewards', '{"enabled": false}'::jsonb)
on conflict (key) do update set value = excluded.value;
select public.rewards_enabled() as must_be_false;

-- A fresh prediction over the SAME real window as survive_yes: 06:20:00Z..06:23:00Z contains no
-- death in the recording, so it resolves `yes` and 0xaaa1 is correct. Everything about it is
-- identical to the prediction that credited 5 TTWO in section 3 except the state of the switch.
with s as (select id from public.sessions limit 1)
insert into public.predictions
  (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
   outcomes, telemetry_rule, reward_pool, reward_asset)
select s.id, 'SWITCH WITNESS — SAME WINDOW AS survive_yes', 'switchtest',
  timestamptz '2026-09-04T06:19:00Z', timestamptz '2026-09-04T06:20:00Z',
  timestamptz '2026-09-04T06:23:00Z',
  '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
  '{"kind":"survives_window","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb, 10, 'TTWO'
from s;
insert into public.prediction_entries (prediction_id, wallet, outcome)
select id, w.wallet, w.outcome from public.predictions,
  (values ('0xaaa1','yes'), ('0xbbb2','no')) as w(wallet, outcome)
where prediction_type = 'switchtest';

select public.lock_due_predictions() as locked;
select public.settle_due_predictions() as settled;

\echo '-- EXPECT: settled | yes | 1 — the GAME still runs with rewards off. Resolution, results,'
\echo '--         correctness and streaks are unaffected; only the money is withheld.'
select status, result, correct_count from public.predictions where prediction_type = 'switchtest';

\echo '-- EXPECT 0: THE WITNESS. Same window, same pool, same correct wallet as the prediction'
\echo '--         that paid 5 TTWO in section 3 — and no ledger row, because the switch is off.'
select count(*) as ledger_rows_for_switchtest_must_be_0
from public.reward_ledger l
join public.predictions p on p.id = l.prediction_id
where p.prediction_type = 'switchtest';

\echo '-- EXPECT 0: and the streak DID advance for the correct wallet, proving the suppression is'
\echo '--         at the ledger and not a settlement that bailed out early.'
select current_streak > 0 as aaa1_streak_still_advanced
from public.wallet_streaks where wallet = '0xaaa1';

\echo '-- Turn it back on. EXPECT 0 still: the switch DROPS a credit, it does not defer one.'
\echo '--         Settlement never revisits a settled prediction, so enabling rewards later does'
\echo '--         not backfill. That is deliberate — rewards are off while the treasury is'
\echo '--         unfunded, and accruing an invisible liability to pay out later is the opposite'
\echo '--         of what off should mean.'
insert into public.site_config (key, value) values ('rewards', '{"enabled": true}'::jsonb)
on conflict (key) do update set value = excluded.value;
select public.settle_due_predictions() as settled_again_should_be_0;
select count(*) as ledger_rows_for_switchtest_still_0
from public.reward_ledger l
join public.predictions p on p.id = l.prediction_id
where p.prediction_type = 'switchtest';

\echo '-- EXPECT: the switch never touches an UPDATE. Releasing a claim''s credits sets claim_id = null,'
\echo '--         and an operator flipping rewards off mid-payout must not be able to destroy a balance'
\echo '--         that already exists. (Payouts stay on: crediting and paying are separate switches.)'
insert into public.site_config (key, value) values ('rewards', '{"enabled": false, "payouts": true}'::jsonb)
on conflict (key) do update set value = excluded.value;
select amount_text, status from public.create_reward_claim('0xccc3', 'TTWO', 0.001, 1000);
select public.cancel_queued_claim(
  (select id from public.reward_claims where wallet = '0xccc3' and status = 'queued'));
\echo '-- EXPECT > 0: ccc3 got its credits back even with the switch off'
select count(*) as ccc3_rows_returned_to_claimable
from public.reward_ledger where wallet = '0xccc3' and claim_id is null;
insert into public.site_config (key, value) values ('rewards', '{"enabled": true}'::jsonb)
on conflict (key) do update set value = excluded.value;

\echo ''
\echo '################ 11. SETTLEMENT GUARDS (20260914000001) ################'
\echo '-- EXPECT t: SETUP has rewards on AND real caps'
select public.rewards_enabled() as must_be_true_with_real_caps;

\echo '-- EXPECT f: rewards on but NO reward_caps row. Before this migration that meant settlement'
\echo '--         credited against a 1,000,000/day placeholder. Now it means no credits at all.'
delete from public.site_config where key = 'reward_caps';
select public.rewards_enabled() as must_be_false_without_caps;

\echo '-- EXPECT f: a misspelled cap key ("dailyCap") fails closed instead of uncapping'
insert into public.site_config (key, value) values
  ('reward_caps', '{"dailyCap": 30, "max_per_prediction": 0.02, "max_per_wallet_day": 2}'::jsonb);
select public.rewards_enabled() as must_be_false_with_misspelled_cap;

\echo '-- EXPECT f: a cap given as a STRING fails closed, and does not raise'
update public.site_config set value = '{"daily_cap": "30", "max_per_prediction": 0.02, "max_per_wallet_day": 2}'::jsonb
 where key = 'reward_caps';
select public.rewards_enabled() as must_be_false_with_string_cap;

\echo '-- EXPECT f: a zero cap fails closed'
update public.site_config set value = '{"daily_cap": 0, "max_per_prediction": 0.02, "max_per_wallet_day": 2}'::jsonb
 where key = 'reward_caps';
select public.rewards_enabled() as must_be_false_with_zero_cap;

\echo '-- EXPECT f, NO ERROR: a non-boolean switch value. The 20260909010000 version RAISED here'
\echo '--         (a cast error, not a NULL), which would have aborted every settlement.'
update public.site_config set value = '{"daily_cap": 30, "max_per_prediction": 0.02, "max_per_wallet_day": 2}'::jsonb
 where key = 'reward_caps';
update public.site_config set value = '{"enabled": "maybe"}'::jsonb where key = 'rewards';
select public.rewards_enabled() as must_be_false_not_an_error;
update public.site_config set value = '{"enabled": "true"}'::jsonb where key = 'rewards';
select public.rewards_enabled() as string_true_is_not_boolean_true_must_be_false;

\echo '-- EXPECT t: the real decided caps, with a real boolean'
update public.site_config set value = '{"enabled": true}'::jsonb where key = 'rewards';
select public.rewards_enabled() as must_be_true_with_decided_caps;

\echo '-- EXPECT: service_role can reach settlement ONLY through the serialising wrapper'
select has_function_privilege('service_role', 'public.settle_due_predictions_serialized()', 'execute')
         as service_role_can_call_wrapper_must_be_t,
       has_function_privilege('service_role', 'public.settle_due_predictions()', 'execute')
         as service_role_can_call_raw_must_be_f,
       has_function_privilege('anon', 'public.settle_due_predictions_serialized()', 'execute')
         as anon_can_call_wrapper_must_be_f;

\echo '-- EXPECT 0 from a run that finds the lock held by another run (the concurrency case the'
\echo '--         wrapper exists for; the holder is a second backend, started by verify-predictions.sh).'
select public.settle_due_predictions_serialized() as uncontended_run_returns_normally;

\echo ''
\echo '################ 12. PAYOUT OUTBOX — the worker path, in SQL (20260914100000) ################'
\echo '-- SQL has no keccak256 and no RLP decoder, so raw_tx / tx_hash below are obviously synthetic hex.'
\echo '-- That a real hash IS keccak256 of a real signed transfer is proven on a chain fork by'
\echo '-- web/scripts/verify-payout.mjs (invariant I5). This section proves the DATABASE rules.'
\set tok 0x5e81213613b6b86eab4c6c50d718d34359459786
\set treasury 0xabababababababababababababababababababab
\set w1 0x1111111111111111111111111111111111111111
\set w2 0x2222222222222222222222222222222222222222
\set w3 0x3333333333333333333333333333333333333333
\set ha aaaaaaaa-0000-4000-8000-00000000000a
\set hb bbbbbbbb-0000-4000-8000-00000000000b
\set hc cccccccc-0000-4000-8000-00000000000c
update public.site_config set value = value || '{"payouts": true}'::jsonb where key = 'rewards';
insert into public.reward_ledger (prediction_id, wallet, amount, asset) select id, :'w1', 1.5, 'TTWO' from public.predictions where question = 'WILL WANTED SURVIVE THE NEXT 3 MINUTES?';
insert into public.reward_ledger (prediction_id, wallet, amount, asset) select id, :'w1', 1, 'TTWO' from public.predictions where question = 'WILL WANTED SURVIVE THE NEXT 4 MINUTES?';
insert into public.reward_ledger (prediction_id, wallet, amount, asset) select id, :'w2', 2, 'TTWO' from public.predictions where question = 'WILL WANTED LOSE THE COPS?';
insert into public.reward_ledger (prediction_id, wallet, amount, asset) select id, :'w3', 1, 'TTWO' from public.predictions where question = 'WILL WANTED SURVIVE THE NEXT 3 MINUTES?';

\echo '-- The lease: one holder at a time; renewal by the same holder is allowed'
select public.acquire_payout_lease(:'ha', 60) as holder_a_gets_lease_must_be_t;
select public.acquire_payout_lease(:'hb', 60) as holder_b_refused_must_be_f;
select public.acquire_payout_lease(:'ha', 60) as holder_a_renews_must_be_t;
\echo '-- EXPECT ERROR P0010 — every worker write fences on the lease, and B does not hold it'
\set ON_ERROR_STOP off
select public.init_treasury_account(:'hb', :'treasury', 7);
\set ON_ERROR_STOP on
select public.init_treasury_account(:'ha', :'treasury', 7) as next_nonce_must_be_7;
select jsonb_typeof(public.payout_snapshot(:'treasury') -> 'account' -> 'next_nonce') as next_nonce_json_type_must_be_string,
       jsonb_typeof(public.payout_snapshot(:'treasury') -> 'liability_text') as liability_json_type_must_be_string;

\echo '-- c1: w1 claims with a max of 1.6 -> takes only the oldest credit (1.5)'
select id as c1 from public.create_reward_claim(:'w1', 'TTWO', 0.001, 1.6) \gset
select public.assign_claim_nonce(:'ha', :'c1', :'treasury') as c1_nonce_must_be_7;
select public.assign_claim_nonce(:'ha', :'c1', :'treasury') as c1_nonce_reused_must_be_7;
select next_nonce as next_nonce_must_be_8 from public.treasury_accounts where address = :'treasury';
select id as d1 from public.create_reward_claim(:'w2', 'TTWO', 0.001, 1000) \gset
\echo '-- EXPECT ERROR P0012 — strictly serial: d1 cannot get a nonce while c1 holds one (FM-09)'
\set ON_ERROR_STOP off
select public.assign_claim_nonce(:'ha', :'d1', :'treasury');
\set ON_ERROR_STOP on

\echo '-- EXPECT ERROR P0013 x3 — the signed record must agree with the claim: nonce, recipient, amount'
\set ON_ERROR_STOP off
select public.record_signed_claim(:'ha', :'c1', 8, '0x02' || repeat('c1', 100), '0x' || repeat('c1', 32), 100000, 1000000000, 0, 4663, :'tok', :'w1', (select amount * power(10::numeric, 18) from public.reward_claims where id = :'c1'), 18, :'treasury');
select public.record_signed_claim(:'ha', :'c1', 7, '0x02' || repeat('c1', 100), '0x' || repeat('c1', 32), 100000, 1000000000, 0, 4663, :'tok', :'w2', (select amount * power(10::numeric, 18) from public.reward_claims where id = :'c1'), 18, :'treasury');
select public.record_signed_claim(:'ha', :'c1', 7, '0x02' || repeat('c1', 100), '0x' || repeat('c1', 32), 100000, 1000000000, 0, 4663, :'tok', :'w1', (select amount * power(10::numeric, 17) from public.reward_claims where id = :'c1'), 18, :'treasury');
\set ON_ERROR_STOP on
select public.record_signed_claim(:'ha', :'c1', 7, '0x02' || repeat('c1', 100), '0x' || repeat('c1', 32), 100000, 1000000000, 0, 4663, :'tok', :'w1', (select amount * power(10::numeric, 18) from public.reward_claims where id = :'c1'), 18, :'treasury');
select status as c1_must_be_signed, amount_base from public.reward_claims where id = :'c1';
select public.payout_snapshot(:'treasury') -> 'in_flight' -> 0 ->> 'signer_address' = :'treasury'
       as fs9_snapshot_carries_signer_must_be_t;
\echo '-- EXPECT ERROR P0013 — once signed, the transaction record is frozen'
\set ON_ERROR_STOP off
update public.reward_claims set nonce = 99 where id = :'c1';
\set ON_ERROR_STOP on
select public.mark_claim_broadcast(:'ha', :'c1');
select status as c1_must_be_broadcast, attempts as attempts_must_be_1 from public.reward_claims where id = :'c1';
\echo '-- EXPECT ERROR 22023 — an error is a CODE, never free text (principle 7)'
\set ON_ERROR_STOP off
select public.record_claim_attempt(:'ha', :'c1', 'RPC said: connection reset');
\set ON_ERROR_STOP on
\echo '-- EXPECT ERROR P0014 — confirmation needs a receipt for a hash RECORDED on this claim'
\set ON_ERROR_STOP off
select public.confirm_claim(:'ha', :'c1', '0x' || repeat('ff', 32), 123);
\set ON_ERROR_STOP on
select public.confirm_claim(:'ha', :'c1', '0x' || repeat('c1', 32), 123);
select status as c1_must_be_confirmed, block_number, receipt_status from public.reward_claims where id = :'c1';
\echo '-- EXPECT ERROR P0013 x2 — a confirmed claim is immutable, and its credits cannot be released,'
\echo '--         not even by a session that sets the release flag itself'
\set ON_ERROR_STOP off
update public.reward_claims set error = 'late_callback' where id = :'c1';
begin;
select set_config('wanted.release_claim', :'c1', true);
update public.reward_ledger set claim_id = null where claim_id = :'c1';
rollback;
\set ON_ERROR_STOP on

\echo '-- FM-01 regression: after a confirmed claim the SAME wallet can claim again (c2, the credit of 1)'
select id as c2 from public.create_reward_claim(:'w1', 'TTWO', 0.001, 1.6) \gset
-- (No P0012 here: with nothing in flight the database lets ANY queued claim take the next nonce.
-- Strict serial means one claim in flight at a time, not first-in-first-out among queued ones —
-- the worker's own ordering, queued-with-nonce first then oldest, is what picks the next claim.)
select public.assign_claim_nonce(:'ha', :'c2', :'treasury') as c2_nonce_must_be_8;
select public.record_signed_claim(:'ha', :'c2', 8, '0x02' || repeat('c2', 100), '0x' || repeat('c2', 32), 100000, 1000000000, 0, 4663, :'tok', :'w1', (select amount * power(10::numeric, 18) from public.reward_claims where id = :'c2'), 18, :'treasury');
select public.mark_claim_broadcast(:'ha', :'c2');
\echo '-- EXPECT ERROR P0014 x2 — a timeout is not proof, and neither is a hash this claim never recorded'
\set ON_ERROR_STOP off
select public.fail_claim(:'ha', :'c2', 'timeout', '0x' || repeat('c2', 32));
select public.fail_claim(:'ha', :'c2', 'receipt_reverted', '0x' || repeat('ff', 32));
\set ON_ERROR_STOP on
\echo '-- A status-0 receipt for c2''s own hash: failed with proof, credits returned in the same transaction'
select public.fail_claim(:'ha', :'c2', 'receipt_reverted', '0x' || repeat('c2', 32));
select status as c2_must_be_failed, proof, receipt_status as receipt_status_must_be_0 from public.reward_claims where id = :'c2';
select count(*) as w1_rows_claimable_again_must_be_1 from public.reward_ledger where wallet = :'w1' and claim_id is null;

\echo '-- Unknown consumer of a nonce: c3 goes to review and the treasury HALTS (principle 5)'
select id as c3 from public.create_reward_claim(:'w1', 'TTWO', 0.001, 1.6) \gset
select public.assign_claim_nonce(:'ha', :'c3', :'treasury') as c3_nonce_must_be_9;
select public.record_signed_claim(:'ha', :'c3', 9, '0x02' || repeat('c3', 100), '0x' || repeat('c3', 32), 100000, 1000000000, 0, 4663, :'tok', :'w1', (select amount * power(10::numeric, 18) from public.reward_claims where id = :'c3'), 18, :'treasury');
select public.flag_claim_review(:'ha', :'c3', 'nonce_consumed_without_receipt');
select status as c3_must_be_needs_review from public.reward_claims where id = :'c3';
select halted as treasury_halted_must_be_t, halt_reason from public.treasury_accounts where address = :'treasury';
select count(*) as c3_credits_still_attached_must_be_1 from public.reward_ledger where claim_id = :'c3';
\echo '-- EXPECT ERROR P0013 — a claim in review cannot be confirmed by a direct write (no block, no receipt)'
\set ON_ERROR_STOP off
update public.reward_claims set status = 'confirmed' where id = :'c3';
\set ON_ERROR_STOP on
\echo '-- EXPECT ERROR P0011 — halted: nothing else gets a nonce'
\set ON_ERROR_STOP off
select public.assign_claim_nonce(:'ha', :'d1', :'treasury');
\set ON_ERROR_STOP on
\echo '-- EXPECT ERROR P0013 — resuming is refused while a claim is still in review'
\set ON_ERROR_STOP off
select public.resume_payouts(:'treasury');
\set ON_ERROR_STOP on
select public.resolve_claim_review(:'c3', 'failed', null, null);
select status as c3_must_be_failed, proof as proof_must_be_operator_review from public.reward_claims where id = :'c3';
select public.resume_payouts(:'treasury');
select halted as treasury_halted_must_be_f from public.treasury_accounts where address = :'treasury';

\echo '-- Operator cancel of a signed-but-stuck claim (c4): record a cancel override, then its receipt fails'
\echo '-- the claim with proof cancel_receipt and returns the credits'
select id as c4 from public.create_reward_claim(:'w1', 'TTWO', 0.001, 1.6) \gset
select public.assign_claim_nonce(:'ha', :'c4', :'treasury') as c4_nonce_must_be_10;
select public.record_signed_claim(:'ha', :'c4', 10, '0x02' || repeat('c4', 100), '0x' || repeat('c4', 32), 100000, 1000000000, 0, 4663, :'tok', :'w1', (select amount * power(10::numeric, 18) from public.reward_claims where id = :'c4'), 18, :'treasury');
select public.record_claim_override(:'c4', '0x' || repeat('ce', 32), 'cancel');
\echo '-- EXPECT ERROR P0013 — an override is recorded once per claim'
\set ON_ERROR_STOP off
select public.record_claim_override(:'c4', '0x' || repeat('cf', 32), 'cancel');
\set ON_ERROR_STOP on
select public.fail_claim(:'ha', :'c4', 'cancel_receipt', '0x' || repeat('ce', 32));
select status as c4_must_be_failed, proof as proof_must_be_cancel_receipt from public.reward_claims where id = :'c4';

\echo '-- Cancel a queued claim that already holds a nonce but was never signed: the nonce is handed back'
select public.assign_claim_nonce(:'ha', :'d1', :'treasury') as d1_nonce_must_be_11;
select public.cancel_queued_claim(:'d1');
select status as d1_must_be_failed, proof as proof_must_be_never_signed, nonce as nonce_must_be_null from public.reward_claims where id = :'d1';
select next_nonce as next_nonce_rolled_back_must_be_11 from public.treasury_accounts where address = :'treasury';

\echo '-- A wallet blocked after it queued: assign_claim_nonce returns NULL, fails it never_signed, releases'
select id as e1 from public.create_reward_claim(:'w3', 'TTWO', 0.001, 1000) \gset
insert into public.policy_flags (wallet, blocked) values (:'w3', true);
select public.assign_claim_nonce(:'ha', :'e1', :'treasury') is null as blocked_assign_returns_null_must_be_t;
select status as e1_must_be_failed, proof as proof_must_be_never_signed from public.reward_claims where id = :'e1';
select count(*) as w3_credits_released_must_be_1 from public.reward_ledger where wallet = :'w3' and claim_id is null;
delete from public.policy_flags where wallet = :'w3';

\echo '-- B2 (2026-09-15 review): a wallet blocked AFTER its claim took a nonce — the interrupted-signing'
\echo '--     case — is cancelled, not signed. Before the fix the shortcut returned the nonce first.'
select id as g1 from public.create_reward_claim(:'w3', 'TTWO', 0.001, 1000) \gset
select public.assign_claim_nonce(:'ha', :'g1', :'treasury') as g1_nonce_must_be_11;
insert into public.policy_flags (wallet, blocked) values (:'w3', true);
select public.assign_claim_nonce(:'ha', :'g1', :'treasury') is null as b2_blocked_holder_returns_null_must_be_t;
select status as g1_must_be_failed, proof as g1_proof_must_be_never_signed, nonce is null as g1_nonce_null_must_be_t
from public.reward_claims where id = :'g1';
select next_nonce as b2_next_nonce_handed_back_must_be_11 from public.treasury_accounts where address = :'treasury';
select count(*) as b2_w3_credit_claimable_again_must_be_1 from public.reward_ledger where wallet = :'w3' and claim_id is null;
delete from public.policy_flags where wallet = :'w3';

\echo '-- The kill switch (FM-12): payouts off stops new claims AND the next nonce, with no deploy'
select id as f1 from public.create_reward_claim(:'w2', 'TTWO', 0.001, 1000) \gset
update public.site_config set value = value || '{"payouts": false}'::jsonb where key = 'rewards';
\echo '-- EXPECT ERROR P0005 x2'
\set ON_ERROR_STOP off
select * from public.create_reward_claim(:'w1', 'TTWO', 0.001, 1000);
select public.assign_claim_nonce(:'ha', :'f1', :'treasury');
\set ON_ERROR_STOP on
update public.site_config set value = value || '{"payouts": true}'::jsonb where key = 'rewards';
select public.cancel_queued_claim(:'f1');

\echo '-- The public status row refuses anything shaped like a secret. EXPECT ERROR 22023, then a clean write.'
\set ON_ERROR_STOP off
select public.write_treasury_status(:'ha', jsonb_build_object('leak', repeat('ab', 32)));
\set ON_ERROR_STOP on
select public.write_treasury_status(:'ha', '{"treasury": "0xabababababababababababababababababababab", "halted": false, "in_flight": 0}'::jsonb);
select value ->> 'treasury' as published_treasury from public.site_config where key = 'treasury_status';

\echo '-- A lease that EXPIRED is refused on its next write (a zombie instance). EXPECT ERROR P0010.'
select public.release_payout_lease(:'ha');
select public.acquire_payout_lease(:'hc', 1) as holder_c_short_lease_must_be_t;
select pg_sleep(1.5);
\set ON_ERROR_STOP off
select public.halt_treasury(:'hc', :'treasury', 'zombie_write');
\set ON_ERROR_STOP on
select halted as zombie_did_not_halt_must_be_f from public.treasury_accounts where address = :'treasury';

\echo '-- B1 (FM-19): service_role writes the money tables ONLY through the functions'
select not (has_table_privilege('service_role', 'public.reward_claims', 'insert')
         or has_table_privilege('service_role', 'public.reward_claims', 'update')
         or has_table_privilege('service_role', 'public.reward_claims', 'delete')
         or has_table_privilege('service_role', 'public.reward_ledger', 'insert')
         or has_table_privilege('service_role', 'public.reward_ledger', 'update')
         or has_table_privilege('service_role', 'public.reward_ledger', 'delete'))
       as b1_service_role_cannot_write_claims_or_ledger_must_be_t;
select not (has_table_privilege('service_role', 'public.site_config', 'insert')
         or has_table_privilege('service_role', 'public.site_config', 'update')
         or has_table_privilege('service_role', 'public.site_config', 'delete'))
       as fs12_service_role_cannot_rewrite_caps_or_switches_must_be_t;
select has_table_privilege('service_role', 'public.site_config', 'select')
       and has_table_privilege('service_role', 'public.reward_claims', 'select')
       as service_role_still_reads_them_must_be_t;
\echo '-- EXPECT ERROR 42501 x2 — the exploit itself: an unbacked queued claim, and a hand-made credit'
\set ON_ERROR_STOP off
set role service_role;
insert into public.reward_claims (wallet, amount, asset) values (:'w2', 100, 'TTWO');
insert into public.reward_ledger (prediction_id, wallet, amount, asset)
select id, :'w1', 100, 'TTWO' from public.predictions where prediction_type = 'locktest';
reset role;
\set ON_ERROR_STOP on
reset role;

\echo '-- INVARIANTS after every path above (fable-design I1, I2, I4, I6)'
select count(*) as i1_failed_claims_owning_credits_must_be_0
from public.reward_ledger l join public.reward_claims c on c.id = l.claim_id where c.status = 'failed';
select count(*) as i2_claims_whose_credits_do_not_sum_to_amount_must_be_0
from public.reward_claims c
where c.status <> 'failed'
  and c.amount <> coalesce((select sum(l.amount) from public.reward_ledger l where l.claim_id = c.id), 0);
select (select max(nonce) + 1 from public.reward_claims) = (select next_nonce from public.treasury_accounts where address = :'treasury')
       as i4_max_nonce_plus_1_equals_next_nonce_must_be_t;
select count(*) as i4_duplicate_nonces_must_be_0 from (
  select nonce from public.reward_claims where nonce is not null group by nonce having count(*) > 1) d;
select count(*) as i6_signed_rows_missing_their_record_must_be_0 from public.reward_claims
where status in ('signed', 'broadcast', 'confirmed')
  and (raw_tx is null or tx_hash is null or nonce is null or amount_base is null or to_address <> wallet);

\echo ''
\echo '################ 13. event_matches — the PAYLOAD FILTER (20260921000000, §3 v2.4) ########'
\echo '-- Ground truth computed from the same 2026-09-04 recording, not chosen:'
\echo '--   window A  02:52:00Z..02:55:00Z  holds three real activity_end rows —'
\echo '--     02:52:19.610918Z outcome=timeout, 02:53:16.913902Z outcome=completed,'
\echo '--     02:54:21.002800Z outcome=completed. The FIRST activity_end in the window is NOT the'
\echo '--     match, so evidence naming 02:53:16.913902Z proves the FILTER ran and not just the type.'
\echo '--   window B  01:15:00Z..01:18:00Z  holds three real activity_end rows and NO completed one'
\echo '--     (gave_up 01:16:12.616663Z, preempted 01:16:14.561255Z, timeout 01:17:08.753869Z)'
\echo '--     -> a definite `no`, which is what makes the question fair rather than void-prone.'
\echo '-- Both windows are the v2.4 scheduled-round shape: 60 s to enter, then a 180 s window.'

-- Section 11 left the deliberately small decided caps behind and section 12 turned payouts on.
-- Restore SETUP's generous caps so the credit assertions below test WHO is credited and HOW OFTEN,
-- not the §6 clamp (which is not what this migration changes).
insert into public.site_config (key, value) values
  ('reward_caps', '{"daily_cap": 100000, "max_per_prediction": 100000, "max_per_wallet_day": 100000}'::jsonb),
  ('rewards',     '{"enabled": true, "payouts": true}'::jsonb)
on conflict (key) do update set value = excluded.value;
select public.rewards_enabled() as rewards_on_for_this_section_must_be_t;

-- Seven predictions, all `event_matches`, all over the real recording. The rule JSON is the literal
-- shape CONTRACTS-PREDICTIONS §3 freezes, including the deliberately broken ones.
with s as (select id from public.sessions limit 1)
insert into public.predictions
  (session_id, question, prediction_type, opened_at, locks_at, resolves_at,
   outcomes, telemetry_rule, reward_pool, reward_asset)
select s.id, v.q, v.ptype, v.o, v.l, v.r,
       '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
       v.rule::jsonb, 10, 'TTWO'
from s, (values
  -- (a) window A really contains a completed activity_end -> yes
  ('em_yes', 'WILL WANTED PULL OFF A GOAL IN THE NEXT 3 MINUTES?',
   timestamptz '2026-09-04T02:51:00Z', timestamptz '2026-09-04T02:52:00Z', timestamptz '2026-09-04T02:55:00Z',
   '{"kind": "event_matches", "params": {"event_type": "activity_end", "payload_match": {"outcome": "completed"}}, "outcome_if_true": "yes", "outcome_if_false": "no"}'),
  -- (b) window B contains activity_end rows but none completed -> no (NOT a void, NOT a yes)
  ('em_no', 'WILL WANTED PULL OFF A GOAL IN THE NEXT 3 MINUTES?',
   timestamptz '2026-09-04T01:14:00Z', timestamptz '2026-09-04T01:15:00Z', timestamptz '2026-09-04T01:18:00Z',
   '{"kind": "event_matches", "params": {"event_type": "activity_end", "payload_match": {"outcome": "completed"}}, "outcome_if_true": "yes", "outcome_if_false": "no"}'),
  -- (c) an EMPTY filter is `event_occurs` under another name -> malformed_rule
  ('em_empty', 'WILL ANY ACTIVITY END? (EMPTY FILTER)',
   timestamptz '2026-09-04T02:51:00Z', timestamptz '2026-09-04T02:52:00Z', timestamptz '2026-09-04T02:55:00Z',
   '{"kind": "event_matches", "params": {"event_type": "activity_end", "payload_match": {}}, "outcome_if_true": "yes", "outcome_if_false": "no"}'),
  -- (d) no filter at all -> malformed_rule
  ('em_missing', 'WILL ANY ACTIVITY END? (NO FILTER KEY)',
   timestamptz '2026-09-04T02:51:00Z', timestamptz '2026-09-04T02:52:00Z', timestamptz '2026-09-04T02:55:00Z',
   '{"kind": "event_matches", "params": {"event_type": "activity_end"}, "outcome_if_true": "yes", "outcome_if_false": "no"}'),
  -- (e) a filter that is not a JSON OBJECT -> malformed_rule, both shapes
  ('em_string', 'WILL WANTED FINISH ONE? (FILTER IS A STRING)',
   timestamptz '2026-09-04T02:51:00Z', timestamptz '2026-09-04T02:52:00Z', timestamptz '2026-09-04T02:55:00Z',
   '{"kind": "event_matches", "params": {"event_type": "activity_end", "payload_match": "completed"}, "outcome_if_true": "yes", "outcome_if_false": "no"}'),
  ('em_array', 'WILL WANTED FINISH ONE? (FILTER IS AN ARRAY)',
   timestamptz '2026-09-04T02:51:00Z', timestamptz '2026-09-04T02:52:00Z', timestamptz '2026-09-04T02:55:00Z',
   '{"kind": "event_matches", "params": {"event_type": "activity_end", "payload_match": ["completed"]}, "outcome_if_true": "yes", "outcome_if_false": "no"}'),
  -- (e) ...and a blank event_type is malformed too, filter or no filter (§3 names both)
  ('em_notype', 'WILL WANTED FINISH ONE? (NO EVENT TYPE)',
   timestamptz '2026-09-04T02:51:00Z', timestamptz '2026-09-04T02:52:00Z', timestamptz '2026-09-04T02:55:00Z',
   '{"kind": "event_matches", "params": {"event_type": "", "payload_match": {"outcome": "completed"}}, "outcome_if_true": "yes", "outcome_if_false": "no"}'),
  -- (f) a perfectly good rule that nobody answered. Nobody enters, nobody wins.
  ('em_noentries', 'NOBODY ANSWERED THIS EVENT_MATCHES ONE',
   timestamptz '2026-09-04T02:51:00Z', timestamptz '2026-09-04T02:52:00Z', timestamptz '2026-09-04T02:55:00Z',
   '{"kind": "event_matches", "params": {"event_type": "activity_end", "payload_match": {"outcome": "completed"}}, "outcome_if_true": "yes", "outcome_if_false": "no"}')
) as v(ptype, q, o, l, r, rule);

-- Direct inserts: these windows are in the past, so enter_prediction() correctly refuses them
-- (section 1 proves that separately). A zero-entry prediction voids as `no_entries` BEFORE the rule
-- is ever inspected, so every malformed-rule case needs a real entrant or it asserts nothing.
insert into public.prediction_entries (prediction_id, wallet, outcome)
select p.id, w.wallet, w.outcome
from public.predictions p, (values ('0xf00d1', 'yes'), ('0xf00d2', 'no')) as w(wallet, outcome)
where p.prediction_type = 'em_yes';
insert into public.prediction_entries (prediction_id, wallet, outcome)
select p.id, '0xf00d3', 'yes' from public.predictions p
where p.prediction_type in ('em_no', 'em_empty', 'em_missing', 'em_string', 'em_array', 'em_notype');

\echo '-- EXPECT 8 locked, 8 settled (2 resolved + 6 voided). em_noentries has no entry on purpose.'
select public.lock_due_predictions() as locked;
select public.settle_due_predictions() as settled;

\echo '-- (a)+(b) EXPECT: em_yes = settled|yes|1, em_no = settled|no|1. The same rule over two real'
\echo '--         windows gives two different definite answers, decided by the payload filter.'
select prediction_type, status, result, entry_count, correct_count
from public.predictions where prediction_type in ('em_yes', 'em_no') order by prediction_type;

\echo '-- (a) EXPECT: evidence names the REAL completed activity_end at 02:53:16.913902Z, echoes the'
\echo '--         filter, and the cited row''s own payload says outcome=completed.'
select (p.resolution_evidence ->> 'ts')::timestamptz = timestamptz '2026-09-04T02:53:16.913902Z'
         as evidence_ts_is_the_real_completed_row,
       (p.resolution_evidence -> 'event_id')::bigint =
         (select e.id from public.events e
           where e.type = 'activity_end' and e.ts = timestamptz '2026-09-04T02:53:16.913902Z')
         as evidence_event_id_is_that_recorded_row,
       (select e.payload ->> 'outcome' from public.events e
         where e.id = (p.resolution_evidence -> 'event_id')::bigint) as cited_event_outcome,
       p.resolution_evidence -> 'payload_match' as evidence_payload_match
from public.predictions p where p.prediction_type = 'em_yes';

\echo '-- (a) EXPECT t: an EARLIER activity_end existed in the same window and was rejected by the'
\echo '--         filter. Without this, "first row of that type" would have produced the same answer.'
select (select min(e.ts) from public.events e
         where e.type = 'activity_end'
           and e.ts >= timestamptz '2026-09-04T02:52:00Z' and e.ts <= timestamptz '2026-09-04T02:55:00Z')
       < (p.resolution_evidence ->> 'ts')::timestamptz as an_earlier_activity_end_was_skipped
from public.predictions p where p.prediction_type = 'em_yes';

\echo '-- (b) EXPECT 3 and 0: window B really did hold activity_end rows, none of them completed.'
\echo '--         That is why `no` there is an observation and not a missing-telemetry void.'
select count(*) as real_activity_end_rows_in_window_b,
       count(*) filter (where payload @> '{"outcome": "completed"}'::jsonb) as completed_ones_must_be_0
from public.events
where type = 'activity_end'
  and ts >= timestamptz '2026-09-04T01:15:00Z' and ts <= timestamptz '2026-09-04T01:18:00Z';

\echo '-- (c)(d)(e) EXPECT void | null | malformed_rule x5. An empty, absent or non-object filter is'
\echo '--         `event_occurs` wearing this kind''s name, and would settle every such question YES.'
\echo '--         A blank event_type is malformed for the same reason it is on `event_occurs`.'
select prediction_type, status, result, entry_count,
       resolution_evidence ->> 'void_reason' as void_reason
from public.predictions
where prediction_type in ('em_empty', 'em_missing', 'em_string', 'em_array', 'em_notype')
order by prediction_type;

\echo '-- (f) EXPECT void | no_entries | 0 ledger rows: nobody entered, so nobody wins and the'
\echo '--         treasury spends nothing. The rule itself was valid and never got to run.'
select prediction_type, status, entry_count,
       resolution_evidence ->> 'void_reason' as void_reason
from public.predictions where prediction_type = 'em_noentries';
select count(*) as ledger_rows_for_the_unanswered_one_must_be_0
from public.reward_ledger l join public.predictions p on p.id = l.prediction_id
where p.prediction_type = 'em_noentries';

\echo '-- (g) EXPECT one row only: 10.000000000000000000 TTWO to 0xf00d1, who said yes. 0xf00d2 said'
\echo '--         no, was counted for accuracy and streak, and is not paid.'
select l.wallet, l.amount, l.asset, l.clamped
from public.reward_ledger l join public.predictions p on p.id = l.prediction_id
where p.prediction_type = 'em_yes' order by l.wallet;
select count(*) as ledger_rows_for_em_yes_must_be_1
from public.reward_ledger l join public.predictions p on p.id = l.prediction_id
where p.prediction_type = 'em_yes';

\echo '-- (g) EXPECT 0 settled and STILL one ledger row: settlement is safely re-runnable over the'
\echo '--         new kind too (idempotency invariant 2).'
select public.settle_due_predictions() as rerun_should_settle_0;
select count(*) as ledger_rows_for_em_yes_still_1
from public.reward_ledger l join public.predictions p on p.id = l.prediction_id
where p.prediction_type = 'em_yes';
select count(*) as rows_for_the_losing_wallet_must_be_0
from public.reward_ledger where wallet = '0xf00d2';

\echo '-- The replace kept everything around the body: 20260921000000 restates no grant, and'
\echo '--   `create or replace function` keeps the owner and ACL, so 20260908120001/20260909000000''s'
\echo '--   revokes and 20260914000001''s service_role revoke are all still in force afterwards.'
select has_function_privilege('service_role', 'public.settle_due_predictions()', 'execute')
         as service_role_raw_execute_must_still_be_f,
       has_function_privilege('anon', 'public.settle_due_predictions()', 'execute')
         as anon_execute_must_still_be_f,
       has_function_privilege('service_role', 'public.settle_due_predictions_serialized()', 'execute')
         as wrapper_still_reachable_must_be_t;
select prosecdef as security_definer_must_be_t, proconfig as search_path_must_be_public_pg_temp
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public' and p.proname = 'settle_due_predictions';

\echo ''
\echo '################ DONE ################'
