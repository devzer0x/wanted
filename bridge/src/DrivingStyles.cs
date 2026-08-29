using System;
using System.Globalization;
using System.Text;
using GTA;

namespace WastedBridge
{
    /// <summary>
    /// CONTRACTS.md §1 driving-style names mapped to VehicleDrivingFlags bitfields (decision D3).
    /// The names are the contract; the bit values may be tuned empirically in Phase 3 without a
    /// contract change.
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
        /// <summary>D3 normal = 786603: stop for cars/peds, obey lights, use shortcuts.</summary>
        public const VehicleDrivingFlags Normal =
            VehicleDrivingFlags.DrivingModeStopForVehicles;

        /// <summary>D3 rushed = 1074528293: avoid vehicles, plus join the road in its direction.</summary>
        public const VehicleDrivingFlags Rushed =
            VehicleDrivingFlags.DrivingModeAvoidVehicles
            | VehicleDrivingFlags.ForceJoinInRoadDirection;

        /// <summary>D3 ignore_lights = 786475: normal minus StopAtTrafficLights.</summary>
        public const VehicleDrivingFlags IgnoreLights =
            VehicleDrivingFlags.DrivingModeStopForVehiclesIgnoreLights;

        /// <summary>D3 avoid_traffic = 786468: swerve hard, no stopping for peds.</summary>
        public const VehicleDrivingFlags AvoidTraffic =
            VehicleDrivingFlags.DrivingModeAvoidVehiclesReckless;

        // Decimal values frozen by CONTRACTS §1 / RESEARCH D3.
        private const uint NormalValue = 786603;
        private const uint RushedValue = 1074528293;
        private const uint IgnoreLightsValue = 786475;
        private const uint AvoidTrafficValue = 786468;

        // The enum members each style is composed from, by name. Verify() looks these up in the
        // loaded assembly's metadata, which is the only way a renumbering can be noticed at run
        // time — the constants above are literals by the time this code runs.
        private const string MemberStopForVehicles = "DrivingModeStopForVehicles";
        private const string MemberAvoidVehicles = "DrivingModeAvoidVehicles";
        private const string MemberForceJoinInRoadDirection = "ForceJoinInRoadDirection";
        private const string MemberIgnoreLights = "DrivingModeStopForVehiclesIgnoreLights";
        private const string MemberAvoidVehiclesReckless = "DrivingModeAvoidVehiclesReckless";

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
            CheckStyle(drift, "normal", Normal, NormalValue, MemberStopForVehicles);
            CheckStyle(drift, "rushed", Rushed, RushedValue,
                MemberAvoidVehicles, MemberForceJoinInRoadDirection);
            CheckStyle(drift, "ignore_lights", IgnoreLights, IgnoreLightsValue, MemberIgnoreLights);
            CheckStyle(drift, "avoid_traffic", AvoidTraffic, AvoidTrafficValue,
                MemberAvoidVehiclesReckless);
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
