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
        // "speed ≈ 0" threshold for stopped_for_s and the /unstick precondition.
        private const float StoppedSpeedThresholdMps = 0.2f;

        private int _stoppedVehicleHandle;
        private int _stoppedSinceMs = -1;

        /// <summary>stopped_for_s of the current vehicle as of the last Build call.</summary>
        public float CurrentStoppedForS { get; private set; }

        public Snapshot Build(long tick, TaskEngine engine, string edition,
                              bool playerDead, bool playerArrested)
        {
            Player player = Game.Player;
            Ped ped = player.Character;
            Vector3 pos = ped.Position;
            bool inVehicle = ped.IsInVehicle();
            Vehicle veh = inVehicle ? ped.CurrentVehicle : null;

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
                    ControlEnabled = player.CanControlCharacter
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
                    Weather = World.Weather.ToString().ToUpperInvariant(),
                    Timescale = Game.TimeScale
                },
                Mission = new MissionDto
                {
                    Active = Game.IsMissionActive,                 // GET_MISSION_FLAG
                    RandomEventActive = Game.IsRandomEventActive,  // GET_RANDOM_EVENT_FLAG
                    CutsceneActive = Game.IsCutsceneActive,        // IS_CUTSCENE_ACTIVE
                    ObjectiveBlip = FindObjectiveBlip()
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

        private static ObjectiveBlipDto FindObjectiveBlip()
        {
            // EMPIRICAL RULE (CONTRACTS §1 / RESEARCH.md §2): the story-mission objective blip is
            // identified as sprite Standard(1) + colour Yellow(66) + route enabled. This is
            // community convention, not documented by Rockstar; it gets validated per mission in
            // Phase 4 and refinements land here without changing the field's shape.
            // World.GetAllBlips is an SHVDN memory scan — the fragile part on any edition change.
            Blip[] blips = World.GetAllBlips(BlipSprite.Standard);
            for (int i = 0; i < blips.Length; i++)
            {
                Blip b = blips[i];
                if (b == null || !b.Exists())
                {
                    continue;
                }
                if (b.Color != BlipColor.Yellow || !b.ShowRoute)
                {
                    continue;
                }
                return new ObjectiveBlipDto
                {
                    Pos = ToDto(b.Position),
                    Kind = IsEntityBlip(b.BlipType) ? "entity" : "coord",
                    Handle = b.Handle
                };
            }
            return null;
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
            Vehicle[] rawVehicles = World.GetNearbyVehicles(playerPed, NearbyVehicleRadiusM);
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
                    Driver = DescribeDriver(v)
                });
            }
            vehicles.Sort(CompareVehicleDistance);
            nearby.Vehicles = Truncate(vehicles);

            var peds = new List<NearbyPedDto>();
            Ped[] rawPeds = World.GetNearbyPeds(playerPed, NearbyPedRadiusM);
            for (int i = 0; i < rawPeds.Length; i++)
            {
                Ped p = rawPeds[i];
                if (p == null || !p.Exists() || p.Handle == playerPed.Handle)
                {
                    continue;
                }
                bool hostile = p.GetRelationshipWithPed(playerPed) == Relationship.Hate
                               || p.IsInCombatAgainst(playerPed);
                peds.Add(new NearbyPedDto
                {
                    Handle = p.Handle,
                    Model = PedModelName(p.Model),
                    Distance = origin.DistanceTo(p.Position),
                    Relationship = hostile ? "hostile" : "neutral"
                });
            }
            peds.Sort(ComparePedDistance);
            nearby.Peds = Truncate(peds);

            return nearby;
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
