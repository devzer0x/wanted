#!/usr/bin/env bash
# Apply the prediction-layer migrations to the CLOUD Supabase project.
#
# This is the one step that touches production, so it is a deliberate, separate script rather than
# something folded into a verification run. Read this header before running it.
#
# WHAT IT DOES
#   Applies infra/supabase/migrations/20260908120000_predictions.sql,
#   ...120001_predictions_policies.sql and ...120002_reward_claim_rpc.sql to the project named in
#   infra/.env.cloud.
#
# WHY IT IS SAFE TO RUN
#   Every migration listed is ADDITIVE ONLY. They create new tables, a new enum, new functions, new
#   policies, one new view and one trigger on a table they themselves created. They contain no
#   ALTER, DROP or UPDATE against any pre-existing object, and they do not touch a single existing
#   row. Verified by the grep in step 2 below, which runs every time and aborts if that ever stops
#   being true.
#
#   The same files have been applied cleanly to a throwaway Postgres 16 repeatedly by
#   infra/verify-predictions.sh, which then replays 5,649 real recorded events through them and
#   asserts settlement, idempotency, the claim path and RLS.
#
#   Running it twice is safe: step 3 asks the project whether the base set has already run and
#   applies only the idempotent repairs if so. The base set is NOT re-runnable on its own — it
#   creates tables with bare CREATE TABLE — which is why the choice is made from the database
#   rather than from a comment claiming the whole list is idempotent. It is not.
#
# WHAT IT DOES NOT DO
#   It does not seed data, does not create a treasury, does not enable rewards. Rewards are off in
#   two independent places and this script flips neither: `REWARDS_ENABLED` in the deployment env
#   gates CLAIMING, and the `rewards` site_config row gates CREDITING (default closed, see
#   20260909010000). Turning rewards on is a deliberate operator step, documented in that migration.
#
# HOW TO UNDO
#   Every object it creates is new and namespaced, so a rollback is a drop of exactly those objects.
#   Step 4 prints the exact rollback statement set before it does anything, so you have it in hand.
#
# Usage:  cd infra && ./apply-predictions-to-cloud.sh          # prompts before applying
#         cd infra && ./apply-predictions-to-cloud.sh --yes    # non-interactive
set -euo pipefail
cd "$(dirname "$0")"

# The migrations split by whether they can be run against a project that already has them, and the
# split is a property of the files, not a preference. BASE creates tables with bare `create table`,
# so a second run aborts on the first one — it is a first-install set. REPAIR is `create or
# replace` / `drop ... if exists` throughout and is safe to run any number of times.
#
# Step 6 picks between them by looking at the project, which is what makes this script the right
# tool for BOTH "stand up a new project" and "bring the live one up to date". It only handled the
# first case before, and the consequence was not theoretical: the two repairs below were missing
# from the list entirely, so the only sanctioned way to update a project could not deliver them,
# and docs/STATUS.md and the live database drifted apart over exactly that gap.
BASE_MIGRATIONS=(
  supabase/migrations/20260908120000_predictions.sql
  supabase/migrations/20260908120001_predictions_policies.sql
  supabase/migrations/20260908120002_reward_claim_rpc.sql
)
REPAIR_MIGRATIONS=(
  # Revokes the anon EXECUTE grant that Supabase's default privileges hand out and that 120001's
  # `revoke ... from public` did not remove. 120001 carries the same fix for fresh installs; this
  # exists for projects that had already run it.
  supabase/migrations/20260909000000_fix_function_grants.sql
  # The rewards master switch. Default CLOSED, so applying this to a project with no `rewards`
  # site_config row stops reward credits being written until the operator opts in.
  supabase/migrations/20260909010000_rewards_master_switch.sql
  # Adds wallet_sessions.rules_version. MUST be applied before a deployment whose /api/auth/verify
  # writes that column goes live, or every sign-in fails with an unknown-column error.
  supabase/migrations/20260914000000_rules_acceptance.sql
  # Serialises settlement behind an advisory lock (the cron calls the wrapper; the raw function is
  # revoked from service_role) and makes rewards_enabled() require real caps. MUST be applied
  # before a deployment whose cron calls settle_due_predictions_serialized goes live.
  supabase/migrations/20260914000001_settlement_guards.sql
  # The payout outbox (CONTRACTS-PREDICTIONS §10). Drops finalize_reward_claim and the 3-argument
  # create_reward_claim, so it MUST be applied before the deployment whose claim route calls the
  # 4-argument version — the old route's claims then fail closed until that deploy, which is harmless
  # while site_config rewards.payouts is off (its default).
  supabase/migrations/20260914100000_payout_outbox.sql
)
MIGRATIONS=("${BASE_MIGRATIONS[@]}" "${REPAIR_MIGRATIONS[@]}")

# ---- 1. credentials, read from the gitignored file; never echoed --------------------------------
if [[ ! -f .env.cloud ]]; then
  echo "error: infra/.env.cloud not found — it holds SUPABASE_DB_URL and is gitignored." >&2
  exit 1
fi
set -a; . ./.env.cloud; set +a
if [[ -z "${SUPABASE_DB_URL:-}" ]]; then
  echo "error: SUPABASE_DB_URL is not set in infra/.env.cloud" >&2
  exit 1
fi

# ---- 2. refuse to run if the migrations ever stop being additive ---------------------------------
echo "== checking the migrations are additive-only =="
if grep -inE '^[[:space:]]*(drop|truncate|delete[[:space:]]+from)[[:space:]]' "${MIGRATIONS[@]}" \
   | grep -viE 'drop[[:space:]]+(function|policy|trigger|index)[[:space:]]+if[[:space:]]+exists' ; then
  echo "REFUSING: a destructive statement appeared in the migrations above." >&2
  exit 1
fi
if grep -inE '^[[:space:]]*alter[[:space:]]+table[[:space:]]+(public\.)?(sessions|decisions|events|missions|clips|stats|site_config)\b' "${MIGRATIONS[@]}"; then
  echo "REFUSING: a migration alters a pre-existing table." >&2
  exit 1
fi
echo "   ok — no destructive statements, no ALTER against an existing table"

# ---- 3. show what is already there ---------------------------------------------------------------
PSQL=(docker run --rm -i -e "PGURL=$SUPABASE_DB_URL" postgres:16-alpine
      sh -c 'psql "$PGURL" -v ON_ERROR_STOP=1 "$@"' --)

echo
echo "== current state of the target project =="
"${PSQL[@]}" -tAc "select 'existing tables: ' || count(*) from information_schema.tables where table_schema='public';"
"${PSQL[@]}" -tAc "select 'prediction tables already present: ' || coalesce(string_agg(table_name, ', '), 'none') from information_schema.tables where table_schema='public' and table_name in ('predictions','prediction_entries','reward_ledger','reward_claims','wallet_sessions','wallet_streaks','policy_flags');"

# `predictions` existing is the marker for "the base set has already run". Asking the database
# beats tracking it in a file: the file can be wrong, and here it was.
ALREADY_INSTALLED=$("${PSQL[@]}" -tAc "select count(*) from information_schema.tables where table_schema='public' and table_name='predictions';" | tr -d '[:space:]')
if [[ "$ALREADY_INSTALLED" == "1" ]]; then
  MODE=repair
  TO_APPLY=("${REPAIR_MIGRATIONS[@]}")
  echo "   -> REPAIR mode: the base migrations have already run here. Applying only the"
  echo "      idempotent repairs; the base set would abort on its first CREATE TABLE."
else
  MODE=install
  TO_APPLY=("${MIGRATIONS[@]}")
  echo "   -> INSTALL mode: applying the full set."
fi

# ---- 4. the rollback, printed before anything is applied -----------------------------------------
cat <<'ROLLBACK'

== rollback, if you ever need it (copy this now) ==
  drop function if exists public.finalize_reward_claim(uuid, text, text, text);
  drop function if exists public.create_reward_claim(text, numeric, numeric);
  drop function if exists public.leaderboard(text);
  drop function if exists public.settle_due_predictions();
  drop function if exists public.lock_due_predictions();
  drop function if exists public.enter_prediction(uuid, text, text);
  drop view     if exists public.prediction_distribution;
  drop table    if exists public.reward_ledger, public.reward_claims, public.prediction_entries,
                          public.predictions, public.wallet_sessions, public.wallet_streaks,
                          public.policy_flags cascade;
  drop type     if exists public.prediction_status;
ROLLBACK

# ---- 5. confirm ----------------------------------------------------------------------------------
if [[ "${1:-}" != "--yes" ]]; then
  echo
  echo "will apply (${MODE}):"
  printf '  %s\n' "${TO_APPLY[@]##*/}"
  read -r -p "Apply these to the CLOUD project? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "aborted."; exit 0; }
fi

# ---- 6. apply ------------------------------------------------------------------------------------
echo
for f in "${TO_APPLY[@]}"; do
  echo "== applying $(basename "$f") =="
  docker run --rm -i -e "PGURL=$SUPABASE_DB_URL" -v "$PWD/$f:/tmp/m.sql:ro" postgres:16-alpine \
    sh -c 'psql "$PGURL" -v ON_ERROR_STOP=1 -q -f /tmp/m.sql'
  echo "   applied"
done

# ---- 7. verify ------------------------------------------------------------------------------------
echo
echo "== verifying =="
"${PSQL[@]}" -c "select table_name from information_schema.tables where table_schema='public' and table_name in ('predictions','prediction_entries','reward_ledger','reward_claims','wallet_sessions','wallet_streaks','policy_flags') order by 1;"
"${PSQL[@]}" -c "select proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and proname in ('enter_prediction','lock_due_predictions','settle_due_predictions','leaderboard','create_reward_claim','finalize_reward_claim') order by 1;"
"${PSQL[@]}" -tAc "select 'rls enabled on all new tables: ' || bool_and(relrowsecurity) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relname in ('predictions','prediction_entries','reward_ledger','reward_claims','wallet_sessions','wallet_streaks','policy_flags');"

# The two repairs, checked as post-conditions rather than assumed from "psql exited 0". Both are
# things that were once believed true of the live project and were not.
"${PSQL[@]}" -tAc "select 'anon EXECUTE revoked on all 3 server-only functions: ' || bool_and(not has_function_privilege('anon', p.oid, 'execute')) from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname in ('enter_prediction','lock_due_predictions','settle_due_predictions');"
"${PSQL[@]}" -tAc "select 'reward_ledger master-switch trigger installed: ' || count(*) from pg_trigger where tgname = 'reward_ledger_master_switch_trg' and not tgisinternal;"
"${PSQL[@]}" -tAc "select 'settlement reachable by service_role only through the lock: ' || (has_function_privilege('service_role', 'public.settle_due_predictions_serialized()', 'execute') and not has_function_privilege('service_role', 'public.settle_due_predictions()', 'execute'));"
"${PSQL[@]}" -tAc "select 'wallet_sessions.rules_version present: ' || count(*) from information_schema.columns where table_schema = 'public' and table_name = 'wallet_sessions' and column_name = 'rules_version';"
# The payout outbox (20260914100000). Each line must print true / the stated count.
"${PSQL[@]}" -tAc "select 'finalize_reward_claim and 3-arg create_reward_claim gone: ' || (to_regprocedure('public.finalize_reward_claim(uuid,text,text,text)') is null and to_regprocedure('public.create_reward_claim(text,numeric,numeric)') is null);"
"${PSQL[@]}" -tAc "select 'payout_lease singleton present: ' || (select count(*) = 1 from public.payout_lease where id = 1);"
"${PSQL[@]}" -tAc "select 'outbox triggers installed (expect 2): ' || count(*) from pg_trigger where not tgisinternal and tgname in ('reward_claims_guard_status_transition', 'reward_ledger_guard_claim_id');"
"${PSQL[@]}" -tAc "select 'service_role can call every worker function: ' || bool_and(has_function_privilege('service_role', p.oid, 'execute')) from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname in ('create_reward_claim','acquire_payout_lease','release_payout_lease','payout_snapshot','init_treasury_account','assign_claim_nonce','record_signed_claim','mark_claim_broadcast','record_claim_attempt','confirm_claim','fail_claim','flag_claim_review','halt_treasury','write_treasury_status','payouts_enabled');"
"${PSQL[@]}" -tAc "select 'anon/authenticated can call NO payout function: ' || not bool_or(has_function_privilege('anon', p.oid, 'execute') or has_function_privilege('authenticated', p.oid, 'execute')) from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname in ('create_reward_claim','acquire_payout_lease','release_payout_lease','payout_snapshot','init_treasury_account','assign_claim_nonce','record_signed_claim','mark_claim_broadcast','record_claim_attempt','confirm_claim','fail_claim','flag_claim_review','halt_treasury','write_treasury_status','payouts_enabled','resolve_claim_review','resume_payouts','cancel_queued_claim','record_claim_override','assert_payout_lease');"
"${PSQL[@]}" -tAc "select 'service_role cannot write reward_claims/reward_ledger directly (B1): ' || not (has_table_privilege('service_role','public.reward_claims','insert') or has_table_privilege('service_role','public.reward_claims','update') or has_table_privilege('service_role','public.reward_claims','delete') or has_table_privilege('service_role','public.reward_ledger','insert') or has_table_privilege('service_role','public.reward_ledger','update') or has_table_privilege('service_role','public.reward_ledger','delete'));"
"${PSQL[@]}" -tAc "select 'service_role cannot rewrite site_config (FS12): ' || not (has_table_privilege('service_role','public.site_config','insert') or has_table_privilege('service_role','public.site_config','update') or has_table_privilege('service_role','public.site_config','delete'));"
"${PSQL[@]}" -tAc "select 'payouts switch is currently: ' || case when public.payouts_enabled() then 'ON — the worker WILL sign' else 'off (default) — nothing will be signed' end;"
"${PSQL[@]}" -tAc "select 'rewards master switch is currently: ' || case when public.rewards_enabled() then 'ON — credits WILL be written' else 'off (default) — no credits will be written' end;"

echo
echo "Done. Next: PostgREST picks up the new schema within a few seconds; then"
echo "  curl -s \"\$SUPABASE_URL/rest/v1/predictions?select=id&limit=1\" -H \"apikey: <publishable>\""
echo "should return [] rather than PGRST205."
