// POST /api/auth/nonce — CONTRACTS-PREDICTIONS.md §4.
// {address} -> {nonce, expiresAt}; stores an unverified `wallet_sessions` row.
//
// SCHEMA ASSUMPTION (flagged in the task report): CONTRACTS-PREDICTIONS.md §2 describes
// `wallet_sessions` only in prose ("auth: address, nonce, chain_id, issued/expires/verified,
// hashed UA+IP") — unlike `predictions`, it gives no column table. This route writes
// `address, nonce, chain_id, issued_at, expires_at, verified, ua_ip_hash`, the most literal
// reading of that prose. The table does not exist yet in the real Supabase project (verified
// 2026-09-08: `GET /rest/v1/wallet_sessions` -> PGRST205 "Could not find the table"), so these
// names are unverified against a real migration and must be reconciled once one lands.
//
// The nonce itself is generated with viem's `generateSiweNonce` (viem/siwe, verified present in
// the installed viem@2.56.3 — an EIP-4361-shaped nonce), not hand-rolled.

import { generateSiweNonce } from "viem/siwe";
import { createHash } from "node:crypto";
import { publicClient } from "@/lib/chain/config";
import { buildSiweMessage } from "@/lib/auth/siwe";
import { siweOrigin } from "../../_lib/siweDomain";
import { createSupabaseAdmin, isSupabaseAdminConfigured } from "../../_lib/supabaseAdmin";
import { jsonError, jsonOk, notConfigured, getClientIp, readJsonBody } from "../../_lib/http";
import { rateLimit, RATE_LIMITS } from "../../_lib/rateLimit";
import { normalizeAddress } from "../../_lib/wallet";

export const dynamic = "force-dynamic";

const NONCE_TTL_MS = 5 * 60 * 1000; // 5 minutes to sign, then the nonce is dead

interface NonceBody {
  address?: unknown;
}

// One-way hash only — this exists for abuse forensics, never to identify a person, and is never
// returned to any client. `wallet_sessions` stores the user-agent and the IP as SEPARATE hashes
// (ua_hash / ip_hash) so that either signal can be correlated on its own: a sybil farm typically
// varies one while holding the other constant, which a single combined hash would conceal.
function hashValue(value: string | null): string | null {
  if (!value) return null;
  return createHash("sha256").update(value).digest("hex");
}

export async function POST(request: Request): Promise<Response> {
  const ip = getClientIp(request);
  const limited = rateLimit(`nonce:${ip}`, RATE_LIMITS.nonce);
  if (!limited.allowed) {
    return jsonError(429, "too many requests", { retryAfterMs: limited.retryAfterMs });
  }

  if (!isSupabaseAdminConfigured()) return notConfigured("database");
  const admin = createSupabaseAdmin();
  if (!admin) return notConfigured("database");

  const body = await readJsonBody<NonceBody>(request);
  if (!body) return jsonError(400, "invalid JSON body");
  const address = normalizeAddress(body.address);
  if (!address) return jsonError(400, "address must be a valid 0x-prefixed EVM address");

  const nonce = generateSiweNonce();
  const issuedAt = new Date();
  const expiresAt = new Date(issuedAt.getTime() + NONCE_TTL_MS);

  let chainId: number | null = null;
  try {
    chainId = publicClient().chain?.id ?? null;
  } catch {
    // `@/lib/chain/config` may not be reachable/configured yet in this environment; chain_id is
    // recorded best-effort and re-validated at /verify time regardless.
    chainId = null;
  }

  const { error } = await admin.from("wallet_sessions").insert({
    address,
    nonce,
    chain_id: chainId,
    issued_at: issuedAt.toISOString(),
    expires_at: expiresAt.toISOString(),
    verified_at: null,
    ua_hash: hashValue(request.headers.get("user-agent")),
    ip_hash: hashValue(ip),
  });

  if (error) {
    return jsonError(500, "failed to issue nonce", { detail: error.message });
  }

  // Return the exact message to sign, built here.
  //
  // The client MUST NOT assemble this string itself. /verify rebuilds the message from the stored
  // nonce and this deployment's pinned origin and verifies the signature against that text, so any
  // divergence — a trailing newline, a different `chainId`, `window.location.host` differing from
  // the configured origin on a preview deployment or a www/non-www mismatch — produces a signature
  // that cannot verify, and the user just sees "signature verification failed" with nothing to
  // debug. Handing back the literal string removes that whole class of drift, and costs nothing:
  // the message is not a secret, and it is still re-derived server-side at /verify rather than
  // trusted from the client.
  // siweOrigin() throws rather than sign a loopback domain in production (see _lib/siweDomain.ts).
  // Surface that as an honest not-configured response instead of an opaque 500.
  let domain, uri;
  try {
    ({ domain, uri } = siweOrigin());
  } catch (err) {
    return jsonError(503, "sign-in is not configured on this deployment", {
      detail: err instanceof Error ? err.message : String(err),
    });
  }
  const message = buildSiweMessage({
    address,
    nonce,
    domain,
    uri,
    chainId: chainId ?? 0,
    issuedAt: issuedAt.toISOString(),
    expirationTime: expiresAt.toISOString(),
  });

  return jsonOk(
    {
      nonce,
      message,
      expiresAt: expiresAt.toISOString(),
      issuedAt: issuedAt.toISOString(),
    },
    201,
  );
}
