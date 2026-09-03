# WANTED — CONTRACTS

**Version: 1.14 — FROZEN 2026-09-03.** Executors treat this file as read-only; changes go through
Fable (the orchestrator) and bump the version. Research backing every external-API claim:
docs/RESEARCH.md (decisions D1–D10) + raw sourced briefs in docs/research/.
Changelog:
- v1.14 (additive; bridge 1.6.0 → **1.7.0**). **WEAPONS AND TARGETED VIOLENCE.**
  **§1 `/state` gains three fields.** `player.weapon` = `{"name": "<WeaponHash member>", "class":
  "unarmed|melee|gun|projectile|unknown", "ammo": int, "owned": {"<member>": int}, "loadout":
  "off"|"ammunation"}`, always present. `owned` is `HAS_PED_GOT_WEAPON` over the three tracked
  loadout weapons only — a missing name means "not one of the three", never "unarmed".
  `vehicle.in_air` = `IS_ENTITY_IN_AIR` (via `Entity.IsInAir`). `vehicle.seat` =
  `"driver"|"passenger"|null` from `GET_PED_IN_VEHICLE_SEAT(veh,-1) == player`.
  **§1 gains three task types.** `shoot_at {handle, duration_s}` (`TASK_SHOOT_AT_ENTITY`, done on
  the clock); `drive_by {handle, duration_s}` (`TASK_DRIVE_BY`, raw — no SHVDN wrapper exists;
  signature from citizenfx natives docs, fetched 2026-09-03; in-vehicle only); `enter_vehicle_seat
  {handle, seat: 0|1|2}` (`TASK_ENTER_VEHICLE`, passenger seats only — the driver's seat stays
  `enter_nearest_vehicle`; fails with `seat_occupied` / `no_driver` / `took_the_wheel`). `fight_ped`
  gains optional `weapon: "auto"|"unarmed"|"armed"`, default `auto` = unchanged v1.11 behaviour.
  `attack_ped` was NOT added: `TASK_COMBAT_PED(player, target, 0, 16)` is byte-for-byte what
  `fight_ped`'s armed arm already issues; a second type with the same native would give the model
  two indistinguishable options.
  **§1 safety rule, amended.** The sentence *"…no money, no weapon-giving endpoint."* becomes:
  *"…no money, and **no endpoint through which the harness can request a weapon**. The bridge may
  maintain a fixed Ammu-Nation-equivalent loadout of its own — pistol, micro SMG, pump shotgun,
  modest ammo (60/90/24), **never infinite** — controlled by `WASTED_BRIDGE_LOADOUT` (`off` |
  `ammunation`), reported in `player.weapon.loadout`, re-applied on the death→alive edge, and no
  caller can vary it. Weapon **selection** is bridge-side and always `HAS_PED_GOT_WEAPON`-guarded."*
  **Operator decision, logged:** the default ships as `ammunation`. CLAUDE.md rule 5's test is
  "a human can't do that": a human with the agent's cash walks into Ammu-Nation and buys exactly these
  three. What a human cannot do — infinite ammo, perfect accuracy, a minigun from nowhere — stays
  refused (`docs/research/brief-combat-natives.json` FAIRNESS JUDGMENTS). Flip the env to `off`
  to go back to whatever he picks up.
  Every wrapper's underlying native hash was verified by reading the pinned SHVDN 3.7.0.189 DLL's
  IL with `System.Reflection.Metadata` (the table is in fix-opus-b's T6 report and in
  `bridge/src/WeaponState.cs`). NOT VERIFIED in-game: all of it — see `docs/findings.md` T6.
  **§1 also gains `flee_ped` `{handle}`** (fix-opus-a, T1): `TASK_SMART_FLEE_PED`, the on-foot
  counterpart of `fight_ped`, reusing the same frozen `handle` wire key — see the task table.
  **Two new failure details** (T1, the drive-start sequence): `"not_in_drivers_seat"` — a drive task
  was issued at a ped who is not in seat −1 (`IS_PED_IN_VEHICLE(ped, veh, false)` and
  `GET_PED_IN_VEHICLE_SEAT(veh, -1)` both checked; no task is issued); `"drive_did_not_start"` — the
  drive task was alive 2 s after one bridge-side re-issue and `vehicle.speed` was still 0. The engine
  is switched on (`SET_VEHICLE_ENGINE_ON`) before every drive task and the cruise speed set after it.
  A drive task the game clears is re-issued ONCE before `"cleared_by_game"` is reported, so the
  harness's `ClearedByGameBackoff` still sees the same detail string. `walk_to`'s `run: true` is now
  move-blend 3.0 (sprint) rather than 2.0 — a bridge-side tuning of a frozen flag, same class as the
  driving-style bit values. NOT VERIFIED in-game: all of it — see `docs/findings.md` T1.
- v1.13 (additive; bridge 1.5.0 → **1.6.0**). **THE PHONE.** `/state` gains
  `phone` = `{"ringing": bool, "in_call": bool}`, and §1 gains two task types, `answer_call` and
  `reject_call`, both `{}`.
  **Why:** the operator watched Simeon call the agent on stream. The harness had no phone capability
  at all — the call simply rang out, unseen and unanswerable — and answering a STORY call
  **starts a mission**. Missions are currently switched off (`Settings.missions_enabled`, env
  `WASTED_MISSIONS_ENABLED`), so without this the switch is a half-measure: it stops him walking
  into a start marker but cannot stop a phone call walking him into a job. "Give him a control to
  accept / reject a call" is the operator's own wording.
  **`ringing` is a sound-level PROXY, and that is stated here rather than hidden.** There is no
  native that reports "an incoming call is ringing": the game keeps it in a script global whose
  index is build-specific, and a hard-coded global index is exactly the kind of guess CLAUDE.md
  rule 6 forbids — it would break silently on the next game update. `CAN_PHONE_BE_SEEN_ON_SCREEN`
  is not the answer either; it is hard-coded to return 1 in this engine. So the bridge derives:

  ```
  ringing := IS_PED_RINGTONE_PLAYING(playerPed)  AND NOT  IS_MOBILE_PHONE_CALL_ONGOING()
  in_call := IS_MOBILE_PHONE_CALL_ONGOING()
  ```

  **The `AND NOT` is load-bearing, not tidiness.** `IS_PED_RINGTONE_PLAYING`
  (`0x1E8E5E20937E3137`, `BOOL(Ped)`) means "this ped's phone is making ringtone noise", which is
  *also* true while the player **dials out** and while a custom ringtone plays. On its own it
  would report "someone is calling you" during the agent's own outgoing call, and the reject reflex
  would hang up on him. `IS_MOBILE_PHONE_CALL_ONGOING` (`0x7497D2CE2C30D24C`, `BOOL()`) is what
  separates "making noise, nobody has picked up" from "a call is live". Both hashes verified
  present in the pinned SHVDN v3.7.0.189 `GTA.Native.Hash` enum by reflection over
  `bridge/lib/ScriptHookVDotNet3.dll`, values matching alloc8or's NativeDB. Neither has a typed
  SHVDN wrapper, so both are raw `Function.Call` — the same pattern `IS_PED_BEING_JACKED` /
  `GET_PEDS_JACKER` already use.
  **`answer_call` / `reject_call` go through the game's own CONTROL layer**, not a keypress, so
  they are independent of whatever the player has the phone bound to: `SET_CONTROL_VALUE_NEXT_FRAME`
  (`0xE8A25867FBA3B05E`, `BOOL(int control, int action, float value)`) with value `1.0`. Answer is
  `Control.PhoneSelect` (176), reject/hang-up is `Control.PhoneCancel` (177) — both read through a
  **compile-time** cast in the bridge, so an SHVDN bump that renames or removes either member
  breaks the BUILD rather than injecting the wrong control at runtime (the same reasoning
  `DrivingStyles` and v1.12's `player.interior` already apply). The input is injected **every tick
  until the state changes**, one control per frame, because it only registers once the handset has
  risen on screen (the game raises it by itself for an incoming call, so no `Control.Phone` press
  is needed). Both are **bounded at ~6 s** and then report `failed`: some story calls cannot be
  refused at all — the game hides the reject soft key and no native says which — so
  `"unrejectable"` / `"unanswered"` is the honest end state, not an infinite retry.
  **These two are `POST /task` like every other §1 verb** (so they preempt the running task, one
  task at a time, unchanged), but they issue **no engine ped-task** and move nobody: v1.10's
  task-liveness check therefore does not apply to them, and the harness deliberately exempts them
  from its own movement arbitration. No new endpoint, no new event type, no `/health` change.
- v1.12 (additive): `player.interior` = `{"id": <int>, "since_s": <float>}` or **null** when he is
  outdoors, and `player.last_outdoor` = `{x,y,z}` or null — the position captured on the last
  outdoor→indoor transition.
  **Why:** after a mission ends, a respawn, or a character switch INSIDE a safehouse, outdoor
  navigation tasks fail because the nav mesh is disconnected by doors, and the agent stands in a
  living room doing nothing. The harness has had a house-escape path for a while, but it was
  unreachable in the live loop: `/state` exposed no way to distinguish "he is indoors" from "he is
  merely stuck", so the escape could only be triggered by a heuristic (a failed
  `enter_nearest_vehicle` plus 20 s of stillness) that fires late and also fires on false
  positives. This is the ground truth instead of the guess.
  Source is SHVDN's **`Entity.CurrentInteriorProxy`** (verified present in the pinned
  nightly.189 `Docs/ScriptHookVDotNet3.xml`: "Gets the current [interior proxy] associated with
  this [entity] ... if they are in an interior; otherwise [null]") plus `InteriorProxy.Handle` for
  the id. A wrapper, not a raw native hash, so a future SHVDN bump that renames or removes it
  breaks the BUILD rather than silently returning a garbage id — the same reasoning
  `DrivingStyles` already applies.
  `since_s` is measured bridge-side from the tick the id last changed, because only the bridge
  sees every tick; a harness derived from a 2-4 Hz poll would round the transition. `last_outdoor`
  is likewise bridge-side: the escape's first move is "walk back to where you came in", and only
  the bridge sees the tick *before* the transition.
- v1.11 (behavioural, from three sourced research passes after the operator asked "isn't there a
  mod that makes him a pro": docs/research/brief-driving-natives.json, brief-combat-natives.json,
  brief-mission-comprehension.json). The answer was that the "mod" is Script Hook V + SHVDN, which
  we already run — the gap was native API we were not calling. Bridge 1.2.0 → **1.4.0**.
  **New `/state` fields (all additive):**
  1. `mission.entity_blips[]` = `[{pos:{x,y,z}, handle, color, is_route: bool, distance: float,
     name: string|null}]`, nearest first, at most 8 — every blip PINNED TO AN ENTITY (a ped or a
     vehicle), **whether or not the game has plotted a route to it**. `route_blips[]` only ever
     carried route-enabled blips, and a crew "blue dot" usually has none, so when the crewmate
     being followed drove beyond the ~50 m `nearby.peds` radius he vanished from `/state`
     completely and the agent searched for him by driving at random — while the game was drawing his
     position on the minimap the whole time. `name` is the blip's own map-legend text
     (`Blip.GetAppropriateName()`): "Lamar", "Objective". Entity attachment is a FACT, not a colour
     convention — the same reasoning `objective_blip` already applies to routes.
  2. `nearby.peds[].attacking_me: bool` and `.weapon_class: "unarmed"|"melee"|"gun"|"projectile"|"unknown"`.
     `attacking_me` is true when the ped's melee target IS the player (valid **while the swing
     animation plays**, i.e. before impact), when the ped is in combat against the player (true the
     frame the game tasks them, before the first punch), or when the player has been damaged by
     that ped. Damage attribution deliberately ignores vehicle damage, so a ped whose car clipped
     us is not called an attacker.
  3. `threat` = `{attacker_handle: int|null, being_jacked_by: int|null}` — the nearest ped currently
     attacking, and whoever is pulling him out of a car.
  4. `player.switch_in_progress: bool` — a protagonist switch is playing. This is the real signal
     behind the agent narrating "wrong body / waiting for the switch": the game was mid-switch and he
     had no field that said so.
  5. `mission.retry_in_flight: bool` — the game's own `mission_repeat_controller` script thread is
     running, i.e. a retry / checkpoint reload is in progress. Read from the same thread walk that
     already produces `mission.script`.
  **New §1 task type — `fight_ped` `{handle}`:** fight one NAMED ped. The bridge picks the
  mechanism from the target's weapon class: unarmed/melee → the engine's direct-melee task, which
  is CONFIRMED to work on the player ped (Rockstar's own `player_scene_t_bbfight` calls it that
  way); armed → the combat task. `failed`/`"target_lost"` when the handle does not resolve; `done`
  when the target is dead or gone. **Why a new verb was needed:** `combat_hated_targets_around`
  requires at least one ped whose relationship toward the player is Neutral/Dislike/Hate or the
  engine task EXITS IMMEDIATELY, and a civilian whose car the agent just stole is plausibly still
  Respect/Like — so the only retaliation action in the vocabulary had been a silent no-op, which is
  why he stood still and was beaten to death on stream. `fight_ped` is target-explicit and needs no
  relationship setup. `combat_hated_targets_around` is unchanged and still available.
  **Behavioural changes with no wire change:** the `avoid_traffic` driving style was the game's
  RECKLESS preset (documented as "doesn't use the brakes at ALL to help with steering") and is
  retuned — §1 already states the style NAMES are the contract and the bridge may tune the bit
  values; an in-vehicle `follow_entity` now uses the engine's mission-follow task with a
  straight-line-to-target distance, which is what stops him losing a target at junctions; a wedged
  car is recovered with reverse / reverse-and-turn nudges (never a teleport); and while he is in a
  vehicle the engine is told not to let him get out to fight, because leaving a working car to
  fistfight the owner is how he died.
  **Declared assist (not a cheat, recorded here on purpose):** the bridge sets the engine's own
  driver-ability and aggressiveness for the player ped (0.8 / 0.5–0.8). Both are engine-clamped to
  1.0 and change AI COMPETENCE, not vehicle physics — no extra grip, torque or invulnerability.
  **Refused as cheats** and absent from the code: perfect-accuracy and shoot-through-walls combat
  attributes, accuracy/shoot-rate above human range, giving weapons or ammo, the teleport-out
  vehicle-exit flag, wanted-level clearing, police-ignore, and self-righting a flipped car.
- v1.10 (root-caused from the 2026-09-02 live follow-mission failure — "drives and then stops").
  Four changes, three additive and one a semantic DEFECT FIX:
  1. **`objective_blip.handle` / `route_blips[].handle` now carry the ENTITY handle when
     `kind == "entity"`** (the ped/vehicle the blip is attached to, resolved via the game's own
     blip→entity lookup). Until now the bridge emitted the BLIP's handle — a different handle
     space — so `follow_entity` on "the game's own route to Lamar's car" resolved no entity and
     failed `target_lost` on arrival; the follower's re-post latch then held, nothing was driving,
     and the car coasted to a stop. That was never usable and no consumer can have depended on it,
     which is why this is a fix rather than a break. When the blip is an entity blip but the
     entity cannot be resolved, the bridge emits `kind: "coord"` (the position is still honest).
  2. **`nearby.peds[]` gains `in_vehicle_handle: int|null`** (null = on foot). Research
     (docs/research: SHVDN `World.GetNearbyPeds` is a raw CPed-pool scan with only radius+model
     filters — seated peds are NOT excluded) killed the first theory that occupants vanish from
     the scan; what actually happens is the crewmate's car pulls beyond the scan radius within
     seconds, and the long-range source (the route blip on their car) was unusable because of the
     handle defect fixed in item 1. `in_vehicle_handle` exists so the harness can do the
     ped→vehicle hand-off the instant the crewmate mounts up (follow the CAR, whose blip and
     handle outlive the ped's presence in `nearby`), instead of only discovering the loss later.
     Same 8-cap, same distance sort, same filters as v1.5/v1.6.
  3. **`mission.script: string|null`** — the active story-mission script name (e.g.
     `"armenian1"`), read from the game's own running script threads and filtered against the
     pinned allowlist in docs/research/brief-mission-scripts.json, emitted only while
     `mission.active`; null when unknown. Script→TITLE pairings are deliberately NOT shipped
     (research found no verifiable table): the harness treats the name as a stable identity key
     and learns script↔title pairs from the real game (OCR'd title card observed together with
     `mission.script`), persisted in harness state.
  4. **Task liveness:** when the game itself clears a task the bridge issued (mission scripted
     beats and cutscenes do this), `last_task` reports `failed` with detail `"cleared_by_game"`
     instead of `running` forever. Consumers already handle `failed` + re-plan; `running` must
     mean the engine is actually still executing the task.
  Harness types widen first (optional, defaulted), then the bridge emits — the v1.5/v1.6
  sequencing. No new task type; the §1 verb set is unchanged.
- v1.9 (behavioural, root-caused from a live stream failure): `follow_entity` gains `style` and
  `speed_mps`. Until now `StartFollowEntity` hard-coded `DrivingStyles.Normal` and a 15 m/s cruise
  cap (`TaskEngine.cs`), so a tail obeyed red lights and topped out at 54 km/h while the NPC being
  tailed did neither. Following a mission car was therefore **not achievable** — the target simply
  drove away, the mission failed, and no amount of prompting could fix it, because the losing
  behaviour was in the bridge, not the decision. Observed live as "Franklin lost Lamar" on a follow
  mission whose only objective is to stay with Lamar. New defaults **when the caller omits them**:
  style `ignore_lights`, `speed_mps` 30.0 (108 km/h). Both are chosen to match, not exceed, what a
  mission NPC does; `avoid_traffic` remains available for a genuine chase, and a caller may still
  pass `normal` for a leisurely tail. The on-foot branch is unchanged (it already runs). Rationale
  for touching the default rather than only the parameter: every existing caller is the harness's
  own follow logic, which wants to keep up in all cases, and a default that loses the target is a
  bug rather than a preference. No new task type, no new event, no `/state` change.
  **Asymmetry, on purpose:** the bridge ACCEPTS `style` on `follow_entity`, but the brain's decision
  schema does NOT expose it (`ACTION_PARAM_KEYS["follow_entity"] = (handle, in_vehicle, speed_mps)`).
  `style` is one of the three non-nullable wire keys from v1.4, so listing it would put
  `style: "normal"` on *every* follow the model ever emits and re-create the bug. Left off, the
  bridge default applies. `speed_mps` is nullable, so it is absent unless a caller means it, and
  that is the one lever the brain and `MissionFollower` get for a widening gap.
- v1.8 (additive; **retroactive — the bridge has emitted this since 2026-09-02, the contract
  entry was missed**): `mission.route_blips[]` = `[{ "pos": {x,y,z}, "kind": "coord"|"entity",
  "handle": int, "color": string }]` — the blips the game has actually plotted a GPS route to,
  nearest first, at most 5 (`MaxRouteBlips`). `objective_blip` is the bridge's single best pick out
  of the same set; the list exists so that the harness can reason when there is more than one (a
  follow target plus a drop-off), and so a wrong pick is recoverable instead of invisible. `color`
  is the SHVDN `BlipColor` member name, and an index with no name in the pinned enum serializes as
  its integer rendered as a string — consumers must tolerate that. `kind: "entity"` means the route
  points at a moving thing (`handle` is valid and can be passed to `follow_entity`); `kind: "coord"`
  means a fixed place. The key is always present as an array, never null.
- v1.7 (additive): `player.protagonist` = `michael|franklin|trevor|unknown` (from the player ped
  model), and `mission.starts[]` = `[{ "pos": {x,y,z}, "protagonist": "michael|franklin|trevor|unknown" }]`
  — the mission-start markers currently on the map (the M/F/T letter blips, identified by the
  game's own per-protagonist blip colours; nearest first, at most 8). Walking or driving into one
  starts that mission. Purpose: the agent can now DECIDE to go and do a job instead of only reacting
  once a mission is already running. Harness types widened (optional, defaulted) before the
  bridge emits them.
- v1.6 (additive): `nearby.vehicles[]` and `nearby.peds[]` gain `pos: {x,y,z}` (same shape as
  `player.pos`). Until now they carried only `distance` + `handle`, so the agent knew a crewmate or an
  enemy was "20 m away" but not in which direction — the minimap's red/blue dots were invisible to
  him as data. With `pos` he can `walk_to`/`drive_to` a crewmate, reason about where enemies are,
  and use `nearby` positions as a legitimate coordinate source. Harness types were widened
  (optional field) before the bridge began emitting it.
- v1.5 (additive): `nearby.peds[].relationship` gains `friendly`. Until now every non-hostile ped
  was `neutral`, so a mission crewmate standing beside the agent was indistinguishable from a bystander
  and "stay with the crew" / `follow_entity` on a crewmate could not be expressed. The harness
  type was widened before the bridge began emitting the value.
- v1.4 (root-caused by a 25-agent adversarial pass over 174 real decisions + server logs):
  §2 `action.params` is now a **closed, typed object** — every key §1's task table and §2's primitive
  list already name (`x y z speed_mps style arrive_radius_m run prefer search_radius_m radius_m
  duration_s handle in_vehicle seconds station ms direction`). On the wire this is a
  strict subset of what v1.3 permitted and adds no task type, event type or decision field. Why it
  had to change: the harness sends the decision schema through the Anthropic SDK's strict
  `transform_schema`, which rewrites a free-form object into `properties: {} +
  additionalProperties: false` — a grammar whose only legal value is `{}`. Constrained decoding
  therefore forced `params: {}` on **every one of 174 real decisions**, so every `drive_to` /
  `walk_to` / `follow_entity` was dead on arrival and the parameterless `enter_nearest_vehicle` /
  `exit_vehicle` pair made up 63% of all actions — the observed get-in-car / get-out / get-in-car
  loop. A typed model is the only fix a decoding grammar respects; prose in the catalog cannot.
  Wire-shape constraints, MEASURED against the real API 2026-09-02 (forced, not chosen): every
  param key is `required` on the wire and the model writes an explicit value or `null` per key,
  because the constrained-decoding grammar compiles in time exponential in the number of OPTIONAL
  properties (n>=15 optional => the server drops the connection at 60 s; all-required => ~3 s cold /
  ~1.3 s warm). The endpoint also rejects >16 union-typed params, so `style`/`run`/`direction` are
  non-nullable with their contract defaults (`normal`/`false`/`left`), holding the union count at 14.
  `wire_params()` strips keys an action does not take (an `ACTION_PARAM_KEYS` allowlist = the §1
  table + §2 primitives), so `decisions.action` / `POST /task` keep exactly the documented shape and
  no default leaks onto an action with no such param. Cost: +~85 output tokens/decision (~+$0.0004);
  the params schema rides inside the cached prefix. `follow_entity.in_vehicle`, when the model omits
  it, is absent from the POST body and the bridge defaults it to `false`.
  Companion (non-contract) findings fixed in the same pass: the harness left `world.timescale` at
  0.15 after a failed restore (the observed "slow motion"); a synchronous screen-grab retry loop
  blocked the tick for up to 172 s; a local schema failure was charged to the API-outage backoff.
- v1.3 (from the first live play session on the real game): `mission_start` now carries a
  **screenshot**. Rationale: /state exposes no mission name and no objective text — verified, the
  field does not exist anywhere in the pipeline — so the ONLY honest source of "what does this
  mission actually want" is the screen the game draws it on. Previously the director could see the
  screen on `death`, `busted`, `mission_fail` and `wanted_change` — i.e. only after things had gone
  wrong, never at the moment the objective is displayed. Observed live: the agent stood in a mission
  with no idea what it wanted. Cost is bounded: missions start rarely, so this adds roughly one
  vision call per mission rather than per tick.
- v1.1 adds the `site_config` table (§5) so stream provider/channel are runtime-switchable
  (D8 — `NEXT_PUBLIC_*` values are baked per-deployment and cannot switch at runtime).
- v1.2 (from the delivery-day readiness pass, all three found by cross-package verification):
  `last_task.id`/`type` are **nullable** before the first task (this exact mismatch would have
  crashed the harness on its first poll); `/health.edition` may be `unknown` before edition
  detection completes; and the bridge **error-code set is enumerated** above and closed per
  version, with consumers required to tolerate unknown codes.

Platform baseline (D1): GTA V **Legacy** edition (Steam app 271590, granted by an Enhanced
purchase), Script Hook V (current: 1.0.3889.0/1.0.1158.13 build), **official SHVDN nightly
≥ v3.7.0-nightly.189** pinned at Phase 1 start, scripts target .NET Framework 4.8, launched with
`-nobattleye`. The pinned SHVDN version is recorded in STATUS.md and bridge/README.

Conventions used everywhere:
- Timestamps: ISO 8601 UTC (`ts`).
- Coordinates: world-space meters, floats `{x, y, z}`. Heading: degrees 0–360. Speed: m/s.
- JSON over HTTP, UTF-8. Errors: HTTP status + `{"error": "<snake_code>", "detail": "human text"}`.
- **v1.2 — the error-code set** (closed per contract version; adding one bumps the version):
  `online_session_active` (503, every endpoint, online-session latch) · `not_ready` (503, /state
  before the first tick has published a snapshot) · `game_thread_stalled` (503, a queued command
  was not applied because the game thread is not ticking) · `queue_full` (503) ·
  `unknown_task_type` (400) · `invalid_params` (400) · `invalid_json` (400) ·
  `not_in_vehicle` (409, vehicle-only task while on foot) ·
  `unstick_conditions_not_met` (409). Consumers must handle unknown codes gracefully
  (log + treat as a transient failure) rather than crashing.

---

## 1. Bridge HTTP API v1 — `http://127.0.0.1:7777`

Served by the SHVDN script. HTTP thread only reads a cached snapshot and enqueues commands; the
game thread applies commands and refreshes the snapshot each tick. **Natives are never called from
the HTTP thread.**

**Safety rule (every endpoint):** if a network/online session is detected at startup or on any
tick, the script disables itself and every endpoint returns
`503 {"error": "online_session_active"}`. There is no teleport endpoint, no god-mode, no money, no
weapon-giving endpoint. `unstick` (≤3 m) is the only positional nudge and self-enforces its
preconditions.

### GET /state → 200

```json
{
  "ts": "2026-08-25T21:14:03.221Z",
  "tick": 123456,
  "player": {
    "pos": {"x": 0.0, "y": 0.0, "z": 0.0},
    "heading": 0.0,
    "health": 200, "max_health": 200, "armor": 0,
    "wanted": 0, "cash": 0,
    "dead": false, "arrested": false,
    "in_vehicle": false,
    "control_enabled": true
  },
  "vehicle": null,
  "location": {"street": "Vinewood Blvd", "zone": "Downtown Vinewood"},
  "world": {"clock": "13:45", "weather": "CLEAR", "timescale": 1.0},
  "mission": {"active": false, "random_event_active": false, "cutscene_active": false,
              "objective_blip": null, "starts": [], "route_blips": []},
  "nearby": {"vehicles": [], "peds": []},
  "last_task": {"id": "t-000123", "type": "drive_to", "status": "running", "detail": ""},
  "bridge": {"version": "1.0.0", "edition": "legacy"}
}
```

Field notes:
- `vehicle` (when in one): `{"handle": 1234, "model": "adder", "display_name": "Adder",
  "class": "Super", "speed": 41.2, "health": 980.0, "upside_down": false, "in_water": false,
  "stopped_for_s": 0.0}`.
- `mission.active` ⇔ `GET_MISSION_FLAG`; `mission.random_event_active` ⇔ `GET_RANDOM_EVENT_FLAG`
  (Strangers & Freaks style content sets the latter, not the former — RESEARCH.md §2).
- `mission.objective_blip` (when identifiable): `{"pos": {x,y,z}, "kind": "coord|entity",
  "handle": <entity handle when kind=="entity", else the blip's own handle — see v1.10>}`.
  Identification rule (since v1.8): the bridge picks its best candidate from the ROUTED blip set
  (preferring yellow), falling back to the legacy sprite Standard(1) + colour Yellow(66) + route
  convention only when nothing is routed; details in bridge/README without changing this field's
  shape.
- `mission.script` (v1.10): the active story-mission script name (e.g. `"armenian1"`) from the
  pinned table in docs/RESEARCH.md, or null. Only ever non-null while `mission.active`.
- `mission.route_blips[]` (v1.8, nearest first, at most 5; handle semantics fixed in v1.10):
  `{"pos": {x,y,z},
  "kind": "coord|entity", "handle": <entity handle>, "color": "<BlipColor member name>"}` — every
  blip the game has plotted a GPS route to, i.e. the yellow line on the minimap made readable as
  data. `objective_blip` is the bridge's single best pick out of this same set. Use the list when
  there is more than one route (a follow target plus a drop-off) or when the pick looks wrong.
  `kind: "entity"` means the route points at something that moves and `handle` may be passed
  straight to `follow_entity`. `color` is a member name such as `"Yellow"`, but an enum index with
  no name in the pinned SHVDN build serializes as its integer as a string — tolerate that.
- `nearby.vehicles[]` (top 8 by distance): `{"handle": 5678, "model": "...", "display_name": "...",
  "class": "...", "distance": 12.3, "driver": "player|npc|empty"}`.
- `nearby.peds[]` (top 8): `{"handle": 9012, "model": "...", "distance": 5.2,
  "relationship": "neutral|hostile|friendly", "pos": {x,y,z}, "in_vehicle_handle": null}`.
  `friendly` (v1.5) = the engine's
  own Companion/Like/Respect relationship towards the player, i.e. mission crewmates. `pos` (v1.6)
  = world position, same shape as `player.pos`; vehicles carry it too. `in_vehicle_handle`
  (v1.10) = the vehicle the ped is currently seated in, or null on foot.
- `mission.entity_blips[]` (v1.11, nearest first, at most 8): `{"pos": {x,y,z}, "handle": <entity>,
  "color": "<BlipColor member name>", "is_route": bool, "distance": float, "name": "<map-legend
  text>"|null}` — blips pinned to a ped/vehicle REGARDLESS of whether a route is drawn. This is the
  long-range source that outlives `nearby.peds`' ~50 m radius: when the crewmate drives off, his
  dot (and his name) are still here. `name` comes from the game's own map legend.
- `mission.retry_in_flight` (v1.11): the game's `mission_repeat_controller` thread is running — a
  retry/checkpoint reload is in progress. Suppress tasks and commentary while true.
- `threat` (v1.11): `{"attacker_handle": int|null, "being_jacked_by": int|null}`. `attacker_handle`
  is the nearest ped with `attacking_me` true — pass it straight to `fight_ped`.
- `phone` (v1.13): `{"ringing": bool, "in_call": bool}` — always a present object.
  `ringing` = the phone is making incoming-call noise and nobody has picked up
  (`IS_PED_RINGTONE_PLAYING(player)` **and not** `IS_MOBILE_PHONE_CALL_ONGOING()`; the AND is what
  keeps an OUTGOING dial from reading as an incoming call — see the v1.13 changelog).
  `in_call` = a call is connected, incoming or outgoing. This is a sound-level PROXY: it cannot
  tell a story call (answering one starts a mission) from a friend's hang-out invite, and it
  cannot see the on-screen soft keys, so it cannot know in advance whether a call is rejectable.
  On a failed native read the bridge serves both false — "let it ring out" is the only safe
  degradation — and logs it, throttled.
- `player.switch_in_progress` (v1.11): a protagonist switch is playing. Treat exactly like a
  cutscene: act on nothing, say nothing about "being the wrong character".
- `last_task.status` lifecycle: `idle` (no task ever / cleared) → `running` → `done` | `failed`.
  **v1.2:** when no task has ever been posted (fresh bridge load / script reload), `id` and `type`
  are `null`; every consumer must treat them as nullable. `status` and `detail` are always present.
  `detail` carries failure reason (`"preempted"`, `"timeout"`, `"target_lost"`,
  `"cleared_by_game"` (v1.10 — the game's own scripts wiped the task; re-plan, don't wait), ...).
- Entity `handle`s are the game's entity handles; valid only while the entity exists. The
  harness must treat them as ephemeral and re-read them from `/state` before use.

### POST /task → 202 `{"task_id": "t-000124"}`

One task at a time. Posting a new task preempts the running one (old task → `failed`,
`detail: "preempted"`). Body: `{"type": "<type>", "params": {...}}`. Unknown type → 400.
Tasks map to the game's own ped-task natives (the engine's pathfinding/driving — same system NPCs
use; verified signatures in docs/research/brief-natives.json). `style` maps to driving-style flag
bitfields (D3): `normal=786603`, `rushed=1074528293`, `ignore_lights=786475`,
`avoid_traffic=786468`. The *names* are the contract; the bridge may tune the underlying bit
values empirically (Phase 3) without a contract change.

| type | params | done when / notes |
|---|---|---|
| `drive_to` | `{x,y,z, speed_mps, style, arrive_radius_m: 8.0}` | player's vehicle within radius; `failed` if not in a vehicle |
| `walk_to` | `{x,y,z, run: false}` | within 2 m |
| `enter_nearest_vehicle` | `{prefer: "nicer"\|"any", search_radius_m: 30}` | `in_vehicle: true`; "nicer" = higher vehicle class rank than current/last, bridge-side heuristic |
| `exit_vehicle` | `{}` | on foot |
| `wander_drive` | `{style}` | never completes; runs until preempted |
| `flee_police` | `{}` | runs while wanted > 0; `done` when wanted = 0 |
| `combat_hated_targets_around` | `{radius_m}` | engine combat task; `done` when no hated targets remain in radius |
| `seek_cover` | `{duration_s: 10}` | cover reached or timeout |
| `follow_entity` | `{handle, in_vehicle: bool, style: "ignore_lights", speed_mps: 30.0}` | runs until preempted or entity gone (`failed`, `"target_lost"`). `style`/`speed_mps` apply to the in-vehicle tail only; on foot he always runs. Defaults keep pace with a mission NPC — see v1.9 |
| `fight_ped` | `{handle}` | v1.11. Fight ONE named ped; the bridge picks melee vs combat from the target's `weapon_class`. `failed`/`"target_lost"` if the handle does not resolve; `done` when the target is dead or gone. Needs no relationship setup, unlike `combat_hated_targets_around` |
| `flee_ped` | `{handle}` | v1.14. Run from ONE named ped (`TASK_SMART_FLEE_PED`), the on-foot counterpart of `fight_ped`, same frozen `handle` key. `done` when the player is 200 m clear or the target is dead/gone; `failed`/`"target_lost"` on a handle that does not resolve; `failed`/`"timeout"` after 120 s. Distinct from `flee_police`, which is coord-based off the last police-spotted position |
| `set_waypoint` | `{x, y}` | immediate (`done` same tick); map waypoint only, no movement |
| `stop` | `{}` | clears current task → `idle` |
| `answer_call` | `{}` | v1.13. Answers the RINGING call by injecting `Control.PhoneSelect` every tick until `phone.in_call` is true. `done` when connected; `failed`/`"not_ringing"` immediately if nothing is ringing; `failed`/`"unanswered"` after ~6 s. **Answering a story call starts a mission.** |
| `reject_call` | `{}` | v1.13. Refuses (or hangs up) the call by injecting `Control.PhoneCancel` every tick until `phone.ringing` and `phone.in_call` are both false. `done` when the line is clear; `failed`/`"unrejectable"` after ~6 s — some story calls hide the reject soft key and cannot be refused |

- `style` enum everywhere: `normal | rushed | ignore_lights | avoid_traffic`.
- `answer_call`/`reject_call` (v1.13) are `POST /task` like everything else — so they preempt the
  running task, one task at a time — but they issue **no engine ped-task** and move nobody, so
  v1.10's task-liveness (`cleared_by_game`) does not apply to them.

### POST /timescale → 200 `{"value": 0.15}`
Body `{"value": 0.1–1.0}` (clamped). Used while the brain thinks on a decision that matters;
harness must restore to 1.0.

### POST /control → 200 `{"enabled": true}`
Body `{"enabled": bool}`. Toggles player control (engine tasks on the player ped can be interrupted
by input; observed behavior documented in bridge/README during Phase 1).

### POST /radio → 200 — body `{"station": "<station name>" | "off"}`
### POST /horn → 200 — body `{"ms": 1–3000}`
### POST /unstick → 200 `{"moved": true, "distance_m": 2.4}`
Preconditions enforced bridge-side: speed ≈ 0 for > 20 s **and** a drive task `running`; otherwise
`409 {"error": "unstick_conditions_not_met"}`. Nudge ≤ 3 m. Always logged; harness announces it in
commentary.

### GET /health → 200
`{"version": "1.0.0", "edition": "legacy|enhanced|unknown", "tick_hz": 60.0, "queue_depth": 0,
"game_fps": 59.8, "online_blocked": false}` — the watchdog's liveness probe. Connection refused ⇒
game/bridge down; `503` ⇒ online session detected.

---

## 2. Decision schema (structured output via tool use)

One model call produces the decision **and** the commentary — never a separate commentary call.
This object is the whole public feed.

```json
{
  "thought": "≤40 words. The agent's private-ish reasoning, shown on site as 'what he was thinking'.",
  "say": "≤20 words. The agent's out-loud line in his voice. This is the commentary.",
  "mood": "chill | bored | hyped | scared | smug",
  "action": { "type": "<one of the bridge tasks or a manual-control primitive>",
              "params": { "<v1.4: only the keys §1/§2 name for THIS type; see the v1.4 note on the closed 17-key wire object>": "..." } },
  "goal": "current goal in ≤12 words, unchanged unless the director changed it",
  "confidence": 0.0
}
```

- **Paste-corruption note:** the master brief's mood enum arrived damaged
  (`"chill | bored | hyped |  smug"`); `scared` is restored from the brief's own driving-style spec
  ("scared avoids traffic"). If the original enum differs, fix here before Phase 2.
- `action.type` catalog = the 11 bridge task types (§1) **plus** harness-side primitives executed
  via SendInput keyboard/mouse (primary; a virtual gamepad is an optional enhancement — ViGEmBus
  is archived with known Windows Server failures, RESEARCH.md D9), v1 set:
  `look_around` (camera stick sweep), `brake_tap`, `swerve`, `reverse_out`,
  `press_prompt_key` (context prompt, e.g. E), `wait {seconds}`, `radio {station}`, `horn {ms}`.
  Adding a primitive bumps the contract version.
- `confidence` ∈ [0,1]. Enforced via strict JSON-schema tool use; a response failing validation is
  retried once, then the reflex layer keeps control and the failure is logged.

## 3. Brain tiers and pricing (D4; source: https://platform.claude.com/docs/en/about-claude/pricing, fetched 2026-08-25; re-verified at harness startup with a 1-token call per model)

| Layer | Model ID | Input $/MTok | Output $/MTok | Notes |
|---|---|---|---|---|
| tactical | `claude-haiku-4-5-20251001` (pinned snapshot) | 1.00 | 5.00 | text-only; thinking OFF (forced tool_choice + manual thinking errors); **min cacheable prefix 4096 tokens** — static prefix must exceed this or caching silently fails |
| director | `claude-sonnet-5` (dateless ID is itself pinned) | 2.00 | 10.00 | the $2/$10 intro price is now permanent (live docs beat older cached tables); text + occasional image; min cacheable prefix 1024 tokens |

- Prompt caching: reads 0.1× input price; writes 1.25× (5-min TTL — correct here, the 8–25 s
  cadence keeps it warm and each read refreshes TTL free). Never the 2× 1-h TTL.
- Decisions come back via structured output (`output_config.format: json_schema` /
  `messages.parse`) or strict tool use; **`tool_choice` stays constant across calls** (changing it
  invalidates cached message blocks).
- Screenshots (D5): 768-px-long-edge JPEG ≈ 448 visual tokens (`ceil(w/28)×ceil(h/28)`); raw
  1080p is never sent.
- `harness/config/pricing.yaml` is the runtime source of truth and must cite the official pricing
  URL + retrieval date. Budget accounting uses per-model `usage` token counts (Sonnet 5 tokenizes
  ~30% more tokens than Haiku for the same text — never estimate cross-model).

## 4. Event types (`events.type` + payload shape)

| type | payload | screenshot |
|---|---|---|
| `death` | `{cause: "?", street, deaths_total}` | yes |
| `busted` | `{wanted_at_arrest, street, busted_total}` | yes |
| `mission_start` | `{name}` | yes |
| `mission_end` | `{name, outcome: "passed", duration_s, deaths, attempts}` | yes |
| `mission_fail` | `{name, reason_text, attempt}` | yes |
| `wanted_change` | `{from, to}` | only if to ≥ 3 |
| `stunt` | `{kind: "jump\|big_air", airtime_s}` | yes |
| `clip` | `{clip_id, event_type}` | no |
| `break` | `{phase: "start"\|"end", planned_s}` | no |
| `governor_level` | `{from, to, reason: "hourly_cap\|reset\|manual"}` | no |
| `bridge_down` / `bridge_up` | `{consecutive_failures}` / `{downtime_s}` | no |
| `unstick` | `{distance_m, stuck_for_s}` | no |
| `activity_start` / `activity_end` | `{activity, params}` / `{activity, outcome, duration_s}` | no |
| `session_start` / `session_end` | `{harness_version, game_edition}` / `{reason}` | no |

The enum is closed per contract version; adding a type bumps the version.

## 5. Supabase data model

Write path: harness only, service-role/secret key, batched inserts with an on-disk offline queue.
Read path: browser, anon/publishable key, RLS public SELECT only (`for select to anon,
authenticated using (true)`; no write policies ⇒ writes denied). Realtime (D6): `postgres_changes`
publication on `decisions`, `events`, `stats` for v1; the web feed renders from **rows** (not
socket messages) so a later migration to coalesced Broadcast-from-Database digests (Realtime bills
per recipient — see RESEARCH.md §5 cost trap) needs no UI contract change. Storage uploads always
go to new timestamped paths with explicit content-type. Exact SQL lands in
`infra/supabase/schema.sql` (Phase 0a-wait work).

```
sessions   id uuid pk, started_at timestamptz, ended_at timestamptz?, game_edition text,
           harness_version text
decisions  id bigint pk, session_id fk, ts, layer 'tactical'|'director', thought text, say text,
           mood text, goal text, action jsonb, confidence real, model text, input_tokens int,
           output_tokens int, cached_tokens int, cost_usd numeric(12,6)
events     id bigint pk, session_id fk, ts, type text (§4 enum), payload jsonb,
           screenshot_url text?
missions   id bigint pk, session_id fk, name text, started_at, ended_at?, outcome
           'passed'|'failed'|'skipped'?, attempts int, deaths int, tokens bigint, summary text
clips      id bigint pk, session_id fk, ts, event_id fk events, storage_path text,
           duration_s real, caption text
stats      session_id uuid pk (one row per session, upserted): deaths int, busted int,
           missions_passed int, hours_alive real, cost_today_usd numeric, cost_per_hour_usd
           numeric, governor_level smallint, heartbeat_at timestamptz, current_goal text,
           hud jsonb
site_config key text pk, value jsonb, updated_at timestamptz — public read; known keys (v1.1):
           "stream" → {"provider": "twitch"|"youtube", "channel": str, "video_id": str|null}.
           Written only by operators (service role); the web app reads it at request time with an
           env-var fallback when the row is absent.
```

- Indexes: `(session_id, ts)` on `decisions` and `events`.
- `stats.heartbeat_at` drives the site's "the agent is offline" state (stale > 60 s ⇒ offline); the
  site never guesses from feed silence.
- `stats.hud` shape (frozen): `{"health": int, "armor": int, "wanted": int, "cash": int,
  "vehicle": str|null, "street": str, "zone": str, "clock": "HH:MM", "weather": str}`.
- Storage buckets: `clips`, `shots` — public read, service-role write. `shots` retention 7 days.

## 6. Overlay (harness-local, for OBS browser source) — `http://127.0.0.1:7788`

- `GET /overlay` — transparent-background page OBS embeds.
- `GET /overlay/stream` — SSE; event types:
  `banner {"kind": "wasted"|"busted"}`, `counters {"deaths": n, "busted": n,
  "missions_passed": n}`, `say {"text": "...", "mood": "..."}`,
  `governor {"level": 0-3, "note": "..."}`.
No external assets; must render with the game offline (blank counters, no banner).

## 7. Budget governor levels

| Level | Behavior (all announced via `governor_level` event + feed line) |
|---|---|
| L0 | normal cadence |
| L1 | slower tactical timers, no flavor shots |
| L2 | director-only; reflex layer drives (native wander/drive with mood-based style) |
| L3 | "asleep in the car": parks somewhere scenic, commentary paused with an honest on-screen note, resumes when the hourly window resets |

Target: < ~$1.50 per streamed hour at L0; measured number reported in STATUS.md every live check.

## 8. Directory ownership (per CLAUDE.md §8)

`bridge/` bridge executors · `harness/` harness executors · `web/` web executors ·
`infra/` infra executor · `scripts/` + `docs/` Fable only. Cross-directory needs are reported back,
never edited directly.
