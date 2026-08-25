using System;
using System.IO;
using System.Text;

namespace WastedBridge
{
    /// <summary>
    /// Thread-safe append-only file logger. Lives in the game's scripts directory next to the
    /// compiled script so operators find it without hunting (WastedBridge.log).
    /// </summary>
    internal static class BridgeLog
    {
        private const long MaxBytes = 5 * 1024 * 1024;
        private static readonly object Gate = new object();
        private static string _path;

        public static void Init(string directory)
        {
            lock (Gate)
            {
                _path = Path.Combine(directory, "WastedBridge.log");
            }
            Info("log initialized at " + _path);
        }

        public static void Info(string message) { Write("INFO", message); }
        public static void Warn(string message) { Write("WARN", message); }
        public static void Error(string message) { Write("ERROR", message); }

        public static void Error(string message, Exception ex)
        {
            Write("ERROR", message + " :: " + ex.GetType().Name + ": " + ex.Message);
        }

        private static void Write(string level, string message)
        {
            lock (Gate)
            {
                if (_path == null)
                {
                    return;
                }
                try
                {
                    RotateIfNeeded();
                    string line = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
                                  + " [" + level + "] " + message + Environment.NewLine;
                    File.AppendAllText(_path, line, Encoding.UTF8);
                }
                catch (IOException)
                {
                    // Never let logging take the game thread down.
                }
                catch (UnauthorizedAccessException)
                {
                }
            }
        }

        private static void RotateIfNeeded()
        {
            var info = new FileInfo(_path);
            if (!info.Exists || info.Length < MaxBytes)
            {
                return;
            }
            string old = _path + ".old";
            if (File.Exists(old))
            {
                File.Delete(old);
            }
            File.Move(_path, old);
        }
    }
}
