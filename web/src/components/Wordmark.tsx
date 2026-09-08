// Original WANTED wordmark: heavy grotesque, skewed, red misprint ghost (styling in
// globals.css). Deliberately unlike the game's own logo, art or death-screen typography.
export function Wordmark({ className = "text-3xl" }: { className?: string }) {
  return (
    <span className={`wordmark ${className}`} data-text="WANTED">
      WANTED
    </span>
  );
}
