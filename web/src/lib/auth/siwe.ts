// EIP-4361 (Sign-In with Ethereum) message construction and verification, hand-rolled per
// docs/CONTRACTS-PREDICTIONS.md §8's instruction to use viem's `verifyMessage` rather than add a
// SIWE dependency. Field layout follows the EIP-4361 ABNF exactly for the fields we use
// (domain, address, statement, uri, version, chain-id, nonce, issued-at, expiration-time);
// optional fields we don't take as parameters (not-before, request-id, resources) are omitted
// entirely, which the spec allows.

import { getAddress, verifyMessage } from "viem";

/**
 * The version of the public rules at /rules that signing in accepts.
 *
 * It lives here, not in a config module, because it is part of the SIGNED BYTES: the statement
 * below names it, so a verified signature is itself the proof of which rules a wallet accepted.
 * Bump it whenever /rules changes in substance. Every existing session then reads as "rules not
 * accepted" (CONTRACTS-PREDICTIONS §5, POLICY_REQUIRE_TERMS) until the wallet signs in again,
 * which is exactly the re-consent a change of terms needs. Predicting is never gated on it —
 * only rewards are.
 */
export const RULES_VERSION = 1;
export const RULES_PATH = "/rules";

// Shown VERBATIM inside the user's wallet signing prompt — this is a public surface, and for
// many users the first sentence of ours they read closely. Keep it plain about what signing
// does: it authenticates, it is free, it moves no funds, and it accepts a specific, versioned set
// of rules the user can open. Changing this string changes the bytes that get signed, so /verify
// must rebuild it identically — which it does, because both sides call buildSiweMessage() rather
// than assembling their own text. EIP-4361 forbids a newline inside the statement; this has none.
function statementFor(uri: string): string {
  const rulesUrl = `${uri.replace(/\/+$/, "")}${RULES_PATH}`;
  return (
    "Sign in to WANTED to make predictions. This is free and moves no funds. " +
    `By signing you accept the WANTED Rules v${RULES_VERSION} at ${rulesUrl}`
  );
}
const VERSION = "1";

/**
 * Canonical EIP-4361 timestamp: ISO-8601, milliseconds, `Z`.
 *
 * This normalisation is load-bearing and its absence was a real, total sign-in outage.
 *
 * `/nonce` built the message from `new Date().toISOString()` — `2026-09-08T18:43:13.595Z` — and
 * stored the same instant in a Postgres `timestamptz`. `/verify` then rebuilt the message from the
 * value it read back, which PostgREST serialises as `2026-09-08T18:43:13.595+00:00`. Same instant,
 * different STRING, so a different message, so a different signing hash: every signature recovered
 * a stranger's address and every sign-in failed with `invalid_signature`.
 *
 * Nothing about either half looked wrong in isolation, and no unit test caught it, because the bug
 * only exists in the round trip through the database. Normalising HERE — inside the one function
 * both halves call — is what makes it structurally impossible rather than fixed at one call site:
 * a caller may now pass an ISO string or a Postgres timestamp and get identical bytes either way.
 */
function canonicalTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    // Never silently pass a bad value through into signed bytes — a message nobody can reproduce
    // is worse than a loud failure, because it fails at the user's wallet with nothing to debug.
    throw new Error(`buildSiweMessage: unparseable timestamp ${JSON.stringify(value)}`);
  }
  return parsed.toISOString();
}

export function buildSiweMessage(p: {
  address: string;
  nonce: string;
  domain: string;
  uri: string;
  chainId: number;
  issuedAt: string;
  expirationTime: string;
}): string {
  // EIP-4361 requires the address in the message to be EIP-55 mixed-case checksummed. Storage is
  // lower-cased (see api/_lib/wallet.ts), so the conversion happens HERE, in the one function both
  // /nonce and /verify call — the same reason the timestamps are canonicalised here. Doing it at the
  // call sites instead is how the two halves drift apart and every signature stops verifying.
  const checksummed = getAddress(p.address as `0x${string}`);
  return (
    `${p.domain} wants you to sign in with your Ethereum account:\n` +
    `${checksummed}\n` +
    `\n` +
    `${statementFor(p.uri)}\n` +
    `\n` +
    `URI: ${p.uri}\n` +
    `Version: ${VERSION}\n` +
    `Chain ID: ${p.chainId}\n` +
    `Nonce: ${p.nonce}\n` +
    `Issued At: ${canonicalTimestamp(p.issuedAt)}\n` +
    `Expiration Time: ${canonicalTimestamp(p.expirationTime)}`
  );
}

interface ParsedSiwe {
  domain: string;
  address: string;
  uri: string;
  version: string;
  chainId: number;
  nonce: string;
  issuedAt: string;
  expirationTime: string | null;
}

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;

function matchField(message: string, label: string): string | null {
  const re = new RegExp(`^${label}: (.*)$`, "m");
  const match = re.exec(message);
  return match ? match[1].trim() : null;
}

function parseSiweMessage(message: string): ParsedSiwe | null {
  const lines = message.split("\n");
  if (lines.length < 2) return null;

  const domainMatch = /^(.*) wants you to sign in with your Ethereum account:$/.exec(lines[0]);
  if (!domainMatch) return null;
  const domain = domainMatch[1];

  const address = lines[1]?.trim();
  if (!address || !ADDRESS_RE.test(address)) return null;

  const uri = matchField(message, "URI");
  const version = matchField(message, "Version");
  const chainIdRaw = matchField(message, "Chain ID");
  const nonce = matchField(message, "Nonce");
  const issuedAt = matchField(message, "Issued At");
  const expirationTime = matchField(message, "Expiration Time");

  if (!uri || !version || !chainIdRaw || !nonce || !issuedAt) return null;
  const chainId = Number(chainIdRaw);
  if (!Number.isInteger(chainId) || chainId <= 0) return null;

  return { domain, address, uri, version, chainId, nonce, issuedAt, expirationTime };
}

export async function verifySiwe(p: {
  message: string;
  signature: `0x${string}`;
  expectedNonce: string;
  expectedDomain: string;
  expectedAddress: string;
}): Promise<{ ok: boolean; address?: string; error?: string }> {
  const parsed = parseSiweMessage(p.message);
  if (!parsed) return { ok: false, error: "malformed_message" };

  if (parsed.domain !== p.expectedDomain) {
    return { ok: false, error: "domain_mismatch" };
  }
  if (parsed.nonce !== p.expectedNonce) {
    return { ok: false, error: "nonce_mismatch" };
  }
  if (parsed.address.toLowerCase() !== p.expectedAddress.toLowerCase()) {
    return { ok: false, error: "address_mismatch" };
  }

  if (!parsed.expirationTime) {
    return { ok: false, error: "missing_expiration" };
  }
  const expiresAtMs = Date.parse(parsed.expirationTime);
  if (Number.isNaN(expiresAtMs)) {
    return { ok: false, error: "malformed_expiration" };
  }
  if (expiresAtMs <= Date.now()) {
    return { ok: false, error: "expired" };
  }

  const issuedAtMs = Date.parse(parsed.issuedAt);
  const CLOCK_SKEW_MS = 60_000;
  if (Number.isNaN(issuedAtMs) || issuedAtMs > Date.now() + CLOCK_SKEW_MS) {
    return { ok: false, error: "issued_in_future" };
  }

  let valid: boolean;
  try {
    valid = await verifyMessage({
      address: parsed.address as `0x${string}`,
      message: p.message,
      signature: p.signature,
    });
  } catch {
    return { ok: false, error: "signature_verification_failed" };
  }

  if (!valid) return { ok: false, error: "invalid_signature" };

  // Belt and braces: verifyMessage already checked this, but a message whose recovered signer
  // does not match the address it claims must never be treated as authenticating that address.
  if (parsed.address.toLowerCase() !== p.expectedAddress.toLowerCase()) {
    return { ok: false, error: "address_mismatch" };
  }

  return { ok: true, address: parsed.address };
}
