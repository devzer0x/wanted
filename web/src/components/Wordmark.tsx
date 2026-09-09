// The WANTED wordmark: heavy display type in a coral sticker with a hard, unblurred offset shadow
// (`.wordmark` in globals.css). Deliberately unlike the game's own logo, art or death-screen
// typography — CLAUDE.md rule 7 keeps our identity clear of it.
//
// `tone="light"` is the footer variant: same coral sticker, but outlined in white and flat, because
// on the ink footer an ink border and an ink shadow both disappear. It has to be an inline style —
// the rules in globals.css are unlayered, so they outrank every Tailwind utility no matter what the
// specificity is.
export function Wordmark({
  className = "text-3xl",
  tone = "ink",
}: {
  className?: string;
  tone?: "ink" | "light";
}) {
  return (
    <span
      className={`wordmark ${className}`}
      style={tone === "light" ? { borderColor: "#fff", boxShadow: "none" } : undefined}
    >
      WANTED
    </span>
  );
}
