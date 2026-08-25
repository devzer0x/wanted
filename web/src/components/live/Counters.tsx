"use client";

import { formatHours, formatInt } from "@/lib/format";
import type { StatsRow } from "@/lib/types";

function Counter({ label, value, loud }: { label: string; value: string; loud?: boolean }) {
  return (
    <div className="panel flex flex-col items-center gap-0.5 px-2 py-3">
      <span
        className={`font-display text-2xl sm:text-3xl leading-none ${loud ? "text-ember" : "text-bone"}`}
      >
        {value}
      </span>
      <span className="ticker text-[0.58rem] text-smoke">{label}</span>
    </div>
  );
}

export function Counters({ stats }: { stats: StatsRow | null }) {
  return (
    <section aria-label="Session counters" className="grid grid-cols-4 gap-2">
      <Counter label="Wasted" value={formatInt(stats?.deaths ?? null)} loud />
      <Counter label="Busted" value={formatInt(stats?.busted ?? null)} loud />
      <Counter label="Missions" value={formatInt(stats?.missions_passed ?? null)} />
      <Counter label="Hours" value={formatHours(stats?.hours_alive ?? null)} />
    </section>
  );
}
