"use client";

import { OutcomeBar } from "@/components/predict/OutcomeBar";
import { pctOf } from "@/components/predict/distribution";
import { formatAge, formatBaseUnits, hasBaseUnits, formatUtcStamp } from "@/lib/format";
import type { MyEntry, Prediction, PredictionDistribution } from "@/lib/prediction/types";

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
      className="panel flex flex-col gap-2 p-3"
      data-testid="resolved-prediction"
      data-status={prediction.status}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className={`ticker border px-1.5 py-0.5 text-[0.55rem] ${
            isVoid ? "border-hazard text-hazard" : "border-bone text-bone"
          }`}
        >
          {isVoid ? "void" : "settled"}
        </span>
        {prediction.settled_at && (
          <time
            dateTime={prediction.settled_at}
            title={formatUtcStamp(prediction.settled_at)}
            className="font-mono text-[0.58rem] text-smoke"
          >
            {mounted ? formatAge(prediction.settled_at, nowMs) : formatUtcStamp(prediction.settled_at)}
          </time>
        )}
      </div>

      <p className="font-mono text-sm leading-snug text-bone [overflow-wrap:anywhere]">
        {prediction.question}
      </p>

      {isVoid ? (
        <p className="text-[0.62rem] leading-relaxed text-hazard">
          Voided — the window didn&apos;t produce a clean signal, so nothing settled.
        </p>
      ) : (
        <div className="flex flex-col gap-1.5">
          {prediction.outcomes.map((outcome) => (
            <OutcomeBar
              key={outcome.key}
              label={outcome.label}
              pct={pctOf(distribution, outcome.key)}
              active={mine?.outcome === outcome.key}
              won={outcome.key === prediction.result}
            />
          ))}
        </div>
      )}

      {mine && !isVoid && (
        <p className="text-[0.6rem] leading-relaxed text-smoke">
          Your pick: {prediction.outcomes.find((o) => o.key === mine.outcome)?.label ?? mine.outcome}
          {" — "}
          {mine.correct
            ? `correct${hasBaseUnits(mine.reward) ? `, ${formatBaseUnits(mine.reward)} ${prediction.reward_asset} credited` : ""}.`
            : "not this time."}
        </p>
      )}
    </article>
  );
}
