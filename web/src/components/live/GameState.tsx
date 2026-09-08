"use client";

import { useEffect, useState } from "react";
import { formatBornAge, formatHours, formatInt, formatMoney, formatUtcStamp } from "@/lib/format";
import type { HudState, StatsRow } from "@/lib/types";

// Bridge reports player health on a 0–200 scale (CONTRACTS §1 `max_health: 200`, the bridge's
// own `/state` payload — the same constant this bar's fill already assumes); armor is 0–100.
// The raw value is shown as "value/max", never a bare number: 169 alone reads as an impossible
// percentage, and computing an actual "84%" would need `max_health` in `stats.hud`, which the
// frozen CONTRACTS §5 shape does not carry — that is a real gap, not a fact to paper over by
// inventing a denominator no one gave the site.
function Bar({
  label,
  value,
  max,
  tone,
}: {
  label: string;
  value: number;
  max: number;
  tone: "blood" | "bone";
}) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="flex items-center gap-2">
      <span className="ticker w-8 text-[0.6rem] text-smoke">{label}</span>
      <div className="h-2 flex-1 border border-ash bg-void">
        <div
          className={tone === "blood" ? "h-full bg-blood" : "h-full bg-bone"}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="min-w-[3.5rem] text-right font-mono text-[0.65rem] tabular-nums text-bone">
        {value}/{max}
      </span>
    </div>
  );
}

function WantedStars({ level }: { level: number }) {
  return (
    <span className="font-mono text-sm tracking-widest" role="img" aria-label={`Wanted level ${level} of 5`}>
      {[0, 1, 2, 3, 4].map((i) => (
        <span key={i} className={i < level ? "star-lit" : "star"} aria-hidden="true">
          ★
        </span>
      ))}
    </span>
  );
}

function Stat({ label, value, loud }: { label: string; value: string; loud?: boolean }) {
  return (
    <div className="flex min-w-0 flex-1 flex-col items-center gap-0.5 border border-ash px-1 py-1.5">
      <span className={`font-mono text-sm tabular-nums ${loud ? "text-ember" : "text-bone"}`}>{value}</span>
      <span className="ticker truncate text-[0.5rem] text-smoke">{label}</span>
    </div>
  );
}

/** "operating for 12 days, 4 hours" — the one number the harness wrote exactly once (the first
 *  session's start), kept because the operator's verdict on the old per-session counters was
 *  "statistics are not accurate". Runs its own clock so the game-state panel doesn't need to
 *  thread nowMs down just for this line. */
function OperatingSince({ bornAt }: { bornAt: string | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(id);
  }, []);
  if (!bornAt) return <span className="ticker text-[0.55rem] text-smoke">not born yet</span>;
  return (
    <span className="ticker text-[0.55rem] text-smoke">
      operating {formatBornAge(bornAt, now)} · since {formatUtcStamp(bornAt)}
    </span>
  );
}

// STATUS, GOAL, HP, WANTED level, VEHICLE, DEATHS, TIME ALIVE — one panel, dense and legible at
// a glance. SPEED is asked for by the product brief but is not part of the frozen stats.hud
// shape (CONTRACTS §5: health/armor/wanted/cash/vehicle/street/zone/clock/weather only); rather
// than invent a number the game never reports, this panel omits it.
export function GameState({
  stats,
  hud,
  offline,
  bornAt,
}: {
  stats: StatsRow | null;
  hud: HudState | null;
  offline: boolean;
  bornAt: string | null;
}) {
  return (
    <section className="panel p-3 sm:p-4" aria-label="Agent game state">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className={`led ${offline ? "led-dead" : "led-live"}`} aria-hidden="true" />
          <span className={`ticker text-[0.62rem] ${offline ? "text-smoke" : "text-ember"}`}>
            {offline ? "offline" : "live"}
          </span>
        </div>
        {typeof stats?.governor_level === "number" && (
          <span className="ticker border border-ash px-1.5 py-0.5 text-[0.55rem] text-smoke">
            L{stats.governor_level}
          </span>
        )}
      </div>

      <p className="mb-3 font-mono text-sm leading-snug text-bone [overflow-wrap:anywhere]">
        {stats?.current_goal || "No goal on record."}
      </p>

      {!hud ? (
        <p className="text-xs text-smoke">No telemetry on record.</p>
      ) : (
        <div className="flex flex-col gap-2.5">
          <Bar label="HP" value={hud.health} max={200} tone="blood" />
          <Bar label="ARM" value={hud.armor} max={100} tone="bone" />
          <div className="flex flex-wrap items-center justify-between gap-2">
            <WantedStars level={hud.wanted} />
            <span className="font-mono text-sm text-bone">{formatMoney(hud.cash, 0)}</span>
          </div>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[0.7rem]">
            <dt className="ticker text-[0.6rem] text-smoke">Vehicle</dt>
            <dd className="text-right font-mono text-bone truncate">{hud.vehicle ?? "on foot"}</dd>
            <dt className="ticker text-[0.6rem] text-smoke">Street</dt>
            <dd className="text-right font-mono text-bone truncate">{hud.street || "—"}</dd>
            <dt className="ticker text-[0.6rem] text-smoke">Zone</dt>
            <dd className="text-right font-mono text-bone truncate">{hud.zone || "—"}</dd>
            <dt className="ticker text-[0.6rem] text-smoke">Clock</dt>
            <dd className="text-right font-mono text-bone">
              {hud.clock || "—"}
              {hud.weather ? ` · ${hud.weather.toLowerCase()}` : ""}
            </dd>
          </dl>
        </div>
      )}

      <div className="mt-3 flex gap-1.5">
        <Stat label="Deaths" value={formatInt(stats?.deaths ?? null)} loud />
        <Stat label="Busted" value={formatInt(stats?.busted ?? null)} loud />
        <Stat label="Missions" value={formatInt(stats?.missions_passed ?? null)} />
        <Stat label="Time alive" value={`${formatHours(stats?.hours_alive ?? null)}h`} />
      </div>

      <div className="mt-3 border-t border-ash pt-2">
        <OperatingSince bornAt={bornAt} />
      </div>
    </section>
  );
}
