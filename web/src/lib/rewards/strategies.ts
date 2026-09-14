// Reward math. Per docs/CONTRACTS-PREDICTIONS.md §7: integer math only, floor division, dust
// stays with the treasury. Per §6, caps come from env, are enforced inside the settlement SQL
// transaction against sum() (not here — this module has no database access), and this is just
// the shared source of truth for what those caps are and how a pool splits.
//
//   CLAIM_MIN_AMOUNT   base units, REQUIRED, > 0. Decided: 2000000000000000 (0.002 TTWO).
//   CLAIM_MAX_AMOUNT   base units, REQUIRED, >= CLAIM_MIN_AMOUNT. Decided: 500000000000000000 (0.5).
//
// Unset is a misconfiguration, not "no limit" (CONTRACTS-PREDICTIONS §10.6): rewardLimits() throws,
// the claim route refuses, and the balance route reports `reward_limits_misconfigured`.

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

/**
 * Parses a REQUIRED base-unit integer from the environment, or throws.
 *
 * Absent used to mean "no limit" for both claim rails, so a deployment that simply forgot them paid
 * claims with no floor and no ceiling. On the money path an absent rail is now a refusal (§10.6).
 * A value that is present but unreadable is also an error: an operator who writes
 * `CLAIM_MAX_AMOUNT=0.5` (the plausible mistake, because the caps in `site_config` ARE whole tokens)
 * must get a refusal, never a silently different ceiling.
 *
 * The thrown messages name the variable and quote its (non-secret) value for the operator reading
 * server logs. Routes never return them: they map the throw to a fixed code.
 */
function parseRequiredBigintEnv(name: "CLAIM_MIN_AMOUNT" | "CLAIM_MAX_AMOUNT"): bigint {
  const raw = process.env[name]?.trim();
  if (!raw) {
    throw new Error(`${name} is not set. It is required, in BASE UNITS (0.002 TTWO is "2000000000000000").`);
  }
  if (!/^\d+$/.test(raw)) {
    throw new Error(
      `${name}="${raw}" is not a non-negative integer. This variable is in BASE UNITS (an 18-decimal ` +
        `asset means 0.002 TTWO is "2000000000000000"), not whole tokens.`,
    );
  }
  return BigInt(raw);
}

/**
 * The CLAIM-side limits (CONTRACTS-PREDICTIONS §6, §10.6). Only these two live in the environment,
 * because only these two are enforced from TypeScript — `/api/rewards/claim` passes them into
 * `create_reward_claim`, which takes the longest prefix of the wallet's credits under the maximum
 * and refuses a total under the minimum.
 *
 * The three SETTLEMENT-side caps — daily, per-prediction and per-wallet-per-day — are deliberately
 * NOT here. They are enforced inside `settle_due_predictions()`, which is a Postgres function and
 * cannot read this deployment's environment; it reads them from `public.site_config` under the key
 * `reward_caps`, and with the row absent or malformed it credits nothing (rewards_enabled() is
 * false). The decided values, and the only example that belongs anywhere:
 *
 *   insert into public.site_config (key, value) values
 *     ('reward_caps', '{"daily_cap": 0.25, "max_per_prediction": 0.02, "max_per_wallet_day": 0.05}')
 *   on conflict (key) do update set value = excluded.value;
 *
 * Those numbers are WHOLE token units, matching the amount columns. The two below are BASE units,
 * matching the rest of the TypeScript; `/api/rewards/claim` converts them at the SQL boundary.
 * CLAIM_MAX_AMOUNT (0.5) must stay >= max_per_prediction (0.02), or a single credit could never be
 * claimed (create_reward_claim raises P0004).
 */
export type RewardLimits = { minClaim: bigint; maxClaim: bigint };

/**
 * Read per call rather than once at module load, because it throws. Next.js evaluates module scope
 * while collecting page data at build time, so a module-level parse would turn one missing variable
 * into a failed build for the whole site. Evaluating it inside the request keeps the failure loud
 * and confined to the routes that depend on it.
 */
export function rewardLimits(): RewardLimits {
  const minClaim = parseRequiredBigintEnv("CLAIM_MIN_AMOUNT");
  const maxClaim = parseRequiredBigintEnv("CLAIM_MAX_AMOUNT");
  if (minClaim <= BigInt(0)) {
    throw new Error("CLAIM_MIN_AMOUNT must be greater than 0 (decided: 2000000000000000 = 0.002 TTWO).");
  }
  if (maxClaim < minClaim) {
    throw new Error("CLAIM_MAX_AMOUNT must be at least CLAIM_MIN_AMOUNT.");
  }
  return { minClaim, maxClaim };
}
