"use client";

import { useEffect, useState, type ReactNode } from "react";
import { LivePredictionCard } from "@/components/predict/LivePredictionCard";
import { ResolvedPredictionCard } from "@/components/predict/ResolvedPredictionCard";
import { usePredictions } from "@/components/predict/usePredictions";
import { viewerRecord } from "@/components/predict/summary";
import { formatBaseUnits, hasBaseUnits } from "@/lib/format";

function Header({ header, pills }: { header?: ReactNode; pills?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
      {header}
      {pills}
    </header>
  );
}

export function PredictView({ header }: { header?: ReactNode }) {
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
      <div className="flex flex-col gap-5">
        <Header header={header} />
        <div className="skeleton-pulse h-64" aria-hidden="true" />
        <div className="grid gap-3.5 sm:grid-cols-2">
          <div className="skeleton-pulse h-40" aria-hidden="true" />
          <div className="skeleton-pulse h-40" aria-hidden="true" />
        </div>
        <p className="sr-only">Loading predictions.</p>
      </div>
    );
  }

  if (fetched.state === "error") {
    return (
      <div className="flex flex-col gap-5">
        <Header header={header} />
        <div className="panel px-5 py-12 text-center" data-testid="predict-error">
          <span
            className="panel-title inline-block rounded-xl border-[3px] border-ink px-4 py-2 text-xl leading-none text-white"
            style={{
              background: "var(--coral)",
              boxShadow: "0 4px 0 var(--ink)",
              transform: "rotate(-2deg)",
            }}
          >
            Data link down
          </span>
          <p className="mt-3 text-[13px] font-bold text-muted">{fetched.message}</p>
        </div>
      </div>
    );
  }

  const { live, resolved, distributions, mine, session_live } = fetched.data;
  const resolvedSorted = [...resolved].sort((a, b) => {
    const ta = a.settled_at ? new Date(a.settled_at).getTime() : 0;
    const tb = b.settled_at ? new Date(b.settled_at).getTime() : 0;
    return tb - ta;
  });

  const record = viewerRecord(resolvedSorted, mine);
  const pills = record ? (
    <div className="flex flex-wrap gap-2">
      <span
        className="pill"
        style={{ boxShadow: "0 3px 0 var(--ink)", padding: "6px 14px", fontSize: "13px" }}
        title="Across the settled predictions shown on this page."
      >
        Recent: {record.correct} for {record.total}
      </span>
      {hasBaseUnits(record.earned) && record.asset && (
        <span
          className="pill"
          style={{
            background: "var(--teal)",
            boxShadow: "0 3px 0 var(--ink)",
            padding: "6px 14px",
            fontSize: "13px",
          }}
          title="Credited by the results shown on this page."
        >
          +{formatBaseUnits(record.earned)} {record.asset}
        </span>
      )}
    </div>
  ) : null;

  return (
    <div className="flex flex-col gap-5">
      <Header header={header} pills={pills} />

      {!session_live && (
        <p
          className="rounded-2xl border-[3px] border-ink px-4 py-3 text-[13px] font-bold leading-relaxed text-ink"
          style={{ background: "var(--yellow-pale)" }}
        >
          The agent is off the air right now, so no new prediction window is running. What&apos;s
          below is whatever was already open or has already settled.
        </p>
      )}

      <section className="flex flex-col gap-3.5" aria-label="Live predictions">
        {live.length === 0 ? (
          <div className="panel px-5 py-12 text-center" data-testid="predict-live-empty">
            <span
              className="panel-title inline-block rounded-xl border-[3px] border-ink px-4 py-2 text-xl leading-none"
              style={{
                background: "var(--sand)",
                boxShadow: "0 4px 0 var(--ink)",
                transform: "rotate(-2deg)",
              }}
            >
              Nothing open
            </span>
            <p className="mx-auto mt-3 max-w-sm text-[13px] font-bold leading-relaxed text-muted">
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
              layout="page"
            />
          ))
        )}
      </section>

      {resolvedSorted.length > 0 && (
        <section className="flex flex-col gap-3.5" aria-label="Recent results">
          <h2 className="panel-title text-xl">Settled</h2>
          <div className="grid gap-3.5 sm:grid-cols-2">
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
