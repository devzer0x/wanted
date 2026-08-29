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
        private const float FollowVehicleCruiseSpeedMps = 15f;
        private const int FollowVehicleDistanceM = 20;
        private const float WalkArriveRadiusM = 2f;       // contract: walk_to done within 2 m

        // Watchdog timeouts (bridge-side judgement; contract names "timeout" as a failure detail).
        private const int DriveToTimeoutMs = 600000;
        private const int WalkToTimeoutMs = 300000;
        private const int EnterVehicleTimeoutMs = 60000;
        private const int ExitVehicleTimeoutMs = 30000;

        private TaskRequest _req;            // null = no task ever posted
        private string _status = "idle";
        private string _detail = "";
        private int _startedAt;              // Game.GameTime ms
        private int _lastFleeReissueAt;
        private int _targetVehicleHandle;

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
                    break;
                }

                case "walk_to":
                    ped.Task.FollowNavMeshTo(new Vector3(req.X, req.Y, req.Z),
                        req.Run ? PedMoveBlendRatio.Run : PedMoveBlendRatio.Walk);
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
                    // only the single closest hated target and needs hostile relationships to
                    // exist, or it exits immediately. The done-check below is ours, not the task's.
                    ped.Task.CombatHatedTargetsAroundPed(req.RadiusM);
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
                    if (CountHatedTargets(ped, _req.RadiusM) == 0)
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
                ped.Task.VehicleFollow(veh, target, FollowVehicleCruiseSpeedMps,
                    DrivingStyles.Normal, FollowVehicleDistanceM);
            }
            else
            {
                // Trail three meters behind the target at a run.
                ped.Task.FollowToOffsetFromEntity(target, new Vector3(0f, -3f, 0f), 2f);
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
            ped.Task.FleeFrom(threat, FleeSafeDistanceM, -1, false);
            _lastFleeReissueAt = Game.GameTime;
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
