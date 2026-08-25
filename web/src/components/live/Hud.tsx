"use client";

import { formatMoney } from "@/lib/format";
import type { HudState } from "@/lib/types";

// Bridge reports player health on a 0–200 scale (CONTRACTS §1 max_health: 200); armor is 0–100.
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
      <span className="w-8 text-right font-mono text-[0.65rem] text-bone">{value}</span>
    </div>
  );
}

function WantedStars({ level }: { level: number }) {
  return (
    <span
      className="font-mono text-sm tracking-widest"
      role="img"
      aria-label={`Wanted level ${level} of 5`}
    >
      {[0, 1, 2, 3, 4].map((i) => (
        <span key={i} className={i < level ? "star-lit" : "star"} aria-hidden="true">
          ★
        </span>
      ))}
    </span>
  );
}

export function Hud({ hud }: { hud: HudState | null }) {
  return (
    <section className="panel p-3 sm:p-4" aria-label="the agent's in-game status">
      <h2 className="panel-title mb-3">Vitals</h2>
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
          <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-[0.7rem]">
            <dt className="ticker text-[0.6rem] text-smoke">Ride</dt>
            <dd className="text-right font-mono text-bone truncate">
              {hud.vehicle ?? "on foot"}
            </dd>
            <dt className="ticker text-[0.6rem] text-smoke">Street</dt>
            <dd className="text-right font-mono text-bone truncate">
              {hud.street || "—"}
            </dd>
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
    </section>
  );
}
