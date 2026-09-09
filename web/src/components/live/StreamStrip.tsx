"use client";

import { formatMoney } from "@/lib/format";
import type { HudState } from "@/lib/types";

// The bridge reports player health on a 0–200 scale (CONTRACTS §1 `max_health: 200`, the same
// constant the bridge's own /state payload assumes) and armor on 0–100. The denominator is
// printed next to the number rather than folded into a percentage: 138 on its own reads as an
// impossible percentage, and the frozen `stats.hud` shape carries no per-session max to compute
// a real one from. The bar shows the share; the text keeps the units honest.
const HP_MAX = 200;
const ARMOR_MAX = 100;

function Meter({
  label,
  labelColor,
  value,
  max,
  fill,
  width,
}: {
  label: string;
  labelColor: string;
  value: number;
  max: number;
  fill: string;
  width: number;
}) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <span className="pill pill-solid" style={{ borderWidth: 3, padding: "4px 12px 4px 8px" }}>
      <span className="font-display leading-none" style={{ color: labelColor }}>
        {label}
      </span>
      <span className="meter" style={{ height: 10, width }}>
        <span style={{ width: `${pct}%`, background: fill }} />
      </span>
      <span className="font-display leading-none tabular-nums">
        {value}
        <span style={{ color: "var(--muted)" }}>/{max}</span>
      </span>
    </span>
  );
}

function WantedStars({ level }: { level: number }) {
  return (
    <span
      className="pill pill-solid"
      role="img"
      aria-label={`Wanted level ${level} of 5`}
      style={{ borderWidth: 3, gap: 2, padding: "3px 10px", fontSize: 16, lineHeight: 1 }}
    >
      {[0, 1, 2, 3, 4].map((i) => (
        <span
          key={i}
          aria-hidden="true"
          className={i < level ? "star-lit" : undefined}
          style={{
            color: i < level ? undefined : "var(--sand)",
            WebkitTextStroke: "1.5px var(--ink)",
          }}
        >
          ★
        </span>
      ))}
    </span>
  );
}

function Chip({ label, value }: { label: string; value: string }) {
  return (
    <span className="pill" style={{ background: "var(--sand)", padding: "3px 10px", maxWidth: "100%" }}>
      <span style={{ color: "var(--muted)" }}>{label}</span>
      <span className="min-w-0 truncate">{value}</span>
    </span>
  );
}

/**
 * The telemetry that belongs to the picture: the meters, the wanted stars and the cash sit on a
 * dark band directly under the player, and the current goal plus the where/when chips sit on the
 * white strip below it.
 *
 * Every value here comes from the last `stats` row the harness wrote — `hud` for the meters and
 * chips, `current_goal` for the goal. A chip whose value the game did not report is not rendered
 * at all; there is no placeholder, and no viewer count, because nothing publishes one. When the
 * row carries neither telemetry nor a goal the whole strip is omitted rather than padded out.
 */
export function StreamStrip({
  hud,
  goal,
  offline,
}: {
  hud: HudState | null;
  goal: string | null;
  offline: boolean;
}) {
  const chips: { label: string; value: string }[] = [];
  if (hud) {
    if (hud.vehicle) chips.push({ label: "Ride", value: hud.vehicle });
    if (hud.street) chips.push({ label: "Street", value: hud.street });
    if (hud.zone) chips.push({ label: "Zone", value: hud.zone });
    if (hud.clock) {
      chips.push({
        label: "Clock",
        value: hud.weather ? `${hud.clock} · ${hud.weather.toLowerCase()}` : hud.clock,
      });
    }
  }

  const trimmedGoal = goal?.trim() ? goal.trim() : null;
  if (!hud && !trimmedGoal) return null;

  return (
    <div className="panel overflow-hidden">
      {hud && (
        <div
          className="flex flex-wrap items-center gap-2 px-3 py-2.5"
          style={{ background: "var(--ink-deep)" }}
        >
          <Meter
            label="HP"
            labelColor="var(--coral)"
            value={hud.health}
            max={HP_MAX}
            fill="var(--coral)"
            width={84}
          />
          <Meter
            label="ARM"
            labelColor="var(--blue)"
            value={hud.armor}
            max={ARMOR_MAX}
            fill="var(--blue)"
            width={60}
          />
          <WantedStars level={hud.wanted} />
          <span className="ml-auto flex items-center gap-2">
            {/* The bridge stopped reporting, so this is the last state it sent, not a live one. */}
            {offline && (
              <span
                className="pill pill-sm"
                style={{ background: "var(--sand)", color: "var(--muted)" }}
              >
                last recorded
              </span>
            )}
            <span
              className="pill pill-solid font-display"
              style={{
                background: "var(--teal)",
                borderWidth: 3,
                fontSize: 15,
                padding: "4px 14px",
              }}
            >
              {formatMoney(hud.cash, 0)}
            </span>
          </span>
        </div>
      )}

      {(trimmedGoal || chips.length > 0) && (
        <div
          className={`flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3 ${
            hud ? "border-t-[3px] border-ink" : ""
          }`}
        >
          {trimmedGoal && (
            <p className="flex min-w-[12rem] flex-1 items-baseline gap-2 text-[0.9rem] leading-snug [overflow-wrap:anywhere]">
              <span className="ticker flex-none text-[0.6rem] text-muted">Goal</span>
              <span className="min-w-0">{trimmedGoal}</span>
            </p>
          )}
          {chips.length > 0 && (
            <div className="flex min-w-0 flex-wrap gap-1.5">
              {chips.map((c) => (
                <Chip key={c.label} label={c.label} value={c.value} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
