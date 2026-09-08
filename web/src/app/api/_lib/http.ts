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

export async function readJsonBody<T>(request: Request): Promise<T | null> {
  try {
    return (await request.json()) as T;
  } catch {
    return null;
  }
}
