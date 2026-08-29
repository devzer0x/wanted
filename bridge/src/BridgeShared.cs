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
        /// <summary>
        /// Hard cap on queued commands. The queue only grows when the game thread has stopped
        /// ticking (loading screen, hang), and an unbounded queue would turn that into a slow
        /// memory leak plus a burst of stale commands the moment the game resumes.
        /// </summary>
        public const int MaxQueueDepth = 128;

        private readonly ConcurrentQueue<BridgeCommand> _commands = new ConcurrentQueue<BridgeCommand>();

        private volatile string _stateJson;
        private volatile Snapshot _lastSnapshot;
        private volatile bool _onlineBlocked;
        private volatile string _edition = "unknown";
        private volatile float _tickHz;
        private volatile float _gameFps;
        private long _lastTickUnixMs;
        private int _taskCounter;
        private int _queueDepth;

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

        /// <summary>O(1) queue depth; ConcurrentQueue.Count walks segments.</summary>
        public int QueueDepth
        {
            get { return Volatile.Read(ref _queueDepth); }
        }

        /// <summary>
        /// Enqueues unless the queue is at <see cref="MaxQueueDepth"/>. HTTP threads only.
        /// Reserves the slot before enqueueing so concurrent requests cannot both slip past the
        /// bound.
        /// </summary>
        public bool TryEnqueue(BridgeCommand command)
        {
            if (Interlocked.Increment(ref _queueDepth) > MaxQueueDepth)
            {
                Interlocked.Decrement(ref _queueDepth);
                return false;
            }
            _commands.Enqueue(command);
            return true;
        }

        /// <summary>Game thread only.</summary>
        public bool TryDequeue(out BridgeCommand command)
        {
            if (!_commands.TryDequeue(out command))
            {
                return false;
            }
            Interlocked.Decrement(ref _queueDepth);
            return true;
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
