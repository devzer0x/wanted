#!/usr/bin/env bash
# End-to-end proof of the prediction layer against a REAL Postgres 16 and REAL recorded gameplay.
#
# This is the strongest verification available without the game running. It does not mock the
# telemetry: it replays 5,649 events captured from an actual session
# (harness/tests/fixtures/real_session_2026-09-04.json, pulled from the production Supabase) and
# asserts that settlement reaches the answer the recording actually contains.
#
# Ground truth used below was computed from that recording, not chosen:
#   - a real death at 2026-09-04T08:42:33Z  -> a window containing it MUST settle "no" for survival
#   - a 258-minute death-free stretch       -> a window inside it MUST settle "yes" for survival
#   - 24 real wanted->0 transitions         -> exercise wanted_clears
#   - the session's own session_end         -> a window overlapping it MUST void
#   - 3 activity_end rows at 02:52-02:55Z,  -> event_matches {"outcome":"completed"} MUST settle
#     the 2nd and 3rd of them "completed"      "yes" and cite the 2nd, not the first row of the type
#   - 3 activity_end rows at 01:15-01:18Z,  -> the same rule MUST settle "no" (the filter applied),
#     none of them "completed"                 not void for missing telemetry
#   - no death/busted from 05:15:21Z to     -> section 14's survives_window rows MUST settle "yes",
#     08:42:33Z                                beside a poisoned row that voids as settlement_error
#
# Usage: ./verify-predictions.sh          (tears its own container up and down)
set -euo pipefail
cd "$(dirname "$0")"

CONTAINER=wanted-verify-pg
PORT=55433
PSQL="docker exec -i $CONTAINER psql -U postgres -v ON_ERROR_STOP=1 -q"

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "== booting a throwaway Postgres 16 =="
docker run -d --name "$CONTAINER" -e POSTGRES_PASSWORD=verify -p ${PORT}:5432 postgres:16-alpine >/dev/null
# pg_isready alone is not enough: the image's entrypoint runs a TEMPORARY server for initdb,
# pg_isready succeeds against it, and then it restarts — a query sent in that window gets "server
# closed the connection unexpectedly". Require two real queries a second apart to both succeed.
ready=0
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" psql -U postgres -tAqc 'select 1' >/dev/null 2>&1; then
    ready=$((ready + 1)); [[ $ready -ge 2 ]] && break
  else
    ready=0
  fi
  sleep 1
done
[[ $ready -ge 2 ]] || { echo "Postgres never became ready" >&2; exit 1; }

# Supabase's roles, which the policies migration grants against.
$PSQL -c "create role anon nologin; create role authenticated nologin; create role service_role nologin bypassrls;"

# Supabase ships these default privileges, and a plain Postgres does not. Without them a local run
# CANNOT reproduce the grant Supabase adds to every new function, and a migration that revokes only
# from the PUBLIC pseudo-role passes here while leaving the function callable by `anon` in
# production. That happened; this line is why it cannot happen again.
$PSQL -c "alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
          alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
          alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;"

echo "== applying ALL migrations in order (base schema, then the prediction layer) =="
for f in supabase/migrations/*.sql; do
  docker cp "$f" "$CONTAINER:/tmp/m.sql" >/dev/null
  $PSQL -f /tmp/m.sql
  echo "   applied $(basename "$f")"
done

echo "== loading the real recorded session =="
python3 ../scripts/load-real-session.py > /tmp/real_session_load.sql
docker cp /tmp/real_session_load.sql "$CONTAINER:/tmp/load.sql" >/dev/null
$PSQL -f /tmp/load.sql
$PSQL -c "select count(*) as real_events_loaded from public.events;"

echo
echo "== the assertions =="
docker cp verify-predictions.sql "$CONTAINER:/tmp/v.sql" >/dev/null
docker exec -i "$CONTAINER" psql -U postgres -f /tmp/v.sql

echo
echo "== the lock, contended: a second backend holds the settlement lock; a run must yield =="
# "Returned 0" alone proves nothing — an uncontended run also returns 0 when nothing is due. So a
# prediction is made due FIRST (same real no-death window as survive_yes, one entrant): the
# contended run must return 0 AND leave it unsettled, and the run after the holder exits must
# settle exactly that one. Only then has the lock, and not an empty queue, been observed.
Q() { docker exec "$CONTAINER" psql -U postgres -tAq -c "$1"; }
Q "insert into public.predictions (session_id, question, prediction_type, opened_at, locks_at, resolves_at, outcomes, telemetry_rule, reward_pool, reward_asset)
   select id, 'LOCK WITNESS', 'lockwitness', timestamptz '2026-09-04T06:19:00Z', timestamptz '2026-09-04T06:20:00Z', timestamptz '2026-09-04T06:23:00Z',
          '[{\"key\":\"yes\",\"label\":\"YES\"},{\"key\":\"no\",\"label\":\"NO\"}]'::jsonb,
          '{\"kind\":\"survives_window\",\"outcome_if_true\":\"yes\",\"outcome_if_false\":\"no\"}'::jsonb, 1, 'TTWO'
   from public.sessions limit 1;" >/dev/null
Q "insert into public.prediction_entries (prediction_id, wallet, outcome) select id, '0xabc9', 'yes' from public.predictions where prediction_type = 'lockwitness';" >/dev/null
Q "select public.lock_due_predictions();" >/dev/null
# A real second connection, not a simulation: advisory locks are shared across sessions, so while
# this one holds key 7741300101 the wrapper's try-lock in the other must fail.
docker exec "$CONTAINER" psql -U postgres -tAq -c "select pg_advisory_lock(7741300101); select pg_sleep(4);" >/dev/null &
HOLDER=$!
sleep 1.5
CONTENDED=$(Q "select public.settle_due_predictions_serialized();")
STILL=$(Q "select status from public.predictions where prediction_type = 'lockwitness';")
wait $HOLDER
AFTER=$(Q "select public.settle_due_predictions_serialized();")
FINAL=$(Q "select status || '|' || coalesce(result, '-') from public.predictions where prediction_type = 'lockwitness';")
echo "   contended run returned: ${CONTENDED}            (EXPECT 0)"
echo "   witness while contended: ${STILL}              (EXPECT locked — untouched)"
echo "   run after holder exited returned: ${AFTER}  (EXPECT 1)"
echo "   witness afterwards: ${FINAL}              (EXPECT settled|yes)"
[[ "$CONTENDED" == "0" && "$STILL" == "locked" && "$AFTER" == "1" && "$FINAL" == "settled|yes" ]] \
  || { echo "FAIL: settlement lock did not serialise"; exit 1; }
echo "   PASS: the lock, not an empty queue, produced the 0"

echo
echo "== SKIP LOCKED with a per-candidate exception block (20260922000000): a row another backend holds is =="
echo "== skipped, a poisoned row in the same run is voided, and the held row settles once it is released  =="
# Both rows are in the real no-death stretch (05:15:21Z..08:42:33Z of the recording). The poisoned one
# resolves first, so the cursor has to come back from an exception handler and keep fetching — and
# the row it then reaches is held FOR UPDATE by a real second backend, so SKIP LOCKED must skip it.
Q "insert into public.predictions (session_id, question, prediction_type, opened_at, locks_at, resolves_at, outcomes, telemetry_rule, reward_pool, reward_asset)
   select s.id, v.q, v.t, timestamptz '2026-09-04T07:00:00Z', timestamptz '2026-09-04T07:01:00Z', v.r,
          '[{\"key\":\"yes\",\"label\":\"YES\"},{\"key\":\"no\",\"label\":\"NO\"}]'::jsonb, v.rule::jsonb, 1, 'TTWO'
   from (select id from public.sessions limit 1) s, (values
     ('SKIP POISON',  'skippoison',  timestamptz '2026-09-04T07:03:00Z', '{\"kind\":\"wanted_reaches\",\"params\":{\"level\":\"two\"},\"outcome_if_true\":\"yes\",\"outcome_if_false\":\"no\"}'),
     ('SKIP WITNESS', 'skipwitness', timestamptz '2026-09-04T07:04:00Z', '{\"kind\":\"survives_window\",\"outcome_if_true\":\"yes\",\"outcome_if_false\":\"no\"}')
   ) v(q, t, r, rule);" >/dev/null
Q "insert into public.prediction_entries (prediction_id, wallet, outcome) select id, '0x5c1a1', 'yes' from public.predictions where prediction_type in ('skippoison', 'skipwitness');" >/dev/null
Q "select public.lock_due_predictions();" >/dev/null
docker exec "$CONTAINER" psql -U postgres -tAq -c "begin; select 1 from public.predictions where prediction_type = 'skipwitness' for update; select pg_sleep(4); commit;" >/dev/null &
HOLDER=$!
sleep 1.5
SKIP_RUN=$(Q "select public.settle_due_predictions_serialized();")
SKIP_HELD=$(Q "select status from public.predictions where prediction_type = 'skipwitness';")
SKIP_POISON=$(Q "select status || '|' || (resolution_evidence ->> 'void_reason') || '|' || (resolution_evidence ->> 'sqlstate') from public.predictions where prediction_type = 'skippoison';")
wait $HOLDER
SKIP_AFTER=$(Q "select public.settle_due_predictions_serialized();")
SKIP_FINAL=$(Q "select p.status || '|' || coalesce(p.result, '-') || '|' || (select count(*) from public.reward_ledger l where l.prediction_id = p.id) from public.predictions p where p.prediction_type = 'skipwitness';")
echo "   run while the witness is held returned: ${SKIP_RUN}   (EXPECT 1 — the poisoned row, voided)"
echo "   held witness during that run: ${SKIP_HELD}            (EXPECT locked — skipped, untouched)"
echo "   poisoned row: ${SKIP_POISON}         (EXPECT void|settlement_error|22P02)"
echo "   run after the holder committed returned: ${SKIP_AFTER}  (EXPECT 1)"
echo "   witness afterwards: ${SKIP_FINAL}               (EXPECT settled|yes|1)"
[[ "$SKIP_RUN" == "1" && "$SKIP_HELD" == "locked" && "$SKIP_POISON" == "void|settlement_error|22P02" \
   && "$SKIP_AFTER" == "1" && "$SKIP_FINAL" == "settled|yes|1" ]] \
  || { echo "FAIL: SKIP LOCKED or row isolation did not hold under a concurrent row lock"; exit 1; }
echo "   PASS: the cursor skipped the held row, survived the handler, and the held row settled once"

echo
echo "Container ${CONTAINER} will be removed on exit."
