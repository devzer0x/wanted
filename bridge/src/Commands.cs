using System.Threading;
using GTA;
using Newtonsoft.Json.Linq;

namespace WastedBridge
{
    /// <summary>
    /// Fully validated /task request. Built on the HTTP thread (validation only, no natives),
    /// consumed on the game thread. Fields are only meaningful for the task type they belong to.
    /// </summary>
    internal sealed class TaskRequest
    {
        public string Id;
        public string Type;

        public float X, Y, Z;               // drive_to, walk_to, set_waypoint; fly_to (1.8.0: Z is the CRUISE ALTITUDE above sea level, not ground)
        public float SpeedMps;              // drive_to; follow_entity in-vehicle tail (v1.9, reuses this field); fly_to (1.8.0)
        public VehicleDrivingFlags Style;   // drive_to, wander_drive; follow_entity in-vehicle tail (v1.9, reuses this field)
        public float ArriveRadiusM;         // drive_to; fly_to (1.8.0)
        public bool Run;                    // walk_to
        public string Prefer;               // enter_nearest_vehicle: "nicer" | "any"
        public float SearchRadiusM;         // enter_nearest_vehicle
        public float RadiusM;               // combat_hated_targets_around
        public float DurationS;             // seek_cover
        public int Handle;                  // follow_entity
        public bool InVehicle;              // follow_entity

        // --- bridge 1.7.0 (fix-opus-b, T6) ---------------------------------------------------
        // fight_ped: "auto" | "unarmed" | "armed". Default "auto" is byte-for-byte the v1.11
        // behaviour (melee vs ranged chosen from the TARGET's weapon class), so a caller that
        // never heard of the field gets exactly what it used to.
        public string WeaponMode;
        // enter_vehicle_seat: 0 front passenger, 1 rear-left, 2 rear-right. The DRIVER's seat
        // (-1) is deliberately unreachable through this task - that is enter_nearest_vehicle,
        // which has the "nicer" chooser and the seat verification that go with driving.
        public int Seat;
    }

    /// <summary>
    /// Synchronization handle for POST endpoints whose effect must run on the game thread
    /// (/timescale, /control, /radio, /horn, /unstick). The HTTP thread blocks on Wait with a
    /// short timeout so the 200 it returns reflects work that actually happened; natives are
    /// still only ever called by the game thread.
    /// </summary>
    internal sealed class CommandReply
    {
        private readonly ManualResetEventSlim _done = new ManualResetEventSlim(false);
        private volatile bool _abandoned;

        public int StatusCode { get; private set; }
        public JObject Body { get; private set; }

        public void Complete(int statusCode, JObject body)
        {
            if (_abandoned)
            {
                return;
            }
            StatusCode = statusCode;
            Body = body;
            _done.Set();
        }

        public bool Wait(int timeoutMs)
        {
            return _done.Wait(timeoutMs);
        }

        /// <summary>
        /// Called by the HTTP thread when it gave up waiting. The event is deliberately never
        /// disposed: the game thread may still hold this reply and must not fault on Complete.
        /// </summary>
        public void Abandon()
        {
            _abandoned = true;
        }
    }

    internal enum CommandKind
    {
        NewTask,
        SetTimescale,
        SetControl,
        SetRadio,
        Horn,
        Unstick
    }

    /// <summary>One queued unit of work handed from the HTTP thread to the game thread.</summary>
    internal sealed class BridgeCommand
    {
        public CommandKind Kind;
        public TaskRequest Task;      // NewTask
        public float TimescaleValue;  // SetTimescale (already clamped)
        public bool ControlEnabled;   // SetControl
        public string RadioStation;   // SetRadio ("off" or a station name)
        public int HornMs;            // Horn (already clamped)
        public CommandReply Reply;    // null for NewTask (202 was already returned)
    }
}
