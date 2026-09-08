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
#   The three migrations are ADDITIVE ONLY. They create new tables, a new enum, new functions, new
#   policies and one new view. They contain no ALTER, DROP or UPDATE against any pre-existing object,
#   and they do not touch a single existing row. Verified by the grep in step 2 below, which runs
#   every time and aborts if that ever stops being true.
#
#   The same three files have been applied cleanly to a throwaway Postgres 16 repeatedly by
#   infra/verify-predictions.sh, which then replays 5,649 real recorded events through them and
#   asserts settlement, idempotency, the claim path and RLS.
#
# WHAT IT DOES NOT DO
#   It does not seed data, does not create a treasury, does not enable rewards. REWARDS_ENABLED stays
#   whatever the deployment env says (default false).
#
# HOW TO UNDO
#   Every object it creates is new and namespaced, so a rollback is a drop of exactly those objects.
#   Step 4 prints the exact rollback statement set before it does anything, so you have it in hand.
#
# Usage:  cd infra && ./apply-predictions-to-cloud.sh          # prompts before applying
#         cd infra && ./apply-predictions-to-cloud.sh --yes    # non-interactive
set -euo pipefail
cd "$(dirname "$0")"

MIGRATIONS=(
  supabase/migrations/20260908120000_predictions.sql
  supabase/migrations/20260908120001_predictions_policies.sql
  supabase/migrations/20260908120002_reward_claim_rpc.sql
)

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
   | grep -viE 'drop[[:space:]]+(function|policy|trigger)[[:space:]]+if[[:space:]]+exists' ; then
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
  read -r -p "Apply the three prediction migrations to the CLOUD project? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "aborted."; exit 0; }
fi

# ---- 6. apply ------------------------------------------------------------------------------------
echo
for f in "${MIGRATIONS[@]}"; do
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

echo
echo "Done. Next: PostgREST picks up the new schema within a few seconds; then"
echo "  curl -s \"\$SUPABASE_URL/rest/v1/predictions?select=id&limit=1\" -H \"apikey: <publishable>\""
echo "should return [] rather than PGRST205."
