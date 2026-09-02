using System;
using System.Globalization;
using System.Text;
using GTA;

namespace WastedBridge
{
    /// <summary>
    /// CONTRACTS.md §1 driving-style names mapped to VehicleDrivingFlags bitfields (decision D3,
    /// retuned per the 2026-09-02 driving overhaul — docs/research/brief-driving-natives.json).
    /// The names are the contract; the bit values may be tuned empirically without a contract
    /// change ("The names are the contract; the bridge may tune the underlying bit values
    /// empirically").
    ///
    /// RETUNE (2026-09-02): <c>avoid_traffic</c> was <c>DrivingModeAvoidVehiclesReckless</c>
    /// (786468), documented by citizenfx/natives as "doesn't use the brakes at ALL to help with
    /// steering" — a crash generator, and the plausible cause of observed head-on/reckless crashes
    /// on stream. It is now composed from the non-reckless <c>DrivingModeAvoidVehicles</c> (786469)
    /// plus <c>AllowGoingWrongWay</c>/<c>GoOffRoadWhenAvoiding</c>, since CONTRACTS v1.9 already
    /// documents this as the style "available for a genuine chase" — i.e. the one pursuit-oriented
    /// style among the four contract names, so the brief's "add these to a pursuit-oriented style"
    /// item folds into this same retune rather than inventing a fifth style name. normal/rushed/
    /// ignore_lights are numerically unchanged (verified below: their pre-existing composite
    /// constants already include ChangeLanesAroundObstructions/UseShortCutLinks — see the OR'd-in
    /// members on each style, which are therefore idempotent, not a behavior change) but are still
    /// written as an explicit flag union rather than a single named constant, so the "every style
    /// that drives on roads carries ChangeLanesAroundObstructions/UseShortCutLinks" invariant the
    /// brief asks for is visible in the source, not just true by accident of which composite
    /// constant SHVDN happens to define.
    ///
    /// Expressed as named flags from the pinned SHVDN nightly rather than raw integers, so a
    /// future SHVDN bump that <em>renames or removes</em> a member breaks the build. Renaming is
    /// not the dangerous case though: a bump that silently <em>renumbers</em> a bit still compiles,
    /// because the C# compiler folds these consts into literals at build time. That is what
    /// <see cref="Verify"/> exists for — it re-reads the members from the SHVDN assembly actually
    /// loaded next to this DLL and re-composes each style from live metadata.
    /// </summary>
    internal static class DrivingStyles
    {
        /// <summary>786603: stop for cars/peds, obey lights, use shortcuts/lane-changes.</summary>
        public const VehicleDrivingFlags Normal =
            VehicleDrivingFlags.DrivingModeStopForVehicles
            | VehicleDrivingFlags.ChangeLanesAroundObstructions
            | VehicleDrivingFlags.UseShortCutLinks;

        /// <summary>1074528293: avoid vehicles, plus join the road in its direction.</summary>
        public const VehicleDrivingFlags Rushed =
            VehicleDrivingFlags.DrivingModeAvoidVehicles
            | VehicleDrivingFlags.ForceJoinInRoadDirection
            | VehicleDrivingFlags.ChangeLanesAroundObstructions
            | VehicleDrivingFlags.UseShortCutLinks;

        /// <summary>786475: normal minus StopAtTrafficLights.</summary>
        public const VehicleDrivingFlags IgnoreLights =
            VehicleDrivingFlags.DrivingModeStopForVehiclesIgnoreLights
            | VehicleDrivingFlags.ChangeLanesAroundObstructions
            | VehicleDrivingFlags.UseShortCutLinks;

        /// <summary>
        /// 787237 (RETUNED — was 786468/DrivingModeAvoidVehiclesReckless, the brake-free crash
        /// generator). Non-reckless avoid-vehicles, still willing to cross the centre line or cut
        /// off-road to keep up — the "genuine chase" style per CONTRACTS v1.9.
        /// </summary>
        public const VehicleDrivingFlags AvoidTraffic =
            VehicleDrivingFlags.DrivingModeAvoidVehicles
            | VehicleDrivingFlags.ChangeLanesAroundObstructions
            | VehicleDrivingFlags.UseShortCutLinks
            | VehicleDrivingFlags.AllowGoingWrongWay
            | VehicleDrivingFlags.GoOffRoadWhenAvoiding;

        // Decimal values this DLL is compiled against; Verify() checks the loaded assembly still
        // agrees. normal/rushed/ignore_lights match the original CONTRACTS §1/RESEARCH D3 numbers
        // (786603/1074528293/786475 respectively — the extra OR'd members above are already
        // present inside those composite constants, see the class comment). avoid_traffic is the
        // 2026-09-02 retune's own new frozen value, not D3's.
        private const uint NormalValue = 786603;
        private const uint RushedValue = 1074528293;
        private const uint IgnoreLightsValue = 786475;
        private const uint AvoidTrafficValue = 787237;

        // The enum members each style is composed from, by name. Verify() looks these up in the
        // loaded assembly's metadata, which is the only way a renumbering can be noticed at run
        // time — the constants above are literals by the time this code runs.
        private const string MemberStopForVehicles = "DrivingModeStopForVehicles";
        private const string MemberAvoidVehicles = "DrivingModeAvoidVehicles";
        private const string MemberForceJoinInRoadDirection = "ForceJoinInRoadDirection";
        private const string MemberIgnoreLights = "DrivingModeStopForVehiclesIgnoreLights";
        private const string MemberChangeLanesAroundObstructions = "ChangeLanesAroundObstructions";
        private const string MemberUseShortCutLinks = "UseShortCutLinks";
        private const string MemberAllowGoingWrongWay = "AllowGoingWrongWay";
        private const string MemberGoOffRoadWhenAvoiding = "GoOffRoadWhenAvoiding";

        /// <summary>Returns false when the name is not one of the four contract styles.</summary>
        public static bool TryParse(string name, out VehicleDrivingFlags flags)
        {
            switch (name)
            {
                case "normal":
                    flags = Normal;
                    return true;
                case "rushed":
                    flags = Rushed;
                    return true;
                case "ignore_lights":
                    flags = IgnoreLights;
                    return true;
                case "avoid_traffic":
                    flags = AvoidTraffic;
                    return true;
                default:
                    flags = Normal;
                    return false;
            }
        }

        /// <summary>
        /// Runtime drift guard, run once at startup. Reads GTA.VehicleDrivingFlags from the SHVDN
        /// assembly that is actually loaded (Enum.Parse against the runtime type — not the folded
        /// constants above), re-composes each contract style from those live values, and compares
        /// the result with both the D3 decimal and the constant this DLL was compiled with.
        ///
        /// Returns null when everything still agrees; otherwise a description of the drift:
        /// a member that no longer exists, a renumbered bit, or a compiled constant that disagrees
        /// with the assembly this DLL was loaded next to. It can and does fail — the offline
        /// checks run it against a deliberately renumbered SHVDN stub and require a non-null
        /// answer (bridge/tools/offline-checks).
        /// </summary>
        public static string Verify()
        {
            var drift = new StringBuilder();
            CheckStyle(drift, "normal", Normal, NormalValue,
                MemberStopForVehicles, MemberChangeLanesAroundObstructions, MemberUseShortCutLinks);
            CheckStyle(drift, "rushed", Rushed, RushedValue,
                MemberAvoidVehicles, MemberForceJoinInRoadDirection,
                MemberChangeLanesAroundObstructions, MemberUseShortCutLinks);
            CheckStyle(drift, "ignore_lights", IgnoreLights, IgnoreLightsValue,
                MemberIgnoreLights, MemberChangeLanesAroundObstructions, MemberUseShortCutLinks);
            CheckStyle(drift, "avoid_traffic", AvoidTraffic, AvoidTrafficValue,
                MemberAvoidVehicles, MemberChangeLanesAroundObstructions, MemberUseShortCutLinks,
                MemberAllowGoingWrongWay, MemberGoOffRoadWhenAvoiding);
            return drift.Length == 0 ? null : drift.ToString().Trim();
        }

        private static void CheckStyle(StringBuilder drift, string style,
                                       VehicleDrivingFlags compiled, uint contractValue,
                                       params string[] members)
        {
            uint loaded = 0;
            for (int i = 0; i < members.Length; i++)
            {
                uint memberValue;
                string failure = TryReadMember(members[i], out memberValue);
                if (failure != null)
                {
                    Append(drift, style + ": VehicleDrivingFlags." + members[i] + " " + failure);
                    return;
                }
                loaded |= memberValue;
            }

            if (loaded != contractValue)
            {
                Append(drift, style + ": the loaded SHVDN assembly composes "
                              + Str(loaded) + " from " + string.Join("|", members)
                              + ", CONTRACTS D3 says " + Str(contractValue));
            }
            if ((uint)compiled != loaded)
            {
                Append(drift, style + ": this DLL was compiled against " + Str((uint)compiled)
                              + " but the loaded SHVDN assembly now means " + Str(loaded));
            }
        }

        /// <summary>
        /// Reads one enum member's value out of the loaded assembly's metadata. Returns null on
        /// success, otherwise a short reason. typeof() resolves to the runtime type, so this sees
        /// whatever SHVDN build the game loaded — the whole point of the check.
        /// </summary>
        private static string TryReadMember(string member, out uint value)
        {
            value = 0;
            try
            {
                object parsed = Enum.Parse(typeof(VehicleDrivingFlags), member, false);
                value = Convert.ToUInt32(parsed, CultureInfo.InvariantCulture);
                return null;
            }
            catch (ArgumentException)
            {
                return "no longer exists";
            }
            catch (OverflowException)
            {
                return "no longer fits in a uint";
            }
            catch (InvalidCastException)
            {
                return "is not an integral enum member";
            }
        }

        private static void Append(StringBuilder drift, string message)
        {
            drift.Append(message).Append("; ");
        }

        private static string Str(uint value)
        {
            return value.ToString(CultureInfo.InvariantCulture);
        }
    }
}
