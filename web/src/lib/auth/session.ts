// Server-only wallet session cookie. HMAC-SHA256 signed so the client cannot forge or edit the
// address inside it; the payload (address + expiry) is not secret, so signing (integrity) is
// what matters here, not encryption (confidentiality) — nobody can produce a valid signature
// without SESSION_SECRET, which never leaves the server.
//
//   SESSION_SECRET         required; server refuses to create or read sessions without it —
//                           there is deliberately no insecure fallback.
//   SESSION_TTL_SECONDS    optional, defaults to 7 days.

import "server-only";

import { cookies } from "next/headers";
import { createHmac, timingSafeEqual } from "node:crypto";

const COOKIE_NAME = "wasted_wallet_session";
const DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 7;

function getSessionSecret(): string {
  const secret = process.env.SESSION_SECRET;
  if (!secret || secret.trim().length === 0) {
    throw new Error(
      "SESSION_SECRET is not set — refusing to create or read a wallet session cookie without " +
        "it. Set SESSION_SECRET in the server environment; there is no insecure default.",
    );
  }
  return secret;
}

function getTtlSeconds(): number {
  const raw = process.env.SESSION_TTL_SECONDS?.trim();
  if (!raw) return DEFAULT_TTL_SECONDS;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : DEFAULT_TTL_SECONDS;
}

interface SessionPayload {
  address: string;
  expiresAt: number;
  /**
   * The /rules version the wallet accepted by signing (see RULES_VERSION in lib/auth/siwe.ts).
   * Optional because cookies minted before rules acceptance existed carry none — those read as
   * "not accepted", never as accepted. Inside the HMAC, so a client cannot raise it.
   */
  rulesVersion?: number;
}

function sign(body: string, secret: string): string {
  return createHmac("sha256", secret).update(body).digest("base64url");
}

function encode(payload: SessionPayload, secret: string): string {
  const body = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  return `${body}.${sign(body, secret)}`;
}

function decode(token: string, secret: string): SessionPayload | null {
  const dot = token.indexOf(".");
  if (dot < 0) return null;
  const body = token.slice(0, dot);
  const signature = token.slice(dot + 1);
  const expected = sign(body, secret);

  const a = Buffer.from(signature, "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length || !timingSafeEqual(a, b)) return null;

  try {
    const parsed: unknown = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      typeof (parsed as SessionPayload).address === "string" &&
      typeof (parsed as SessionPayload).expiresAt === "number" &&
      ((parsed as SessionPayload).rulesVersion === undefined ||
        Number.isInteger((parsed as SessionPayload).rulesVersion))
    ) {
      return parsed as SessionPayload;
    }
    return null;
  } catch {
    return null;
  }
}

export async function createSession(address: string, rulesVersion: number): Promise<void> {
  const secret = getSessionSecret();
  const ttlSeconds = getTtlSeconds();
  const token = encode({ address, expiresAt: Date.now() + ttlSeconds * 1000, rulesVersion }, secret);

  const store = await cookies();
  store.set(COOKIE_NAME, token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: ttlSeconds,
  });
}

export async function readSession(): Promise<{ address: string; rulesVersion: number | null } | null> {
  const secret = getSessionSecret();
  const store = await cookies();
  const token = store.get(COOKIE_NAME)?.value;
  if (!token) return null;

  const payload = decode(token, secret);
  if (!payload) return null;
  if (payload.expiresAt <= Date.now()) return null;

  return { address: payload.address, rulesVersion: payload.rulesVersion ?? null };
}

export async function destroySession(): Promise<void> {
  const store = await cookies();
  store.delete(COOKIE_NAME);
}
