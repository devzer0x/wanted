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
//   POLICY_REQUIRE_TERMS      when true, a wallet must have confirmed terms acceptance; this
//                             function has no terms-acceptance signal in its input contract, so
//                             while this flag is on it can never confirm acceptance and reports
//                             ineligible — the safe default — until a real acceptance signal is
//                             wired into the caller's context.

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
    reasons.push("terms_not_confirmed");
  }

  return { eligible: reasons.length === 0, reasons };
}
