using System;

namespace WastedBridge
{
    /// <summary>
    /// Edition detection state machine for <c>/health.edition</c> and <c>/state.bridge.edition</c>
    /// (CONTRACTS v1.2 enum: legacy | enhanced | unknown).
    ///
    /// The reason this is its own object rather than two lines in the tick handler: the first
    /// implementation latched after a single attempt, so one early <c>Game.FileVersion</c> throw
    /// (it is read on the first tick, which can land on a loading screen) pinned
    /// <c>edition: "unknown"</c> for the entire session. Detection now keeps retrying — at most
    /// once per <see cref="RetryIntervalMs"/> so a permanently failing read costs one native call
    /// per five seconds — and latches only on success.
    ///
    /// Pure and native-free by construction: the caller reads <c>Game.FileVersion</c> on the game
    /// thread and hands the value (or null, when the read threw) to <see cref="Accept"/>. That is
    /// what makes the policy provable off-server — bridge/tools/offline-checks exercises it.
    /// </summary>
    internal sealed class EditionDetector
    {
        public const string Unknown = "unknown";
        public const string Legacy = "legacy";
        public const string Enhanced = "enhanced";

        /// <summary>Minimum gap between detection attempts, in milliseconds.</summary>
        public const int RetryIntervalMs = 5000;

        /// <summary>
        /// Shipping Legacy builds are 1.0.3xxx.x (currently 1.0.3889.0); Enhanced builds are
        /// 1.0.1158.x and climb slowly. 2000 splits the two ranges (RESEARCH.md D1).
        /// </summary>
        public const int LegacyMinBuild = 2000;

        private string _edition = Unknown;
        private int _attempts;
        private int _lastAttemptAt;

        /// <summary>The value currently reported by /health and /state; never null.</summary>
        public string Edition
        {
            get { return _edition; }
        }

        /// <summary>True once a real game version has been classified; only then does it latch.</summary>
        public bool Resolved
        {
            get { return _edition != Unknown; }
        }

        public int Attempts
        {
            get { return _attempts; }
        }

        /// <summary>
        /// True when the caller should read Game.FileVersion this tick: immediately on the first
        /// tick, then every <see cref="RetryIntervalMs"/> until detection succeeds, never again
        /// afterwards. <paramref name="nowMs"/> is Environment.TickCount (wraps; compared unchecked).
        /// </summary>
        public bool ShouldAttempt(int nowMs)
        {
            if (Resolved)
            {
                return false;
            }
            if (_attempts == 0)
            {
                return true;
            }
            return unchecked(nowMs - _lastAttemptAt) >= RetryIntervalMs;
        }

        /// <summary>
        /// Records one attempt. <paramref name="version"/> is null when the read failed. Returns
        /// the newly detected edition, or null when this attempt did not resolve it (the reported
        /// edition then stays "unknown" and another attempt follows later).
        /// </summary>
        public string Accept(int nowMs, Version version)
        {
            _attempts++;
            _lastAttemptAt = nowMs;
            string edition = Classify(version);
            if (edition != null)
            {
                _edition = edition;
            }
            return edition;
        }

        /// <summary>Version → contract edition, or null when the version could not be read.</summary>
        public static string Classify(Version version)
        {
            if (version == null)
            {
                return null;
            }
            return version.Build >= LegacyMinBuild ? Legacy : Enhanced;
        }
    }
}
