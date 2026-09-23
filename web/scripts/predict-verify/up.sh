#!/usr/bin/env bash
# A real Postgres 16 + real PostgREST v12.2.3, holding every infra/supabase migration, seeded
# with real rows, so the Playwright suite in e2e/predictLocal.spec.ts can exercise the running
# Next.js app against real data it controls — never against production (CLAUDE.md rule 2/3) and
# never by mocking a route (this repo's standing convention: see e2e/walletHarness.ts's own "no
# route under src/app/api/** is intercepted" rule, and web/scripts/payout-stack for the same
# pattern applied to the payout outbox).
#
# Fixed ports and a fixed, throwaway JWT secret (every port is published on 127.0.0.1 only, and
# down.sh destroys the stack at the end of every run) — nothing here is a real secret.
# Container/network names carry the web-f5- prefix. Idempotent: reruns tear down first.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"

NET=web-f5-net
PG=web-f5-pg
REST=web-f5-rest
PG_PORT=55580
REST_PORT=55581
SHIM_PORT=55582
JWT_SECRET="wanted-web-f5-predict-verify-local-only-2026-09-22-not-used-elsewhere"

# A service-role JWT signed with the SAME secret PostgREST is told to verify (PGRST_JWT_SECRET
# below), so the product's own createSupabaseAdmin() (web/src/app/api/_lib/supabaseAdmin.ts, which
# reads SUPABASE_URL + SUPABASE_SECRET_KEY — never the browser NEXT_PUBLIC_* pair) can reach this
# stack as service_role and bypass RLS the same way it does against the real project. Without this
# every route under web/src/app/api/predictions/** and /api/leaderboard answers 503 "database is
# not configured" (isSupabaseAdminConfigured() is false with no key), which is what this stack was
# missing before this fix. Same technique as web/scripts/payout-stack/up.sh's SERVICE_JWT.
SERVICE_JWT="$(node -e '
const c=require("crypto"),b=(o)=>Buffer.from(JSON.stringify(o)).toString("base64url");
const h=b({alg:"HS256",typ:"JWT"}),p=b({role:"service_role",iss:"web-f5-predict-verify",exp:Math.floor(Date.now()/1e3)+86400});
process.stdout.write(`${h}.${p}.`+c.createHmac("sha256",process.argv[1]).update(`${h}.${p}`).digest("base64url"));' "$JWT_SECRET")"

SESSION_ID=33333333-3333-3333-3333-333333333333
LOCK_GUARD_ID=44444444-4444-4444-4444-444444444444
DUP_GUARD_ID=55555555-5555-5555-5555-555555555555
TINY_REWARD_ID=66666666-6666-6666-6666-666666666666

"$HERE/down.sh" >/dev/null 2>&1 || true
docker network create "$NET" >/dev/null

docker run -d --name "$PG" --network "$NET" -e POSTGRES_PASSWORD=verify -p 127.0.0.1:${PG_PORT}:5432 postgres:16-alpine >/dev/null
until docker exec "$PG" pg_isready -U postgres -q 2>/dev/null; do sleep 1; done
sleep 1
PSQL=(docker exec -i "$PG" psql -U postgres -v ON_ERROR_STOP=1 -q)

"${PSQL[@]}" <<'SQL'
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
create role authenticator login password 'verify' noinherit;
grant anon, authenticated, service_role to authenticator;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
SQL

echo "== applying every migration in order =="
for f in "$ROOT"/infra/supabase/migrations/*.sql; do
  docker cp "$f" "$PG:/tmp/m.sql" >/dev/null
  "${PSQL[@]}" -f /tmp/m.sql >/dev/null
  echo "   applied $(basename "$f")"
done

echo "== seeding: rewards on, a session, and the three fixed-id predictions the spec drives =="
"${PSQL[@]}" <<SQL
insert into public.site_config (key, value) values
  ('rewards', '{"enabled": true, "payouts": false}'),
  ('reward_caps', '{"daily_cap": 999999999999, "max_per_prediction": 999999999999, "max_per_wallet_day": 999999999999}')
on conflict (key) do update set value = excluded.value;

insert into public.sessions (id, game_edition, harness_version)
values ('$SESSION_ID', 'enhanced', 'verify-1.0');

-- LOCK-GUARD: open, but locks_at is already in the past at seed time (and stays in the past for
-- the rest of the run, since nothing here calls lock_due_predictions()). Reproduces the exact
-- client-side race MobilePredictBar's lockPassed guard exists for: the server still says "open"
-- because no lifecycle tick has run, while the wall clock is past locks_at.
insert into public.predictions (
  id, session_id, question, prediction_type, opened_at, locks_at, resolves_at,
  outcomes, telemetry_rule, status, reward_pool, reward_asset
) values (
  '$LOCK_GUARD_ID', '$SESSION_ID', 'LOCK GUARD PROOF — does it clear cops', 'wanted_clears',
  now() - interval '40 seconds', now() - interval '5 seconds', now() + interval '55 seconds',
  '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
  '{"kind":"wanted_clears","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb,
  'open', 1, 'TTWO'
);

-- DUP-GUARD: open with a locks_at comfortably in the future (30 minutes — well past any build
-- time), for the duplicate-entry (3b) proof: first POST /enter succeeds, the second must not read
-- as "locked" when it is really "already entered".
insert into public.predictions (
  id, session_id, question, prediction_type, opened_at, locks_at, resolves_at,
  outcomes, telemetry_rule, status, reward_pool, reward_asset
) values (
  '$DUP_GUARD_ID', '$SESSION_ID', 'DUP GUARD PROOF — does it enter a vehicle', 'vehicle_entered',
  now() - interval '10 seconds', now() + interval '1800 seconds', now() + interval '2000 seconds',
  '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
  '{"kind":"vehicle_entered","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb,
  'open', 1, 'TTWO'
);

-- TINY-REWARD: already settled, correct outcome "yes", with NO entry/ledger row yet — up.sh does
-- not know any wallet address (that only exists once a test signs in with a real EIP-1193
-- signature). e2e/predictLocal.spec.ts inserts its own wallet's prediction_entries row and a
-- sub-1e-6 reward_ledger credit directly against this same container (docker exec ... psql, the
-- same technique this file uses to seed) once it has a signed-in address, and separately seeds a
-- second, fixed wallet's many-significant-digit credit the same way for the leaderboard proof —
-- below formatBaseUnits' default 4-digit display precision, so ResolvedPredictionCard's
-- "correct, 0 TTWO credited." bug (web/src/components/predict/ResolvedPredictionCard.tsx, the
-- card a SETTLED prediction like this one renders) is reproduced and its
-- "correct, <0.0001 TTWO credited." fix is provable from the real rendered DOM.
insert into public.predictions (
  id, session_id, question, prediction_type, opened_at, locks_at, resolves_at,
  outcomes, telemetry_rule, status, result, reward_pool, reward_asset,
  entry_count, correct_count, settled_at
) values (
  '$TINY_REWARD_ID', '$SESSION_ID', 'TINY REWARD PROOF — did it survive the block', 'survives_window',
  now() - interval '3600 seconds', now() - interval '3500 seconds', now() - interval '3000 seconds',
  '[{"key":"yes","label":"YES"},{"key":"no","label":"NO"}]'::jsonb,
  '{"kind":"survives_window","outcome_if_true":"yes","outcome_if_false":"no"}'::jsonb,
  'settled', 'yes', 1, 'TTWO', 0, 0, now() - interval '2900 seconds'
);
SQL

echo "== booting PostgREST v12.2.3 =="
docker run -d --name "$REST" --network "$NET" -p 127.0.0.1:${REST_PORT}:3000 \
  -e PGRST_DB_URI="postgres://authenticator:verify@$PG:5432/postgres" \
  -e PGRST_DB_SCHEMAS=public -e PGRST_DB_ANON_ROLE=anon \
  -e PGRST_JWT_SECRET="$JWT_SECRET" postgrest/postgrest:v12.2.3 >/dev/null
until curl -s -m 2 "http://127.0.0.1:${REST_PORT}/" >/dev/null 2>&1; do sleep 1; done

echo "== starting the Supabase-URL-shape shim (reuses web/scripts/payout-stack/shim.mjs) =="
STATE=/tmp/web-f5-predict-verify
mkdir -p "$STATE"
node "$ROOT/web/scripts/payout-stack/shim.mjs" "$SHIM_PORT" "$REST_PORT" > "$STATE/shim.log" 2>&1 &
echo $! > "$STATE/shim.pid"
until curl -s -m 2 "http://127.0.0.1:${SHIM_PORT}/rest/v1/" >/dev/null 2>&1; do sleep 1; done

# What web/playwright.config.ts reads for PLAYWRIGHT_PREDICT_LOCAL=1 (readPredictVerifyEnv()) to
# get the spawned Next.js server's SUPABASE_URL / SUPABASE_SECRET_KEY — the only two var names
# web/src/app/api/_lib/supabaseAdmin.ts reads. Same shape and same fixed path convention as
# web/scripts/payout-stack/up.sh's own $STATE/env.
cat > "$STATE/env" <<ENV
export SUPABASE_URL=http://127.0.0.1:${SHIM_PORT}
export SUPABASE_SECRET_KEY=${SERVICE_JWT}
ENV

echo "stack up: postgres ${PG_PORT}, postgrest ${REST_PORT}, shim (Supabase URL shape) ${SHIM_PORT}"
echo "web app env for this stack: $STATE/env (read by web/playwright.config.ts)"
echo "LOCK_GUARD_ID=$LOCK_GUARD_ID DUP_GUARD_ID=$DUP_GUARD_ID TINY_REWARD_ID=$TINY_REWARD_ID"
