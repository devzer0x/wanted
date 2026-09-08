// Reward math. Per docs/CONTRACTS-PREDICTIONS.md §7: integer math only, floor division, dust
// stays with the treasury. Per §6, caps come from env, are enforced inside the settlement SQL
// transaction against sum() (not here — this module has no database access), and this is just
// the shared source of truth for what those caps are and how a pool splits.
//
//   REWARDS_DAILY_CAP            base units; unset = no cap
//   REWARDS_MAX_PER_PREDICTION   base units; unset = no cap
//   REWARDS_MAX_PER_WALLET_DAY   base units; unset = no cap
//   CLAIM_MIN_AMOUNT             base units; unset = 0 (no minimum enforced yet)
//   CLAIM_MAX_AMOUNT             base units; unset = no cap (no manual-release ceiling)

export function computeRewardPerWallet(p: {
  pool: bigint;
  correctCount: number;
  strategy?: string;
}): bigint {
  const zero = BigInt(0);
  if (p.correctCount <= 0) return zero;
  if (p.pool <= zero) return zero;

  const strategy = p.strategy ?? "even_split";
  switch (strategy) {
    case "even_split":
      return p.pool / BigInt(p.correctCount);
    default:
      // Never guess a distribution scheme for money — an unrecognised strategy must fail loudly,
      // the same posture settlement takes toward an unrecognised telemetry rule kind (§3).
      throw new Error(`computeRewardPerWallet: unknown reward strategy "${strategy}"`);
  }
}

function parseOptionalBigintEnv(name: string): bigint | null {
  const raw = process.env[name]?.trim();
  if (!raw) return null;
  try {
    const value = BigInt(raw);
    return value >= BigInt(0) ? value : null;
  } catch {
    return null;
  }
}

/**
 * The CLAIM-side limits (CONTRACTS-PREDICTIONS §6). Only these two live in the environment, because
 * only these two are enforced by TypeScript — `/api/rewards/claim` passes them into
 * `create_reward_claim`.
 *
 * The three SETTLEMENT-side caps — daily, per-prediction and per-wallet-per-day — are deliberately
 * NOT here. They are enforced inside `settle_due_predictions()`, which is a Postgres function and
 * cannot read this deployment's environment; it reads them from `public.site_config` under the key
 * `reward_caps`. They used to be parsed here as well, from `REWARDS_DAILY_CAP` and friends, and
 * nothing ever consumed the result — so an operator who set a daily treasury cap in Vercel would
 * have been told nothing and capped nothing. For a treasury rail that is the worst possible failure
 * mode, so the dead half is gone and there is now exactly one place to set them:
 *
 *   insert into public.site_config (key, value) values
 *     ('reward_caps', '{"daily_cap": 250, "max_per_prediction": 25, "max_per_wallet_day": 10}')
 *   on conflict (key) do update set value = excluded.value;
 *
 * Those numbers are WHOLE token units, matching the amount columns. The two below are BASE units,
 * matching the rest of the TypeScript; `/api/rewards/claim` converts them at the SQL boundary.
 */
export const REWARD_LIMITS: {
  minClaim: bigint;
  maxClaim: bigint | null;
} = {
  minClaim: parseOptionalBigintEnv("CLAIM_MIN_AMOUNT") ?? BigInt(0),
  maxClaim: parseOptionalBigintEnv("CLAIM_MAX_AMOUNT"),
};
