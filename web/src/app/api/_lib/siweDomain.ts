// The domain and URI that SIWE messages are bound to.
//
// SECURITY — why this is not `request.headers.get("host")`.
//
// EIP-4361's whole purpose is that the signed message names the site the user believes they are
// signing in to, so a signature produced for one origin cannot be spent at another. That binding
// only holds if the verifier decides the domain from something the caller cannot influence.
//
// An earlier version reconstructed the message from the request's `Host` header and then compared
// the parsed domain against that same header. The comparison always passed, because both sides
// came from the caller. That reopens the exact attack the domain field exists to prevent:
//
//   1. attacker requests a nonce from us for the victim's address (the nonce route is public and
//      takes an address, so this costs nothing),
//   2. attacker runs evil.example, and has the victim sign a SIWE message showing
//      `evil.example` and carrying that nonce,
//   3. attacker POSTs the signature to our /api/auth/verify with `Host: evil.example`,
//   4. we rebuild a message with domain `evil.example`, it matches, the signature is valid — and
//      the attacker holds a session cookie for the victim's address.
//
// Pinning the domain to the deployment's own canonical origin breaks step 3: a signature made for
// any other origin no longer reconstructs to a message we will verify.

import { getSiteUrl } from "@/lib/env";

export interface SiweOrigin {
  /** RFC 3986 authority (host[:port]) — the SIWE `domain` field. */
  domain: string;
  /** Full origin — the SIWE `uri` field. */
  uri: string;
}

/**
 * The one origin this deployment will accept signatures for. Derived from server configuration
 * (`NEXT_PUBLIC_SITE_URL`, else Vercel's `VERCEL_PROJECT_PRODUCTION_URL`, else localhost for
 * development) — never from the incoming request.
 *
 * Throws rather than falling back to localhost outside development. `getSiteUrl()` ends at
 * `http://localhost:3000` when neither variable is set, and that default is harmless for metadata
 * but actively dangerous here: the domain in this message is what the user reads in their wallet
 * before approving. A deployment that quietly signed `localhost:3000` while the user was on the
 * real site would show them a mismatched domain — which is the exact signal the pinned domain
 * exists to make trustworthy — and every signature would then be bound to an origin that is not
 * ours. Refusing to sign is the safe failure; the route surfaces it as "not configured".
 */
export function siweOrigin(): SiweOrigin {
  const url = new URL(getSiteUrl());
  const isLoopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (isLoopback && process.env.NODE_ENV === "production" && !process.env.ALLOW_LOOPBACK_SIWE) {
    throw new Error(
      "SIWE origin resolved to a loopback address in a production build. Set NEXT_PUBLIC_SITE_URL " +
        "(or deploy where VERCEL_PROJECT_PRODUCTION_URL is set) so the message names the real site.",
    );
  }
  return { domain: url.host, uri: url.origin };
}
