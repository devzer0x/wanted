// Original WANTED wordmark: heavy grotesque, skewed, red misprint ghost (styling in
// globals.css). Deliberately unlike the game's death-screen typography or any Rockstar mark.
export function Wordmark({ className = "text-3xl" }: { className?: string }) {
  return (
    <span className={`wordmark ${className}`} data-text="WASTED">
      WASTED
    </span>
  );
}
