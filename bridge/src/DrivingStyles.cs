using GTA;

namespace WastedBridge
{
    /// <summary>
    /// CONTRACTS.md §1 driving-style names mapped to VehicleDrivingFlags bitfields (decision D3).
    /// The names are the contract; the bit values may be tuned empirically in Phase 3 without a
    /// contract change. Values cross-checked against the pinned SHVDN nightly's
    /// VehicleDrivingFlags enum (DrivingModeStopForVehicles = 786603 etc.).
    /// </summary>
    internal static class DrivingStyles
    {
        public const uint Normal = 786603;        // stop for cars/peds, obey lights, use shortcuts
        public const uint Rushed = 1074528293;    // avoid vehicles + ForceJoinInRoadDirection
        public const uint IgnoreLights = 786475;  // normal minus StopAtTrafficLights behavior
        public const uint AvoidTraffic = 786468;  // swerve hard, no stopping for peds

        /// <summary>Returns false when the name is not one of the four contract styles.</summary>
        public static bool TryParse(string name, out VehicleDrivingFlags flags)
        {
            switch (name)
            {
                case "normal":
                    flags = (VehicleDrivingFlags)Normal;
                    return true;
                case "rushed":
                    flags = (VehicleDrivingFlags)Rushed;
                    return true;
                case "ignore_lights":
                    flags = (VehicleDrivingFlags)IgnoreLights;
                    return true;
                case "avoid_traffic":
                    flags = (VehicleDrivingFlags)AvoidTraffic;
                    return true;
                default:
                    flags = (VehicleDrivingFlags)Normal;
                    return false;
            }
        }
    }
}
