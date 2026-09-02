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

The script logs the SHVDN assembly version it is actually loaded by at startup and shouts if it is
not `3.7.0.189` (`Diagnostics.ExpectedShvdnVersion`).

### API re-verification (2026-08-29, against `lib/ScriptHookVDotNet3.dll` metadata)

Every wrapper and raw native the bridge calls was re-checked member by member against the pinned
DLL's own metadata (types, argument order, defaults, `[Obsolete]` flags) and — for raw calls —
against alloc8or's `natives.json` (2026-07-16, builds 3889.0/1158.13):

| Call site | Verified signature in nightly.189 |
|---|---|
| `Task.DriveTo` | `(Vehicle, Vector3, float speed, VehicleDrivingFlags, float radius)` — the nightly order, not stable's `(veh, target, radius, speed, DrivingStyle)` |
| `Task.CruiseWithVehicle` | `(Vehicle, float speed, VehicleDrivingFlags)` — non-obsolete overload |
| `Task.FollowNavMeshTo` | `(Vector3, PedMoveBlendRatio? = null, int timeBeforeWarp = -1, float radius = 0.25f, …)` |
| `Task.EnterVehicle` | `(Vehicle, VehicleSeat = Any, int timeout = -1, float speed = 1, EnterVehicleFlags = None)` |
| `Task.LeaveVehicle` | `(LeaveVehicleFlags = None)` |
| `Task.CombatHatedTargetsAroundPed` | `(float radius, TaskCombatFlags = 0)` (stable's `FightAgainstHatedTargets` is `[Obsolete]`) |
| `Task.FollowToOffsetFromEntity` | `(Entity, Vector3 offset, float movementSpeed, int timeout = -1, float distanceToFollow = 10, bool persistFollowing = true)` |
| `Task.VehicleFollow` | `(Vehicle, Entity, float cruiseSpeed, VehicleDrivingFlags, int followDistance = 20)` |
| `Task.FleeFrom` | `(Vector3, float safeDistance, int duration, bool quitIfOutOfRange = false)` |
| `Player.Wanted.WantedLevel` / `.LastPositionSpottedByPolice` | present; `Player.WantedLevel` is `[Obsolete]` |
| `Player.SetControlState` | `(bool, SetPlayerControlFlags = None)` — the non-obsolete replacement for the `[Obsolete]` `CanControlCharacter` setter; the bridge now uses it instead of a raw `SET_PLAYER_CONTROL` call |
| `GameClock.Hour/.Minute` | present (`World.CurrentTimeOfDay` is `[Obsolete]`) |
| `World.GetAllBlips/GetNearbyVehicles/GetNearbyPeds/GetStreetName/GetZoneLocalizedName/Weather/WaypointPosition` | all present with the argument shapes used |
| `Blip.Position/.Color/.ShowRoute/.BlipType`, `BlipSprite.Standard=1`, `BlipColor.Yellow=66` | confirmed |
| `Entity.Speed` | documented "in m/s" — matches the contract's speed unit |
| `World.Weather` | documented as the *previous* weather hash, which is the weather in force **now**; `NextWeather` is the one coming. Do not "fix" this to `NextWeather`. |

Raw `Function.Call` sites (no wrapper exists) — hash **and** parameter list matched against
`natives.json`: `TASK_SEEK_COVER_FROM_POS 0x75AC2B60386D89F2 (Ped, float x, float y, float z,
int duration, BOOL allowPeekingAndFiring)`, `IS_PLAYER_BEING_ARRESTED 0x388A47C51ABDAC8E
(Player, BOOL atArresting)`, `NETWORK_IS_SESSION_STARTED 0x9DE624D2FC4B603F ()`,
`NETWORK_IS_GAME_IN_PROGRESS 0x10FAB35428CCC9D7 ()`, `SET_RADIO_TO_STATION_NAME 0xC69EDA28699D5107
(const char*)`. No mismatches were found; the only change made was replacing the raw
`SET_PLAYER_CONTROL` call with `Player.SetControlState`.

Driving-style bitfields are written as named `VehicleDrivingFlags` members instead of magic
integers, so a bump that **renames or removes** a member fails the build. Renaming is not the
dangerous case, though: a bump that silently **renumbers** a bit still compiles, because the C#
compiler folds those `const`s into literals.

`DrivingStyles.Verify()` covers that case at startup. It re-reads the members from the SHVDN
assembly *actually loaded next to this DLL* (`Enum.Parse` against the runtime type), re-composes
each contract style from those live values, and compares the result with both the D3 decimal and
the constant this DLL was compiled with; drift is logged as a `WARN` line naming the style, the
composed value and the expectation.

> The earlier version of this check compared the compiled constants with the D3 decimals. Both
> sides were compile-time literals, so it could never fail — a "drift guard" that was really a
> comment. The offline checks now run `Verify()` against a deliberately renumbered SHVDN stub and
> **require** it to report drift, so the guard cannot silently rot back into a tautology.

## Architecture (frozen)

One `GTA.Script` subclass, `WastedBridgeScript` (`src/WastedBridgeScript.cs`):

- **Game thread (Tick handler)** — the only place natives run. Each tick it: retries the HTTP bind
  if it is not up, checks the online guard, drains a bounded command queue, updates the task state
  machine (`src/TaskEngine.cs`), rebuilds the `/state` snapshot (`src/SnapshotBuilder.cs`) and
  publishes it as a pre-serialized JSON string (Newtonsoft).
- **HTTP threads** (`src/HttpServer.cs`, `src/HttpTransport.cs`, `src/BridgeRouter.cs`) — GETs
  return the cached snapshot; POSTs validate, enqueue a command, and either return
  `202 {"task_id"}` immediately (`/task`) or block briefly until the game thread has applied the
  command (`/timescale`, `/control`, `/radio`, `/horn`, `/unstick`) so the `200` reflects work that
  actually happened. If a tick doesn't come within 1.5 s (loading screen, hang) they return
  `503 game_thread_stalled` — deliberately shorter than the harness's own 2 s client timeout, so a
  stalled game thread reads as an explicit bridge error rather than as "bridge down".
  **Natives are never called from HTTP threads.**
- Shared state (`src/BridgeShared.cs`) is one volatile snapshot reference + the command queue.
- `src/Diagnostics.cs` writes the two first-run log blocks (see below).

### Failure containment (why the bridge does not die quietly)

- **A throw in `Tick` does not kill the script.** SHVDN aborts a script whose tick handler throws,
  and an aborted bridge is a dead stream with no HTTP endpoint to notice it. The whole tick body is
  guarded; failures are counted and logged at most every 5 s. The task-engine update and the
  snapshot build have their own inner guards so one broken subsystem cannot stop the other.
- **The objective-blip scan is guarded separately.** `World.GetAllBlips` is an SHVDN memory scan and
  the most likely thing to break on a game update; when it throws, `mission.objective_blip` goes
  null and the rest of `/state` keeps flowing.
- **The command queue is bounded** at 128 (`BridgeShared.MaxQueueDepth`). Past that, requests get
  `503 queue_full` instead of the process quietly accumulating stale commands during a loading
  screen.
- **A stalled POST cannot be answered twice.** After the 2 s wait the HTTP thread abandons the
  reply; a late completion from the game thread is dropped.
- **The listener is released on reload.** `Aborted` (which SHVDN raises on script reload as well as
  shutdown) stops the transport, and the new instance retries the bind every 10 s until the port is
  free — so the classic "port already in use after reload" case heals itself instead of needing a
  game restart.
- **The bridge never throws out of its constructor.** A bind failure is logged with a fix, and the
  script keeps ticking and retrying.

## Safety

- **Online guard**: `NETWORK_IS_SESSION_STARTED` (+ `NETWORK_IS_GAME_IN_PROGRESS` belt-and-braces)
  checked at startup and every tick, and the two flags are logged separately so a trip is
  diagnosable. On detection the script latches off — no further natives, and every endpoint returns
  `503 {"error": "online_session_active"}` until the game restarts.
- **No teleport, god-mode, money, or weapon endpoints exist.** The only positional write in the
  entire codebase is the `/unstick` nudge (`WastedBridgeScript.ApplyUnstick`): ≤ 3 m (2.5 m
  backward + 0.25 m lift), only when a drive task is running **and** the vehicle has been at
  standstill (speed < 0.2 m/s) for > 20 s, re-verified on the game thread, always written to the
  file log. Anything else → `409 unstick_conditions_not_met`.

## Log file

`WastedBridge.log`, 5 MB rotation to `.log.old`. Location, in order of preference — the first one
that can actually be opened for append wins, and the choice is printed in the startup block:

1. `<game root>\scripts\WastedBridge.log` (next to the deployed DLL — look here first)
2. `%LOCALAPPDATA%\WASTED\WastedBridge.log` (used when `scripts\` is read-only for the game's
   account, which happens on installs under `C:\Program Files`)
3. `%TEMP%\WastedBridge.log`

If none is writable the bridge still runs, and the startup block would have said
`log file: <NONE WRITABLE …>` — except that with no log there is nowhere to say it, which is why
the fallback chain exists.

### What the log says on a first run

Two blocks. The first is written from the constructor, before anything can fail:

```
===== WANTED bridge starting =============================…
  bridge           : 1.0.0 (assembly 1.0.0.0)
  shvdn            : ScriptHookVDotNet3 3.7.0.189 (matches the pinned build)
  newtonsoft       : 13.0.4 from C:\...\scripts\Newtonsoft.Json.dll
  script dir       : C:\...\Grand Theft Auto V\scripts
  log file         : C:\...\Grand Theft Auto V\scripts\WastedBridge.log
  process          : GTA5 pid 1234 · user WIN-XXXX\wanted · elevated True
  os / clr         : Microsoft Windows NT 10.0.26100.0 · CLR 4.0.30319.42000 · 64-bit
==========================================================…
```

The second is written from the first tick that has a **live player ped** (SHVDN starts ticking at
the main menu, where world reads would be nothing but noise — until then the log says
`no player ped yet (main menu or loading screen)` once, `/health` is live and `/state` returns
`503 not_ready`). It is the one that proves natives resolved
against the running game build — game version and detected edition, both online-guard flags, the
HTTP endpoint actually in use, player ped/handle/position/health, wanted level, money, control
state, street/zone/clock/weather/timescale, the three mission flags, the blip memory scan, and the
nearby-entity scan. Each line is probed independently, so a single failure shows as
`FAILED: <ExceptionType>: <message>` on its own line instead of hiding the rest.

**If the game starts and nothing appears in `WastedBridge.log`, the script never loaded at all** —
look in `ScriptHookVDotNet.log` in the game root (wrong .NET target, missing `Newtonsoft.Json.dll`
next to `WastedBridge.dll`, or SHVDN itself not loaded).

## HTTP transport, and the Windows URL-ACL trap

`HttpListener` on Windows is a front end for **HTTP.SYS**, which requires the URL namespace to be
*reserved* before a non-administrator process may listen on it. Microsoft's guidance is explicit
that only the `localhost` host name is exempt: *"the code uses **localhost** as host name, which
will only match traffic sent to localhost … My experiments show that using the wildcards, machine
name, or even the loopback addressing 127.0.0.1 requires admin token or namespace reservation."*
([Microsoft Learn archive](https://learn.microsoft.com/en-us/archive/blogs/haoxu/one-note-about-running-the-examples-using-http)).
CONTRACTS §1 fixes the endpoint at `http://127.0.0.1:7777`, so the exemption does not help us: a
`localhost` prefix would not match a client that connects to `127.0.0.1` (HTTP.SYS routes on the
`Host` header). Without a reservation, `HttpListener.Start()` throws
`HttpListenerException` with `ErrorCode == 5` (`ERROR_ACCESS_DENIED`).

The bridge handles this in three layers:

1. **It tries HTTP.SYS first** and, on `ERROR_ACCESS_DENIED`, logs the exact `netsh` command with
   the current account already filled in.
2. **It then falls back automatically to a loopback socket transport** (`SocketTransport`) — a
   plain `TcpListener` on `127.0.0.1:7777` speaking the slice of HTTP/1.1 the API needs. A
   user-mode socket on a high port needs no privilege and no reservation, and it answers every
   `Host` header. This is the layer that means **no elevated command is required on delivery day.**
3. **The operator can pin a transport** with the `WASTED_BRIDGE_TRANSPORT` environment variable:
   `auto` (default), `socket`, or `httpsys`. Set it in the environment the game is launched from.

If you would rather use HTTP.SYS, run this **once, from an elevated prompt**, substituting the
account the game runs as (the script logs the exact string, and `netsh http add urlacl` is
documented [here](https://learn.microsoft.com/en-us/windows/win32/http/add-urlacl)):

```
netsh http add urlacl url=http://127.0.0.1:7777/ user="MACHINE\wanted"
```

`user=Everyone` also works and is what a locked-down single-purpose box can live with. To inspect
or undo:

```
netsh http show urlacl url=http://127.0.0.1:7777/
netsh http delete urlacl url=http://127.0.0.1:7777/
```

Neither transport opens anything outside the loopback interface, so no firewall rule is needed and
none should be added.

## Endpoints and tasks

Exactly the CONTRACTS §1 surface: `GET /state`, `GET /health`, `POST /task` (all 11 types,
new task preempts the running one → `failed`/`"preempted"`), `POST /timescale` (clamped 0.1–1.0),
`POST /control`, `POST /radio`, `POST /horn` (1–3000 ms), `POST /unstick`. Errors are
`{"error": "<snake_code>", "detail": "..."}`.

CONTRACTS **v1.2** enumerates and closes the error-code set: `online_session_active`,
`not_ready`, `game_thread_stalled`, `queue_full` (503) · `unknown_task_type`, `invalid_params`,
`invalid_json` (400) · `not_in_vehicle`, `unstick_conditions_not_met` (409). The bridge also emits
codes outside that set for cases the contract does not model — `method_not_allowed`, `not_found`,
`internal_error`, plus `body_too_large` / `length_required` / `headers_too_large` / `bad_request`
from the socket transport's protocol layer — which is exactly why v1.2 requires consumers to log
and tolerate unknown codes rather than crash. Real captured responses for every offline-reachable
code live in `contract-samples/errors.json`.

Two more v1.2 clarifications this package implements: `last_task.id` and `last_task.type` are
`null` (key present, value null) until the first task is posted — `status`/`detail` are always
present — and `bridge.edition` / `/health.edition` may be `"unknown"` until detection succeeds.
Both are asserted by the offline checks and visible in `contract-samples/state-fresh-load.json`.

Driving styles (D3): `normal=786603`, `rushed=1074528293`, `ignore_lights=786475`,
`avoid_traffic=786468` (`src/DrivingStyles.cs`). The names are the contract; bit values may be
tuned empirically in Phase 3.

Implementation choices where the contract is silent (all in code comments too):

- Arrival checks (`drive_to` radius, `walk_to` 2 m) use **XY distance** — blip/waypoint Z is often
  ground-projected and would make 3D arrival unreachable.
- `enter_nearest_vehicle` prefers empty vehicles, and `"nicer"` is a *preference*: if nothing
  outranks the current/last vehicle's class (`VehicleRank` heuristic), it falls back to the
  nearest usable vehicle instead of failing.
- `wander_drive` cruises at 13 m/s. `follow_entity` in-vehicle follows with a 20 m gap; CONTRACTS
  v1.9 gives it caller-supplied `style`/`speed_mps` (defaulting to `ignore_lights`/30 m/s when
  omitted — the old hard-coded `DrivingStyles.Normal` + 15 m/s cruise cap could not keep pace with
  a mission NPC, who neither stops for lights nor caps its own speed, and lost the target outright).
  An explicit `speed_mps` is clamped to 1–60 m/s (`TaskEngine.FollowVehicleMinSpeedMps`/
  `MaxSpeedMps`) so a malformed value cannot produce an undrivable tail. Flee re-aims at the latest
  police-spotted position every 5 s.
- Bridge-side watchdog timeouts (→ `failed`/`"timeout"`): drive_to 600 s, walk_to 300 s,
  enter_nearest_vehicle 60 s, exit_vehicle 30 s. `seek_cover` completes (`done`) on cover **or**
  timeout per contract.
- Tasks fail with `not_in_vehicle` when the player ped reports a vehicle that no longer exists, and
  with `no_player_ped` during a character switch or load transition (the harness retries).
- **Preemption is not observable in `/state`.** CONTRACTS §1 says a preempted task ends as
  `failed`/`"preempted"`, but `last_task` is one slot and the replacement is applied on the same
  tick, so a `/state` poll never catches the old task in its terminal state. The harness detects
  preemption from `last_task.id` changing while the previous task was `running`; the log carries
  the explicit `failed: preempted by …` line. Making this visible in `/state` would need a second
  field (a contract change) — reported to the orchestrator rather than done here.
- `/state` `vehicle.health` is the entity health float (`Entity.HealthFloat`, 1000 = pristine);
  `weather` is the SHVDN `Weather` enum name upper-cased (e.g. `CLEAR`, `EXTRASUNNY`).
- `/radio` accepts `"off"`, an SHVDN `RadioStation` enum name in any casing/spacing, or a raw
  internal audio name (e.g. `RADIO_01_CLASS_ROCK`) passed straight to `SET_RADIO_TO_STATION_NAME`.
- `edition` is detected from `Game.FileVersion` (build ≥ 2000 ⇒ `legacy`; Enhanced builds are
  1.0.1158.x) — a heuristic that only matters if this DLL is ever loaded by the SHVDN-Enhanced
  fork; the pinned official SHVDN is Legacy-only (RESEARCH.md D1). Before detection succeeds it
  reports `unknown`, which CONTRACTS v1.2 allows — but only as a *transient* state: detection
  **retries every 5 s until it succeeds** and latches only on success (`src/EditionDetector.cs`).
  The first read happens on the first tick, which can land on a loading screen, and an earlier
  version of this code latched after a single attempt — one throw there pinned `edition: "unknown"`
  for the whole session. The retry policy and the version→edition rule are pure, so the offline
  checks exercise them; only the `Game.FileVersion` read itself needs the game.

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
(copied — must be deployed alongside). `tools/` is excluded from the compile glob: it holds the
off-server check harness (net8.0) and its SHVDN stubs, none of which ship — see *Off-server
checks* below.

## Deploy (Windows game server)

1. Prereqs on the server: Script Hook V + `ScriptHookVDotNet.asi`/`ScriptHookVDotNet3.dll`
   (v3.7.0-nightly.189) in the game root, game launched with `-nobattleye`.
2. Copy `bin/Release/net48/WastedBridge.dll` **and** `Newtonsoft.Json.dll` into
   `<game root>/scripts/` (`scripts/deploy-bridge.ps1` does exactly this).
3. Nothing else is required. A URL ACL is optional (see above); the bridge falls back to the
   loopback socket transport by itself if HTTP.SYS refuses.

## Delivery day: in-game verification (on the server — not runnable on a dev machine)

Preconditions before any of this means anything: the game is running **in the console session**
(not inside an RDP session), in **Story Mode**, windowed-borderless, with a real display present
(physical, or a virtual display driver on a headless box). Detach with `tscon … /dest:console`
rather than closing the RDP window, or the session — and the game with it — goes away.

**Step 0 — read the log before touching HTTP.** Open `WastedBridge.log` (see *Log file* above).
The two startup blocks answer, in order, every question a failed first attempt raises:

| Symptom | What the log tells you |
|---|---|
| No log file anywhere | The script never loaded — check `ScriptHookVDotNet.log` in the game root |
| Only the "starting" block, never "first tick" | Either the game never got past the main menu (look for the `no player ped yet` line, and for `player ped is live` once it does), SHVDN never ticked the script, or the online guard tripped — the guard logs its own ERROR line naming both flags |
| `shvdn: … !!! MISMATCH` | Wrong SHVDN build is installed; every wrapper signature in this README was checked against 3.7.0.189 |
| `bind failed on HTTP.SYS … ERROR_ACCESS_DENIED` followed by `listening on … loopback socket` | Working as designed; no action needed |
| `NO HTTP TRANSPORT COULD BIND` | Something else owns port 7777 — `netstat -ano \| findstr :7777` |
| A `FAILED:` line inside the first-tick block | That specific native/wrapper did not resolve; everything else did |

**Step 1 — transport, before the game is even interesting.** From the server:
`curl http://127.0.0.1:7777/health` → 200 with `tick_hz` ≈ frame rate and `online_blocked: false`.
Connection refused ⇒ read the log, not the code.

**Steps 2–11 — `scripts/bridge-smoke.ps1` with the game in Story Mode.** It must show:

1. `GET /health` → 200, `tick_hz` ≈ frame rate, `online_blocked: false`.
2. `GET /state` → 200 with live position/street/clock changing across calls.
3. `POST /task` `walk_to` a point 10 m away → 202; `/state.last_task` goes `running` → `done`.
4. `drive_to` in a vehicle with each of the four styles; observe arrival within
   `arrive_radius_m`; posting a second task mid-drive → `/state.last_task.id` switches to the new
   task and `WastedBridge.log` records the old one as `failed: preempted by …`. (`last_task` is a
   single slot and the new task is applied in the same tick, so the preempted task's terminal
   state is observable in the log, not in a `/state` poll — see the note below.)
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

**Step 12 — reload survival.** With the game running, press SHVDN's reload key (Insert by default).
The log must show `script aborted; bridge shut down`, then a fresh startup block, then
`HTTP API listening on …` within ~10 s, and `/health` must answer again. This is the check that
proves a redeploy of `WastedBridge.dll` does not require a game restart.

**Step 13 — the log after an hour.** `WastedBridge.log` should contain task lines and nothing else.
Repeating `tick failed`, `snapshot build failed`, or `objective-blip scan failed` lines name the
subsystem to fix; none of them stop the stream, which is exactly why they must be read rather than
waited on.

Until steps 1–13 pass on the server, this package's status is **authored-awaiting-server**.

## Off-server checks — `bridge/tools/offline-checks`

```
bridge/tools/offline-checks/run.sh        # builds the bridge, regenerates the stubs, runs everything
```

The harness lives in the repo (it used to be a throwaway script in a scratch directory, which is
why nothing it produced could be regenerated or reviewed). It builds `bridge/` in Release, loads
the compiled **net48 `WastedBridge.dll` under .NET 8**, and drives the real routing, transport and
serialization code. It rewrites `bridge/contract-samples/` on every run and exits non-zero if any
check fails.

### ⚠ What it substitutes: a stub ScriptHookVDotNet3 assembly

**The real SHVDN is not loaded by these checks.** The pinned nightly is an x64 PE for .NET
Framework, and arm64 CoreCLR refuses it (`BadImageFormatException`), so `WastedBridge.dll` cannot
resolve its `GTA.*` types on a dev machine. The harness therefore resolves `ScriptHookVDotNet3` to
a generated stand-in (`tools/offline-checks/shvdn-stub/`) that carries the same assembly identity
and exactly one type — `GTA.VehicleDrivingFlags` — whose members are **generated verbatim from the
real DLL's metadata** (read with `MetadataLoadContext`, which never executes the image; the
generator records the source DLL's SHA-256 in the file header). Nothing about the stub is
hand-written and nothing about it is deployed: the game server loads the real SHVDN.

The consequence is the honest one: **no code path that touches a native is exercised here.** The
task engine, the snapshot *builder*, the online guard's flag reads, `/timescale`, `/control`,
`/radio`, `/horn` and `/unstick`'s actual effects, `not_in_vehicle`, HTTP.SYS's real ACL behaviour
on Windows Server, and SHVDN's loader are all delivery-day items (steps 1–13 above). What the
checks prove is the layer above the natives: routing, validation, both transports, the queue
bound, the online kill switch, the log fallback, reload recovery, and — since 2026-08-29 — the
serialized shape of `/state` and `/health`.

### What it covers (185 checks, all passing 2026-08-29)

- `dotnet build -c Release --no-incremental` clean: 0 warnings / 0 errors (.NET SDK 8.0.424,
  macOS arm64).
- **CONTRACTS §1 surface over both transports** (HTTP.SYS-style `HttpListener` and the loopback
  socket) with a real `HttpClient`: all 11 task types → 202 with `t-` ids, every param/unknown-type
  400, unstick 409, `queue_depth` bound → `queue_full`, the online kill-switch 503 on every
  endpoint, method/404/normalization routing.
- **Raw-socket protocol checks** of the hand-rolled transport: status line, `Content-Length`
  correctness, `Expect: 100-continue`, chunked → 411, oversize → 413, malformed request line → 400,
  absolute-form target, arbitrary `Host`, idle connection.
- **Log-directory fallback** off an unwritable scripts dir, and a **simulated script reload**
  (double-bind refused, then automatic rebind and service restored).
- **`/state` serialization** — real `Snapshot` objects built from the compiled assembly's own DTOs
  and serialized through the bridge's own `SnapshotJson.Serialize`, then checked against all 68
  documented CONTRACTS §1 field specs (presence, JSON type, enums, `ts`/`clock` formats) with a
  "no undocumented keys" check in both directions. `last_task.id`/`type` are asserted to be
  **present and null** on a fresh load, from a real `TaskEngine.ToDto()`.
- **`/health` shape** captured from the real router and checked field by field.
- **Error codes**: every CONTRACTS v1.2 code reachable without the game is provoked over HTTP and
  asserted to carry the contract's status (`not_ready`, `game_thread_stalled`, `queue_full`,
  `online_session_active`, `unknown_task_type`, `invalid_params`, `invalid_json`,
  `unstick_conditions_not_met`). `not_in_vehicle` is game-thread-only.
- **Driving-style bitfields** read from the real SHVDN DLL's metadata and confirmed equal to the D3
  decimals, plus `DrivingStyles.Verify()` run both ways: no drift against the pinned values, and
  **drift correctly reported** against a renumbered stub.
- **Edition-detection policy**: classification of a Legacy and an Enhanced build, an unreadable
  version staying unresolved, the 5 s retry actually retrying (the regression that pinned
  `unknown`), latching only on success, and `Environment.TickCount` wrap safety.

### Contract samples — `bridge/contract-samples/`

| File | Contents |
|---|---|
| `contract-samples/state-fresh-load.json` | `GET /state` on the first poll of a session: `last_task.id`/`type` **null**, `vehicle` null, `objective_blip` null, `edition: "unknown"` |
| `contract-samples/state-populated.json` | `GET /state` with every optional branch present (vehicle, nearby, mission blip, running task) |
| `contract-samples/health-fresh-load.json` | `GET /health` before the first tick |
| `contract-samples/errors.json` | Real error responses, one per offline-reachable v1.2 code |
| `contract-samples/README.md` | Generated provenance: how they were produced, what is real, what is synthetic |

These are **real serializations from the compiled assembly**, not hand-written: the `state-*` and
`health-*` files are byte-for-byte the HTTP response bodies. The world *values* are synthetic
(natives cannot run here) — the shape, null handling and number formatting are not. The harness
package validates its pydantic models against these files; regenerate them with `run.sh` after any
change to `src/Snapshot.cs`.
