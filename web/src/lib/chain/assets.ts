// The reward asset. Per docs/CONTRACTS-PREDICTIONS.md §1: "The reward asset is configurable.
// Nothing outside web/src/lib/chain/assets.ts and the `asset` columns may hardcode TTWO." This
// file is that one place.
//
// Two DIFFERENT tokens live in this product and must not be conflated:
//
//   TTWO    the REWARD asset. Already deployed and verified on chain 4663 at
//           0x5e81213613b6B86EaB4c6c50d718d34359459786 — symbol TTWO, 18 decimals, read live
//           from the contract. Correct predictions are credited in this, paid from a treasury
//           we fund. It is a Robinhood Stock Token: a tokenized debt security whose distribution
//           is restricted by jurisdiction. That restriction is enforced server-side, in
//           lib/policy — NOT by refusing to name the asset here.
//
//   $WANTED the show's own token (brief §19). NOT a wager, NOT the reward asset, and NOT yet
//           deployed — it has no address. Its utility surfaces (badges, votes, special rounds)
//           stay hidden while NEXT_PUBLIC_WANTED_TOKEN is empty.
//
// The reward asset stays env-resolved rather than hardcoded so that changing it — if the
// tokenized-security posture makes TTWO unusable — is a config change, not a rewrite.
//
//   NEXT_PUBLIC_TTWO_TOKEN       reward ERC-20 address; empty = unconfigured, surfaces hide
//   NEXT_PUBLIC_TTWO_SYMBOL      display symbol (default "TTWO")
//   NEXT_PUBLIC_TTWO_DECIMALS    decimals; unset = 18 (the verified contract). Present but not a
//                                plain integer 0..77 = the asset is UNCONFIGURED (rewardAsset()
//                                returns null), never a silent 18 (FM-16: a wrong scale is a
//                                10^n error in the paying-out direction).
//   NEXT_PUBLIC_WANTED_TOKEN     $WANTED, once it exists. Unrelated to rewards.

import { isAddress } from "viem";
import { publicClient } from "./config";

export interface RewardAsset {
  address: `0x${string}`;
  symbol: string;
  decimals: number;
}

const ERC20_ABI = [
  {
    type: "function",
    name: "balanceOf",
    stateMutability: "view",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ name: "", type: "uint256" }],
  },
  {
    type: "function",
    name: "paused",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "bool" }],
  },
] as const;

// STATIC member access, deliberately. Next.js inlines `process.env.NEXT_PUBLIC_X` into the client
// bundle by literal substitution at build time; a dynamic `process.env[name]` lookup is not
// substituted, so it resolves on the server and silently yields `undefined` in the browser. Nothing
// calls these from a client component today, but the failure mode if something did — a reward asset
// that exists server-side and vanishes client-side — is exactly the kind of bug that is invisible
// until a user reports a blank panel.
function parseAddress(raw: string | undefined): `0x${string}` | null {
  const trimmed = raw?.trim();
  if (!trimmed) return null;
  return isAddress(trimmed) ? (trimmed as `0x${string}`) : null;
}

/**
 * Strict: absent -> 18 (the verified TTWO value); present -> must be a plain decimal integer with no
 * sign, exponent, fraction or leading zero, in 0..77 (the range api/_lib/amount.ts can shift by).
 * Anything else -> null, which makes the whole asset unconfigured: a malformed scale is refused, not
 * guessed. `Number("1e1")`, `Number(" 18 ")` and `Number("0x12")` all parse — this does not use them.
 */
function parseDecimals(raw: string | undefined): number | null {
  if (raw === undefined) return 18;
  const trimmed = raw.trim();
  if (trimmed === "") return 18;
  if (!/^(0|[1-9][0-9]?)$/.test(trimmed)) return null;
  const n = Number(trimmed);
  return n <= 77 ? n : null;
}

/** Null when the reward asset is not yet configured — never invent an address to fill this in. */
export function rewardAsset(): RewardAsset | null {
  const address = parseAddress(process.env.NEXT_PUBLIC_TTWO_TOKEN);
  if (!address) return null;

  const symbol = process.env.NEXT_PUBLIC_TTWO_SYMBOL?.trim() || "TTWO";

  const decimals = parseDecimals(process.env.NEXT_PUBLIC_TTWO_DECIMALS);
  if (decimals === null) return null;

  return { address, symbol, decimals };
}

/** The show's own token. Separate from rewards; null until $WANTED is deployed and configured. */
export function wantedToken(): `0x${string}` | null {
  return parseAddress(process.env.NEXT_PUBLIC_WANTED_TOKEN);
}

/** $WANTED-gated surfaces (holder badges, votes, special rounds) hide behind this. */
export function isWantedTokenConfigured(): boolean {
  return wantedToken() !== null;
}

export function isRewardAssetConfigured(): boolean {
  return rewardAsset() !== null;
}

/** Raw on-chain balance of `owner` in `token`'s base units. Works for any ERC-20, not just the
 * configured reward asset — used to verify chain plumbing against real, already-deployed tokens. */
export async function readErc20Balance(
  token: `0x${string}`,
  owner: `0x${string}`,
): Promise<bigint> {
  return publicClient().readContract({
    address: token,
    abi: ERC20_ABI,
    functionName: "balanceOf",
    args: [owner],
  });
}

/** `paused()` per CONTRACTS-PREDICTIONS §1 — TTWO exposes it and it must return false to spend.
 * Propagates if the token has no such function; callers that gate spending on this must treat a
 * thrown error as "cannot confirm unpaused" and refuse, the same safe-default posture as §5. */
export async function isTokenPaused(token: `0x${string}`): Promise<boolean> {
  return publicClient().readContract({
    address: token,
    abi: ERC20_ABI,
    functionName: "paused",
  });
}

/** Integer-only formatting: floors to `places` fractional digits, never touches `Number`. */
export function formatUnitsFixed(v: bigint, decimals: number, places = 4): string {
  const zero = BigInt(0);
  const negative = v < zero;
  const abs = negative ? -v : v;
  const base = BigInt(10) ** BigInt(decimals);
  const whole = abs / base;
  const frac = abs % base;
  const fracDigits = Math.max(0, Math.min(places, decimals));
  const fracStr = frac.toString().padStart(decimals, "0").slice(0, fracDigits);
  const out = fracDigits > 0 ? `${whole.toString()}.${fracStr.padEnd(fracDigits, "0")}` : whole.toString();
  return negative && (whole > zero || frac > zero) ? `-${out}` : out;
}
