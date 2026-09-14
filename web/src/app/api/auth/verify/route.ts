// POST /api/auth/verify — CONTRACTS-PREDICTIONS.md §4.
// {address, signature} -> sets HttpOnly session cookie; verifies EIP-4361.
//
// `@/lib/auth/siwe`'s real `verifySiwe` takes the full signed `message` text plus
// `expectedNonce`/`expectedDomain`/`expectedAddress`, and validates the message's own embedded
// fields (parsed back out of the string) against those — it does not take a client-sent
// `expiresAt`/timestamps separately, and it does not accept a `wallet_sessions` row directly. The
// server therefore reconstructs the exact message the client must have signed using
// `buildSiweMessage` with the same inputs the client used (nonce + issuedAt from /nonce's
// response, expirationTime = /nonce's `expiresAt`, chainId from the stored session row, and
// domain/uri derived from this request) and hands that string to `verifySiwe` — the server never
// trusts a client-supplied message, only a client-supplied signature over a message it rebuilt
// itself from server-held state.

import { buildSiweMessage, verifySiwe, RULES_VERSION } from "@/lib/auth/siwe";
import { publicClient } from "@/lib/chain/config";
import { createSession } from "@/lib/auth/session";
import { siweOrigin } from "../../_lib/siweDomain";
import { assessEligibility } from "@/lib/policy";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured, getClientCountry, getClientIp, readJsonBody, internalError } from "../../_lib/http";
import { rateLimit, RATE_LIMITS } from "../../_lib/rateLimit";
import { normalizeAddress } from "../../_lib/wallet";

export const dynamic = "force-dynamic";

interface VerifyBody {
  address?: unknown;
  signature?: unknown;
}

interface WalletSessionRow {
  id: string;
  address: string;
  nonce: string;
  chain_id: number | null;
  issued_at: string;
  expires_at: string;
  verified_at: string | null;
}

export async function POST(request: Request): Promise<Response> {
  const ip = getClientIp(request);
  const limited = rateLimit(`verify:${ip}`, RATE_LIMITS.verify);
  if (!limited.allowed) {
    return jsonError(429, "too many requests", { retryAfterMs: limited.retryAfterMs });
  }

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  const body = await readJsonBody<VerifyBody>(request);
  if (!body) return jsonError(400, "invalid JSON body");
  const address = normalizeAddress(body.address);
  const signature = typeof body.signature === "string" ? body.signature : null;
  if (!address || !signature) {
    return jsonError(400, "address and signature are required");
  }

  // Single-use, expiring nonce: only the most recent unverified, unexpired row for this address
  // is eligible. A second /verify for the same nonce cannot succeed once `verified_at` is stamped.
  const { data: session, error: lookupError } = await admin
    .from("wallet_sessions")
    .select("id, address, nonce, chain_id, issued_at, expires_at, verified_at")
    .eq("address", address)
    .is("verified_at", null)
    .gt("expires_at", new Date().toISOString())
    .order("issued_at", { ascending: false })
    .limit(1)
    .maybeSingle<WalletSessionRow>();

  if (lookupError) {
    return internalError("auth/verify: failed to look up session nonce", lookupError, "failed to look up session nonce");
  }
  if (!session) {
    return jsonError(401, "no live nonce for this address — request a new one from /api/auth/nonce");
  }

  // The domain is pinned to this deployment's configured origin, NOT taken from the request.
  // See _lib/siweDomain.ts — deriving it from the Host header makes the domain check compare
  // attacker-supplied input against itself, which defeats EIP-4361's binding entirely.
  // siweOrigin() throws rather than sign a loopback domain in production (see _lib/siweDomain.ts).
  // Surface that as an honest not-configured response instead of an opaque 500.
  let domain, uri;
  try {
    ({ domain, uri } = siweOrigin());
  } catch (err) {
    console.error(`auth/verify: ${err instanceof Error ? err.message : String(err)}`);
    return jsonError(503, "sign-in is not configured on this deployment");
  }
  const chainId = session.chain_id ?? publicClient().chain?.id ?? 0;

  const message = buildSiweMessage({
    address,
    nonce: session.nonce,
    domain,
    uri,
    chainId,
    issuedAt: session.issued_at,
    expirationTime: session.expires_at,
  });

  const result = await verifySiwe({
    message,
    signature: signature as `0x${string}`,
    expectedNonce: session.nonce,
    expectedDomain: domain,
    expectedAddress: address,
  });

  if (!result.ok) {
    return jsonError(401, "signature verification failed", { reason: result.error });
  }

  // Burn the nonce before doing anything else — a crash after this point cannot be replayed.
  // The same UPDATE records which rules version the signed statement named: the message verified
  // above was rebuilt with RULES_VERSION, so this signature is proof of accepting exactly that.
  const { data: burned, error: burnError } = await admin
    .from("wallet_sessions")
    .update({ verified_at: new Date().toISOString(), rules_version: RULES_VERSION })
    .eq("id", session.id)
    .is("verified_at", null)
    .select("id");
  if (burnError) {
    return internalError("auth/verify: failed to finalize session", burnError, "failed to finalize session");
  }
  // Zero rows updated is NOT an error from PostgREST — it means another concurrent request burned
  // this nonce first. Without checking, both racers would be issued a session off one nonce, and
  // the "single-use" property would be a comment rather than a fact.
  if ((burned ?? []).length !== 1) {
    return jsonError(409, "nonce was already used — request a new one");
  }

  await createSession(address, RULES_VERSION);

  const policy = await assessEligibility({
    address,
    country: getClientCountry(request),
    rulesVersion: RULES_VERSION,
  });
  return jsonOk({ address, eligible: policy.eligible });
}
