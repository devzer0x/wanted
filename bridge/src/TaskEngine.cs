using GTA;
using GTA.Math;
using GTA.Native;

namespace WastedBridge
{
    /// <summary>
    /// Game-thread-only task state machine implementing the CONTRACTS.md §1 task types (plus the
    /// proposed flee_ped - see StartFleePed).
    /// Tasks map to the game's own ped-task natives (the same pathfinding/driving NPCs use).
    /// Every method here must be called from the script's Tick handler, never from HTTP threads.
    /// </summary>
    internal sealed class TaskEngine
    {
        // Bridge-side defaults where the contract leaves values to the implementation.
        private const float WanderCruiseSpeedMps = 13f;   // ~47 km/h city cruise
        private const float FleeSafeDistanceM = 200f;
        private const int FleeReissueMs = 5000;           // re-aim at updated police position
        private const float WalkArriveRadiusM = 2f;       // contract: walk_to done within 2 m

        // MEASURED IN-GAME 2026-09-03: TASK_FOLLOW_NAV_MESH_TO_COORD moves the player ped to a
        // target 25 m away (arrives) and 120 m away (57.9 m of progress in 10 s), but a target
        // 400 m away produces EXACTLY ZERO movement while the task still reports itself as
        // running. The nav mesh will not path that far in one order, and the silent no-op is what
        // left the agent standing in the street while a goal that had walked him at a distant landmark
        // timed out over and over. So a long walk is issued as a chain of LEGS: each leg is at most
        // WalkLegMaxM toward the real target, and the next one is issued when the current one is
        // reached. `walk_to` still reports `done` only at the caller's real target, so the wire
        // contract (CONTRACTS §1 `{x,y,z,run}`, done within 2 m) is unchanged.
        private const float WalkLegMaxM = 120f;
        private const float WalkLegArriveM = 6f;          // a leg is "reached" loosely; only the real target uses 2 m

        // CONTRACTS v1.9 defaults for follow_entity's in-vehicle tail, applied by BridgeRouter when
        // the caller omits style/speed_mps. The old hard-coded DrivingStyles.Normal + 15 m/s (54
        // km/h) cruise cap could not keep pace with a mission NPC, who neither stops for lights nor
        // caps its own speed - the target simply drove away and the mission failed (observed live
        // as "Franklin lost Lamar"). These two values are chosen to match, not exceed, what a
        // mission NPC does.
        internal const string FollowVehicleDefaultStyleName = "ignore_lights";
        internal const float FollowVehicleDefaultSpeedMps = 30f; // 108 km/h

        // Sanity band clamped onto an explicit (caller-supplied) follow_entity speed_mps before it
        // reaches the VehicleFollow native. Floor: below this the "tail" would fall behind its own
        // target at a crawl, which is never useful. Ceiling: a hair above the fastest Super-class
        // car's real top speed in this game (~55-60 m/s / ~200-215 km/h) - GTA's own vehicle physics
        // caps actual speed there regardless of what is asked for, so anything higher only hands the
        // AI driver task a target speed it can never reach, and this is what stops a malformed or
        // absurd caller value (the "500 m/s tail" case) from doing anything but nothing.
        private const float FollowVehicleMinSpeedMps = 1f;
        private const float FollowVehicleMaxSpeedMps = 60f;

        // CONTRACTS v1.11 driving overhaul item 3 (docs/research/brief-driving-natives.json fact
        // #4): StartVehicleMission's straightLineDist - "the distance in meters at which the AI
        // switches to heading for the target directly instead of following the nodes" - clamped to
        // 255 by the native. This is the actual fix for losing a mission NPC at a junction; plain
        // VehicleFollow (the old call here) has no such parameter. targetReachedDist is irrelevant
        // to VehicleMissionType.Follow (which never "arrives"); -1 selects the SHVDN default.
        private const float FollowStraightLineDistM = 80f;
        private const float FollowTargetReachedDistM = -1f;

        // Item 4 — DISCLOSED driver-competence assist, not a cheat under CLAUDE.md rule 5: no god
        // mode, teleport, invincibility or free money is granted here. SET_DRIVER_ABILITY and
        // Ped.DrivingAggressiveness are ordinary AI-skill dials the engine already exposes for every
        // NPC driver in the game, engine-clamped to <= 1.0 - "a skilled human driver", per
        // docs/research/brief-driving-natives.json's own judgment-call section - not a change to
        // vehicle physics or the rules of the road. 0.8 is a practical ceiling with headroom below
        // the native's hard clamp at 1.0. Aggressiveness is lower for a leisurely tail (0.5) and
        // raised only for the two styles this bridge treats as "keep pace with traffic / pursuit"
        // (rushed, avoid_traffic - see DrivingStyles.cs) so a plain `normal` drive does not also
        // start driving like a chase.
        private const float DriverAbility = 0.8f;
        private const float DriverAggressivenessNormal = 0.5f;
        private const float DriverAggressivenessPursuit = 0.8f;

        // Item 5 — anti-stuck recovery ladder (docs/research/brief-driving-natives.json fact #6).
        // NOT a teleport/warp: TASK_VEHICLE_TEMP_ACTION drives the wheels like a human tapping
        // reverse/steering would. StuckDetectMs matches the brief's own "~4500 ms" recommendation
        // for Vehicle.IsStuckTimerUp(Jammed, ...). ReverseMs/TurnMs are the brief's own rung
        // durations. MaxStuckAttemptsPerEpisode is a judgment call - the brief says only "cap the
        // attempts per episode" without a number; 3 rungs of the full reverse->turn ladder per
        // running task is generous enough to clear a normal jam without turning a genuinely stuck
        // car (flipped, wedged under geometry) into an infinite retry loop the harness never hears
        // about - CLAUDE.md rule 5 forbids the actual fix (warp) for that case, so past the cap the
        // ladder deliberately stops and leaves it to the harness/human (or /unstick, which has its
        // own independent 20 s-stopped precondition).
        private const int StuckDetectMs = 4500;
        private const int StuckReverseMs = 1500;
        private const int StuckTurnMs = 2000;
        private const int MaxStuckAttemptsPerEpisode = 3;

        // eTempAction values used by the stuck ladder (TASK_VEHICLE_TEMP_ACTION 0xC429DCEEB339E129,
        // no SHVDN wrapper; signature and values verified against
        // https://github.com/citizenfx/natives/blob/master/TASK/TaskVehicleTempAction.md).
        private const int TaGoInReverse = 22;
        private const int TaTurnLeftGoReverse = 13;
        private const int TaTurnRightGoReverse = 14;

        // fight_ped (CONTRACTS v1.11 — root-caused from the 2026-09-02 live bug: a carjack victim,
        // plausibly still Respect/Like toward the player, punched the agent to death because
        // TASK_COMBAT_HATED_TARGETS_AROUND_PED silently no-ops without a Neutral/Dislike/Hate
        // relationship — docs/research/brief-combat-natives.json). Branch chosen from the TARGET's
        // weapon class (SnapshotBuilder.WeaponClassOf's IS_PED_ARMED classification), not the
        // caller's intent, so the harness never needs to know it up front.
        //   MELEE PATH (target unarmed or melee): TASK_PUT_PED_DIRECTLY_INTO_MELEE, called RAW
        //   rather than through SHVDN's TaskInvoker.PutDirectlyIntoMelee wrapper — that wrapper's own
        //   XML docs say "Not intended to use with a player Ped" (its aiCombatFlags overload "only
        //   applies when the Ped being given the task is an AI/NPC one"), whereas R*'s own
        //   player_scene_t_bbfight calls the native directly on PLAYER_PED_ID() with these exact
        //   args, and SHVDN separately documents timeInTask as "Only applies when the Ped being
        //   given the task IS a player one" — direct evidence of verified player-ped behaviour for
        //   the RAW native, which the byte-for-byte R* argument list is chosen to match.
        //   RANGED PATH (target has a gun/projectile): TASK_COMBAT_PED via the confirmed-present
        //   SHVDN wrapper Ped.Task.Combat(target, TaskCombatFlags.None, CanFightArmedPedsWhenNotArmed)
        //   — UNVERIFIED for a player ped (docs/research/brief-combat-natives.json: "R* only ever
        //   sets [combat attributes] on the player in am_taxi; zero precedent for TASK_COMBAT_PED on
        //   PLAYER_PED_ID() — must be A/B measured live"). Flagged again in Start/Update below and in
        //   this package's report; needs the live smoke test before it can be trusted.
        private const float MeleeBlendIn = 0f;          // R*'s own args, unchanged
        private const float MeleeStrafePhaseSync = -1f;
        private const float MeleeTimeInTask = 0f;

        // answer_call / reject_call (CONTRACTS v1.13 — the operator watched Simeon call the agent on
        // stream with no way to accept or refuse). Both work through the game's own CONTROL layer
        // rather than a raw keypress, so they are independent of whatever the player has the phone
        // bound to: SET_CONTROL_VALUE_NEXT_FRAME (0xE8A25867FBA3B05E, BOOL(int control, int action,
        // float value)) with value 1.0 for one frame.
        //
        // THE TWO ACTIONS, from the pinned SHVDN v3.7.0.189 GTA.Control enum (verified by
        // reflection over lib/ScriptHookVDotNet3.dll: PhoneSelect = 176, PhoneCancel = 177, and
        // there is NO `Cellphone*` member in this build). Cast to int in a CONST initializer on
        // purpose: the value is baked in at COMPILE time, so a future SHVDN bump that renames or
        // removes either member breaks this BUILD instead of silently injecting the wrong control
        // at runtime — the same reasoning DrivingStyles and player.interior already document.
        private const int PhoneAnswerControl = (int)Control.PhoneSelect;   // 176
        private const int PhoneRejectControl = (int)Control.PhoneCancel;   // 177

        // The native's FIRST argument is the control GROUP, not the action: 0 = PLAYER,
        // 1 = CAMERA, 2 = FRONTEND. The phone reads FRONTEND, but SHVDN's own control helpers pass
        // 0 throughout and the engine is documented as tolerating it, so 0 is tried FIRST.
        //
        // T8 (findings.md R6) — FALLBACK ORDERING, not a single guessed number: nothing on a dev
        // machine can tell 0 from 2 here (this needs the live smoke test), so rather than hard-code
        // one and hope, the injector spends the first half of PhoneInputTimeoutMs on group 0, and if
        // the phone state has not moved by then, switches to group 2 for the remainder. Both
        // StartPhoneInput and UpdatePhoneInput log which group is ACTIVE (StartPhoneInput once at
        // the start, the switch itself once more if it happens), and Done()/Fail() at the end of a
        // completed attempt name the group that was active when the state actually changed — that
        // is the one line the live smoke test reads to know which to hard-code from here on. A
        // virtual/synthetic gamepad (a third, input-SOURCE-level path rather than a different
        // control GROUP) is explicitly OUT OF SCOPE: SHVDN exposes no native or wrapper to
        // synthesize a gamepad device from the game thread — only SET_CONTROL_VALUE_NEXT_FRAME
        // against the engine's existing control groups — and inventing one would mean writing to
        // the Windows input stack directly, which is exactly the kind of unverified guess CLAUDE.md
        // rule 6 forbids for a capability nothing here has asked for.
        private const int PhoneControlGroupPrimary = 0;
        private const int PhoneControlGroupFallback = 2;
        private const float PhoneControlValue = 1f;

        // Which group THIS episode is currently injecting, and whether the fallback has already
        // been tried — both reset per task in StartPhoneInput, the same lifecycle every other
        // per-episode field on this class (e.g. _stuckStage) already follows.
        private int _phoneActiveGroup;
        private bool _phoneFallbackTried;

        // T8's phone-UI-stuck watchdog state (UpdatePhoneUiWatchdog). NOT per-task-episode like the
        // two fields above — this runs every tick regardless of `_req`, so it needs its own
        // independent lifetime rather than being reset by StartPhoneInput.
        private int? _phoneUiIdleSince;      // Game.GameTime the UI was first seen up, ring/call both false
        private int _lastPhoneUiDestroyAt = int.MinValue;

        // How long either phone task keeps injecting before giving up. The input only registers
        // once the phone has RISEN on screen — the game raises it by itself for an incoming call,
        // which is why neither task presses Control.Phone first — so a single frame of injection
        // is not enough and both tasks re-inject every tick until the state actually changes.
        // Bounded because some story calls CANNOT be rejected (the game hides the reject soft key):
        // "still ringing after this long" is reported as failed/"unrejectable" and the task stops,
        // rather than mashing a key at a call the game will not let go of. ~6 s is comfortably
        // longer than the phone's rise animation and short enough that the harness re-plans while
        // the call is still ringing.
        private const int PhoneInputTimeoutMs = 6000;

        // T8: how long the phone UI may sit open with neither a ring nor a call before
        // UpdatePhoneUiWatchdog forces it closed. Longer than PhoneInputTimeoutMs on purpose: an
        // answer_call/reject_call task in flight already owns clearing the UI within 6 s on its
        // own, so this is the backstop for the UI being up for a reason THIS bridge did not cause.
        private const int PhoneUiStuckTimeoutMs = 10000;

        // ---- T1/R1: the drive-start sequence -------------------------------------------------
        //
        // ROOT CAUSE, measured. Bridge log 2026-09-03 09:14Z: `enter_nearest_vehicle` started ->
        // ~1.0 s -> `failed: cleared_by_game`, 111 times in two hours, re-posted within 300 ms by
        // whichever harness owner got the wheel next. `/state` at the same moment: `in_vehicle:
        // false`, an EMPTY Prairie 2.84 m away. Nothing in this file ever confirmed the DRIVER'S
        // SEAT (only `ped.IsInVehicle()`, which is true in a passenger seat and true mid-entry),
        // nothing ever started the ENGINE, and nothing ever checked, after issuing a drive task,
        // that the task was alive and the wheels were turning.
        //
        // The sequence every drive task now runs through PrepareToDrive/ApplyDriveTuning/
        // ArmDriveVerification, in this order:
        //   1. seat        IS_PED_IN_VEHICLE(ped, veh, atGetIn: false) AND
        //                  GET_PED_IN_VEHICLE_SEAT(veh, -1) == ped
        //   2. engine      SET_VEHICLE_ENGINE_ON(veh, true, true, false)
        //   3. the task    TASK_VEHICLE_* (unchanged)
        //   4. tuning      SET_DRIVE_TASK_CRUISE_SPEED + SET_DRIVER_ABILITY +
        //                  SET_DRIVER_AGGRESSIVENESS - all three documented as effective only
        //                  while the drive task is ALREADY running, hence after step 3
        //   5. verify      +2 s: script task live AND veh.Speed > 0, else ONE re-issue, else fail

        /// <summary>How long after a drive task is issued (or re-issued) the bridge grades whether
        /// it actually started. Long enough for the engine to close a door, start the motor and get
        /// the car rolling; short enough that a dead order is reported while leaving is still
        /// survivable (the harness's own motion watchdog, behavior/vehicle.py, sits at 6 s and
        /// grades a stronger 1.5 m/s bar - two layers, deliberately different bars).</summary>
        private const int DriveStartVerifyMs = 2000;

        /// <summary>ONE re-issue per task episode, then a reasoned failure. Not a loop: a re-post
        /// storm is the measured bug (111 clears / 2 h), so the cap is the point of the mechanism,
        /// not a detail of it.</summary>
        private const int MaxDriveStarts = 1;

        // ---- T1: the no-progress watchdog inside a movement step -----------------------------
        //
        // walk_to's own timeout is 5 minutes and enter_nearest_vehicle's is 1 minute: long enough
        // for a whole roam goal to die of old age while the ped stands against a wall. This is the
        // per-step stall check the ticket asks for - 10 s without moving, escalate ONCE by
        // re-issuing the same order, 10 s more, fail the step with a reason the harness can read.
        //
        // NOT applied to follow_entity (a tail standing still next to a target that is also
        // standing still is correct), nor to the combat/cover/phone tasks (standing and shooting is
        // the task). Applied to a VEHICLE task only once the engine's own jam ladder below has
        // spent its attempt budget: a car stopped at a red light for 10 s is not stalled, and
        // failing every drive at every junction would be worse than the bug being fixed.
        private const int NoProgressWindowMs = 10000;

        /// <summary>How far he has to get in <see cref="NoProgressWindowMs"/> to count as making
        /// progress. Above walk_to's own 2 m arrival radius would make arrival unreachable, so it
        /// sits below it: 1.5 m is further than a ped shuffles on the spot and less than one
        /// walking second.</summary>
        private const float NoProgressMinMoveM = 1.5f;

        // Watchdog timeouts (bridge-side judgement; contract names "timeout" as a failure detail).
        private const int DriveToTimeoutMs = 600000;
        private const int WalkToTimeoutMs = 300000;
        private const int EnterVehicleTimeoutMs = 60000;
        private const int ExitVehicleTimeoutMs = 30000;
        private const int FleePedTimeoutMs = 120000;

        // CONTRACTS v1.10 item 4 (task liveness / "cleared_by_game"; recipe:
        // docs/research/brief-script-task-status.json). WaitingToStart is a legal opening state for
        // a just-issued task, so liveness is not judged until this much time has passed since the
        // expected hash was (re)set.
        private const int LivenessSettleMs = 1000;
        // CLEAR_PED_TASKS transition timing is unverified; a single-frame hash mismatch must not
        // kill a healthy task, so a mismatch has to hold for this many consecutive Update() ticks.
        private const int LivenessDebounceTicks = 3;

        // ---- bridge 1.8.0: fly_to - the flight step of the roam goal `go_flying` ---------------
        //
        // One wire verb, two engine tasks, chosen from the aircraft's MODEL at Start():
        //   plane      -> TaskInvoker.StartPlaneMission (wraps TASK_PLANE_MISSION)
        //   helicopter -> TaskInvoker.StartHeliMission  (wraps TASK_HELI_MISSION)
        // both with VehicleMissionType.GoTo and the Vector3-target overload. Every signature and
        // parameter meaning is quoted at the call site in IssueFlyTo from the PINNED
        // lib/Docs/ScriptHookVDotNet3.xml (CLAUDE.md rule 6) so the next reader can check it
        // without the DLL. The enum member names used (VehicleMissionType.GoTo,
        // HeliMissionFlags as a type) were confirmed present in the pinned
        // lib/ScriptHookVDotNet3.dll's string heap, and GoTo=4 is in the verified
        // VehicleMissionType table in docs/research/brief-driving-natives.json.
        //
        // Deliberately NOT a "vehicle driving task" for the rest of this file: the stuck ladder
        // (TASK_VEHICLE_TEMP_ACTION reverse/turn rungs), the 2 s drive-start verification and
        // the 10 s PLANAR no-progress watchdog were all written for a CAR, and each of them
        // would fail or fight a helicopter climbing straight up or a plane holding at the end of
        // the runway. fly_to has its own take-off watchdog instead (FlyToTakeoffTimeoutMs), and
        // the liveness check (cleared_by_game) still applies to it like any other scripted task.
        //
        // UNCOMPILED AND UNVERIFIED IN-GAME as written (no dotnet on the dev box or the server).
        internal const float FlyToDefaultArriveRadiusM = 120f;
        private const float FlyToMinSpeedMps = 10f;
        private const float FlyToMaxSpeedMps = 120f;
        // XML (both mission wrappers): "The height in meters that the heli will try to stay above
        // terrain (ie 20 == always tries to stay at least 20 meters above ground)". Bridge-side;
        // the wire carries only the absolute cruise altitude (flightHeight) as `z`.
        private const int FlyToMinHeightAboveTerrainM = 50;
        // Wheels never left the ground -> failed/"did_not_take_off". Long enough for engine start,
        // a taxi to the runway and a take-off roll; short enough that an aircraft the engine will
        // not fly is reported while the goal still has time to try something else.
        private const int FlyToTakeoffTimeoutMs = 60000;
        private const int FlyToTimeoutMs = 600000;

        private TaskRequest _req;            // null = no task ever posted
        private string _status = "idle";
        private string _detail = "";
        private int _startedAt;              // Game.GameTime ms
        private int _lastFleeReissueAt;
        private int _targetVehicleHandle;

        // CONTRACTS v1.10 item 4: the ScriptTaskNameHash this task's own native call should produce
        // when polled, or null when the task type has no researched hash (seek_cover, set_waypoint,
        // stop, exit_vehicle — brief-script-task-status.json explicitly excludes guessing these) and
        // therefore gets no liveness check at all. Set via SetExpectedHash() at every native
        // call site that issues a scripted task, not once per task TYPE, because some task types
        // (combat_hated_targets_around, follow_entity) issue one of two different underlying
        // natives depending on runtime state.
        //
        // Stored as the enum's raw uint, not GTA.ScriptTaskNameHash itself: bridge/tools/
        // offline-checks loads WastedBridge.dll under .NET 8 against a generated SHVDN stub that
        // only defines the one GTA type its checks actually touch (GTA.VehicleDrivingFlags — see
        // StubGenerator.cs). A field of a SHVDN enum TYPE forces eager resolution of that type when
        // the CLR loads TaskEngine (for field layout), which throws TypeLoadException against the
        // stub before any test even runs; a plain uint field does not. GTA.ScriptTaskNameHash is
        // still used at every call site below (SetExpectedHash's parameter, CheckLiveness's local
        // variables) — those are method-body types, resolved lazily only when JITted, which never
        // happens in the offline harness because it never calls these two methods.
        private uint? _expectedTaskHash;
        private int _expectedHashSetAt;      // Game.GameTime ms, reset on every (re)issue
        private int _clearedStreak;          // consecutive Update() ticks the hash has mismatched

        // T1 drive-start verification state, reset per task episode in Start(). Deliberately
        // primitive-typed, like _expectedTaskHash above and for the same reason: a FIELD of a SHVDN
        // type forces eager type resolution when the CLR loads TaskEngine, which throws
        // TypeLoadException against bridge/tools/offline-checks' stub (it defines exactly one GTA
        // type). That is why the progress anchor below is two floats rather than a Vector3.
        private int _driveVerifyAt;          // Game.GameTime ms at which to grade the start; 0 = off
        private int _driveStarts;            // re-issues of the drive task this episode (cap: 1)
        private float _walkLegX;             // current nav-mesh leg of a long walk (see WalkLegMaxM)
        private float _walkLegY;

        // T1 no-progress watchdog state, reset per task episode in Start().
        private int _progressAt;             // Game.GameTime ms the current no-progress window opened
        private float _progressAnchorX;
        private float _progressAnchorY;
        private int _progressEscalations;    // escalations this episode (cap: 1, then fail)

        // bridge 1.8.0: fly_to's take-off watchdog state, reset per task episode in Start().
        // Primitive-typed for the same offline-checks reason as the fields above.
        private bool _flyEverAirborne;

        // Item 5: anti-stuck recovery ladder state, reset per task episode in Start().
        private enum StuckStage { Idle, Reversing, Turning }
        private StuckStage _stuckStage = StuckStage.Idle;
        private int _stuckStageStartedAt;    // Game.GameTime ms
        private int _stuckAttempts;          // full reverse->turn ladder cycles this episode
        private bool _stuckTurnLeftNext = true; // alternates so a lopsided obstacle isn't retried identically

        public bool IsDriveTaskRunning
        {
            get
            {
                return _status == "running" && _req != null
                       && (_req.Type == "drive_to" || _req.Type == "wander_drive");
            }
        }

        public LastTaskDto ToDto()
        {
            return new LastTaskDto
            {
                Id = _req == null ? null : _req.Id,
                Type = _req == null ? null : _req.Type,
                Status = _status,
                Detail = _detail
            };
        }

        /// <summary>Starts a new task, preempting any running one (contract: old → failed/preempted).</summary>
        public void Start(TaskRequest req)
        {
            if (_status == "running" && _req != null)
            {
                // CONTRACTS §1: the preempted task ends as failed/"preempted". last_task is a
                // single slot, so the new task overwrites it in the same tick and the terminal
                // state of the old one is only ever visible here, in the log. The harness sees the
                // preemption as last_task.id changing while the old task was still running.
                BridgeLog.Info("task " + _req.Id + " (" + _req.Type
                               + ") failed: preempted by " + req.Id + " (" + req.Type + ")");
            }

            _req = req;
            _status = "running";
            _detail = "";
            _startedAt = Game.GameTime;
            _lastFleeReissueAt = 0;
            _targetVehicleHandle = 0;
            _expectedTaskHash = null;    // v1.10: no liveness check until a case below sets one
            _clearedStreak = 0;
            _stuckStage = StuckStage.Idle;   // item 5: fresh episode, fresh attempt budget
            _stuckAttempts = 0;
            _stuckTurnLeftNext = true;
            _driveVerifyAt = 0;          // T1: armed by ArmDriveVerification at each drive issue
            _driveStarts = 0;
            _progressEscalations = 0;
            _progressAt = 0;
            _flyEverAirborne = false;    // 1.8.0: fly_to's take-off watchdog

            Ped ped = Game.Player.Character;
            if (ped == null || !ped.Exists())
            {
                // Happens during a player switch or a load transition; the harness retries.
                Fail("no_player_ped");
                return;
            }

            switch (req.Type)
            {
                case "drive_to":
                {
                    Vehicle veh = CurrentVehicle(ped);
                    if (veh == null)
                    {
                        Fail("not_in_vehicle");
                        return;
                    }
                    IssueDriveTo(ped, veh);
                    break;
                }

                case "walk_to":
                    IssueWalkTo(ped);
                    break;

                case "enter_nearest_vehicle":
                    StartEnterNearestVehicle(ped, req);
                    break;

                case "exit_vehicle":
                    if (ped.IsInVehicle())
                    {
                        ped.Task.LeaveVehicle();
                    }
                    break;

                case "wander_drive":
                {
                    Vehicle veh = CurrentVehicle(ped);
                    if (veh == null)
                    {
                        Fail("not_in_vehicle");
                        return;
                    }
                    IssueWanderDrive(ped, veh);
                    break;
                }

                case "flee_police":
                    if (Game.Player.Wanted.WantedLevel == 0)
                    {
                        Done("");
                        return;
                    }
                    IssueFlee(ped);
                    break;

                case "combat_hated_targets_around":
                    // Engine gotcha (brief-natives): despite the plural name the native engages
                    // only the single closest HATED target and needs hostile relationships to
                    // exist, or it exits immediately. Observed live: 234 of these started and the
                    // ones that ran finished in the same millisecond while cops were shooting from
                    // ~45 m - mission cops attacking the player are "in combat against" him without
                    // necessarily being in a hated group. So: use the hated-group native when it
                    // has something to bite on, otherwise fight the nearest ped that is actually
                    // attacking him. Search radius is widened to at least 60 m for the same reason.
                    {
                        float r = System.Math.Max(req.RadiusM, 60f);
                        if (CountHatedTargets(ped, req.RadiusM) > 0)
                        {
                            ped.Task.CombatHatedTargetsAroundPed(req.RadiusM);
                            // Wraps TASK_COMBAT_HATED_TARGETS_AROUND_PED -> matching hash.
                            SetExpectedHash(ScriptTaskNameHash.CombatHatedTargetsAroundPed);
                        }
                        else
                        {
                            Ped target = NearestHostile(ped, r);
                            if (target != null)
                            {
                                ped.Task.Combat(target, (TaskCombatFlags)0, (TaskThreatResponseFlags)0);
                                // Wraps TASK_COMBAT_PED -> the separate "Combat" script-task hash,
                                // NOT CombatHatedTargetsAroundPed - this branch issues a different
                                // native than the one above, so it needs its own expected hash.
                                SetExpectedHash(ScriptTaskNameHash.Combat);
                            }
                            // else: no hated target found at all - nothing was actually issued to
                            // the engine, so no liveness check is set (matches "running" staying a
                            // no-op state, unchanged pre-existing behavior).
                        }
                    }
                    break;

                case "seek_cover":
                    StartSeekCover(ped, req);
                    break;

                case "follow_entity":
                    StartFollowEntity(ped, req);
                    break;

                case "fight_ped":
                    StartFightPed(ped, req);
                    break;

                case "flee_ped":
                    StartFleePed(ped, req);
                    break;

                // --- bridge 1.7.0 (fix-opus-b, T6) --------------------------------------
                case "shoot_at":
                    StartShootAt(ped, req);
                    break;

                case "drive_by":
                    StartDriveBy(ped, req);
                    break;

                case "enter_vehicle_seat":
                    StartEnterVehicleSeat(ped, req);
                    break;

                case "answer_call":
                case "reject_call":
                    StartPhoneInput(ped, req);
                    break;

                // --- bridge 1.8.0 ---------------------------------------------------------
                case "fly_to":
                    StartFlyTo(ped, req);
                    break;

                case "set_waypoint":
                    World.WaypointPosition = new Vector3(req.X, req.Y, 0f);
                    Done("");
                    break;

                case "stop":
                    ped.Task.ClearAll();
                    _status = "idle";
                    break;

                default:
                    // Unreachable: the HTTP layer rejects unknown types with 400 before enqueue.
                    Fail("unknown_task_type");
                    break;
            }

            if (_status == "running")
            {
                BridgeLog.Info("task " + req.Id + " (" + req.Type + ") started");
            }
        }

        /// <summary>Per-tick completion checks per the CONTRACTS §1 table.</summary>
        public void Update(Ped ped, bool playerDead, bool playerArrested)
        {
            // T8: runs every tick regardless of `_status`/`_req` below — see
            // UpdatePhoneUiWatchdog's own docstring for why it cannot wait for a phone task.
            if (ped != null && ped.Exists() && !playerDead && !playerArrested)
            {
                UpdatePhoneUiWatchdog(ped);
            }

            if (_status != "running" || _req == null)
            {
                return;
            }
            if (ped == null || !ped.Exists())
            {
                Fail("no_player_ped");
                return;
            }
            if (playerDead)
            {
                Fail("player_dead");
                return;
            }
            if (playerArrested)
            {
                Fail("player_arrested");
                return;
            }

            CheckLiveness(ped);
            if (_status != "running")
            {
                // CheckLiveness just failed the task (cleared_by_game) - the per-task-type grading
                // below has nothing left to grade this tick.
                return;
            }

            // Item 5: run the anti-stuck ladder before grading arrival/timeout below. A recovery in
            // progress can itself Fail the task (follow_entity's target going away during a
            // reissue), so re-check status the same way CheckLiveness's caller does.
            UpdateStuckRecovery(ped);
            if (_status != "running")
            {
                return;
            }

            // T1: did the drive order actually take? +2 s after each issue, once per issue.
            UpdateDriveStart(ped);
            if (_status != "running")
            {
                return;
            }

            // T1: the per-step stall watchdog. Runs after the two above so a task the game already
            // cleared, or a drive that never started, is reported as THAT rather than as "he did
            // not move" - the diagnosis the harness reads has to name the actual cause.
            UpdateProgress(ped);
            if (_status != "running")
            {
                return;
            }

            int elapsed = Game.GameTime - _startedAt;

            switch (_req.Type)
            {
                case "drive_to":
                    if (!ped.IsInVehicle())
                    {
                        Fail("not_in_vehicle");
                    }
                    else if (DistanceXY(ped.Position, _req.X, _req.Y) <= _req.ArriveRadiusM)
                    {
                        Done("");
                    }
                    else if (elapsed > DriveToTimeoutMs)
                    {
                        Fail("timeout");
                    }
                    break;

                case "walk_to":
                    if (DistanceXY(ped.Position, _req.X, _req.Y) <= WalkArriveRadiusM)
                    {
                        Done("");
                    }
                    else if (WalkLegIsIntermediate()
                             && DistanceXY(ped.Position, _walkLegX, _walkLegY) <= WalkLegArriveM)
                    {
                        // Reached an intermediate leg of a long walk: aim the next one. Not Done —
                        // the caller asked for the real target and only that ends this task.
                        BridgeLog.Info("task " + _req.Id + " (walk_to): leg reached, "
                            + DistanceXY(ped.Position, _req.X, _req.Y).ToString("F0")
                            + " m still to go - issuing the next leg");
                        IssueWalkTo(ped);
                        ResetProgress(ped);
                    }
                    else if (elapsed > WalkToTimeoutMs)
                    {
                        Fail("timeout");
                    }
                    break;

                case "enter_nearest_vehicle":
                    UpdateEnterNearestVehicle(ped, elapsed);
                    break;

                case "exit_vehicle":
                    if (ped.IsOnFoot)
                    {
                        Done("");
                    }
                    else if (elapsed > ExitVehicleTimeoutMs)
                    {
                        Fail("timeout");
                    }
                    break;

                case "wander_drive":
                    // Never completes; runs until preempted (contract).
                    if (!ped.IsInVehicle())
                    {
                        Fail("not_in_vehicle");
                    }
                    break;

                case "flee_police":
                    if (Game.Player.Wanted.WantedLevel == 0)
                    {
                        Done("");
                    }
                    else if (Game.GameTime - _lastFleeReissueAt > FleeReissueMs)
                    {
                        IssueFlee(ped);
                    }
                    break;

                case "combat_hated_targets_around":
                    // Widened radius (see the start case) and a 2 s grace so the engine's combat
                    // task has actually spun up before we judge "nothing left to fight".
                    if (elapsed > 2000 && CountHatedTargets(ped, System.Math.Max(_req.RadiusM, 60f)) == 0)
                    {
                        Done("");
                    }
                    break;

                case "seek_cover":
                    if (ped.IsInCover)
                    {
                        Done("");
                    }
                    else if (elapsed > (int)(_req.DurationS * 1000f))
                    {
                        Done("timeout"); // contract: cover reached OR timeout both complete the task
                    }
                    break;

                case "follow_entity":
                    UpdateFollowEntity(ped);
                    break;

                case "fight_ped":
                    UpdateFightPed();
                    break;

                case "flee_ped":
                    UpdateFleePed(ped, elapsed);
                    break;

                // --- bridge 1.7.0 (fix-opus-b, T6) --------------------------------------
                case "shoot_at":
                case "drive_by":
                    UpdateTimedFire(elapsed);
                    break;

                case "enter_vehicle_seat":
                    UpdateEnterVehicleSeat(ped, elapsed);
                    break;

                case "answer_call":
                case "reject_call":
                    UpdatePhoneInput(ped, elapsed);
                    break;

                // --- bridge 1.8.0 ---------------------------------------------------------
                case "fly_to":
                    UpdateFlyTo(ped, elapsed);
                    break;
            }
        }

        /// <summary>Records which ScriptTaskNameHash the task just issued should poll as, and
        /// (re)starts the settle window / debounce streak used by <see cref="CheckLiveness"/>.
        /// Called from every native-issuing call site, including flee_police's periodic reissue -
        /// a reissue is itself a fresh task, so it gets its own fresh 1 s settle grace.</summary>
        private void SetExpectedHash(ScriptTaskNameHash hash)
        {
            _expectedTaskHash = (uint)hash;
            _expectedHashSetAt = Game.GameTime;
            _clearedStreak = 0;
        }

        /// <summary>
        /// CONTRACTS v1.10 item 4: detects when the GAME (not the bridge) cleared the ped's
        /// scripted task - mission scripted beats and cutscenes call CLEAR_PED_TASKS on the player,
        /// and without this the bridge kept reporting "running" forever (observed live: a followed
        /// mission car "drove and then stopped" with last_task stuck on running). Recipe from
        /// docs/research/brief-script-task-status.json: read the ped's CURRENT script-task hash and
        /// status every tick; if the hash no longer matches what THIS task's own native call should
        /// have produced (including a mismatch against Invalid), something else took the task away.
        /// Debounced <see cref="LivenessDebounceTicks"/> consecutive ticks (CLEAR_PED_TASKS
        /// transition timing is unverified; a single-frame flicker must not kill a healthy task) and
        /// skipped for <see cref="LivenessSettleMs"/> after (re)issue (WaitingToStart is a legal
        /// opening state). Tasks with no researched hash (_expectedTaskHash stays null - seek_cover,
        /// set_waypoint, stop, exit_vehicle) get no liveness check at all rather than a guessed one.
        /// The existing per-task Update() grading (arrival checks, target_lost, preemption) is
        /// unchanged and still applies on top of this - liveness is an additional failure path.
        /// </summary>
        private void CheckLiveness(Ped ped)
        {
            if (_expectedTaskHash == null)
            {
                return;
            }
            if (_stuckStage != StuckStage.Idle)
            {
                // Item 5: a TASK_VEHICLE_TEMP_ACTION rung is deliberately running instead of the
                // drive task right now (temp actions override it - the whole point of the ladder).
                // Its own script-task hash is never going to match _expectedTaskHash, and that is
                // expected, not the game clearing our task - do not let the debounce accumulate
                // while a rung is in flight, or a stuck-recovery cycle would fail the very task it
                // is trying to save.
                _clearedStreak = 0;
                return;
            }
            if (Game.GameTime - _expectedHashSetAt < LivenessSettleMs)
            {
                return;
            }

            ScriptTaskNameHash currentHash;
            ScriptTaskStatus currentStatus;
            ped.GetCurrentScriptTaskNameHashAndStatus(out currentHash, out currentStatus);

            if ((uint)currentHash == _expectedTaskHash.Value)
            {
                _clearedStreak = 0;
                return;
            }

            _clearedStreak++;
            if (_clearedStreak >= LivenessDebounceTicks)
            {
                // T1. A DRIVE task the game clears gets exactly one re-issue before it is reported
                // failed; everything else fails immediately, unchanged. The measured storm
                // (`started` -> ~1 s -> `failed: cleared_by_game`, 111 times) was the harness
                // re-posting into a game that was going to clear the task again, so the retry
                // belongs HERE, where it is counted and capped, not out there where three owners
                // each get their own turn.
                //
                // The failure detail stays exactly "cleared_by_game" - the contract's own v1.10
                // value, and the string behavior/recovery.py's ClearedByGameBackoff matches
                // EXACTLY (`(task.detail or "") != "cleared_by_game"`). Renaming it here would
                // silently disarm the harness-side backoff, which is the other half of the same
                // fix. What the ticket calls "drive_did_not_start" is the OTHER failure mode -
                // the task is alive and the wheels never turned - and it has its own detail below.
                if (!IsVehicleDrivingTask() || _driveStarts >= MaxDriveStarts)
                {
                    Fail("cleared_by_game");
                    return;
                }
                Vehicle veh = CurrentVehicle(ped);
                if (veh == null)
                {
                    Fail("cleared_by_game");
                    return;
                }
                BridgeLog.Warn("DRIVE START: the game cleared " + Describe()
                               + " (expected script task hash " + _expectedTaskHash.Value
                               + ", " + _clearedStreak + " consecutive ticks mismatched)"
                               + " - re-issuing once, then failing");
                RestartDrive(ped, veh);
            }
        }

        // ---- T1: the drive-start sequence ------------------------------------------------------

        /// <summary>
        /// The seat check the observed failure needed and did not have. `ped.IsInVehicle()` - what
        /// every drive path in this file used to rely on - is true in a PASSENGER seat and true
        /// while the ped is still climbing in, so a drive task issued on it goes to a ped who is
        /// not driving anything.
        ///
        /// Both halves are called RAW rather than through a SHVDN wrapper, on purpose:
        ///   IS_PED_IN_VEHICLE (0xA3EE4A07279BB9DB, BOOL(Ped, Vehicle, BOOL atGetIn)) - the wrapper
        ///     `Ped.IsInVehicle(Vehicle)` is documented in the pinned lib/Docs/ScriptHookVDotNet3.xml
        ///     (M:GTA.Ped.IsInVehicle(GTA.Vehicle)) as "sitting in OR GETTING OUT the specified
        ///     Vehicle", i.e. it does not mean what this check needs. The raw call names
        ///     atGetIn: false explicitly, which is the ticket's own requirement.
        ///   GET_PED_IN_VEHICLE_SEAT - reached through `Vehicle.GetPedOnSeat(VehicleSeat)` because
        ///     that wrapper's exact body was read from SHVDN source during research
        ///     (docs/research/brief-shvdn-blips-occupants.json: "`Vehicle.GetPedOnSeat(VehicleSeat)`
        ///     (returns null for empty seat: `handle != 0 ? new Ped(handle) : null`, native
        ///     GET_PED_IN_VEHICLE_SEAT)"), so it is known to pass the seat index straight through -
        ///     and `VehicleSeat.Driver` is -1 in the pinned DLL (read by MetadataLoadContext over
        ///     bridge/lib/ScriptHookVDotNet3.dll), making this literally
        ///     GET_PED_IN_VEHICLE_SEAT(veh, -1) == ped. Using the wrapper also side-steps the
        ///     native's build-dependent third parameter, which a raw call would have to guess at.
        ///
        /// NOT VERIFIED in-game (no server access from this environment): whether either read is
        /// true on the exact frame TASK_ENTER_VEHICLE reports done. If the live log shows
        /// `not_in_drivers_seat` immediately after a successful entry, the settle window is the
        /// thing to add, not the check.
        /// </summary>
        private static bool InDriversSeat(Ped ped, Vehicle veh)
        {
            if (ped == null || !ped.Exists() || veh == null || !veh.Exists())
            {
                return false;
            }
            if (!Function.Call<bool>(Hash.IS_PED_IN_VEHICLE, ped.Handle, veh.Handle, false))
            {
                return false;
            }
            Ped driver = veh.GetPedOnSeat(VehicleSeat.Driver);
            return driver != null && driver.Exists() && driver.Handle == ped.Handle;
        }

        /// <summary>
        /// The two preconditions of every drive task, in order: confirm the driver's seat, then
        /// start the engine. Returns false having ALREADY failed the task, so callers just return.
        ///
        /// SET_VEHICLE_ENGINE_ON (0x2497C4717C8B881E, void(Vehicle, BOOL value, BOOL instantly,
        /// BOOL disableAutoStart)) is called raw rather than through `Vehicle.IsEngineRunning`'s
        /// setter: the pinned XML documents that property only as "gets or sets a value indicating
        /// whether the engine is running" and says nothing about which of the native's other two
        /// arguments it passes, and `instantly: true` (no ignition animation) with
        /// `disableAutoStart: false` is exactly what this sequence needs. Hash confirmed present in
        /// the pinned DLL's GTA.Native.Hash by MetadataLoadContext.
        ///
        /// Starting a car the agent is sitting in is not a cheat under CLAUDE.md rule 5: turning the
        /// key is what the player character does when a human presses W, and the game's own AI
        /// drivers get the same treatment. No health, money, position or physics is touched.
        /// </summary>
        private bool PrepareToDrive(Ped ped, Vehicle veh)
        {
            if (!InDriversSeat(ped, veh))
            {
                Fail("not_in_drivers_seat");
                return false;
            }
            Function.Call(Hash.SET_VEHICLE_ENGINE_ON, veh.Handle, true, true, false);
            return true;
        }

        /// <summary>Opens the verification window for a drive task that was just issued.</summary>
        private void ArmDriveVerification()
        {
            _driveVerifyAt = Game.GameTime + DriveStartVerifyMs;
        }

        /// <summary>Is the script task this episode issued still the one the ped is running?
        /// Same read as <see cref="CheckLiveness"/> but without the debounce - used at the single
        /// 2 s verification point, where one sample is the whole question. `Vacant`/`Finished` are
        /// the two statuses that mean "not running" (pinned XML: Vacant is what
        /// ScriptTaskNameHash.Invalid resolves to); WaitingToStart and Dormant are both legal
        /// states for a live task the engine has briefly interrupted.</summary>
        private bool ScriptTaskIsLive(Ped ped)
        {
            if (_expectedTaskHash == null)
            {
                return true; // no researched hash for this task type: nothing to judge it against
            }
            ScriptTaskNameHash currentHash;
            ScriptTaskStatus currentStatus;
            ped.GetCurrentScriptTaskNameHashAndStatus(out currentHash, out currentStatus);
            return (uint)currentHash == _expectedTaskHash.Value
                   && currentStatus != ScriptTaskStatus.Vacant
                   && currentStatus != ScriptTaskStatus.Finished;
        }

        /// <summary>
        /// T1's post-start verification: 2 s after a drive task was issued, is the task alive AND
        /// is the car moving? Anything else is a drive order that went nowhere, which is what
        /// `wander_drive`'s old `CruiseWithVehicle(...)` -and-hope did not notice for six seconds.
        /// One re-issue, then <c>failed/"drive_did_not_start"</c>. Never a third.
        /// </summary>
        private void UpdateDriveStart(Ped ped)
        {
            if (_driveVerifyAt == 0 || !IsVehicleDrivingTask())
            {
                return;
            }
            if (_stuckStage != StuckStage.Idle)
            {
                // A TASK_VEHICLE_TEMP_ACTION rung deliberately owns the wheels right now; the drive
                // task is supposed to look dead. Same reasoning as CheckLiveness's guard.
                _driveVerifyAt = Game.GameTime + DriveStartVerifyMs;
                return;
            }
            if (Game.GameTime < _driveVerifyAt)
            {
                return;
            }

            Vehicle veh = CurrentVehicle(ped);
            if (veh == null)
            {
                // Out of the car entirely: the per-task not_in_vehicle grading owns that, and it
                // reports the truth more precisely than this check could.
                _driveVerifyAt = 0;
                return;
            }

            bool live = ScriptTaskIsLive(ped);
            bool moving = veh.Speed > 0f;
            if (live && moving)
            {
                _driveVerifyAt = 0; // it started. The stuck ladder and the timeouts own it now.
                return;
            }

            string why = (live ? "task running, " : "script task not running, ")
                         + "speed " + veh.Speed.ToString("F2") + " m/s";
            if (_driveStarts < MaxDriveStarts && InDriversSeat(ped, veh))
            {
                BridgeLog.Warn("DRIVE START: " + Describe() + " did not start (" + why
                               + ") - re-issuing once, then failing");
                RestartDrive(ped, veh);
                return;
            }
            BridgeLog.Warn("DRIVE START: " + Describe() + " did not start after "
                           + _driveStarts + " re-issue(s) (" + why + ") - giving up");
            Fail("drive_did_not_start");
        }

        /// <summary>The one permitted re-issue: count it, re-run the full seat/engine/task/tuning
        /// sequence, and re-open the verification window so the retry is graded exactly like the
        /// original. <see cref="ReissueVehicleTask"/> routes back through Issue*, which calls
        /// <see cref="PrepareToDrive"/>, so a retry into a seat he has since lost fails
        /// not_in_drivers_seat rather than posting a drive order at a passenger.</summary>
        private void RestartDrive(Ped ped, Vehicle veh)
        {
            _driveStarts++;
            _clearedStreak = 0;
            ReissueVehicleTask(ped, veh);
        }

        // ---- T1: the per-step no-progress watchdog ---------------------------------------------

        /// <summary>The movement steps the no-progress watchdog grades. See NoProgressWindowMs for
        /// why follow_entity and the combat/cover tasks are not on this list.</summary>
        private bool IsProgressWatchedTask()
        {
            if (_status != "running" || _req == null)
            {
                return false;
            }
            return _req.Type == "walk_to" || _req.Type == "enter_nearest_vehicle"
                   || _req.Type == "enter_vehicle_seat"
                   || _req.Type == "flee_ped" || _req.Type == "flee_police"
                   || _req.Type == "drive_to" || _req.Type == "wander_drive";
        }

        private void ResetProgress(Ped ped)
        {
            _progressAt = Game.GameTime;
            _progressAnchorX = ped.Position.X;
            _progressAnchorY = ped.Position.Y;
        }

        /// <summary>10 s without moving <see cref="NoProgressMinMoveM"/>: escalate once by
        /// re-issuing the same order, then fail the step <c>no_progress</c>. This is the watchdog
        /// the ticket asks for inside every movement step; without it walk_to's own timeout is five
        /// minutes of a ped stood against a wall while a roam goal times out around him.</summary>
        private void UpdateProgress(Ped ped)
        {
            if (!IsProgressWatchedTask())
            {
                return;
            }
            if (_stuckStage != StuckStage.Idle)
            {
                ResetProgress(ped); // a temp-action rung is driving; judge nothing while it runs
                return;
            }
            if (IsVehicleDrivingTask() && _stuckAttempts < MaxStuckAttemptsPerEpisode)
            {
                // The engine's own jam ladder gets first refusal on a wedged car, and a red light
                // is not a stall. Only once that ladder is spent does standing still become a
                // failure worth reporting.
                ResetProgress(ped);
                return;
            }
            if (_progressAt == 0)
            {
                ResetProgress(ped);
                return;
            }
            float moved = DistanceXY(ped.Position, _progressAnchorX, _progressAnchorY);
            if (moved >= NoProgressMinMoveM)
            {
                ResetProgress(ped);
                return;
            }
            if (Game.GameTime - _progressAt < NoProgressWindowMs)
            {
                return;
            }
            if (_progressEscalations == 0)
            {
                _progressEscalations = 1;
                BridgeLog.Warn("NO PROGRESS: " + Describe() + " moved " + moved.ToString("F2")
                               + " m in " + NoProgressWindowMs + " ms - re-issuing once");
                ReissueMovementTask(ped);
                if (_status == "running")
                {
                    ResetProgress(ped);
                }
                return;
            }
            BridgeLog.Warn("NO PROGRESS: " + Describe() + " moved " + moved.ToString("F2")
                           + " m in " + NoProgressWindowMs + " ms after an escalation - failing");
            Fail("no_progress");
        }

        /// <summary>The escalation rung: re-issue exactly what this step already asked for. A
        /// vehicle task goes through <see cref="ReissueVehicleTask"/> (seat + engine + tuning +
        /// a fresh verification window); an on-foot one re-runs its own native.</summary>
        private void ReissueMovementTask(Ped ped)
        {
            if (IsVehicleDrivingTask())
            {
                Vehicle veh = CurrentVehicle(ped);
                if (veh != null)
                {
                    ReissueVehicleTask(ped, veh);
                }
                return;
            }
            switch (_req.Type)
            {
                case "walk_to":
                    IssueWalkTo(ped);
                    break;
                case "enter_nearest_vehicle":
                    StartEnterNearestVehicle(ped, _req);
                    break;
                case "flee_ped":
                    StartFleePed(ped, _req);
                    break;
                case "flee_police":
                    IssueFlee(ped);
                    break;
            }
        }

        /// <summary>
        /// The vehicle the ped is actually in, or null. IsInVehicle() and CurrentVehicle can
        /// disagree for a frame while entering/exiting, and CurrentVehicle can hand back a handle
        /// whose entity is already gone — dereferencing either blindly is a null-ref on the game
        /// thread, which SHVDN turns into an aborted script.
        /// </summary>
        private static Vehicle CurrentVehicle(Ped ped)
        {
            if (ped == null || !ped.IsInVehicle())
            {
                return null;
            }
            Vehicle veh = ped.CurrentVehicle;
            return veh != null && veh.Exists() ? veh : null;
        }

        private void StartEnterNearestVehicle(Ped ped, TaskRequest req)
        {
            Vehicle current = CurrentVehicle(ped) ?? Game.Player.LastVehicle;
            int baselineRank = (current != null && current.Exists())
                ? VehicleRank.Rank(current.ClassType)
                : -1;

            Vehicle best = null;
            float bestDist = float.MaxValue;
            bool bestEmpty = false;
            Vehicle bestNicer = null;
            float bestNicerDist = float.MaxValue;
            bool bestNicerEmpty = false;

            Vehicle[] candidates = World.GetNearbyVehicles(ped, req.SearchRadiusM)
                                   ?? new Vehicle[0];
            for (int i = 0; i < candidates.Length; i++)
            {
                Vehicle v = candidates[i];
                if (v == null || !v.Exists() || !v.IsDriveable)
                {
                    continue;
                }
                if (current != null && current.Exists() && v.Handle == current.Handle)
                {
                    continue;
                }
                float dist = DistanceXY(ped.Position, v.Position.X, v.Position.Y);
                bool empty = v.Driver == null || !v.Driver.Exists();

                // Empty vehicles beat occupied ones; distance breaks ties.
                if (Better(empty, dist, bestEmpty, bestDist))
                {
                    best = v;
                    bestDist = dist;
                    bestEmpty = empty;
                }
                if (VehicleRank.Rank(v.ClassType) > baselineRank
                    && Better(empty, dist, bestNicerEmpty, bestNicerDist))
                {
                    bestNicer = v;
                    bestNicerDist = dist;
                    bestNicerEmpty = empty;
                }
            }

            // "nicer" is a preference: when nothing outranks the current/last ride, fall back to
            // the nearest usable vehicle rather than failing (documented in bridge/README.md).
            Vehicle chosen = req.Prefer == "nicer" && bestNicer != null ? bestNicer : best;
            if (chosen == null)
            {
                Fail("no_vehicle_found");
                return;
            }
            _targetVehicleHandle = chosen.Handle;
            ped.Task.EnterVehicle(chosen, VehicleSeat.Driver);
            // Wraps TASK_ENTER_VEHICLE -> EnterVehicle.
            SetExpectedHash(ScriptTaskNameHash.EnterVehicle);
        }

        private void UpdateEnterNearestVehicle(Ped ped, int elapsedMs)
        {
            if (ped.IsInVehicle())
            {
                Done("");
                return;
            }
            Entity target = Entity.FromHandle(_targetVehicleHandle);
            if (target == null || !target.Exists())
            {
                Fail("target_lost");
                return;
            }
            if (elapsedMs > EnterVehicleTimeoutMs)
            {
                Fail("timeout");
            }
        }

        private void StartSeekCover(Ped ped, TaskRequest req)
        {
            Vector3 threat = Game.Player.Wanted.WantedLevel > 0
                ? Game.Player.Wanted.LastPositionSpottedByPolice
                : ped.Position;
            // TASK_SEEK_COVER_FROM_POS has no SHVDN wrapper (verified against nightly.189);
            // hash + signature from docs/research/brief-natives.json (alloc8or DB 2026-07-16).
            Function.Call(Hash.TASK_SEEK_COVER_FROM_POS, ped.Handle,
                threat.X, threat.Y, threat.Z, (int)(req.DurationS * 1000f), false);
        }

        // ---- driving-task issuance (Start() and the item-5 stuck-recovery ladder both call these,
        //      so a re-issue after a temp-action recovery is byte-identical to the original issue)
        // -----------------------------------------------------------------------------------------

        /// <summary>drive_to's native call, factored out so item 5's stuck-recovery ladder can
        /// re-issue exactly this after a temp-action rung, per the brief ("temp actions override
        /// [the drive task]; re-issue it").</summary>
        private void IssueDriveTo(Ped ped, Vehicle veh)
        {
            // T1 steps 1-2: the seat and the engine, before any drive order goes out.
            if (!PrepareToDrive(ped, veh))
            {
                return;
            }
            // Nightly signature: DriveTo(vehicle, target, speed, VehicleDrivingFlags, radius) —
            // argument order differs from stable v3.6.0 (bridge/README). This wraps
            // TASK_VEHICLE_DRIVE_TO_COORD_LONGRANGE, whose native signature (docs/research/
            // brief-natives.json: void(Ped, Vehicle, x, y, z, speed, driveMode, stopRange)) has NO
            // driveAgainstTraffic parameter at all — item 1's audit of every vehicle-task call site
            // found nothing to fix here; wrong-way driving is controlled purely by the style's
            // AllowGoingWrongWay bit (none of the four contract styles set it — DrivingStyles.cs).
            ped.Task.DriveTo(veh, new Vector3(_req.X, _req.Y, _req.Z),
                _req.SpeedMps, _req.Style, _req.ArriveRadiusM);
            // Polls as this exact hash (name match verified against the pinned ScriptTaskNameHash
            // enum).
            SetExpectedHash(ScriptTaskNameHash.VehicleDriveToCoordLongrange);
            ApplyDriveTuning(ped, _req.Style, _req.SpeedMps);
            ArmDriveVerification();
        }

        /// <summary>wander_drive's native call, factored out for the same reissue reason as
        /// <see cref="IssueDriveTo"/>.</summary>
        private void IssueWanderDrive(Ped ped, Vehicle veh)
        {
            // T1 steps 1-2, same as IssueDriveTo. This is the exact call site the 2026-09-03
            // findings name (`ped.Task.CruiseWithVehicle(veh, 13 m/s, style)` with no seat check,
            // no engine and no post-start verification).
            if (!PrepareToDrive(ped, veh))
            {
                return;
            }
            ped.Task.CruiseWithVehicle(veh, WanderCruiseSpeedMps, _req.Style);
            // Wraps TASK_VEHICLE_DRIVE_WANDER (docs/research/brief-natives.json: void(Ped, Vehicle,
            // speed, drivingStyle) — likewise no driveAgainstTraffic parameter, same item-1 audit
            // result as IssueDriveTo). The pinned ScriptTaskNameHash enum has a dedicated
            // VehicleDriveWander member (not just the generic VehicleMission family) whose name
            // matches the native 1:1, verified directly against lib/ScriptHookVDotNet3.dll — used
            // here in preference to the research brief's more tentative "VehicleMission family"
            // guess.
            SetExpectedHash(ScriptTaskNameHash.VehicleDriveWander);
            // wander_drive carries no speed on the wire (CONTRACTS §1: params are `{style}`), so the
            // cruise speed the ticket asks for is this file's own WanderCruiseSpeedMps - set again
            // through SET_DRIVE_TASK_CRUISE_SPEED because the value handed to
            // TASK_VEHICLE_DRIVE_WANDER is only the task's OPENING speed, and the mid-task field is
            // what the engine actually reads afterwards.
            ApplyDriveTuning(ped, _req.Style, WanderCruiseSpeedMps);
            ArmDriveVerification();
        }

        /// <summary>
        /// follow_entity's in-vehicle native call (CONTRACTS v1.11 driving-overhaul item 3),
        /// factored out for the same reissue reason as <see cref="IssueDriveTo"/>. Replaces the old
        /// <c>Task.VehicleFollow</c> (TASK_VEHICLE_FOLLOW) with SHVDN's <c>StartVehicleMission</c>
        /// (TASK_VEHICLE_MISSION_PED_TARGET / TASK_VEHICLE_MISSION, VehicleMissionType.Follow=7),
        /// verified present via MetadataLoadContext against the pinned
        /// bridge/lib/ScriptHookVDotNet3.dll: both the <c>(Vehicle,Ped,...)</c> and
        /// <c>(Vehicle,Vehicle,...)</c> overloads exist, so no raw Function.Call fallback is needed.
        /// Only the mission-family natives expose straightLineDist — "the distance at which the AI
        /// heads straight for the target instead of following the nodes" — which is the actual fix
        /// for losing a mission NPC at a junction; plain VehicleFollow has no such parameter
        /// (docs/research/brief-driving-natives.json fact #4).
        /// </summary>
        private void IssueFollowVehicleMission(Ped ped, Vehicle veh, Entity target)
        {
            // req.Style/req.SpeedMps already carry either the caller's explicit value or the v1.9
            // defaults (BridgeRouter's follow_entity parsing) - clamp the speed into the sane band
            // regardless of which one it is. Unchanged from the old VehicleFollow call.
            // T1 steps 1-2, same as the other two drive issuers: a follow is a drive task too, and
            // "he was in the passenger seat" is exactly as fatal here as it is for wander_drive.
            if (!PrepareToDrive(ped, veh))
            {
                return;
            }
            float speed = System.Math.Min(FollowVehicleMaxSpeedMps,
                System.Math.Max(FollowVehicleMinSpeedMps, _req.SpeedMps));

            // Item 1 — CRITICAL BUG FIX: StartVehicleMission's driveAgainstTraffic overloads default
            // to true when not passed (docs/research/brief-driving-natives.json headline finding
            // #1), a plausible direct cause of head-on crashes. Passed FALSE explicitly here.
            if (target is Ped pedTarget)
            {
                ped.Task.StartVehicleMission(veh, pedTarget, VehicleMissionType.Follow, speed,
                    _req.Style, FollowTargetReachedDistM, FollowStraightLineDistM,
                    driveAgainstTraffic: false);
            }
            else if (target is Vehicle vehTarget)
            {
                ped.Task.StartVehicleMission(veh, vehTarget, VehicleMissionType.Follow, speed,
                    _req.Style, FollowTargetReachedDistM, FollowStraightLineDistM,
                    driveAgainstTraffic: false);
            }
            else
            {
                // The resolved entity is neither a Ped nor a Vehicle (e.g. an object/pickup handle
                // handed to follow_entity) - StartVehicleMission has no overload that tails an
                // arbitrary Entity, and there is no legitimate "drive at a prop" fallback.
                Fail("target_lost");
                return;
            }
            // Wraps TASK_VEHICLE_MISSION_PED_TARGET / TASK_VEHICLE_MISSION depending on target type;
            // both poll under the shared VehicleMission script-task hash family. KNOWN AMBIGUITY
            // (accepted, not fixable from here, unchanged by this swap from VehicleFollow): if
            // something else issues another TASK_VEHICLE_*_MISSION-family task to this same ped
            // while our follow is running, the hash still matches and liveness reads it as "still
            // running" even though it is not our follow anymore - the existing UpdateFollowEntity
            // target_lost / not_in_vehicle checks are what actually catch that case, not this
            // liveness check.
            SetExpectedHash(ScriptTaskNameHash.VehicleMission);
            ApplyDriveTuning(ped, _req.Style, speed);
            ArmDriveVerification();
        }

        /// <summary>
        /// Item 4 — DISCLOSED driver-competence assist (see the constants' doc comment for what
        /// this is and is not). Applied to every vehicle-driving task issue: drive_to, wander_drive,
        /// follow_entity's in-vehicle tail. Called IMMEDIATELY after the native that starts the
        /// drive task, in the same method call — not deferred to the next Tick(). SHVDN's XML docs
        /// for Ped.DrivingAggressiveness/DrivingSpeed/VehicleDrivingFlags all say the setter "must
        /// be on a Vehicle as a driver and the drive task running on this Ped must be active before
        /// setting the value can actually affect", and the same "already running" requirement is
        /// documented for SET_DRIVER_ABILITY's sibling SET_DRIVER_AGGRESSIVENESS. The TASK_VEHICLE_*
        /// natives create their CTaskVehicleMissionBase synchronously when called (per the same XML
        /// remarks, these setters write directly onto fields of that C++ task object), so by the
        /// time this method runs — immediately after — the task instance already exists.
        ///
        /// NOT VERIFIED against the live game (no server access from this dev-machine environment);
        /// this timing choice is the "check which works and document what you chose" judgment call
        /// the brief calls out explicitly. If bridge-smoke or stream telemetry shows the values are
        /// not taking hold, move this call to the following Tick() instead.
        /// </summary>
        private static void ApplyDriveTuning(Ped ped, VehicleDrivingFlags style, float cruiseSpeedMps)
        {
            // SET_DRIVER_ABILITY has no SHVDN wrapper (no P:/M: entry in
            // lib/Docs/ScriptHookVDotNet3.xml; confirmed present in GTA.Native.Hash via
            // MetadataLoadContext against the pinned DLL) - raw Function.Call, same pattern as
            // StartSeekCover's TASK_SEEK_COVER_FROM_POS.
            Function.Call(Hash.SET_DRIVER_ABILITY, ped.Handle, DriverAbility);
            bool pursuit = style == DrivingStyles.Rushed || style == DrivingStyles.AvoidTraffic;
            ped.DrivingAggressiveness = pursuit
                ? DriverAggressivenessPursuit
                : DriverAggressivenessNormal;
            // T1 step 4: SET_DRIVE_TASK_CRUISE_SPEED, through the SHVDN wrapper Ped.DrivingSpeed
            // (verified present and SETTABLE in the pinned DLL by MetadataLoadContext:
            // `GTA.Ped P: Single DrivingSpeed {set;}`; the pinned XML's own remark says it "actually
            // changes the cruise speed field on CTaskVehicleMissionBase"). The same XML is why this
            // runs AFTER the task-issuing native rather than before it: "the drive task running on
            // this Ped must be active before setting the value can actually affect".
            //
            // Guarded because the speed a drive task opened with is not always meaningful to
            // re-assert: a zero or negative here would be an order to stop, which no caller means.
            if (cruiseSpeedMps > 0f)
            {
                ped.DrivingSpeed = cruiseSpeedMps;
            }
        }

        // ---- item 5: anti-stuck recovery ladder ------------------------------------------------

        /// <summary>True while the running task is a vehicle-driving one the stuck ladder covers.
        /// follow_entity only counts in its in-vehicle form (the on-foot tail has no vehicle to get
        /// wedged).</summary>
        private bool IsVehicleDrivingTask()
        {
            if (_status != "running" || _req == null)
            {
                return false;
            }
            return _req.Type == "drive_to" || _req.Type == "wander_drive"
                   || (_req.Type == "follow_entity" && _req.InVehicle);
        }

        /// <summary>
        /// Detects a wedged car (Vehicle.IsStuckTimerUp(Jammed, ~4500 ms)) and runs the bounded
        /// ladder from docs/research/brief-driving-natives.json fact #6: reverse for ~1.5 s; if
        /// still jammed, turn-and-reverse (alternating left/right across episodes) for ~2 s; then
        /// always reset the stuck timer and re-issue the drive task, because a temp action overrides
        /// whatever task was running. Deliberately does NOT call SET_VEHICLE_ON_GROUND_PROPERLY,
        /// SET_ENTITY_COORDS, or any other positional write — CLAUDE.md rule 5: a human cannot right
        /// a flipped car by magic, so neither does the agent; past <see cref="MaxStuckAttemptsPerEpisode"/>
        /// the ladder stops trying and leaves recovery to the harness/human or the existing
        /// (separately gated) /unstick nudge.
        /// </summary>
        private void UpdateStuckRecovery(Ped ped)
        {
            if (!IsVehicleDrivingTask())
            {
                return;
            }
            Vehicle veh = CurrentVehicle(ped);
            if (veh == null)
            {
                return; // not_in_vehicle is caught by the task's own Update() grading, not here.
            }

            if (_stuckStage == StuckStage.Idle)
            {
                if (_stuckAttempts >= MaxStuckAttemptsPerEpisode)
                {
                    return; // cap reached this episode — see the constant's doc comment.
                }
                if (!veh.IsStuckTimerUp(VehicleStuckType.Jammed, StuckDetectMs))
                {
                    return;
                }
                BeginStuckRung(ped, veh, StuckStage.Reversing, TaGoInReverse, StuckReverseMs,
                    "reverse");
                return;
            }

            int elapsed = Game.GameTime - _stuckStageStartedAt;
            if (_stuckStage == StuckStage.Reversing)
            {
                if (elapsed < StuckReverseMs)
                {
                    return; // rung still executing
                }
                if (!veh.IsStuckTimerUp(VehicleStuckType.Jammed, StuckDetectMs))
                {
                    FinishStuckRecovery(ped, veh, "freed after the reverse rung");
                    return;
                }
                int turnAction = _stuckTurnLeftNext ? TaTurnLeftGoReverse : TaTurnRightGoReverse;
                BeginStuckRung(ped, veh, StuckStage.Turning, turnAction, StuckTurnMs,
                    _stuckTurnLeftNext ? "turn-left-and-reverse" : "turn-right-and-reverse");
                return;
            }

            // StuckStage.Turning
            if (elapsed < StuckTurnMs)
            {
                return; // rung still executing
            }
            // Ladder complete either way (freed or not) — the brief's recipe resets the timer and
            // re-issues regardless, rather than adding a third rung.
            FinishStuckRecovery(ped, veh, "ladder complete");
        }

        private void BeginStuckRung(Ped ped, Vehicle veh, StuckStage stage, int tempAction,
                                    int durationMs, string label)
        {
            _stuckStage = stage;
            _stuckStageStartedAt = Game.GameTime;
            if (stage == StuckStage.Reversing)
            {
                _stuckAttempts++; // count once per full ladder cycle, not per rung
            }
            // TASK_VEHICLE_TEMP_ACTION 0xC429DCEEB339E129: void(Ped driver, Vehicle vehicle,
            // int action, int time) — no SHVDN wrapper; signature and eTempAction values verified
            // against https://github.com/citizenfx/natives/blob/master/TASK/TaskVehicleTempAction.md
            // (matches docs/research/brief-driving-natives.json fact #6). This OVERRIDES whatever
            // drive task is running (the brief's own note); CheckLiveness is paused for the duration
            // via the _stuckStage guard below so the temp action is never mistaken for the game
            // clearing our task.
            Function.Call(Hash.TASK_VEHICLE_TEMP_ACTION, ped.Handle, veh.Handle, tempAction,
                durationMs);
            BridgeLog.Warn("STUCK RECOVERY rung " + _stuckAttempts + " (" + label + "): vehicle "
                           + veh.Handle + " jammed >= " + StuckDetectMs + " ms during task "
                           + Describe() + " - issuing TASK_VEHICLE_TEMP_ACTION action=" + tempAction
                           + " for " + durationMs + " ms");
        }

        private void FinishStuckRecovery(Ped ped, Vehicle veh, string reason)
        {
            // RESET_VEHICLE_STUCK_TIMER(veh, ResetAll) via the SHVDN wrapper (verified present:
            // GTA.Vehicle.ResetVehicleStuckTimer(VehicleStuckType) in the pinned DLL's metadata).
            veh.ResetVehicleStuckTimer(VehicleStuckType.ResetAll);
            _stuckTurnLeftNext = !_stuckTurnLeftNext;
            _stuckStage = StuckStage.Idle;
            BridgeLog.Warn("STUCK RECOVERY: " + reason + " - resetting the stuck timer and "
                           + "re-issuing " + Describe());
            // Item 6: this reissue is a rare, event-driven correction (a jam that held for the
            // whole detect+ladder window), never a per-tick re-post - the task-churn hazard the
            // brief warns about does not apply here.
            ReissueVehicleTask(ped, veh);
        }

        /// <summary>Re-issues the exact native the running task started with — the brief's "temp
        /// actions override [the drive task]; re-issue it" recipe.</summary>
        private void ReissueVehicleTask(Ped ped, Vehicle veh)
        {
            switch (_req.Type)
            {
                case "drive_to":
                    IssueDriveTo(ped, veh);
                    break;
                case "wander_drive":
                    IssueWanderDrive(ped, veh);
                    break;
                case "follow_entity":
                    Entity target = Entity.FromHandle(_req.Handle);
                    if (target == null || !target.Exists())
                    {
                        Fail("target_lost");
                        return;
                    }
                    IssueFollowVehicleMission(ped, veh, target);
                    break;
            }
        }

        // ---- bridge 1.8.0: fly_to ----------------------------------------------------------------

        /// <summary>
        /// fly_to's preconditions: he is in something that flies. The seat and the engine are
        /// checked by <see cref="PrepareToDrive"/> inside <see cref="IssueFlyTo"/>, exactly as for
        /// every drive task.
        /// </summary>
        private void StartFlyTo(Ped ped, TaskRequest req)
        {
            Vehicle veh = CurrentVehicle(ped);
            if (veh == null)
            {
                Fail("not_in_vehicle");
                return;
            }
            // Pinned XML, P:GTA.Entity.Model: "Gets the model of the current Entity."
            // P:GTA.Model.IsPlane: "Gets a value indicating whether this Model is a plane."
            // P:GTA.Model.IsHelicopter: "Gets a value indicating whether this Model is a
            // helicopter." Read off the MODEL, so a car taken on the apron fails here at once
            // rather than being handed a flight mission it cannot run - the harness's plan
            // then runs out and re-plans from where he actually is.
            bool plane = veh.Model.IsPlane;
            bool heli = veh.Model.IsHelicopter;
            if (!plane && !heli)
            {
                Fail("not_an_aircraft");
                return;
            }
            IssueFlyTo(ped, veh, heli);
        }

        /// <summary>
        /// The one native call of fly_to. Two SHVDN wrappers, chosen by airframe; both take the
        /// target as a Vector3 and VehicleMissionType.GoTo. The parameter docs below are quoted
        /// verbatim from the pinned lib/Docs/ScriptHookVDotNet3.xml so they can be checked
        /// without the DLL. No SET_DRIVER_ABILITY / DrivingAggressiveness / DrivingSpeed tuning
        /// (those are the CAR-driving dials; the cruise speed goes into the mission natives
        /// directly) and no <see cref="ArmDriveVerification"/> (its grader is keyed on
        /// <see cref="IsVehicleDrivingTask"/>, which this task deliberately is not).
        /// </summary>
        private void IssueFlyTo(Ped ped, Vehicle veh, bool helicopter)
        {
            // T1 steps 1-2 apply to an aircraft too: IS_PED_IN_VEHICLE + GET_PED_IN_VEHICLE_SEAT
            // (driver), then SET_VEHICLE_ENGINE_ON. A mission handed to a passenger flies nobody.
            if (!PrepareToDrive(ped, veh))
            {
                return;
            }
            float speed = System.Math.Min(FlyToMaxSpeedMps,
                System.Math.Max(FlyToMinSpeedMps, _req.SpeedMps));
            // The wire's z IS the cruise altitude; both natives want it as an int flightHeight.
            // The Vector3 target's Z is set to the same value so the two never disagree.
            int flightHeight = (int)System.Math.Round(_req.Z);
            var target = new Vector3(_req.X, _req.Y, _req.Z);

            if (helicopter)
            {
                // Pinned XML, M:GTA.TaskInvoker.StartHeliMission(GTA.Vehicle,GTA.Math.Vector3,
                //   GTA.VehicleMissionType,System.Single,System.Single,System.Int32,System.Int32,
                //   System.Single,System.Single,GTA.HeliMissionFlags)
                //   <summary>Gives the helicopter a mission.</summary>
                //   heli:              "The helicopter."
                //   target:            "The target coordinate."
                //   missionType:       "The vehicle mission type."
                //   cruiseSpeed:       "The cruise speed for the task in m/s."
                //   targetReachedDist: "The distance in meters at which heli thinks it's arrived.
                //                       Also used as the hover distance for Attack and Circle.
                //                       To pick default value 4f, the parameter can be passed in
                //                       as -1 or any other values less than zero."
                //   flightHeight:      "The Z coordinate the heli tries to maintain (i.e. 30 == 30
                //                       meters above sea level)."
                //   minHeightAboveTerrain: "The height in meters that the heli will try to stay
                //                       above terrain (ie 20 == always tries to stay at least 20
                //                       meters above ground)."
                //   heliOrientation:   "The orientation the heli tries to be in (0f to 360f). Use
                //                       -1f (or any value less than zero) if not bothered. -1f
                //                       Should be used in 99% of the times."
                //   slowDownDistance:  "In general, get more control with big number and more
                //                       dynamic with smaller. Setting to -1 means use default
                //                       tuning (100)."
                //   missionFlags:      "The heli mission flags for the task."
                // (HeliMissionFlags)0: the enum's members are not documented in the pinned XML
                // (no T:/F: entries), so no member NAME is relied on - same cast pattern as the
                // (TaskCombatFlags)0 / (TaskThreatResponseFlags)0 calls above.
                ped.Task.StartHeliMission(veh, target, VehicleMissionType.GoTo, speed,
                    _req.ArriveRadiusM, flightHeight, FlyToMinHeightAboveTerrainM,
                    -1f, -1f, (HeliMissionFlags)0);
            }
            else
            {
                // Pinned XML, M:GTA.TaskInvoker.StartPlaneMission(GTA.Vehicle,GTA.Math.Vector3,
                //   GTA.VehicleMissionType,System.Single,System.Single,System.Int32,System.Int32,
                //   System.Single,System.Boolean) - documented by <inheritdoc/> from the
                //   (Vehicle,Vehicle,...) overload, whose text is:
                //   <summary>Gives a plane a mission.</summary>
                //   plane:             "The helicopter." [sic, in the XML]
                //   target:            "The target coordinate."
                //   missionType:       "The vehicle mission type."
                //   cruiseSpeed:       "The cruise speed for the task in m/s."
                //   targetReachedDist: "Distance in meters at which heli thinks it's arrived. Also
                //                       used as the hover distance for Attack and Circle. To pick
                //                       default value 4f, the parameter can be passed in as -1 or
                //                       any other values less than zero."
                //   flightHeight:      "The Z coordinate the heli tries to maintain (i.e. 30 == 30
                //                       meters above sea level)."
                //   minHeightAboveTerrain: "The height in meters that the heli will try to stay
                //                       above terrain (ie 20 == always tries to stay at least 20
                //                       meters above ground)."
                //   planeOrientation:  "The orientation the plane tries to be in (0f to 360f). Use
                //                       -1f if not bothered. -1f Should be used in 99% of the
                //                       times."
                //   precise:           "Specifies whether to tell the plane to move precisely with
                //                       VTOL. ... If the plane does not support VTOL, this
                //                       parameter has no effect."
                ped.Task.StartPlaneMission(veh, target, VehicleMissionType.GoTo, speed,
                    _req.ArriveRadiusM, flightHeight, FlyToMinHeightAboveTerrainM,
                    -1f, false);
            }
            // docs/research/brief-script-task-status.json: "TASK_VEHICLE_FOLLOW, TASK_VEHICLE_ESCORT,
            // TASK_VEHICLE_MISSION, TASK_HELI/PLANE/BOAT_MISSION all report the same script-task
            // type VehicleMission=0xB41F1A34 when polled" - the same hash, with the same known
            // ambiguity, that follow_entity's in-vehicle tail already relies on.
            SetExpectedHash(ScriptTaskNameHash.VehicleMission);
            BridgeLog.Info("task " + Describe() + ": " + (helicopter ? "heli" : "plane")
                           + " mission GoTo (" + _req.X.ToString("F0") + ", " + _req.Y.ToString("F0")
                           + ") at " + flightHeight + " m ASL, " + speed.ToString("F0") + " m/s");
        }

        /// <summary>
        /// fly_to grading. Arrival is planar (drive_to's own rule, and for the same reason: a
        /// ground-projected target Z would make 3D arrival unreachable from cruise altitude).
        /// "The wheels left the ground" is Entity.IsInAir - pinned XML: "Gets a value indicating
        /// whether this Entity is in the air." - the SAME read SnapshotBuilder publishes as
        /// vehicle.in_air, so what fails here is exactly what the harness can see.
        /// </summary>
        private void UpdateFlyTo(Ped ped, int elapsed)
        {
            Vehicle veh = CurrentVehicle(ped);
            if (veh == null)
            {
                Fail("not_in_vehicle");
                return;
            }
            if (!_flyEverAirborne && veh.IsInAir)
            {
                _flyEverAirborne = true;
                BridgeLog.Info("task " + Describe() + ": airborne after " + elapsed + " ms");
            }
            if (DistanceXY(ped.Position, _req.X, _req.Y) <= _req.ArriveRadiusM)
            {
                Done("");
            }
            else if (!_flyEverAirborne && elapsed > FlyToTakeoffTimeoutMs)
            {
                Fail("did_not_take_off");
            }
            else if (elapsed > FlyToTimeoutMs)
            {
                Fail("timeout");
            }
        }

        private void StartFollowEntity(Ped ped, TaskRequest req)
        {
            Entity target = Entity.FromHandle(req.Handle);
            if (target == null || !target.Exists())
            {
                Fail("target_lost");
                return;
            }
            if (req.InVehicle)
            {
                Vehicle veh = CurrentVehicle(ped);
                if (veh == null)
                {
                    Fail("not_in_vehicle");
                    return;
                }
                IssueFollowVehicleMission(ped, veh, target);
            }
            else
            {
                // Trail three meters behind the target at a run.
                ped.Task.FollowToOffsetFromEntity(target, new Vector3(0f, -3f, 0f), 2f);
                // Wraps TASK_FOLLOW_TO_OFFSET_OF_ENTITY -> FollowToOffsetOfEntity.
                SetExpectedHash(ScriptTaskNameHash.FollowToOffsetOfEntity);
            }
        }

        private void UpdateFollowEntity(Ped ped)
        {
            Entity target = Entity.FromHandle(_req.Handle);
            if (target == null || !target.Exists())
            {
                Fail("target_lost");
                return;
            }
            if (_req.InVehicle && !ped.IsInVehicle())
            {
                Fail("not_in_vehicle");
            }
            // Otherwise runs until preempted (contract).
        }

        /// <summary>
        /// fight_ped's Start(): resolve the target, pick melee-vs-ranged from its CURRENT weapon
        /// class, issue the matching native. Dead-on-arrival and missing-handle both fail
        /// target_lost (same convention as follow_entity); a target that is already dead needs no
        /// fight, so that is Done("") immediately rather than a failure.
        /// </summary>
        private void StartFightPed(Ped ped, TaskRequest req)
        {
            Ped target = Entity.FromHandle(req.Handle) as Ped;
            if (target == null || !target.Exists())
            {
                Fail("target_lost");
                return;
            }
            if (target.IsDead)
            {
                Done("");
                return;
            }

            // Bridge 1.7.0 (fix-opus-b, T6): the caller's weapon mode, applied BEFORE the combat
            // task so the engine builds the right CTask for what is actually in his hands.
            //
            //   "unarmed"  fists, whatever he is carrying. This is what makes `pick_a_fight` a
            //              BIT rather than a shooting: a the agent who happens to own a pistol must
            //              not execute a pedestrian who annoyed him.
            //   "armed"    the loadout gun chosen by RANGE (pump shotgun inside 10 m, pistol
            //              beyond) — read here, at task start, rather than from the harness's
            //              snapshot, for the same reason the target's weapon class is re-read
            //              below. Selection is HAS_PED_GOT_WEAPON-guarded: if he owns neither,
            //              nothing is selected and this is a fist fight after all.
            //   "auto"     the v1.11 behaviour, unchanged: answer in kind.
            //
            // Nothing here GIVES him anything — see WeaponState for why the loadout is off by
            // default — so every branch works on whatever he actually earned in-game.
            string mode = string.IsNullOrEmpty(req.WeaponMode) ? "auto" : req.WeaponMode;
            if (mode == "unarmed")
            {
                WeaponState.SelectUnarmed(ped);
            }
            else if (mode == "armed")
            {
                float range = DistanceXY(ped.Position, target.Position.X, target.Position.Y);
                WeaponState.SelectForRange(ped, range);
            }

            // SnapshotBuilder.WeaponClassOf's own IS_PED_ARMED classification, re-read here rather
            // than trusted from a stale /state snapshot: the target's weapon can change between the
            // harness reading /state and this task actually starting.
            bool ranged = Function.Call<bool>(Hash.IS_PED_ARMED, target.Handle, 4)   // gun
                          || Function.Call<bool>(Hash.IS_PED_ARMED, target.Handle, 2); // projectile
            if (mode == "unarmed")
            {
                // The mode is about HIS hands, and it decides the branch too: an armed target
                // would otherwise route a deliberate fist fight into CTaskCombat, which draws
                // whatever he is carrying back out and undoes the selection above.
                ranged = false;
            }
            else if (mode == "armed" && WeaponState.CurrentHash(ped) != WeaponHash.Unarmed)
            {
                ranged = true;
            }

            if (ranged)
            {
                // UNVERIFIED for a player ped — see the constants' doc comment above. Uses the
                // confirmed-present SHVDN wrapper (already used elsewhere in this file for an AI
                // target in the combat_hated_targets_around fallback) so the call itself is not in
                // question, only whether the engine actually lets a PLAYER ped run CTaskCombat.
                ped.Task.Combat(target, TaskCombatFlags.None,
                    TaskThreatResponseFlags.CanFightArmedPedsWhenNotArmed);
                // Wraps TASK_COMBAT_PED -> the "Combat" script-task hash.
                SetExpectedHash(ScriptTaskNameHash.Combat);
            }
            else
            {
                // CONFIRMED player-ped usage (R*'s player_scene_t_bbfight) — see the constants' doc
                // comment. Called RAW, matching R*'s exact 6 positional args, rather than through
                // SHVDN's PutDirectlyIntoMelee wrapper (documented as NPC-oriented).
                Function.Call(Hash.TASK_PUT_PED_DIRECTLY_INTO_MELEE, ped.Handle, target.Handle,
                    MeleeBlendIn, MeleeStrafePhaseSync, MeleeTimeInTask, false);
                // Wraps TASK_PUT_PED_DIRECTLY_INTO_MELEE -> the "PutPedDirectlyIntoMelee" script-task
                // hash (confirmed present in the pinned ScriptTaskNameHash enum via
                // MetadataLoadContext, independent of which call site issues the native).
                SetExpectedHash(ScriptTaskNameHash.PutPedDirectlyIntoMelee);
            }
        }

        /// <summary>
        /// fight_ped's Update(): done once the target is dead or gone — either way there is nothing
        /// left to fight, matching combat_hated_targets_around's "done when nothing left to fight"
        /// philosophy rather than treating a target that despawned mid-fight as a failure. Only an
        /// initially-bad handle (caught in Start) is target_lost.
        /// </summary>
        private void UpdateFightPed()
        {
            Entity target = Entity.FromHandle(_req.Handle);
            if (target == null || !target.Exists() || (target is Ped p && p.IsDead))
            {
                Done("");
            }
            // Otherwise runs until preempted (contract) - no timeout: a real fight has no fixed
            // duration and the game's own combat/melee task ends the encounter (flee, death, or the
            // player wins), at which point the next Update() sees target.IsDead or gone.
        }

        // ======================================================================================
        // Bridge 1.7.0 (fix-opus-b, T6) — the targeted-violence verbs and the passenger seat.
        //
        // EVERY NATIVE HERE WAS VERIFIED AGAINST THE PINNED SHVDN before it was written, by
        // reading the wrapper's IL out of bridge/lib/ScriptHookVDotNet3.dll (assembly 3.7.0.189)
        // with System.Reflection.Metadata and checking which 8-byte native hash it pushes:
        //
        //   GTA.TaskInvoker.ShootAt(Ped, int, FiringPattern)   -> 0x08DA95E8298AE772
        //                                                         TASK_SHOOT_AT_ENTITY
        //   GTA.TaskInvoker.EnterVehicle(Vehicle, VehicleSeat,
        //       int, float, EnterVehicleFlags)                 -> 0xC20E50AA46D09CA8
        //                                                         TASK_ENTER_VEHICLE
        //   GTA.Entity.IsInAir                                 -> 0x886E37EC497200B6
        //                                                         IS_ENTITY_IN_AIR  (SnapshotBuilder)
        //
        // TASK_DRIVE_BY (0x2F8AF0E82773A171) HAS NO SHVDN WRAPPER in the pinned build — the hash
        // is in GTA.Native.Hash, but nothing in TaskInvoker calls it — so it is called RAW, and
        // its argument list comes from citizenfx/natives (TASK/TaskDriveBy.md, fetched
        // 2026-09-03), the same source bridge/src already cites for TASK_VEHICLE_TEMP_ACTION:
        //
        //   void TASK_DRIVE_BY(Ped driverPed, Ped targetPed, Vehicle targetVehicle,
        //                      float x, float y, float z, float distanceToShoot,
        //                      int pedAccuracy, BOOL p8, Hash firingPattern)
        //
        // ** UNVERIFIED, AND FLAGGED RATHER THAN HIDDEN. ** That same page says the native
        // "doesn't seem to do anything" reliably, and docs/research/brief-combat-natives.json
        // records the direct conflict in its own NOT-CONFIRMED list: "TASK_DRIVE_BY on a player
        // ped (NativeDB says it does nothing, TwoPlayerMod uses it successfully)". It ships
        // because the alternative is no drive-by at all, and because the harness goal that uses
        // it (behavior.roam.drive_by_run) is graded on ROUNDS ACTUALLY SPENT — so a native that
        // quietly does nothing produces a timeout and a log line, which is the evidence the
        // operator needs, instead of a completion nobody earned.
        //
        // pedAccuracy is 40, NOT the 75-100 the native's own docs suggest: the combat research
        // brief's fairness list names "SET_PED_ACCURACY>=75" among the cheats to refuse, and a
        // drive-by is spray-and-pray when a human does it too.
        private const float DriveByShootDistM = 60f;
        private const int DriveByAccuracy = 40;

        /// <summary>How long a shoot_at/drive_by burst runs before it is Done. The task is graded
        /// on TIME, not on the target dying: "did he kill him" is not a question /state can
        /// answer for an arbitrary ped, and a burst that ends because the target ran away is a
        /// finished burst, not a failure.</summary>
        private const int FireTaskMinMs = 500;

        /// <summary>enter_vehicle_seat's own budget. Longer than enter_nearest_vehicle's because
        /// the walk to a specific stopped cab is a real walk, and shorter than walk_to's five
        /// minutes because a cab that has not been boarded in ninety seconds has driven off.</summary>
        private const int EnterSeatTimeoutMs = 90000;

        /// <summary>
        /// shoot_at Start(): stand where you are and fire at ONE named ped.
        ///
        /// Weapon SELECTION happens here rather than in the harness because the rule is a
        /// function of RANGE at the instant the task starts (pump shotgun inside 10 m, pistol
        /// beyond), and the harness's most recent snapshot is up to a poll period old — the same
        /// argument fight_ped already makes for re-reading the target's weapon class. Selection
        /// is HAS_PED_GOT_WEAPON-guarded inside WeaponState: nothing is ever conjured, and with
        /// empty hands this is a man pointing at somebody, which is honest and which
        /// `player.weapon` lets the harness see coming.
        /// </summary>
        private void StartShootAt(Ped ped, TaskRequest req)
        {
            Ped target = Entity.FromHandle(req.Handle) as Ped;
            if (target == null || !target.Exists())
            {
                Fail("target_lost");
                return;
            }
            if (target.IsDead)
            {
                Done("");
                return;
            }
            float dist = DistanceXY(ped.Position, target.Position.X, target.Position.Y);
            WeaponHash selected = WeaponState.SelectForRange(ped, dist);
            int durationMs = (int)(req.DurationS * 1000f);
            ped.Task.ShootAt(target, durationMs, WeaponState.PatternFor(selected));
            // Wraps TASK_SHOOT_AT_ENTITY -> the "ShootAtEntity" script-task hash, which is a
            // member of the pinned ScriptTaskNameHash enum (verified by reflection, value
            // 0x0A01F8B8) — so this task gets the v1.10 liveness check like the others.
            SetExpectedHash(ScriptTaskNameHash.ShootAtEntity);
            BridgeLog.Info("task " + req.Id + " (shoot_at): " + selected + " at " + dist.ToString("F1")
                           + " m for " + durationMs + " ms");
        }

        /// <summary>
        /// drive_by Start(): fire out of the car window at ONE named ped while still driving.
        /// Raw TASK_DRIVE_BY — see the block comment above for the sourced signature and for why
        /// its behaviour on a PLAYER ped is flagged unverified rather than assumed.
        /// </summary>
        private void StartDriveBy(Ped ped, TaskRequest req)
        {
            Vehicle veh = CurrentVehicle(ped);
            if (veh == null)
            {
                Fail("not_in_vehicle");
                return;
            }
            Ped target = Entity.FromHandle(req.Handle) as Ped;
            if (target == null || !target.Exists())
            {
                Fail("target_lost");
                return;
            }
            if (target.IsDead)
            {
                Done("");
                return;
            }
            WeaponHash selected = WeaponState.SelectForDriveBy(ped);
            Vector3 at = target.Position;
            Function.Call(Hash.TASK_DRIVE_BY, ped.Handle, target.Handle, 0,
                at.X, at.Y, at.Z, DriveByShootDistM, DriveByAccuracy, false,
                (uint)FiringPattern.BurstFireDriveby);
            // No SetExpectedHash: the pinned ScriptTaskNameHash enum HAS a `DriveBy` member
            // (0x7D711E7D), but whether a raw TASK_DRIVE_BY on a PLAYER ped actually produces it
            // is exactly the thing that is unverified. Arming the v1.10 liveness check on a guess
            // would fail every drive-by after ~1 s with "cleared_by_game" and bury the real
            // answer under a wrong diagnosis; the duration bound below ends the task instead.
            BridgeLog.Info("task " + req.Id + " (drive_by): " + selected + " out of "
                           + veh.DisplayName + " for " + (int)(req.DurationS * 1000f)
                           + " ms — UNVERIFIED native on a player ped; if ammo does not drop, it did nothing");
        }

        /// <summary>
        /// shoot_at / drive_by Update(): both are bounded bursts, so both are Done on the clock.
        /// A minimum of half a second stops a task posted and graded inside one tick from
        /// completing before the engine has issued anything.
        /// </summary>
        private void UpdateTimedFire(int elapsed)
        {
            int budget = (int)(_req.DurationS * 1000f);
            if (elapsed >= System.Math.Max(FireTaskMinMs, budget))
            {
                Done("");
            }
        }

        /// <summary>
        /// enter_vehicle_seat Start(): get in as a PASSENGER. This is the taxi verb — the harness
        /// sets a waypoint first, then puts him in the back and the game's own cab AI drives.
        ///
        /// VehicleSeat is mapped from the wire's 0/1/2 explicitly rather than cast, because the
        /// pinned enum's numbering is not the obvious one (Driver = -1, RightFront = Passenger = 0,
        /// LeftRear = 1, RightRear = 2 — verified by reflection over the pinned DLL), and a cast
        /// would silently turn a future wire value into a seat nobody meant.
        /// </summary>
        private void StartEnterVehicleSeat(Ped ped, TaskRequest req)
        {
            Vehicle veh = Entity.FromHandle(req.Handle) as Vehicle;
            if (veh == null || !veh.Exists() || !veh.IsDriveable)
            {
                Fail("target_lost");
                return;
            }
            VehicleSeat seat;
            switch (req.Seat)
            {
                case 0: seat = VehicleSeat.RightFront; break;
                case 1: seat = VehicleSeat.LeftRear; break;
                default: seat = VehicleSeat.RightRear; break;
            }
            if (!veh.IsSeatFree(seat))
            {
                // Riding as a passenger means taking an EMPTY seat. Jacking somebody out of one
                // is a different act with a different consequence, and it is not what a task
                // named "get in the back" should quietly do.
                Fail("seat_occupied");
                return;
            }
            _targetVehicleHandle = veh.Handle;
            // EnterVehicleFlags.None, not JackAnyone / WarpIn: no teleport into the seat
            // (CLAUDE.md rule 5) and no pulling anyone out. -1 timeout leaves the engine's own
            // budget alone; EnterSeatTimeoutMs below is the bridge's.
            ped.Task.EnterVehicle(veh, seat, -1, 2f, EnterVehicleFlags.None);
            // Wraps TASK_ENTER_VEHICLE -> EnterVehicle, the same hash enter_nearest_vehicle uses.
            SetExpectedHash(ScriptTaskNameHash.EnterVehicle);
        }

        /// <summary>
        /// enter_vehicle_seat Update(): done once he is in THAT vehicle and somebody else has the
        /// wheel. "In the car" alone is not enough — if the entry turned into a jack he is now
        /// the driver, which is a different outcome and the harness grades a taxi ride on exactly
        /// this distinction (behavior.roam.taxi_ride).
        /// </summary>
        private void UpdateEnterVehicleSeat(Ped ped, int elapsedMs)
        {
            Vehicle veh = CurrentVehicle(ped);
            if (veh != null && veh.Handle == _targetVehicleHandle)
            {
                Ped driver = veh.Driver;
                if (driver != null && driver.Exists() && driver.Handle != ped.Handle)
                {
                    Done("");
                    return;
                }
                if (driver == null || !driver.Exists())
                {
                    // He is aboard, but nobody is driving: an empty cab goes nowhere, and
                    // reporting "done" would tell the harness a ride had started.
                    Fail("no_driver");
                    return;
                }
                Fail("took_the_wheel");
                return;
            }
            Entity target = Entity.FromHandle(_targetVehicleHandle);
            if (target == null || !target.Exists())
            {
                Fail("target_lost");
                return;
            }
            if (elapsedMs > EnterSeatTimeoutMs)
            {
                Fail("timeout");
            }
        }

        /// <summary>
        /// answer_call / reject_call Start() (CONTRACTS v1.13).
        ///
        /// No engine ped-task is issued at all — these two drive the game's own CONTROL layer, so
        /// <c>_expectedTaskHash</c> stays null (Start() cleared it) and the v1.10 liveness check
        /// correctly does not apply: there is no script task for the game to clear.
        ///
        /// The preconditions below are checked against a FRESH read rather than the harness's
        /// snapshot, which is up to a poll period old:
        ///   answer_call while a call is already connected -> done, it is answered;
        ///   answer_call with nothing ringing -> failed/"not_ringing", immediately. Injecting
        ///     PhoneSelect at a phone that is not up would otherwise spend 6 s poking at the
        ///     handset's app grid for no reason.
        ///   reject_call with nothing ringing and no call -> done, there is nothing to refuse.
        /// </summary>
        private void StartPhoneInput(Ped ped, TaskRequest req)
        {
            bool answering = req.Type == "answer_call";
            PhoneDto phone = PhoneState.Read(ped);

            if (answering)
            {
                if (phone.InCall)
                {
                    Done("");
                    return;
                }
                if (!phone.Ringing)
                {
                    Fail("not_ringing");
                    return;
                }
            }
            else if (!phone.Ringing && !phone.InCall)
            {
                Done("");
                return;
            }

            // T8: fresh episode, always starts on the primary group; the fallback (if any) is
            // decided in UpdatePhoneInput once enough of the timeout has passed with no result.
            _phoneActiveGroup = PhoneControlGroupPrimary;
            _phoneFallbackTried = false;

            // Logged once per task, at INFO, naming the group actually used: this is the one
            // number that cannot be verified off the server (see the fallback-ordering note
            // above), so the live log has to say which one produced whatever the operator sees on
            // screen.
            BridgeLog.Info("task " + req.Id + " (" + req.Type + "): injecting control "
                           + (answering ? PhoneAnswerControl : PhoneRejectControl)
                           + " (" + (answering ? "PhoneSelect" : "PhoneCancel")
                           + ") in control group " + _phoneActiveGroup
                           + " every tick for up to " + PhoneInputTimeoutMs + " ms"
                           + " (falls back to group " + PhoneControlGroupFallback
                           + " at the halfway point if nothing has moved)");
            InjectPhoneControl(answering, _phoneActiveGroup);
        }

        /// <summary>
        /// answer_call / reject_call Update(): re-inject every tick until the phone state actually
        /// changes, then stop. Bounded by <see cref="PhoneInputTimeoutMs"/>.
        ///
        /// answer_call is done when a call is CONNECTED. If the ringing simply stops without
        /// connecting (the caller gave up) it keeps trying until the timeout and then reports
        /// failed/"unanswered" — honest, because the call was not answered.
        ///
        /// reject_call is done when the phone is neither ringing nor connected. PhoneCancel is
        /// both "reject" and "hang up", so if a call connects anyway mid-reject the same input
        /// keeps working and the task still ends when the line is clear. Its timeout detail is
        /// "unrejectable": SOME story calls hide the reject soft key entirely and cannot be
        /// refused, and there is no native that says which — timing out and reporting it is the
        /// only honest answer, and it is strictly better than looping forever on a call the game
        /// will not let go of.
        /// </summary>
        private void UpdatePhoneInput(Ped ped, int elapsed)
        {
            bool answering = _req.Type == "answer_call";
            PhoneDto phone = PhoneState.Read(ped);

            if (answering)
            {
                if (phone.InCall)
                {
                    BridgeLog.Info("task " + _req.Id + " (" + _req.Type + "): connected via "
                                   + "control group " + _phoneActiveGroup);
                    Done("");
                    return;
                }
            }
            else if (!phone.Ringing && !phone.InCall)
            {
                BridgeLog.Info("task " + _req.Id + " (" + _req.Type + "): line cleared via "
                               + "control group " + _phoneActiveGroup);
                Done("");
                return;
            }

            if (elapsed > PhoneInputTimeoutMs)
            {
                Fail(answering ? "unanswered" : "unrejectable");
                return;
            }

            // T8: halfway through the bound with no result yet — try the other control group for
            // the remainder. Once per episode; StartPhoneInput resets both fields on the next task.
            if (!_phoneFallbackTried && elapsed > PhoneInputTimeoutMs / 2)
            {
                _phoneFallbackTried = true;
                _phoneActiveGroup = PhoneControlGroupFallback;
                BridgeLog.Info("task " + _req.Id + " (" + _req.Type + "): control group "
                               + PhoneControlGroupPrimary + " has not registered after "
                               + (PhoneInputTimeoutMs / 2) + " ms; falling back to control group "
                               + _phoneActiveGroup);
            }
            InjectPhoneControl(answering, _phoneActiveGroup);
        }

        /// <summary>
        /// One frame's worth of one phone control. ONE per frame on purpose: the phone input
        /// system latches a single control per frame, so injecting answer and reject together (or
        /// adding a Control.Phone press alongside) loses one of them. The game raises the handset
        /// by itself for an incoming call, so no Control.Phone press is needed or wanted here.
        /// `controlGroup` is T8's fallback ordering (see the constants above) — the caller decides
        /// which group is active this tick, this only injects into it.
        /// </summary>
        private static void InjectPhoneControl(bool answer, int controlGroup)
        {
            Function.Call(Hash.SET_CONTROL_VALUE_NEXT_FRAME, controlGroup,
                answer ? PhoneAnswerControl : PhoneRejectControl, PhoneControlValue);
        }

        /// <summary>
        /// T8 (findings.md R6) — the phone-UI-stuck watchdog. R1's own evidence was a story call
        /// CONNECTED the whole time, taking the ped's task for the phone UI; this is the general
        /// case of the same failure — the UI can be raised (running CTaskMobilePhone) with NEITHER
        /// `ringing` NOR `in_call` true, whether or not this bridge ever posted answer_call/
        /// reject_call for it, and nothing else in this engine notices.
        ///
        /// IS_PED_RUNNING_MOBILE_PHONE_TASK (0x2AFE52F782F25775, BOOL(Ped)) is the proxy: true
        /// while CTaskMobilePhone is running on the ped, which is the UI being up. Verified present
        /// in the pinned SHVDN v3.7.0.189 GTA.Native.Hash enum the same way every other raw phone
        /// hash in this file is (reflection over the PE metadata of lib/ScriptHookVDotNet3.dll,
        /// reading the Hash field's own constant blob rather than trusting a string match) — no
        /// typed SHVDN wrapper exists for it (there is no GTA.Phone class at all; see
        /// PhoneState.cs), so this is another raw Function.Call. DESTROY_MOBILE_PHONE
        /// (0x3BC861DF703E5097, void(), verified the same way) is the documented way this engine's
        /// own scripts dismiss the handset; its exact parameterless void signature is NOT
        /// independently confirmed against the running game (there is no typed wrapper to check it
        /// against), so this is called exactly like every other bare Hash member with no arguments
        /// in this file (e.g. Hash.SCRIPT_THREAD_ITERATOR_RESET) and the behaviour is listed in this
        /// ticket's NOT VERIFIED — see the T8 report.
        ///
        /// Runs on EVERY tick from Update(), independent of whatever `_status`/`_req` currently are
        /// — the stuck UI is not necessarily something an answer_call/reject_call task ever posted,
        /// so gating this on a running phone task (as the rest of Update() does) would mean it never
        /// runs while idle, which is most of the time. Does nothing while `ringing`/`in_call` is
        /// live: an active ring or a connected call is legitimately using the phone task, and
        /// destroying it there would hang up a call `_phone_reflex` might still want.
        /// </summary>
        private void UpdatePhoneUiWatchdog(Ped ped)
        {
            bool uiUp = Function.Call<bool>(Hash.IS_PED_RUNNING_MOBILE_PHONE_TASK, ped.Handle);
            PhoneDto phone = PhoneState.Read(ped);

            if (!uiUp || phone.Ringing || phone.InCall)
            {
                _phoneUiIdleSince = null;
                return;
            }

            int now = Game.GameTime;
            if (_phoneUiIdleSince == null)
            {
                _phoneUiIdleSince = now;
                return;
            }
            if (unchecked(now - _phoneUiIdleSince.Value) < PhoneUiStuckTimeoutMs)
            {
                return;
            }
            // Rate-limited: DESTROY_MOBILE_PHONE is a blunt instrument, and if it does not clear
            // the UI there is nothing more this native can say — retrying every tick would just be
            // noise. One attempt per PhoneUiStuckTimeoutMs while the condition keeps holding.
            if (unchecked(now - _lastPhoneUiDestroyAt) < PhoneUiStuckTimeoutMs)
            {
                return;
            }
            _lastPhoneUiDestroyAt = now;
            BridgeLog.Warn("phone UI has been open with no ring and no call for at least "
                           + PhoneUiStuckTimeoutMs + " ms; calling DESTROY_MOBILE_PHONE "
                           + "(see UpdatePhoneUiWatchdog)");
            Function.Call(Hash.DESTROY_MOBILE_PHONE);
        }

        // ---- T1: the on-foot movement steps -----------------------------------------------------

        /// <summary>
        /// walk_to's native call, factored out so the no-progress watchdog's escalation can
        /// re-issue exactly the same order (same reason as <see cref="IssueDriveTo"/>).
        ///
        /// TWO CHANGES from the original one-liner, both from the ticket's "on-foot equivalents":
        ///
        /// 1. SET_PED_MOVE_RATE_OVERRIDE(ped, 1.0) (0x085BF80FA50A39D1, void(Ped, float); no SHVDN
        ///    wrapper - no P:/M: entry in lib/Docs/ScriptHookVDotNet3.xml, hash confirmed present in
        ///    GTA.Native.Hash by MetadataLoadContext over the pinned DLL - so raw Function.Call,
        ///    same pattern as SET_DRIVER_ABILITY). Set to exactly 1.0 and never above: 1.0 is the
        ///    game's own default, so this RESTORES a normal walking speed that a mission script,
        ///    an injury reaction or a previous task may have scaled down. A value above 1.0 would
        ///    make the agent move faster than a human can, which CLAUDE.md rule 5 forbids.
        ///
        /// 2. `run: true` is PedMoveBlendRatio.Sprint (3.0f per the pinned XML's own wording,
        ///    "returns the same struct as new PedMoveBlendRatio(3.0f)"), not Run (2.0f) - the
        ///    ticket's "TASK_FOLLOW_NAV_MESH_TO_COORD at 3.0". This is a bridge-side tuning of what
        ///    the frozen `run` flag MEANS, not a contract change: CONTRACTS §1 gives walk_to
        ///    `{x,y,z,run}` and an arrival radius, and says nothing about the blend ratio, exactly
        ///    as it leaves the driving-style bit values to this side.
        /// </summary>
        private void IssueWalkTo(Ped ped)
        {
            Function.Call(Hash.SET_PED_MOVE_RATE_OVERRIDE, ped.Handle, 1f);
            Vector3 leg = NextWalkLeg(ped);
            _walkLegX = leg.X;
            _walkLegY = leg.Y;
            ped.Task.FollowNavMeshTo(leg, _req.Run ? PedMoveBlendRatio.Sprint : PedMoveBlendRatio.Walk);
            // Wraps TASK_FOLLOW_NAV_MESH_TO_COORD -> FollowNavMeshToCoord.
            SetExpectedHash(ScriptTaskNameHash.FollowNavMeshToCoord);
        }

        /// <summary>
        /// The next nav-mesh order for a walk_to: the real target when it is within
        /// <see cref="WalkLegMaxM"/>, otherwise a point that far along the straight line to it.
        /// See the WalkLegMaxM comment for the in-game measurement this exists for.
        /// </summary>
        private Vector3 NextWalkLeg(Ped ped)
        {
            Vector3 here = ped.Position;
            float dx = _req.X - here.X;
            float dy = _req.Y - here.Y;
            float dist = (float)System.Math.Sqrt((dx * dx) + (dy * dy));
            if (dist <= WalkLegMaxM || dist <= 0.01f)
            {
                return new Vector3(_req.X, _req.Y, _req.Z);
            }
            float f = WalkLegMaxM / dist;
            return new Vector3(here.X + (dx * f), here.Y + (dy * f), here.Z);
        }

        /// <summary>True when this walk's current leg is not the caller's real target.</summary>
        private bool WalkLegIsIntermediate()
        {
            float dx = _walkLegX - _req.X;
            float dy = _walkLegY - _req.Y;
            return ((dx * dx) + (dy * dy)) > 1f;
        }

        /// <summary>
        /// flee_ped: run away from ONE named ped, the on-foot counterpart of fight_ped and the
        /// answer the reflex ladder needs when hitting back is not the move.
        ///
        /// TASK_SMART_FLEE_PED through the SHVDN wrapper `TaskInvoker.FleeFrom(Ped otherPed, float
        /// safeDistance, int duration)` - overload confirmed present in the pinned DLL by
        /// MetadataLoadContext, and docs/research/brief-natives.json fact (6) names
        /// TASK_SMART_FLEE_PED/COORD as exactly what Ped.Task.FleeFrom wraps. This is the PED
        /// overload, so it polls as ScriptTaskNameHash.SmartFleePed (1805844857 in the pinned enum),
        /// NOT flee_police's SmartFleePoint - the two are separate members for this reason.
        ///
        /// NOT ON THE WIRE YET. CONTRACTS §1 is frozen at v1.13 and this type is not in it, so the
        /// harness cannot post it: brain/schemas.py's BRIDGE_TASKS and brain/prompts/
        /// action_catalog.md are fix-opus-b's files under findings.md T6 and tests/test_prompts.py
        /// binds the two together. The exact three-line harness change and the proposed CONTRACTS
        /// changelog entry are in this package's report; until they land, this branch is reachable
        /// only from a hand-made POST /task and BridgeRouter validates it like any other type.
        /// </summary>
        private void StartFleePed(Ped ped, TaskRequest req)
        {
            Ped target = Entity.FromHandle(req.Handle) as Ped;
            if (target == null || !target.Exists())
            {
                Fail("target_lost");
                return;
            }
            // Same reasoning as IssueWalkTo: restore the default move rate (never raise it) so a
            // scaled-down one cannot turn fleeing into an amble.
            Function.Call(Hash.SET_PED_MOVE_RATE_OVERRIDE, ped.Handle, 1f);
            ped.Task.FleeFrom(target, FleeSafeDistanceM, -1);
            SetExpectedHash(ScriptTaskNameHash.SmartFleePed);
        }

        /// <summary>flee_ped's Update(): done once he is clear of the threat, or the threat is gone.
        /// A target that despawns mid-flight is a success, not a failure, for the same reason
        /// fight_ped treats it that way - there is nothing left to run from.</summary>
        private void UpdateFleePed(Ped ped, int elapsedMs)
        {
            Ped target = Entity.FromHandle(_req.Handle) as Ped;
            if (target == null || !target.Exists() || target.IsDead)
            {
                Done("");
                return;
            }
            if (ped.Position.DistanceTo(target.Position) >= FleeSafeDistanceM)
            {
                Done("");
                return;
            }
            if (elapsedMs > FleePedTimeoutMs)
            {
                Fail("timeout");
            }
        }

        private void IssueFlee(Ped ped)
        {
            // Same default-move-rate restore as IssueWalkTo/StartFleePed: 1.0 exactly, never above
            // (CLAUDE.md rule 5). Running from the police at a scaled-down move rate is the one
            // place a leftover override would be most expensive.
            Function.Call(Hash.SET_PED_MOVE_RATE_OVERRIDE, ped.Handle, 1f);
            Vector3 threat = Game.Player.Wanted.LastPositionSpottedByPolice;
            // A Vector3 target routes through the TaskInvoker.FleeFrom(Vector3, ...) overload,
            // which wraps TASK_SMART_FLEE_COORD (verified against scripthookvdotnet source) - NOT
            // TASK_SMART_FLEE_PED, which is what a Ped-target FleeFrom overload would use. The
            // ScriptTaskNameHash enum has separate SmartFleePed and SmartFleePoint members for
            // exactly this ped/coord split; SmartFleePoint is the one that matches what this call
            // actually issues (correcting the research brief's tentative SmartFleePed guess, which
            // was written before this call site was checked).
            ped.Task.FleeFrom(threat, FleeSafeDistanceM, -1, false);
            SetExpectedHash(ScriptTaskNameHash.SmartFleePoint);
            _lastFleeReissueAt = Game.GameTime;
        }

        /// <summary>Nearest living, non-animal ped within radius that hates the player or is in
        /// combat against him - the same criteria as CountHatedTargets, returned as a target.</summary>
        private static Ped NearestHostile(Ped player, float radiusM)
        {
            Ped best = null;
            float bestD = float.MaxValue;
            Ped[] peds = World.GetNearbyPeds(player, radiusM) ?? new Ped[0];
            for (int i = 0; i < peds.Length; i++)
            {
                Ped p = peds[i];
                if (p == null || !p.Exists() || p.IsDead || p.Handle == player.Handle)
                {
                    continue;
                }
                // An animal can never be a combat target. Observed live: a cat 29.1 m away reported
                // model='cat' rel=hostile, was selected here, and the agent moved 0.2 m in 20 s during
                // an active mission while narrating "cat" over and over. Reuses
                // SnapshotBuilder.IsAnimal - the same PedType/IsAnimalPed check nearby.peds[] is
                // already filtered with - rather than a second animal test.
                if (SnapshotBuilder.IsAnimal(p, p.Model))
                {
                    continue;
                }
                if (p.GetRelationshipWithPed(player) == Relationship.Hate || p.IsInCombatAgainst(player))
                {
                    float d = player.Position.DistanceTo(p.Position);
                    if (d < bestD) { bestD = d; best = p; }
                }
            }
            return best;
        }

        private static int CountHatedTargets(Ped player, float radiusM)
        {
            int count = 0;
            Ped[] peds = World.GetNearbyPeds(player, radiusM) ?? new Ped[0];
            for (int i = 0; i < peds.Length; i++)
            {
                Ped p = peds[i];
                if (p == null || !p.Exists() || p.IsDead)
                {
                    continue;
                }
                // See NearestHostile: an animal must never count as (or be selected as) a hated
                // combat target.
                if (SnapshotBuilder.IsAnimal(p, p.Model))
                {
                    continue;
                }
                if (p.GetRelationshipWithPed(player) == Relationship.Hate || p.IsInCombatAgainst(player))
                {
                    count++;
                }
            }
            return count;
        }

        private static bool Better(bool empty, float dist, bool bestEmpty, float bestDist)
        {
            if (empty != bestEmpty)
            {
                return empty;
            }
            return dist < bestDist;
        }

        private static float DistanceXY(Vector3 from, float x, float y)
        {
            float dx = from.X - x;
            float dy = from.Y - y;
            return (float)System.Math.Sqrt(dx * dx + dy * dy);
        }

        private void Done(string detail)
        {
            _status = "done";
            _detail = detail;
            BridgeLog.Info("task " + Describe() + " done"
                           + (detail.Length > 0 ? " (" + detail + ")" : ""));
        }

        private void Fail(string detail)
        {
            _status = "failed";
            _detail = detail;
            BridgeLog.Info("task " + Describe() + " failed: " + detail);
        }

        private string Describe()
        {
            return _req == null ? "<none>" : _req.Id + " (" + _req.Type + ")";
        }
    }

    /// <summary>
    /// Bridge-side "niceness" ranking for enter_nearest_vehicle's "nicer" preference
    /// (CONTRACTS §1: higher vehicle class rank than current/last, bridge-side heuristic).
    /// </summary>
    internal static class VehicleRank
    {
        public static int Rank(VehicleClass c)
        {
            switch (c)
            {
                case VehicleClass.Super: return 10;
                case VehicleClass.Sports: return 9;
                case VehicleClass.SportsClassics: return 8;
                case VehicleClass.Coupes: return 7;
                case VehicleClass.Muscle: return 6;
                case VehicleClass.OpenWheel: return 6;
                case VehicleClass.Sedans: return 5;
                case VehicleClass.SUVs: return 4;
                case VehicleClass.OffRoad: return 3;
                case VehicleClass.Motorcycles: return 3;
                case VehicleClass.Compacts: return 2;
                case VehicleClass.Vans: return 1;
                default: return 0; // industrial/utility/service/boats/aircraft/etc.
            }
        }
    }
}
