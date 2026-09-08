// Address validation/normalization shared by the auth routes. Uses `viem` directly (an installed
// dependency, verified present at web/node_modules/viem@2.56.3) rather than reimplementing EIP-55
// checksum logic by hand.
//
// STORAGE IS LOWER-CASE, DISPLAY IS CHECKSUMMED, and the split matters.
//
// Every wallet column in the database stores the lower-cased address, enforced by a
// `check (wallet = lower(wallet))` constraint. That is what makes
// `UNIQUE (prediction_id, wallet)` mean one entry per ADDRESS: if the EIP-55 checksummed form were
// stored, two spellings of the same address would be two different rows and the constraint would
// not bind at all.
//
// Storing the checksummed form also broke claiming outright — `create_reward_claim` looks up
// `lower(wallet)`, found nothing against checksummed rows, and reported "nothing to claim" for
// every wallet, so credited rewards were permanently unreachable.
//
// `getAddress` is still used first, because it VALIDATES the checksum: an address written with
// mixed case whose checksum is wrong is a typo, and it should be rejected here rather than
// lower-cased into something that looks fine.

import { isAddress, getAddress } from "viem";

/** Validates (including EIP-55 checksum, when present) and returns the canonical STORAGE form. */
export function normalizeAddress(raw: unknown): string | null {
  if (typeof raw !== "string" || !isAddress(raw)) return null;
  try {
    return getAddress(raw).toLowerCase();
  } catch {
    return null;
  }
}

/** The checksummed form, for showing to a human. Never use this as a database key. */
export function displayAddress(raw: string): string {
  try {
    return getAddress(raw);
  } catch {
    return raw;
  }
}
