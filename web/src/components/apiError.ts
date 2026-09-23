/**
 * Turns a failed API response into a real, specific message instead of a bare status code,
 * using the `{error, ...}` shape every route in `src/app/api/**` returns on failure
 * (`app/api/_lib/http.ts`'s `jsonError`). Falls back to the status code only when the body isn't
 * that shape — this never invents a reason the server didn't give.
 */
export async function apiErrorMessage(res: Response, fallback: string): Promise<string> {
  if (res.status === 404) return `${fallback} (not available on this deployment yet).`;
  try {
    const body = (await res.json()) as { error?: string };
    if (typeof body?.error === "string" && body.error) return body.error;
  } catch {
    // Body wasn't JSON — fall through to the generic message.
  }
  return `${fallback} (${res.status}).`;
}

/**
 * What a 409 from `POST /api/predictions/{id}/enter` means to the viewer. The route answers 409 for
 * two different things (CONTRACTS-PREDICTIONS §4): the round locked before the pick landed, or this
 * wallet already has a pick on it (a double tap, a second tab). The second is not an error to show
 * — the refresh that follows renders "You picked …" — so it returns null; anything else is the lock.
 */
export async function enterConflictMessage(res: Response, lockedMessage: string): Promise<string | null> {
  const reason = await apiErrorMessage(res, "");
  return /already entered/i.test(reason) ? null : lockedMessage;
}
