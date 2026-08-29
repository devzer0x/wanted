using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;

namespace WastedBridge
{
    /// <summary>
    /// Owns the Bridge HTTP API v1 endpoint on http://127.0.0.1:7777 (CONTRACTS.md §1): picks a
    /// transport, keeps trying if the first attempt fails, and never throws at its caller — a
    /// bridge that cannot bind must still tick, log why, and recover on its own, because the only
    /// operator is a remote desktop session on a server nobody is watching.
    /// </summary>
    internal sealed class HttpServer
    {
        public const string Host = "127.0.0.1";
        public const int Port = 7777;
        public const string Prefix = "http://" + Host + ":7777/";

        /// <summary>
        /// Operator override, read once at startup: <c>auto</c> (default) tries HTTP.SYS then the
        /// loopback socket; <c>socket</c> and <c>httpsys</c> pin one transport. Set it on the
        /// server only if the automatic choice misbehaves (documented in bridge/README.md).
        /// </summary>
        public const string TransportEnvVar = "WASTED_BRIDGE_TRANSPORT";

        private const int RebindRetryMs = 10000;
        // Win32 error codes HttpListener surfaces through HttpListenerException.ErrorCode.
        private const int ErrorAccessDenied = 5;
        private const int ErrorSharingViolation = 32;
        private const int ErrorAlreadyExists = 183;

        private readonly object _gate = new object();
        private readonly BridgeRouter _router;
        private readonly string _mode;

        private IBridgeTransport _active;
        private bool _stopped;
        private int _nextRetryAt = int.MinValue;
        private bool _httpSysRefused;
        private bool _failureLogged;
        private long _bindAttempts;

        public HttpServer(BridgeShared shared)
        {
            _router = new BridgeRouter(shared);
            _mode = ReadMode();
        }

        /// <summary>True once a transport is bound and accepting.</summary>
        public bool IsListening
        {
            get { lock (_gate) { return _active != null; } }
        }

        /// <summary>What the log and the diagnostics block should say about the endpoint.</summary>
        public string Description
        {
            get
            {
                lock (_gate)
                {
                    return _active != null ? _active.Description : "NOT LISTENING — see log for why";
                }
            }
        }

        /// <summary>Binds. Never throws; on failure the game thread keeps retrying via <see cref="EnsureStarted"/>.</summary>
        public void Start()
        {
            lock (_gate)
            {
                _stopped = false;
                TryBind();
            }
        }

        /// <summary>
        /// Called every tick. Cheap when already listening; otherwise retries on a timer. This is
        /// what recovers from the classic SHVDN reload race, where the previous script instance is
        /// still releasing port 7777 while the new one is constructed.
        /// </summary>
        public void EnsureStarted()
        {
            lock (_gate)
            {
                if (_stopped || _active != null)
                {
                    return;
                }
                int now = Environment.TickCount;
                if (unchecked(now - _nextRetryAt) < 0)
                {
                    return;
                }
                TryBind();
            }
        }

        public void Stop()
        {
            IBridgeTransport active;
            lock (_gate)
            {
                _stopped = true;
                active = _active;
                _active = null;
            }
            if (active != null)
            {
                active.Stop();
                BridgeLog.Info("HTTP API stopped (" + active.Description + ")");
            }
        }

        // ---- binding -----------------------------------------------------------------------

        private void TryBind()
        {
            if (_active != null)
            {
                return;
            }
            _nextRetryAt = unchecked(Environment.TickCount + RebindRetryMs);
            _bindAttempts++;

            foreach (IBridgeTransport transport in CandidateTransports())
            {
                try
                {
                    transport.Start();
                    _active = transport;
                    _failureLogged = false;
                    _bindAttempts = 0;
                    BridgeLog.Info("HTTP API listening on " + transport.Description);
                    return;
                }
                catch (HttpListenerException ex)
                {
                    NoteHttpSysFailure(ex);
                }
                catch (SocketException ex)
                {
                    LogBindFailure("loopback socket", ex.SocketErrorCode + "/" + ex.ErrorCode,
                        ex.Message, SocketAdvice(ex));
                }
                catch (PlatformNotSupportedException ex)
                {
                    _httpSysRefused = true;
                    BridgeLog.Warn("HttpListener is not supported on this platform; "
                                   + "falling back to the loopback socket transport", ex);
                }
                catch (Exception ex)
                {
                    BridgeLog.Error("transport " + transport.Description + " failed to start", ex);
                }
            }

            if (!_failureLogged)
            {
                _failureLogged = true;
                BridgeLog.Error("NO HTTP TRANSPORT COULD BIND " + Prefix
                                + " — the harness and the watchdog will see connection refused. "
                                + "Retrying every " + (RebindRetryMs / 1000) + " s. Check whether "
                                + "another process owns port " + Port
                                + " (netstat -ano | findstr :" + Port + ").");
            }
        }

        private IEnumerable<IBridgeTransport> CandidateTransports()
        {
            bool wantHttpSys = _mode != "socket" && !_httpSysRefused;
            bool wantSocket = _mode != "httpsys";
            if (wantHttpSys)
            {
                yield return new HttpSysTransport(Prefix, _router);
            }
            if (wantSocket)
            {
                yield return new SocketTransport(IPAddress.Loopback, Port, _router);
            }
        }

        private void NoteHttpSysFailure(HttpListenerException ex)
        {
            switch (ex.ErrorCode)
            {
                case ErrorAccessDenied:
                    // HTTP.SYS reserves URL namespaces per account. Microsoft's own guidance is
                    // that only the "localhost" host name is exempt for non-administrators;
                    // 127.0.0.1 needs an elevated token or a reservation. Never retry HTTP.SYS
                    // after this — an ACL will not appear by itself — go straight to the socket.
                    _httpSysRefused = true;
                    LogBindFailure("HTTP.SYS", "5 ERROR_ACCESS_DENIED", ex.Message,
                        "the account running the game may not reserve " + Prefix + ". Either run "
                        + "this once from an elevated prompt:  netsh http add urlacl url=" + Prefix
                        + " user=\"" + CurrentUserForNetsh() + "\"   — or do nothing and let the "
                        + "loopback socket transport serve the API, which needs no reservation.");
                    break;
                case ErrorSharingViolation:
                case ErrorAlreadyExists:
                    LogBindFailure("HTTP.SYS", ex.ErrorCode + " port in use", ex.Message,
                        "another listener still owns " + Prefix + ". This is normal for a few "
                        + "seconds after an SHVDN script reload; the bridge retries automatically.");
                    break;
                default:
                    LogBindFailure("HTTP.SYS", ex.ErrorCode.ToString(), ex.Message,
                        "falling through to the loopback socket transport.");
                    break;
            }
        }

        private static string SocketAdvice(SocketException ex)
        {
            if (ex.SocketErrorCode == SocketError.AddressAlreadyInUse)
            {
                return "another process owns 127.0.0.1:" + Port
                       + " (netstat -ano | findstr :" + Port + "). If it is a stale copy of the "
                       + "game, close it; the bridge retries automatically.";
            }
            if (ex.SocketErrorCode == SocketError.AccessDenied)
            {
                return "the socket bind was denied — check for a firewall or endpoint-protection "
                       + "rule blocking loopback listeners for this account.";
            }
            return "unexpected socket failure; the bridge retries automatically.";
        }

        /// <summary>
        /// Logs the first failure in full, then only every 60th attempt (≈ every 10 minutes), so a
        /// permanently occupied port cannot fill the log while the bridge keeps retrying.
        /// </summary>
        private void LogBindFailure(string what, string code, string message, string advice)
        {
            _failureLogged = true;
            if (_bindAttempts != 1 && _bindAttempts % 60 != 0)
            {
                return;
            }
            BridgeLog.Error("bind failed on " + what + " for " + Prefix + " [" + code + "]"
                            + (_bindAttempts > 1 ? " (attempt " + _bindAttempts + ")" : "") + ": "
                            + message + " -> " + advice);
        }

        private static string CurrentUserForNetsh()
        {
            try
            {
                string domain = Environment.UserDomainName;
                string user = Environment.UserName;
                return string.IsNullOrEmpty(domain) ? user : domain + "\\" + user;
            }
            catch (Exception)
            {
                return "Everyone";
            }
        }

        private static string ReadMode()
        {
            string raw;
            try
            {
                raw = Environment.GetEnvironmentVariable(TransportEnvVar);
            }
            catch (Exception)
            {
                return "auto";
            }
            if (string.IsNullOrEmpty(raw))
            {
                return "auto";
            }
            string mode = raw.Trim().ToLowerInvariant();
            if (mode != "auto" && mode != "socket" && mode != "httpsys")
            {
                BridgeLog.Warn(TransportEnvVar + "=\"" + raw
                               + "\" is not one of auto|socket|httpsys; using auto");
                return "auto";
            }
            if (mode != "auto")
            {
                BridgeLog.Info(TransportEnvVar + "=" + mode + " — transport pinned by the operator");
            }
            return mode;
        }
    }
}
