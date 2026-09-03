# contract-samples — real serializations of the bridge's JSON

**Generated file. Do not edit by hand.** Everything here is rewritten by
`bridge/tools/offline-checks/run.sh`; edit the tool, not the output.

These files exist so the harness package can test its pydantic models against the
bytes the bridge really emits, instead of against a hand-typed guess. Until they
existed, the offline checks covered routing and transport but never serialized a
`Snapshot` — which is exactly how the `last_task.id: null` mismatch survived to
delivery day (CONTRACTS v1.2 now blesses those nulls).

## How they were produced

1. `dotnet build -c Release --no-incremental` compiles `bridge/` to
   `bin/Release/net48/WastedBridge.dll`.
2. `bridge/tools/offline-checks` loads **that compiled DLL** under .NET 8, builds
   `WastedBridge.Snapshot` object graphs out of its own DTO types by reflection, and
   serializes them by calling the bridge's own `SnapshotJson.Serialize` — the exact
   method the game thread publishes `/state` with (`WastedBridgeScript.TickCore`).
3. `/health` and every offline-reachable error body are captured from the real
   `BridgeRouter` over a real HTTP connection to `127.0.0.1:7777`.
4. Each document is then checked field-by-field against CONTRACTS §1 before it is
   written; the tool exits non-zero if any documented field is missing or has the
   wrong JSON type.

Regenerate with:

```
bridge/tools/offline-checks/run.sh
```

## What is real and what is not

| Aspect | Status |
|---|---|
| Field names, nesting, JSON types, null handling, number formatting | **Real** — produced by the compiled assembly's DTOs and serializer settings |
| `/health` and `errors.json` bodies | **Real** — HTTP responses from the compiled router |
| `last_task` in `state-fresh-load.json` | **Real** — `TaskEngine.ToDto()` on a never-tasked engine |
| The world *values* (positions, models, street names, tick, ts) | **Synthetic** — natives cannot run off the game server, so the object graphs are assembled by the tool |

A sample captured from a live session is a Phase-1 server deliverable
(`scripts/bridge-smoke.ps1` with the game running); these are shape fixtures, not
recordings, and must never be presented as one.

## Files

| File | What it is |
|---|---|
| `state-fresh-load.json` | `GET /state` on the first poll of a session: no task ever posted (`last_task.id`/`type` **null**, status `idle`), player on foot (`vehicle: null`), no objective blip, `bridge.edition: "unknown"`. The document that crashed the harness before v1.2. |
| `state-populated.json` | `GET /state` with every optional branch present: in a vehicle, one nearby vehicle and ped, a mission with an `objective_blip`, a `running` `drive_to`, `edition: "legacy"`. |
| `health-fresh-load.json` | `GET /health` before the first tick — `edition: "unknown"`, `tick_hz: 0`. |
| `errors.json` | Catalog of real error responses, one per CONTRACTS v1.2 code reachable without the game, plus the bridge-side extras (`not_found`, `method_not_allowed`). `not_in_vehicle` is game-thread-only and is listed under `not_capturable_offline`. |

`state-*.json` and `health-*.json` are written **verbatim**: byte-for-byte the HTTP
response body, compact, with no trailing newline. `errors.json` is a catalog wrapper
(indented) whose `captured[].body` values are the verbatim response bodies.

## Provenance of this run

| | |
|---|---|
| generated (UTC) | 2026-09-03 11:07:37 |
| tool | `bridge/tools/offline-checks` on .NET 8.0.30 (osx-arm64) |
| bridge DLL | `bridge/bin/Release/net48/WastedBridge.dll` |
| bridge DLL sha256 | `cf2fff6f14d6052bda459c05f98760eca047159de3d8f1b0d774b1ed893c8d13` |
| bridge DLL built | 2026-09-03 11:07:19 UTC |
| SHVDN reference | `bridge/lib/ScriptHookVDotNet3.dll` sha256 `0f2b8d30ebe79edd74cf364df3943afb7e6305d7453bc687503dcc7411ed5648` |

- `state-fresh-load.json` — 973 bytes
- `state-populated.json` — 1613 bytes
- `health-fresh-load.json` — 107 bytes
- `errors.json` — 2506 bytes
