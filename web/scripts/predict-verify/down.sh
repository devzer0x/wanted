#!/usr/bin/env bash
# Tears down exactly what up.sh started. Only touches web-f5-* containers/network.
set -uo pipefail
STATE=/tmp/web-f5-predict-verify
if [[ -f "$STATE/shim.pid" ]]; then
  kill "$(cat "$STATE/shim.pid")" >/dev/null 2>&1 || true
  rm -f "$STATE/shim.pid"
fi
# The env file too: a later PLAYWRIGHT_PREDICT_LOCAL run with the stack down must fail with
# "run up.sh first", not read a dead shim's URL.
rm -f "$STATE/env" "$STATE/shim.log"
docker rm -f web-f5-pg web-f5-rest >/dev/null 2>&1 || true
docker network rm web-f5-net >/dev/null 2>&1 || true
true
