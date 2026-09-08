"use client";

import { useEffect, useState } from "react";
import { LivePredictionCard } from "@/components/predict/LivePredictionCard";
import { ResolvedPredictionCard } from "@/components/predict/ResolvedPredictionCard";
import { usePredictions } from "@/components/predict/usePredictions";

export function PredictView() {
  const { fetched, refresh } = usePredictions();
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    setNowMs(Date.now());
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  if (fetched.state === "loading") {
    return (
      <div className="flex flex-col gap-3">
        <div className="panel skeleton-pulse h-48" aria-hidden="true" />
        <div className="panel skeleton-pulse h-48" aria-hidden="true" />
      </div>
    );
  }

  if (fetched.state === "error") {
    return (
      <div className="panel px-4 py-12 text-center" data-testid="predict-error">
        <p className="wordmark text-2xl text-ember" data-text="DATA LINK DOWN">
          DATA LINK DOWN
        </p>
        <p className="mt-2 text-xs text-smoke">{fetched.message}</p>
      </div>
    );
  }

  const { live, resolved, distributions, mine, session_live } = fetched.data;
  const resolvedSorted = [...resolved].sort((a, b) => {
    const ta = a.settled_at ? new Date(a.settled_at).getTime() : 0;
    const tb = b.settled_at ? new Date(b.settled_at).getTime() : 0;
    return tb - ta;
  });

  return (
    <div className="flex flex-col gap-6">
      {!session_live && (
        <div className="panel border-blood px-4 py-3 text-xs leading-relaxed text-smoke">
          The agent is off the air right now, so no new prediction window is running. What&apos;s
          below is whatever was already open or has already settled.
        </div>
      )}

      <section className="flex flex-col gap-3" aria-label="Live predictions">
        {live.length === 0 ? (
          <div className="panel px-4 py-10 text-center" data-testid="predict-live-empty">
            <p className="wordmark text-xl" data-text="NOTHING OPEN">
              NOTHING OPEN
            </p>
            <p className="mt-2 text-xs text-smoke">
              No live prediction right now. The next one opens from something that happens on
              stream.
            </p>
          </div>
        ) : (
          live.map((prediction) => (
            <LivePredictionCard
              key={prediction.id}
              prediction={prediction}
              distribution={distributions[prediction.id]}
              mine={mine[prediction.id]}
              onEntered={refresh}
            />
          ))
        )}
      </section>

      {resolvedSorted.length > 0 && (
        <section className="flex flex-col gap-3" aria-label="Recent results">
          <h2 className="panel-title">Recent results</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            {resolvedSorted.map((prediction) => (
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
      )}
    </div>
  );
}
