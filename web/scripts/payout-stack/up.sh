#!/usr/bin/env bash
# Stands up the payout verification stack: the same three moving parts production has, all real.
#
#   Postgres 16      every migration in infra/supabase/migrations, with Supabase's roles AND its
#                    default privileges (without them a local run hides grant bugs — it has before)
#   PostgREST        a real PostgREST verifying a real HS256 service-role JWT, behind a shim that
#                    gives it Supabase's /rest/v1 URL shape, so the product's supabase-js is unmodified
#   anvil            a fork of Robinhood Chain MAINNET (id 4663): the real TTWO bytecode, real
#                    balances, real receipts. Nothing broadcast here ever reaches the real chain.
#
# Prints `export` lines for the env the verifier needs. Tear down with down.sh.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
NET=wanted-payout-net
PG=wanted-payout-pg
REST=wanted-payout-rest
PG_PORT=55440; REST_PORT=55441; SHIM_PORT=55442; ANVIL_PORT=8599
STATE=/tmp/wanted-payout-stack; mkdir -p "$STATE"
export PATH="$HOME/.foundry/bin:$PATH" FOUNDRY_DISABLE_NIGHTLY_WARNING=1

# A fresh secret per run, never committed: it signs only a throwaway local token.
JWT_SECRET="$(openssl rand -hex 32)"

"$HERE/down.sh" >/dev/null 2>&1 || true
docker network create "$NET" >/dev/null

docker run -d --name "$PG" --network "$NET" -e POSTGRES_PASSWORD=verify -p ${PG_PORT}:5432 postgres:16-alpine >/dev/null
until docker exec "$PG" pg_isready -U postgres -q 2>/dev/null; do sleep 1; done
sleep 1
PSQL=(docker exec -i "$PG" psql -U postgres -v ON_ERROR_STOP=1 -q)
"${PSQL[@]}" <<'SQL'
create role anon nologin; create role authenticated nologin; create role service_role nologin bypassrls;
create role authenticator login password 'verify' noinherit;
grant anon, authenticated, service_role to authenticator;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
SQL
for f in "$ROOT"/infra/supabase/migrations/*.sql; do
  docker cp "$f" "$PG:/tmp/m.sql" >/dev/null
  "${PSQL[@]}" -f /tmp/m.sql
done

docker run -d --name "$REST" --network "$NET" -p ${REST_PORT}:3000 \
  -e PGRST_DB_URI="postgres://authenticator:verify@$PG:5432/postgres" \
  -e PGRST_DB_SCHEMAS=public -e PGRST_DB_ANON_ROLE=anon \
  -e PGRST_JWT_SECRET="$JWT_SECRET" postgrest/postgrest:v12.2.3 >/dev/null
until curl -s -m 2 "http://127.0.0.1:${REST_PORT}/" >/dev/null 2>&1; do sleep 1; done

node "$HERE/shim.mjs" "$SHIM_PORT" "$REST_PORT" > "$STATE/shim.log" 2>&1 & echo $! > "$STATE/shim.pid"
anvil --fork-url https://rpc.mainnet.chain.robinhood.com --port "$ANVIL_PORT" --silent > "$STATE/anvil.log" 2>&1 & echo $! > "$STATE/anvil.pid"
until curl -s -m 2 -X POST "http://127.0.0.1:${ANVIL_PORT}" -H 'content-type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' 2>/dev/null | grep -q 0x1237; do sleep 1; done

SERVICE_JWT="$(node -e '
const c=require("crypto"),b=(o)=>Buffer.from(JSON.stringify(o)).toString("base64url");
const h=b({alg:"HS256",typ:"JWT"}),p=b({role:"service_role",iss:"wanted-payout-stack",exp:Math.floor(Date.now()/1e3)+86400});
process.stdout.write(`${h}.${p}.`+c.createHmac("sha256",process.argv[1]).update(`${h}.${p}`).digest("base64url"));' "$JWT_SECRET")"

cat > "$STATE/env" <<ENV
export SUPABASE_URL=http://127.0.0.1:${SHIM_PORT}
export SUPABASE_SECRET_KEY=${SERVICE_JWT}
export PAYOUT_PG_CONTAINER=${PG}
export PAYOUT_ANVIL_URL=http://127.0.0.1:${ANVIL_PORT}
export NEXT_PUBLIC_RPC_URL=http://127.0.0.1:${ANVIL_PORT}
export NEXT_PUBLIC_CHAIN_ID=4663
export NEXT_PUBLIC_TTWO_TOKEN=0x5e81213613b6B86EaB4c6c50d718d34359459786
export NEXT_PUBLIC_TTWO_DECIMALS=18
ENV
echo "stack up — source $STATE/env"
