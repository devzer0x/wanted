using System;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace WastedBridge
{
    /// <summary>One finished HTTP reply: status plus an already-serialized JSON body.</summary>
    internal sealed class BridgeResponse
    {
        public readonly int Status;
        public readonly string Body;

        public BridgeResponse(int status, string body)
        {
            Status = status;
            Body = body;
        }

        public static BridgeResponse FromJson(int status, JObject body)
        {
            return new BridgeResponse(status, body.ToString(Formatting.None));
        }

        /// <summary>Error shape per CONTRACTS conventions: {"error": "&lt;snake_code&gt;", "detail": "text"}.</summary>
        public static BridgeResponse Error(int status, string error, string detail)
        {
            return FromJson(status, new JObject { ["error"] = error, ["detail"] = detail });
        }
    }

    /// <summary>
    /// Bridge HTTP API v1 routing and validation (CONTRACTS.md §1), independent of the transport
    /// that carried the bytes. Runs on background threads only: it reads the cached snapshot and
    /// enqueues commands for the game thread. NATIVES ARE NEVER CALLED FROM THIS FILE.
    /// </summary>
    internal sealed class BridgeRouter
    {
        // How long a POST waits for the game thread to apply its command. Ticks run per-frame, so
        // anything longer means the game is loading or hung — report that honestly. Deliberately
        // under the harness's own 2 s client timeout (harness/wasted_harness/bridge_client.py) so
        // a stalled game thread surfaces as an explicit 503 game_thread_stalled rather than as a
        // client-side read timeout, which the harness can only report as "bridge down".
        private const int GameThreadWaitMs = 1500;
        private const float UnstickMinStoppedS = 20f;

        private readonly BridgeShared _shared;
        private int _lastQueueFullLogAt = int.MinValue;

        public BridgeRouter(BridgeShared shared)
        {
            _shared = shared;
        }

        /// <summary>
        /// Routes one request. <paramref name="target"/> may carry a query string; it is ignored.
        /// <paramref name="body"/> is the decoded request body ("" for bodiless requests).
        /// Never throws: transports translate an escaped exception into 500 but should not see one.
        /// </summary>
        public BridgeResponse Route(string method, string target, string body)
        {
            try
            {
                return RouteCore(method, target, body);
            }
            catch (Exception ex)
            {
                BridgeLog.Error("unhandled routing error for " + method + " " + target, ex);
                return BridgeResponse.Error(500, "internal_error", ex.Message);
            }
        }

        private BridgeResponse RouteCore(string method, string target, string body)
        {
            // Safety rule (CONTRACTS §1): once an online session is detected, every endpoint
            // returns 503 online_session_active.
            if (_shared.OnlineBlocked)
            {
                return BridgeResponse.Error(503, "online_session_active",
                    "a GTA Online session was detected; the bridge disabled itself");
            }

            string path = NormalizePath(target);
            switch (path)
            {
                case "/state":
                    return RequireMethod(method, "GET", path) ?? HandleState();
                case "/health":
                    return RequireMethod(method, "GET", path) ?? HandleHealth();
                case "/task":
                    return RequireMethod(method, "POST", path) ?? HandleTask(body);
                case "/timescale":
                    return RequireMethod(method, "POST", path) ?? HandleTimescale(body);
                case "/control":
                    return RequireMethod(method, "POST", path) ?? HandleControl(body);
                case "/radio":
                    return RequireMethod(method, "POST", path) ?? HandleRadio(body);
                case "/horn":
                    return RequireMethod(method, "POST", path) ?? HandleHorn(body);
                case "/unstick":
                    return RequireMethod(method, "POST", path) ?? HandleUnstick();
                default:
                    return BridgeResponse.Error(404, "not_found", "unknown path " + path);
            }
        }

        internal static string NormalizePath(string target)
        {
            if (string.IsNullOrEmpty(target))
            {
                return "/";
            }
            int cut = target.IndexOfAny(new[] { '?', '#' });
            string path = cut >= 0 ? target.Substring(0, cut) : target;
            // An absolute-form request-target (RFC 7230 §5.3.2) is legal; reduce it to its path.
            int schemeEnd = path.IndexOf("://", StringComparison.Ordinal);
            if (schemeEnd >= 0)
            {
                int slash = path.IndexOf('/', schemeEnd + 3);
                path = slash >= 0 ? path.Substring(slash) : "/";
            }
            if (path.Length == 0)
            {
                return "/";
            }
            if (path.Length > 1)
            {
                path = path.TrimEnd('/');
                if (path.Length == 0)
                {
                    return "/";
                }
            }
            return path;
        }

        private static BridgeResponse RequireMethod(string actual, string expected, string path)
        {
            if (string.Equals(actual, expected, StringComparison.Ordinal))
            {
                return null;
            }
            return BridgeResponse.Error(405, "method_not_allowed", path + " requires " + expected);
        }

        // ---- GET /state ----------------------------------------------------------------------

        private BridgeResponse HandleState()
        {
            string json = _shared.StateJson;
            if (json == null)
            {
                return BridgeResponse.Error(503, "not_ready",
                    "the first game tick has not completed yet");
            }
            return new BridgeResponse(200, json);
        }

        // ---- GET /health ---------------------------------------------------------------------

        private BridgeResponse HandleHealth()
        {
            bool stale = _shared.MillisSinceLastTick() > 2000;
            var body = new JObject
            {
                ["version"] = WastedBridgeScript.BridgeVersion,
                ["edition"] = _shared.Edition,
                ["tick_hz"] = stale ? 0f : _shared.TickHz,
                ["queue_depth"] = _shared.QueueDepth,
                ["game_fps"] = _shared.GameFps,
                ["online_blocked"] = _shared.OnlineBlocked
            };
            return BridgeResponse.FromJson(200, body);
        }

        // ---- POST /task ----------------------------------------------------------------------

        private BridgeResponse HandleTask(string rawBody)
        {
            JObject body;
            BridgeResponse bad = TryParseBody(rawBody, out body);
            if (bad != null)
            {
                return bad;
            }
            string type = (string)body["type"];
            if (string.IsNullOrEmpty(type))
            {
                return BridgeResponse.Error(400, "invalid_params", "missing \"type\"");
            }
            var p = body["params"] as JObject ?? new JObject();

            TaskRequest req;
            string error, detail;
            if (!TryBuildTaskRequest(type, p, out req, out error, out detail))
            {
                return BridgeResponse.Error(400, error, detail);
            }
            req.Id = _shared.NextTaskId();
            var cmd = new BridgeCommand { Kind = CommandKind.NewTask, Task = req };
            if (!_shared.TryEnqueue(cmd))
            {
                return QueueFull();
            }
            return BridgeResponse.FromJson(202, new JObject { ["task_id"] = req.Id });
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
                    if (!TryStyle(p, "normal", out req.Style, out detail))
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
                    if (!TryStyle(p, "normal", out req.Style, out detail))
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
                    // CONTRACTS v1.9: style/speed_mps are OPTIONAL on follow_entity. Absent means
                    // the v1.9 defaults (TaskEngine.FollowVehicleDefaultStyleName/SpeedMps), which
                    // keep pace with the mission NPC being tailed instead of losing it (the old
                    // hard-coded DrivingStyles.Normal + 15 m/s could not). An explicit value from
                    // the caller always wins. Parsed through the exact same validated path drive_to
                    // uses, so a malformed value fails invalid_params (already-enumerated code) the
                    // same way, never a new code.
                    if (!TryStyle(p, TaskEngine.FollowVehicleDefaultStyleName, out req.Style, out detail))
                    {
                        error = "invalid_params";
                        return false;
                    }
                    if (!TryOptFloat(p, "speed_mps", TaskEngine.FollowVehicleDefaultSpeedMps,
                        out req.SpeedMps))
                    {
                        return Invalid(out error, out detail,
                            "follow_entity speed_mps must be numeric when present");
                    }
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

        private BridgeResponse HandleTimescale(string rawBody)
        {
            JObject body;
            BridgeResponse bad = TryParseBody(rawBody, out body);
            if (bad != null)
            {
                return bad;
            }
            float value;
            if (!TryFloat(body, "value", out value))
            {
                return BridgeResponse.Error(400, "invalid_params",
                    "timescale requires numeric \"value\"");
            }
            float clamped = Math.Min(1.0f, Math.Max(0.1f, value)); // contract: clamp to 0.1–1.0
            return RunOnGameThread(new BridgeCommand
            {
                Kind = CommandKind.SetTimescale,
                TimescaleValue = clamped
            });
        }

        // ---- POST /control -------------------------------------------------------------------

        private BridgeResponse HandleControl(string rawBody)
        {
            JObject body;
            BridgeResponse bad = TryParseBody(rawBody, out body);
            if (bad != null)
            {
                return bad;
            }
            JToken tok = body["enabled"];
            if (tok == null || tok.Type != JTokenType.Boolean)
            {
                return BridgeResponse.Error(400, "invalid_params",
                    "control requires boolean \"enabled\"");
            }
            return RunOnGameThread(new BridgeCommand
            {
                Kind = CommandKind.SetControl,
                ControlEnabled = (bool)tok
            });
        }

        // ---- POST /radio ---------------------------------------------------------------------

        private BridgeResponse HandleRadio(string rawBody)
        {
            JObject body;
            BridgeResponse bad = TryParseBody(rawBody, out body);
            if (bad != null)
            {
                return bad;
            }
            string station = (string)body["station"];
            if (string.IsNullOrEmpty(station))
            {
                return BridgeResponse.Error(400, "invalid_params",
                    "radio requires \"station\" (a name or \"off\")");
            }
            return RunOnGameThread(new BridgeCommand
            {
                Kind = CommandKind.SetRadio,
                RadioStation = station
            });
        }

        // ---- POST /horn ----------------------------------------------------------------------

        private BridgeResponse HandleHorn(string rawBody)
        {
            JObject body;
            BridgeResponse bad = TryParseBody(rawBody, out body);
            if (bad != null)
            {
                return bad;
            }
            float ms;
            if (!TryFloat(body, "ms", out ms))
            {
                return BridgeResponse.Error(400, "invalid_params",
                    "horn requires numeric \"ms\" (1-3000)");
            }
            int clamped = (int)Math.Min(3000f, Math.Max(1f, ms));
            return RunOnGameThread(new BridgeCommand { Kind = CommandKind.Horn, HornMs = clamped });
        }

        // ---- POST /unstick -------------------------------------------------------------------

        private BridgeResponse HandleUnstick()
        {
            // Fast precheck against the cached snapshot (no natives). The game thread re-verifies
            // authoritatively before moving anything, so a stale snapshot can only cause an extra
            // 409, never an unjustified nudge.
            Snapshot snap = _shared.LastSnapshot;
            string why = UnstickBlockReason(snap);
            if (why != null)
            {
                return BridgeResponse.Error(409, "unstick_conditions_not_met", why);
            }
            return RunOnGameThread(new BridgeCommand { Kind = CommandKind.Unstick });
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
        private BridgeResponse RunOnGameThread(BridgeCommand cmd)
        {
            var reply = new CommandReply();
            cmd.Reply = reply;
            if (!_shared.TryEnqueue(cmd))
            {
                return QueueFull();
            }
            if (!reply.Wait(GameThreadWaitMs))
            {
                // Abandon the reply so a late completion cannot write to a dead response.
                reply.Abandon();
                return BridgeResponse.Error(503, "game_thread_stalled",
                    "the game tick did not process the command within "
                    + GameThreadWaitMs + " ms (loading screen or hang)");
            }
            return BridgeResponse.FromJson(reply.StatusCode, reply.Body);
        }

        private BridgeResponse QueueFull()
        {
            // Throttled: a polling harness against a stalled game thread would otherwise write
            // several lines a second for the whole loading screen.
            int now = Environment.TickCount;
            if (unchecked(now - _lastQueueFullLogAt) > 5000)
            {
                _lastQueueFullLogAt = now;
                BridgeLog.Warn("command queue is full (" + _shared.QueueDepth + "/"
                               + BridgeShared.MaxQueueDepth + "); rejecting requests until the game "
                               + "thread drains it — the game is most likely on a loading screen");
            }
            return BridgeResponse.Error(503, "queue_full",
                "the game thread has not drained the command queue (depth "
                + BridgeShared.MaxQueueDepth + "); it is loading or hung");
        }

        private static BridgeResponse TryParseBody(string raw, out JObject parsed)
        {
            parsed = null;
            if (string.IsNullOrWhiteSpace(raw))
            {
                return BridgeResponse.Error(400, "invalid_json", "request body is empty");
            }
            try
            {
                parsed = JToken.Parse(raw) as JObject;
            }
            catch (JsonReaderException ex)
            {
                return BridgeResponse.Error(400, "invalid_json", ex.Message);
            }
            if (parsed == null)
            {
                return BridgeResponse.Error(400, "invalid_json", "request body must be a JSON object");
            }
            return null;
        }

        private static bool Invalid(out string error, out string detail, string message)
        {
            error = "invalid_params";
            detail = message;
            return false;
        }

        /// <summary>
        /// Validates an optional "style" key against the four contract driving styles.
        /// <paramref name="fallbackName"/> is the name substituted when the caller omits the key
        /// entirely — drive_to/wander_drive pass "normal"; follow_entity (v1.9) passes
        /// <see cref="TaskEngine.FollowVehicleDefaultStyleName"/> so a caller who says nothing
        /// still gets a tail that keeps pace with a mission NPC.
        /// </summary>
        private static bool TryStyle(JObject p, string fallbackName, out GTA.VehicleDrivingFlags style,
                                     out string detail)
        {
            string name = OptString(p, "style", fallbackName);
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

        /// <summary>
        /// Like <see cref="TryFloat"/> but the key is OPTIONAL: a missing key succeeds with
        /// <paramref name="fallback"/> (the contract default), while a key that is present but not
        /// a JSON number still fails — same "malformed value" contract as every other typed param,
        /// just with an extra "absent is fine" escape hatch that OptFloat's fallback-on-any-mismatch
        /// behavior does not give us (OptFloat cannot tell "absent" from "wrong type").
        /// </summary>
        private static bool TryOptFloat(JObject o, string name, float fallback, out float value)
        {
            JToken t = o[name];
            if (t == null)
            {
                value = fallback;
                return true;
            }
            if (t.Type != JTokenType.Float && t.Type != JTokenType.Integer)
            {
                value = 0f;
                return false;
            }
            value = (float)t;
            return true;
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
    }
}
