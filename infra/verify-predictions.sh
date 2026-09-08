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
until docker exec "$CONTAINER" pg_isready -U postgres -q; do sleep 1; done

# Supabase's roles, which the policies migration grants against.
$PSQL -c "create role anon nologin; create role authenticated nologin; create role service_role nologin bypassrls;"

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
echo "Container ${CONTAINER} will be removed on exit."
