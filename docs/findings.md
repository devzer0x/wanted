# findings.md — why the agent is not fun to watch (2026-09-03, stream paused)

Root causes ranked by impact, with file:line evidence, then a ticket list. Every ticket is one
root cause, one owner, one acceptance check. Nothing here is fixed yet.

Evidence sources: the live harness log `harness-20260903-1141*.log.err` on the box, the bridge
log `scripts/WastedBridge.log`, `/state` read at 09:14Z and 09:44Z, the dashboard, and the code
at commit `f1c0498`. Recorded `/state` dumps: none exist yet (fix-sonnet-e is building the
recorder); the last 2 h of logs will be pulled and this doc amended when sshd re-accepts.

## R1 — the driver's seat: no drive-start sequence, and the game clears the task (impact: F1, F6)

**Evidence.** Bridge log, 09:14Z, for minutes: `task t-0000NN (enter_nearest_vehicle) started`
→ ~1.0 s → `failed: cleared_by_game`, re-posted within 300 ms by whichever owner got the wheel
next (day_plan, brain, roam). `/state` at that moment: `in_vehicle: False`, an EMPTY Prairie
2.84 m away, `last_task.status: running`. At 09:44Z: `phone: {"ringing":false,"in_call":true}`
— a story call was CONNECTED the whole time; the game takes the ped's task for the phone UI.
**Code.** `bridge/src/TaskEngine.cs`: no `SET_VEHICLE_ENGINE_ON` anywhere; seat checks are
`ped.IsInVehicle()` only (`:274`, `:420`, `:462`, `:658`) — never `IS_PED_IN_VEHICLE(atGetIn=false)`
nor `GET_PED_IN_VEHICLE_SEAT(veh,-1)==ped`; wander is `ped.Task.CruiseWithVehicle(veh, 13 m/s,
style)` at `:714` with no post-start verification that the task is running and speed > 0.
`harness/wasted_harness/behavior/vehicle.py` posts `wander_drive` on SEATED and verifies motion
from `vehicle.speed`/`stopped_for_s` — but a task cleared 1 s in is indistinguishable from
"not yet moving" until the watchdog window (6 s), by which time three owners have re-posted.
**Also.** `ClearedByGameBackoff` (recovery.py, wired into `_execute_action` at `f1c0498`) now
refuses a type the game just cleared — it stops the storm; it does not start the car.

## R2 — a line on every decision, and decisions happen on every poll (impact: F3 "narrating")

**Evidence.** `main.py:2352-2358`: every decision's `d.say` goes through `commentary.gate_say`
(dedupe) and is published to the bus and recorded — unconditionally on the DECISION, not on an
EVENT. `main.py:3552`: tactical decisions are made on the poll trigger. So a stationary the agent
whose brain is polled every 8–25 s emits a fresh line each time with nothing behind it — the
transmissions panel filling while the car does not move. `gate_say` only dedupes text; it does
not ask "did anything happen".

## R3 — the mission goal leaks through the missions-off switch (impact: F2, the dashboard)

**Evidence.** Dashboard CURRENT GOAL: "walk to Franklin's marker and start the job" with
`WASTED_MISSIONS_ENABLED=false`. The switch gates `RoamEngine.mission_forced()`/`available()`
(`roam.py`) and `DayPlanner._overdue_for_a_mission`/`request_mission_block` (`planner.py`) — but
the BRAIN set that goal itself: `_dynamic_context` still dumped `mission.starts[]`, and
`director.md` said "roam, then work". Fixed at `788956b` (MISSIONS ARE OFF line, starts
stripped, both prompts told) — **not yet deployed** (sshd throttle). Remaining design gap: the
dashboard goal is still model-writable outside a locked roam goal (`main.py` `dashboard_goal()`
falls back to `self.current_goal`, which the director writes).

## R4 — the model's goal id never reached the plugin (impact: F2, F4)

**Evidence.** Live log, every pick: `goal_fallback: the model named no offered goal ... said="cruise
around, find a bike, aim for a hill"`. Two prompts contradicted each other (`situations.md` "pick
ONE by id" vs `decision_guide.md` "goal is an echo, repeat it unchanged"); the echo rule won.
Fixed in prompts (`2d9338e`) and the menu line now names the field and the bare ids
(`roam.py note()`), **not yet deployed**. No regenerate-once path exists; invalid → `available[0]`.

## R5 — reflex-class recovery preempted every roam goal within 2 s (impact: F2, F4) — FIXED, deployed

`wheel preempted owner=roam by=stranded ... duration_s=0.3` — `stranded` outranked roam and
preempted `roam_the_block`, whose own plan IS `enter_nearest_vehicle`. Guard at `main._reflex`
(`c6cb891`): a live roam goal stands the stranded ladder down. Deployed 11:41Z.

## R6 — the phone (impact: R1, F2)

A connected story call takes the ped's task (R1) and ringing rings out unseen. Bridge 1.6.0 /
CONTRACTS v1.13 adds `phone{ringing,in_call}`, `answer_call`, `reject_call` via the game's control
layer (`PhoneSelect` 176 / `PhoneCancel` 177 through `SET_CONTROL_VALUE_NEXT_FRAME`); reflex
policy: missions off → reject a ring once, hang up a connected call once (`f1c0498`). Bridge
deployed 11:41Z; harness with the hang-up **not yet deployed**. NOT VERIFIED in-game: whether
control group 0 registers, whether `IS_PED_RINGTONE_PLAYING` is a clean ringing proxy on this
build, how un-rejectable story calls behave.

## R7 — the brain is dead: API key 401 on the box (impact: everything)

`harness-20260903-114118.log.err`: `FATAL: startup model check failed ... 401 authentication_error`.
The key in the server's `.env` AND in the dev `.env` both return 401 as of 09:44Z. This is a
physical-world item (CLAUDE.md rule 4): the operator must supply the enabled key. Until then the
harness cannot start; everything below is built and verified offline with the fake brain.

## R8 — weapons, targeted attacks, drive-bys, airtime, taxis: not expressible (impact: chaos catalog)

`brain/schemas.py`: 14 bridge tasks + 8 primitives; no weapon give/select, no `shoot_at`, no
`drive_by`, no airtime field, no passenger seat (`enter_nearest_vehicle` seats DRIVER). Four
catalog goals are declared impossible for exactly this (`roam.py UNBUILDABLE_GOALS`).

## Numbers from the last 2 h of logs (sessions 20260903-104653, 20260903-105156, 20260903-110549, 20260903-114118), pulled 11:56Z

| signal | count | reading |
|---|---|---|
| `roam goal picked` | 22 | picks across 4 sessions ({'roam_the_block': 9, 'steal_nice_car': 5, 'drive_to_landmark': 4, 'start_nearest_mission': 2, 'pick_a_fight': 1, 'hijack_bus': 1}) |
| `goal_fallback` | 22 | **every single pick** — the model never once returned an id (R4) |
| `roam goal ended` outcomes | {'preempted': 4, 'stuck': 7, 'completed': 3, 'timeout': 2, 'actions_done_goal_unmet': 4, 'bridge_task_lost': 2} | median duration 10.3 s — goals barely start (R5/R1) |
| `wheel preempted` | 62 | the stranded livelock before `c6cb891` (R5) |
| `cleared_by_game` (bridge log) | 111 | the game clearing `enter_nearest_vehicle` ~1 s after start (R1) |
| `start the job` | 3 | the mission goal leaking with missions off (R3) |
| `FATAL` | 1 | the 401 that killed the newest session (R7) |
| `/state` at pull | idle | `last_task idle`, `in_vehicle false`, `phone` quiet — the harness is down |

F1–F6 "before" numbers: the replayer + funcheck (T10) will produce them from these logs; the
recorder did not exist when they were written, so the harness log is the only stream.

---

# Tickets

| # | Root cause | Owner | Acceptance check | Files |
|---|---|---|---|---|
| T1 | R1 — drive-start sequence + verify: seat confirmed (`IS_PED_IN_VEHICLE(atGetIn=false)` and `GET_PED_IN_VEHICLE_SEAT(veh,-1)==ped`), `SET_VEHICLE_ENGINE_ON`, cruise speed set, wander/drive/follow posted rushed, then within 2 s task running AND speed>0 else re-post once then fail with reason; on-foot equivalents (walk/run/flee); stuck watchdog inside every movement step; F6 on every control-regained edge | fix-opus-a | replay: F1 idle_ratio < 5 %, F6 100 %; unit: cleared-in-1s → one re-post then a reasoned fail, never a storm | `bridge/src/TaskEngine.cs` (driving cases), `BridgeRouter.cs`, `behavior/vehicle.py`, `behavior/navigation.py`, `main.py` movement rungs |
| T2 | R1/R5 — MovementWheel is the ONLY path to movement (acquire before post; refused = do nothing, no timers, no pick) | fix-opus-a | verify: zero ticks with two movement owners over a 2 h replay | `behavior/vehicle.py`, `main.py` |
| T3 | R2 — a line only on an event; no event → `say` forced to "" in code | fix-sonnet-c | funcheck F3: 0 lines without an event on replay | `main.py` say path, `commentary.py` |
| T4 | R4 — validator: names ⊂ STATE, mission name == `mission.name`, banned phrases, dedupe ≥ 0.6, ONE regenerate then drop; roam `goal` ∈ available else regenerate → `available[0]` logged | fix-sonnet-c | fake-brain misbehaviour mode: every bad output caught, exactly one regenerate | `brain/tactical.py`, `director.py`, `schemas.py`, `commentary.py` |
| T5 | R3 — dashboard CURRENT GOAL is plugin-written (`roam.current` desc or `mission.name — objective`); the model never writes it; mood from state | fix-sonnet-c | grep: no path writes the goal box from model output | `main.py dashboard_goal`, `humanizer.py` |
| T6 | R8 — weapon prep on session start and after death (`GIVE_WEAPON_TO_PED` pistol/micro-SMG/pump + ammo), `SET_CURRENT_PED_WEAPON` rule (unarmed→pick_a_fight, pistol on foot, SMG drive-by, shotgun < 10 m); bridge tasks `attack_ped` (`TASK_COMBAT_PED`), `shoot_at` (`TASK_SHOOT_AT_ENTITY`), `drive_by` (`TASK_DRIVE_BY`), airtime (`IS_ENTITY_IN_AIR`), taxi (`TASK_ENTER_VEHICLE` seat 2) — each verified against the pinned SHVDN; CONTRACTS entries proposed in the report, not written | fix-opus-b | bridge builds 0 warnings; harness schema/catalog agreement test green; every new native named with how it was verified | `TaskEngine.cs`/`BridgeRouter.cs`/`Commands.cs` (weapon/combat cases), `schemas.py`, `action_catalog.md` |
| T7 | R8 — chaos ladder L1–L3 with auto step-down on F5; goals: L1 steal_nice_car/freeway_run/big_jump/bike_hills/drive_to_landmark/hijack_bus; L2 pick_a_fight/gang_trouble/steal_cop_car/earn_two_stars→lose_the_cops/drive_by_run; L3 three_star_survival/armed_rampage_block/helicopter_grab; heat goals exempt from the wanted override; every goal has done_when/timeout/cooldown/category/why; opportunistic triggers to the top with a why; mission cadence = config (offer after 6 completed goals, force only after 40 min) | fix-opus-b | replay: F2 longest gap < 60 s, F4 ≥ 60 %/h, F5 auto step-down proven on a synthetic 7-death hour | `behavior/roam.py`, `activities.py`, `planner.py` (cadence) |
| T8 | R6 — phone: answer with `INPUT_CELLPHONE_SELECT` (calls are entertaining and can start story), hang up after 25 s or when a fight/chase is active; `DESTROY_MOBILE_PHONE` if the UI is open with no call > 10 s; try `_SET_CONTROL_NORMAL` first, virtual gamepad fallback | fix-sonnet-d | unit: answer-then-hang-up-at-25 s, hang-up-on-fight; NOT VERIFIED in-game listed | phone cases in `TaskEngine.cs`/`SnapshotBuilder.cs`, `main._phone_reflex` |
| T9 | R1/R5 — reflexes: damaged_by → attack_ped or flee; being_jacked → attack jacker then re-enter; on-road + vehicle incoming → step off; hp<40 → cover; flipped → right it; in water > 10 s → swim to shore; F6 on respawn / mission end / cutscene end / interior exit | fix-sonnet-d | replay: F6 100 %; each reflex fires on its fixture and nothing else | `behavior/recovery.py`, `main._reflex` rungs |
| T10 | Safety net — fake brain, recorder, replayer, funcheck, soak script; before/after F1–F6 | fix-sonnet-e | funcheck table on the synthetic and the recorded streams | `tests/support/`, `tools/`, `docs/FUNCHECK.md` |
| T11 | R7 — the enabled API key onto the box (`C:\wasted\harness\.env`) | operator | `--prompt-audit` on the box prints PROMPT AUDIT PASSED | — |

Parallelism: T1+T2 (a), T3+T4+T5 (c), T6+T7 (b), T8+T9 (d), T10 (e) touch disjoint files, except
`main.py` and `TaskEngine.cs`/`BridgeRouter.cs`, which are split by REGION (a: movement rungs and
driving cases; c: say/dashboard paths; d: reflex rungs and phone cases; b: weapon/combat cases).
Merge order a → c → b → d.


## Ticket status (2026-09-03, PHASE 3 integration)

T1 DONE (fix-opus-a) · T2 DONE (fix-opus-a) · T3/T4/T5 DONE (fix-sonnet-c) · T6/T7 DONE
(fix-opus-b) · T8/T9 DONE (fix-sonnet-d) · T10 DONE (fix-sonnet-e) · **T11 OPEN — operator**
(the enabled API key onto the box; both the box's and the dev machine's keys return 401).

Whole-tree audit (`verify`, read-only): movement single-owner PASS; goal box plugin-only PASS;
no live brain call reachable from tests/tools PASS; commentary-without-event FAIL on two
harness-authored lines — both removed (below).

## Integration findings (found by the soak / the audit after the fan-out; all fixed, all tested)

| # | What the trace showed | Root cause | Fix |
|---|---|---|---|
| I1 | A locked goal sat with nothing running until the stuck watchdog failed it ~20 s later | `ActivityRunner.next_step` abandons the step machine on any foreign RUNNING task; only the wheel's preempt hook closes `roam.current`. Phone tasks never touch the wheel, and `_begin_roam_goal` then released roam's own lease on a `pick()` with nothing to pick | `main._drive_activities` routes on the lock; `_advance_roam_goal` restarts the plan (`_restart_locked_plan`) once the phone task is done, and waits while it runs (posting over it would cancel the answer/hang-up). `tests/test_goal_survives_phone.py` |
| I2 | `earn_two_stars` sat at zero stars for its whole 180 s timeout — the longest silent stretch | The plan was get-in-a-car-and-run-red-lights; the police do not care | Armed: `drive_by` from the seat / `shoot_at` on foot first (bridge 1.7.0 verbs), then drive. Unarmed: the old carjack. `test_roam.py::test_earn_two_stars_*` |
| I3 | The goal above, once active, was closed as `wanted` one poll after its drive-by earned the star | `_drive_activities`' stand-down gate had no `wants_heat` exemption (only `judge()` did) | Gate exempts `roam.heat_is_the_goal()` |
| I4 | ...and the threat reflex fled the star on the next poll, preempting the goal | Rung 6 of `threat_action` (stars alone → `flee_police`) knew nothing about the goal | `heat_wanted=` kwarg; only the stars-alone rung yields, every damage rung still fires. `test_recovery.py::test_the_stars_only_rung_*` |
| I5 | A completed goal, a stopped car, 12.7 s of nothing | The brain said `wait`; the quiet period held the drive-away (`deliberate_wait`) AND the next goal pick, and the next think said `wait` again — the "he's just thinking, not playing" loop seen on stream | `wait` is honoured only with a reason (cutscene / switch / retry / dead / mission / stars), capped at 30 s; in free roam with control it is logged and ignored. `test_mission_following.py::test_a_wait_in_free_roam_*` |
| I6 | `big_jump` airborne at 107 s, timed out at 216 s | The approach is the nearest one; a goal picked 60 m from the ramp jumps under the 100 m run-up bar | `done_when` also accepts airtime ≥ 8 s into the goal; timeout 240 → 120 s. `test_roam.py::test_big_jump_picked_next_to_the_ramp_*` |
| I7 | — (audit) | `flee_ped` in `schemas.BRIDGE_TASKS` and the catalog but not in `bridge_client.BRIDGE_TASK_TYPES` — the same miss as the phone bug | Added; `test_bridge_client.py` tuple updated |
| I8 | — (audit) | "Lost the screen feed" and "Something rebooted" were spoken with no §4 event | Both lines removed; the log keeps the record |
| I9 | — (audit) | A break start posted `stop` under the previous iteration's wheel tick number | `_handle_breaks` opens and closes its own wheel tick |
