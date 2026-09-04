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
        // v1.12: the interior he is standing in, or null when he is outdoors. GROUND TRUTH, not a
        // guess - see InteriorDto.
        [JsonProperty("interior")] public InteriorDto Interior;
        // v1.12: where he was the last time he crossed from outdoors to indoors, or null if this
        // script has never seen him cross one. The escape's first move is "walk back to where you
        // came in", and only the bridge sees the tick BEFORE the door.
        [JsonProperty("last_outdoor")] public Vec3Dto LastOutdoor;

        // Bridge 1.7.0 (CONTRACTS proposal v1.14) — what he is carrying. Always a present
        // object, never null, so a consumer never has to null-check it (PhoneDto's rule).
        [JsonProperty("weapon")] public WeaponDto Weapon = new WeaponDto();
    }

    /// <summary>
    /// CONTRACTS v1.12 <c>player.interior</c>: which interior the player ped is inside, and for how
    /// long. Null (the whole object) when he is outdoors.
    ///
    /// WHY THIS FIELD EXISTS. After a mission ends, a respawn, or a character switch INSIDE a
    /// safehouse, outdoor navigation tasks fail - the nav mesh is disconnected by doors - and the agent
    /// stands in a living room doing nothing. The harness has had a house-escape ladder for a
    /// while, but /state exposed no way to distinguish "he is indoors" from "he is merely stuck",
    /// so the ladder could only be reached through a heuristic (a failed vehicle entry plus 20 s of
    /// stillness) that fires late and also fires on false positives. This is the fact instead.
    ///
    /// SOURCE: <c>GTA.Entity.CurrentInteriorProxy</c> (SHVDN wrapper, verified in the pinned
    /// nightly.189's own <c>lib/Docs/ScriptHookVDotNet3.xml</c>: "Gets the current interior proxy
    /// associated with this entity ... if they are in an interior; otherwise null") plus
    /// <c>GTA.InteriorProxy.Handle</c> for the id. A WRAPPER rather than a raw native hash on
    /// purpose: a future SHVDN bump that renames or removes it breaks the BUILD instead of
    /// silently returning a garbage id - the same reasoning <c>DrivingStyles</c> documents.
    /// </summary>
    internal sealed class InteriorDto
    {
        // The InteriorProxy handle. An opaque identity key: it is stable while the proxy lives and
        // is comparable between ticks, which is all the harness needs ("same room" vs "new room").
        // It is NOT a curated map id and must not be treated as one - no table of interior ids
        // ships with this bridge, because none has been verified against the real game.
        [JsonProperty("id")] public int Id;
        // Seconds since the tick this id last CHANGED, measured bridge-side. The harness polls at
        // 2-4 Hz and would round the transition; the bridge sees every tick.
        [JsonProperty("since_s")] public float SinceS;
    }

    /// <summary>
    /// Bridge 1.7.0 (CONTRACTS proposal v1.14). `player.weapon`: the weapon in his hands, plus
    /// how many rounds he has for each of the three the bridge tracks. `owned` is
    /// HAS_PED_GOT_WEAPON over the LOADOUT SET ONLY — a name that is missing means "not one of
    /// the three", never "he has nothing". `loadout` reports which prep mode the bridge is
    /// running ("off" by default; see WeaponState for why).
    /// </summary>
    internal sealed class WeaponDto
    {
        [JsonProperty("name")] public string Name = "Unarmed";
        [JsonProperty("class")] public string Class = "unarmed";
        [JsonProperty("ammo")] public int Ammo;
        [JsonProperty("owned")] public Dictionary<string, int> Owned = new Dictionary<string, int>();
        [JsonProperty("loadout")] public string Loadout = "off";
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

        // Bridge 1.7.0 (CONTRACTS proposal v1.14). IS_ENTITY_IN_AIR through the SHVDN
        // Entity.IsInAir wrapper (verified by IL: it pushes 0x886E37EC497200B6). The game's own
        // answer to "are the wheels off the ground" — the field that turns the harness's
        // `big_jump` from a declared-impossible goal into a read.
        [JsonProperty("in_air")] public bool InAir;

        // Bridge 1.7.0. "driver" | "passenger", from GET_PED_IN_VEHICLE_SEAT(veh, -1) == player.
        // A single native call rather than SHVDN's Ped.SeatIndex, which reads the ped's memory at
        // a hard-coded offset (verified by IL: MemDataMarshal.ReadByte) and would break silently
        // on a game patch. Two values, not a seat index: "which of the twelve back seats" is not
        // a question anything asks, and "is somebody else driving" is.
        [JsonProperty("seat")] public string Seat;
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

    /// <summary>
    /// CONTRACTS v1.13 <c>phone</c>: the cellphone, as two booleans.
    ///
    /// Exists because the operator watched Simeon call the agent on stream and the harness had no
    /// phone capability at all — the call simply rang out, unseen and unanswerable. Answering a
    /// STORY call starts a mission, so this is the field the "accept / reject a call" control is
    /// built on.
    ///
    /// Always a present object (never itself null); the two booleans inside carry the state. The
    /// derivation, the natives behind it and the reason the two are ANDed live on
    /// <see cref="PhoneState"/>.
    /// </summary>
    internal sealed class PhoneDto
    {
        // True while the phone is making incoming-call noise and nobody has picked up:
        // IS_PED_RINGTONE_PLAYING(player) AND NOT IS_MOBILE_PHONE_CALL_ONGOING().
        [JsonProperty("ringing")] public bool Ringing;
        // IS_MOBILE_PHONE_CALL_ONGOING() — a call is CONNECTED (incoming or outgoing).
        [JsonProperty("in_call")] public bool InCall;
    }

    /// <summary>
    /// Bridge 1.9.0 (CONTRACTS §1 proposal): <c>effects</c> — every cheat-driven effect the
    /// bridge currently has switched on, so `done_when`, the overlay and the commentary can
    /// all be honest about it (CLAUDE.md rule 5, "say so"). Always a present object, never
    /// null; each effect inside is always a present object too (PhoneDto's rule). An effect
    /// that is off reads `active: false` with an empty list, never a missing key.
    /// </summary>
    internal sealed class EffectsDto
    {
        [JsonProperty("arsenal")] public ArsenalEffectDto Arsenal = new ArsenalEffectDto();
    }

    /// <summary>
    /// <c>effects.arsenal</c>: the time-boxed cheat weapon layer (see <see cref="ArsenalState"/>).
    /// `expires_in_s` is the bridge's own TTL clock, which runs whether or not a harness is
    /// alive; `weapons` is the kit's `WeaponHash` member names while on (the same names that
    /// appear in `player.weapon.owned` for the duration); `last_cleared` is the reason the
    /// most recent bit ended (`ttl_expired`, `player_dead`, `goal_end`, ...) or "" if never.
    /// </summary>
    internal sealed class ArsenalEffectDto
    {
        [JsonProperty("active")] public bool Active;
        [JsonProperty("expires_in_s")] public float ExpiresInS;
        [JsonProperty("weapons")] public List<string> Weapons = new List<string>();
        // Name -> rounds GRANTED, while on ({} when off). The harness's "rounds gone" fold
        // seeds its baseline from this rather than from its first poll, so a shot fired
        // between the grant and the first 2-4 Hz snapshot is still counted.
        [JsonProperty("kit")] public Dictionary<string, int> Kit = new Dictionary<string, int>();
        [JsonProperty("last_cleared")] public string LastCleared = "";
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
        // v1.13: always a present object (see PhoneDto); defaulted the same way threat is, so a
        // builder that forgets to set it still serializes the documented shape rather than null.
        [JsonProperty("phone")] public PhoneDto Phone = new PhoneDto();
        // 1.9.0: always a present object (see EffectsDto); defaulted like phone/threat so a
        // builder that forgets to set it still serializes the documented shape.
        [JsonProperty("effects")] public EffectsDto Effects = new EffectsDto();
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
