"use client";

import { useEffect, useState } from "react";
import { formatBornAge, formatHours, formatInt, formatUtcStamp } from "@/lib/format";
import type { HudState, StatsRow } from "@/lib/types";

function Tile({ label, value, tone }: { label: string; value: string; tone?: "coral" }) {
  return (
    <div
      className="flex min-w-0 flex-col items-center justify-center gap-1 rounded-[14px] border-[3px] border-ink px-2 py-2.5 text-center"
      style={{ background: "var(--cream)" }}
    >
      <span
        className="font-display text-[1.35rem] leading-none tabular-nums"
        style={tone === "coral" ? { color: "var(--coral)" } : undefined}
      >
        {value}
      </span>
      <span className="ticker text-[0.55rem] text-muted">{label}</span>
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
  if (!bornAt) return <span className="ticker text-[0.55rem] text-muted">not born yet</span>;
  return (
    <span className="ticker text-[0.55rem] text-muted">
      operating {formatBornAge(bornAt, now)} · since {formatUtcStamp(bornAt)}
    </span>
  );
}

// The run so far: how often he has died, been arrested, passed a job, and how long he has been
// awake. HP, armor, the wanted stars, the cash and the current goal live under the stream, where
// the picture they describe is — this panel is the ledger, not the HUD.
//
// SPEED is asked for by the product brief but is not part of the frozen stats.hud shape
// (CONTRACTS §5: health/armor/wanted/cash/vehicle/street/zone/clock/weather only); rather than
// invent a number the game never reports, it is omitted.
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
  const hours = formatHours(stats?.hours_alive ?? null);
  return (
    <section className="panel overflow-hidden" aria-label="Agent game state">
      <div
        className="flex flex-wrap items-center justify-between gap-2 border-b-[3px] border-ink px-4 py-2.5"
        style={{ background: "var(--sand)" }}
      >
        <h2 className="panel-title text-[1.05rem]">The run so far</h2>
        <div className="flex items-center gap-2">
          {typeof stats?.governor_level === "number" && (
            <span className="pill pill-sm" style={{ background: "var(--cream)" }}>
              governor L{stats.governor_level}
            </span>
          )}
          <span className="pill pill-sm">
            <span className={`led ${offline ? "led-dead" : "led-live"}`} aria-hidden="true" />
            {offline ? "off air" : "live"}
          </span>
        </div>
      </div>

      <div className="px-4 py-3.5">
        {!hud && (
          <p className="mb-3 text-[0.8rem] leading-relaxed text-dim">
            No telemetry on record — nothing has been reported for the panels below.
          </p>
        )}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {/* The coral is for a number he actually earned; an em dash is the absence of one and
              must not be dressed up as a score. */}
          <Tile
            label="Deaths"
            value={formatInt(stats?.deaths ?? null)}
            tone={typeof stats?.deaths === "number" ? "coral" : undefined}
          />
          <Tile
            label="Busted"
            value={formatInt(stats?.busted ?? null)}
            tone={typeof stats?.busted === "number" ? "coral" : undefined}
          />
          <Tile label="Missions" value={formatInt(stats?.missions_passed ?? null)} />
          <Tile label="Hours alive" value={hours === "—" ? "—" : `${hours}h`} />
        </div>
        <div
          className="mt-3 border-t-[3px] border-dashed pt-2.5"
          style={{ borderColor: "var(--sand-deep)" }}
        >
          <OperatingSince bornAt={bornAt} />
        </div>
      </div>
    </section>
  );
}
