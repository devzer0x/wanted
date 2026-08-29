using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using Newtonsoft.Json.Linq;

namespace WastedBridge.OfflineChecks
{
    /// <summary>
    /// Builds real <c>WastedBridge.Snapshot</c> object graphs out of the compiled assembly,
    /// serializes them with the bridge's own <c>SnapshotJson.Serialize</c> — the exact method the
    /// game thread publishes <c>/state</c> with — checks the result against every documented
    /// CONTRACTS §1 field, and writes the JSON to bridge/contract-samples/.
    ///
    /// The object graphs are assembled here rather than read off a running game (natives cannot
    /// run on a dev machine), so the *values* are synthetic. The *shape* is not: it comes from the
    /// DTOs and the serializer settings inside WastedBridge.dll, which is what the harness's
    /// models have to agree with.
    /// </summary>
    internal static class ContractSamples
    {
        /// <summary>The fresh-load document, kept so other sections can serve the real bytes.</summary>
        public static string FreshLoadJson { get; private set; }

        public static void Run()
        {
            Directory.CreateDirectory(Paths.ContractSamplesDir);
            string bridgeVersion = BridgeMetadata.ReadConstString(
                "WastedBridge.WastedBridgeScript", "BridgeVersion");
            Checks.Report(
                System.Text.RegularExpressions.Regex.IsMatch(bridgeVersion, @"^\d+\.\d+\.\d+$"),
                "[state] bridge.version read from the compiled assembly's BridgeVersion const",
                bridgeVersion);

            string fresh = BuildFreshLoad(bridgeVersion);
            FreshLoadJson = fresh;
            string populated = BuildPopulated(bridgeVersion);

            ValidateState("fresh-load", fresh);
            ValidateState("populated", populated);

            FreshLoadContractRules(fresh);
            PopulatedContractRules(populated);

            Write("state-fresh-load.json", fresh);
            Write("state-populated.json", populated);
        }

        // ---- snapshot construction ----------------------------------------------------------

        /// <summary>
        /// What the harness meets on its very first poll of a session: the bridge has ticked once
        /// with a live player ped, nothing has been asked of it yet, the player is on foot, and
        /// edition detection has not completed. This is the document whose last_task.id/type nulls
        /// crashed the harness before CONTRACTS v1.2 spelled them out.
        /// </summary>
        private static string BuildFreshLoad(string bridgeVersion)
        {
            object snap = BridgeAssembly.New("Snapshot");
            BridgeAssembly.SetField(snap, "Ts", "2026-08-29T09:14:03.221Z");
            BridgeAssembly.SetField(snap, "Tick", 1L);
            BridgeAssembly.SetField(snap, "Player", Player(
                0f, 0f, 0f, 0f, 200, 200, 0, 0, 0, false, false, false, true));
            BridgeAssembly.SetField(snap, "Vehicle", null);
            BridgeAssembly.SetField(snap, "Location", Location("Vinewood Blvd", "Downtown Vinewood"));
            BridgeAssembly.SetField(snap, "World", World("13:45", "CLEAR", 1f));
            BridgeAssembly.SetField(snap, "Mission", Mission(false, false, false, null));
            BridgeAssembly.SetField(snap, "Nearby", BridgeAssembly.New("NearbyDto"));

            // The real thing: a TaskEngine that has never been given a task, asked for its DTO.
            object engine = BridgeAssembly.New("TaskEngine");
            object lastTask = BridgeAssembly.CallInstance(engine, "ToDto");
            BridgeAssembly.SetField(snap, "LastTask", lastTask);

            BridgeAssembly.SetField(snap, "Bridge", BridgeInfo(bridgeVersion, "unknown"));
            return Serialize(snap);
        }

        /// <summary>
        /// Every optional branch of the document populated at once: in a vehicle, a nearby vehicle
        /// and ped, a mission with an objective blip, and a running task.
        /// </summary>
        private static string BuildPopulated(string bridgeVersion)
        {
            object snap = BridgeAssembly.New("Snapshot");
            BridgeAssembly.SetField(snap, "Ts", "2026-08-29T09:41:57.008Z");
            BridgeAssembly.SetField(snap, "Tick", 123456L);
            BridgeAssembly.SetField(snap, "Player", Player(
                -1037.42f, -2737.91f, 20.17f, 328.4f, 164, 200, 45, 2, 4871, false, false, true, true));

            object veh = BridgeAssembly.New("VehicleDto");
            BridgeAssembly.SetField(veh, "Handle", 197121);
            BridgeAssembly.SetField(veh, "Model", "adder");
            BridgeAssembly.SetField(veh, "DisplayName", "Adder");
            BridgeAssembly.SetField(veh, "Class", "Super");
            BridgeAssembly.SetField(veh, "Speed", 41.2f);
            BridgeAssembly.SetField(veh, "Health", 980f);
            BridgeAssembly.SetField(veh, "UpsideDown", false);
            BridgeAssembly.SetField(veh, "InWater", false);
            BridgeAssembly.SetField(veh, "StoppedForS", 0f);
            BridgeAssembly.SetField(snap, "Vehicle", veh);

            BridgeAssembly.SetField(snap, "Location", Location("Los Santos Fwy", "El Burro Heights"));
            BridgeAssembly.SetField(snap, "World", World("21:07", "RAIN", 0.15f));

            object blip = BridgeAssembly.New("ObjectiveBlipDto");
            BridgeAssembly.SetField(blip, "Pos", Vec3(-75.02f, -818.44f, 326.18f));
            BridgeAssembly.SetField(blip, "Kind", "coord");
            BridgeAssembly.SetField(blip, "Handle", 42);
            BridgeAssembly.SetField(snap, "Mission", Mission(true, false, false, blip));

            object nearby = BridgeAssembly.New("NearbyDto");
            object nv = BridgeAssembly.New("NearbyVehicleDto");
            BridgeAssembly.SetField(nv, "Handle", 200705);
            BridgeAssembly.SetField(nv, "Model", "police3");
            BridgeAssembly.SetField(nv, "DisplayName", "Police Cruiser");
            BridgeAssembly.SetField(nv, "Class", "Emergency");
            BridgeAssembly.SetField(nv, "Distance", 18.44f);
            BridgeAssembly.SetField(nv, "Driver", "npc");
            AddToList(nearby, "Vehicles", nv);

            object np = BridgeAssembly.New("NearbyPedDto");
            BridgeAssembly.SetField(np, "Handle", 198657);
            BridgeAssembly.SetField(np, "Model", "s_m_y_cop_01");
            BridgeAssembly.SetField(np, "Distance", 19.02f);
            BridgeAssembly.SetField(np, "Relationship", "hostile");
            AddToList(nearby, "Peds", np);
            BridgeAssembly.SetField(snap, "Nearby", nearby);

            // A running task: TaskEngine.Start() needs natives, so the DTO is built directly. The
            // fresh-load sample above covers the engine's own idle output.
            object lastTask = BridgeAssembly.New("LastTaskDto");
            BridgeAssembly.SetField(lastTask, "Id", "t-000123");
            BridgeAssembly.SetField(lastTask, "Type", "drive_to");
            BridgeAssembly.SetField(lastTask, "Status", "running");
            BridgeAssembly.SetField(lastTask, "Detail", "");
            BridgeAssembly.SetField(snap, "LastTask", lastTask);

            BridgeAssembly.SetField(snap, "Bridge", BridgeInfo(bridgeVersion, "legacy"));
            return Serialize(snap);
        }

        // ---- assertions ----------------------------------------------------------------------

        private static void ValidateState(string label, string json)
        {
            JObject doc = StateSchema.Parse(json);
            List<string> problems = StateSchema.Validate(doc, StateSchema.State);
            Checks.Report(problems.Count == 0,
                "[state] " + label + ": every documented CONTRACTS §1 field present with the "
                + "documented JSON type",
                problems.Count == 0
                    ? StateSchema.State.Count + " field specs satisfied"
                    : string.Join(" | ", problems));

            List<string> extra = StateSchema.Undocumented(doc, StateSchema.State);
            Checks.Report(extra.Count == 0,
                "[state] " + label + ": no undocumented keys",
                extra.Count == 0 ? "none" : string.Join(", ", extra));
        }

        private static void FreshLoadContractRules(string json)
        {
            JObject doc = StateSchema.Parse(json);
            JObject task = (JObject)doc["last_task"];

            Checks.Report(task.Property("id") != null && task["id"].Type == JTokenType.Null,
                "[state] fresh-load: last_task.id is present and null (CONTRACTS v1.2)",
                Describe(task, "id"));
            Checks.Report(task.Property("type") != null && task["type"].Type == JTokenType.Null,
                "[state] fresh-load: last_task.type is present and null (CONTRACTS v1.2)",
                Describe(task, "type"));
            Checks.Report((string)task["status"] == "idle",
                "[state] fresh-load: last_task.status is \"idle\"", (string)task["status"]);
            Checks.Report(task.Property("detail") != null && (string)task["detail"] == "",
                "[state] fresh-load: last_task.detail is present and empty",
                Describe(task, "detail"));
            Checks.Report(json.Contains("\"last_task\":{\"id\":null,\"type\":null,"),
                "[state] fresh-load: serialized bytes carry the nulls (NullValueHandling.Include)",
                json.Substring(json.IndexOf("\"last_task\"", StringComparison.Ordinal)));

            Checks.Report(doc.Property("vehicle") != null && doc["vehicle"].Type == JTokenType.Null,
                "[state] fresh-load: vehicle is present and null when on foot",
                Describe(doc, "vehicle"));
            var mission = (JObject)doc["mission"];
            Checks.Report(mission.Property("objective_blip") != null
                          && mission["objective_blip"].Type == JTokenType.Null,
                "[state] fresh-load: mission.objective_blip is present and null",
                Describe(mission, "objective_blip"));
            Checks.Report((string)doc["bridge"]["edition"] == "unknown",
                "[state] fresh-load: bridge.edition may be \"unknown\" (CONTRACTS v1.2)",
                (string)doc["bridge"]["edition"]);
        }

        private static void PopulatedContractRules(string json)
        {
            JObject doc = StateSchema.Parse(json);
            Checks.Report(doc["vehicle"].Type == JTokenType.Object
                          && (string)doc["vehicle"]["model"] == "adder",
                "[state] populated: vehicle object is emitted when in a vehicle",
                doc["vehicle"].ToString(Newtonsoft.Json.Formatting.None));
            Checks.Report(((JArray)doc["nearby"]["vehicles"]).Count == 1
                          && ((JArray)doc["nearby"]["peds"]).Count == 1,
                "[state] populated: nearby.vehicles/peds carry element objects",
                doc["nearby"].ToString(Newtonsoft.Json.Formatting.None));
            Checks.Report(doc["mission"]["objective_blip"].Type == JTokenType.Object,
                "[state] populated: mission.objective_blip object is emitted",
                doc["mission"]["objective_blip"].ToString(Newtonsoft.Json.Formatting.None));
            Checks.Report((string)doc["last_task"]["id"] == "t-000123"
                          && (string)doc["last_task"]["type"] == "drive_to"
                          && (string)doc["last_task"]["status"] == "running",
                "[state] populated: last_task carries id/type/status once a task exists",
                doc["last_task"].ToString(Newtonsoft.Json.Formatting.None));
            Checks.Report((string)doc["bridge"]["edition"] == "legacy",
                "[state] populated: bridge.edition is \"legacy\" once detection has run",
                (string)doc["bridge"]["edition"]);
        }

        // ---- helpers -------------------------------------------------------------------------

        private static string Serialize(object snapshot)
        {
            // The bridge's own publish path, called on the compiled assembly.
            return (string)BridgeAssembly.CallStatic("SnapshotJson", "Serialize", snapshot);
        }

        private static void Write(string name, string json)
        {
            string path = Path.Combine(Paths.ContractSamplesDir, name);
            // Written verbatim: the file is byte-for-byte the HTTP response body, no reformatting
            // and no trailing newline.
            File.WriteAllText(path, json);
            Checks.Report(File.Exists(path) && File.ReadAllText(path) == json,
                "[samples] wrote " + name, path + " (" + json.Length + " bytes)");
        }

        private static string Describe(JObject obj, string name)
        {
            return obj.Property(name) == null
                ? "<key absent>"
                : name + " = " + obj[name].ToString(Newtonsoft.Json.Formatting.None);
        }

        private static void AddToList(object owner, string field, object item)
        {
            FieldInfo f = owner.GetType().GetField(field,
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            object list = f.GetValue(owner);
            list.GetType().GetMethod("Add").Invoke(list, new[] { item });
        }

        private static object Vec3(float x, float y, float z)
        {
            object v = BridgeAssembly.New("Vec3Dto");
            BridgeAssembly.SetField(v, "X", x);
            BridgeAssembly.SetField(v, "Y", y);
            BridgeAssembly.SetField(v, "Z", z);
            return v;
        }

        private static object Player(float x, float y, float z, float heading, int health,
                                     int maxHealth, int armor, int wanted, int cash, bool dead,
                                     bool arrested, bool inVehicle, bool controlEnabled)
        {
            object p = BridgeAssembly.New("PlayerDto");
            BridgeAssembly.SetField(p, "Pos", Vec3(x, y, z));
            BridgeAssembly.SetField(p, "Heading", heading);
            BridgeAssembly.SetField(p, "Health", health);
            BridgeAssembly.SetField(p, "MaxHealth", maxHealth);
            BridgeAssembly.SetField(p, "Armor", armor);
            BridgeAssembly.SetField(p, "Wanted", wanted);
            BridgeAssembly.SetField(p, "Cash", cash);
            BridgeAssembly.SetField(p, "Dead", dead);
            BridgeAssembly.SetField(p, "Arrested", arrested);
            BridgeAssembly.SetField(p, "InVehicle", inVehicle);
            BridgeAssembly.SetField(p, "ControlEnabled", controlEnabled);
            return p;
        }

        private static object Location(string street, string zone)
        {
            object l = BridgeAssembly.New("LocationDto");
            BridgeAssembly.SetField(l, "Street", street);
            BridgeAssembly.SetField(l, "Zone", zone);
            return l;
        }

        private static object World(string clock, string weather, float timescale)
        {
            object w = BridgeAssembly.New("WorldDto");
            BridgeAssembly.SetField(w, "Clock", clock);
            BridgeAssembly.SetField(w, "Weather", weather);
            BridgeAssembly.SetField(w, "Timescale", timescale);
            return w;
        }

        private static object Mission(bool active, bool randomEvent, bool cutscene, object blip)
        {
            object m = BridgeAssembly.New("MissionDto");
            BridgeAssembly.SetField(m, "Active", active);
            BridgeAssembly.SetField(m, "RandomEventActive", randomEvent);
            BridgeAssembly.SetField(m, "CutsceneActive", cutscene);
            BridgeAssembly.SetField(m, "ObjectiveBlip", blip);
            return m;
        }

        private static object BridgeInfo(string version, string edition)
        {
            object b = BridgeAssembly.New("BridgeInfoDto");
            BridgeAssembly.SetField(b, "Version", version);
            BridgeAssembly.SetField(b, "Edition", edition);
            return b;
        }

        public static string Iso(DateTime utc)
        {
            return utc.ToString("yyyy-MM-ddTHH:mm:ss.fffZ", CultureInfo.InvariantCulture);
        }
    }
}
