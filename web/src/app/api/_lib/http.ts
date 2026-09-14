// Small response/request helpers shared by the API routes. No business logic lives here.

export function jsonOk<T>(body: T, status = 200): Response {
  return Response.json(body, { status, headers: { "cache-control": "no-store" } });
}

export function jsonError(
  status: number,
  error: string,
  extra?: Record<string, unknown>
): Response {
  return Response.json({ error, ...extra }, { status, headers: { "cache-control": "no-store" } });
}

/** 503, not a fake 200 — used whenever a required server-side dependency is absent. */
export function notConfigured(what: string): Response {
  return jsonError(503, `${what} is not configured`);
}

/**
 * Best-effort caller identity for rate limiting only — NOT used for anything security-sensitive
 * beyond throttling (wallet identity always comes from the verified session cookie, never from a
 * header). Vercel sets `x-forwarded-for` on every request reaching a Function.
 */
export function getClientIp(request: Request): string {
  const fwd = request.headers.get("x-forwarded-for");
  if (fwd) {
    const first = fwd.split(",")[0]?.trim();
    if (first) return first;
  }
  const real = request.headers.get("x-real-ip");
  if (real) return real.trim();
  return "unknown";
}

/**
 * The requester's country as an ISO 3166-1 alpha-2 code, from `x-vercel-ip-country`, which Vercel's
 * edge sets from the client's public IP (https://vercel.com/docs/headers/request-headers). Null
 * when absent or not two letters — policy treats null as "region_unconfirmed", never as allowed.
 *
 * Only meaningful behind Vercel. Anywhere else a client can send this header itself, which is why
 * nothing but reward ELIGIBILITY reads it, and why eligibility treats a missing value as a refusal.
 */
export function getClientCountry(request: Request): string | null {
  const raw = request.headers.get("x-vercel-ip-country")?.trim().toUpperCase();
  return raw && /^[A-Z]{2}$/.test(raw) ? raw : null;
}

/**
 * A 500 that says nothing about why. CONTRACTS-PREDICTIONS §10.1 principle 7 covers every route, not
 * only the payout ones: Postgres and PostgREST messages carry table, column and constraint names and
 * occasionally row values, and none of that belongs in a response body. The SQLSTATE (or "unknown")
 * goes to the server log under `where`; the caller gets the fixed `message` it chose.
 */
export function internalError(where: string, error: unknown, message: string): Response {
  const code = (error as { code?: unknown } | null)?.code;
  console.error(`${where}: ${typeof code === "string" && /^[0-9A-Z]{5}$/.test(code) ? code : "unknown"}`);
  return jsonError(500, message);
}

export async function readJsonBody<T>(request: Request): Promise<T | null> {
  try {
    return (await request.json()) as T;
  } catch {
    return null;
  }
}
