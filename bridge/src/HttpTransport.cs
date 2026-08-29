using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace WastedBridge
{
    /// <summary>A way of getting HTTP bytes to and from <see cref="BridgeRouter"/>.</summary>
    internal interface IBridgeTransport
    {
        /// <summary>Human-readable description for the log and the startup diagnostics block.</summary>
        string Description { get; }

        /// <summary>Binds and starts accepting. Throws on failure; the caller decides what next.</summary>
        void Start();

        /// <summary>Idempotent, never throws.</summary>
        void Stop();
    }

    /// <summary>
    /// Preferred transport: <see cref="HttpListener"/>, which on Windows is HTTP.SYS. Battle-tested
    /// and the reason the API speaks real HTTP — but HTTP.SYS requires the URL namespace to be
    /// reserved for non-administrator accounts, which is why <see cref="SocketTransport"/> exists.
    /// </summary>
    internal sealed class HttpSysTransport : IBridgeTransport
    {
        private const int MaxBodyBytes = 64 * 1024;

        private readonly string _prefix;
        private readonly BridgeRouter _router;
        private volatile HttpListener _listener;
        private Thread _acceptThread;
        private volatile bool _running;

        public HttpSysTransport(string prefix, BridgeRouter router)
        {
            _prefix = prefix;
            _router = router;
        }

        public string Description
        {
            get { return _prefix + " via HttpListener (HTTP.SYS on Windows)"; }
        }

        public void Start()
        {
            var listener = new HttpListener();
            listener.Prefixes.Add(_prefix);
            try
            {
                listener.Start(); // throws HttpListenerException on bind/ACL failure
            }
            catch (Exception)
            {
                // Retries run every 10 s forever; a half-open listener per attempt would leak.
                try { ((IDisposable)listener).Dispose(); } catch (Exception) { }
                throw;
            }
            _listener = listener;
            _running = true;
            _acceptThread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "WastedBridge.Http"
            };
            _acceptThread.Start();
        }

        public void Stop()
        {
            _running = false;
            HttpListener listener = _listener;
            _listener = null;
            if (listener == null)
            {
                return;
            }
            try
            {
                // Abort() (not Stop()) tears down queued requests immediately, which is what a
                // script reload needs: the port must be free before the new instance binds.
                listener.Abort();
            }
            catch (Exception)
            {
            }
            try
            {
                ((IDisposable)listener).Dispose();
            }
            catch (Exception)
            {
            }
            JoinAcceptThread(_acceptThread);
            _acceptThread = null;
        }

        internal static void JoinAcceptThread(Thread thread)
        {
            if (thread == null)
            {
                return;
            }
            try
            {
                thread.Join(1000);
            }
            catch (Exception)
            {
            }
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                HttpListenerContext ctx;
                HttpListener listener = _listener;
                if (listener == null)
                {
                    return;
                }
                try
                {
                    ctx = listener.GetContext();
                }
                catch (HttpListenerException)
                {
                    if (_running)
                    {
                        continue;
                    }
                    return;
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (InvalidOperationException)
                {
                    return;
                }
                ThreadPool.QueueUserWorkItem(HandleSafe, ctx);
            }
        }

        private void HandleSafe(object state)
        {
            var ctx = (HttpListenerContext)state;
            try
            {
                BridgeResponse response = Handle(ctx);
                Write(ctx, response);
            }
            catch (Exception ex)
            {
                BridgeLog.Error("unhandled HTTP error for " + SafeUrl(ctx), ex);
                try
                {
                    Write(ctx, BridgeResponse.Error(500, "internal_error", ex.Message));
                }
                catch (Exception)
                {
                    // Response already gone (client hung up); nothing sane to do.
                }
            }
        }

        private BridgeResponse Handle(HttpListenerContext ctx)
        {
            string body;
            if (!TryReadBody(ctx, out body))
            {
                return BridgeResponse.Error(413, "body_too_large",
                    "request bodies are limited to " + MaxBodyBytes + " bytes");
            }
            return _router.Route(ctx.Request.HttpMethod, ctx.Request.RawUrl, body);
        }

        private static bool TryReadBody(HttpListenerContext ctx, out string body)
        {
            body = "";
            if (!ctx.Request.HasEntityBody)
            {
                return true;
            }
            if (ctx.Request.ContentLength64 > MaxBodyBytes)
            {
                return false;
            }
            var buffer = new MemoryStream();
            var chunk = new byte[8192];
            Stream input = ctx.Request.InputStream;
            int read;
            while ((read = input.Read(chunk, 0, chunk.Length)) > 0)
            {
                if (buffer.Length + read > MaxBodyBytes)
                {
                    return false;
                }
                buffer.Write(chunk, 0, read);
            }
            body = Encoding.UTF8.GetString(buffer.ToArray());
            return true;
        }

        private static string SafeUrl(HttpListenerContext ctx)
        {
            try
            {
                return ctx.Request.RawUrl;
            }
            catch (Exception)
            {
                return "<unreadable request>";
            }
        }

        private static void Write(HttpListenerContext ctx, BridgeResponse response)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(response.Body);
            ctx.Response.StatusCode = response.Status;
            ctx.Response.ContentType = "application/json; charset=utf-8";
            ctx.Response.ContentLength64 = bytes.Length;
            ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
            ctx.Response.Close();
        }
    }

    /// <summary>
    /// Fallback transport: a plain loopback TCP socket speaking the small slice of HTTP/1.1 the
    /// Bridge API needs. It exists because HTTP.SYS refuses <c>http://127.0.0.1:port/</c> for
    /// non-administrator accounts unless the URL namespace has been reserved with
    /// <c>netsh http add urlacl</c> (Microsoft: only the <c>localhost</c> host name is exempt), and
    /// on delivery day nobody may be around to run an elevated netsh. A user-mode socket bound to
    /// 127.0.0.1 needs no privilege and no reservation, and it answers every Host header.
    /// Responses always close the connection: no keep-alive state to get wrong on loopback.
    /// </summary>
    internal sealed class SocketTransport : IBridgeTransport
    {
        private const int MaxHeaderBytes = 16 * 1024;
        private const int MaxBodyBytes = 64 * 1024;
        private const int SocketTimeoutMs = 15000;

        private readonly IPAddress _address;
        private readonly int _port;
        private readonly BridgeRouter _router;
        private volatile TcpListener _listener;
        private Thread _acceptThread;
        private volatile bool _running;

        public SocketTransport(IPAddress address, int port, BridgeRouter router)
        {
            _address = address;
            _port = port;
            _router = router;
        }

        public string Description
        {
            get
            {
                return "http://" + _address + ":" + _port + "/ via a loopback socket "
                       + "(no HTTP.SYS, no URL ACL required)";
            }
        }

        public void Start()
        {
            var listener = new TcpListener(_address, _port);
            try
            {
                // Fail fast when something else owns the port rather than silently sharing it.
                listener.ExclusiveAddressUse = true;
            }
            catch (Exception)
            {
                // Not supported on every platform; the bind below still reports a real conflict.
            }
            try
            {
                listener.Start(16);
            }
            catch (Exception)
            {
                try { listener.Stop(); } catch (Exception) { }
                throw;
            }
            _listener = listener;
            _running = true;
            _acceptThread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "WastedBridge.Socket"
            };
            _acceptThread.Start();
        }

        public void Stop()
        {
            _running = false;
            TcpListener listener = _listener;
            _listener = null;
            if (listener == null)
            {
                return;
            }
            try
            {
                listener.Stop();
            }
            catch (Exception)
            {
            }
            HttpSysTransport.JoinAcceptThread(_acceptThread);
            _acceptThread = null;
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                TcpListener listener = _listener;
                if (listener == null)
                {
                    return;
                }
                TcpClient client;
                try
                {
                    client = listener.AcceptTcpClient();
                }
                catch (SocketException)
                {
                    if (_running)
                    {
                        continue;
                    }
                    return;
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (InvalidOperationException)
                {
                    return;
                }
                ThreadPool.QueueUserWorkItem(HandleSafe, client);
            }
        }

        private void HandleSafe(object state)
        {
            var client = (TcpClient)state;
            try
            {
                client.NoDelay = true;
                client.ReceiveTimeout = SocketTimeoutMs;
                client.SendTimeout = SocketTimeoutMs;
                using (NetworkStream stream = client.GetStream())
                {
                    Serve(stream);
                }
            }
            catch (IOException)
            {
                // Client hung up mid-exchange; normal for polling clients.
            }
            catch (SocketException)
            {
            }
            catch (ObjectDisposedException)
            {
            }
            catch (Exception ex)
            {
                BridgeLog.Error("socket transport failed while serving a request", ex);
            }
            finally
            {
                try
                {
                    client.Close();
                }
                catch (Exception)
                {
                }
            }
        }

        private void Serve(NetworkStream stream)
        {
            byte[] headerBytes;
            byte[] leftover;
            if (!ReadHeaderBlock(stream, out headerBytes, out leftover))
            {
                WriteResponse(stream, BridgeResponse.Error(431, "headers_too_large",
                    "request headers exceed " + MaxHeaderBytes + " bytes"));
                return;
            }
            if (headerBytes == null)
            {
                return; // client closed before sending anything
            }

            string headerText = Encoding.ASCII.GetString(headerBytes);
            string[] lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.None);
            string method, target;
            if (!TryParseRequestLine(lines.Length > 0 ? lines[0] : "", out method, out target))
            {
                WriteResponse(stream, BridgeResponse.Error(400, "bad_request",
                    "malformed HTTP request line"));
                return;
            }

            Dictionary<string, string> headers = ParseHeaders(lines);
            string transferEncoding;
            if (headers.TryGetValue("transfer-encoding", out transferEncoding)
                && transferEncoding.IndexOf("chunked", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                WriteResponse(stream, BridgeResponse.Error(411, "length_required",
                    "chunked request bodies are not supported; send Content-Length"));
                return;
            }

            int contentLength = 0;
            string rawLength;
            if (headers.TryGetValue("content-length", out rawLength)
                && !int.TryParse(rawLength.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture,
                                 out contentLength))
            {
                WriteResponse(stream, BridgeResponse.Error(400, "bad_request",
                    "malformed Content-Length"));
                return;
            }
            if (contentLength < 0 || contentLength > MaxBodyBytes)
            {
                WriteResponse(stream, BridgeResponse.Error(413, "body_too_large",
                    "request bodies are limited to " + MaxBodyBytes + " bytes"));
                return;
            }

            string expect;
            if (headers.TryGetValue("expect", out expect)
                && expect.IndexOf("100-continue", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                byte[] cont = Encoding.ASCII.GetBytes("HTTP/1.1 100 Continue\r\n\r\n");
                stream.Write(cont, 0, cont.Length);
                stream.Flush();
            }

            string body = "";
            if (contentLength > 0)
            {
                byte[] bodyBytes;
                if (!ReadBody(stream, leftover, contentLength, out bodyBytes))
                {
                    return; // connection died mid-body; nothing to reply to
                }
                body = Encoding.UTF8.GetString(bodyBytes);
            }

            WriteResponse(stream, _router.Route(method, target, body));
        }

        /// <summary>
        /// Reads until the CRLFCRLF header terminator. Returns false when the cap is exceeded;
        /// sets <paramref name="header"/> to null when the peer closed before sending a request.
        /// </summary>
        private static bool ReadHeaderBlock(NetworkStream stream, out byte[] header, out byte[] leftover)
        {
            header = null;
            leftover = new byte[0];
            var buffer = new MemoryStream();
            var chunk = new byte[1024];
            while (true)
            {
                int read = stream.Read(chunk, 0, chunk.Length);
                if (read <= 0)
                {
                    if (buffer.Length == 0)
                    {
                        return true; // clean close, no request
                    }
                    return true; // truncated request; header stays null
                }
                buffer.Write(chunk, 0, read);
                byte[] all = buffer.ToArray();
                int end = IndexOfHeaderEnd(all);
                if (end >= 0)
                {
                    header = new byte[end];
                    Array.Copy(all, 0, header, 0, end);
                    int bodyStart = end + 4;
                    leftover = new byte[all.Length - bodyStart];
                    Array.Copy(all, bodyStart, leftover, 0, leftover.Length);
                    return true;
                }
                if (all.Length > MaxHeaderBytes)
                {
                    return false;
                }
            }
        }

        private static int IndexOfHeaderEnd(byte[] data)
        {
            for (int i = 0; i + 3 < data.Length; i++)
            {
                if (data[i] == 13 && data[i + 1] == 10 && data[i + 2] == 13 && data[i + 3] == 10)
                {
                    return i;
                }
            }
            return -1;
        }

        private static bool ReadBody(NetworkStream stream, byte[] leftover, int contentLength,
                                     out byte[] body)
        {
            body = new byte[contentLength];
            int have = Math.Min(leftover.Length, contentLength);
            Array.Copy(leftover, 0, body, 0, have);
            while (have < contentLength)
            {
                int read = stream.Read(body, have, contentLength - have);
                if (read <= 0)
                {
                    return false;
                }
                have += read;
            }
            return true;
        }

        private static bool TryParseRequestLine(string line, out string method, out string target)
        {
            method = null;
            target = null;
            if (string.IsNullOrEmpty(line))
            {
                return false;
            }
            string[] parts = line.Split(' ');
            if (parts.Length < 3)
            {
                return false;
            }
            method = parts[0];
            target = parts[1];
            return method.Length > 0 && target.Length > 0;
        }

        private static Dictionary<string, string> ParseHeaders(string[] lines)
        {
            var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 1; i < lines.Length; i++)
            {
                string line = lines[i];
                if (line.Length == 0)
                {
                    continue;
                }
                int colon = line.IndexOf(':');
                if (colon <= 0)
                {
                    continue;
                }
                headers[line.Substring(0, colon).Trim()] = line.Substring(colon + 1).Trim();
            }
            return headers;
        }

        private static void WriteResponse(NetworkStream stream, BridgeResponse response)
        {
            byte[] payload = Encoding.UTF8.GetBytes(response.Body);
            var head = new StringBuilder();
            head.Append("HTTP/1.1 ").Append(response.Status).Append(' ')
                .Append(ReasonPhrase(response.Status)).Append("\r\n");
            head.Append("Content-Type: application/json; charset=utf-8\r\n");
            head.Append("Content-Length: ").Append(payload.Length).Append("\r\n");
            head.Append("Connection: close\r\n\r\n");
            byte[] headBytes = Encoding.ASCII.GetBytes(head.ToString());
            stream.Write(headBytes, 0, headBytes.Length);
            stream.Write(payload, 0, payload.Length);
            stream.Flush();
        }

        private static string ReasonPhrase(int status)
        {
            switch (status)
            {
                case 200: return "OK";
                case 202: return "Accepted";
                case 400: return "Bad Request";
                case 404: return "Not Found";
                case 405: return "Method Not Allowed";
                case 409: return "Conflict";
                case 411: return "Length Required";
                case 413: return "Payload Too Large";
                case 431: return "Request Header Fields Too Large";
                case 500: return "Internal Server Error";
                case 503: return "Service Unavailable";
                default: return "Status";
            }
        }
    }
}
