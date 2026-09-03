"use client";

import { useEffect, useState } from "react";

import { formatBornAge, formatUtcStamp } from "@/lib/format";

/**
 * Replaces the four session counters. The operator's verdict on those was
 * "statistics are not accurate", and a number nobody trusts is worse than no number.
 * This is one fact the harness wrote exactly once: when the first session started.
 * It re-renders every minute so the hours tick over without a reload.
 */
export function Born({ bornAt }: { bornAt: string | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(id);
  }, []);

  return (
    <section aria-label="Born" className="panel flex flex-col items-center gap-1 px-3 py-4">
      {bornAt ? (
        <>
          <span className="font-display text-xl leading-none tabular-nums text-bone sm:text-2xl md:text-3xl">
            {formatBornAge(bornAt, now)}
          </span>
          <span className="ticker text-[0.55rem] text-smoke sm:text-[0.62rem]">
            since the agent was born · {formatUtcStamp(bornAt)}
          </span>
        </>
      ) : (
        <span className="ticker text-[0.55rem] text-smoke sm:text-[0.62rem]">not born yet</span>
      )}
    </section>
  );
}
