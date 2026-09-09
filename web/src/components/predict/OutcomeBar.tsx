"use client";

import { outcomeBadge, outcomeFill } from "@/components/predict/outcomeStyle";

/**
 * One outcome, rendered as a tote-board row: letter chip and label on the left, the participation
 * share filling in from the left edge, the percentage on the right. The fill width IS the share —
 * there is no decorative bar. Doubles as the vote button while a prediction is open (`onClick`
 * present) and as a static result row once it isn't.
 */
export function OutcomeBar({
  label,
  pct,
  index,
  active,
  won,
  onClick,
  busy,
}: {
  label: string;
  /** Participation share 0–100, or null when there is no distribution to show yet. */
  pct: number | null;
  /** Position in `Prediction.outcomes`; drives the chip letter and the fill colour. */
  index: number;
  /** This is the viewer's own pick. */
  active?: boolean;
  /** Settled only: true = this outcome won, false = it lost, undefined = not settled. */
  won?: boolean;
  onClick?: () => void;
  busy?: boolean;
}) {
  const clickable = Boolean(onClick) && !busy;
  const fill = outcomeFill(index);

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!clickable}
      aria-pressed={active}
      aria-busy={busy || undefined}
      className={[
        "predict-bar w-full min-h-[58px] text-left transition-transform duration-100",
        "shadow-[0_5px_0_var(--ink)]",
        clickable
          ? "cursor-pointer hover:-translate-y-px active:translate-y-1 active:shadow-[0_1px_0_var(--ink)]"
          : "cursor-default",
        won === false ? "opacity-50" : "",
        busy ? "opacity-70" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span
        className="fill transition-[width] duration-500 ease-out"
        style={{ width: `${pct ?? 0}%`, background: fill }}
        aria-hidden="true"
      />
      <span className="label flex min-w-0 items-center gap-2.5">
        <span
          className="flex h-[26px] w-[26px] flex-none items-center justify-center rounded-lg border-[3px] border-ink font-display text-[13px] leading-none text-ink"
          style={{ background: fill }}
          aria-hidden="true"
        >
          {outcomeBadge(index)}
        </span>
        <span className="truncate font-display text-base leading-tight text-ink sm:text-lg">
          {label}
        </span>
        {won === true ? (
          <span className="flex-none rounded-full bg-ink px-2 py-[3px] text-[10px] font-black uppercase tracking-[0.1em] text-white">
            Winner
          </span>
        ) : active ? (
          <span className="flex-none rounded-full bg-ink px-2 py-[3px] text-[10px] font-black uppercase tracking-[0.1em] text-white">
            Your pick
          </span>
        ) : null}
        {won === false && <span className="sr-only">did not happen</span>}
      </span>
      <span className="pct flex-none font-display text-xl text-ink">
        {pct === null ? "—" : `${pct}%`}
      </span>
    </button>
  );
}
