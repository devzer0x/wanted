#!/usr/bin/env bash
# Off-server checks for the WANTED bridge. Builds the bridge, regenerates the SHVDN stubs from the
# real pinned DLL's metadata, runs the whole check suite, and rewrites bridge/contract-samples/.
#
# This proves nothing about natives, HTTP.SYS on Windows, or SHVDN's loader — those are server-only
# (bridge/README.md "Delivery day"). It is the layer that can be proven on a dev machine.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bridge="$(cd "$here/../.." && pwd)"

if ! command -v dotnet >/dev/null 2>&1; then
  if [ -x "$HOME/.dotnet/dotnet" ]; then
    export DOTNET_ROOT="$HOME/.dotnet"
    export PATH="$DOTNET_ROOT:$PATH"
  else
    echo "FATAL: dotnet SDK 8 not found on PATH" >&2
    exit 2
  fi
fi

if [ ! -f "$bridge/lib/ScriptHookVDotNet3.dll" ]; then
  echo "FATAL: bridge/lib/ScriptHookVDotNet3.dll is missing (gitignored third-party content)." >&2
  echo "       Fetch it with scripts/fetch-shvdn.ps1, or unzip" >&2
  echo "       bridge/lib/ScriptHookVDotNet-v3.7.0-nightly.189.zip, then retry." >&2
  exit 2
fi

echo "=== building the bridge (net48, Release) ==========================================="
dotnet build "$bridge/WastedBridge.csproj" -c Release --no-incremental

echo
echo "=== building the check harness ====================================================="
dotnet build "$here/OfflineChecks.csproj" -c Release --nologo -v quiet
tool="$here/bin/Release/net8.0/OfflineChecks.dll"

echo
echo "=== regenerating the SHVDN stubs from lib/ScriptHookVDotNet3.dll metadata ==========="
dotnet "$tool" gen-stub
dotnet "$tool" gen-stub --drift
dotnet build "$here/shvdn-stub/ShvdnStub.csproj" -c Release --nologo -v quiet
dotnet build "$here/shvdn-stub-drift/ShvdnStubDrift.csproj" -c Release --nologo -v quiet

echo
echo "=== checks ========================================================================="
status=0
dotnet "$tool" all || status=$?

echo
echo "=== sub-modes (separate processes: one-shot global state) ==========================="
dotnet "$tool" logfallback || status=$?
dotnet "$tool" drift || status=$?

echo
if [ "$status" -eq 0 ]; then
  echo "OFFLINE CHECKS: OK"
else
  echo "OFFLINE CHECKS: FAILURES (exit $status)"
fi
exit "$status"
