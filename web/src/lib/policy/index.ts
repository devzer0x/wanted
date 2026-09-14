// Reward eligibility. Per docs/CONTRACTS-PREDICTIONS.md §5: predicting is always allowed;
// rewarding is not. This module decides ONLY the latter, and only from config — never a
// hardcoded rule in UI. When a required signal is missing, the safe default is ineligible
// (§5: "If eligibility cannot be confirmed, no reward is issued").
//
//   REWARDS_ENABLED           master switch; unset/false = no rewards issued at all
//   POLICY_BLOCKED_REGIONS    comma-separated ISO 3166-1 alpha-2 country codes, e.g. "US,CA,GB,CH"
//                             (the jurisdictions the contract's §1 names as restricted for the
//                             underlying Stock Token — kept configurable rather than hardcoded
//                             because the reward asset itself is configurable)
//   POLICY_REQUIRE_TERMS      when true, a wallet must have accepted the CURRENT /rules version,
//                             which it does by signing in: the SIWE statement names the version
//                             (lib/auth/siwe.ts RULES_VERSION) and the signed session cookie
//                             carries it. Callers pass it as `rulesVersion`.
//
// COUNTRY comes from Vercel's `x-vercel-ip-country` (ISO 3166-1 alpha-2, set by Vercel's edge —
// https://vercel.com/docs/headers/request-headers), read by getClientCountry() in api/_lib/http.ts.
// No caller passed it before, which with POLICY_BLOCKED_REGIONS set made every wallet
// "region_unconfirmed" — nobody could ever have claimed. It is IP geolocation, so a VPN defeats
// it; /rules forbids that, and it is a residual risk the operator accepts knowingly.

import { RULES_VERSION } from "@/lib/auth/siwe";

export type PolicyReason = string;

function isEnabled(value: string | undefined): boolean {
  const v = value?.trim().toLowerCase();
  return v === "true" || v === "1" || v === "yes";
}

function blockedRegions(): Set<string> {
  const raw = process.env.POLICY_BLOCKED_REGIONS?.trim();
  if (!raw) return new Set();
  return new Set(
    raw
      .split(",")
      .map((code) => code.trim().toUpperCase())
      .filter((code) => code.length > 0),
  );
}

export async function assessEligibility(ctx: {
  address: string;
  ip?: string | null;
  country?: string | null;
  /** The /rules version from the signed session; null for a session minted before acceptance. */
  rulesVersion?: number | null;
}): Promise<{ eligible: boolean; reasons: PolicyReason[] }> {
  const reasons: PolicyReason[] = [];

  if (!isEnabled(process.env.REWARDS_ENABLED)) {
    reasons.push("rewards_disabled");
  }

  if (!ctx.address) {
    reasons.push("no_wallet");
  }

  const blocked = blockedRegions();
  if (blocked.size > 0) {
    const country = ctx.country?.trim().toUpperCase();
    if (!country) {
      reasons.push("region_unconfirmed");
    } else if (blocked.has(country)) {
      reasons.push("region_blocked");
    }
  }

  if (isEnabled(process.env.POLICY_REQUIRE_TERMS)) {
    const accepted = ctx.rulesVersion ?? null;
    if (accepted === null || accepted < RULES_VERSION) {
      reasons.push("terms_not_confirmed");
    }
  }

  return { eligible: reasons.length === 0, reasons };
}
