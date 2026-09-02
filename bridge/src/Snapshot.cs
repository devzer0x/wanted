using System.Collections.Generic;
using System.Globalization;
using Newtonsoft.Json;

namespace WastedBridge
{
    // DTOs for GET /state per CONTRACTS.md §1. Instances are immutable-after-publish: the game
    // thread builds a fresh object graph each tick and swaps a single reference, so HTTP threads
    // may read the last published snapshot without locking.

    internal sealed class Vec3Dto
    {
        [JsonProperty("x")] public float X;
        [JsonProperty("y")] public float Y;
        [JsonProperty("z")] public float Z;
    }

    internal sealed class PlayerDto
    {
        [JsonProperty("pos")] public Vec3Dto Pos;
        [JsonProperty("heading")] public float Heading;
        [JsonProperty("health")] public int Health;
        [JsonProperty("max_health")] public int MaxHealth;
        [JsonProperty("armor")] public int Armor;
        [JsonProperty("wanted")] public int Wanted;
        [JsonProperty("cash")] public int Cash;
        [JsonProperty("dead")] public bool Dead;
        [JsonProperty("arrested")] public bool Arrested;
        [JsonProperty("in_vehicle")] public bool InVehicle;
        [JsonProperty("control_enabled")] public bool ControlEnabled;
        [JsonProperty("protagonist")] public string Protagonist;   // v1.7: michael|franklin|trevor|unknown
    }

    internal sealed class VehicleDto
    {
        [JsonProperty("handle")] public int Handle;
        [JsonProperty("model")] public string Model;
        [JsonProperty("display_name")] public string DisplayName;
        [JsonProperty("class")] public string Class;
        [JsonProperty("speed")] public float Speed;
        [JsonProperty("health")] public float Health;
        [JsonProperty("upside_down")] public bool UpsideDown;
        [JsonProperty("in_water")] public bool InWater;
        [JsonProperty("stopped_for_s")] public float StoppedForS;
    }

    internal sealed class LocationDto
    {
        [JsonProperty("street")] public string Street;
        [JsonProperty("zone")] public string Zone;
    }

    internal sealed class WorldDto
    {
        [JsonProperty("clock")] public string Clock;
        [JsonProperty("weather")] public string Weather;
        [JsonProperty("timescale")] public float Timescale;
    }

    internal sealed class ObjectiveBlipDto
    {
        [JsonProperty("pos")] public Vec3Dto Pos;
        [JsonProperty("kind")] public string Kind; // "coord" | "entity"
        [JsonProperty("handle")] public int Handle;
    }

    internal sealed class MissionStartDto
    {
        [JsonProperty("pos")] public Vec3Dto Pos;
        [JsonProperty("protagonist")] public string Protagonist; // michael|franklin|trevor|unknown
    }

    /// <summary>
    /// CONTRACTS v1.8: one blip the game is currently drawing a route line to. objective_blip is
    /// the bridge's single best pick out of this set; the list is here so the harness can reason
    /// when there is more than one (e.g. a follow-target plus a drop-off), and so a wrong pick is
    /// recoverable instead of invisible.
    /// </summary>
    internal sealed class RouteBlipDto
    {
        [JsonProperty("pos")] public Vec3Dto Pos;
        [JsonProperty("kind")] public string Kind;   // "coord" | "entity"
        [JsonProperty("handle")] public int Handle;
        // BlipColor member name ("Yellow", "Blue", ...). An index with no name in the pinned SHVDN
        // enum serializes as its integer rendered as a string; consumers must tolerate that.
        [JsonProperty("color")] public string Color;
    }

    internal sealed class MissionDto
    {
        [JsonProperty("active")] public bool Active;
        [JsonProperty("random_event_active")] public bool RandomEventActive;
        [JsonProperty("cutscene_active")] public bool CutsceneActive;
        [JsonProperty("objective_blip")] public ObjectiveBlipDto ObjectiveBlip;
        [JsonProperty("starts")] public List<MissionStartDto> Starts;    // v1.7: M/F/T markers on the map
        // v1.8: route-enabled blips, nearest first, at most 5. Initialized so the key is always an
        // array — never null — even if a caller forgets to set it.
        [JsonProperty("route_blips")] public List<RouteBlipDto> RouteBlips = new List<RouteBlipDto>();
    }

    internal sealed class NearbyVehicleDto
    {
        [JsonProperty("handle")] public int Handle;
        [JsonProperty("model")] public string Model;
        [JsonProperty("display_name")] public string DisplayName;
        [JsonProperty("class")] public string Class;
        [JsonProperty("distance")] public float Distance;
        [JsonProperty("driver")] public string Driver; // "player" | "npc" | "empty"
        [JsonProperty("pos")] public Vec3Dto Pos;      // v1.6: world position, same shape as player.pos
    }

    internal sealed class NearbyPedDto
    {
        [JsonProperty("handle")] public int Handle;
        [JsonProperty("model")] public string Model;
        [JsonProperty("distance")] public float Distance;
        [JsonProperty("relationship")] public string Relationship; // "neutral" | "hostile" | "friendly"
        [JsonProperty("pos")] public Vec3Dto Pos;                 // v1.6: world position - the minimap dot, as data
    }

    internal sealed class NearbyDto
    {
        [JsonProperty("vehicles")] public List<NearbyVehicleDto> Vehicles = new List<NearbyVehicleDto>();
        [JsonProperty("peds")] public List<NearbyPedDto> Peds = new List<NearbyPedDto>();
    }

    internal sealed class LastTaskDto
    {
        // CONTRACTS v1.2: id and type are NULL — key present, value null — until the first task is
        // posted (status "idle"). status and detail are always present. Do not "fix" the nulls
        // away: the harness's model declares them Optional and a missing key is the mismatch.
        [JsonProperty("id")] public string Id;
        [JsonProperty("type")] public string Type;
        [JsonProperty("status")] public string Status; // idle | running | done | failed
        [JsonProperty("detail")] public string Detail;
    }

    internal sealed class BridgeInfoDto
    {
        [JsonProperty("version")] public string Version;
        // CONTRACTS v1.2 enum: "legacy" | "enhanced" | "unknown" (before detection succeeds).
        [JsonProperty("edition")] public string Edition;
    }

    internal sealed class Snapshot
    {
        [JsonProperty("ts")] public string Ts;
        [JsonProperty("tick")] public long Tick;
        [JsonProperty("player")] public PlayerDto Player;
        [JsonProperty("vehicle")] public VehicleDto Vehicle;
        [JsonProperty("location")] public LocationDto Location;
        [JsonProperty("world")] public WorldDto World;
        [JsonProperty("mission")] public MissionDto Mission;
        [JsonProperty("nearby")] public NearbyDto Nearby;
        [JsonProperty("last_task")] public LastTaskDto LastTask;
        [JsonProperty("bridge")] public BridgeInfoDto Bridge;
    }

    /// <summary>
    /// The single place /state JSON is produced. The game thread publishes through it every tick,
    /// and the offline contract-sample tool (bridge/tools/offline-checks) calls this exact method
    /// on the compiled assembly — so bridge/contract-samples/*.json are byte-for-byte what the
    /// bridge serves, not a hand-written approximation of it.
    /// </summary>
    internal static class SnapshotJson
    {
        /// <summary>
        /// NullValueHandling is pinned to Include on purpose: CONTRACTS v1.2 requires
        /// <c>last_task.id</c>/<c>type</c> to be present-and-null before the first task, and
        /// <c>vehicle</c>/<c>mission.objective_blip</c> to be present-and-null when absent.
        /// Switching this to Ignore would drop the keys and break every consumer.
        /// </summary>
        private static readonly JsonSerializerSettings Settings = new JsonSerializerSettings
        {
            NullValueHandling = NullValueHandling.Include,
            Culture = CultureInfo.InvariantCulture
        };

        public static string Serialize(Snapshot snapshot)
        {
            return JsonConvert.SerializeObject(snapshot, Settings);
        }
    }
}
