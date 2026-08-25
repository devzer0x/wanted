"use client";

import type { StatsRow } from "@/lib/types";

// Budget governor levels per CONTRACTS §7.
const GOVERNOR_LABELS: Record<number, string> = {
  0: "full brain",
  1: "thrifty",
  2: "reflexes only",
  3: "asleep in the car",
};

export function GoalCard({ stats }: { stats: StatsRow | null }) {
  const level = stats?.governor_level;
  return (
    <section className="panel p-3 sm:p-4" aria-label="Current goal">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="panel-title">Current goal</h2>
        {typeof level === "number" && (
          <span className="ticker border border-ash px-1.5 py-0.5 text-[0.58rem] text-smoke">
            L{level} · {GOVERNOR_LABELS[level] ?? "unknown"}
          </span>
        )}
      </div>
      <p className="font-mono text-sm leading-snug text-bone">
        {stats?.current_goal || "No goal on record."}
      </p>
    </section>
  );
}
