"use client";

import { useEffect, useState } from "react";
import { formatCountdown } from "@/lib/format";

export function PredictionCountdown({ target, label }: { target: string; label: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const targetMs = new Date(target).getTime();
  if (Number.isNaN(targetMs)) return null;
  const remaining = targetMs - now;
  const done = remaining <= 0;

  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span
        className={`font-mono text-sm tabular-nums ${
          !done && remaining < 10_000 ? "urgent" : "text-bone"
        }`}
      >
        {done ? "0:00" : formatCountdown(remaining)}
      </span>
      <span className="ticker text-[0.55rem] text-smoke">{label}</span>
    </span>
  );
}
