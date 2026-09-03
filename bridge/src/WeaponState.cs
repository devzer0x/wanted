using GTA;
using GTA.Native;

namespace WastedBridge
{
    /// <summary>
    /// Bridge 1.7.0 (CONTRACTS proposal v1.14) — what the agent is carrying, what he selects, and the
    /// one place the optional Ammu-Nation loadout lives. Its own file for the same reason
    /// <see cref="PhoneState"/> is: it is a self-contained capability with one read used by the
    /// snapshot and a couple of writes used by the task engine, and keeping it out of
    /// SnapshotBuilder/TaskEngine keeps two parallel workstreams off the same lines.
    ///
    /// EVERY NATIVE AND WRAPPER BELOW WAS VERIFIED AGAINST THE PINNED
    /// bridge/lib/ScriptHookVDotNet3.dll (assembly 3.7.0.189) BEFORE IT WAS WRITTEN, by reading
    /// the wrapper's IL out of the assembly with System.Reflection.Metadata and looking at the
    /// 8-byte native hash it pushes. Results, so the next reader does not have to redo it:
    ///
    ///   GTA.WeaponCollection.Give(WeaponHash, int, bool, bool)  pushes 0xBF0FD6E56C964FCB
    ///                                                           = GIVE_WEAPON_TO_PED
    ///   GTA.WeaponCollection.Select(WeaponHash, bool)           pushes 0x8DECB02F88F428BC
    ///                                                           (HAS_PED_GOT_WEAPON) and then
    ///                                                           0xADF692B254977C0C
    ///                                                           = SET_CURRENT_PED_WEAPON
    ///   GTA.WeaponCollection.HasWeapon(WeaponHash)              pushes 0x8DECB02F88F428BC
    ///                                                           = HAS_PED_GOT_WEAPON
    ///   GTA.WeaponCollection.Current                            pushes 0x3A87E44BB9A01D54
    ///                                                           = GET_CURRENT_PED_WEAPON
    ///
    /// The Select wrapper being HAS_PED_GOT_WEAPON-guarded ALREADY is the whole reason it is used
    /// rather than a raw SET_CURRENT_PED_WEAPON: docs/research/brief-combat-natives.json's own
    /// fairness judgment is "selecting a weapon he ALREADY OWNS (SET_CURRENT_PED_WEAPON /
    /// Ped.Weapons.Select, guarded by HAS_PED_GOT_WEAPON) is legitimate - one keypress for a
    /// human", and the guard is what makes that sentence true in code.
    /// </summary>
    internal static class WeaponState
    {
        // ---- the loadout, and why it is OFF by default ---------------------------------------
        //
        // THE THREE WEAPONS. A pistol, a micro SMG and a pump shotgun with modest ammo is exactly
        // the shelf a human player walks out of Ammu-Nation with early in the story, paid for with
        // the cash he has. It is not a god loadout: no rifle, no RPG, no sticky bombs, no minigun,
        // no armour, and INFINITE AMMO IS NEVER SET (GTA.Weapon.InfiniteAmmo is deliberately not
        // touched anywhere in this file) — he runs out and then he is a man with his fists, which
        // is the funnier half of the show anyway. Ammo counts are one or two magazines' worth, not
        // a war chest.
        //
        // AND YET IT SHIPS DISABLED. Two things in this repo say so, both of them written down
        // before this ticket existed:
        //
        //   1. CONTRACTS.md §1, FROZEN, safety rule: "There is no teleport endpoint, no god-mode,
        //      no money, no weapon-giving endpoint." CLAUDE.md §Contracts: the contract wins until
        //      it is formally changed. Shipping this ON would put the bridge in breach of a frozen
        //      contract on the very tick it loads.
        //   2. docs/research/brief-combat-natives.json's FAIRNESS JUDGMENTS list names
        //      GIVE_WEAPON_TO_PED explicitly among the "CHEATS to refuse", next to
        //      CA_PERFECT_ACCURACY and CLEAR_PLAYER_WANTED_LEVEL.
        //
        // The code is complete and reports itself in player.weapon.loadout. The default is
        // "ammunation" (CONTRACTS v1.14, operator decision); WASTED_BRIDGE_LOADOUT=off means the agent keeps only what he earned in the game, every
        // selection and combat verb below works identically on whatever that turns out to be, and
        // `player.weapon.loadout` tells the harness (and therefore the site, and therefore the
        // viewer) which of the two he is living under. Flipping the default is a one-word change
        // for whoever signs off the contract bump; it is not this executor's call to make silently.
        internal const string LoadoutEnvVar = "WASTED_BRIDGE_LOADOUT";
        internal const string LoadoutOff = "off";
        internal const string LoadoutAmmunation = "ammunation";

        // One magazine plus a spare, per weapon. Chosen to be obviously modest: the pistol's own
        // default clip is 12, the micro SMG's 16, the pump shotgun's 8.
        private const int PistolAmmo = 60;
        private const int SmgAmmo = 90;
        private const int ShotgunAmmo = 24;

        /// <summary>Range at/inside which the shotgun is the honest choice for `weapon: "armed"`.
        /// Bridge-side rather than harness-side on purpose: the harness's snapshot is up to a poll
        /// period (250-500 ms) old, and this is read at the instant the task starts — the same
        /// reasoning fight_ped already documents for reading the target's weapon class fresh.</summary>
        private const float ShotgunRangeM = 10f;

        private static string _mode;
        private static bool _modeLogged;
        private static bool _wasDead;
        private static bool _wasArrested;
        private static bool _prepared;

        /// <summary>"off" | "ammunation", read once from the environment and logged once.</summary>
        internal static string Mode
        {
            get
            {
                if (_mode != null)
                {
                    return _mode;
                }
                string raw = null;
                try
                {
                    raw = System.Environment.GetEnvironmentVariable(LoadoutEnvVar);
                }
                catch (System.Exception)
                {
                    raw = null;
                }
                string value = raw == null ? "" : raw.Trim().ToLowerInvariant();
                if (value == LoadoutOff)
                {
                    // Explicit opt-out wins: he keeps only what he picks up in the world.
                }
                else
                {
                    if (value.Length > 0 && value != LoadoutAmmunation)
                    {
                        BridgeLog.Warn(LoadoutEnvVar + "=\"" + raw + "\" is not one of off|ammunation; "
                                       + "using the default (ammunation)");
                    }
                    // DEFAULT = ammunation, by operator decision recorded in CONTRACTS v1.14: a human
                    // with the agent's cash buys exactly these three at Ammu-Nation, so this is not a
                    // cheat under CLAUDE.md rule 5. What a human cannot do (infinite ammo, perfect
                    // accuracy, a minigun from nowhere) stays refused.
                    value = LoadoutAmmunation;
                }
                _mode = value;
                return _mode;
            }
        }

        /// <summary>
        /// Called once per tick from the script's Tick handler, with the same `dead` and
        /// `arrested` flags the snapshot is built from. Applies the loadout at session start and
        /// again on every death->alive AND arrest->free edge, and does nothing at all — not one
        /// native call — while the mode is off.
        ///
        /// WHY ARREST COUNTS. Busted does not kill him, so the death edge never fires — but the
        /// game STRIPS AMMO on arrest while leaving the weapons themselves in his inventory.
        /// Observed live 2026-09-03: `player.weapon` reported all three weapons `owned` with
        /// ammo 0/0/0 and class "unarmed", and it stayed that way indefinitely because only a
        /// death re-applied the loadout. Every armed free-roam goal gates on being armed, so one
        /// arrest silently removed the whole violence half of the catalog for the rest of the
        /// session. This is a top-up of the SAME modest loadout on an edge that already strips
        /// it, not a new grant: the "no weapon-giving" safety rule is untouched.
        /// </summary>
        internal static void Maintain(Ped ped, bool dead, bool arrested)
        {
            if (!_modeLogged)
            {
                _modeLogged = true;
                BridgeLog.Info("weapon loadout mode: " + Mode
                               + (Mode == LoadoutOff
                                  ? " (no weapon is ever given; CONTRACTS §1 safety rule)"
                                  : " (pistol + micro SMG + pump shotgun, modest ammo, no infinite ammo)"));
            }
            if (Mode == LoadoutOff)
            {
                return;
            }
            if (ped == null || !ped.Exists())
            {
                return;
            }
            bool respawned = _wasDead && !dead;
            bool released = _wasArrested && !arrested;
            _wasDead = dead;
            _wasArrested = arrested;
            if (dead || arrested)
            {
                return;
            }
            if (_prepared && !respawned && !released)
            {
                return;
            }
            if (released && !respawned && !StrippedOfAmmo(ped))
            {
                // IS_PLAYER_BEING_ARRESTED can flicker true for a frame during a chase without
                // an actual Busted. Re-giving on every such blip would be a top-up loop, and a
                // top-up loop is infinite ammo wearing a different hat — the one thing the
                // loadout's own comment promises it is not. A real arrest always leaves him
                // with nothing in the magazine, so that, not the edge alone, is the condition.
                return;
            }
            _prepared = true;
            // equipNow: false on all three — being handed a gun should not put it in his hands.
            // What he HOLDS is decided by the task he is running (see SelectFor*), and a the agent
            // walking down the street with a shotgun out is a wanted level, not a loadout.
            // isAmmoLoaded: true matches SHVDN's own documented no-op default.
            ped.Weapons.Give(WeaponHash.Pistol, PistolAmmo, false, true);
            ped.Weapons.Give(WeaponHash.MicroSMG, SmgAmmo, false, true);
            ped.Weapons.Give(WeaponHash.PumpShotgun, ShotgunAmmo, false, true);
            BridgeLog.Info("weapon loadout applied ("
                           + (respawned ? "after death" : released ? "after arrest" : "session start")
                           + "): Pistol " + PistolAmmo + ", MicroSMG " + SmgAmmo
                           + ", PumpShotgun " + ShotgunAmmo + "; no infinite ammo, nothing equipped");
        }

        // ---- selection ------------------------------------------------------------------------

        /// <summary>Put his fists up. `pick_a_fight` is a bit; an execution is not.</summary>
        internal static void SelectUnarmed(Ped ped)
        {
            ped.Weapons.Select(WeaponHash.Unarmed, true);
        }

        /// <summary>
        /// The `weapon: "armed"` / shoot_at rule: pump shotgun inside <see cref="ShotgunRangeM"/>,
        /// pistol beyond it, and whatever he already has in his hands if he owns neither.
        /// Returns the hash actually selected, for the firing pattern and the log.
        /// </summary>
        internal static WeaponHash SelectForRange(Ped ped, float distanceM)
        {
            if (distanceM <= ShotgunRangeM && ped.Weapons.HasWeapon(WeaponHash.PumpShotgun))
            {
                ped.Weapons.Select(WeaponHash.PumpShotgun, true);
                return WeaponHash.PumpShotgun;
            }
            if (ped.Weapons.HasWeapon(WeaponHash.Pistol))
            {
                ped.Weapons.Select(WeaponHash.Pistol, true);
                return WeaponHash.Pistol;
            }
            return CurrentHash(ped);
        }

        /// <summary>The drive-by rule: the micro SMG, because that is the one that works out of a
        /// car window. Falls back to whatever he is holding rather than failing the task — a
        /// drive-by with a pistol is a worse drive-by, not an impossible one.</summary>
        internal static WeaponHash SelectForDriveBy(Ped ped)
        {
            if (ped.Weapons.HasWeapon(WeaponHash.MicroSMG))
            {
                ped.Weapons.Select(WeaponHash.MicroSMG, true);
                return WeaponHash.MicroSMG;
            }
            return CurrentHash(ped);
        }

        /// <summary>
        /// A firing pattern matched to the weapon, from the pinned GTA.FiringPattern enum (members
        /// verified present by reflection over the pinned DLL). This is a CADENCE, not an accuracy
        /// dial: nothing here touches SET_PED_ACCURACY or SET_PED_SHOOT_RATE, which the combat
        /// research brief lists among the cheats to refuse.
        /// </summary>
        internal static FiringPattern PatternFor(WeaponHash hash)
        {
            switch (hash)
            {
                case WeaponHash.PumpShotgun:
                    return FiringPattern.BurstFirePumpShotGun;
                case WeaponHash.MicroSMG:
                    return FiringPattern.BurstFireSMG;
                case WeaponHash.Pistol:
                    return FiringPattern.BurstFirePistol;
                default:
                    return FiringPattern.Default;
            }
        }

        /// <summary>True when all three tracked weapons are carrying zero rounds — what the game
        /// leaves behind after a Busted (weapons kept, ammo stripped). Never throws: on a native
        /// failure it answers false, so the loadout is NOT re-applied and he simply keeps what he
        /// has, which is the same safe direction every other read in this file degrades in.</summary>
        private static bool StrippedOfAmmo(Ped ped)
        {
            try
            {
                return AmmoFor(ped, WeaponHash.Pistol) == 0
                       && AmmoFor(ped, WeaponHash.MicroSMG) == 0
                       && AmmoFor(ped, WeaponHash.PumpShotgun) == 0;
            }
            catch (System.Exception)
            {
                return false;
            }
        }

        private static int AmmoFor(Ped ped, WeaponHash hash)
        {
            if (!ped.Weapons.HasWeapon(hash))
            {
                return 0;
            }
            Weapon w = ped.Weapons[hash];
            return w == null ? 0 : w.Ammo;
        }

        internal static WeaponHash CurrentHash(Ped ped)
        {
            Weapon current = ped.Weapons.Current;
            return current == null ? WeaponHash.Unarmed : current.Hash;
        }

        // ---- the /state read ------------------------------------------------------------------

        /// <summary>
        /// `player.weapon` for the snapshot. Never throws and never returns null: on any native
        /// failure it serves "unarmed, owns nothing", which is the state in which every
        /// weapon-gated goal in the harness refuses itself — the safe degradation, and the same
        /// one PhoneState makes.
        /// </summary>
        internal static WeaponDto Read(Ped ped)
        {
            var dto = new WeaponDto { Loadout = Mode };
            if (ped == null || !ped.Exists())
            {
                return dto;
            }
            try
            {
                WeaponHash hash = CurrentHash(ped);
                dto.Name = hash.ToString();
                dto.Class = ClassOf(ped);
                Weapon current = ped.Weapons.Current;
                dto.Ammo = current == null ? 0 : current.Ammo;
                // The tracked set is the loadout set and nothing else: HAS_PED_GOT_WEAPON is a
                // per-weapon question and there is no "list his weapons" native worth calling
                // every tick, so the contract says plainly that a name missing from `owned` means
                // "not one of the three we track", never "he is unarmed".
                AddOwned(dto, ped, WeaponHash.Pistol);
                AddOwned(dto, ped, WeaponHash.MicroSMG);
                AddOwned(dto, ped, WeaponHash.PumpShotgun);
            }
            catch (System.Exception)
            {
                return new WeaponDto { Loadout = Mode };
            }
            return dto;
        }

        private static void AddOwned(WeaponDto dto, Ped ped, WeaponHash hash)
        {
            if (!ped.Weapons.HasWeapon(hash))
            {
                return;
            }
            Weapon w = ped.Weapons[hash];
            dto.Owned[hash.ToString()] = w == null ? 0 : w.Ammo;
        }

        /// <summary>
        /// The same four-value vocabulary `nearby.peds[].weapon_class` already uses, over the same
        /// IS_PED_ARMED type flags. Re-implemented here rather than calling
        /// SnapshotBuilder.WeaponClassOf (which is private) so this capability stays in one file
        /// and two parallel workstreams do not have to edit the same method to share four lines.
        /// </summary>
        private static string ClassOf(Ped ped)
        {
            if (Function.Call<bool>(Hash.IS_PED_ARMED, ped.Handle, 4))
            {
                return "gun";
            }
            if (Function.Call<bool>(Hash.IS_PED_ARMED, ped.Handle, 2))
            {
                return "projectile";
            }
            if (Function.Call<bool>(Hash.IS_PED_ARMED, ped.Handle, 1))
            {
                return "melee";
            }
            return "unarmed";
        }
    }
}
