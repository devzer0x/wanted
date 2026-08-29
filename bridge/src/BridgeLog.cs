using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace WastedBridge
{
    /// <summary>
    /// Thread-safe append-only file logger. Prefers the game's scripts directory (next to the
    /// compiled script, where operators look first) but falls back to per-user writable locations
    /// because a game installed under Program Files can leave scripts/ read-only for the account
    /// the game runs as. Logging never throws and never takes the game thread down.
    /// </summary>
    internal static class BridgeLog
    {
        private const long MaxBytes = 5 * 1024 * 1024;
        private const string FileName = "WastedBridge.log";

        private static readonly object Gate = new object();
        private static string _path;
        private static bool _initialized;

        /// <summary>Resolved log path, or null when no writable location was found.</summary>
        public static string LogPath
        {
            get { lock (Gate) { return _path; } }
        }

        /// <summary>
        /// Picks the first writable log location, in order: the script's own directory,
        /// %LOCALAPPDATA%\WASTED, %TEMP%. Returns the chosen path (null if every candidate failed).
        /// Safe to call more than once; the first successful resolution wins.
        /// </summary>
        public static string Init(string scriptDirectory)
        {
            lock (Gate)
            {
                if (_initialized && _path != null)
                {
                    return _path;
                }
                _initialized = true;

                var attempts = new List<string>();
                foreach (string dir in CandidateDirectories(scriptDirectory))
                {
                    if (string.IsNullOrEmpty(dir))
                    {
                        continue;
                    }
                    string candidate = Path.Combine(dir, FileName);
                    string failure = TryClaim(dir, candidate);
                    if (failure == null)
                    {
                        _path = candidate;
                        if (attempts.Count > 0)
                        {
                            WriteLocked("WARN", "log directory fallback in use; earlier candidates "
                                                + "failed: " + string.Join(" | ", attempts.ToArray()));
                        }
                        return _path;
                    }
                    attempts.Add(candidate + " (" + failure + ")");
                }
                _path = null;
                return null;
            }
        }

        private static IEnumerable<string> CandidateDirectories(string scriptDirectory)
        {
            yield return scriptDirectory;
            string localAppData = SafeSpecialFolder(Environment.SpecialFolder.LocalApplicationData);
            if (!string.IsNullOrEmpty(localAppData))
            {
                yield return Path.Combine(localAppData, "WASTED");
            }
            yield return SafeTempPath();
        }

        private static string SafeSpecialFolder(Environment.SpecialFolder folder)
        {
            try
            {
                return Environment.GetFolderPath(folder);
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static string SafeTempPath()
        {
            try
            {
                return Path.GetTempPath();
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>Returns null when the path is usable, otherwise a short reason string.</summary>
        private static string TryClaim(string directory, string path)
        {
            try
            {
                if (!Directory.Exists(directory))
                {
                    Directory.CreateDirectory(directory);
                }
                // Opening for append is the only honest writability test: existence and ACL checks
                // both lie on Windows (virtualization, deny ACEs, read-only volumes).
                using (var fs = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
                {
                    fs.Flush();
                }
                return null;
            }
            catch (Exception ex)
            {
                return ex.GetType().Name;
            }
        }

        public static void Info(string message) { Write("INFO", message); }
        public static void Warn(string message) { Write("WARN", message); }
        public static void Error(string message) { Write("ERROR", message); }

        public static void Warn(string message, Exception ex) { Write("WARN", Describe(message, ex)); }
        public static void Error(string message, Exception ex) { Write("ERROR", Describe(message, ex)); }

        /// <summary>Writes a boxed multi-line block so startup diagnostics are one visual unit.</summary>
        public static void Block(string title, IList<string> lines)
        {
            var sb = new StringBuilder();
            sb.Append("===== ").Append(title).Append(' ');
            for (int i = title.Length + 7; i < 96; i++)
            {
                sb.Append('=');
            }
            Write("INFO", sb.ToString());
            for (int i = 0; i < lines.Count; i++)
            {
                Write("INFO", "  " + lines[i]);
            }
            Write("INFO", new string('=', 96));
        }

        private static string Describe(string message, Exception ex)
        {
            if (ex == null)
            {
                return message;
            }
            string where = "";
            try
            {
                string trace = ex.StackTrace;
                if (!string.IsNullOrEmpty(trace))
                {
                    int nl = trace.IndexOf('\n');
                    where = " @ " + (nl > 0 ? trace.Substring(0, nl) : trace).Trim();
                }
            }
            catch (Exception)
            {
            }
            string inner = ex.InnerException != null
                ? " <- " + ex.InnerException.GetType().Name + ": " + ex.InnerException.Message
                : "";
            return message + " :: " + ex.GetType().Name + ": " + ex.Message + inner + where;
        }

        private static void Write(string level, string message)
        {
            lock (Gate)
            {
                WriteLocked(level, message);
            }
        }

        private static void WriteLocked(string level, string message)
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
