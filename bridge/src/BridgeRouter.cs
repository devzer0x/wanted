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
                case "/arsenal":
                    return RequireMethod(method, "POST", path) ?? HandleArsenal(body);
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

        /// <summary>Validates params for all 14 CONTRACTS §1 task types, plus the proposed flee_ped;
        /// no natives involved.</summary>
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
                // --- bridge 1.8.0: fly_to (CONTRACTS §1 proposal; the flight step of the roam
                // goal `go_flying`) --------------------------------------------------------------
                case "fly_to":
                {
                    // drive_to's keys minus `style` (an aircraft has no traffic lights to ignore).
                    // x, y = the target; z = the CRUISE ALTITUDE above sea level that
                    // TaskEngine.IssueFlyTo hands the engine as flightHeight - NOT the ground at
                    // x,y. Validated through the exact same path as drive_to so a fly_to without
                    // a target is a 400 here, never a plane mission at (0,0,0).
                    if (!TryFloat(p, "x", out req.X) || !TryFloat(p, "y", out req.Y)
                        || !TryFloat(p, "z", out req.Z))
                    {
                        return Invalid(out error, out detail, "fly_to requires numeric x, y, z");
                    }
                    if (!TryFloat(p, "speed_mps", out req.SpeedMps))
                    {
                        return Invalid(out error, out detail, "fly_to requires numeric speed_mps");
                    }
                    req.ArriveRadiusM = OptFloat(p, "arrive_radius_m",
                        TaskEngine.FlyToDefaultArriveRadiusM);
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
                // CONTRACTS v1.13: the two phone verbs take NO params — which call is ringing is
                // not something the caller gets to name, because no native exposes the caller's
                // identity. They are grouped with the other no-param verbs deliberately: an
                // unknown key in `params` is ignored here exactly as it is for `stop`, so a
                // harness that grows a param before the bridge does is a no-op, not a 400.
                case "answer_call":
                case "reject_call":
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
                case "fight_ped":
                {
                    // CONTRACTS v1.11 (root-caused from the 2026-09-02 live "carjack victim punched
                    // the agent to death" bug): {handle} only - reuses follow_entity's already-frozen
                    // "handle" wire key. Weapon-class-driven melee-vs-ranged branching happens
                    // bridge-side in TaskEngine so the harness never needs to know the target's
                    // weapon class up front.
                    if (!TryInt(p, "handle", out req.Handle))
                    {
                        return Invalid(out error, out detail, "fight_ped requires integer handle");
                    }
                    // Bridge 1.7.0 (fix-opus-b, T6): optional weapon mode. Absent means "auto",
                    // which is exactly v1.11's behaviour, so no existing caller changes. An
                    // unknown value is a 400 rather than a silent fallback: "unarmed" and "armed"
                    // are the difference between a fist fight and a shooting, and quietly picking
                    // one because the caller misspelled the other is not a safe degradation.
                    req.WeaponMode = OptString(p, "weapon", "auto");
                    if (req.WeaponMode != "auto" && req.WeaponMode != "unarmed"
                        && req.WeaponMode != "armed")
                    {
                        return Invalid(out error, out detail,
                            "weapon must be \"auto\", \"unarmed\" or \"armed\"");
                    }
                    return true;
                }
                case "flee_ped":
                {
                    // The on-foot counterpart of fight_ped (TASK_SMART_FLEE_PED - see
                    // TaskEngine.StartFleePed), validated identically and reusing the same frozen
                    // "handle" wire key. PROPOSED for CONTRACTS v1.14, not in v1.13: the harness
                    // cannot post it until brain/schemas.py and the action catalog list it, and
                    // those are another package's files. Accepting it here is what makes the
                    // bridge half of that change deployable and testable on its own.
                    if (!TryInt(p, "handle", out req.Handle))
                    {
                        return Invalid(out error, out detail, "flee_ped requires integer handle");
                    }
                    return true;
                }
                // --- bridge 1.7.0 (fix-opus-b, T6) ------------------------------------------
                case "shoot_at":
                case "drive_by":
                {
                    // Both are target-explicit and both reuse follow_entity's already-frozen
                    // "handle" wire key. WHICH WEAPON is deliberately NOT a parameter: it is
                    // chosen bridge-side from the range at task start (WeaponState.SelectForRange
                    // / SelectForDriveBy), read fresh, because the harness's snapshot is up to a
                    // poll period old - the same reasoning fight_ped already documents for
                    // reading the target's weapon class.
                    if (!TryInt(p, "handle", out req.Handle))
                    {
                        return Invalid(out error, out detail, type + " requires integer handle");
                    }
                    req.DurationS = OptFloat(p, "duration_s", type == "drive_by" ? 12f : 8f);
                    if (req.DurationS <= 0f || req.DurationS > 60f)
                    {
                        return Invalid(out error, out detail,
                            "duration_s must be between 0 and 60 seconds");
                    }
                    return true;
                }
                // --- bridge 1.9.0 --------------------------------------------------------------
                case "throw_at":
                {
                    // Target-explicit, same frozen "handle" wire key as shoot_at; the handle
                    // may be a ped OR a vehicle (a parked car is the honest grenade target).
                    // WHICH throwable is chosen bridge-side (WeaponState.SelectThrowable) from
                    // what has rounds at task start, for the reason shoot_at documents.
                    if (!TryInt(p, "handle", out req.Handle))
                    {
                        return Invalid(out error, out detail, "throw_at requires integer handle");
                    }
                    int count;
                    JToken countToken = p["count"];
                    if (countToken == null)
                    {
                        count = 1;
                    }
                    else if (!TryInt(p, "count", out count) || count < 1 || count > 5)
                    {
                        return Invalid(out error, out detail, "count must be an integer 1..5");
                    }
                    req.Count = count;
                    return true;
                }
                case "enter_vehicle_seat":
                {
                    if (!TryInt(p, "handle", out req.Handle))
                    {
                        return Invalid(out error, out detail,
                            "enter_vehicle_seat requires integer handle");
                    }
                    if (!TryInt(p, "seat", out req.Seat))
                    {
                        req.Seat = 2;   // rear-right: where the game's own cab AI seats a player
                    }
                    if (req.Seat < 0 || req.Seat > 2)
                    {
                        // The driver's seat is NOT reachable from here (see TaskRequest.Seat):
                        // "get in and drive" is enter_nearest_vehicle, which has the "nicer"
                        // chooser and the seat verification that belong with driving.
                        return Invalid(out error, out detail,
                            "seat must be 0 (front passenger), 1 (rear-left) or 2 (rear-right); "
                            + "the driver's seat is enter_nearest_vehicle");
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

        // ---- POST /arsenal (1.9.0) -------------------------------------------------------------

        /// <summary>
        /// Body `{"on": bool, "ttl_s": 30–300}` (ttl_s optional, default ArsenalState.DefaultTtlMs).
        /// The HTTP thread only validates and clamps; the grant/clear and every 409 reason are
        /// decided on the game thread (WastedBridgeScript.ApplyArsenal) against fresh reads.
        /// </summary>
        private BridgeResponse HandleArsenal(string rawBody)
        {
            JObject body;
            BridgeResponse bad = TryParseBody(rawBody, out body);
            if (bad != null)
            {
                return bad;
            }
            JToken onToken = body["on"];
            if (onToken == null || onToken.Type != JTokenType.Boolean)
            {
                return BridgeResponse.Error(400, "invalid_params",
                    "arsenal requires boolean \"on\"");
            }
            bool on = (bool)onToken;
            float ttlS = OptFloat(body, "ttl_s", ArsenalState.DefaultTtlMs / 1000f);
            if (float.IsNaN(ttlS) || ttlS <= 0f)
            {
                return BridgeResponse.Error(400, "invalid_params", "ttl_s must be positive");
            }
            int ttlMs = (int)Math.Max(ArsenalState.MinTtlMs,
                Math.Min(ArsenalState.MaxTtlMs, ttlS * 1000f));
            return RunOnGameThread(new BridgeCommand
            {
                Kind = CommandKind.Arsenal,
                ArsenalOn = on,
                ArsenalTtlMs = ttlMs
            });
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
            // On foot the only precondition is that he really is standing still: there is no
            // drive task to require, and a wedged pedestrian is exactly the case this grew to
            // cover (v1.8.0). The game thread re-checks authoritatively.
            if (snap.Vehicle == null)
            {
                // The snapshot carries no ped stationary clock, and adding one to the wire would
                // be a contract change for a value only this check wants. The game thread holds
                // it (`SnapshotBuilder.CurrentPedStoppedForS`) and re-checks authoritatively, so
                // the honest precheck here is simply "he is on foot": at worst that costs one
                // round trip to a 409, never an unjustified nudge.
                return null;
            }
            LastTaskDto task = snap.LastTask;
            bool driveRunning = task != null && task.Status == "running"
                                && (task.Type == "drive_to" || task.Type == "wander_drive");
            if (!driveRunning)
            {
                return "no drive task is running";
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
