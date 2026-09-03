using GTA;
using GTA.Native;

namespace WastedBridge
{
    /// <summary>
    /// CONTRACTS v1.13 <c>phone</c>: is the cellphone RINGING, and is a call CONNECTED.
    ///
    /// WHY A PROXY AND NOT "THE RINGING FLAG". There is no native that reports "an incoming call
    /// is ringing". The game keeps that in a script global whose index is build-specific, and
    /// reading a hard-coded global index would break silently on the next game update — exactly
    /// the class of guess CLAUDE.md rule 6 forbids. <c>CAN_PHONE_BE_SEEN_ON_SCREEN</c> is not the
    /// answer either: it is hard-coded to return 1 in this engine, so it says "yes" always.
    ///
    /// So this reads the only honest sound-level proxy the native API exposes:
    ///
    ///     ringing := IS_PED_RINGTONE_PLAYING(playerPed) AND NOT IS_MOBILE_PHONE_CALL_ONGOING()
    ///
    /// THE "AND NOT" IS LOAD-BEARING, not defensive tidiness. IS_PED_RINGTONE_PLAYING
    /// (0x1E8E5E20937E3137, BOOL(Ped)) reports "this ped's phone is making ringtone noise", which
    /// is also true while the player DIALS OUT and while a custom ringtone plays. On its own it
    /// would tell the harness "someone is calling you" during the agent's own outgoing call and the
    /// reject reflex would hang up on him. IS_MOBILE_PHONE_CALL_ONGOING (0x7497D2CE2C30D24C,
    /// BOOL()) is what separates "the phone is making noise and nobody has picked up" from "a call
    /// is live"; ANDing them is what makes the field mean what its name says.
    ///
    /// Both hashes were verified present in the pinned SHVDN v3.7.0.189
    /// (lib/ScriptHookVDotNet3.dll) GTA.Native.Hash enum by reflection, and their values match
    /// alloc8or's NativeDB: IS_PED_RINGTONE_PLAYING = 0x1E8E5E20937E3137,
    /// IS_MOBILE_PHONE_CALL_ONGOING = 0x7497D2CE2C30D24C. Neither has a typed SHVDN wrapper, so
    /// both are raw <c>Function.Call</c> — the same pattern IS_PED_BEING_JACKED / GET_PEDS_JACKER
    /// already use in SnapshotBuilder.
    ///
    /// WHAT THIS CANNOT TELL YOU, stated here rather than pretended away: it cannot distinguish a
    /// STORY call (Simeon, Lester, Lamar — answering one starts a mission) from a friend's
    /// hang-out invite, and it cannot see the on-screen soft keys, so it cannot know in advance
    /// whether a given call is rejectable at all. The task engine handles the second one by
    /// timing out rather than looping (see TaskEngine's phone section); the first is a policy
    /// decision the harness makes from `Settings.missions_enabled`, not a fact the bridge has.
    /// </summary>
    internal static class PhoneState
    {
        /// <summary>
        /// Reads both natives once. Game thread only, like everything else that touches natives.
        /// </summary>
        public static PhoneDto Read(Ped playerPed)
        {
            // Read the ongoing-call flag FIRST so both halves of the AND describe the same frame.
            bool inCall = Function.Call<bool>(Hash.IS_MOBILE_PHONE_CALL_ONGOING);
            bool ringtone = Function.Call<bool>(Hash.IS_PED_RINGTONE_PLAYING, playerPed.Handle);
            return new PhoneDto
            {
                Ringing = ringtone && !inCall,
                InCall = inCall
            };
        }
    }
}
