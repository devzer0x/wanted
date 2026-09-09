/**
 * Presentation-only helpers shared by the prediction cards.
 *
 * Both are derived from an outcome's POSITION in `Prediction.outcomes` — the real order the API
 * returned — so nothing here invents a value. They exist because the arcade design gives each
 * outcome its own fill colour and a letter chip, and two components need the same mapping.
 */

// Sticker palette, in the order the design uses it: the first outcome reads positive (teal), the
// second reads the hard one (coral). Beyond two, keep cycling rather than repeat a colour early.
const OUTCOME_FILLS = [
  "var(--teal)",
  "var(--coral)",
  "var(--yellow)",
  "var(--blue)",
  "var(--purple)",
];

export function outcomeFill(index: number): string {
  return OUTCOME_FILLS[index % OUTCOME_FILLS.length];
}

/** A, B, C… — the chip on an outcome row. Positional, never part of the payload. */
export function outcomeBadge(index: number): string {
  return String.fromCharCode(65 + (index % 26));
}
