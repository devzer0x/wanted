# WANTED bridge — `WastedBridge`

C# ScriptHookVDotNet script that exposes the Bridge HTTP API v1 (`docs/CONTRACTS.md` §1) on
`http://127.0.0.1:7777` while GTA V Story Mode runs. Owner: bridge executors (CLAUDE.md §8).

## Pinned SHVDN version

**ScriptHookVDotNet v3.7.0-nightly.189** (assembly version `3.7.0.189`, released 2026-08-05),
extracted into `bridge/lib/` (gitignored). Integrity re-verified 2026-08-25: every extracted file's
SHA-256 matches the archive `lib/ScriptHookVDotNet-v3.7.0-nightly.189.zip`
(`ScriptHookVDotNet3.dll` = `0f2b8d30ebe79edd74cf364df3943afb7e6305d7453bc687503dcc7411ed5648`).
`lib/Docs/ScriptHookVDotNet3.xml` plus a metadata dump of the DLL itself are the API reference this
code was written against — the nightly API differs from stable v3.6.0 (e.g. `DriveTo` takes
`VehicleDrivingFlags` with a new argument order, `Player.WantedLevel` moved to
`Player.Wanted.WantedLevel`, clock moved to `GTA.Chrono.GameClock`). Do not bump the nightly
without recompiling and re-checking every wrapper used here.

## Architecture (frozen)

One `GTA.Script` subclass, `WastedBridgeScript` (`src/WastedBridgeScript.cs`):

- **Game thread (Tick handler)** — the only place natives run. Each tick it: checks the online
  guard, drains a `ConcurrentQueue` of commands from the HTTP side, updates the task state machine
  (`src/TaskEngine.cs`), rebuilds the `/state` snapshot (`src/SnapshotBuilder.cs`) and publishes it
  as a pre-serialized JSON string (Newtonsoft).
- **HTTP threads** (`src/HttpServer.cs`) — an `HttpListener` bound to `http://127.0.0.1:7777/` on
  a background thread. GETs return the cached snapshot; POSTs validate, enqueue a command, and
  either return `202 {"task_id"}` immediately (`/task`) or block briefly until the game thread has
  applied the command (`/timescale`, `/control`, `/radio`, `/horn`, `/unstick`) so the `200`
  reflects work that actually happened. If a tick doesn't come (loading screen, hang) they return
  `503 game_thread_stalled`. **Natives are never called from HTTP threads.**
- Shared state (`src/BridgeShared.cs`) is one volatile snapshot reference + the command queue.

## Safety

- **Online guard**: `NETWORK_IS_SESSION_STARTED` (+ `NETWORK_IS_GAME_IN_PROGRESS` belt-and-braces)
  checked at startup and every tick. On detection the script latches off — no further natives, and
  every endpoint returns `503 {"error": "online_session_active"}` until the game restarts.
- **No teleport, god-mode, money, or weapon endpoints exist.** The only positional write in the
  entire codebase is the `/unstick` nudge (`WastedBridgeScript.ApplyUnstick`): ≤ 3 m (2.5 m
  backward + 0.25 m lift), only when a drive task is running **and** the vehicle has been at
  standstill (speed < 0.2 m/s) for > 20 s, re-verified on the game thread, always written to the
  file log. Anything else → `409 unstick_conditions_not_met`.
- Log file: `WastedBridge.log` next to the compiled script in the game's `scripts/` directory
  (5 MB rotation to `.old`).

## Endpoints and tasks

Exactly the CONTRACTS §1 surface: `GET /state`, `GET /health`, `POST /task` (all 11 types,
new task preempts the running one → `failed`/`"preempted"`), `POST /timescale` (clamped 0.1–1.0),
`POST /control`, `POST /radio`, `POST /horn` (1–3000 ms), `POST /unstick`. Errors are
`{"error": "<snake_code>", "detail": "..."}`.

Driving styles (D3): `normal=786603`, `rushed=1074528293`, `ignore_lights=786475`,
`avoid_traffic=786468` (`src/DrivingStyles.cs`). The names are the contract; bit values may be
tuned empirically in Phase 3.

Implementation choices where the contract is silent (all in code comments too):

- Arrival checks (`drive_to` radius, `walk_to` 2 m) use **XY distance** — blip/waypoint Z is often
  ground-projected and would make 3D arrival unreachable.
- `enter_nearest_vehicle` prefers empty vehicles, and `"nicer"` is a *preference*: if nothing
  outranks the current/last vehicle's class (`VehicleRank` heuristic), it falls back to the
  nearest usable vehicle instead of failing.
- `wander_drive` cruises at 13 m/s; `follow_entity` in-vehicle follows at 15 m/s, 20 m gap; flee
  re-aims at the latest police-spotted position every 5 s.
- Bridge-side watchdog timeouts (→ `failed`/`"timeout"`): drive_to 600 s, walk_to 300 s,
  enter_nearest_vehicle 60 s, exit_vehicle 30 s. `seek_cover` completes (`done`) on cover **or**
  timeout per contract.
- `/state` `vehicle.health` is the entity health float (`Entity.HealthFloat`, 1000 = pristine);
  `weather` is the SHVDN `Weather` enum name upper-cased (e.g. `CLEAR`, `EXTRASUNNY`).
- `/radio` accepts `"off"`, an SHVDN `RadioStation` enum name in any casing/spacing, or a raw
  internal audio name (e.g. `RADIO_01_CLASS_ROCK`) passed straight to `SET_RADIO_TO_STATION_NAME`.
- `edition` is detected from `Game.FileVersion` (build ≥ 2000 ⇒ `legacy`; Enhanced builds are
  1.0.1158.x) — a heuristic that only matters if this DLL is ever loaded by the SHVDN-Enhanced
  fork; the pinned official SHVDN is Legacy-only (RESEARCH.md D1).

### ⚠ Objective-blip rule is empirical

`mission.objective_blip` is identified as **sprite `Standard(1)` + colour `Yellow(66)` + route
enabled** (`SnapshotBuilder.FindObjectiveBlip`). That is community convention, not documented by
any primary source; some missions use character-sprite blips (M/F/T) instead, and
`World.GetAllBlips` is an SHVDN **memory scan** — the fragile part on any edition change. The rule
gets validated per mission in Phase 4; refinements land in `FindObjectiveBlip` without changing the
field's shape.

## Build

```
cd bridge
dotnet build -c Release        # → bin/Release/net48/WastedBridge.dll
```

SDK-style project, `TargetFramework net48` (builds on any OS via
`Microsoft.NETFramework.ReferenceAssemblies`; verified with .NET SDK 8 on macOS arm64).
References: `lib/ScriptHookVDotNet3.dll` (not copied to output) and Newtonsoft.Json 13.0.4
(copied — must be deployed alongside).

## Deploy (Windows game server)

1. Prereqs on the server: Script Hook V + `ScriptHookVDotNet.asi`/`ScriptHookVDotNet3.dll`
   (v3.7.0-nightly.189) in the game root, game launched with `-nobattleye`.
2. Copy `bin/Release/net48/WastedBridge.dll` **and** `Newtonsoft.Json.dll` into
   `<game root>/scripts/` (`scripts/deploy-bridge.ps1` does exactly this).
3. If the game runs under a non-admin account and the listener logs an access-denied bind error,
   run once: `netsh http add urlacl url=http://127.0.0.1:7777/ user=Everyone`.

## In-game verification (Phase 1, on the server — not runnable on a dev machine)

Run `scripts/bridge-smoke.ps1` with the game in Story Mode; it must show:

1. `GET /health` → 200, `tick_hz` ≈ frame rate, `online_blocked: false`.
2. `GET /state` → 200 with live position/street/clock changing across calls.
3. `POST /task` `walk_to` a point 10 m away → 202; `/state.last_task` goes `running` → `done`.
4. `drive_to` in a vehicle with each of the four styles; observe arrival within
   `arrive_radius_m`; posting a second task mid-drive → first shows `failed`/`"preempted"`.
5. `enter_nearest_vehicle`, `exit_vehicle`, `wander_drive`, `flee_police` (cheat-free: gain a
   star by bumping a police car), `combat_hated_targets_around`, `seek_cover`, `follow_entity`,
   `set_waypoint`, `stop` — each reaches its contract end state.
6. `/timescale` 0.5 visibly slows the game and `/state.world.timescale` reports it; restore 1.0.
7. `/radio`, `/horn` audible; `/control` toggling blocks/unblocks keyboard input (observed
   behavior documented here afterwards, per CONTRACTS §1).
8. `/unstick` while driving normally → 409; wedge the car > 20 s → 200 `moved: true` and a
   matching `UNSTICK` line in `WastedBridge.log`.
9. Cutscene + mission flags: start a story mission, confirm `mission.active`,
   `cutscene_active`, and `objective_blip` behave; note per-mission blip deviations here.
10. Load into GTA Online **once, deliberately, without the harness**: every endpoint must return
    `503 online_session_active` (then restart the game back into Story Mode).

Until those pass on the server, this package's status is **authored-awaiting-server** (built and
HTTP-layer-tested locally; game-dependent behavior unproven).
