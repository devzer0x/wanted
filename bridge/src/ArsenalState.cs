using System;
using System.Collections.Generic;
using GTA;

namespace WastedBridge
{
    /// <summary>
    /// Bridge 1.9.0 — THE ARSENAL: a cheat-granted, time-boxed weapon layer on top of the ordinary
    /// Ammu-Nation loadout (<see cref="WeaponState"/>), built for the `burn_the_city` bit.
    ///
    /// WHY THIS EXISTS. CLAUDE.md rule 5 was rewritten on 2026-09-04: cheats are allowed, on
    /// purpose, for named bits — with four guardrails that this class is the bridge half of:
    ///
    ///   * SAY SO. `/state.effects.arsenal` reports the effect every tick it is on, so the
    ///     overlay, the commentary and a goal's `done_when` can all be honest about it.
    ///   * NO TYPED CHEAT CODES. Nothing here types anything; the effect is GIVE_WEAPON_TO_PED
    ///     through the pinned SHVDN wrapper, exactly the native the old rule refused.
    ///   * DELIBERATE, NOT AMBIENT. The arsenal is never his baseline. It is switched on by
    ///     `POST /arsenal {on:true}` for ONE bit and it goes away on its own: this class owns
    ///     the restore on goal end (`POST /arsenal {on:false}`), on death, on arrest, on a
    ///     cutscene, on a story mission starting, on script abort/reload, AND on a TTL so a
    ///     dead harness still restores. An effect that can be left on permanently is a
    ///     stream-wide bug (the timescale-stuck-at-0.15 class), which is why the restore
    ///     ships in the same file as the grant.
    ///   * STORY MODE ONLY. Unchanged; the online latch in WastedBridgeScript runs first.
    ///
    /// WHAT IT IS NOT. It is not god mode and it is not infinite ammo: `Weapon.InfiniteAmmo`
    /// is never touched (its pinned XML warns it "globally affects any of the weapons that
    /// uses the ammo the current weapon is using"), the counts are fixed and modest, and
    /// nothing here stops him dying — his deaths are the clip pipeline.
    ///
    /// EVERY WRAPPER USED HERE WAS VERIFIED AGAINST THE PINNED bridge/lib/ScriptHookVDotNet3.dll
    /// (assembly 3.7.0.189) by reading the 8-byte native hash each one pushes out of its IL
    /// (System.Reflection.Metadata, 2026-09-04), and the doc text is quoted from
    /// bridge/lib/Docs/ScriptHookVDotNet3.xml at each call site:
    ///
    ///   GTA.WeaponCollection.Give(WeaponHash, int, bool, bool)  pushes 0xBF0FD6E56C964FCB
    ///                                                           = GIVE_WEAPON_TO_PED
    ///   GTA.WeaponCollection.Remove(WeaponHash)                 pushes 0x4899CB088EDF59B8
    ///                                                           = REMOVE_WEAPON_FROM_PED
    ///   GTA.Weapon.Ammo (setter)                                pushes 0x14E56BC5B5DB6A19
    ///                                                           = SET_PED_AMMO
    ///   GTA.Game.IsCutsceneActive                               pushes 0x991251AFC3981F84
    ///                                                           = IS_CUTSCENE_ACTIVE
    ///   GTA.Game.IsMissionActive                                pushes 0xA33CDCCDA663159E
    ///                                                           = GET_MISSION_FLAG
    ///
    /// The WeaponHash members below are NOT documented in the XML (enum fields carry no doc
    /// text), so their presence and values were read by reflection over the same pinned DLL:
    ///   Grenade 0x93E220BD, Molotov 0x24B17070, RPG 0xB1CA77B1, GrenadeLauncher 0xA284510B,
    ///   Minigun 0x42BF8A85, AssaultRifle 0xBFEFFF6D. A rename in a future SHVDN breaks the
    ///   BUILD (they are used as enum members, not as literals), which is the failure mode we
    ///   want.
    ///
    /// OFFLINE-CHECKS NOTE. Like TaskEngine, this class keeps SHVDN types OUT of its fields:
    /// the kit stores hashes as uint (C# folds `(uint)WeaponHash.X` to a literal at compile
    /// time, so the pinned-enum check survives) so that bridge/tools/offline-checks can load
    /// WastedBridge.dll against its one-type SHVDN stub without a TypeLoadException.
    /// </summary>
    internal static class ArsenalState
    {
        /// <summary>TTL bounds for `POST /arsenal {on:true, ttl_s}`. Thirty seconds is the
        /// shortest bit worth announcing; five minutes is the longest anything cheat-driven
        /// may stay on without somebody choosing it again.</summary>
        internal const int DefaultTtlMs = 180000;
        internal const int MinTtlMs = 30000;
        internal const int MaxTtlMs = 300000;

        private sealed class Item
        {
            public uint Hash;
            public string Name;
            public int Ammo;
            /// <summary>Rounds he had for this hash BEFORE the grant, or -1 if he did not own
            /// it. What <see cref="Clear"/> restores to.</summary>
            public int PreOwned = -1;
        }

        // THE KIT. Chosen to be obviously a cheat (nobody walks out of Ammu-Nation with an RPG
        // and a minigun in the first hour of the story) and obviously time-boxed by its ammo:
        // a handful of throwables, a few rockets, one belt. Fixed counts, never topped up
        // while the bit runs, never infinite. Grenades and molotovs are the "throw" half;
        // the launchers are the "explosives that fire through the ordinary shoot_at /
        // fight_ped path" half — see TaskEngine.StartThrowAt for why that split matters.
        private static readonly Item[] Kit =
        {
            new Item { Hash = (uint)WeaponHash.Grenade,         Name = "Grenade",         Ammo = 8 },
            new Item { Hash = (uint)WeaponHash.Molotov,         Name = "Molotov",         Ammo = 6 },
            new Item { Hash = (uint)WeaponHash.RPG,             Name = "RPG",             Ammo = 4 },
            new Item { Hash = (uint)WeaponHash.GrenadeLauncher, Name = "GrenadeLauncher", Ammo = 10 },
            new Item { Hash = (uint)WeaponHash.Minigun,         Name = "Minigun",         Ammo = 600 },
            new Item { Hash = (uint)WeaponHash.AssaultRifle,    Name = "AssaultRifle",    Ammo = 150 },
        };

        private static bool _active;
        private static int _grantedAtMs;
        private static int _expiresAtMs;
        private static bool _swept;
        private static string _lastCleared = "";

        internal static bool IsActive
        {
            get { return _active; }
        }

        /// <summary>The kit's hashes, for <see cref="WeaponState"/>'s owned/selection reads.</summary>
        internal static IEnumerable<uint> KitHashes
        {
            get
            {
                for (int i = 0; i < Kit.Length; i++)
                {
                    yield return Kit[i].Hash;
                }
            }
        }

        internal static string NameOf(uint hash)
        {
            for (int i = 0; i < Kit.Length; i++)
            {
                if (Kit[i].Hash == hash)
                {
                    return Kit[i].Name;
                }
            }
            return null;
        }

        /// <summary>
        /// Switch the arsenal ON for <paramref name="ttlMs"/>. Refuses (returns false with a
        /// v1.2-style error code) while he is dead/arrested, in a cutscene, in a story mission,
        /// or when it is already on — a cheat bit is one bit, not a standing state that gets
        /// refreshed. Runs on the game thread only.
        /// </summary>
        internal static bool Grant(Ped ped, int ttlMs, bool dead, bool arrested,
                                   out string error, out string detail)
        {
            error = null;
            detail = null;
            if (ped == null || !ped.Exists())
            {
                error = "not_ready";
                detail = "no player ped";
                return false;
            }
            if (dead || arrested)
            {
                error = "player_down";
                detail = "the arsenal is not granted to a corpse or a man in cuffs";
                return false;
            }
            // XML: "Gets a value indicating whether the cutscene is active." (IS_CUTSCENE_ACTIVE)
            if (Game.IsCutsceneActive)
            {
                error = "cutscene_active";
                detail = "a cutscene is playing";
                return false;
            }
            // XML: "Gets or sets a value informing the engine if a mission is in progress."
            // (GET_MISSION_FLAG — the same read SnapshotBuilder publishes as mission.active.)
            if (Game.IsMissionActive)
            {
                error = "mission_active";
                detail = "a story mission is running; the arsenal is a free-roam bit only";
                return false;
            }
            if (_active)
            {
                error = "arsenal_active";
                detail = "the arsenal is already on (expires in "
                         + (ExpiresInS()).ToString("F0") + " s); one bit at a time";
                return false;
            }

            int ttl = Math.Max(MinTtlMs, Math.Min(MaxTtlMs, ttlMs));
            for (int i = 0; i < Kit.Length; i++)
            {
                Item item = Kit[i];
                WeaponHash hash = (WeaponHash)item.Hash;
                // XML (WeaponCollection.HasWeapon): "Gets the value that indicates whether the
                // owner Ped has the weapon for weaponHash." Recorded so Clear() can put back
                // exactly what he had rather than strip something he earned in the game.
                item.PreOwned = ped.Weapons.HasWeapon(hash) ? WeaponState.RoundsFor(ped, hash) : -1;
                // XML (WeaponCollection.Give): "Gives the specified weapon if the owner Ped does
                // not have one, or selects the weapon if they have one and equipNow is set to
                // true." equipNow false: being handed a rocket launcher must not put it in his
                // hands — the task decides what he holds. isAmmoLoaded: XML says it "Does not
                // work", so `true` is the documented no-op, the same value WeaponState passes.
                ped.Weapons.Give(hash, item.Ammo, false, true);
                try
                {
                    // XML (Weapon.Ammo): "Gets or sets the amount of ammo for this weapon."
                    // The same Give-is-a-no-op-when-owned fix WeaponState.GiveAndLoad documents:
                    // a top-up to the kit count, never a trim, so a rifle he already carried
                    // with more rounds keeps them.
                    Weapon w = ped.Weapons[hash];
                    if (w != null && w.Ammo < item.Ammo)
                    {
                        w.Ammo = item.Ammo;
                    }
                }
                catch (Exception ex)
                {
                    BridgeLog.Error("arsenal: could not set ammo for " + item.Name, ex);
                }
            }
            _active = true;
            _grantedAtMs = Game.GameTime;
            _expiresAtMs = _grantedAtMs + ttl;
            _lastCleared = "";
            BridgeLog.Info("ARSENAL ON (cheat, announced): " + Describe() + "; ttl " + (ttl / 1000)
                           + " s; restores on goal end / death / arrest / cutscene / mission / "
                           + "script abort / ttl");
            return true;
        }

        /// <summary>
        /// Switch it OFF and put his inventory back. Idempotent: a second call is a no-op
        /// that returns false. Every restore path in the bridge ends here, and the reason is
        /// logged because "why did the RPG vanish" is a question the operator will ask.
        /// </summary>
        internal static bool Clear(Ped ped, string reason)
        {
            if (!_active)
            {
                return false;
            }
            _active = false;
            _lastCleared = reason ?? "";
            if (ped == null || !ped.Exists())
            {
                // The ped is gone (a switch, a load). The sweep in Maintain() catches what is
                // left in the inventory the moment a ped exists again.
                _swept = false;
                BridgeLog.Warn("ARSENAL OFF (" + reason + "): no player ped to restore; "
                               + "the load-time sweep will finish the job");
                return true;
            }
            RestoreKit(ped, reason);
            return true;
        }

        private static void RestoreKit(Ped ped, string reason)
        {
            int removed = 0;
            int trimmed = 0;
            for (int i = 0; i < Kit.Length; i++)
            {
                Item item = Kit[i];
                WeaponHash hash = (WeaponHash)item.Hash;
                try
                {
                    if (!ped.Weapons.HasWeapon(hash))
                    {
                        continue;
                    }
                    if (item.PreOwned < 0)
                    {
                        // XML (WeaponCollection.Remove(Weapon), inherited by the WeaponHash
                        // overload): "Removes the specified weapon." REMOVE_WEAPON_FROM_PED.
                        ped.Weapons.Remove(hash);
                        removed++;
                    }
                    else
                    {
                        // He owned it before the bit: leave the gun, trim the rounds back to
                        // what he had if the grant added any. Never below what he has now
                        // if that is already less (he fired them; they are gone either way).
                        Weapon w = ped.Weapons[hash];
                        if (w != null && w.Ammo > item.PreOwned)
                        {
                            w.Ammo = item.PreOwned;
                            trimmed++;
                        }
                    }
                }
                catch (Exception ex)
                {
                    BridgeLog.Error("arsenal: restore failed for " + item.Name
                                    + " — the load-time sweep is the backstop", ex);
                }
                item.PreOwned = -1;
            }
            BridgeLog.Info("ARSENAL OFF (" + reason + "): removed " + removed + ", trimmed "
                           + trimmed + " of " + Kit.Length + " kit weapons");
        }

        /// <summary>
        /// Called once per tick from the script's Tick handler with the same flags the
        /// snapshot is built from. Two jobs:
        ///
        /// 1. THE LOAD-TIME SWEEP, once per script instance. Static state does not survive a
        ///    script reload, but the ped's inventory does — and the single-player autosave
        ///    carries it across a game restart too. So a bridge that died with the arsenal on
        ///    would otherwise leave him with a permanent RPG. On the first tick with a live
        ///    ped, every kit weapon present is removed and the count logged. Cost: a rifle a
        ///    mission handed him is lost at a reload. Accepted, because the alternative is
        ///    the stream-wide bug this class exists to rule out.
        /// 2. THE EDGES AND THE TTL while active: death, arrest, cutscene, a story mission
        ///    starting, or the clock — each one clears it, with its reason.
        /// </summary>
        internal static void Maintain(Ped ped, bool dead, bool arrested)
        {
            if (ped == null || !ped.Exists())
            {
                return;
            }
            if (!_swept)
            {
                _swept = true;
                Sweep(ped);
            }
            if (!_active)
            {
                return;
            }
            if (dead)
            {
                Clear(ped, "player_dead");
            }
            else if (arrested)
            {
                Clear(ped, "player_arrested");
            }
            else if (Game.IsCutsceneActive)
            {
                Clear(ped, "cutscene");
            }
            else if (Game.IsMissionActive)
            {
                Clear(ped, "mission_started");
            }
            else if (Game.GameTime >= _expiresAtMs)
            {
                Clear(ped, "ttl_expired");
            }
        }

        private static void Sweep(Ped ped)
        {
            int removed = 0;
            for (int i = 0; i < Kit.Length; i++)
            {
                try
                {
                    WeaponHash hash = (WeaponHash)Kit[i].Hash;
                    if (ped.Weapons.HasWeapon(hash))
                    {
                        ped.Weapons.Remove(hash);
                        removed++;
                    }
                }
                catch (Exception ex)
                {
                    BridgeLog.Error("arsenal sweep: could not remove " + Kit[i].Name, ex);
                }
            }
            if (removed > 0)
            {
                BridgeLog.Warn("arsenal sweep at load: removed " + removed
                               + " kit weapon(s) left over from a previous script instance or "
                               + "an autosave taken while the arsenal was on");
            }
            else
            {
                BridgeLog.Info("arsenal sweep at load: inventory clean");
            }
        }

        /// <summary>Script abort/reload: the last chance to restore from THIS instance.</summary>
        internal static void OnAbort()
        {
            if (!_active)
            {
                return;
            }
            try
            {
                Ped ped = Game.Player.Character;
                Clear(ped, "script_abort");
            }
            catch (Exception ex)
            {
                _active = false;
                BridgeLog.Error("arsenal: restore on script abort failed; the next instance's "
                                + "load-time sweep removes what is left", ex);
            }
        }

        internal static float ExpiresInS()
        {
            if (!_active)
            {
                return 0f;
            }
            return Math.Max(0f, (_expiresAtMs - Game.GameTime) / 1000f);
        }

        /// <summary>`effects.arsenal` for the snapshot. Never throws, never null.</summary>
        internal static ArsenalEffectDto Read()
        {
            var dto = new ArsenalEffectDto();
            try
            {
                dto.Active = _active;
                dto.ExpiresInS = ExpiresInS();
                dto.LastCleared = _lastCleared;
                if (_active)
                {
                    for (int i = 0; i < Kit.Length; i++)
                    {
                        dto.Weapons.Add(Kit[i].Name);
                        // The count he was topped up TO. A pre-owned gun with more rounds
                        // than the kit keeps them (Grant never trims), and the harness
                        // takes the max of this and what it sees, so that case is covered.
                        dto.Kit[Kit[i].Name] = Kit[i].Ammo;
                    }
                }
            }
            catch (Exception)
            {
                return new ArsenalEffectDto { Active = _active };
            }
            return dto;
        }

        internal static string Describe()
        {
            var parts = new List<string>(Kit.Length);
            for (int i = 0; i < Kit.Length; i++)
            {
                parts.Add(Kit[i].Name + " x" + Kit[i].Ammo);
            }
            return string.Join(", ", parts.ToArray());
        }
    }
}
