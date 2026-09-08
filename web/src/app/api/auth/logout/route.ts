// POST /api/auth/logout — CONTRACTS-PREDICTIONS.md §4. Clears the session cookie.

import { destroySession } from "@/lib/auth/session";
import { jsonOk } from "../../_lib/http";

export const dynamic = "force-dynamic";

export async function POST(): Promise<Response> {
  await destroySession();
  return jsonOk({ ok: true });
}
