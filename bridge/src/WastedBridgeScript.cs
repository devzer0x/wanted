using System;
using GTA;
using GTA.Math;
using GTA.Native;
using Newtonsoft.Json;
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
        private const int SnapshotErrorLogIntervalMs = 5000;

        private readonly BridgeShared _shared = new BridgeShared();
        private readonly TaskEngine _engine = new TaskEngine();
        private readonly SnapshotBuilder _builder = new SnapshotBuilder();
        private readonly HttpServer _server;

        private long _tick;
        private bool _firstTickDone;
        private bool _onlineLatched;
        private int _tickWindowStart;
        private int _tickWindowCount;
        private int _lastSnapshotErrorAt = int.MinValue;

        public WastedBridgeScript()
        {
            BridgeLog.Init(BaseDirectory);
            BridgeLog.Info("WastedBridge " + BridgeVersion + " starting (SHVDN "
                           + typeof(Script).Assembly.GetName().Version + ")");
            _server = new HttpServer(_shared);
            _server.Start(); // throws loudly on bind failure; SHVDN then aborts this script
            Tick += OnTick;
            Aborted += OnAborted;
        }

        private void OnTick(object sender, EventArgs e)
        {
            if (_onlineLatched)
            {
                RejectQueuedCommands();
                return;
            }

            // Safety rule (CLAUDE.md §5 / CONTRACTS §1): checked at startup and on every tick.
            // NETWORK_IS_SESSION_STARTED is the documented guard ("block your mod if true");
            // NETWORK_IS_GAME_IN_PROGRESS is belt-and-braces. Latched: once tripped, the script
            // stays disabled until the game (and with it the bridge) restarts.
            bool online = Function.Call<bool>(Hash.NETWORK_IS_SESSION_STARTED)
                          || Function.Call<bool>(Hash.NETWORK_IS_GAME_IN_PROGRESS);
            if (online)
            {
                _onlineLatched = true;
                _shared.OnlineBlocked = true;
                BridgeLog.Error("online session detected — bridge disabled; every endpoint now "
                                + "returns 503 online_session_active until restart");
                RejectQueuedCommands();
                return;
            }

            if (!_firstTickDone)
            {
                _firstTickDone = true;
                _shared.Edition = DetectEdition();
                BridgeLog.Info("game file version " + Game.FileVersion
                               + " → edition \"" + _shared.Edition + "\"");
            }

            _tick++;
            MeasureTickRate();

            DrainCommands();

            bool dead = Game.Player.IsDead; // IS_PLAYER_DEAD
            // IS_PLAYER_BEING_ARRESTED both-phase (brief-natives): atArresting=true fires during
            // the hands-up phase, false only once actually busted — OR them so the harness sees
            // the whole arrest.
            int playerHandle = Game.Player.Handle;
            bool arrested = Function.Call<bool>(Hash.IS_PLAYER_BEING_ARRESTED, playerHandle, true)
                            || Function.Call<bool>(Hash.IS_PLAYER_BEING_ARRESTED, playerHandle, false);

            _engine.Update(Game.Player.Character, dead, arrested);

            try
            {
                Snapshot snapshot = _builder.Build(_tick, _engine, _shared.Edition, dead, arrested);
                _shared.LastSnapshot = snapshot;
                _shared.StateJson = JsonConvert.SerializeObject(snapshot);
            }
            catch (Exception ex)
            {
                // Keep serving the last good snapshot; log throttled so a persistent failure
                // (e.g. during a player switch) cannot flood the disk.
                int now = Game.GameTime;
                if (now - _lastSnapshotErrorAt > SnapshotErrorLogIntervalMs)
                {
                    _lastSnapshotErrorAt = now;
                    BridgeLog.Error("snapshot build failed; serving previous snapshot", ex);
                }
            }

            _shared.GameFps = Game.FPS;
            _shared.MarkTickNow();
        }

        private void OnAborted(object sender, EventArgs e)
        {
            _server.Stop();
            BridgeLog.Info("script aborted; bridge shut down");
        }

        // ---- command handling (game thread) --------------------------------------------------

        private void DrainCommands()
        {
            BridgeCommand cmd;
            while (_shared.Commands.TryDequeue(out cmd))
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
                    // SET_PLAYER_CONTROL directly: the SHVDN property setter is [Obsolete] on the
                    // pinned nightly. Flags 0 = plain toggle, nothing cleared or removed.
                    Function.Call(Hash.SET_PLAYER_CONTROL, Game.Player.Handle, cmd.ControlEnabled, 0);
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
            if (!ped.IsInVehicle())
            {
                cmd.Reply.Complete(409, new JObject
                {
                    ["error"] = "not_in_vehicle",
                    ["detail"] = "the horn needs a vehicle"
                });
                return;
            }
            ped.CurrentVehicle.SoundHorn(cmd.HornMs); // START_VEHICLE_HORN
            cmd.Reply.Complete(200, new JObject { ["ms"] = cmd.HornMs });
        }

        private void ApplyUnstick(CommandReply reply)
        {
            // Authoritative re-check on the game thread (the HTTP-side precheck ran against a
            // snapshot that may be a tick stale). Preconditions per CONTRACTS §1: speed ≈ 0 for
            // > 20 s AND a drive task running; nudge ≤ 3 m; always logged.
            Ped ped = Game.Player.Character;
            if (!_engine.IsDriveTaskRunning || !ped.IsInVehicle()
                || _builder.CurrentStoppedForS <= UnstickMinStoppedS)
            {
                reply.Complete(409, new JObject
                {
                    ["error"] = "unstick_conditions_not_met",
                    ["detail"] = "requires a running drive task and > 20 s at standstill"
                });
                return;
            }

            Vehicle veh = ped.CurrentVehicle;
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
            while (_shared.Commands.TryDequeue(out cmd))
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

        private static string DetectEdition()
        {
            // Heuristic: shipping Legacy builds are 1.0.3xxx.x (currently 1.0.3889.0) while
            // Enhanced builds are 1.0.1158.x and climb slowly; 2000 splits the ranges. Only
            // relevant if this script is ever loaded by the SHVDN-Enhanced fork — the official
            // SHVDN this build pins is Legacy-only (RESEARCH.md D1).
            Version v = Game.FileVersion;
            return v.Build >= 2000 ? "legacy" : "enhanced";
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
