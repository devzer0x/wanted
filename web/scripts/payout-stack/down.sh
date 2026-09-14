#!/usr/bin/env bash
# Removes ONLY what up.sh created. Other projects' containers on this machine are never touched.
S=/tmp/wanted-payout-stack
for p in shim anvil; do [ -f "$S/$p.pid" ] && kill "$(cat "$S/$p.pid")" 2>/dev/null; rm -f "$S/$p.pid"; done
docker rm -f wanted-payout-rest wanted-payout-pg >/dev/null 2>&1
docker network rm wanted-payout-net >/dev/null 2>&1
rm -f "$S/env"
echo "stack down"
