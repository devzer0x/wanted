// Off-server checks for the WANTED bridge (no natives, no game). Loads the compiled net48
// WastedBridge.dll under .NET 8, drives its real routing/transport/serialization code, and
// exercises the CONTRACTS §1 surface over BOTH transports plus raw sockets. It also builds and
// serializes real Snapshot objects and writes bridge/contract-samples/*.json.
//
// What it CANNOT cover, by construction: anything that calls a native (the game thread's task
// engine, snapshot building, the online guard's flags), HTTP.SYS's real ACL behaviour on Windows,
// and SHVDN's loader. Those are server-only — bridge/README.md "Delivery day".
//
// It also substitutes a stub ScriptHookVDotNet3 assembly, because arm64 CoreCLR cannot load the
// real x64 SHVDN PE. The stub's enum values are generated from the real DLL's metadata; see
// StubGenerator.cs and the disclosure in bridge/README.md.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace WastedBridge.OfflineChecks
{
    internal static class Program
    {
        private static Type _sharedT;
        private static Type _serverT;
        private static Type _logT;
        private static Type _snapT;
        private static string _logPath;

        private static readonly List<JObject> ErrorCatalog = new List<JObject>();
        private static string _healthSample;

        private static async Task<int> Main(string[] args)
        {
            Paths.Resolve();
            string mode = args.Length > 0 ? args[0] : "all";

            switch (mode)
            {
                case "gen-stub":
                    return StubGenerator.Run(args);
                case "logfallback":
                    return LogFallbackMode();
                case "drift":
                    return DriftMode();
                default:
                    return await RunAll();
            }
        }

        // =====================================================================================
        // Sub-mode: the driving-style guard must FAIL against a renumbered SHVDN. Runs in its own
        // process because the stub and the drift stub share one assembly identity.
        // =====================================================================================
        private static int DriftMode()
        {
            BridgeAssembly.Load(Paths.StubDir("shvdn-stub-drift"));
            Type stylesT = BridgeAssembly.T("DrivingStyles");
            var drift = (string)BridgeAssembly.Call(stylesT, "Verify");
            bool ok = drift != null && drift.Contains("normal");
            Console.WriteLine((ok ? "PASS" : "FAIL")
                + "  [styles] DrivingStyles.Verify() reports drift against a renumbered SHVDN "
                + "assembly (the guard can fail)  -> " + (drift ?? "<null — the guard is a no-op>"));
            return ok ? 0 : 1;
        }

        // =====================================================================================
        // Sub-mode: BridgeLog falls back when the scripts directory is not writable. Its own
        // process because BridgeLog resolves its path once per load.
        // =====================================================================================
        private static int LogFallbackMode()
        {
            BridgeAssembly.Load(Paths.StubDir("shvdn-stub"));
            Type logT = BridgeAssembly.T("BridgeLog");
            string unwritable = "/System/wasted-bridge-should-not-exist"; // SIP-protected on macOS
            var resolved = (string)BridgeAssembly.Call(logT, "Init", unwritable);
            BridgeAssembly.Call(logT, "Info", "fallback probe line");
            bool ok = resolved != null && !resolved.StartsWith(unwritable)
                      && File.Exists(resolved)
                      && File.ReadAllText(resolved).Contains("fallback probe line");
            Console.WriteLine((ok ? "PASS" : "FAIL")
                + "  BridgeLog falls back off an unwritable scripts dir  -> " + (resolved ?? "<null>"));
            return ok ? 0 : 1;
        }

        // =====================================================================================
        private static async Task<int> RunAll()
        {
            Assembly asm = BridgeAssembly.Load(Paths.StubDir("shvdn-stub"));
            Console.WriteLine("bridge assembly : " + asm.GetName() + "  (" + Paths.CompiledBridgeDll + ")");
            Console.WriteLine("shvdn stub      : " + Paths.StubDir("shvdn-stub"));
            Console.WriteLine();

            _sharedT = BridgeAssembly.T("BridgeShared");
            _serverT = BridgeAssembly.T("HttpServer");
            _logT = BridgeAssembly.T("BridgeLog");
            _snapT = BridgeAssembly.T("Snapshot");

            _logPath = (string)BridgeAssembly.Call(_logT, "Init", AppContext.BaseDirectory);
            Checks.Report(_logPath != null, "BridgeLog.Init resolves a writable log path",
                _logPath ?? "<null>");

            // ---- the parts the 2026-08-29 verification pass found missing ----
            // Run first so the surface checks below can serve the real fresh-load document.
            DrivingStyleGuard();
            EditionPolicy();
            ContractSamples.Run();

            await RunSurface("httpsys");
            await RunSurface("socket");
            TransportSelection();
            LogBlocks();
            RawSocketProtocol();
            await ReloadRace();

            await HealthSample();
            await ErrorCodeCatalog();
            SamplesReadme.Write();

            Console.WriteLine();
            Console.WriteLine("RESULT: " + Checks.Passed + " passed, " + Checks.Failed + " failed");
            return Checks.Failed == 0 ? 0 : 1;
        }

        // =====================================================================================
        // The full CONTRACTS §1 surface, run once per transport so both code paths are proven equal.
        // =====================================================================================
        private static async Task RunSurface(string transportMode)
        {
            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", transportMode);
            object shared = NewShared();
            object server = NewServer(shared);
            StartServer(server);
            string label = "[" + transportMode + "] ";
            Checks.Report(Listening(server), label + "transport bound", Describe(server));
            if (!Listening(server))
            {
                return;
            }

            var http = new HttpClient { BaseAddress = new Uri("http://127.0.0.1:7777/") };

            async Task Check(string name, Func<Task<(int status, string body)>> run, int wantStatus,
                             params string[] wantContains)
            {
                try
                {
                    var (status, body) = await run();
                    bool ok = status == wantStatus && wantContains.All(s => body.Contains(s));
                    Checks.Report(ok, label + name, status + " " + body);
                }
                catch (Exception ex)
                {
                    Checks.Report(false, label + name, "EXCEPTION " + ex.Message);
                }
            }

            async Task<(int, string)> Get(string path)
            {
                var r = await http.GetAsync(path);
                return ((int)r.StatusCode, await r.Content.ReadAsStringAsync());
            }

            async Task<(int, string)> Post(string path, string json)
            {
                var r = await http.PostAsync(path,
                    new StringContent(json, Encoding.UTF8, "application/json"));
                return ((int)r.StatusCode, await r.Content.ReadAsStringAsync());
            }

            // --- before any snapshot exists ---
            await Check("GET /state before first tick", () => Get("/state"), 503, "not_ready");
            await Check("GET /health", () => Get("/health"), 200, "\"version\":\"1.0.0\"",
                "\"edition\":\"unknown\"", "queue_depth", "tick_hz", "game_fps",
                "\"online_blocked\":false");

            // --- /task validation: all 11 types accepted with valid params ---
            await Check("task drive_to valid", () => Post("/task", "{\"type\":\"drive_to\",\"params\":{\"x\":1,\"y\":2,\"z\":3,\"speed_mps\":15,\"style\":\"rushed\"}}"), 202, "t-000001");
            await Check("task walk_to valid", () => Post("/task", "{\"type\":\"walk_to\",\"params\":{\"x\":1,\"y\":2,\"z\":3,\"run\":true}}"), 202, "t-000002");
            await Check("task enter_nearest_vehicle valid", () => Post("/task", "{\"type\":\"enter_nearest_vehicle\",\"params\":{\"prefer\":\"nicer\"}}"), 202, "t-000003");
            await Check("task exit_vehicle valid", () => Post("/task", "{\"type\":\"exit_vehicle\",\"params\":{}}"), 202, "t-000004");
            await Check("task wander_drive valid", () => Post("/task", "{\"type\":\"wander_drive\",\"params\":{\"style\":\"avoid_traffic\"}}"), 202, "t-000005");
            await Check("task flee_police valid", () => Post("/task", "{\"type\":\"flee_police\"}"), 202, "t-000006");
            await Check("task combat valid", () => Post("/task", "{\"type\":\"combat_hated_targets_around\",\"params\":{\"radius_m\":30}}"), 202, "t-000007");
            await Check("task seek_cover valid", () => Post("/task", "{\"type\":\"seek_cover\",\"params\":{\"duration_s\":10}}"), 202, "t-000008");
            await Check("task follow_entity valid", () => Post("/task", "{\"type\":\"follow_entity\",\"params\":{\"handle\":1234,\"in_vehicle\":false}}"), 202, "t-000009");
            await Check("task set_waypoint valid", () => Post("/task", "{\"type\":\"set_waypoint\",\"params\":{\"x\":10,\"y\":20}}"), 202, "t-000010");
            await Check("task stop valid", () => Post("/task", "{\"type\":\"stop\"}"), 202, "t-000011");

            // --- /task validation failures ---
            await Check("task unknown type -> 400", () => Post("/task", "{\"type\":\"teleport\",\"params\":{}}"), 400, "unknown_task_type");
            await Check("task drive_to missing speed -> 400", () => Post("/task", "{\"type\":\"drive_to\",\"params\":{\"x\":1,\"y\":2,\"z\":3}}"), 400, "invalid_params", "speed_mps");
            await Check("task drive_to bad style -> 400", () => Post("/task", "{\"type\":\"drive_to\",\"params\":{\"x\":1,\"y\":2,\"z\":3,\"speed_mps\":10,\"style\":\"insane\"}}"), 400, "invalid_params", "normal|rushed|ignore_lights|avoid_traffic");
            await Check("task combat missing radius -> 400", () => Post("/task", "{\"type\":\"combat_hated_targets_around\"}"), 400, "invalid_params", "radius_m");
            await Check("task follow_entity missing handle -> 400", () => Post("/task", "{\"type\":\"follow_entity\"}"), 400, "invalid_params", "handle");
            await Check("task enter prefer bogus -> 400", () => Post("/task", "{\"type\":\"enter_nearest_vehicle\",\"params\":{\"prefer\":\"fastest\"}}"), 400, "invalid_params");
            await Check("task no type -> 400", () => Post("/task", "{\"params\":{}}"), 400, "invalid_params");
            await Check("task garbage body -> 400", () => Post("/task", "not json"), 400, "invalid_json");
            await Check("task empty body -> 400", () => Post("/task", ""), 400, "invalid_json");

            // --- other POST validation (no game thread running -> valid ones stall honestly) ---
            await Check("timescale non-numeric -> 400", () => Post("/timescale", "{\"value\":\"x\"}"), 400, "invalid_params");
            await Check("timescale valid stalls honestly -> 503", () => Post("/timescale", "{\"value\":0.5}"), 503, "game_thread_stalled");
            await Check("control non-bool -> 400", () => Post("/control", "{\"enabled\":1}"), 400, "invalid_params");
            await Check("radio missing station -> 400", () => Post("/radio", "{}"), 400, "invalid_params");
            await Check("horn missing ms -> 400", () => Post("/horn", "{}"), 400, "invalid_params");
            await Check("unstick precheck no state -> 409", () => Post("/unstick", "{}"), 409, "unstick_conditions_not_met");

            // --- routing ---
            await Check("unknown path -> 404", () => Get("/teleport"), 404, "not_found");
            await Check("POST /state -> 405", () => Post("/state", "{}"), 405, "method_not_allowed");
            await Check("GET /task -> 405", () => Get("/task"), 405, "method_not_allowed");
            await Check("trailing slash normalizes", () => Get("/health/"), 200, "\"version\":\"1.0.0\"");
            await Check("query string ignored", () => Get("/health?probe=1"), 200, "\"version\":\"1.0.0\"");

            // --- published snapshot serving: the real fresh-load document, over HTTP ---
            string freshLoad = ContractSamples.FreshLoadJson;
            _sharedT.GetProperty("StateJson").SetValue(shared, "{\"smoke_probe\":true}");
            await Check("GET /state serves published snapshot", () => Get("/state"), 200, "smoke_probe");
            if (freshLoad != null)
            {
                _sharedT.GetProperty("StateJson").SetValue(shared, freshLoad);
                await Check("GET /state returns the snapshot bytes unmodified",
                    () => Get("/state"), 200, "\"last_task\":{\"id\":null,\"type\":null,");
            }

            // --- unstick precheck against a snapshot lacking a drive task ---
            object snap = Activator.CreateInstance(_snapT);
            _sharedT.GetProperty("LastSnapshot").SetValue(shared, snap);
            await Check("unstick with idle snapshot -> 409", () => Post("/unstick", "{}"), 409,
                "no drive task is running");

            // --- queue depth reflects the 11 enqueued tasks + the stalled timescale command ---
            await Check("health queue_depth = 12", () => Get("/health"), 200, "\"queue_depth\":12");

            // --- queue bound: fill to MaxQueueDepth (128) then reject ---
            for (int i = 0; i < 116; i++)
            {
                await Post("/task", "{\"type\":\"stop\"}");
            }
            await Check("health queue_depth = 128 (bound reached)", () => Get("/health"), 200,
                "\"queue_depth\":128");
            await Check("task beyond bound -> 503 queue_full", () => Post("/task", "{\"type\":\"stop\"}"), 503, "queue_full");
            await Check("timescale beyond bound -> 503 queue_full", () => Post("/timescale", "{\"value\":0.5}"), 503, "queue_full");

            // --- online kill switch blankets every endpoint ---
            _sharedT.GetProperty("OnlineBlocked").SetValue(shared, true);
            await Check("online: GET /state -> 503", () => Get("/state"), 503, "online_session_active");
            await Check("online: GET /health -> 503", () => Get("/health"), 503, "online_session_active");
            await Check("online: POST /task -> 503", () => Post("/task", "{\"type\":\"stop\"}"), 503, "online_session_active");
            await Check("online: POST /unstick -> 503", () => Post("/unstick", "{}"), 503, "online_session_active");

            http.Dispose();
            StopServer(server);
            Checks.Report(!Listening(server), label + "stopped releases the listener",
                "IsListening=false");
        }

        // =====================================================================================
        // Transport selection: default (auto) and a bogus override.
        // =====================================================================================
        private static void TransportSelection()
        {
            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", null);
            object shared = NewShared();
            object server = NewServer(shared);
            StartServer(server);
            Checks.Report(Listening(server), "[auto] default mode binds", Describe(server));
            Checks.Report(Describe(server).Contains("HttpListener"),
                "[auto] default mode prefers HttpListener", Describe(server));
            StopServer(server);

            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", "nonsense");
            object shared2 = NewShared();
            object server2 = NewServer(shared2);
            StartServer(server2);
            Checks.Report(Listening(server2), "[auto] bogus override falls back to auto",
                Describe(server2));
            StopServer(server2);
            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", null);
        }

        // =====================================================================================
        // Log block rendering (the shape the first-run diagnostics are printed in).
        // =====================================================================================
        private static void LogBlocks()
        {
            MethodInfo blockM = _logT.GetMethod("Block",
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
            try
            {
                blockM.Invoke(null, new object[] { "smoke block", new List<string> { "a: 1", "b: 2" } });
                Checks.Report(true, "BridgeLog.Block renders without throwing", "ok");
            }
            catch (Exception ex)
            {
                Checks.Report(false, "BridgeLog.Block renders without throwing", ex.ToString());
            }
            string logText = File.ReadAllText(_logPath);
            Checks.Report(logText.Contains("===== smoke block ====") && logText.Contains("  a: 1"),
                "BridgeLog.Block wrote a boxed block", "block present in log");
        }

        // =====================================================================================
        // Raw-socket protocol checks against the fallback transport (the one we hand-rolled).
        // =====================================================================================
        private static void RawSocketProtocol()
        {
            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", "socket");
            object shared = NewShared();
            object server = NewServer(shared);
            StartServer(server);
            Checks.Report(Listening(server), "[raw] socket transport bound", Describe(server));

            string RawExchange(string request, int readMs = 4000)
            {
                using var client = new TcpClient("127.0.0.1", 7777)
                {
                    ReceiveTimeout = readMs,
                    SendTimeout = readMs
                };
                using var s = client.GetStream();
                byte[] bytes = Encoding.ASCII.GetBytes(request);
                s.Write(bytes, 0, bytes.Length);
                s.Flush();
                var sb = new StringBuilder();
                var buf = new byte[4096];
                try
                {
                    int n;
                    while ((n = s.Read(buf, 0, buf.Length)) > 0)
                    {
                        sb.Append(Encoding.UTF8.GetString(buf, 0, n));
                    }
                }
                catch (IOException)
                {
                }
                return sb.ToString();
            }

            string r1 = RawExchange("GET /health HTTP/1.1\r\nHost: 127.0.0.1:7777\r\n\r\n");
            Checks.Report(r1.StartsWith("HTTP/1.1 200 OK\r\n"), "[raw] GET /health status line",
                r1.Split('\n')[0].Trim());
            Checks.Report(r1.Contains("Connection: close"), "[raw] GET /health closes connection",
                "Connection: close present");
            Checks.Report(r1.Contains("Content-Type: application/json; charset=utf-8"),
                "[raw] GET /health content-type", "json content-type");
            Checks.Report(
                int.Parse(System.Text.RegularExpressions.Regex.Match(r1, @"Content-Length: (\d+)")
                    .Groups[1].Value)
                    == Encoding.UTF8.GetByteCount(r1.Substring(r1.IndexOf("\r\n\r\n") + 4)),
                "[raw] GET /health content-length matches body",
                "declared length == actual body bytes");

            string body = "{\"type\":\"stop\"}";
            string r2 = RawExchange("POST /task HTTP/1.1\r\nHost: 127.0.0.1:7777\r\nContent-Length: "
                                    + body.Length + "\r\nContent-Type: application/json\r\n\r\n" + body);
            Checks.Report(r2.StartsWith("HTTP/1.1 202 Accepted") && r2.Contains("t-000001"),
                "[raw] POST with Content-Length body", r2.Split('\n')[0].Trim());

            string r3 = RawExchange("POST /task HTTP/1.1\r\nHost: 127.0.0.1:7777\r\nExpect: 100-continue\r\nContent-Length: "
                                    + body.Length + "\r\n\r\n" + body);
            Checks.Report(r3.StartsWith("HTTP/1.1 100 Continue") && r3.Contains("202 Accepted"),
                "[raw] Expect: 100-continue handled",
                r3.Replace("\r\n", "|").Substring(0, Math.Min(60, r3.Length)));

            string r4 = RawExchange("POST /task HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n");
            Checks.Report(r4.StartsWith("HTTP/1.1 411") && r4.Contains("length_required"),
                "[raw] chunked rejected with 411", r4.Split('\n')[0].Trim());

            string r5 = RawExchange("POST /task HTTP/1.1\r\nHost: x\r\nContent-Length: 999999\r\n\r\n");
            Checks.Report(r5.StartsWith("HTTP/1.1 413") && r5.Contains("body_too_large"),
                "[raw] oversized body rejected with 413", r5.Split('\n')[0].Trim());

            string r6 = RawExchange("NOTAREQUESTLINE\r\n\r\n");
            Checks.Report(r6.StartsWith("HTTP/1.1 400") && r6.Contains("bad_request"),
                "[raw] malformed request line -> 400", r6.Split('\n')[0].Trim());

            string r7 = RawExchange("GET http://127.0.0.1:7777/health HTTP/1.1\r\nHost: 127.0.0.1:7777\r\n\r\n");
            Checks.Report(r7.StartsWith("HTTP/1.1 200 OK"),
                "[raw] absolute-form request target routes", r7.Split('\n')[0].Trim());

            string r8 = RawExchange("GET /health HTTP/1.1\r\nHost: some-other-name:7777\r\n\r\n");
            Checks.Report(r8.StartsWith("HTTP/1.1 200 OK"), "[raw] any Host header accepted",
                "no HTTP.SYS host matching");

            using (var idle = new TcpClient("127.0.0.1", 7777))
            {
            }
            string r9 = RawExchange("GET /health HTTP/1.1\r\nHost: x\r\n\r\n");
            Checks.Report(r9.StartsWith("HTTP/1.1 200 OK"),
                "[raw] accept loop survives an empty connection", r9.Split('\n')[0].Trim());

            StopServer(server);
            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", null);
        }

        // =====================================================================================
        // Reload race: a second instance cannot bind while the first holds the port, and recovers
        // by itself once the port is free (this is what an SHVDN script reload looks like).
        // =====================================================================================
        private static async Task ReloadRace()
        {
            object sharedA = NewShared();
            object serverA = NewServer(sharedA);
            StartServer(serverA);
            Checks.Report(Listening(serverA), "[reload] first instance bound", Describe(serverA));

            object sharedB = NewShared();
            object serverB = NewServer(sharedB);
            StartServer(serverB);
            Checks.Report(!Listening(serverB), "[reload] second instance refuses to double-bind",
                "IsListening=false while A holds the port");

            StopServer(serverA);
            Checks.Report(!Listening(serverB),
                "[reload] EnsureStarted is a no-op inside the retry window",
                "still not listening immediately after A stops");
            _serverT.GetMethod("EnsureStarted").Invoke(serverB, null);
            bool boundEarly = Listening(serverB);

            Console.WriteLine("      (waiting 10.5 s for the rebind timer...)");
            Thread.Sleep(10500);
            _serverT.GetMethod("EnsureStarted").Invoke(serverB, null);
            Checks.Report(Listening(serverB),
                "[reload] second instance rebinds after the retry interval",
                "IsListening=" + Listening(serverB) + " (early attempt bound=" + boundEarly + ")");

            using (var http = new HttpClient { BaseAddress = new Uri("http://127.0.0.1:7777/") })
            {
                var resp = await http.GetAsync("/health");
                Checks.Report((int)resp.StatusCode == 200, "[reload] rebound instance serves requests",
                    (int)resp.StatusCode + " " + await resp.Content.ReadAsStringAsync());
            }
            StopServer(serverB);
        }

        // =====================================================================================
        // Driving-style guard: the values come from the REAL SHVDN DLL's metadata, and the guard
        // itself is exercised both ways (this run = agreement; the "drift" sub-mode = disagreement).
        // =====================================================================================
        private static void DrivingStyleGuard()
        {
            var expected = new Dictionary<string, uint>
            {
                { "normal", 786603 },
                { "rushed", 1074528293 },
                { "ignore_lights", 786475 },
                { "avoid_traffic", 786468 }
            };

            if (File.Exists(Paths.RealShvdnDll))
            {
                AssemblyName identity;
                List<KeyValuePair<string, ulong>> members;
                StubGenerator.ReadEnum(Paths.RealShvdnDll, out identity, out members);
                var byName = new Dictionary<string, ulong>();
                foreach (KeyValuePair<string, ulong> m in members)
                {
                    byName[m.Key] = m.Value;
                }

                CheckStyleMembers("normal", byName, expected["normal"],
                    "DrivingModeStopForVehicles");
                CheckStyleMembers("rushed", byName, expected["rushed"],
                    "DrivingModeAvoidVehicles", "ForceJoinInRoadDirection");
                CheckStyleMembers("ignore_lights", byName, expected["ignore_lights"],
                    "DrivingModeStopForVehiclesIgnoreLights");
                CheckStyleMembers("avoid_traffic", byName, expected["avoid_traffic"],
                    "DrivingModeAvoidVehiclesReckless");
            }
            else
            {
                Checks.Report(false, "[styles] real SHVDN DLL available for metadata check",
                    Paths.RealShvdnDll + " is missing (run scripts/fetch-shvdn.ps1)");
            }

            // The guard, run against the stub whose values came from that same real DLL.
            Type stylesT = BridgeAssembly.T("DrivingStyles");
            var drift = (string)BridgeAssembly.Call(stylesT, "Verify");
            Checks.Report(drift == null,
                "[styles] DrivingStyles.Verify() finds no drift against the pinned SHVDN values",
                drift ?? "null (no drift)");

            // TryParse still maps the four contract names onto those bitfields.
            foreach (KeyValuePair<string, uint> style in expected)
            {
                object[] args = { style.Key, null };
                var ok = (bool)stylesT.GetMethod("TryParse",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)
                    .Invoke(null, args);
                uint value = ok ? Convert.ToUInt32(args[1], CultureInfo.InvariantCulture) : 0;
                Checks.Report(ok && value == style.Value,
                    "[styles] TryParse(\"" + style.Key + "\") -> " + style.Value,
                    ok ? value.ToString(CultureInfo.InvariantCulture) : "not parsed");
            }
            var parsedBogus = (bool)stylesT.GetMethod("TryParse",
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)
                .Invoke(null, new object[] { "insane", null });
            Checks.Report(!parsedBogus, "[styles] TryParse rejects an unknown style name", "false");
        }

        private static void CheckStyleMembers(string style, Dictionary<string, ulong> byName,
                                              uint expected, params string[] members)
        {
            ulong composed = 0;
            var missing = new List<string>();
            foreach (string m in members)
            {
                if (!byName.ContainsKey(m))
                {
                    missing.Add(m);
                    continue;
                }
                composed |= byName[m];
            }
            Checks.Report(missing.Count == 0 && composed == expected,
                "[styles] real SHVDN metadata: " + string.Join("|", members) + " = "
                + expected + " (CONTRACTS D3 " + style + ")",
                missing.Count > 0 ? "missing members: " + string.Join(",", missing)
                                  : composed.ToString(CultureInfo.InvariantCulture));
        }

        // =====================================================================================
        // Edition detection policy (CONTRACTS v1.2: "unknown" is legal, but only transiently).
        // The native read stays on the game thread; this is the pure policy object it drives.
        // =====================================================================================
        private static void EditionPolicy()
        {
            Type t = BridgeAssembly.T("EditionDetector");
            MethodInfo classify = t.GetMethod("Classify",
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);

            Checks.Report((string)classify.Invoke(null, new object[] { new Version(1, 0, 3889, 0) }) == "legacy",
                "[edition] 1.0.3889.0 (the pinned Legacy build) classifies as \"legacy\"", "legacy");
            Checks.Report((string)classify.Invoke(null, new object[] { new Version(1, 0, 1158, 13) }) == "enhanced",
                "[edition] 1.0.1158.13 (an Enhanced build) classifies as \"enhanced\"", "enhanced");
            Checks.Report(classify.Invoke(null, new object[] { null }) == null,
                "[edition] an unreadable Game.FileVersion classifies as null (not a latch)", "null");

            // The regression the verification pass found: one early throw used to pin "unknown".
            object det = BridgeAssembly.New("EditionDetector");
            Checks.Report((string)BridgeAssembly.GetProp(det, "Edition") == "unknown",
                "[edition] a fresh detector reports \"unknown\"", "unknown");
            Checks.Report((bool)BridgeAssembly.CallInstance(det, "ShouldAttempt", 0),
                "[edition] first tick attempts detection immediately", "true");

            // Attempt 1 fails (Game.FileVersion threw).
            BridgeAssembly.CallInstance(det, "Accept", 0, null);
            Checks.Report((string)BridgeAssembly.GetProp(det, "Edition") == "unknown"
                          && !(bool)BridgeAssembly.GetProp(det, "Resolved"),
                "[edition] a failed attempt leaves it unresolved and reporting \"unknown\"",
                "unknown / Resolved=false");
            Checks.Report(!(bool)BridgeAssembly.CallInstance(det, "ShouldAttempt", 4999),
                "[edition] does not retry before the 5 s interval", "false at +4999 ms");
            Checks.Report((bool)BridgeAssembly.CallInstance(det, "ShouldAttempt", 5000),
                "[edition] RETRIES after the interval instead of latching \"unknown\"",
                "true at +5000 ms");

            // Attempt 2 succeeds.
            var detected = (string)BridgeAssembly.CallInstance(det, "Accept", 5000,
                new Version(1, 0, 3889, 0));
            Checks.Report(detected == "legacy"
                          && (string)BridgeAssembly.GetProp(det, "Edition") == "legacy",
                "[edition] a later attempt resolves the edition", "legacy on attempt 2");
            Checks.Report(!(bool)BridgeAssembly.CallInstance(det, "ShouldAttempt", 999999),
                "[edition] stops probing once resolved", "false");
            Checks.Report((int)BridgeAssembly.GetProp(det, "Attempts") == 2,
                "[edition] exactly two attempts were made", "2");

            // Wrap-around safety: Environment.TickCount is a 32-bit counter that wraps.
            object wrap = BridgeAssembly.New("EditionDetector");
            BridgeAssembly.CallInstance(wrap, "Accept", int.MaxValue - 1000, null);
            Checks.Report((bool)BridgeAssembly.CallInstance(wrap, "ShouldAttempt", int.MinValue + 4000),
                "[edition] retry timing survives a TickCount wrap", "true across the wrap");
        }

        // =====================================================================================
        // /health, captured from the real router and validated field by field.
        // =====================================================================================
        private static async Task HealthSample()
        {
            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", "socket");
            object shared = NewShared();
            object server = NewServer(shared);
            StartServer(server);
            using (var http = new HttpClient { BaseAddress = new Uri("http://127.0.0.1:7777/") })
            {
                var resp = await http.GetAsync("/health");
                _healthSample = await resp.Content.ReadAsStringAsync();
                Checks.Report((int)resp.StatusCode == 200, "[health] GET /health -> 200",
                    _healthSample);

                JObject doc = StateSchema.Parse(_healthSample);
                List<string> problems = StateSchema.Validate(doc, StateSchema.Health);
                Checks.Report(problems.Count == 0,
                    "[health] every documented CONTRACTS §1 /health field present with the right type",
                    problems.Count == 0 ? StateSchema.Health.Count + " field specs satisfied"
                                        : string.Join(" | ", problems));
                List<string> extra = StateSchema.Undocumented(doc, StateSchema.Health);
                Checks.Report(extra.Count == 0, "[health] no undocumented keys",
                    extra.Count == 0 ? "none" : string.Join(", ", extra));
                Checks.Report((string)doc["edition"] == "unknown",
                    "[health] edition is \"unknown\" before detection completes (CONTRACTS v1.2)",
                    (string)doc["edition"]);

                string path = Path.Combine(Paths.ContractSamplesDir, "health-fresh-load.json");
                File.WriteAllText(path, _healthSample);
                Checks.Report(File.Exists(path), "[samples] wrote health-fresh-load.json", path);
            }
            StopServer(server);
            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", null);
        }

        // =====================================================================================
        // Every CONTRACTS v1.2 error code that can be produced without the game, captured from the
        // real router and written to contract-samples/errors.json for the harness to test against.
        // =====================================================================================
        private static async Task ErrorCodeCatalog()
        {
            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", "socket");
            object shared = NewShared();
            object server = NewServer(shared);
            StartServer(server);

            using (var http = new HttpClient { BaseAddress = new Uri("http://127.0.0.1:7777/") })
            {
                async Task Capture(string method, string path, string body)
                {
                    var resp = method == "GET"
                        ? await http.GetAsync(path)
                        : await http.PostAsync(path,
                            new StringContent(body ?? "", Encoding.UTF8, "application/json"));
                    string text = await resp.Content.ReadAsStringAsync();
                    JObject parsed;
                    try
                    {
                        parsed = StateSchema.Parse(text);
                    }
                    catch (JsonException)
                    {
                        parsed = new JObject { ["error"] = "<unparseable>", ["detail"] = text };
                    }
                    ErrorCatalog.Add(new JObject
                    {
                        ["request"] = method + " " + path
                                      + (body == null ? "" : "  " + body),
                        ["status"] = (int)resp.StatusCode,
                        ["body"] = parsed
                    });
                    List<string> problems = StateSchema.Validate(parsed, StateSchema.Error);
                    Checks.Report(problems.Count == 0,
                        "[errors] " + method + " " + path + " -> " + (int)resp.StatusCode
                        + " body matches {\"error\",\"detail\"}",
                        problems.Count == 0 ? text : string.Join(" | ", problems));
                }

                await Capture("GET", "/state", null);                                      // not_ready
                await Capture("POST", "/task", "{\"type\":\"teleport\"}");                 // unknown_task_type
                await Capture("POST", "/task", "{\"type\":\"drive_to\",\"params\":{}}");   // invalid_params
                await Capture("POST", "/task", "not json");                                // invalid_json
                await Capture("POST", "/unstick", "{}");                                   // unstick_conditions_not_met
                await Capture("POST", "/timescale", "{\"value\":0.5}");                    // game_thread_stalled
                await Capture("GET", "/teleport", null);                                   // not_found (bridge extra)
                await Capture("POST", "/state", "{}");                                     // method_not_allowed (extra)

                for (int i = 0; i < BridgeShared_MaxQueueDepth(); i++)
                {
                    await http.PostAsync("/task",
                        new StringContent("{\"type\":\"stop\"}", Encoding.UTF8, "application/json"));
                }
                await Capture("POST", "/task", "{\"type\":\"stop\"}");                     // queue_full

                _sharedT.GetProperty("OnlineBlocked").SetValue(shared, true);
                await Capture("GET", "/state", null);                                      // online_session_active
            }
            StopServer(server);
            Environment.SetEnvironmentVariable("WASTED_BRIDGE_TRANSPORT", null);

            // CONTRACTS v1.2 closed error-code set, with the status each code must carry.
            var contractCodes = new Dictionary<string, int>
            {
                { "online_session_active", 503 },
                { "not_ready", 503 },
                { "game_thread_stalled", 503 },
                { "queue_full", 503 },
                { "unknown_task_type", 400 },
                { "invalid_params", 400 },
                { "invalid_json", 400 },
                { "unstick_conditions_not_met", 409 }
                // not_in_vehicle (409) is raised on the game thread only — server-only check.
            };
            var seen = new Dictionary<string, int>();
            foreach (JObject entry in ErrorCatalog)
            {
                seen[(string)entry["body"]["error"]] = (int)entry["status"];
            }
            foreach (KeyValuePair<string, int> code in contractCodes)
            {
                bool ok = seen.ContainsKey(code.Key) && seen[code.Key] == code.Value;
                Checks.Report(ok,
                    "[errors] CONTRACTS v1.2 code " + code.Key + " observed with HTTP "
                    + code.Value,
                    seen.ContainsKey(code.Key) ? "status " + seen[code.Key] : "NOT OBSERVED");
            }

            var doc = new JObject
            {
                ["_note"] = "Real responses captured from the compiled bridge by "
                            + "bridge/tools/offline-checks. Every body below came out of "
                            + "BridgeRouter over a real HTTP connection; nothing is hand-written.",
                ["contract_version"] = "1.2",
                ["captured"] = new JArray(ErrorCatalog.ToArray()),
                ["not_capturable_offline"] = new JArray(
                    new JObject
                    {
                        ["error"] = "not_in_vehicle",
                        ["status"] = 409,
                        ["why"] = "raised by the game thread (POST /horn on foot); needs the game"
                    })
            };
            string path = Path.Combine(Paths.ContractSamplesDir, "errors.json");
            File.WriteAllText(path, doc.ToString(Formatting.Indented));
            Checks.Report(File.Exists(path), "[samples] wrote errors.json",
                path + " (" + ErrorCatalog.Count + " captured responses)");
        }

        private static int BridgeShared_MaxQueueDepth()
        {
            return (int)_sharedT.GetField("MaxQueueDepth",
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)
                .GetValue(null);
        }

        // ---- reflection plumbing --------------------------------------------------------------

        private static object NewShared()
        {
            return Activator.CreateInstance(_sharedT, true);
        }

        private static object NewServer(object shared)
        {
            return Activator.CreateInstance(_serverT,
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic, null,
                new[] { shared }, null);
        }

        private static void StartServer(object s)
        {
            _serverT.GetMethod("Start").Invoke(s, null);
        }

        private static void StopServer(object s)
        {
            _serverT.GetMethod("Stop").Invoke(s, null);
        }

        private static bool Listening(object s)
        {
            return (bool)_serverT.GetProperty("IsListening").GetValue(s);
        }

        private static string Describe(object s)
        {
            return (string)_serverT.GetProperty("Description").GetValue(s);
        }
    }
}
