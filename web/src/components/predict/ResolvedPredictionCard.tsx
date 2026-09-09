"use client";

import { pctOf } from "@/components/predict/distribution";
import { formatAge, formatBaseUnits, hasBaseUnits, formatUtcStamp } from "@/lib/format";
import type { MyEntry, Prediction, PredictionDistribution } from "@/lib/prediction/types";

/**
 * A settled (or voided) prediction. The two outcome chips are sized by their real share, so the
 * split reads at a glance; a share of zero still keeps a minimum width so its label and its "0%"
 * stay legible rather than collapsing to nothing.
 */
export function ResolvedPredictionCard({
  prediction,
  distribution,
  mine,
  nowMs,
  mounted,
}: {
  prediction: Prediction;
  distribution: PredictionDistribution | undefined;
  mine: MyEntry | undefined;
  nowMs: number;
  mounted: boolean;
}) {
  const isVoid = prediction.status === "void";

  return (
    <article
      className="panel flex flex-col gap-2.5 p-3.5"
      data-testid="resolved-prediction"
      data-status={prediction.status}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className="pill pill-sm panel-title"
          style={{
            background: isVoid ? "var(--yellow)" : "var(--teal)",
            transform: "rotate(-2deg)",
          }}
        >
          {isVoid ? "Void" : "Settled"}
        </span>
        {prediction.settled_at && (
          <time
            dateTime={prediction.settled_at}
            title={formatUtcStamp(prediction.settled_at)}
            suppressHydrationWarning
            className="text-[11px] font-extrabold text-muted"
          >
            {mounted
              ? formatAge(prediction.settled_at, nowMs)
              : formatUtcStamp(prediction.settled_at)}
          </time>
        )}
      </div>

      <p className="text-[15px] font-black leading-tight text-ink [overflow-wrap:anywhere]">
        {prediction.question}
      </p>

      {isVoid ? (
        <p className="text-xs font-bold leading-relaxed text-muted">
          Voided — the window didn&apos;t produce a clean signal, so nothing settled.
        </p>
      ) : (
        <div className="flex gap-1.5">
          {prediction.outcomes.map((outcome) => {
            const pct = pctOf(distribution, outcome.key);
            const won = outcome.key === prediction.result;
            return (
              <div
                key={outcome.key}
                className="flex min-w-[5.5rem] items-center justify-between gap-1.5 rounded-[10px] border-2 border-ink px-2.5 py-1.5 text-xs font-black text-ink"
                style={{
                  flex: `${pct ?? 50} 1 0%`,
                  // Teal is "this is what happened", whichever outcome it was — the per-outcome
                  // identity colours belong to the live card, where they label a choice.
                  background: won ? "var(--teal)" : "var(--sand)",
                  opacity: won ? 1 : 0.65,
                }}
              >
                <span className="truncate">{outcome.label}</span>
                <span className="flex-none">{pct === null ? "—" : `${pct}%`}</span>
                {won && <span className="sr-only">— this is what happened</span>}
              </div>
            );
          })}
        </div>
      )}

      {mine && !isVoid && (
        <p className="text-xs font-bold leading-relaxed text-muted">
          You picked{" "}
          {prediction.outcomes.find((o) => o.key === mine.outcome)?.label ?? mine.outcome}
          {" — "}
          {mine.correct
            ? `correct${hasBaseUnits(mine.reward) ? `, ${formatBaseUnits(mine.reward)} ${prediction.reward_asset} credited` : ""}.`
            : "not this time."}
        </p>
      )}
    </article>
  );
}
