using System;
using GTA;
using GTA.Math;
using GTA.Native;
using Newtonsoft.Json.Linq;

namespace WastedBridge
{
    /// <summary>
    /// The single GTA.Script of project WANTED (CONTRACTS.md §1). The Tick handler — the only
    /// place natives run — drains the command queue, updates the task engine, and republishes the
    /// /state snapshot; the HttpServer serves it from background threads.
    /// </summary>
    public sealed class WastedBridgeScript : Script
    {
        public const string BridgeVersion = "1.0.0";

        private const float UnstickMinStoppedS = 20f;
        private const float UnstickNudgeBackM = 2.5f;   // total displacement stays ≤ 3 m (contract)
        private const float UnstickNudgeUpM = 0.25f;
        private const int ErrorLogIntervalMs = 5000;

        private readonly BridgeShared _shared = new BridgeShared();
        private readonly TaskEngine _engine = new TaskEngine();
        private readonly SnapshotBuilder _builder = new SnapshotBuilder();
        private readonly EditionDetector _edition = new EditionDetector();
        private readonly HttpServer _server;

        private long _tick;
        private bool _firstTickDone;
        private bool _editionFailureLogged;
        private bool _waitingForPed;
        private bool _onlineLatched;
        private int _tickWindowStart;
        private int _tickWindowCount;
        private int _lastSnapshotErrorAt = int.MinValue;
        private int _lastTickErrorAt = int.MinValue;
        private int _lastEngineErrorAt = int.MinValue;
        private long _tickErrorCount;

        public WastedBridgeScript()
        {
            string logPath = BridgeLog.Init(BaseDirectory);
            Diagnostics.LogStartupBlock(BaseDirectory, logPath);

            string drift = DrivingStyles.Verify();
            if (drift != null)
            {
                BridgeLog.Warn("driving-style bitfields drifted from CONTRACTS D3: " + drift
                               + "— re-check src/DrivingStyles.cs against the pinned SHVDN nightly");
            }

            // Subscribe Aborted before starting anything that owns an OS resource: if construction
            // fails later, SHVDN still calls Aborted and the listener is released. Tick is
            // subscribed last, once every field it touches exists.
            Aborted += OnAborted;
            _server = new HttpServer(_shared);
            _server.Start(); // never throws; retries from the tick loop if it could not bind
            Tick += OnTick;
        }

        private void OnTick(object sender, EventArgs e)
        {
            // SHVDN aborts a script whose Tick handler throws, and an aborted bridge is a dead
            // stream. Nothing below is allowed to escape: one bad frame must cost one frame.
            try
            {
                TickCore();
            }
            catch (Exception ex)
            {
                _tickErrorCount++;
                int now = Environment.TickCount; // never a native: natives may be what just failed
                if (unchecked(now - _lastTickErrorAt) > ErrorLogIntervalMs)
                {
                    _lastTickErrorAt = now;
                    BridgeLog.Error("tick failed (" + _tickErrorCount + " total since load); the "
                                    + "bridge keeps ticking and serves the last good snapshot", ex);
                }
            }
        }

        private void TickCore()
        {
            _server.EnsureStarted();

            if (_onlineLatched)
            {
                RejectQueuedCommands();
                return;
            }

            // Safety rule (CLAUDE.md §5 / CONTRACTS §1): checked at startup and on every tick.
            // NETWORK_IS_SESSION_STARTED is the documented guard ("block your mod if true");
            // NETWORK_IS_GAME_IN_PROGRESS is belt-and-braces. Latched: once tripped, the script
            // stays disabled until the game (and with it the bridge) restarts.
            bool sessionStarted = Function.Call<bool>(Hash.NETWORK_IS_SESSION_STARTED);
            bool gameInProgress = Function.Call<bool>(Hash.NETWORK_IS_GAME_IN_PROGRESS);
            if (sessionStarted || gameInProgress)
            {
                _onlineLatched = true;
                _shared.OnlineBlocked = true;
                BridgeLog.Error("online session detected (NETWORK_IS_SESSION_STARTED="
                                + sessionStarted + ", NETWORK_IS_GAME_IN_PROGRESS=" + gameInProgress
                                + ") — bridge disabled; every endpoint now returns 503 "
                                + "online_session_active until the game restarts");
                RejectQueuedCommands();
                return;
            }

            // Edition detection retries until it succeeds instead of latching on the first attempt:
            // Game.FileVersion is read while the game may still be on a loading screen, and one
            // early throw used to pin edition:"unknown" for the whole session (CONTRACTS v1.2
            // allows "unknown", but only as a transient state).
            if (_edition.ShouldAttempt(Environment.TickCount))
            {
                AttemptEditionDetection();
            }

            _tick++;
            MeasureTickRate();

            DrainCommands();

            // SHVDN starts ticking at the main menu / loading screen, before the player ped
            // exists. Reading world state then produces nothing but noise, so tick honestly
            // (/health stays live, /state stays 503 not_ready) and wait.
            Ped playerPed = Game.Player.Character;
            if (playerPed == null || !playerPed.Exists())
            {
                if (!_waitingForPed)
                {
                    _waitingForPed = true;
                    BridgeLog.Info("no player ped yet (main menu or loading screen) — ticking, "
                                   + "/health is live, /state stays 503 not_ready until it appears");
                }
                _shared.GameFps = Game.FPS;
                _shared.MarkTickNow();
                return;
            }
            if (_waitingForPed)
            {
                _waitingForPed = false;
                BridgeLog.Info("player ped is live; publishing /state again");
            }

            // Logged on the first tick with a live ped so the block reports real world state
            // instead of a screenful of loading-screen failures.
            if (!_firstTickDone)
            {
                _firstTickDone = true;
                Diagnostics.LogFirstTickBlock(_shared.Edition, sessionStarted, gameInProgress,
                    _server.Description);
            }

            bool dead = Game.Player.IsDead; // IS_PLAYER_DEAD
            // IS_PLAYER_BEING_ARRESTED both-phase (brief-natives): atArresting=true fires during
            // the hands-up phase, false only once actually busted — OR them so the harness sees
            // the whole arrest.
            int playerHandle = Game.Player.Handle;
            bool arrested = Function.Call<bool>(Hash.IS_PLAYER_BEING_ARRESTED, playerHandle, true)
                            || Function.Call<bool>(Hash.IS_PLAYER_BEING_ARRESTED, playerHandle, false);

            try
            {
                _engine.Update(playerPed, dead, arrested);
            }
            catch (Exception ex)
            {
                // A task-completion check must never cost us the snapshot: /state is what the
                // harness steers by, and a stale last_task is far less harmful than a stale world.
                int now = Environment.TickCount;
                if (unchecked(now - _lastEngineErrorAt) > ErrorLogIntervalMs)
                {
                    _lastEngineErrorAt = now;
                    BridgeLog.Error("task engine update failed; task state may lag", ex);
                }
            }

            try
            {
                Snapshot snapshot = _builder.Build(_tick, _engine, _shared.Edition, dead, arrested);
                _shared.LastSnapshot = snapshot;
                // Same method the contract-sample tool calls on this compiled assembly, so
                // bridge/contract-samples/*.json are exactly these bytes (nulls included).
                _shared.StateJson = SnapshotJson.Serialize(snapshot);
            }
            catch (Exception ex)
            {
                // Keep serving the last good snapshot; log throttled so a persistent failure
                // (e.g. during a player switch) cannot flood the disk.
                int now = Environment.TickCount;
                if (unchecked(now - _lastSnapshotErrorAt) > ErrorLogIntervalMs)
                {
                    _lastSnapshotErrorAt = now;
                    BridgeLog.Error(_shared.StateJson == null
                        ? "snapshot build failed and no snapshot has EVER been published — "
                          + "/state will keep returning 503 not_ready until this is fixed"
                        : "snapshot build failed; serving previous snapshot", ex);
                }
            }

            _shared.GameFps = Game.FPS;
            _shared.MarkTickNow();
        }

        private void OnAborted(object sender, EventArgs e)
        {
            // Runs on script reload as well as game shutdown. Releasing the port here is what
            // stops the reloaded instance from finding 7777 already taken.
            if (_server != null)
            {
                _server.Stop();
            }
            BridgeLog.Info("script aborted; bridge shut down");
        }

        // ---- command handling (game thread) --------------------------------------------------

        private void DrainCommands()
        {
            BridgeCommand cmd;
            while (_shared.TryDequeue(out cmd))
            {
                try
                {
                    Apply(cmd);
                }
                catch (Exception ex)
                {
                    BridgeLog.Error("command " + cmd.Kind + " failed", ex);
                    if (cmd.Reply != null)
                    {
                        cmd.Reply.Complete(500, new JObject
                        {
                            ["error"] = "internal_error",
                            ["detail"] = ex.Message
                        });
                    }
                }
            }
        }

        private void Apply(BridgeCommand cmd)
        {
            switch (cmd.Kind)
            {
                case CommandKind.NewTask:
                    _engine.Start(cmd.Task);
                    break;

                case CommandKind.SetTimescale:
                    Game.TimeScale = cmd.TimescaleValue; // SET_TIME_SCALE, clamped 0.1–1.0 upstream
                    cmd.Reply.Complete(200, new JObject { ["value"] = cmd.TimescaleValue });
                    break;

                case CommandKind.SetControl:
                    // SET_PLAYER_CONTROL via the typed wrapper (the CanControlCharacter setter is
                    // [Obsolete] on the pinned nightly). Flags None = plain toggle: no tasks
                    // cleared, no fires/explosions/projectiles removed.
                    Game.Player.SetControlState(cmd.ControlEnabled, SetPlayerControlFlags.None);
                    cmd.Reply.Complete(200, new JObject { ["enabled"] = cmd.ControlEnabled });
                    break;

                case CommandKind.SetRadio:
                    ApplyRadio(cmd.RadioStation);
                    cmd.Reply.Complete(200, new JObject { ["station"] = cmd.RadioStation });
                    break;

                case CommandKind.Horn:
                    ApplyHorn(cmd);
                    break;

                case CommandKind.Unstick:
                    ApplyUnstick(cmd.Reply);
                    break;
            }
        }

        private static void ApplyRadio(string station)
        {
            if (station == "off")
            {
                Game.RadioStation = RadioStation.RadioOff;
                return;
            }
            // Accept SHVDN enum names in any casing/spacing ("Los Santos Rock Radio" →
            // LosSantosRockRadio); anything else is passed through to SET_RADIO_TO_STATION_NAME,
            // which takes the game's internal audio names (e.g. "RADIO_01_CLASS_ROCK").
            string key = NormalizeStationName(station);
            foreach (RadioStation candidate in Enum.GetValues(typeof(RadioStation)))
            {
                if (NormalizeStationName(candidate.ToString()) == key)
                {
                    Game.RadioStation = candidate;
                    return;
                }
            }
            Function.Call(Hash.SET_RADIO_TO_STATION_NAME, station);
        }

        private static string NormalizeStationName(string name)
        {
            var sb = new System.Text.StringBuilder(name.Length);
            for (int i = 0; i < name.Length; i++)
            {
                char c = name[i];
                if (char.IsLetterOrDigit(c))
                {
                    sb.Append(char.ToLowerInvariant(c));
                }
            }
            return sb.ToString();
        }

        private static void ApplyHorn(BridgeCommand cmd)
        {
            Ped ped = Game.Player.Character;
            Vehicle veh = ped != null && ped.IsInVehicle() ? ped.CurrentVehicle : null;
            if (veh == null || !veh.Exists())
            {
                cmd.Reply.Complete(409, new JObject
                {
                    ["error"] = "not_in_vehicle",
                    ["detail"] = "the horn needs a vehicle"
                });
                return;
            }
            veh.SoundHorn(cmd.HornMs); // START_VEHICLE_HORN
            cmd.Reply.Complete(200, new JObject { ["ms"] = cmd.HornMs });
        }

        private void ApplyUnstick(CommandReply reply)
        {
            // Authoritative re-check on the game thread (the HTTP-side precheck ran against a
            // snapshot that may be a tick stale). Preconditions per CONTRACTS §1: speed ≈ 0 for
            // > 20 s AND a drive task running; nudge ≤ 3 m; always logged.
            Ped ped = Game.Player.Character;
            Vehicle veh = ped != null && ped.IsInVehicle() ? ped.CurrentVehicle : null;
            if (!_engine.IsDriveTaskRunning || veh == null || !veh.Exists()
                || _builder.CurrentStoppedForS <= UnstickMinStoppedS)
            {
                reply.Complete(409, new JObject
                {
                    ["error"] = "unstick_conditions_not_met",
                    ["detail"] = "requires a running drive task and > 20 s at standstill"
                });
                return;
            }

            Vector3 before = veh.Position;
            Vector3 offset = veh.ForwardVector * -UnstickNudgeBackM;
            offset.Z += UnstickNudgeUpM;
            float distance = offset.Length();

            // The ONLY positional write in the bridge (CLAUDE.md §5 narrow exception).
            veh.Position = before + offset;

            BridgeLog.Warn("UNSTICK: nudged vehicle " + veh.Handle + " by "
                           + distance.ToString("F2") + " m (stopped for "
                           + _builder.CurrentStoppedForS.ToString("F1") + " s at "
                           + before.X.ToString("F1") + "," + before.Y.ToString("F1") + ","
                           + before.Z.ToString("F1") + ")");
            reply.Complete(200, new JObject
            {
                ["moved"] = true,
                ["distance_m"] = (float)Math.Round(distance, 2)
            });
        }

        private void RejectQueuedCommands()
        {
            BridgeCommand cmd;
            while (_shared.TryDequeue(out cmd))
            {
                if (cmd.Reply != null)
                {
                    cmd.Reply.Complete(503, new JObject
                    {
                        ["error"] = "online_session_active",
                        ["detail"] = "a GTA Online session was detected; the bridge disabled itself"
                    });
                }
            }
        }

        // ---- misc ----------------------------------------------------------------------------

        /// <summary>
        /// One detection attempt on the game thread. The native read lives here; the retry policy
        /// and the version → edition rule live in <see cref="EditionDetector"/>, which is pure and
        /// therefore provable off-server (bridge/tools/offline-checks).
        /// </summary>
        private void AttemptEditionDetection()
        {
            Version version = null;
            Exception failure = null;
            try
            {
                version = Game.FileVersion;
            }
            catch (Exception ex)
            {
                failure = ex;
            }

            string detected = _edition.Accept(Environment.TickCount, version);
            _shared.Edition = _edition.Edition;

            if (detected != null)
            {
                BridgeLog.Info("game build " + version + " -> edition \"" + detected + "\""
                               + (_edition.Attempts > 1
                                   ? " (resolved on attempt " + _edition.Attempts + ")"
                                   : ""));
                return;
            }

            // Report the first failure, then stay quiet: retries continue every 5 s and a game
            // still on its loading screen must not fill the log with the same line.
            if (!_editionFailureLogged)
            {
                _editionFailureLogged = true;
                BridgeLog.Warn("could not read Game.FileVersion; edition reports \"unknown\" and "
                               + "detection retries every " + EditionDetector.RetryIntervalMs
                               + " ms until it succeeds", failure);
            }
        }

        private void MeasureTickRate()
        {
            int now = Environment.TickCount;
            if (_tickWindowCount == 0)
            {
                _tickWindowStart = now;
            }
            _tickWindowCount++;
            int elapsed = unchecked(now - _tickWindowStart);
            if (elapsed >= 1000)
            {
                _shared.TickHz = _tickWindowCount * 1000f / elapsed;
                _tickWindowCount = 0;
            }
        }
    }
}
