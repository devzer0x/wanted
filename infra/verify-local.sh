#!/usr/bin/env bash
# Reproduces the infra verification that ran 2026-08-25: applies the WASTED migrations to a
# throwaway real Postgres 16 and proves the RLS/grant behavior (docs/STATUS.md carries the output).
# This is SQL-level verification only — PostgREST/Realtime/Storage behavior is verified against the
# real Supabase project once keys exist (checklist item 3).
set -euo pipefail
cd "$(dirname "$0")"

CONTAINER=wasted-verify-pg
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" -e POSTGRES_PASSWORD=verify -p 55432:5432 postgres:16-alpine >/dev/null
until docker exec "$CONTAINER" pg_isready -U postgres -q; do sleep 1; done

docker exec "$CONTAINER" psql -U postgres -q -v ON_ERROR_STOP=1 \
  -c "create role anon nologin; create role authenticated nologin; create role service_role nologin bypassrls;"

for f in supabase/migrations/*.sql; do
  docker cp "$f" "$CONTAINER:/tmp/m.sql" >/dev/null
  docker exec "$CONTAINER" psql -U postgres -q -v ON_ERROR_STOP=1 -f /tmp/m.sql
done
echo "== migrations applied clean =="

docker cp supabase/rls-verify.sql "$CONTAINER:/tmp/v.sql" >/dev/null
docker exec "$CONTAINER" psql -U postgres -f /tmp/v.sql

echo
echo "Done. Expected: anon SELECT ok on 7 tables; 3x permission denied for anon writes;"
echo "service_role writes ok; counts 1,1,1,1; 2x check-constraint violations; publication ="
echo "decisions/events/stats. Tear down with: docker rm -f $CONTAINER"
