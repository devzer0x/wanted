# Go-live: the fun-to-watch program (2026-09-03)

The agent's stream is paused. This is what changed, what the numbers say, what the game still has to
confirm, and the exact steps to put him back on air. Written at HEAD of the program; nothing in
it has been deployed to the box yet.

## 1. What changed, by ticket

| Ticket | Owner | What shipped |
|---|---|---|
| T1 drive-start | fix-opus-a | Bridge 1.7.0: every drive task runs seat-confirmed (`IS_PED_IN_VEHICLE(atGetIn=false)` **and** `GET_PED_IN_VEHICLE_SEAT(veh,-1)==ped`) → `SET_VEHICLE_ENGINE_ON` → task → cruise speed → 2 s verify (task alive **and** `speed>0`) → one re-issue → `drive_did_not_start`/`cleared_by_game`. 10 s no-progress watchdog on walk/enter/flee/drive steps. `walk_to run:true` sprints. New `flee_ped{handle}` (`TASK_SMART_FLEE_PED`). Harness `ControlRegained`: a movement task within 3 s of every control-regained edge (respawn, mission end, cutscene end, interior exit, switch end), `resume` fallback at 2 s posts `wander_drive rushed`. |
| T2 one wheel | fix-opus-a | `post_task` has exactly one caller (`main._execute_action`); every movement type carries a `MovementWheel` token; `_handle_breaks`' bare `stop` was the last hole. 21 600-tick randomised-order proof in `test_drive_start.py`. |
| T3 line = event | fix-sonnet-c | `say` is forced to `""` when the tick has no event (`_tick_event_reason`); both tiers go through `_apply_decision`. Audit found two harness-authored lines without an event ("Lost the screen feed", "Something rebooted") — removed. |
| T4 validator | fix-sonnet-c | `validate_decision_content`: names ⊂ STATE, mission name == `mission.name`, banned phrases (`commentary_style.md`), Jaccard dedupe ≥ 0.6, one regenerate then drop; a roam `goal` not in `available` → head of the menu, logged. |
| T5 goal box | fix-sonnet-c | `Harness.goal_text` = `roam.dashboard_goal()` → mission name — objective → neutral text. The model's `goal` field is never displayed. |
| T6 weapons | fix-opus-b | Bridge 1.7.0 `player.weapon {name,class,ammo,owned,loadout}`, `vehicle.in_air`, `vehicle.seat`; tasks `shoot_at`, `drive_by`, `enter_vehicle_seat`, `fight_ped{weapon}`; loadout default `ammunation` (operator decision, honours `off`). CONTRACTS v1.14. |
| T7 chaos catalog | fix-opus-b | L1–L3 ladder (`ChaosLadder`): starts at L2, **down** one level after 6 roam deaths in an hour, **up** one after 30 clean minutes. New goals `drive_by_run`, `three_star_survival`, `armed_rampage_block`, `helicopter_grab`, `big_jump`, `taxi_ride`; heat goals exempt from the wanted override; opportunistic triggers to the top of the menu; mission cadence via `WASTED_ROAM_GOALS_BEFORE_MISSION` / `WASTED_ROAM_MISSION_FORCE_S`. |
| T8 phone | fix-sonnet-d | Answer every ring (missions on or off), hang up after 25 s or at once on a fight/chase while missions are off; one attempt per ring; bridge tries control group 0 then 2 and logs which worked; `DESTROY_MOBILE_PHONE` if the handset is up with no ring/call for 10 s. **Bug fixed:** `answer_call`/`reject_call` were missing from `bridge_client.BRIDGE_TASK_TYPES` — the literal cause of "it cant cut the call". |
| T9 reflexes | fix-sonnet-d | `WaterEscalator` (in water > 10 s → out, walk toward `last_outdoor`), `RoadDodge` (on foot, vehicle closing → step off), `JackHandoffGate`; wired into the ladder flip → water → road_dodge → stalled/threat. |
| T10 safety net | fix-sonnet-e | `tests/support/fakebrain.py` (swaps only `BilledCall`; the retry/validator path is real), `replayer.py` (the real tick), `tools/record_state.py` (5 Hz `/state` recorder), `tools/funcheck.py` (F1–F6), `tools/soak.py` (12-minute synthetic run). `docs/FUNCHECK.md`. |
| Integration | Fable | Nine findings from the soak and the audit, all fixed with tests: a goal died as `stuck` after every phone task; `earn_two_stars` was passive; the wanted gate and the threat reflex both killed heat goals the moment they earned a star; the brain's `wait` looped him into standing still; `big_jump` refused a jump made from next to the ramp; `flee_ped` missing from the client's task table; two event-less lines; a wheel-tick bookkeeping gap on break start. `docs/findings.md` I1–I9. |
| T11 API key | **operator** | Both the box's and this machine's `ANTHROPIC_API_KEY` return 401. Nothing runs until the enabled key is in `C:\wasted\harness\.env`. |

## 2. F1–F6, before → after

"Before" is the last 2 h of live logs on 2026-09-03 (`docs/findings.md`, sessions 104653–114118).
"After" is `harness/tools/soak.py` — the fake brain through the real tick, over a synthetic world that
grants only what an action physically produces (`docs/FUNCHECK.md`). Three runs: 12 min seed 3,
12 min seed 7, 20 min seed 3.

| Check | Rule | Before (live, 2 h) | After (replay) |
|---|---|---|---|
| F1 idle | < 5 % of any window with control and no movement task | not measurable from the log; 111 `cleared_by_game`, 62 wheel preemptions, 3 "start the job" stalls | **3.9 % / 3.6 % / 3.9 %** |
| F2 something new | no gap > 60 s | 22 picks, every one a `goal_fallback`; goals timing out unseen | **46 s / 51 s / 51 s** (52 / 52 / 79 moments) |
| F3 commentary | no line without an event, no banned phrase, dedupe, no absent names | lines on every poll | **0 offenders** (11 / 9 / 10 lines) |
| F4 goals completed | ≥ 60 % | 3 completed of 22 (14 %) | **95 % / 95 % / 97 %** |
| F5 deaths | < 6/h, auto step-down | not counted | **5.0 / 5.0 / 3.0 per h** (the one scripted death) |
| F6 moves in 3 s | every control-regained edge | 0/7 (fix-opus-b's first run) | **2/2** (and 96/96 on fix-opus-a's 2 h synthetic stream) |

Tree: `pytest` **893 passed**, `ruff` clean, bridge **1.7.0** builds with 0 warnings, offline checks
187/187, `verify` audit: one movement owner per tick, goal box plugin-only, no live brain call
reachable from tests or tools.

## 3. NOT VERIFIED — the game has to say yes

Every item below is a native or a world response the soak *granted* and the real game may refuse.
None of them can be checked here; all of them are checked by the first 20 live minutes (§5).

- Drive-start: `IS_PED_IN_VEHICLE`/`GET_PED_IN_VEHICLE_SEAT` reading true on the frame entry
  completes (if the log shows `not_in_drivers_seat` right after a good entry, the fix is a settle
  window); `SET_VEHICLE_ENGINE_ON(instantly)`; `SET_DRIVE_TASK_CRUISE_SPEED` set on the issuing
  frame; whether **one** bridge-side re-issue survives a story call (the 111-clears root cause).
- `TASK_SMART_FLEE_PED`, `TASK_DRIVE_BY`, `TASK_SHOOT_AT_ENTITY`, `TASK_COMBAT_PED` on the **player**
  ped (the combat brief flags contradictory sources); `PedMoveBlendRatio.Sprint` on the player.
- The `ammunation` loadout actually arming him at session start and after death.
- Phone: which control group registers `PhoneSelect`/`PhoneCancel` (the bridge logs it);
  `DESTROY_MOBILE_PHONE`; whether a story call can be answered mid-drive without the goal's
  task being cleared for longer than the restart path expects.
- `IS_ENTITY_IN_AIR` on a real ramp; the wanted system answering a drive-by the way the soak
  assumes (1 star on the burst, 2 soon after); `flee_police` losing 2 stars in ~30–60 s.
- The 10 s / 1.5 m no-progress rule at a long red light; `veh.Speed > 0` at +2 s as the bar.
- F1 on a **real** `/state` stream — the fake brain cannot echo a task back into the next
  snapshot, so the replay's F1 is a floor, not the number.
- `RoadDodge`'s step-away heuristic and `WaterEscalator`'s `last_outdoor` bearing against real
  geometry.

## 4. Putting him back on air — exact steps

Only the operator can do 1 and 6. Everything else is one script.

1. **The key.** On the box, in `C:\wasted\harness\.env`, set `ANTHROPIC_API_KEY=` to the enabled
   key (the current one there answers 401). Do not copy this machine's `.env` over it — the box's
   file holds the Supabase service key too. Leave `WASTED_MISSIONS_ENABLED=false` (operator call:
   roam only until he is ready for jobs) and `WASTED_HOURLY_CAP_USD=2.00`.
2. **Build and package here** (dev machine):
   ```
   ~/.dotnet/dotnet build bridge/WastedBridge.csproj -c Release
   tar czf harness-today.tgz -C harness wasted_harness
   ```
   Upload `bridge/bin/Release/net48/WastedBridge.dll` and `harness-today.tgz` to `C:\wasted\tmp\`
   on the box with the same `scp` you used on 2026-09-03 (sshd has been fixed for it).
3. **Check the game is alive and ticking** — it must be running, unpaused, on the console session
   (`tscon`), with RDP closed; `Invoke-RestMethod http://127.0.0.1:7777/health` must show
   `tick_hz > 0`. If `tick_hz` is 0 the game is paused on focus loss — fix that first
   (`scripts/prepare-to-leave.ps1` / `leave-safely.ps1`), deploying now only adds a variable.
4. **Deploy:** on the box, elevated PowerShell, `scripts\deploy-all.ps1`. It stops the harness only,
   hot-reloads the bridge to 1.7.0 through SHVDN's `Insert` binding (no game restart), unpacks the
   harness, appends the missions-off line if it is missing, **gates** on an import probe
   (`GOALS … WHEEL ok` — it restores the previous build if the probe fails), starts one hidden,
   unbuffered harness, and prints the bridge version and the log tail. It never touches OBS, the
   game, or the stream.
5. **Prompt audit on the box:** `C:\wasted\harness\.venv\Scripts\python.exe -m wasted_harness.main
   --prompt-audit` must print `PROMPT AUDIT PASSED` — that is the first real brain call and the
   proof the key works (T11's acceptance).
6. **Start the stream** in OBS yourself (rule: never automated) and close the RDP session the way
   `docs/RUNBOOK.md` says.
7. **Record the first 20 minutes** so the numbers above become real:
   `python tools\record_state.py --out C:\wasted\logs\live-1.states.jsonl --hz 5` on the box for
   20 min, pull it, then `python harness/tools/funcheck.py <file>` here. That file is the first
   real fixture this program has.

## 5. The first ten minutes — what to expect

- 0–10 s: he is armed (`player.weapon` in `/state`, the loadout line in the bridge log), in or at a
  car, and **moving within 3 s** of control (`control regained: answered` in the log). If the log
  says `control regained and nothing moved him`, F6 failed live — stop and read the drive-start
  lines (`not_in_drivers_seat` / `drive_did_not_start` / `cleared_by_game`).
- First minute: a roam goal locks (`activity_start`) and the dashboard shows its description, never
  a model sentence. Expect L2 goals: a nice car, a drive-by, two stars then losing them, a fight.
- A phone ring: answered within a poll, hung up at 25 s, the goal **continues** (the log line
  `roam step machine was abandoned under a locked goal; restarting its plan` is the fix working,
  not a fault).
- Commentary only on events; the same line never twice; no name that is not on screen.
- Deaths: one or two in the first hour are fine; six in an hour steps the ladder down to L1
  (`CHAOS LEVEL DOWN` in the log and in commentary), 30 clean minutes step it back up.
- What would be wrong: `goal_fallback` on every pick (prompt/menu drift), `wait ignored` on every
  think (the brain has nothing to do — check the menu is non-empty), any `task refused` storm,
  `FATAL 401`.

## 6. Chaos level up / down

It moves itself: **down** after 6 roam deaths in a rolling hour, **up** after 30 minutes without a
death, between L1 (scenic, errands, stunts) and L3 (three-star survival, rampage, helicopter). To
pin the level by hand, set `WASTED_CHAOS_LEVEL=1|2|3` in the box's `.env` and restart the harness
(`deploy-all.ps1` step 5, or `Restart-ScheduledTask WASTED-Harness`); missions stay off until
`WASTED_MISSIONS_ENABLED=true`.
