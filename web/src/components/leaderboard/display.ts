/** Presentation-only helpers shared by the leaderboard podium and table.
 *
 * Everything here is derived from a real `LeaderboardRow`. Nothing is invented: the avatar chip is
 * a deterministic function of the wallet address itself, so two surfaces showing the same wallet
 * always draw the same chip, and a wallet that has never predicted never gets one.
 */

/** The chip palette, in design-system order. Deliberately excludes cream/sand: the chip has to
 *  read against both the white table row and the cream #1 row. */
const CHIP_COLORS = [
  "var(--yellow)",
  "var(--blue)",
  "var(--teal)",
  "var(--purple)",
  "var(--coral)",
] as const;

/** `0x3fA2…9c1e` → `3F`. The first two address characters after the `0x`, which is what the
 *  design's chip shows — an identifier the viewer can check against the address beside it. */
export function walletInitials(address: string): string {
  const body = address.replace(/^0x/i, "").trim();
  if (body.length === 0) return "??";
  return body.slice(0, 2).toUpperCase();
}

/** A stable colour for one address. Case-folded first so a checksummed and a lowercase spelling of
 *  the same wallet cannot end up two different colours. */
export function walletChipColor(address: string): string {
  const key = address.toLowerCase();
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) % 100_003;
  }
  return CHIP_COLORS[hash % CHIP_COLORS.length];
}

// `accuracy` is a fraction in [0, 1] — `public.leaderboard()` returns `correct / total` rounded to
// 4 places (verified against the real function: 0.6667 for 2 correct of 3), and that is now stated
// on `LeaderboardRow`. The `<= 1` branch is kept anyway: it costs nothing, and it is correct at
// both ends (a perfect 1.0 renders 100%, and a hypothetical already-percentage value passes
// through) — so a future change of convention shows up as a wrong-looking number here rather than
// as a silent factor-of-100 error in front of viewers comparing their rank.
export function accuracyLabel(accuracy: number): string {
  if (!Number.isFinite(accuracy)) return "—";
  const pct = accuracy <= 1 ? accuracy * 100 : accuracy;
  // One decimal below 10% so 3.2% and 3.8% are distinguishable, but not for an exact 0 or 100 —
  // "0.0%" beside "100%" in the same column reads as a formatting bug rather than as precision.
  if (pct === 0 || pct === 100) return `${pct}%`;
  return `${pct.toFixed(pct < 10 ? 1 : 0)}%`;
}
