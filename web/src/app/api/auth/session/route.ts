// GET /api/auth/session — CONTRACTS-PREDICTIONS.md §4. -> {address | null, eligible}
//
// Reads the wallet address from the HttpOnly session cookie only (`readSession`), never from a
// query string or body — this is the one place the rest of the app is meant to ask "who is this".

import { readSession } from "@/lib/auth/session";
import { assessEligibility } from "@/lib/policy";
import { jsonOk } from "../../_lib/http";

export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  const session = await readSession();
  if (!session?.address) {
    return jsonOk({ address: null, eligible: false });
  }
  const policy = await assessEligibility({ address: session.address });
  return jsonOk({ address: session.address, eligible: policy.eligible });
}
