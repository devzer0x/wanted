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
        // v1.11: IS_PLAYER_SWITCH_IN_PROGRESS - true while the camera is mid-flight between
        // protagonists. The real signal behind "wrong body / waiting for the switch" commentary.
        [JsonProperty("switch_in_progress")] public bool SwitchInProgress;
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

    /// <summary>
    /// CONTRACTS v1.11: a blip ATTACHED TO AN ENTITY (a ped or a vehicle), whether or not the game
    /// has plotted a route to it. This is the blue dot a follow mission puts on a crewmate.
    ///
    /// Why it exists, observed live 2026-09-02: `nearby.peds[]` reaches ~50 m, so the moment the
    /// crewmate being tailed drove off he vanished from /state entirely and the agent went looking for
    /// him by driving around at random - while the game was drawing his position on the minimap the
    /// whole time. `route_blips[]` did not cover it: that list is route-enabled blips only, and a
    /// plain blue crew dot usually has no route. Entity attachment is a FACT (the blip is pinned to
    /// a specific entity) rather than a colour convention, which is the same reasoning
    /// `objective_blip` already uses for routes.
    /// </summary>
    internal sealed class EntityBlipDto
    {
        [JsonProperty("pos")] public Vec3Dto Pos;
        [JsonProperty("handle")] public int Handle;      // the ENTITY handle, never the blip's
        [JsonProperty("color")] public string Color;     // BlipColor member name, or its integer
        [JsonProperty("is_route")] public bool IsRoute;  // the game is also drawing a route to it
        [JsonProperty("distance")] public float Distance;
        // v1.11: Blip.GetAppropriateName() - the map-legend text ("Lamar", "Objective"), or null
        // when empty/unavailable. Turns an anonymous dot into a name with no OCR.
        [JsonProperty("name")] public string Name;
    }

    internal sealed class MissionDto
    {
        [JsonProperty("active")] public bool Active;
        [JsonProperty("random_event_active")] public bool RandomEventActive;
        [JsonProperty("cutscene_active")] public bool CutsceneActive;
        [JsonProperty("objective_blip")] public ObjectiveBlipDto ObjectiveBlip;
        // v1.7: M/F/T markers on the map. Contract: "always present as an array, never null" —
        // defaulted here the same way route_blips is, so a builder that forgets to set it (e.g. a
        // synthetic/off-server sample) still serializes the documented shape rather than null.
        [JsonProperty("starts")] public List<MissionStartDto> Starts = new List<MissionStartDto>();
        // v1.8: route-enabled blips, nearest first, at most 5. Initialized so the key is always an
        // array — never null — even if a caller forgets to set it.
        [JsonProperty("route_blips")] public List<RouteBlipDto> RouteBlips = new List<RouteBlipDto>();
        // v1.10: the active story-mission script name (e.g. "armenian1"), or null. Only ever
        // non-null while "active" is true.
        [JsonProperty("script")] public string Script;
        // v1.11: blips pinned to a ped/vehicle, nearest first, at most 8. Always an array.
        [JsonProperty("entity_blips")] public List<EntityBlipDto> EntityBlips = new List<EntityBlipDto>();
        // v1.11: true while a "mission_repeat_controller" script thread is running (the
        // checkpoint-reload/retry executor - see docs/research/brief-mission-comprehension.json).
        // Detected from the same script-thread walk mission.script already runs.
        [JsonProperty("retry_in_flight")] public bool RetryInFlight;
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
        // v1.10: the vehicle handle this ped is currently seated in, or null on foot.
        [JsonProperty("in_vehicle_handle")] public int? InVehicleHandle;
        // v1.11: true if this ped's melee-target is the player, the game already has it tasked
        // in combat against the player, or it has damaged the player this tick (see
        // SnapshotBuilder's threat scan). True BEFORE the first punch lands where possible.
        [JsonProperty("attacking_me")] public bool AttackingMe;
        // v1.11: "unarmed"|"melee"|"gun"|"projectile"|"unknown" - IS_PED_ARMED bit classification
        // of this ped's CURRENT weapon. Drives fight_ped's melee-vs-ranged response.
        [JsonProperty("weapon_class")] public string WeaponClass;
    }

    internal sealed class NearbyDto
    {
        [JsonProperty("vehicles")] public List<NearbyVehicleDto> Vehicles = new List<NearbyVehicleDto>();
        [JsonProperty("peds")] public List<NearbyPedDto> Peds = new List<NearbyPedDto>();
    }

    /// <summary>
    /// CONTRACTS v1.11: the reflex-relevant "who is hurting me right now" summary, derived from
    /// the same per-tick threat scan that sets nearby.peds[].attacking_me. Root-caused fix for
    /// the 2026-09-02 live bug: a carjack victim, plausibly still Respect/Like toward the player,
    /// punched the agent to death because TASK_COMBAT_HATED_TARGETS_AROUND_PED silently no-ops
    /// without a Neutral/Dislike/Hate relationship. attacker_handle lets the harness target
    /// fight_ped explicitly instead of depending on the hated-relationship gate.
    /// </summary>
    internal sealed class ThreatDto
    {
        // Nearest ped with attacking_me true, or null. Always present as an object (never itself
        // null) - only the two fields inside are nullable.
        [JsonProperty("attacker_handle")] public int? AttackerHandle;
        // GET_PEDS_JACKER(player) while IS_PED_BEING_JACKED(player) is true, else null.
        [JsonProperty("being_jacked_by")] public int? BeingJackedBy;
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
        // v1.11: always a present object (see ThreatDto); defaulted so a builder that forgets to
        // set it still serializes the documented shape.
        [JsonProperty("threat")] public ThreatDto Threat = new ThreatDto();
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
