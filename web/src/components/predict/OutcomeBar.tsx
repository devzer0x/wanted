"use client";

/**
 * One outcome, rendered as a tote-board row: label on the left, participation share filling in
 * from the left edge, percentage on the right. Doubles as the vote button while a prediction is
 * open (`onClick` present) and as a static result row once it isn't.
 */
export function OutcomeBar({
  label,
  pct,
  active,
  won,
  onClick,
  busy,
}: {
  label: string;
  /** Participation share 0–100, or null when there is no distribution to show yet. */
  pct: number | null;
  /** This is the viewer's own pick. */
  active?: boolean;
  /** Settled only: true = this outcome won, false = it lost, undefined = not settled. */
  won?: boolean;
  onClick?: () => void;
  busy?: boolean;
}) {
  const clickable = Boolean(onClick) && !busy;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!clickable}
      aria-pressed={active}
      className={`relative flex w-full items-center justify-between overflow-hidden border px-3 py-2 text-left transition-colors ${
        won === true ? "border-bone" : active ? "border-ember" : "border-ash"
      } ${clickable ? "cursor-pointer hover:border-blood" : "cursor-default"}`}
    >
      <span
        className="absolute inset-y-0 left-0 bg-blood/15"
        style={{ width: `${pct ?? 0}%` }}
        aria-hidden="true"
      />
      <span className="relative z-10 flex min-w-0 items-center gap-2 font-mono text-sm text-bone">
        {active && (
          <span className="text-ember" aria-hidden="true">
            ▸
          </span>
        )}
        <span className="truncate">{label}</span>
        {won === true && <span className="ticker shrink-0 text-[0.55rem] text-bone">won</span>}
        {won === false && <span className="ticker shrink-0 text-[0.55rem] text-smoke">lost</span>}
      </span>
      <span className="relative z-10 shrink-0 font-mono text-xs text-smoke">
        {pct === null ? "—" : `${pct}%`}
      </span>
    </button>
  );
}
