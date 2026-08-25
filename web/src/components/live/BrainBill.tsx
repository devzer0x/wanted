"use client";

import { formatCompact, formatMoney } from "@/lib/format";
import type { StatsRow } from "@/lib/types";
import type { TokensToday } from "@/lib/data";

export function BrainBill({
  stats,
  tokens,
}: {
  stats: StatsRow | null;
  tokens: TokensToday | null;
}) {
  return (
    <section className="panel p-3 sm:p-4" aria-label="Brain running costs">
      <h2 className="panel-title mb-2">Brain bill</h2>
      <div className="flex items-baseline gap-2">
        <span className="font-display text-2xl text-bone">
          {formatMoney(stats?.cost_per_hour_usd ?? null)}
        </span>
        <span className="ticker text-[0.6rem] text-smoke">per hour</span>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[0.7rem]">
        <dt className="ticker text-[0.6rem] text-smoke">Today</dt>
        <dd className="text-right font-mono text-bone">
          {formatMoney(stats?.cost_today_usd ?? null)}
        </dd>
        <dt className="ticker text-[0.6rem] text-smoke">Tokens today</dt>
        <dd className="text-right font-mono text-bone">
          {tokens ? `${formatCompact(tokens.input + tokens.output)}` : "—"}
        </dd>
        {tokens && (
          <>
            <dt className="ticker text-[0.6rem] text-smoke">of which cached</dt>
            <dd className="text-right font-mono text-smoke">{formatCompact(tokens.cached)}</dd>
          </>
        )}
      </dl>
      <p className="mt-2 text-[0.62rem] leading-snug text-smoke">
        Every thought costs real money. This is the meter.
      </p>
    </section>
  );
}
