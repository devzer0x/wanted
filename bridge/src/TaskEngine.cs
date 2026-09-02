using GTA;
using GTA.Math;
using GTA.Native;

namespace WastedBridge
{
    /// <summary>
    /// Game-thread-only task state machine implementing the 11 CONTRACTS.md §1 task types.
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

        // Watchdog timeouts (bridge-side judgement; contract names "timeout" as a failure detail).
        private const int DriveToTimeoutMs = 600000;
        private const int WalkToTimeoutMs = 300000;
        private const int EnterVehicleTimeoutMs = 60000;
        private const int ExitVehicleTimeoutMs = 30000;

        // CONTRACTS v1.10 item 4 (task liveness / "cleared_by_game"; recipe:
        // docs/research/brief-script-task-status.json). WaitingToStart is a legal opening state for
        // a just-issued task, so liveness is not judged until this much time has passed since the
        // expected hash was (re)set.
        private const int LivenessSettleMs = 1000;
        // CLEAR_PED_TASKS transition timing is unverified; a single-frame hash mismatch must not
        // kill a healthy task, so a mismatch has to hold for this many consecutive Update() ticks.
        private const int LivenessDebounceTicks = 3;

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
                    ped.Task.FollowNavMeshTo(new Vector3(req.X, req.Y, req.Z),
                        req.Run ? PedMoveBlendRatio.Run : PedMoveBlendRatio.Walk);
                    // Wraps TASK_FOLLOW_NAV_MESH_TO_COORD -> FollowNavMeshToCoord.
                    SetExpectedHash(ScriptTaskNameHash.FollowNavMeshToCoord);
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
                Fail("cleared_by_game");
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
            ApplyDriverCompetence(ped, _req.Style);
        }

        /// <summary>wander_drive's native call, factored out for the same reissue reason as
        /// <see cref="IssueDriveTo"/>.</summary>
        private void IssueWanderDrive(Ped ped, Vehicle veh)
        {
            ped.Task.CruiseWithVehicle(veh, WanderCruiseSpeedMps, _req.Style);
            // Wraps TASK_VEHICLE_DRIVE_WANDER (docs/research/brief-natives.json: void(Ped, Vehicle,
            // speed, drivingStyle) — likewise no driveAgainstTraffic parameter, same item-1 audit
            // result as IssueDriveTo). The pinned ScriptTaskNameHash enum has a dedicated
            // VehicleDriveWander member (not just the generic VehicleMission family) whose name
            // matches the native 1:1, verified directly against lib/ScriptHookVDotNet3.dll — used
            // here in preference to the research brief's more tentative "VehicleMission family"
            // guess.
            SetExpectedHash(ScriptTaskNameHash.VehicleDriveWander);
            ApplyDriverCompetence(ped, _req.Style);
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
            ApplyDriverCompetence(ped, _req.Style);
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
        private static void ApplyDriverCompetence(Ped ped, VehicleDrivingFlags style)
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

            // SnapshotBuilder.WeaponClassOf's own IS_PED_ARMED classification, re-read here rather
            // than trusted from a stale /state snapshot: the target's weapon can change between the
            // harness reading /state and this task actually starting.
            bool ranged = Function.Call<bool>(Hash.IS_PED_ARMED, target.Handle, 4)   // gun
                          || Function.Call<bool>(Hash.IS_PED_ARMED, target.Handle, 2); // projectile

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

        private void IssueFlee(Ped ped)
        {
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
