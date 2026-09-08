// Converting between the database's amount representation and on-chain base units.
//
// THE UNIT CONVENTION, stated once so the two layers cannot drift apart again:
//
//   * The DATABASE stores WHOLE TOKEN UNITS in `numeric(38,18)`. A 2.5 TTWO reward is the row
//     value `2.500000000000000000`. This is what `settle_due_predictions()` writes — it does its
//     division in integer base-unit math and then divides back down by 10^18 before the INSERT —
//     and it is what an operator reads and writes when setting the §6 caps in `site_config`, where
//     "a daily cap of 25" sensibly means 25 tokens and not 25 attotokens.
//
//   * CODE works in BASE UNITS as `bigint`, because that is what an ERC-20 `transfer()` takes and
//     because base units are exact where a decimal fraction in JavaScript is not.
//
// So the boundary between them is a DECIMAL SHIFT by the token's `decimals`, not a reinterpretation
// of the same digits. An earlier version of this file read the numeric column as if it already
// held base units; that is a 10^18 error in the paying-out direction, and it threw outright on any
// reward with a fractional part. The shift below is the fix, and the reason `decimals` is a
// required argument rather than an assumed 18: the reward asset is configurable (CONTRACTS
// -PREDICTIONS §1), so its scale is a property of the asset, never a constant in this file.
//
// Everything uses BigInt; `Number` cannot hold 1e18 without loss, and a reward that rounds is a
// reward that lies.

/**
 * Parse a Postgres `numeric` amount (whole token units, e.g. "2.500000000000000000") into exact
 * base units. Throws on anything it cannot represent exactly rather than silently truncating.
 */
export function toBaseUnits(numericStr: string, decimals: number): bigint {
  const trimmed = String(numericStr).trim();
  if (!/^-?\d+(\.\d+)?$/.test(trimmed)) {
    throw new Error(`amount "${numericStr}" is not a plain decimal number`);
  }
  if (!Number.isInteger(decimals) || decimals < 0 || decimals > 77) {
    throw new Error(`invalid decimals: ${decimals}`);
  }

  const negative = trimmed.startsWith("-");
  const unsigned = negative ? trimmed.slice(1) : trimmed;
  const [whole, fraction = ""] = unsigned.split(".");

  // Significant digits beyond the token's scale cannot be paid out — refuse rather than round,
  // because rounding here would be inventing (or destroying) a fraction of someone's reward.
  if (fraction.length > decimals && /[1-9]/.test(fraction.slice(decimals))) {
    throw new Error(
      `amount "${numericStr}" has more precision than the asset's ${decimals} decimals`,
    );
  }

  const padded = (fraction + "0".repeat(decimals)).slice(0, decimals);
  const magnitude = BigInt(whole + padded);
  return negative ? -magnitude : magnitude;
}

/** Sum a set of Postgres `numeric` amounts into exact base units. */
export function sumBaseUnits(values: Array<string | number>, decimals: number): bigint {
  // BigInt(0), not `0n` — tsconfig targets ES2017, which has no BigInt literal syntax.
  let total = BigInt(0);
  for (const v of values) total += toBaseUnits(String(v), decimals);
  return total;
}

/**
 * Render base units back into the decimal string the database column expects. Used when passing a
 * §6 limit down into SQL, where the caps are expressed in whole units.
 */
export function fromBaseUnits(value: bigint, decimals: number): string {
  const negative = value < BigInt(0);
  const digits = (negative ? -value : value).toString().padStart(decimals + 1, "0");
  const whole = digits.slice(0, digits.length - decimals);
  const fraction = decimals === 0 ? "" : `.${digits.slice(digits.length - decimals)}`;
  return `${negative ? "-" : ""}${whole}${fraction}`;
}
