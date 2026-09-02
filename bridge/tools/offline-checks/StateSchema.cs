using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace WastedBridge.OfflineChecks
{
    internal enum Kind
    {
        Object,
        Array,
        String,
        Integer,
        Number,
        Boolean
    }

    /// <summary>One documented field: where it lives, what JSON type it must have.</summary>
    internal sealed class FieldSpec
    {
        public string Path;
        public Kind Kind;
        public bool Nullable;
        public string[] Allowed;
        public string Pattern;

        public FieldSpec(string path, Kind kind, bool nullable = false,
                         string[] allowed = null, string pattern = null)
        {
            Path = path;
            Kind = kind;
            Nullable = nullable;
            Allowed = allowed;
            Pattern = pattern;
        }
    }

    /// <summary>
    /// CONTRACTS.md §1 GET /state and GET /health, transcribed field by field. This is the table
    /// the 2026-08-29 verification pass found missing: the offline checks covered routing and
    /// transport but never looked at a serialized snapshot, which is how the nullable
    /// last_task.id/type mismatch survived to delivery day.
    /// </summary>
    internal static class StateSchema
    {
        public static readonly string TsPattern = @"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$";

        public static readonly List<FieldSpec> State = new List<FieldSpec>
        {
            new FieldSpec("ts", Kind.String, pattern: @"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"),
            new FieldSpec("tick", Kind.Integer),

            new FieldSpec("player", Kind.Object),
            new FieldSpec("player.pos", Kind.Object),
            new FieldSpec("player.pos.x", Kind.Number),
            new FieldSpec("player.pos.y", Kind.Number),
            new FieldSpec("player.pos.z", Kind.Number),
            new FieldSpec("player.heading", Kind.Number),
            new FieldSpec("player.health", Kind.Integer),
            new FieldSpec("player.max_health", Kind.Integer),
            new FieldSpec("player.armor", Kind.Integer),
            new FieldSpec("player.wanted", Kind.Integer),
            new FieldSpec("player.cash", Kind.Integer),
            new FieldSpec("player.dead", Kind.Boolean),
            new FieldSpec("player.arrested", Kind.Boolean),
            new FieldSpec("player.in_vehicle", Kind.Boolean),
            new FieldSpec("player.control_enabled", Kind.Boolean),
            // CONTRACTS v1.7: who the player currently is, from the ped model. Always present
            // (defaults to "unknown" rather than being absent/null).
            new FieldSpec("player.protagonist", Kind.String,
                allowed: new[] { "michael", "franklin", "trevor", "unknown" }),
            // CONTRACTS v1.11: IS_PLAYER_SWITCH_IN_PROGRESS - true only while the camera is
            // mid-flight between protagonists. Always present.
            new FieldSpec("player.switch_in_progress", Kind.Boolean),

            // null when on foot — the key is always present (contract example shows "vehicle": null)
            new FieldSpec("vehicle", Kind.Object, nullable: true),
            new FieldSpec("vehicle.handle", Kind.Integer),
            new FieldSpec("vehicle.model", Kind.String),
            new FieldSpec("vehicle.display_name", Kind.String),
            new FieldSpec("vehicle.class", Kind.String),
            new FieldSpec("vehicle.speed", Kind.Number),
            new FieldSpec("vehicle.health", Kind.Number),
            new FieldSpec("vehicle.upside_down", Kind.Boolean),
            new FieldSpec("vehicle.in_water", Kind.Boolean),
            new FieldSpec("vehicle.stopped_for_s", Kind.Number),

            new FieldSpec("location", Kind.Object),
            new FieldSpec("location.street", Kind.String),
            new FieldSpec("location.zone", Kind.String),

            new FieldSpec("world", Kind.Object),
            new FieldSpec("world.clock", Kind.String, pattern: @"^\d{2}:\d{2}$"),
            new FieldSpec("world.weather", Kind.String),
            new FieldSpec("world.timescale", Kind.Number),

            new FieldSpec("mission", Kind.Object),
            new FieldSpec("mission.active", Kind.Boolean),
            new FieldSpec("mission.random_event_active", Kind.Boolean),
            new FieldSpec("mission.cutscene_active", Kind.Boolean),
            new FieldSpec("mission.objective_blip", Kind.Object, nullable: true),
            new FieldSpec("mission.objective_blip.pos", Kind.Object),
            new FieldSpec("mission.objective_blip.pos.x", Kind.Number),
            new FieldSpec("mission.objective_blip.pos.y", Kind.Number),
            new FieldSpec("mission.objective_blip.pos.z", Kind.Number),
            new FieldSpec("mission.objective_blip.kind", Kind.String,
                allowed: new[] { "coord", "entity" }),
            new FieldSpec("mission.objective_blip.handle", Kind.Integer),
            // CONTRACTS v1.7: mission-start markers (the M/F/T letter blips) currently on the map,
            // nearest first, at most 8. Always an array, never null.
            new FieldSpec("mission.starts", Kind.Array),
            new FieldSpec("mission.starts[]", Kind.Object),
            new FieldSpec("mission.starts[].pos", Kind.Object),
            new FieldSpec("mission.starts[].pos.x", Kind.Number),
            new FieldSpec("mission.starts[].pos.y", Kind.Number),
            new FieldSpec("mission.starts[].pos.z", Kind.Number),
            new FieldSpec("mission.starts[].protagonist", Kind.String,
                allowed: new[] { "michael", "franklin", "trevor", "unknown" }),
            // CONTRACTS v1.8: the route-enabled blips objective_blip was picked from, nearest
            // first, at most 5. Always an array, never null.
            new FieldSpec("mission.route_blips", Kind.Array),
            new FieldSpec("mission.route_blips[]", Kind.Object),
            new FieldSpec("mission.route_blips[].pos", Kind.Object),
            new FieldSpec("mission.route_blips[].pos.x", Kind.Number),
            new FieldSpec("mission.route_blips[].pos.y", Kind.Number),
            new FieldSpec("mission.route_blips[].pos.z", Kind.Number),
            new FieldSpec("mission.route_blips[].kind", Kind.String,
                allowed: new[] { "coord", "entity" }),
            new FieldSpec("mission.route_blips[].handle", Kind.Integer),
            new FieldSpec("mission.route_blips[].color", Kind.String),
            // CONTRACTS v1.10 item 3: the active story-mission script name, or null. Only ever
            // non-null while mission.active.
            new FieldSpec("mission.script", Kind.String, nullable: true),
            // CONTRACTS v1.11: true while a "mission_repeat_controller" script thread is running.
            new FieldSpec("mission.retry_in_flight", Kind.Boolean),
            // CONTRACTS v1.11: blips pinned to a ped/vehicle entity, nearest first, at most 8.
            // Always an array, never null (route requirement dropped vs. route_blips - see
            // EntityBlipDto).
            new FieldSpec("mission.entity_blips", Kind.Array),
            new FieldSpec("mission.entity_blips[]", Kind.Object),
            new FieldSpec("mission.entity_blips[].pos", Kind.Object),
            new FieldSpec("mission.entity_blips[].pos.x", Kind.Number),
            new FieldSpec("mission.entity_blips[].pos.y", Kind.Number),
            new FieldSpec("mission.entity_blips[].pos.z", Kind.Number),
            new FieldSpec("mission.entity_blips[].handle", Kind.Integer),
            new FieldSpec("mission.entity_blips[].color", Kind.String),
            new FieldSpec("mission.entity_blips[].is_route", Kind.Boolean),
            new FieldSpec("mission.entity_blips[].distance", Kind.Number),
            // CONTRACTS v1.11: Blip.GetAppropriateName() - the map-legend text, or null when
            // empty/unavailable.
            new FieldSpec("mission.entity_blips[].name", Kind.String, nullable: true),

            new FieldSpec("nearby", Kind.Object),
            new FieldSpec("nearby.vehicles", Kind.Array),
            new FieldSpec("nearby.vehicles[]", Kind.Object),
            new FieldSpec("nearby.vehicles[].handle", Kind.Integer),
            new FieldSpec("nearby.vehicles[].model", Kind.String),
            new FieldSpec("nearby.vehicles[].display_name", Kind.String),
            new FieldSpec("nearby.vehicles[].class", Kind.String),
            new FieldSpec("nearby.vehicles[].distance", Kind.Number),
            new FieldSpec("nearby.vehicles[].driver", Kind.String,
                allowed: new[] { "player", "npc", "empty" }),
            // CONTRACTS v1.6: world position, same shape as player.pos.
            new FieldSpec("nearby.vehicles[].pos", Kind.Object),
            new FieldSpec("nearby.vehicles[].pos.x", Kind.Number),
            new FieldSpec("nearby.vehicles[].pos.y", Kind.Number),
            new FieldSpec("nearby.vehicles[].pos.z", Kind.Number),
            new FieldSpec("nearby.peds", Kind.Array),
            new FieldSpec("nearby.peds[]", Kind.Object),
            new FieldSpec("nearby.peds[].handle", Kind.Integer),
            new FieldSpec("nearby.peds[].model", Kind.String),
            new FieldSpec("nearby.peds[].distance", Kind.Number),
            // CONTRACTS v1.5: gains "friendly" (the engine's own Companion/Like/Respect
            // relationship towards the player - mission crewmates).
            new FieldSpec("nearby.peds[].relationship", Kind.String,
                allowed: new[] { "neutral", "hostile", "friendly" }),
            // CONTRACTS v1.6: world position, same shape as player.pos.
            new FieldSpec("nearby.peds[].pos", Kind.Object),
            new FieldSpec("nearby.peds[].pos.x", Kind.Number),
            new FieldSpec("nearby.peds[].pos.y", Kind.Number),
            new FieldSpec("nearby.peds[].pos.z", Kind.Number),
            // CONTRACTS v1.10 item 2: the vehicle handle this ped is seated in, or null on foot.
            new FieldSpec("nearby.peds[].in_vehicle_handle", Kind.Integer, nullable: true),
            // CONTRACTS v1.11: true if this ped's melee-target is the player, it is already tasked
            // in combat against the player, or it has damaged the player this tick.
            new FieldSpec("nearby.peds[].attacking_me", Kind.Boolean),
            // CONTRACTS v1.11: IS_PED_ARMED classification of this ped's CURRENT weapon.
            new FieldSpec("nearby.peds[].weapon_class", Kind.String,
                allowed: new[] { "unarmed", "melee", "gun", "projectile", "unknown" }),

            // CONTRACTS v1.11: the reflex-relevant "who is hurting me right now" summary. Always a
            // present object; the two fields inside are independently nullable.
            new FieldSpec("threat", Kind.Object),
            new FieldSpec("threat.attacker_handle", Kind.Integer, nullable: true),
            new FieldSpec("threat.being_jacked_by", Kind.Integer, nullable: true),

            new FieldSpec("last_task", Kind.Object),
            // CONTRACTS v1.2: present-and-null before the first task, never absent.
            new FieldSpec("last_task.id", Kind.String, nullable: true),
            new FieldSpec("last_task.type", Kind.String, nullable: true),
            new FieldSpec("last_task.status", Kind.String,
                allowed: new[] { "idle", "running", "done", "failed" }),
            new FieldSpec("last_task.detail", Kind.String),

            new FieldSpec("bridge", Kind.Object),
            new FieldSpec("bridge.version", Kind.String),
            // CONTRACTS v1.2 added "unknown" to this enum.
            new FieldSpec("bridge.edition", Kind.String,
                allowed: new[] { "legacy", "enhanced", "unknown" })
        };

        public static readonly List<FieldSpec> Health = new List<FieldSpec>
        {
            new FieldSpec("version", Kind.String),
            new FieldSpec("edition", Kind.String,
                allowed: new[] { "legacy", "enhanced", "unknown" }),
            new FieldSpec("tick_hz", Kind.Number),
            new FieldSpec("queue_depth", Kind.Integer),
            new FieldSpec("game_fps", Kind.Number),
            new FieldSpec("online_blocked", Kind.Boolean)
        };

        /// <summary>CONTRACTS conventions: every error body is {"error": snake, "detail": text}.</summary>
        public static readonly List<FieldSpec> Error = new List<FieldSpec>
        {
            new FieldSpec("error", Kind.String, pattern: "^[a-z][a-z0-9_]*$"),
            new FieldSpec("detail", Kind.String)
        };

        /// <summary>
        /// Parses a document the way a non-.NET consumer sees it. JObject.Parse defaults to
        /// DateParseHandling.Auto, which silently turns the ISO-8601 "ts" string into a Date token
        /// — an artifact of Newtonsoft's reader, not of the payload. Python's json (what the
        /// harness uses) does no such thing, so the checks must not either.
        /// </summary>
        public static JObject Parse(string json)
        {
            using (var reader = new JsonTextReader(new StringReader(json)))
            {
                reader.DateParseHandling = DateParseHandling.None;
                reader.FloatParseHandling = FloatParseHandling.Double;
                return JObject.Load(reader);
            }
        }

        /// <summary>
        /// Validates a document against a spec list. Returns one line per problem; an empty list
        /// means every documented field is present with the documented JSON type.
        /// </summary>
        public static List<string> Validate(JToken root, List<FieldSpec> specs)
        {
            var problems = new List<string>();
            var nullPrefixes = new List<string>();

            foreach (FieldSpec spec in specs)
            {
                if (IsUnderNull(spec.Path, nullPrefixes))
                {
                    continue;
                }
                foreach (Located loc in Locate(root, spec.Path))
                {
                    if (loc.Missing)
                    {
                        problems.Add(loc.Path + ": key is absent (contract requires it"
                                     + (spec.Nullable ? ", value may be null)" : ")"));
                        continue;
                    }
                    JToken t = loc.Token;
                    if (t.Type == JTokenType.Null)
                    {
                        if (!spec.Nullable)
                        {
                            problems.Add(loc.Path + ": null, but the contract does not allow null");
                        }
                        else
                        {
                            nullPrefixes.Add(loc.Path);
                        }
                        continue;
                    }
                    if (!Matches(spec.Kind, t.Type))
                    {
                        problems.Add(loc.Path + ": expected " + spec.Kind + ", got " + t.Type);
                        continue;
                    }
                    if (spec.Allowed != null
                        && Array.IndexOf(spec.Allowed, (string)t) < 0)
                    {
                        problems.Add(loc.Path + ": \"" + (string)t + "\" is not one of "
                                     + string.Join("|", spec.Allowed));
                    }
                    if (spec.Pattern != null && !Regex.IsMatch((string)t, spec.Pattern))
                    {
                        problems.Add(loc.Path + ": \"" + (string)t + "\" does not match "
                                     + spec.Pattern);
                    }
                }
            }
            return problems;
        }

        /// <summary>Keys present in the document that CONTRACTS §1 does not document.</summary>
        public static List<string> Undocumented(JToken root, List<FieldSpec> specs)
        {
            var documented = new HashSet<string>();
            foreach (FieldSpec s in specs)
            {
                documented.Add(s.Path);
            }
            var found = new List<string>();
            Walk(root, "", documented, found);
            return found;
        }

        private static void Walk(JToken node, string path, HashSet<string> documented,
                                 List<string> found)
        {
            if (node is JObject obj)
            {
                foreach (JProperty p in obj.Properties())
                {
                    string child = path.Length == 0 ? p.Name : path + "." + p.Name;
                    if (!documented.Contains(child))
                    {
                        found.Add(child);
                        continue; // do not descend into something the contract never mentions
                    }
                    Walk(p.Value, child, documented, found);
                }
            }
            else if (node is JArray arr)
            {
                foreach (JToken item in arr)
                {
                    Walk(item, path + "[]", documented, found);
                }
            }
        }

        private static bool Matches(Kind kind, JTokenType type)
        {
            switch (kind)
            {
                case Kind.Object: return type == JTokenType.Object;
                case Kind.Array: return type == JTokenType.Array;
                case Kind.String: return type == JTokenType.String;
                case Kind.Integer: return type == JTokenType.Integer;
                // JSON has one number type; a float that happens to be integral may serialize
                // either way, so both are accepted where the contract shows a float.
                case Kind.Number: return type == JTokenType.Float || type == JTokenType.Integer;
                case Kind.Boolean: return type == JTokenType.Boolean;
                default: return false;
            }
        }

        private static bool IsUnderNull(string path, List<string> nullPrefixes)
        {
            foreach (string prefix in nullPrefixes)
            {
                if (path.StartsWith(prefix + ".", StringComparison.Ordinal))
                {
                    return true;
                }
            }
            return false;
        }

        private struct Located
        {
            public string Path;
            public JToken Token;
            public bool Missing;
        }

        /// <summary>
        /// Resolves a spec path against the document, expanding "[]" over array elements.
        /// A path under an empty array simply yields nothing.
        /// </summary>
        private static List<Located> Locate(JToken root, string path)
        {
            string[] segments = path.Replace("[]", ".[]").Split('.');
            var results = new List<Located>();
            Descend(root, "", segments, 0, results);
            return results;
        }

        private static void Descend(JToken node, string nodePath, string[] segments, int index,
                                    List<Located> results)
        {
            if (index == segments.Length)
            {
                results.Add(new Located { Path = nodePath, Token = node });
                return;
            }
            string seg = segments[index];
            if (seg.Length == 0)
            {
                Descend(node, nodePath, segments, index + 1, results);
                return;
            }
            if (seg == "[]")
            {
                var arr = node as JArray;
                if (arr == null)
                {
                    return; // the array itself failed its own check already
                }
                for (int i = 0; i < arr.Count; i++)
                {
                    Descend(arr[i], nodePath + "[" + i + "]", segments, index + 1, results);
                }
                return;
            }
            var obj = node as JObject;
            if (obj == null)
            {
                return;
            }
            string childPath = nodePath.Length == 0 ? seg : nodePath + "." + seg;
            JProperty prop = obj.Property(seg);
            if (prop == null)
            {
                results.Add(new Located { Path = childPath, Missing = true });
                return;
            }
            Descend(prop.Value, childPath, segments, index + 1, results);
        }
    }
}
