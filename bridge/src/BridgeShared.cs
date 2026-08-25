using System;
using System.Collections.Concurrent;
using System.Threading;

namespace WastedBridge
{
    /// <summary>
    /// The only state shared between the game thread and HTTP threads. The game thread writes
    /// (publishes immutable snapshots, completes commands); HTTP threads read and enqueue.
    /// </summary>
    internal sealed class BridgeShared
    {
        public readonly ConcurrentQueue<BridgeCommand> Commands = new ConcurrentQueue<BridgeCommand>();

        private volatile string _stateJson;
        private volatile Snapshot _lastSnapshot;
        private volatile bool _onlineBlocked;
        private volatile string _edition = "unknown";
        private volatile float _tickHz;
        private volatile float _gameFps;
        private long _lastTickUnixMs;
        private int _taskCounter;

        public string StateJson
        {
            get { return _stateJson; }
            set { _stateJson = value; }
        }

        /// <summary>Immutable-after-publish snapshot for HTTP-side prechecks (no natives).</summary>
        public Snapshot LastSnapshot
        {
            get { return _lastSnapshot; }
            set { _lastSnapshot = value; }
        }

        public bool OnlineBlocked
        {
            get { return _onlineBlocked; }
            set { _onlineBlocked = value; }
        }

        public string Edition
        {
            get { return _edition; }
            set { _edition = value; }
        }

        public float TickHz
        {
            get { return _tickHz; }
            set { _tickHz = value; }
        }

        public float GameFps
        {
            get { return _gameFps; }
            set { _gameFps = value; }
        }

        public void MarkTickNow()
        {
            Interlocked.Exchange(ref _lastTickUnixMs, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        }

        public long MillisSinceLastTick()
        {
            long last = Interlocked.Read(ref _lastTickUnixMs);
            if (last == 0)
            {
                return long.MaxValue;
            }
            return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - last;
        }

        public string NextTaskId()
        {
            int n = Interlocked.Increment(ref _taskCounter);
            return "t-" + n.ToString("D6");
        }
    }
}
