"use client";

import { formatHours, formatInt } from "@/lib/format";
import type { StatsRow } from "@/lib/types";

// Values are formatted, not truncated: a wide number shrinks the type rather than clipping, so
// the four-up row can never push the mobile layout sideways.
function Counter({ label, value, loud }: { label: string; value: string; loud?: boolean }) {
  return (
    <div className="panel flex min-w-0 flex-col items-center gap-0.5 overflow-hidden px-1.5 py-3 sm:px-2">
      <span
        className={`font-display leading-none tabular-nums ${
          value.length > 4 ? "text-base sm:text-xl md:text-2xl" : "text-xl sm:text-2xl md:text-3xl"
        } ${loud ? "text-ember" : "text-bone"}`}
      >
        {value}
      </span>
      <span className="ticker truncate text-[0.5rem] text-smoke sm:text-[0.58rem]">{label}</span>
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
