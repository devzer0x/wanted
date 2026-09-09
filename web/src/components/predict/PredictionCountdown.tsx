"use client";

import { useEffect, useState } from "react";
import { formatCountdown } from "@/lib/format";

const RING = 100.5; // circumference of r=16, to two places — the dash array below.

/**
 * The clock in a prediction card's header strip. Colour is inherited from that strip, so the same
 * component reads correctly on yellow, coral and ink.
 *
 * `from` is optional and only ever a real timestamp (`opened_at`, or `locks_at` while resolving):
 * when it is present the ring shows how much of that real window is left. With no `from` there is
 * nothing honest to draw a ring against, so only the digits render.
 */
export function PredictionCountdown({
  target,
  label,
  from,
}: {
  target: string;
  label: string;
  from?: string;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const targetMs = new Date(target).getTime();
  if (Number.isNaN(targetMs)) return null;
  const remaining = targetMs - now;
  const done = remaining <= 0;
  const urgent = !done && remaining < 10_000;

  const fromMs = from ? new Date(from).getTime() : Number.NaN;
  const span = targetMs - fromMs;
  const ringOffset =
    Number.isFinite(span) && span > 0
      ? RING * (1 - Math.min(1, Math.max(0, remaining / span)))
      : null;

  return (
    <span
      className="inline-flex items-center gap-2"
      aria-label={`${done ? "0:00" : formatCountdown(remaining)} ${label}`}
    >
      {ringOffset !== null && (
        <svg width="34" height="34" viewBox="0 0 40 40" aria-hidden="true" className="block flex-none">
          <circle cx="20" cy="20" r="16" fill="#fff" stroke="var(--ink)" strokeWidth="3" />
          <circle
            cx="20"
            cy="20"
            r="16"
            fill="none"
            stroke="var(--coral)"
            strokeWidth="6"
            strokeLinecap="round"
            strokeDasharray={RING}
            strokeDashoffset={ringOffset}
            transform="rotate(-90 20 20)"
            style={{ transition: "stroke-dashoffset 1s linear" }}
          />
        </svg>
      )}
      {/* No scaling/pulsing on the digits: `.urgent` is a 1.4x scale-and-fade, which makes a
          clock unreadable at exactly the moment it matters most. Urgency is a colour change. */}
      <span
        suppressHydrationWarning
        className={`font-display text-xl leading-none tabular-nums ${urgent ? "text-coral" : ""}`}
      >
        {done ? "0:00" : formatCountdown(remaining)}
      </span>
      <span
        aria-hidden="true"
        className="hidden text-[10px] font-extrabold uppercase tracking-[0.12em] opacity-75 sm:inline"
      >
        {label}
      </span>
    </span>
  );
}
