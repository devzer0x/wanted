using System;
using System.Collections.Generic;
using GTA;
using GTA.Chrono;
using GTA.Math;
using GTA.Native;

namespace WastedBridge
{
    /// <summary>
    /// Game-thread-only construction of the /state snapshot (CONTRACTS.md §1). Reads game state
    /// via SHVDN wrappers/natives each tick; the script serializes the result once and publishes
    /// the JSON string for HTTP threads.
    /// </summary>
    internal sealed class SnapshotBuilder
    {
        private const float NearbyVehicleRadiusM = 80f;
        private const float NearbyPedRadiusM = 50f;
        private const int NearbyTopN = 8;
        // CONTRACTS v1.8: mission.route_blips is bounded so a map full of routed blips can never
        // grow the snapshot (and the harness's prompt) without limit.
        private const int MaxRouteBlips = 5;
        // CONTRACTS v1.11: mission.entity_blips is bounded the same way, nearest first.
        private const int MaxEntityBlips = 8;
        // v1.11 THROTTLE (load-bearing, see FindEntityBlipsSafe): resolving an entity for a blip via
        // Blip.Entity runs a native per candidate blip. The existing objective-blip pass sorts blips
        // into "routed" vs not BEFORE running any per-blip native specifically to keep that native
        // off every map blip at the ~47 Hz snapshot cadence; entity_blips has no such cheap
        // pre-filter (a plain crew-member dot is not routed), so instead the whole pass is rate
        // limited to ~4 Hz and the previous result is served on the ticks in between.
        private const int EntityBlipThrottleMs = 250;
        // v1.11: the combined mission-script-name + retry-in-flight scan is a full
        // SCRIPT_THREAD_ITERATOR walk over every running script thread - the same cost class as
        // the blip-pool scans above (unbounded thread count, unlike the tightly-radius-capped
        // per-ped threat checks below) - so it gets the same ~4 Hz throttle rather than running
        // unconditionally every tick just because "free" retry detection is tempting.
        private const int MissionScriptThrottleMs = 250;
        // "speed ≈ 0" threshold for stopped_for_s and the /unstick precondition.
        private const float StoppedSpeedThresholdMps = 0.2f;

        private int _stoppedVehicleHandle;
        private int _stoppedSinceMs = -1;
        // CONTRACTS v1.12 per-tick interior memory. Same shape as the stopped_for_s memory above,
        // and here for the same reason: only the game thread sees every tick, so "how long has he
        // been in this room" and "where was he standing the tick before the door" are measured
        // here or not at all. 0 == outdoors (no InteriorProxy handle is 0).
        private int _interiorHandle;
        private int _interiorSinceMs = -1;
        // The most recent position at which he was OUTDOORS. On the tick an outdoor->indoor
        // transition is detected this still holds the PREVIOUS tick's position, which is the one
        // outside the door - it is only refreshed on ticks where he is actually outdoors.
        private Vector3 _outdoorPos;
        private bool _haveOutdoorPos;
        private Vector3 _lastOutdoor;
        private bool _haveLastOutdoor;
        private int _lastInteriorErrorAt = int.MinValue;
        private int _lastBlipErrorAt = int.MinValue;
        private int _lastStartsErrorAt = int.MinValue;
        private int _lastScriptErrorAt = int.MinValue;
        private int _lastEntityBlipErrorAt = int.MinValue;
        private List<EntityBlipDto> _lastEntityBlips;
        private int _lastEntityBlipsAt = int.MinValue;
        private MissionScriptScan _lastMissionScriptScan = new MissionScriptScan();
        private int _lastMissionScriptScanAt = int.MinValue;

        /// <summary>stopped_for_s of the current vehicle as of the last Build call.</summary>
        public float CurrentStoppedForS { get; private set; }

        public Snapshot Build(long tick, TaskEngine engine, string edition,
                              bool playerDead, bool playerArrested)
        {
            // CONTRACTS v1.10 item 5 (protagonist read verification): Game.Player.Character is
            // fetched FRESH here every Build() call, never cached across ticks or stashed on this
            // (long-lived) SnapshotBuilder instance. The player ped's handle changes on a
            // character switch (Michael/Franklin/Trevor) and on respawn, so caching it here would
            // eventually serve a stale/wrong ped for player.pos, player.protagonist, etc. — this is
            // the exact invariant that a live stream caught failing (protagonist read "franklin"
            // during a Michael mission) before this comment existed. WastedBridgeScript.TickCore()
            // does its own independent fresh Game.Player.Character fetch each tick for the task
            // engine; the two are deliberately separate reads, not a shared cached reference.
            Player player = Game.Player;
            Ped ped = player.Character;
            if (ped == null || !ped.Exists())
            {
                // Normal during a load transition or a character switch; the caller keeps serving
                // the previous snapshot and logs this (throttled) rather than publishing garbage.
                throw new InvalidOperationException(
                    "the player ped does not exist yet (loading screen or character switch)");
            }
            Vector3 pos = ped.Position;
            bool inVehicle = ped.IsInVehicle();
            Vehicle veh = inVehicle ? ped.CurrentVehicle : null;
            // One blip pass produces both mission.objective_blip and mission.route_blips: they are
            // the same scan, and running it twice would double the per-frame cost for nothing.
            ObjectiveScan objective = FindObjectiveBlipSafe(pos);
            // Read once, reused for both mission.active and the mission.script gate below (v1.10
            // item 3 only ever emits a script name while the mission flag is set).
            bool missionActive = Game.IsMissionActive;
            // v1.11: one script-thread walk for both mission.script and mission.retry_in_flight -
            // the latter must NOT be gated on missionActive (a mission_repeat_controller thread can
            // plausibly still be running in the window right after the mission flag drops), so the
            // combined scan itself is unconditional; only the Script field is filtered by
            // missionActive below, same rule as before.
            MissionScriptScan scriptScan = FindMissionScriptScanSafe();
            NearbyDto nearby = BuildNearby(ped, veh, out ThreatDto threat);

            var snapshot = new Snapshot
            {
                Ts = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ"),
                Tick = tick,
                Player = new PlayerDto
                {
                    Pos = ToDto(pos),
                    Heading = ped.Heading,
                    Health = ped.Health,
                    MaxHealth = ped.MaxHealth,
                    Armor = ped.Armor,
                    Wanted = player.Wanted.WantedLevel,
                    Cash = player.Money,
                    Dead = playerDead,
                    Arrested = playerArrested,
                    InVehicle = inVehicle,
                    ControlEnabled = player.CanControlCharacter,
                    Protagonist = ProtagonistName(ped),
                    // v1.11: IS_PLAYER_SWITCH_IN_PROGRESS(), no SHVDN wrapper (verified absent from
                    // lib/Docs/ScriptHookVDotNet3.xml; hash confirmed present in GTA.Native.Hash via
                    // MetadataLoadContext), no arguments - raw Function.Call.
                    SwitchInProgress = Function.Call<bool>(Hash.IS_PLAYER_SWITCH_IN_PROGRESS),
                    // v1.12. TrackInteriorSafe also maintains LastOutdoor, so it must run before
                    // the field below reads it - object-initializer members are evaluated in
                    // source order, which is what makes this safe.
                    Interior = TrackInteriorSafe(ped, pos),
                    LastOutdoor = _haveLastOutdoor ? ToDto(_lastOutdoor) : null
                },
                Vehicle = BuildVehicle(veh),
                Location = new LocationDto
                {
                    Street = World.GetStreetName(pos),
                    Zone = World.GetZoneLocalizedName(pos)
                },
                World = new WorldDto
                {
                    Clock = GameClock.Hour.ToString("D2") + ":" + GameClock.Minute.ToString("D2"),
                    // World.Weather wraps GET_PREV_WEATHER_TYPE_HASH_NAME, which despite the name
                    // is the weather in force right now (World.NextWeather is the one coming).
                    Weather = World.Weather.ToString().ToUpperInvariant(),
                    Timescale = Game.TimeScale
                },
                Mission = new MissionDto
                {
                    Active = missionActive,                        // GET_MISSION_FLAG
                    RandomEventActive = Game.IsRandomEventActive,  // GET_RANDOM_EVENT_FLAG
                    CutsceneActive = Game.IsCutsceneActive,        // IS_CUTSCENE_ACTIVE
                    ObjectiveBlip = objective.Objective,
                    Starts = FindMissionStartsSafe(ped),
                    RouteBlips = objective.RouteBlips,
                    // v1.10 item 3: only ever non-null while active - filtered here rather than
                    // inside the (now-unconditional) scan, see the comment above.
                    Script = missionActive ? scriptScan.Script : null,
                    EntityBlips = FindEntityBlipsSafe(pos, missionActive),
                    RetryInFlight = scriptScan.RetryInFlight
                },
                Nearby = nearby,
                Threat = threat,
                LastTask = engine.ToDto(),
                Bridge = new BridgeInfoDto
                {
                    Version = WastedBridgeScript.BridgeVersion,
                    Edition = edition
                }
            };
            return snapshot;
        }

        private VehicleDto BuildVehicle(Vehicle veh)
        {
            if (veh == null || !veh.Exists())
            {
                CurrentStoppedForS = 0f;
                _stoppedSinceMs = -1;
                _stoppedVehicleHandle = 0;
                return null;
            }

            float speed = veh.Speed;
            if (speed >= StoppedSpeedThresholdMps)
            {
                _stoppedSinceMs = -1;
                _stoppedVehicleHandle = 0;
                CurrentStoppedForS = 0f;
            }
            else
            {
                if (_stoppedSinceMs < 0 || _stoppedVehicleHandle != veh.Handle)
                {
                    _stoppedSinceMs = Game.GameTime;
                    _stoppedVehicleHandle = veh.Handle;
                }
                CurrentStoppedForS = (Game.GameTime - _stoppedSinceMs) / 1000f;
            }

            return new VehicleDto
            {
                Handle = veh.Handle,
                Model = VehicleModelName(veh.Model),
                DisplayName = veh.LocalizedName,
                Class = veh.ClassType.ToString(),
                Speed = speed,
                Health = veh.HealthFloat,
                UpsideDown = veh.IsUpsideDown,
                InWater = veh.IsInWater,
                StoppedForS = CurrentStoppedForS
            };
        }

        /// <summary>
        /// CONTRACTS v1.12 <c>player.interior</c> + <c>player.last_outdoor</c>, behind its own
        /// guard.
        ///
        /// <c>Entity.CurrentInteriorProxy</c> is the SHVDN wrapper over the game's own
        /// entity->CInteriorProxy lookup, and like every SHVDN wrapper that reaches into game
        /// memory it is a candidate to break on a game update. Guarded exactly the way
        /// <see cref="FindObjectiveBlipSafe"/> is: one field degrading is an acceptable loss,
        /// freezing all of /state is not.
        ///
        /// WHAT IT SERVES ON FAILURE, and why it is not <c>null</c>: null on the wire means "he is
        /// OUTDOORS", which is a positive claim this method cannot make when the lookup just
        /// threw. So it repeats the last value it actually measured (the same thing
        /// <see cref="FindEntityBlipsSafe"/> does on a throttled tick) and logs, throttled. A
        /// persistent failure therefore degrades to "the last thing we knew" plus a loud log,
        /// never to a confident lie in either direction.
        /// </summary>
        private InteriorDto TrackInteriorSafe(Ped ped, Vector3 pos)
        {
            try
            {
                return TrackInterior(ped, pos);
            }
            catch (Exception ex)
            {
                int now = Environment.TickCount;
                if (unchecked(now - _lastInteriorErrorAt) > 5000)
                {
                    _lastInteriorErrorAt = now;
                    BridgeLog.Error("interior lookup failed (Entity.CurrentInteriorProxy); "
                                    + "player.interior repeats its last measured value and "
                                    + "player.last_outdoor stops updating", ex);
                }
                return _lastInterior;
            }
        }

        private InteriorDto _lastInterior;

        private InteriorDto TrackInterior(Ped ped, Vector3 pos)
        {
            // Null == not in an interior, per the pinned nightly's own doc XML. There is no
            // "unknown" third state in the wrapper and the contract does not model one.
            InteriorProxy proxy = ped.CurrentInteriorProxy;
            int handle = proxy == null ? 0 : proxy.Handle;

            if (handle != _interiorHandle)
            {
                // The id CHANGED - this is the tick since_s is measured from. Interior A straight
                // into interior B (adjacent rooms with their own proxies) resets since_s and does
                // NOT touch last_outdoor: the last outdoor->indoor crossing is still the one into
                // A, which is where the door he came through is.
                if (_interiorHandle == 0 && handle != 0 && _haveOutdoorPos)
                {
                    // Outdoors -> indoors. _outdoorPos is still the PREVIOUS tick's position (it is
                    // only refreshed below, on outdoor ticks), i.e. the last place he stood outside
                    // the door. That is the whole reason this is measured bridge-side: a 2-4 Hz
                    // poll cannot see the tick before the transition.
                    _lastOutdoor = _outdoorPos;
                    _haveLastOutdoor = true;
                }
                _interiorHandle = handle;
                _interiorSinceMs = Game.GameTime;
            }

            if (handle == 0)
            {
                _outdoorPos = pos;
                _haveOutdoorPos = true;
                _lastInterior = null;
                return null;
            }

            _lastInterior = new InteriorDto
            {
                Id = handle,
                SinceS = (Game.GameTime - _interiorSinceMs) / 1000f
            };
            return _lastInterior;
        }

        /// <summary>CONTRACTS v1.7: who the player currently is, from the ped model.</summary>
        private static string ProtagonistName(Ped ped)
        {
            int hash = ped.Model.Hash;
            if (hash == unchecked((int)PedHash.Michael)) return "michael";
            if (hash == unchecked((int)PedHash.Franklin)) return "franklin";
            if (hash == unchecked((int)PedHash.Trevor)) return "trevor";
            return "unknown";
        }

        private static string ProtagonistForBlipColor(BlipColor c)
        {
            if (c == BlipColor.Michael) return "michael";
            if (c == BlipColor.Franklin) return "franklin";
            if (c == BlipColor.Trevor) return "trevor";
            return null;
        }

        /// <summary>
        /// CONTRACTS v1.7: mission-start markers (the M/F/T letters) currently on the map. They are
        /// identified by the game's own per-protagonist blip COLOURS (BlipColor.Michael / Franklin /
        /// Trevor), which are documented enum members - not by sprite ids, which are not. Nearest
        /// first, capped at 8. Same memory-scan caveat as FindObjectiveBlip.
        /// </summary>
        private List<MissionStartDto> FindMissionStartsSafe(Ped ped)
        {
            try
            {
                return FindMissionStarts(ped);
            }
            catch (Exception ex)
            {
                int now = Environment.TickCount;
                if (unchecked(now - _lastStartsErrorAt) > 5000)
                {
                    _lastStartsErrorAt = now;
                    BridgeLog.Error("mission-start scan failed (World.GetAllBlips memory scan); "
                                    + "mission.starts stays empty", ex);
                }
                return new List<MissionStartDto>();
            }
        }

        private static List<MissionStartDto> FindMissionStarts(Ped ped)
        {
            var found = new List<KeyValuePair<float, MissionStartDto>>();
            Blip[] blips = World.GetAllBlips();
            if (blips == null)
            {
                return new List<MissionStartDto>();
            }
            Vector3 origin = ped.Position;
            for (int i = 0; i < blips.Length; i++)
            {
                Blip b = blips[i];
                if (b == null || !b.Exists())
                {
                    continue;
                }
                string who = ProtagonistForBlipColor(b.Color);
                if (who == null)
                {
                    continue;
                }
                Vector3 pos = b.Position;
                found.Add(new KeyValuePair<float, MissionStartDto>(
                    origin.DistanceTo(pos),
                    new MissionStartDto { Pos = ToDto(pos), Protagonist = who }));
            }
            found.Sort((a, c) => a.Key.CompareTo(c.Key));
            var result = new List<MissionStartDto>();
            for (int i = 0; i < found.Count && i < 8; i++)
            {
                result.Add(found[i].Value);
            }
            return result;
        }

        /// <summary>
        /// CONTRACTS v1.10 item 3: the pinned story/side-mission script-name allowlist, vendored
        /// verbatim from docs/research/brief-mission-scripts.json (source: YimMenu
        /// GTA-V-Decompiled-Scripts all_script_names.txt, confirmed against the game's own running
        /// script threads at SCRIPT_THREAD_ITERATOR time, not guessed). A running script thread's
        /// name counts as "the mission" only if it is in this set; anything else (HUD/ambient/
        /// network scripts, camera scripts, etc.) is not what mission.script means. Title pairings
        /// (e.g. armenian1 = "Franklin and Lamar") are NOT sourced — the harness learns those from
        /// observed (OCR title, script) co-occurrence rather than the bridge shipping a guess.
        /// </summary>
        private static readonly HashSet<string> MissionScriptNames = new HashSet<string>(
            StringComparer.OrdinalIgnoreCase)
        {
            "prologue1",
            "armenian1", "armenian2", "armenian3",
            "family1", "family2", "family3", "family4", "family5", "family6",
            "lamar1",
            "franklin0", "franklin1", "franklin2",
            "michael1", "michael2", "michael3", "michael4", "michael4leadout",
            "trevor1", "trevor2", "trevor3", "trevor4",
            "fbi1", "fbi2", "fbi3", "fbi4", "fbi4_intro",
            "fbi4_prep1", "fbi4_prep2", "fbi4_prep3", "fbi4_prep4", "fbi4_prep5",
            "fbi5a",
            "jewelry_heist", "jewelry_prep1a", "jewelry_prep1b", "jewelry_prep2a", "jewelry_setup1",
            "agency_heist1", "agency_heist2", "agency_heist3a", "agency_heist3b",
            "agency_prep1", "agency_prep2amb",
            "finale_heist1", "finale_heist2_intro", "finale_heist2a", "finale_heist2b",
            "finale_heist_prepa", "finale_heist_prepb", "finale_heist_prepc", "finale_heist_prepd",
            "finale_heist_prepeamb",
            "finale_choice", "finalea", "finaleb", "finalec1", "finalec2",
            "finale_endgame", "finale_intro", "finale_credits",
            "solomon1", "solomon2", "solomon3",
            "martin1",
            "exile1", "exile2", "exile3",
            "docks_heista", "docks_heistb", "docks_prep1", "docks_prep2b", "docks_setup",
            "rural_bank_heist", "rural_bank_prep1", // The Paleto Score - no "paleto_score" name exists
            "lester1",
            "carsteals1", "carsteal2", "carsteal3", "carsteal4",
            // Strangers and Freaks / side content families
            "barry1", "barry2", "barry3", "barry3a", "barry3c", "barry4",
            "epsilon1", "epsilon2", "epsilon3", "epsilon4", "epsilon5", "epsilon6", "epsilon7",
            "epsilon8",
            "nigel1", "nigel1a", "nigel1b", "nigel1c", "nigel1d", "nigel2", "nigel3",
            "omega1", "omega2",
            "fanatic1", "fanatic2", "fanatic3",
            "abigail1", "abigail2",
            "maude1",
            "bailbond1", "bailbond2", "bailbond3", "bailbond4",
            "josh1", "josh2", "josh3", "josh4",
            "drf1", "drf2", "drf3", "drf4", "drf5",
            "tonya1", "tonya2", "tonya3", "tonya4", "tonya5",
            "chinese1", "chinese2",
            "hao1",
            "mrsphilips1", "mrsphilips2",
            "paparazzo1", "paparazzo2", "paparazzo3", "paparazzo3a", "paparazzo3b", "paparazzo4",
            "minute1", "minute2", "minute3",
            "extreme1", "extreme2", "extreme3", "extreme4",
            "rampage1", "rampage2", "rampage3", "rampage4", "rampage5",
            "thelastone"
        };

        /// <summary>Combined result of one script-thread walk: the mission-script name (subject
        /// to the missionActive filter applied by the caller) and whether a retry/checkpoint-reload
        /// is in flight.</summary>
        private sealed class MissionScriptScan
        {
            public string Script;
            public bool RetryInFlight;
        }

        // CONTRACTS v1.11: the game's own script-thread name for the checkpoint-reload/retry
        // executor (docs/research/brief-mission-comprehension.json: "mission_repeat_controller ...
        // is the retry/checkpoint-reload executor" - QUEUE_MISSION_REPEAT_LOAD, fade out/in,
        // TERMINATE_THIS_THREAD; it renders no UI itself). Not gated by missionActive: the research
        // could not confirm the thread never outlives the mission flag, and the whole point is to
        // catch the window a human sees as "reloading" even if active has already dropped.
        private const string MissionRepeatControllerThreadName = "mission_repeat_controller";

        /// <summary>
        /// CONTRACTS v1.10 item 3 / v1.11: throttled the same way FindEntityBlipsSafe is (see
        /// MissionScriptThrottleMs) - a scan failure or a throttled tick serves the previous result
        /// rather than flashing mission.script/retry_in_flight to their zero values.
        /// </summary>
        private MissionScriptScan FindMissionScriptScanSafe()
        {
            int now = Environment.TickCount;
            if (unchecked(now - _lastMissionScriptScanAt) < MissionScriptThrottleMs)
            {
                return _lastMissionScriptScan;
            }
            try
            {
                _lastMissionScriptScan = FindMissionScriptScan();
                _lastMissionScriptScanAt = now;
            }
            catch (Exception ex)
            {
                int errNow = Environment.TickCount;
                if (unchecked(errNow - _lastScriptErrorAt) > 5000)
                {
                    _lastScriptErrorAt = errNow;
                    BridgeLog.Error("mission-script thread scan failed (SCRIPT_THREAD_ITERATOR_*); "
                                    + "mission.script/retry_in_flight serve the last good result", ex);
                }
                // Keep _lastMissionScriptScan as it was - do not overwrite with a fresh (empty) one.
            }
            return _lastMissionScriptScan;
        }

        /// <summary>
        /// Enumerates every running script thread (SCRIPT_THREAD_ITERATOR_RESET +
        /// SCRIPT_THREAD_ITERATOR_GET_NEXT_THREAD_ID, 0 = end; GET_NAME_OF_SCRIPT_WITH_THIS_ID per
        /// thread id). Records the first name found in <see cref="MissionScriptNames"/> (mission
        /// name) AND whether <see cref="MissionRepeatControllerThreadName"/> is present
        /// (retry_in_flight) in the SAME pass - CONTRACTS v1.11: "free to detect... you are already
        /// walking the thread list". All three natives are present in the pinned SHVDN nightly's
        /// Hash enum (verified against lib/ScriptHookVDotNet3.dll with MetadataLoadContext) but have
        /// no typed GTA.* wrapper, so they are called raw via Function.Call - the same pattern
        /// TaskEngine.StartSeekCover uses for TASK_SEEK_COVER_FROM_POS. If more than one allowlisted
        /// mission thread is running at once (unconfirmed whether this happens; a mission and one of
        /// its prep/sub-scripts could both match) the first one seen in iteration order wins and the
        /// collision is logged once - no priority scheme is guessed.
        /// </summary>
        private static MissionScriptScan FindMissionScriptScan()
        {
            var scan = new MissionScriptScan();
            Function.Call(Hash.SCRIPT_THREAD_ITERATOR_RESET);
            bool loggedCollision = false;
            while (true)
            {
                int threadId = Function.Call<int>(Hash.SCRIPT_THREAD_ITERATOR_GET_NEXT_THREAD_ID);
                if (threadId == 0)
                {
                    break;
                }
                string name = Function.Call<string>(Hash.GET_NAME_OF_SCRIPT_WITH_THIS_ID, threadId);
                if (string.IsNullOrEmpty(name))
                {
                    continue;
                }
                if (string.Equals(name, MissionRepeatControllerThreadName,
                    StringComparison.OrdinalIgnoreCase))
                {
                    scan.RetryInFlight = true;
                }
                if (!MissionScriptNames.Contains(name))
                {
                    continue;
                }
                if (scan.Script == null)
                {
                    scan.Script = name;
                }
                else if (!loggedCollision)
                {
                    loggedCollision = true;
                    BridgeLog.Info("mission.script: multiple allowlisted script threads running at "
                                   + "once (\"" + scan.Script + "\" and \"" + name + "\"); reporting "
                                   + "the first seen (\"" + scan.Script + "\") - no priority scheme "
                                   + "is defined");
                }
            }
            return scan;
        }

        /// <summary>
        /// CONTRACTS v1.11: blips pinned to an entity (ped/vehicle), whether or not the game has
        /// plotted a route to them - the fix for a followed crewmate vanishing from /state the
        /// moment he drives beyond nearby's ~50 m radius while the game keeps drawing his position
        /// on the minimap. Guarded and throttled the same way FindObjectiveBlipSafe is (both walk
        /// World.GetAllBlips, the fragile memory scan), PLUS the v1.11 rate limit below: only run
        /// the real scan at most every <see cref="EntityBlipThrottleMs"/> ms, and only at all while
        /// a mission is active (mission.entity_blips has no use outside a mission and this halves
        /// the already-small per-frame cost). Serves the previous result in between, so a caller
        /// polling every tick never sees the list go empty just because this tick was throttled.
        /// </summary>
        private List<EntityBlipDto> FindEntityBlipsSafe(Vector3 origin, bool missionActive)
        {
            if (!missionActive)
            {
                // Reset the cache too: a mission that just ended must not leak its last followed
                // entity forward into whatever comes next (a new mission, free roam) before the
                // throttle window would otherwise have refreshed it.
                _lastEntityBlips = null;
                _lastEntityBlipsAt = int.MinValue;
                return new List<EntityBlipDto>();
            }

            int now = Environment.TickCount;
            if (_lastEntityBlips != null && unchecked(now - _lastEntityBlipsAt) < EntityBlipThrottleMs)
            {
                return _lastEntityBlips;
            }

            try
            {
                List<EntityBlipDto> found = FindEntityBlips(origin);
                _lastEntityBlips = found;
                _lastEntityBlipsAt = now;
                return found;
            }
            catch (Exception ex)
            {
                int errNow = Environment.TickCount;
                if (unchecked(errNow - _lastEntityBlipErrorAt) > 5000)
                {
                    _lastEntityBlipErrorAt = errNow;
                    BridgeLog.Error("entity-blip scan failed (World.GetAllBlips memory scan); "
                                    + "mission.entity_blips serves the last good result", ex);
                }
                // Serve whatever we had rather than flashing to empty on a transient scan failure -
                // a single bad tick must not make a followed target look lost.
                return _lastEntityBlips ?? new List<EntityBlipDto>();
            }
        }

        /// <summary>
        /// The actual entity-blip scan (throttled caller above). Collects every blip that is
        /// attached to a live ped/vehicle entity - <see cref="ResolveBlipKindAndHandle"/>'s
        /// same "entity" classification and the same "never hand back a handle nothing can
        /// resolve" rule, just without that method's coord fallback: an entity_blips entry IS an
        /// entity blip or it is not emitted at all. ShowRoute is NOT required (that is exactly the
        /// gap route_blips/objective_blip leave open - a plain blue crew dot usually has no route).
        /// The player's own waypoint is excluded, same convention as the objective-blip pass.
        /// Nearest first, capped at <see cref="MaxEntityBlips"/>.
        /// </summary>
        private static List<EntityBlipDto> FindEntityBlips(Vector3 origin)
        {
            var found = new List<KeyValuePair<float, EntityBlipDto>>();
            Blip[] blips = World.GetAllBlips();
            if (blips == null)
            {
                return new List<EntityBlipDto>();
            }
            for (int i = 0; i < blips.Length; i++)
            {
                Blip b = blips[i];
                if (b == null || !b.Exists())
                {
                    continue;
                }
                if (b.Sprite == BlipSprite.Waypoint || b.Color == BlipColor.Waypoint)
                {
                    continue; // the agent's own map waypoint, never a followable entity.
                }
                if (!IsEntityBlip(b.BlipType))
                {
                    continue;
                }
                Entity ent = b.Entity; // GET_BLIP_INFO_ID_ENTITY_INDEX, re-resolved fresh here.
                if (ent == null || !ent.Exists())
                {
                    continue; // stale/unresolved - never emit a handle nothing can use.
                }
                Vector3 blipPos = b.Position;
                float distance = origin.DistanceTo(blipPos);
                // v1.11: Blip.GetAppropriateName() - "the same string as Blip.Name if the custom
                // string is set; otherwise the localized string ... with the same GXT key hash as
                // DisplayNameHash" (SHVDN docs) - turns an anonymous dot into "Lamar" with one call.
                // Returns null if the blip does not exist; an empty string is normalized to null too
                // (both mean "no name to show").
                string name = b.GetAppropriateName();
                found.Add(new KeyValuePair<float, EntityBlipDto>(distance, new EntityBlipDto
                {
                    Pos = ToDto(blipPos),
                    Handle = ent.Handle,
                    Color = b.Color.ToString(),
                    IsRoute = b.ShowRoute,
                    Distance = distance,
                    Name = string.IsNullOrEmpty(name) ? null : name
                }));
            }
            found.Sort((a, c) => a.Key.CompareTo(c.Key));
            var result = new List<EntityBlipDto>();
            for (int i = 0; i < found.Count && i < MaxEntityBlips; i++)
            {
                result.Add(found[i].Value);
            }
            return result;
        }

        /// <summary>What one blip pass found: the chosen objective, plus the routed set behind it.</summary>
        private sealed class ObjectiveScan
        {
            public ObjectiveBlipDto Objective;
            public List<RouteBlipDto> RouteBlips = new List<RouteBlipDto>();
        }

        /// <summary>A route-enabled blip plus its distance, so the set can be ordered once.</summary>
        private sealed class RouteCandidate
        {
            public float Distance;
            public Vec3Dto Pos;
            public string Kind;
            public int Handle;
            public BlipColor Color;
        }

        /// <summary>
        /// Objective-blip lookup, isolated behind its own guard: World.GetAllBlips is an SHVDN
        /// memory scan and is by far the most likely call here to break on a game update. One
        /// nullable field is an acceptable loss; freezing all of /state is not.
        /// </summary>
        private ObjectiveScan FindObjectiveBlipSafe(Vector3 origin)
        {
            try
            {
                return FindObjectiveBlip(origin);
            }
            catch (Exception ex)
            {
                int now = Environment.TickCount;
                if (unchecked(now - _lastBlipErrorAt) > 5000)
                {
                    _lastBlipErrorAt = now;
                    BridgeLog.Error("objective-blip scan failed (World.GetAllBlips memory scan); "
                                    + "mission.objective_blip stays null and mission.route_blips "
                                    + "stays empty", ex);
                }
                return new ObjectiveScan();
            }
        }

        /// <summary>
        /// Which blip is "the objective", and the routed set it was picked from.
        ///
        /// WHY THIS IS NOT THE OLD RULE. Until CONTRACTS v1.8 this asked for sprite Standard(1) +
        /// colour Yellow(66) + route enabled, all three. That is the shape of a plain yellow story
        /// marker and nothing else, so every mission whose target is a VEHICLE or a CHARACTER —
        /// different sprite, usually Blue — reported objective_blip: null. Observed live
        /// 2026-09-02: the operator could see the game drawing a blue route line to a car the agent was
        /// meant to follow, /state said there was no objective, and the agent sat on the freeway
        /// narrating "no objective blip yet — wait for the marker" until something killed him.
        ///
        /// THE RULE, in priority order:
        ///   (a) Any blip with ShowRoute == true. That flag is the game's own PED_TYPE-level fact:
        ///       SET_BLIP_ROUTE sets it and it is what makes the engine draw the route line on the
        ///       map. If the game is drawing a line to it, it is where the game wants the player to
        ///       go — whatever its colour or sprite. Colour and sprite are conventions; the route
        ///       flag is the intent.
        ///   (b) Among routed blips, prefer a yellow one. When a mission routes to several things
        ///       at once the yellow one is the primary story objective; the others are secondary
        ///       (a companion, a second drop-off). Ties inside each tier break on distance, nearest
        ///       first, so the pick is stable frame to frame rather than pool-order dependent.
        ///   (c) Only if nothing is routed, fall back to the legacy yellow + Standard marker
        ///       WITHOUT requiring the route flag — a mission that shows a marker but never calls
        ///       SET_BLIP_ROUTE still gets reported.
        ///
        /// What is deliberately NOT an objective:
        ///   - short-range / always-on map furniture (shops, safehouses, properties). It is
        ///     excluded structurally: tiers (a)/(b) need the route flag, which furniture never has,
        ///     and tier (c) additionally rejects IsShortRange.
        ///   - the player's own map waypoint. It draws a route too, and the bridge itself creates
        ///     it (TaskEngine's set_waypoint sets World.WaypointPosition). Feeding it back as "the
        ///     objective" would have the agent chase a marker he placed himself.
        ///
        /// World.GetAllBlips() with no sprite argument is the documented params overload
        /// (ScriptHookVDotNet3.xml: M:GTA.World.GetAllBlips(GTA.BlipSprite[]) — "leave blank to get
        /// all Blips"); it is an SHVDN memory scan and is the fragile part on any edition change,
        /// hence the guard above.
        /// </summary>
        private static ObjectiveScan FindObjectiveBlip(Vector3 origin)
        {
            var scan = new ObjectiveScan();
            Blip[] blips = World.GetAllBlips();
            if (blips == null)
            {
                return scan;
            }

            var routed = new List<RouteCandidate>();
            for (int i = 0; i < blips.Length; i++)
            {
                Blip b = blips[i];
                // ShowRoute is tested before Exists() on purpose: it is a pure memory read of the
                // blip's own Route flag and returns false for a stale handle, so the far more
                // expensive per-blip natives below only run for the handful actually being routed
                // to. Every other blip costs one memory read per tick.
                if (b == null || !b.ShowRoute || !b.Exists())
                {
                    continue;
                }
                BlipColor color = b.Color;
                if (b.Sprite == BlipSprite.Waypoint || color == BlipColor.Waypoint)
                {
                    continue; // the agent's own waypoint, not the game's objective — see above.
                }
                Vector3 blipPos = b.Position;
                string kind;
                int handle;
                ResolveBlipKindAndHandle(b, out kind, out handle);
                routed.Add(new RouteCandidate
                {
                    Distance = origin.DistanceTo(blipPos),
                    Pos = ToDto(blipPos),
                    Kind = kind,
                    Handle = handle,
                    Color = color
                });
            }
            routed.Sort(CompareRouteDistance);

            for (int i = 0; i < routed.Count && i < MaxRouteBlips; i++)
            {
                RouteCandidate c = routed[i];
                scan.RouteBlips.Add(new RouteBlipDto
                {
                    Pos = c.Pos,
                    Kind = c.Kind,
                    Handle = c.Handle,
                    Color = c.Color.ToString()
                });
            }

            if (routed.Count > 0)
            {
                RouteCandidate chosen = routed[0];      // (a) nearest routed blip
                for (int i = 0; i < routed.Count; i++)  // (b) ... unless a yellow one is routed too
                {
                    if (IsYellow(routed[i].Color))
                    {
                        chosen = routed[i];
                        break;                          // routed is distance-sorted: nearest yellow
                    }
                }
                scan.Objective = new ObjectiveBlipDto
                {
                    Pos = chosen.Pos,
                    Kind = chosen.Kind,
                    Handle = chosen.Handle
                };
                return scan;
            }

            scan.Objective = FindLegacyYellowStandardBlip(origin);   // (c)
            return scan;
        }

        /// <summary>
        /// Tier (c): the pre-v1.8 rule minus the route requirement — a yellow Standard marker the
        /// game shows without calling SET_BLIP_ROUTE. Reached only when nothing is routed, so it
        /// can add objectives that used to be missed but can never override a routed one.
        /// GetAllBlips(BlipSprite.Standard) filters on the sprite inside the memory scan, so this
        /// second pass only touches blips that could possibly match.
        /// </summary>
        private static ObjectiveBlipDto FindLegacyYellowStandardBlip(Vector3 origin)
        {
            Blip[] blips = World.GetAllBlips(BlipSprite.Standard);
            if (blips == null)
            {
                return null;
            }
            ObjectiveBlipDto best = null;
            float bestDistance = float.MaxValue;
            for (int i = 0; i < blips.Length; i++)
            {
                Blip b = blips[i];
                if (b == null || !b.Exists())
                {
                    continue;
                }
                if (!IsYellow(b.Color))
                {
                    continue;
                }
                // Short-range blips are the always-on map furniture — shops, safehouses, owned
                // property. A story objective is long-range. Without this, a tier that no longer
                // demands a route flag could point the agent at a clothes shop.
                if (b.IsShortRange)
                {
                    continue;
                }
                Vector3 blipPos = b.Position;
                float distance = origin.DistanceTo(blipPos);
                if (distance >= bestDistance)
                {
                    continue;
                }
                bestDistance = distance;
                string kind;
                int handle;
                ResolveBlipKindAndHandle(b, out kind, out handle);
                best = new ObjectiveBlipDto
                {
                    Pos = ToDto(blipPos),
                    Kind = kind,
                    Handle = handle
                };
            }
            return best;
        }

        /// <summary>
        /// "Looks yellow on the map." BlipColor carries six indices that render identically —
        /// SHVDN's own docs say Yellow2/3/4/5/6 are "always the same as Yellow, the only difference
        /// is color index" (values read from the pinned ScriptHookVDotNet3.dll v3.7.0.189:
        /// Yellow=66, Yellow2=5, Yellow3=60, Yellow4=70, Yellow5=71, Yellow6=73). The old rule
        /// compared against Yellow(66) alone and would have missed a marker on any other index.
        /// YellowDark(56) and MenuYellow(80) are genuinely different colours and are excluded.
        /// </summary>
        private static bool IsYellow(BlipColor color)
        {
            return color == BlipColor.Yellow
                   || color == BlipColor.Yellow2
                   || color == BlipColor.Yellow3
                   || color == BlipColor.Yellow4
                   || color == BlipColor.Yellow5
                   || color == BlipColor.Yellow6;
        }

        /// <summary>
        /// Nearest first. List.Sort is an unstable introsort, so equal distances fall back to the
        /// handle: two blips pinned to the same spot must not swap places from frame to frame and
        /// hand the harness a different objective each tick.
        /// </summary>
        private static int CompareRouteDistance(RouteCandidate a, RouteCandidate b)
        {
            int byDistance = a.Distance.CompareTo(b.Distance);
            return byDistance != 0 ? byDistance : a.Handle.CompareTo(b.Handle);
        }

        /// <summary>
        /// CONTRACTS v1.10 item 1 — the fix. Until this version the bridge emitted the BLIP's own
        /// handle under kind:"entity", a different handle space than the entity handles
        /// follow_entity/enter_nearest_vehicle etc. take; a caller could never resolve it
        /// (root cause of the 2026-09-02 "drives and then stops" follow-mission failure). The
        /// correct resolution is Blip.Entity, which wraps GET_BLIP_INFO_ID_ENTITY_INDEX and
        /// re-resolves fresh on every call (docs/research/brief-shvdn-blips-occupants.json) - it is
        /// deliberately read here, inside this same snapshot pass, and never cached across ticks.
        /// It returns null for a coord blip and (per the brief, unconfirmed either way) possibly
        /// for an entity outside streaming range; either way a null/gone result degrades to
        /// kind:"coord" with the blip's own position and handle, which is still honest - never
        /// "entity" with a handle nothing can resolve.
        /// </summary>
        private static void ResolveBlipKindAndHandle(Blip b, out string kind, out int handle)
        {
            if (IsEntityBlip(b.BlipType))
            {
                Entity ent = b.Entity;
                if (ent != null && ent.Exists())
                {
                    kind = "entity";
                    handle = ent.Handle;
                    return;
                }
            }
            kind = "coord";
            handle = b.Handle;
        }

        private static bool IsEntityBlip(BlipType type)
        {
            switch (type)
            {
                case BlipType.Vehicle:
                case BlipType.Character:
                case BlipType.Object:
                case BlipType.Pickup:
                case BlipType.PickupObject:
                    return true;
                default:
                    return false;
            }
        }

        /// <summary>
        /// CONTRACTS v1.11 combat-comprehension threat scan, folded into the SAME per-tick pass
        /// that already walks nearby.peds[] - deliberately UNTHROTTLED (unlike the blip-pool scans
        /// above), see the reasoning at the bottom of this method. Root-caused fix for the
        /// 2026-09-02 live bug: a carjack victim punched the agent to death while he stood there,
        /// because TASK_COMBAT_HATED_TARGETS_AROUND_PED silently no-ops without a
        /// Neutral/Dislike/Hate relationship (a carjack victim is plausibly still Respect/Like) -
        /// docs/research/brief-combat-natives.json. attacking_me/threat give the harness (and a
        /// future reflex layer) a relationship-independent "am I being hit" signal.
        /// </summary>
        private static NearbyDto BuildNearby(Ped playerPed, Vehicle ownVehicle, out ThreatDto threat)
        {
            var nearby = new NearbyDto();
            Vector3 origin = playerPed.Position;
            int bestAttackerHandle = 0;
            float bestAttackerDist = float.MaxValue;

            var vehicles = new List<NearbyVehicleDto>();
            Vehicle[] rawVehicles = World.GetNearbyVehicles(playerPed, NearbyVehicleRadiusM)
                                    ?? new Vehicle[0];
            for (int i = 0; i < rawVehicles.Length; i++)
            {
                Vehicle v = rawVehicles[i];
                if (v == null || !v.Exists())
                {
                    continue;
                }
                if (ownVehicle != null && v.Handle == ownVehicle.Handle)
                {
                    continue;
                }
                vehicles.Add(new NearbyVehicleDto
                {
                    Handle = v.Handle,
                    Model = VehicleModelName(v.Model),
                    DisplayName = v.LocalizedName,
                    Class = v.ClassType.ToString(),
                    Distance = origin.DistanceTo(v.Position),
                    Driver = DescribeDriver(v),
                    Pos = ToDto(v.Position)
                });
            }
            vehicles.Sort(CompareVehicleDistance);
            nearby.Vehicles = Truncate(vehicles);

            var peds = new List<NearbyPedDto>();
            Ped[] rawPeds = World.GetNearbyPeds(playerPed, NearbyPedRadiusM) ?? new Ped[0];
            for (int i = 0; i < rawPeds.Length; i++)
            {
                Ped p = rawPeds[i];
                if (p == null || !p.Exists() || p.Handle == playerPed.Handle)
                {
                    continue;
                }
                Model model = p.Model;   // read once; PedModelName below reuses it
                if (IsAnimal(p, model))
                {
                    continue;
                }
                Relationship rel = p.GetRelationshipWithPed(playerPed);
                bool hostile = rel == Relationship.Hate || p.IsInCombatAgainst(playerPed);
                // CONTRACTS v1.5: "friendly" = the engine's own companion/like/respect relationship
                // towards the player - i.e. mission crewmates (Michael and Brad in the prologue).
                // Before this every non-hostile ped was "neutral", so the harness could not tell a
                // crewmate from a bystander and "stay with the crew" was unexpressible.
                bool friendly = !hostile
                                && (rel == Relationship.Companion
                                    || rel == Relationship.Like
                                    || rel == Relationship.Respect);
                float pedDistance = origin.DistanceTo(p.Position);
                // v1.11: cheap per-ped natives (bounded to whatever GetNearbyPeds already returned
                // inside NearbyPedRadiusM, i.e. at most a handful) - run every tick, UNTHROTTLED.
                // Unlike the blip-pool scans (unbounded map-wide memory scans, hence their ~4 Hz
                // throttle), this is the same cost class as the hostile/friendly relationship checks
                // immediately above, which have always run every tick. It also has to be fast: a
                // melee swing animation is on screen for a fraction of a second, and even a "fast"
                // 4 Hz throttle (250 ms) could miss the window entirely - the whole point of using
                // MeleeTarget/IsInCombatAgainst is to see the attack BEFORE the first punch lands.
                bool attackingMe = IsAttackingMe(p, playerPed);
                string weaponClass = WeaponClassOf(p);
                if (attackingMe && pedDistance < bestAttackerDist)
                {
                    bestAttackerDist = pedDistance;
                    bestAttackerHandle = p.Handle;
                }
                peds.Add(new NearbyPedDto
                {
                    Handle = p.Handle,
                    Model = PedModelName(model),
                    Distance = pedDistance,
                    Relationship = hostile ? "hostile" : (friendly ? "friendly" : "neutral"),
                    Pos = ToDto(p.Position),
                    InVehicleHandle = InVehicleHandleOf(p),
                    AttackingMe = attackingMe,
                    WeaponClass = weaponClass
                });
            }
            peds.Sort(ComparePedDistance);
            nearby.Peds = Truncate(peds);

            // CRITICAL (docs/research/brief-combat-natives.json): HAS_ENTITY_BEEN_DAMAGED_BY_ENTITY
            // is STICKY until cleared - R*'s own re_cartheft clears it every cycle, exactly here,
            // once per scan, AFTER every ped has been read against it above. Skipping this makes
            // "who hit me" stale forever (a ped who tagged the player once would read attacking_me
            // forever after).
            Function.Call(Hash.CLEAR_ENTITY_LAST_DAMAGE_ENTITY, playerPed.Handle);

            threat = new ThreatDto
            {
                AttackerHandle = bestAttackerHandle != 0 ? (int?)bestAttackerHandle : null,
                BeingJackedBy = BeingJackedByOf(playerPed)
            };

            return nearby;
        }

        /// <summary>
        /// True if <paramref name="p"/> is attacking the player RIGHT NOW, by any of three signals
        /// (docs/research/brief-combat-natives.json facts #3/#4/SHVDN-footguns):
        ///   - Ped.MeleeTarget == the player: valid WHILE THE SWING ANIMATION PLAYS, i.e. before
        ///     impact - the earliest possible signal for a melee attacker.
        ///   - Ped.IsInCombatAgainst(player): true the frame the attacker's own CTaskCombat targets
        ///     the player, also before any damage lands. Deliberately NOT Ped.IsInCombat (SHVDN bug:
        ///     that property calls the two-argument IS_PED_IN_COMBAT native with only one argument -
        ///     verified against the pinned SHVDN source description in the research brief).
        ///   - HAS_ENTITY_BEEN_DAMAGED_BY_ENTITY(player, p, bCheckDamagerVehicle: 0): the fallback
        ///     for damage that already landed. Called RAW rather than through SHVDN's
        ///     Entity.HasBeenDamagedBy(Entity), which hardcodes bCheckDamagerVehicle=1 and would
        ///     therefore count a ped whose CAR merely clipped the player as a melee attacker.
        /// </summary>
        private static bool IsAttackingMe(Ped p, Ped playerPed)
        {
            Ped meleeTarget = p.MeleeTarget;
            if (meleeTarget != null && meleeTarget.Exists() && meleeTarget.Handle == playerPed.Handle)
            {
                return true;
            }
            if (p.IsInCombatAgainst(playerPed))
            {
                return true;
            }
            return Function.Call<bool>(Hash.HAS_ENTITY_BEEN_DAMAGED_BY_ENTITY,
                playerPed.Handle, p.Handle, 0);
        }

        /// <summary>
        /// "unarmed"|"melee"|"gun"|"projectile" classification of <paramref name="p"/>'s CURRENT
        /// weapon via IS_PED_ARMED's bitmask (no SHVDN wrapper; verified present in GTA.Native.Hash;
        /// signature/bits confirmed against citizenfx/natives WEAPON/IsPedArmed.md: bit 1 = melee
        /// weapons, bit 2 = explosive/projectile weapons, bit 4 = any other (gun) weapon; passing 0
        /// always returns false, which is why "unarmed" is a separate fall-through rather than a
        /// fourth bit). Tested most-dangerous-first so a single native call per class is enough:
        /// IS_PED_ARMED reports on the ped's CURRENTLY EQUIPPED weapon, so at most one of the three
        /// bits can be true for a given ped at a given tick.
        /// </summary>
        private static string WeaponClassOf(Ped p)
        {
            if (Function.Call<bool>(Hash.IS_PED_ARMED, p.Handle, 4))
            {
                return "gun";
            }
            if (Function.Call<bool>(Hash.IS_PED_ARMED, p.Handle, 2))
            {
                return "projectile";
            }
            if (Function.Call<bool>(Hash.IS_PED_ARMED, p.Handle, 1))
            {
                return "melee";
            }
            return "unarmed";
        }

        /// <summary>
        /// threat.being_jacked_by: GET_PEDS_JACKER(player) while IS_PED_BEING_JACKED(player) is
        /// true, else null. Neither has an SHVDN wrapper (verified absent from
        /// lib/Docs/ScriptHookVDotNet3.xml; both hashes confirmed present in GTA.Native.Hash) - raw
        /// Function.Call, including Function.Call&lt;Ped&gt; for the entity-returning native (the
        /// same generic-return pattern SHVDN uses throughout; not a footgun, just uncommon in this
        /// file until now).
        /// </summary>
        private static int? BeingJackedByOf(Ped playerPed)
        {
            if (!Function.Call<bool>(Hash.IS_PED_BEING_JACKED, playerPed.Handle))
            {
                return null;
            }
            Ped jacker = Function.Call<Ped>(Hash.GET_PEDS_JACKER, playerPed.Handle);
            return jacker != null && jacker.Exists() ? (int?)jacker.Handle : null;
        }

        /// <summary>
        /// Is this "ped" an animal? In this engine cats, dogs, coyotes, chickens, pigs, rats,
        /// seagulls and sharks are all peds, and World.GetNearbyPeds returns them alongside people.
        ///
        /// Observed live 2026-09-02: a stray cat came back in nearby.peds[] with relationship
        /// "hostile", so both the agent's brain and the combat reflex treated it as an attacker. He
        /// narrated it for a whole mission ("Cat's three meters closer and closing fast") while
        /// real threats went unranked. nearby.peds is meant to be people.
        ///
        /// Two engine-side signals, verified against the pinned SHVDN v3.7.0.189:
        ///   - Ped.PedType wraps GET_PED_TYPE; PedType.Animal is the game's own value 28.
        ///   - Model.IsAnimalPed reads the model's ped-personality "is human" flag and negates it.
        /// Both fail closed: GET_PED_TYPE on an unclassifiable ped does not report Animal, and
        /// IsAnimalPed returns false when the model's personality data cannot be resolved. So a ped
        /// we cannot classify is reported as a person, and no human is ever dropped.
        ///
        /// internal (not private): TaskEngine's NearestHostile/CountHatedTargets reuse this exact
        /// check so an animal can never be selected as a combat target either, rather than growing
        /// a second, possibly-divergent animal test in that file.
        /// </summary>
        internal static bool IsAnimal(Ped p, Model model)
        {
            return p.PedType == PedType.Animal || model.IsAnimalPed;
        }

        /// <summary>
        /// CONTRACTS v1.10 item 2: the vehicle handle a nearby ped is currently seated in, or null
        /// on foot. World.GetNearbyPeds is a raw CPed-pool scan with no seat exclusion (research:
        /// docs/research/brief-shvdn-blips-occupants.json) so seated peds are already IN
        /// nearby.peds[] today - this only adds the attribution so the harness can hand off from a
        /// ped to the car the instant a crewmate mounts up, instead of only discovering the loss
        /// once the ped scrolls out of nearby's radius. Mirrors TaskEngine.CurrentVehicle's
        /// null-guard: IsInVehicle() and CurrentVehicle can disagree for a frame while entering, so
        /// CurrentVehicle is null-checked even when IsInVehicle() is true.
        /// </summary>
        private static int? InVehicleHandleOf(Ped p)
        {
            if (!p.IsInVehicle())
            {
                return null;
            }
            Vehicle veh = p.CurrentVehicle;
            return veh != null && veh.Exists() ? (int?)veh.Handle : null;
        }

        private static string DescribeDriver(Vehicle v)
        {
            Ped driver = v.Driver;
            if (driver == null || !driver.Exists())
            {
                return "empty";
            }
            return driver.IsPlayer ? "player" : "npc";
        }

        private static int CompareVehicleDistance(NearbyVehicleDto a, NearbyVehicleDto b)
        {
            return a.Distance.CompareTo(b.Distance);
        }

        private static int ComparePedDistance(NearbyPedDto a, NearbyPedDto b)
        {
            return a.Distance.CompareTo(b.Distance);
        }

        private static List<T> Truncate<T>(List<T> list)
        {
            if (list.Count > NearbyTopN)
            {
                list.RemoveRange(NearbyTopN, list.Count - NearbyTopN);
            }
            return list;
        }

        private static string VehicleModelName(Model model)
        {
            var hash = (VehicleHash)model.Hash;
            return Enum.IsDefined(typeof(VehicleHash), hash)
                ? hash.ToString().ToLowerInvariant()
                : "0x" + model.Hash.ToString("X8");
        }

        private static string PedModelName(Model model)
        {
            var hash = (PedHash)model.Hash;
            return Enum.IsDefined(typeof(PedHash), hash)
                ? hash.ToString().ToLowerInvariant()
                : "0x" + model.Hash.ToString("X8");
        }

        private static Vec3Dto ToDto(Vector3 v)
        {
            return new Vec3Dto { X = v.X, Y = v.Y, Z = v.Z };
        }
    }
}
