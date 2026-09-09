"use client";

import Link from "next/link";
import { ResolvedPredictionCard } from "@/components/predict/ResolvedPredictionCard";
import type { MyEntry, Prediction, PredictionDistribution } from "@/lib/prediction/types";

/** Compact teaser for the home page's sidebar — the full history lives on /predict. */
export function RecentResults({
  predictions,
  distributions,
  mine,
  nowMs,
  mounted,
  limit = 2,
}: {
  predictions: Prediction[];
  distributions: Record<string, PredictionDistribution>;
  mine: Record<string, MyEntry>;
  nowMs: number;
  mounted: boolean;
  limit?: number;
}) {
  const shown = predictions.slice(0, limit);
  if (shown.length === 0) return null;

  return (
    <section className="flex flex-col gap-2.5" aria-label="Recent results">
      <div className="flex items-center justify-between gap-2">
        <h2 className="panel-title text-lg">Recent results</h2>
        <Link
          href="/predict"
          className="pill pill-sm panel-title transition-transform hover:-translate-y-px"
          style={{ background: "var(--yellow-pale)" }}
        >
          All results →
        </Link>
      </div>
      <div className="flex flex-col gap-2.5">
        {shown.map((prediction) => (
          <ResolvedPredictionCard
            key={prediction.id}
            prediction={prediction}
            distribution={distributions[prediction.id]}
            mine={mine[prediction.id]}
            nowMs={nowMs}
            mounted={mounted}
          />
        ))}
      </div>
    </section>
  );
}
