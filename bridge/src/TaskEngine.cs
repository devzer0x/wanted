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
        private const int FollowVehicleDistanceM = 20;    // trailing distance for VehicleFollow, unchanged by v1.9
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
                    // Nightly signature: DriveTo(vehicle, target, speed, VehicleDrivingFlags, radius)
                    // — argument order differs from stable v3.6.0 (bridge/README).
                    ped.Task.DriveTo(veh, new Vector3(req.X, req.Y, req.Z),
                        req.SpeedMps, req.Style, req.ArriveRadiusM);
                    // Wraps TASK_VEHICLE_DRIVE_TO_COORD_LONGRANGE, which polls as this exact hash
                    // (name match verified against the pinned ScriptTaskNameHash enum).
                    SetExpectedHash(ScriptTaskNameHash.VehicleDriveToCoordLongrange);
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
                    ped.Task.CruiseWithVehicle(veh, WanderCruiseSpeedMps, req.Style);
                    // Wraps TASK_VEHICLE_DRIVE_WANDER. The pinned ScriptTaskNameHash enum has a
                    // dedicated VehicleDriveWander member (not just the generic VehicleMission
                    // family) whose name matches the native 1:1, verified directly against
                    // lib/ScriptHookVDotNet3.dll — used here in preference to the research brief's
                    // more tentative "VehicleMission family" guess.
                    SetExpectedHash(ScriptTaskNameHash.VehicleDriveWander);
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
                // req.Style/req.SpeedMps already carry either the caller's explicit value or the
                // v1.9 defaults (BridgeRouter's follow_entity parsing) - clamp the speed into the
                // sane band regardless of which one it is.
                float speed = System.Math.Min(FollowVehicleMaxSpeedMps,
                    System.Math.Max(FollowVehicleMinSpeedMps, req.SpeedMps));
                ped.Task.VehicleFollow(veh, target, speed, req.Style, FollowVehicleDistanceM);
                // Wraps TASK_VEHICLE_FOLLOW, which - per research - polls under the shared
                // "VehicleMission" hash along with TASK_VEHICLE_ESCORT/TASK_VEHICLE_MISSION/heli/
                // plane/boat mission tasks. KNOWN AMBIGUITY (accepted, not fixable from here): if
                // something else issues another TASK_VEHICLE_*_MISSION-family task to this same ped
                // while our follow is running, the hash still matches and liveness reads it as
                // "still running" even though it is not our follow anymore - the existing
                // UpdateFollowEntity target_lost / not_in_vehicle checks are what actually catch
                // that case, not this liveness check.
                SetExpectedHash(ScriptTaskNameHash.VehicleMission);
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
