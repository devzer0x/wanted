using System;
using System.Collections.Generic;
using GTA;
using GTA.Chrono;
using GTA.Math;

namespace WastedBridge
{
    /// <summary>
    /// Game-thread-only construction of the /state snapshot (CONTRACTS.md §1). Reads game state
    /// via SHVDN wrappers/natives each tick; the script serializes the result once and publishes
    /// the JSON string for HTTP threads.
    /// </summary>
    internal sealed class SnapshotBuilder
    {
        private const float NearbyVehicleRadiusM = 80f;
        private const float NearbyPedRadiusM = 50f;
        private const int NearbyTopN = 8;
        // CONTRACTS v1.8: mission.route_blips is bounded so a map full of routed blips can never
        // grow the snapshot (and the harness's prompt) without limit.
        private const int MaxRouteBlips = 5;
        // "speed ≈ 0" threshold for stopped_for_s and the /unstick precondition.
        private const float StoppedSpeedThresholdMps = 0.2f;

        private int _stoppedVehicleHandle;
        private int _stoppedSinceMs = -1;
        private int _lastBlipErrorAt = int.MinValue;
        private int _lastStartsErrorAt = int.MinValue;

        /// <summary>stopped_for_s of the current vehicle as of the last Build call.</summary>
        public float CurrentStoppedForS { get; private set; }

        public Snapshot Build(long tick, TaskEngine engine, string edition,
                              bool playerDead, bool playerArrested)
        {
            Player player = Game.Player;
            Ped ped = player.Character;
            if (ped == null || !ped.Exists())
            {
                // Normal during a load transition or a character switch; the caller keeps serving
                // the previous snapshot and logs this (throttled) rather than publishing garbage.
                throw new InvalidOperationException(
                    "the player ped does not exist yet (loading screen or character switch)");
            }
            Vector3 pos = ped.Position;
            bool inVehicle = ped.IsInVehicle();
            Vehicle veh = inVehicle ? ped.CurrentVehicle : null;
            // One blip pass produces both mission.objective_blip and mission.route_blips: they are
            // the same scan, and running it twice would double the per-frame cost for nothing.
            ObjectiveScan objective = FindObjectiveBlipSafe(pos);

            var snapshot = new Snapshot
            {
                Ts = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ"),
                Tick = tick,
                Player = new PlayerDto
                {
                    Pos = ToDto(pos),
                    Heading = ped.Heading,
                    Health = ped.Health,
                    MaxHealth = ped.MaxHealth,
                    Armor = ped.Armor,
                    Wanted = player.Wanted.WantedLevel,
                    Cash = player.Money,
                    Dead = playerDead,
                    Arrested = playerArrested,
                    InVehicle = inVehicle,
                    ControlEnabled = player.CanControlCharacter,
                    Protagonist = ProtagonistName(ped)
                },
                Vehicle = BuildVehicle(veh),
                Location = new LocationDto
                {
                    Street = World.GetStreetName(pos),
                    Zone = World.GetZoneLocalizedName(pos)
                },
                World = new WorldDto
                {
                    Clock = GameClock.Hour.ToString("D2") + ":" + GameClock.Minute.ToString("D2"),
                    // World.Weather wraps GET_PREV_WEATHER_TYPE_HASH_NAME, which despite the name
                    // is the weather in force right now (World.NextWeather is the one coming).
                    Weather = World.Weather.ToString().ToUpperInvariant(),
                    Timescale = Game.TimeScale
                },
                Mission = new MissionDto
                {
                    Active = Game.IsMissionActive,                 // GET_MISSION_FLAG
                    RandomEventActive = Game.IsRandomEventActive,  // GET_RANDOM_EVENT_FLAG
                    CutsceneActive = Game.IsCutsceneActive,        // IS_CUTSCENE_ACTIVE
                    ObjectiveBlip = objective.Objective,
                    Starts = FindMissionStartsSafe(ped),
                    RouteBlips = objective.RouteBlips
                },
                Nearby = BuildNearby(ped, veh),
                LastTask = engine.ToDto(),
                Bridge = new BridgeInfoDto
                {
                    Version = WastedBridgeScript.BridgeVersion,
                    Edition = edition
                }
            };
            return snapshot;
        }

        private VehicleDto BuildVehicle(Vehicle veh)
        {
            if (veh == null || !veh.Exists())
            {
                CurrentStoppedForS = 0f;
                _stoppedSinceMs = -1;
                _stoppedVehicleHandle = 0;
                return null;
            }

            float speed = veh.Speed;
            if (speed >= StoppedSpeedThresholdMps)
            {
                _stoppedSinceMs = -1;
                _stoppedVehicleHandle = 0;
                CurrentStoppedForS = 0f;
            }
            else
            {
                if (_stoppedSinceMs < 0 || _stoppedVehicleHandle != veh.Handle)
                {
                    _stoppedSinceMs = Game.GameTime;
                    _stoppedVehicleHandle = veh.Handle;
                }
                CurrentStoppedForS = (Game.GameTime - _stoppedSinceMs) / 1000f;
            }

            return new VehicleDto
            {
                Handle = veh.Handle,
                Model = VehicleModelName(veh.Model),
                DisplayName = veh.LocalizedName,
                Class = veh.ClassType.ToString(),
                Speed = speed,
                Health = veh.HealthFloat,
                UpsideDown = veh.IsUpsideDown,
                InWater = veh.IsInWater,
                StoppedForS = CurrentStoppedForS
            };
        }

        /// <summary>CONTRACTS v1.7: who the player currently is, from the ped model.</summary>
        private static string ProtagonistName(Ped ped)
        {
            int hash = ped.Model.Hash;
            if (hash == unchecked((int)PedHash.Michael)) return "michael";
            if (hash == unchecked((int)PedHash.Franklin)) return "franklin";
            if (hash == unchecked((int)PedHash.Trevor)) return "trevor";
            return "unknown";
        }

        private static string ProtagonistForBlipColor(BlipColor c)
        {
            if (c == BlipColor.Michael) return "michael";
            if (c == BlipColor.Franklin) return "franklin";
            if (c == BlipColor.Trevor) return "trevor";
            return null;
        }

        /// <summary>
        /// CONTRACTS v1.7: mission-start markers (the M/F/T letters) currently on the map. They are
        /// identified by the game's own per-protagonist blip COLOURS (BlipColor.Michael / Franklin /
        /// Trevor), which are documented enum members - not by sprite ids, which are not. Nearest
        /// first, capped at 8. Same memory-scan caveat as FindObjectiveBlip.
        /// </summary>
        private List<MissionStartDto> FindMissionStartsSafe(Ped ped)
        {
            try
            {
                return FindMissionStarts(ped);
            }
            catch (Exception ex)
            {
                int now = Environment.TickCount;
                if (unchecked(now - _lastStartsErrorAt) > 5000)
                {
                    _lastStartsErrorAt = now;
                    BridgeLog.Error("mission-start scan failed (World.GetAllBlips memory scan); "
                                    + "mission.starts stays empty", ex);
                }
                return new List<MissionStartDto>();
            }
        }

        private static List<MissionStartDto> FindMissionStarts(Ped ped)
        {
            var found = new List<KeyValuePair<float, MissionStartDto>>();
            Blip[] blips = World.GetAllBlips();
            if (blips == null)
            {
                return new List<MissionStartDto>();
            }
            Vector3 origin = ped.Position;
            for (int i = 0; i < blips.Length; i++)
            {
                Blip b = blips[i];
                if (b == null || !b.Exists())
                {
                    continue;
                }
                string who = ProtagonistForBlipColor(b.Color);
                if (who == null)
                {
                    continue;
                }
                Vector3 pos = b.Position;
                found.Add(new KeyValuePair<float, MissionStartDto>(
                    origin.DistanceTo(pos),
                    new MissionStartDto { Pos = ToDto(pos), Protagonist = who }));
            }
            found.Sort((a, c) => a.Key.CompareTo(c.Key));
            var result = new List<MissionStartDto>();
            for (int i = 0; i < found.Count && i < 8; i++)
            {
                result.Add(found[i].Value);
            }
            return result;
        }

        /// <summary>What one blip pass found: the chosen objective, plus the routed set behind it.</summary>
        private sealed class ObjectiveScan
        {
            public ObjectiveBlipDto Objective;
            public List<RouteBlipDto> RouteBlips = new List<RouteBlipDto>();
        }

        /// <summary>A route-enabled blip plus its distance, so the set can be ordered once.</summary>
        private sealed class RouteCandidate
        {
            public float Distance;
            public Vec3Dto Pos;
            public string Kind;
            public int Handle;
            public BlipColor Color;
        }

        /// <summary>
        /// Objective-blip lookup, isolated behind its own guard: World.GetAllBlips is an SHVDN
        /// memory scan and is by far the most likely call here to break on a game update. One
        /// nullable field is an acceptable loss; freezing all of /state is not.
        /// </summary>
        private ObjectiveScan FindObjectiveBlipSafe(Vector3 origin)
        {
            try
            {
                return FindObjectiveBlip(origin);
            }
            catch (Exception ex)
            {
                int now = Environment.TickCount;
                if (unchecked(now - _lastBlipErrorAt) > 5000)
                {
                    _lastBlipErrorAt = now;
                    BridgeLog.Error("objective-blip scan failed (World.GetAllBlips memory scan); "
                                    + "mission.objective_blip stays null and mission.route_blips "
                                    + "stays empty", ex);
                }
                return new ObjectiveScan();
            }
        }

        /// <summary>
        /// Which blip is "the objective", and the routed set it was picked from.
        ///
        /// WHY THIS IS NOT THE OLD RULE. Until CONTRACTS v1.8 this asked for sprite Standard(1) +
        /// colour Yellow(66) + route enabled, all three. That is the shape of a plain yellow story
        /// marker and nothing else, so every mission whose target is a VEHICLE or a CHARACTER —
        /// different sprite, usually Blue — reported objective_blip: null. Observed live
        /// 2026-09-02: the operator could see the game drawing a blue route line to a car the agent was
        /// meant to follow, /state said there was no objective, and the agent sat on the freeway
        /// narrating "no objective blip yet — wait for the marker" until something killed him.
        ///
        /// THE RULE, in priority order:
        ///   (a) Any blip with ShowRoute == true. That flag is the game's own PED_TYPE-level fact:
        ///       SET_BLIP_ROUTE sets it and it is what makes the engine draw the route line on the
        ///       map. If the game is drawing a line to it, it is where the game wants the player to
        ///       go — whatever its colour or sprite. Colour and sprite are conventions; the route
        ///       flag is the intent.
        ///   (b) Among routed blips, prefer a yellow one. When a mission routes to several things
        ///       at once the yellow one is the primary story objective; the others are secondary
        ///       (a companion, a second drop-off). Ties inside each tier break on distance, nearest
        ///       first, so the pick is stable frame to frame rather than pool-order dependent.
        ///   (c) Only if nothing is routed, fall back to the legacy yellow + Standard marker
        ///       WITHOUT requiring the route flag — a mission that shows a marker but never calls
        ///       SET_BLIP_ROUTE still gets reported.
        ///
        /// What is deliberately NOT an objective:
        ///   - short-range / always-on map furniture (shops, safehouses, properties). It is
        ///     excluded structurally: tiers (a)/(b) need the route flag, which furniture never has,
        ///     and tier (c) additionally rejects IsShortRange.
        ///   - the player's own map waypoint. It draws a route too, and the bridge itself creates
        ///     it (TaskEngine's set_waypoint sets World.WaypointPosition). Feeding it back as "the
        ///     objective" would have the agent chase a marker he placed himself.
        ///
        /// World.GetAllBlips() with no sprite argument is the documented params overload
        /// (ScriptHookVDotNet3.xml: M:GTA.World.GetAllBlips(GTA.BlipSprite[]) — "leave blank to get
        /// all Blips"); it is an SHVDN memory scan and is the fragile part on any edition change,
        /// hence the guard above.
        /// </summary>
        private static ObjectiveScan FindObjectiveBlip(Vector3 origin)
        {
            var scan = new ObjectiveScan();
            Blip[] blips = World.GetAllBlips();
            if (blips == null)
            {
                return scan;
            }

            var routed = new List<RouteCandidate>();
            for (int i = 0; i < blips.Length; i++)
            {
                Blip b = blips[i];
                // ShowRoute is tested before Exists() on purpose: it is a pure memory read of the
                // blip's own Route flag and returns false for a stale handle, so the far more
                // expensive per-blip natives below only run for the handful actually being routed
                // to. Every other blip costs one memory read per tick.
                if (b == null || !b.ShowRoute || !b.Exists())
                {
                    continue;
                }
                BlipColor color = b.Color;
                if (b.Sprite == BlipSprite.Waypoint || color == BlipColor.Waypoint)
                {
                    continue; // the agent's own waypoint, not the game's objective — see above.
                }
                Vector3 blipPos = b.Position;
                routed.Add(new RouteCandidate
                {
                    Distance = origin.DistanceTo(blipPos),
                    Pos = ToDto(blipPos),
                    Kind = IsEntityBlip(b.BlipType) ? "entity" : "coord",
                    Handle = b.Handle,
                    Color = color
                });
            }
            routed.Sort(CompareRouteDistance);

            for (int i = 0; i < routed.Count && i < MaxRouteBlips; i++)
            {
                RouteCandidate c = routed[i];
                scan.RouteBlips.Add(new RouteBlipDto
                {
                    Pos = c.Pos,
                    Kind = c.Kind,
                    Handle = c.Handle,
                    Color = c.Color.ToString()
                });
            }

            if (routed.Count > 0)
            {
                RouteCandidate chosen = routed[0];      // (a) nearest routed blip
                for (int i = 0; i < routed.Count; i++)  // (b) ... unless a yellow one is routed too
                {
                    if (IsYellow(routed[i].Color))
                    {
                        chosen = routed[i];
                        break;                          // routed is distance-sorted: nearest yellow
                    }
                }
                scan.Objective = new ObjectiveBlipDto
                {
                    Pos = chosen.Pos,
                    Kind = chosen.Kind,
                    Handle = chosen.Handle
                };
                return scan;
            }

            scan.Objective = FindLegacyYellowStandardBlip(origin);   // (c)
            return scan;
        }

        /// <summary>
        /// Tier (c): the pre-v1.8 rule minus the route requirement — a yellow Standard marker the
        /// game shows without calling SET_BLIP_ROUTE. Reached only when nothing is routed, so it
        /// can add objectives that used to be missed but can never override a routed one.
        /// GetAllBlips(BlipSprite.Standard) filters on the sprite inside the memory scan, so this
        /// second pass only touches blips that could possibly match.
        /// </summary>
        private static ObjectiveBlipDto FindLegacyYellowStandardBlip(Vector3 origin)
        {
            Blip[] blips = World.GetAllBlips(BlipSprite.Standard);
            if (blips == null)
            {
                return null;
            }
            ObjectiveBlipDto best = null;
            float bestDistance = float.MaxValue;
            for (int i = 0; i < blips.Length; i++)
            {
                Blip b = blips[i];
                if (b == null || !b.Exists())
                {
                    continue;
                }
                if (!IsYellow(b.Color))
                {
                    continue;
                }
                // Short-range blips are the always-on map furniture — shops, safehouses, owned
                // property. A story objective is long-range. Without this, a tier that no longer
                // demands a route flag could point the agent at a clothes shop.
                if (b.IsShortRange)
                {
                    continue;
                }
                Vector3 blipPos = b.Position;
                float distance = origin.DistanceTo(blipPos);
                if (distance >= bestDistance)
                {
                    continue;
                }
                bestDistance = distance;
                best = new ObjectiveBlipDto
                {
                    Pos = ToDto(blipPos),
                    Kind = IsEntityBlip(b.BlipType) ? "entity" : "coord",
                    Handle = b.Handle
                };
            }
            return best;
        }

        /// <summary>
        /// "Looks yellow on the map." BlipColor carries six indices that render identically —
        /// SHVDN's own docs say Yellow2/3/4/5/6 are "always the same as Yellow, the only difference
        /// is color index" (values read from the pinned ScriptHookVDotNet3.dll v3.7.0.189:
        /// Yellow=66, Yellow2=5, Yellow3=60, Yellow4=70, Yellow5=71, Yellow6=73). The old rule
        /// compared against Yellow(66) alone and would have missed a marker on any other index.
        /// YellowDark(56) and MenuYellow(80) are genuinely different colours and are excluded.
        /// </summary>
        private static bool IsYellow(BlipColor color)
        {
            return color == BlipColor.Yellow
                   || color == BlipColor.Yellow2
                   || color == BlipColor.Yellow3
                   || color == BlipColor.Yellow4
                   || color == BlipColor.Yellow5
                   || color == BlipColor.Yellow6;
        }

        /// <summary>
        /// Nearest first. List.Sort is an unstable introsort, so equal distances fall back to the
        /// handle: two blips pinned to the same spot must not swap places from frame to frame and
        /// hand the harness a different objective each tick.
        /// </summary>
        private static int CompareRouteDistance(RouteCandidate a, RouteCandidate b)
        {
            int byDistance = a.Distance.CompareTo(b.Distance);
            return byDistance != 0 ? byDistance : a.Handle.CompareTo(b.Handle);
        }

        private static bool IsEntityBlip(BlipType type)
        {
            switch (type)
            {
                case BlipType.Vehicle:
                case BlipType.Character:
                case BlipType.Object:
                case BlipType.Pickup:
                case BlipType.PickupObject:
                    return true;
                default:
                    return false;
            }
        }

        private static NearbyDto BuildNearby(Ped playerPed, Vehicle ownVehicle)
        {
            var nearby = new NearbyDto();
            Vector3 origin = playerPed.Position;

            var vehicles = new List<NearbyVehicleDto>();
            Vehicle[] rawVehicles = World.GetNearbyVehicles(playerPed, NearbyVehicleRadiusM)
                                    ?? new Vehicle[0];
            for (int i = 0; i < rawVehicles.Length; i++)
            {
                Vehicle v = rawVehicles[i];
                if (v == null || !v.Exists())
                {
                    continue;
                }
                if (ownVehicle != null && v.Handle == ownVehicle.Handle)
                {
                    continue;
                }
                vehicles.Add(new NearbyVehicleDto
                {
                    Handle = v.Handle,
                    Model = VehicleModelName(v.Model),
                    DisplayName = v.LocalizedName,
                    Class = v.ClassType.ToString(),
                    Distance = origin.DistanceTo(v.Position),
                    Driver = DescribeDriver(v),
                    Pos = ToDto(v.Position)
                });
            }
            vehicles.Sort(CompareVehicleDistance);
            nearby.Vehicles = Truncate(vehicles);

            var peds = new List<NearbyPedDto>();
            Ped[] rawPeds = World.GetNearbyPeds(playerPed, NearbyPedRadiusM) ?? new Ped[0];
            for (int i = 0; i < rawPeds.Length; i++)
            {
                Ped p = rawPeds[i];
                if (p == null || !p.Exists() || p.Handle == playerPed.Handle)
                {
                    continue;
                }
                Model model = p.Model;   // read once; PedModelName below reuses it
                if (IsAnimal(p, model))
                {
                    continue;
                }
                Relationship rel = p.GetRelationshipWithPed(playerPed);
                bool hostile = rel == Relationship.Hate || p.IsInCombatAgainst(playerPed);
                // CONTRACTS v1.5: "friendly" = the engine's own companion/like/respect relationship
                // towards the player - i.e. mission crewmates (Michael and Brad in the prologue).
                // Before this every non-hostile ped was "neutral", so the harness could not tell a
                // crewmate from a bystander and "stay with the crew" was unexpressible.
                bool friendly = !hostile
                                && (rel == Relationship.Companion
                                    || rel == Relationship.Like
                                    || rel == Relationship.Respect);
                peds.Add(new NearbyPedDto
                {
                    Handle = p.Handle,
                    Model = PedModelName(model),
                    Distance = origin.DistanceTo(p.Position),
                    Relationship = hostile ? "hostile" : (friendly ? "friendly" : "neutral"),
                    Pos = ToDto(p.Position)
                });
            }
            peds.Sort(ComparePedDistance);
            nearby.Peds = Truncate(peds);

            return nearby;
        }

        /// <summary>
        /// Is this "ped" an animal? In this engine cats, dogs, coyotes, chickens, pigs, rats,
        /// seagulls and sharks are all peds, and World.GetNearbyPeds returns them alongside people.
        ///
        /// Observed live 2026-09-02: a stray cat came back in nearby.peds[] with relationship
        /// "hostile", so both the agent's brain and the combat reflex treated it as an attacker. He
        /// narrated it for a whole mission ("Cat's three meters closer and closing fast") while
        /// real threats went unranked. nearby.peds is meant to be people.
        ///
        /// Two engine-side signals, verified against the pinned SHVDN v3.7.0.189:
        ///   - Ped.PedType wraps GET_PED_TYPE; PedType.Animal is the game's own value 28.
        ///   - Model.IsAnimalPed reads the model's ped-personality "is human" flag and negates it.
        /// Both fail closed: GET_PED_TYPE on an unclassifiable ped does not report Animal, and
        /// IsAnimalPed returns false when the model's personality data cannot be resolved. So a ped
        /// we cannot classify is reported as a person, and no human is ever dropped.
        ///
        /// internal (not private): TaskEngine's NearestHostile/CountHatedTargets reuse this exact
        /// check so an animal can never be selected as a combat target either, rather than growing
        /// a second, possibly-divergent animal test in that file.
        /// </summary>
        internal static bool IsAnimal(Ped p, Model model)
        {
            return p.PedType == PedType.Animal || model.IsAnimalPed;
        }

        private static string DescribeDriver(Vehicle v)
        {
            Ped driver = v.Driver;
            if (driver == null || !driver.Exists())
            {
                return "empty";
            }
            return driver.IsPlayer ? "player" : "npc";
        }

        private static int CompareVehicleDistance(NearbyVehicleDto a, NearbyVehicleDto b)
        {
            return a.Distance.CompareTo(b.Distance);
        }

        private static int ComparePedDistance(NearbyPedDto a, NearbyPedDto b)
        {
            return a.Distance.CompareTo(b.Distance);
        }

        private static List<T> Truncate<T>(List<T> list)
        {
            if (list.Count > NearbyTopN)
            {
                list.RemoveRange(NearbyTopN, list.Count - NearbyTopN);
            }
            return list;
        }

        private static string VehicleModelName(Model model)
        {
            var hash = (VehicleHash)model.Hash;
            return Enum.IsDefined(typeof(VehicleHash), hash)
                ? hash.ToString().ToLowerInvariant()
                : "0x" + model.Hash.ToString("X8");
        }

        private static string PedModelName(Model model)
        {
            var hash = (PedHash)model.Hash;
            return Enum.IsDefined(typeof(PedHash), hash)
                ? hash.ToString().ToLowerInvariant()
                : "0x" + model.Hash.ToString("X8");
        }

        private static Vec3Dto ToDto(Vector3 v)
        {
            return new Vec3Dto { X = v.X, Y = v.Y, Z = v.Z };
        }
    }
}
