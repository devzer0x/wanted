// Maps Supabase's URL shape onto a bare PostgREST, so the product's own supabase-js client can be
// pointed at a throwaway database without a single line of product code knowing.
//
// supabase-js calls `${SUPABASE_URL}/rest/v1/<table>` and `${SUPABASE_URL}/rest/v1/rpc/<fn>`; a
// bare PostgREST serves the same API at `/`. This strips the prefix and forwards everything else —
// method, headers (apikey + Authorization carry a real HS256 JWT PostgREST verifies), body —
// verbatim, and streams the real response back. It never fabricates a response.
import { createServer, request } from "node:http";

const [, , listenPort, upstreamPort] = process.argv;
const PREFIX = "/rest/v1";

createServer((req, res) => {
  if (!req.url.startsWith(PREFIX)) {
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ message: `shim serves ${PREFIX} only, got ${req.url}` }));
    return;
  }
  const headers = { ...req.headers, host: `127.0.0.1:${upstreamPort}` };
  const up = request(
    { host: "127.0.0.1", port: Number(upstreamPort), method: req.method, path: req.url.slice(PREFIX.length) || "/", headers },
    (upRes) => {
      res.writeHead(upRes.statusCode ?? 502, upRes.headers);
      upRes.pipe(res);
    },
  );
  up.on("error", (err) => {
    res.writeHead(502, { "content-type": "application/json" });
    res.end(JSON.stringify({ message: `shim upstream error: ${String(err)}` }));
  });
  req.pipe(up);
}).listen(Number(listenPort), "127.0.0.1", () => console.log(`shim :${listenPort} -> postgrest :${upstreamPort}`));
