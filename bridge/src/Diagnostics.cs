using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Reflection;
using System.Security.Principal;
using GTA;
using GTA.Chrono;
using GTA.Math;

namespace WastedBridge
{
    /// <summary>
    /// First-run diagnostics. The server is remote, headless and expensive to iterate on, so a
    /// failed first attempt must explain itself from the log alone. Every probe is individually
    /// guarded: one failing line never hides the rest of the block.
    /// </summary>
    internal static class Diagnostics
    {
        /// <summary>SHVDN build this code was written and signature-checked against (bridge/README).</summary>
        public const string ExpectedShvdnVersion = "3.7.0.189";

        /// <summary>Environment block, logged from the constructor before anything can fail.</summary>
        public static void LogStartupBlock(string scriptDirectory, string logPath)
        {
            var lines = new List<string>();
            Add(lines, "bridge", delegate
            {
                return WastedBridgeScript.BridgeVersion + " (assembly "
                       + typeof(Diagnostics).Assembly.GetName().Version + ")";
            });
            Add(lines, "shvdn", DescribeShvdn);
            Add(lines, "newtonsoft", delegate
            {
                Assembly a = typeof(Newtonsoft.Json.JsonConvert).Assembly;
                return a.GetName().Version + " from " + SafeLocation(a);
            });
            lines.Add(Pad("script dir") + (scriptDirectory ?? "<null>"));
            lines.Add(Pad("log file") + (logPath ?? "<NONE WRITABLE — diagnostics are lost, fix ACLs>"));
            Add(lines, "process", DescribeProcess);
            Add(lines, "os / clr", delegate
            {
                return Environment.OSVersion.VersionString + " · CLR " + Environment.Version
                       + " · " + (Environment.Is64BitProcess ? "64-bit" : "32-bit");
            });
            BridgeLog.Block("WANTED bridge starting", lines);
        }

        /// <summary>
        /// Game-state block, logged once from the first successful tick. This is the line block that
        /// tells us whether natives actually resolved against the running game build.
        /// </summary>
        public static void LogFirstTickBlock(string edition, bool onlineSessionStarted,
                                             bool onlineGameInProgress, string transport)
        {
            var lines = new List<string>();
            Add(lines, "game build", delegate
            {
                return Game.FileVersion + " -> edition \"" + edition + "\"";
            });
            lines.Add(Pad("online guard") + "NETWORK_IS_SESSION_STARTED=" + onlineSessionStarted
                      + " NETWORK_IS_GAME_IN_PROGRESS=" + onlineGameInProgress
                      + (onlineSessionStarted || onlineGameInProgress
                          ? "  <<< BRIDGE DISABLED (story mode only)"
                          : "  (clear — story mode)"));
            lines.Add(Pad("http") + transport);
            Add(lines, "player ped", delegate
            {
                Ped ped = Game.Player.Character;
                Vector3 p = ped.Position;
                return "handle " + ped.Handle + " model 0x" + ped.Model.Hash.ToString("X8")
                       + " at " + Fmt(p) + " heading " + ped.Heading.ToString("F1")
                       + " health " + ped.Health + "/" + ped.MaxHealth;
            });
            Add(lines, "player state", delegate
            {
                Player pl = Game.Player;
                return "wanted " + pl.Wanted.WantedLevel + " · money " + pl.Money
                       + " · control " + pl.CanControlCharacter
                       + " · in vehicle " + pl.Character.IsInVehicle();
            });
            Add(lines, "world", delegate
            {
                Vector3 p = Game.Player.Character.Position;
                return "street \"" + World.GetStreetName(p) + "\" zone \""
                       + World.GetZoneLocalizedName(p) + "\" clock "
                       + GameClock.Hour.ToString("D2", CultureInfo.InvariantCulture) + ":"
                       + GameClock.Minute.ToString("D2", CultureInfo.InvariantCulture)
                       + " weather " + World.Weather + " timescale " + Game.TimeScale.ToString("F2");
            });
            Add(lines, "mission flags", delegate
            {
                return "active=" + Game.IsMissionActive
                       + " random_event=" + Game.IsRandomEventActive
                       + " cutscene=" + Game.IsCutsceneActive;
            });
            Add(lines, "blip scan", delegate
            {
                // World.GetAllBlips is an SHVDN memory scan — the single most edition-fragile call
                // in the bridge. Probing it here means a first-run failure names it explicitly.
                Blip[] blips = World.GetAllBlips(BlipSprite.Standard);
                return (blips == null ? 0 : blips.Length) + " Standard-sprite blips (memory scan OK)";
            });
            Add(lines, "world scan", delegate
            {
                Ped ped = Game.Player.Character;
                Vehicle[] v = World.GetNearbyVehicles(ped, 80f);
                Ped[] p = World.GetNearbyPeds(ped, 50f);
                return (v == null ? 0 : v.Length) + " vehicles / " + (p == null ? 0 : p.Length)
                       + " peds within 80/50 m";
            });
            BridgeLog.Block("WANTED bridge first tick", lines);
        }

        private static string DescribeShvdn()
        {
            Assembly asm = typeof(Script).Assembly;
            Version v = asm.GetName().Version;
            string status = string.Equals(v.ToString(), ExpectedShvdnVersion, StringComparison.Ordinal)
                ? "matches the pinned build"
                : "!!! MISMATCH — this bridge was signature-checked against " + ExpectedShvdnVersion
                  + "; nightly API churn (DriveTo argument order, Player.Wanted, GameClock) can "
                  + "change behaviour silently. Re-verify bridge/README before trusting it.";
            return asm.GetName().Name + " " + v + " (" + status + ")";
        }

        private static string DescribeProcess()
        {
            Process p = Process.GetCurrentProcess();
            string user;
            string elevated;
            try
            {
                WindowsIdentity id = WindowsIdentity.GetCurrent();
                user = id.Name;
                elevated = new WindowsPrincipal(id)
                    .IsInRole(WindowsBuiltInRole.Administrator).ToString();
            }
            catch (Exception ex)
            {
                user = "<unknown>";
                elevated = "<unknown: " + ex.GetType().Name + ">";
            }
            return p.ProcessName + " pid " + p.Id + " · user " + user + " · elevated " + elevated;
        }

        private static string SafeLocation(Assembly a)
        {
            try
            {
                return string.IsNullOrEmpty(a.Location) ? "<no file>" : a.Location;
            }
            catch (Exception)
            {
                return "<unavailable>";
            }
        }

        private static string Fmt(Vector3 v)
        {
            return "(" + v.X.ToString("F1", CultureInfo.InvariantCulture) + ", "
                   + v.Y.ToString("F1", CultureInfo.InvariantCulture) + ", "
                   + v.Z.ToString("F1", CultureInfo.InvariantCulture) + ")";
        }

        private static void Add(List<string> lines, string label, Func<string> probe)
        {
            try
            {
                lines.Add(Pad(label) + probe());
            }
            catch (Exception ex)
            {
                lines.Add(Pad(label) + "FAILED: " + ex.GetType().Name + ": " + ex.Message);
            }
        }

        private static string Pad(string label)
        {
            return (label + "                 ").Substring(0, 17) + ": ";
        }
    }
}
