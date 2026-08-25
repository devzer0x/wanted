using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace WastedBridge
{
    /// <summary>
    /// Bridge HTTP API v1 on http://127.0.0.1:7777 (CONTRACTS.md §1). Runs entirely on background
    /// threads: GETs serve the cached snapshot, POSTs validate and enqueue commands for the game
    /// thread. NATIVES ARE NEVER CALLED FROM THIS FILE.
    /// </summary>
    internal sealed class HttpServer
    {
        private const string Prefix = "http://127.0.0.1:7777/";
        // How long a POST waits for the game thread to apply its command. Ticks run per-frame, so
        // anything longer means the game is loading or hung — report that honestly.
        private const int GameThreadWaitMs = 2000;
        private const float UnstickMinStoppedS = 20f;

        private readonly BridgeShared _shared;
        private HttpListener _listener;
        private Thread _acceptThread;
        private volatile bool _running;

        public HttpServer(BridgeShared shared)
        {
            _shared = shared;
        }

        public void Start()
        {
            _listener = new HttpListener();
            _listener.Prefixes.Add(Prefix);
            try
            {
                _listener.Start();
            }
            catch (HttpListenerException ex)
            {
                // Fail loudly: without the listener the bridge is useless. On a locked-down user
                // account HTTP.SYS may require a one-time URL ACL:
                //   netsh http add urlacl url=http://127.0.0.1:7777/ user=Everyone
                BridgeLog.Error("HttpListener failed to bind " + Prefix
                                + " (win32 error " + ex.ErrorCode + "). If access was denied, run: "
                                + "netsh http add urlacl url=http://127.0.0.1:7777/ user=Everyone", ex);
                throw;
            }
            _running = true;
            _acceptThread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "WastedBridge.Http"
            };
            _acceptThread.Start();
            BridgeLog.Info("HTTP API listening on " + Prefix);
        }

        public void Stop()
        {
            _running = false;
            try
            {
                if (_listener != null)
                {
                    _listener.Stop();
                    _listener.Close();
                }
            }
            catch (ObjectDisposedException)
            {
            }
            BridgeLog.Info("HTTP API stopped");
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                HttpListenerContext ctx;
                try
                {
                    ctx = _listener.GetContext();
                }
                catch (HttpListenerException)
                {
                    if (_running)
                    {
                        continue;
                    }
                    return;
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                ThreadPool.QueueUserWorkItem(HandleSafe, ctx);
            }
        }

        private void HandleSafe(object state)
        {
            var ctx = (HttpListenerContext)state;
            try
            {
                Handle(ctx);
            }
            catch (Exception ex)
            {
                BridgeLog.Error("unhandled HTTP error for " + ctx.Request.RawUrl, ex);
                TryWriteError(ctx, 500, "internal_error", ex.Message);
            }
        }

        private void Handle(HttpListenerContext ctx)
        {
            // Safety rule (CONTRACTS §1): once an online session is detected, every endpoint
            // returns 503 online_session_active.
            if (_shared.OnlineBlocked)
            {
                WriteError(ctx, 503, "online_session_active",
                    "a GTA Online session was detected; the bridge disabled itself");
                return;
            }

            string path = ctx.Request.Url.AbsolutePath;
            if (path.Length > 1)
            {
                path = path.TrimEnd('/');
            }
            string method = ctx.Request.HttpMethod;

            switch (path)
            {
                case "/state":
                    if (RequireMethod(ctx, method, "GET")) HandleState(ctx);
                    break;
                case "/health":
                    if (RequireMethod(ctx, method, "GET")) HandleHealth(ctx);
                    break;
                case "/task":
                    if (RequireMethod(ctx, method, "POST")) HandleTask(ctx);
                    break;
                case "/timescale":
                    if (RequireMethod(ctx, method, "POST")) HandleTimescale(ctx);
                    break;
                case "/control":
                    if (RequireMethod(ctx, method, "POST")) HandleControl(ctx);
                    break;
                case "/radio":
                    if (RequireMethod(ctx, method, "POST")) HandleRadio(ctx);
                    break;
                case "/horn":
                    if (RequireMethod(ctx, method, "POST")) HandleHorn(ctx);
                    break;
                case "/unstick":
                    if (RequireMethod(ctx, method, "POST")) HandleUnstick(ctx);
                    break;
                default:
                    WriteError(ctx, 404, "not_found", "unknown path " + path);
                    break;
            }
        }

        // ---- GET /state ----------------------------------------------------------------------

        private void HandleState(HttpListenerContext ctx)
        {
            string json = _shared.StateJson;
            if (json == null)
            {
                WriteError(ctx, 503, "not_ready", "the first game tick has not completed yet");
                return;
            }
            WriteRaw(ctx, 200, json);
        }

        // ---- GET /health ---------------------------------------------------------------------

        private void HandleHealth(HttpListenerContext ctx)
        {
            bool stale = _shared.MillisSinceLastTick() > 2000;
            var body = new JObject
            {
                ["version"] = WastedBridgeScript.BridgeVersion,
                ["edition"] = _shared.Edition,
                ["tick_hz"] = stale ? 0f : _shared.TickHz,
                ["queue_depth"] = _shared.Commands.Count,
                ["game_fps"] = _shared.GameFps,
                ["online_blocked"] = _shared.OnlineBlocked
            };
            WriteJson(ctx, 200, body);
        }

        // ---- POST /task ----------------------------------------------------------------------

        private void HandleTask(HttpListenerContext ctx)
        {
            JObject body = ReadJsonBody(ctx);
            if (body == null)
            {
                return;
            }
            string type = (string)body["type"];
            if (string.IsNullOrEmpty(type))
            {
                WriteError(ctx, 400, "invalid_params", "missing \"type\"");
                return;
            }
            var p = body["params"] as JObject ?? new JObject();

            TaskRequest req;
            string error, detail;
            if (!TryBuildTaskRequest(type, p, out req, out error, out detail))
            {
                WriteError(ctx, 400, error, detail);
                return;
            }
            req.Id = _shared.NextTaskId();
            _shared.Commands.Enqueue(new BridgeCommand { Kind = CommandKind.NewTask, Task = req });
            WriteJson(ctx, 202, new JObject { ["task_id"] = req.Id });
        }

        /// <summary>Validates params for all 11 CONTRACTS §1 task types; no natives involved.</summary>
        private static bool TryBuildTaskRequest(string type, JObject p, out TaskRequest req,
                                                out string error, out string detail)
        {
            req = new TaskRequest { Type = type };
            error = null;
            detail = null;

            switch (type)
            {
                case "drive_to":
                {
                    if (!TryFloat(p, "x", out req.X) || !TryFloat(p, "y", out req.Y)
                        || !TryFloat(p, "z", out req.Z))
                    {
                        return Invalid(out error, out detail, "drive_to requires numeric x, y, z");
                    }
                    if (!TryFloat(p, "speed_mps", out req.SpeedMps))
                    {
                        return Invalid(out error, out detail, "drive_to requires numeric speed_mps");
                    }
                    if (!TryStyle(p, out req.Style, out detail))
                    {
                        error = "invalid_params";
                        return false;
                    }
                    req.ArriveRadiusM = OptFloat(p, "arrive_radius_m", 8f);
                    return true;
                }
                case "walk_to":
                {
                    if (!TryFloat(p, "x", out req.X) || !TryFloat(p, "y", out req.Y)
                        || !TryFloat(p, "z", out req.Z))
                    {
                        return Invalid(out error, out detail, "walk_to requires numeric x, y, z");
                    }
                    req.Run = OptBool(p, "run", false);
                    return true;
                }
                case "enter_nearest_vehicle":
                {
                    req.Prefer = OptString(p, "prefer", "any");
                    if (req.Prefer != "any" && req.Prefer != "nicer")
                    {
                        return Invalid(out error, out detail, "prefer must be \"nicer\" or \"any\"");
                    }
                    req.SearchRadiusM = OptFloat(p, "search_radius_m", 30f);
                    return true;
                }
                case "exit_vehicle":
                case "flee_police":
                case "stop":
                    return true;
                case "wander_drive":
                {
                    if (!TryStyle(p, out req.Style, out detail))
                    {
                        error = "invalid_params";
                        return false;
                    }
                    return true;
                }
                case "combat_hated_targets_around":
                {
                    if (!TryFloat(p, "radius_m", out req.RadiusM) || req.RadiusM <= 0f)
                    {
                        return Invalid(out error, out detail,
                            "combat_hated_targets_around requires positive numeric radius_m");
                    }
                    return true;
                }
                case "seek_cover":
                {
                    req.DurationS = OptFloat(p, "duration_s", 10f);
                    if (req.DurationS <= 0f)
                    {
                        return Invalid(out error, out detail, "duration_s must be positive");
                    }
                    return true;
                }
                case "follow_entity":
                {
                    if (!TryInt(p, "handle", out req.Handle))
                    {
                        return Invalid(out error, out detail, "follow_entity requires integer handle");
                    }
                    req.InVehicle = OptBool(p, "in_vehicle", false);
                    return true;
                }
                case "set_waypoint":
                {
                    if (!TryFloat(p, "x", out req.X) || !TryFloat(p, "y", out req.Y))
                    {
                        return Invalid(out error, out detail, "set_waypoint requires numeric x, y");
                    }
                    return true;
                }
                default:
                    error = "unknown_task_type";
                    detail = "\"" + type + "\" is not a CONTRACTS §1 task type";
                    return false;
            }
        }

        // ---- POST /timescale -----------------------------------------------------------------

        private void HandleTimescale(HttpListenerContext ctx)
        {
            JObject body = ReadJsonBody(ctx);
            if (body == null)
            {
                return;
            }
            float value;
            if (!TryFloat(body, "value", out value))
            {
                WriteError(ctx, 400, "invalid_params", "timescale requires numeric \"value\"");
                return;
            }
            float clamped = Math.Min(1.0f, Math.Max(0.1f, value)); // contract: clamp to 0.1–1.0
            RunOnGameThread(ctx, new BridgeCommand
            {
                Kind = CommandKind.SetTimescale,
                TimescaleValue = clamped
            });
        }

        // ---- POST /control -------------------------------------------------------------------

        private void HandleControl(HttpListenerContext ctx)
        {
            JObject body = ReadJsonBody(ctx);
            if (body == null)
            {
                return;
            }
            JToken tok = body["enabled"];
            if (tok == null || tok.Type != JTokenType.Boolean)
            {
                WriteError(ctx, 400, "invalid_params", "control requires boolean \"enabled\"");
                return;
            }
            RunOnGameThread(ctx, new BridgeCommand
            {
                Kind = CommandKind.SetControl,
                ControlEnabled = (bool)tok
            });
        }

        // ---- POST /radio ---------------------------------------------------------------------

        private void HandleRadio(HttpListenerContext ctx)
        {
            JObject body = ReadJsonBody(ctx);
            if (body == null)
            {
                return;
            }
            string station = (string)body["station"];
            if (string.IsNullOrEmpty(station))
            {
                WriteError(ctx, 400, "invalid_params", "radio requires \"station\" (a name or \"off\")");
                return;
            }
            RunOnGameThread(ctx, new BridgeCommand
            {
                Kind = CommandKind.SetRadio,
                RadioStation = station
            });
        }

        // ---- POST /horn ----------------------------------------------------------------------

        private void HandleHorn(HttpListenerContext ctx)
        {
            JObject body = ReadJsonBody(ctx);
            if (body == null)
            {
                return;
            }
            float ms;
            if (!TryFloat(body, "ms", out ms))
            {
                WriteError(ctx, 400, "invalid_params", "horn requires numeric \"ms\" (1-3000)");
                return;
            }
            int clamped = (int)Math.Min(3000f, Math.Max(1f, ms));
            RunOnGameThread(ctx, new BridgeCommand { Kind = CommandKind.Horn, HornMs = clamped });
        }

        // ---- POST /unstick -------------------------------------------------------------------

        private void HandleUnstick(HttpListenerContext ctx)
        {
            // Fast precheck against the cached snapshot (no natives). The game thread re-verifies
            // authoritatively before moving anything, so a stale snapshot can only cause an extra
            // 409, never an unjustified nudge.
            Snapshot snap = _shared.LastSnapshot;
            string why = UnstickBlockReason(snap);
            if (why != null)
            {
                WriteError(ctx, 409, "unstick_conditions_not_met", why);
                return;
            }
            RunOnGameThread(ctx, new BridgeCommand { Kind = CommandKind.Unstick });
        }

        private static string UnstickBlockReason(Snapshot snap)
        {
            if (snap == null)
            {
                return "no game state yet";
            }
            LastTaskDto task = snap.LastTask;
            bool driveRunning = task != null && task.Status == "running"
                                && (task.Type == "drive_to" || task.Type == "wander_drive");
            if (!driveRunning)
            {
                return "no drive task is running";
            }
            if (snap.Vehicle == null)
            {
                return "player is not in a vehicle";
            }
            if (snap.Vehicle.StoppedForS <= UnstickMinStoppedS)
            {
                return "vehicle has only been stopped for "
                       + snap.Vehicle.StoppedForS.ToString("F1") + " s (need > 20 s)";
            }
            return null;
        }

        // ---- plumbing ------------------------------------------------------------------------

        /// <summary>
        /// Enqueues a command and blocks this HTTP thread until the game thread has applied it,
        /// so the returned status reflects reality. Natives stay on the game thread.
        /// </summary>
        private void RunOnGameThread(HttpListenerContext ctx, BridgeCommand cmd)
        {
            var reply = new CommandReply();
            cmd.Reply = reply;
            _shared.Commands.Enqueue(cmd);
            if (!reply.Wait(GameThreadWaitMs))
            {
                WriteError(ctx, 503, "game_thread_stalled",
                    "the game tick did not process the command within "
                    + GameThreadWaitMs + " ms (loading screen or hang)");
                return;
            }
            WriteJson(ctx, reply.StatusCode, reply.Body);
        }

        private JObject ReadJsonBody(HttpListenerContext ctx)
        {
            string raw;
            using (var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
            {
                raw = reader.ReadToEnd();
            }
            if (string.IsNullOrWhiteSpace(raw))
            {
                WriteError(ctx, 400, "invalid_json", "request body is empty");
                return null;
            }
            try
            {
                var parsed = JToken.Parse(raw) as JObject;
                if (parsed == null)
                {
                    WriteError(ctx, 400, "invalid_json", "request body must be a JSON object");
                    return null;
                }
                return parsed;
            }
            catch (JsonReaderException ex)
            {
                WriteError(ctx, 400, "invalid_json", ex.Message);
                return null;
            }
        }

        private bool RequireMethod(HttpListenerContext ctx, string actual, string expected)
        {
            if (actual == expected)
            {
                return true;
            }
            WriteError(ctx, 405, "method_not_allowed",
                ctx.Request.Url.AbsolutePath + " requires " + expected);
            return false;
        }

        private static bool Invalid(out string error, out string detail, string message)
        {
            error = "invalid_params";
            detail = message;
            return false;
        }

        private static bool TryStyle(JObject p, out GTA.VehicleDrivingFlags style, out string detail)
        {
            string name = OptString(p, "style", "normal");
            if (!DrivingStyles.TryParse(name, out style))
            {
                detail = "style must be one of normal|rushed|ignore_lights|avoid_traffic";
                return false;
            }
            detail = null;
            return true;
        }

        private static bool TryFloat(JObject o, string name, out float value)
        {
            value = 0f;
            JToken t = o[name];
            if (t == null || (t.Type != JTokenType.Float && t.Type != JTokenType.Integer))
            {
                return false;
            }
            value = (float)t;
            return true;
        }

        private static bool TryInt(JObject o, string name, out int value)
        {
            value = 0;
            JToken t = o[name];
            if (t == null || t.Type != JTokenType.Integer)
            {
                return false;
            }
            value = (int)t;
            return true;
        }

        private static float OptFloat(JObject o, string name, float fallback)
        {
            float v;
            return TryFloat(o, name, out v) ? v : fallback;
        }

        private static bool OptBool(JObject o, string name, bool fallback)
        {
            JToken t = o[name];
            return t != null && t.Type == JTokenType.Boolean ? (bool)t : fallback;
        }

        private static string OptString(JObject o, string name, string fallback)
        {
            JToken t = o[name];
            return t != null && t.Type == JTokenType.String ? (string)t : fallback;
        }

        private void WriteError(HttpListenerContext ctx, int status, string error, string detail)
        {
            // Error shape per CONTRACTS conventions: {"error": "<snake_code>", "detail": "text"}.
            WriteJson(ctx, status, new JObject { ["error"] = error, ["detail"] = detail });
        }

        private void TryWriteError(HttpListenerContext ctx, int status, string error, string detail)
        {
            try
            {
                WriteError(ctx, status, error, detail);
            }
            catch (Exception)
            {
                // Response already gone (client hung up); nothing sane to do.
            }
        }

        private void WriteJson(HttpListenerContext ctx, int status, JObject body)
        {
            WriteRaw(ctx, status, body.ToString(Formatting.None));
        }

        private void WriteRaw(HttpListenerContext ctx, int status, string json)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(json);
            ctx.Response.StatusCode = status;
            ctx.Response.ContentType = "application/json; charset=utf-8";
            ctx.Response.ContentLength64 = bytes.Length;
            ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
            ctx.Response.Close();
        }
    }
}
