// Locale-independent formatters: server render and client hydration must produce identical
// strings, so no toLocaleString/Intl with implicit locales.

export function formatMoney(value: number | string | null | undefined, decimals = 2): string {
  if (value === null || value === undefined) return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "—";
  const fixed = Math.abs(n).toFixed(decimals);
  const [whole, frac] = fixed.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const sign = n < 0 ? "-" : "";
  return `${sign}$${grouped}${frac ? `.${frac}` : ""}`;
}

export function formatInt(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return String(Math.trunc(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)}B`;
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000) return `${(value / 1_000).toFixed(1)}K`;
  return formatInt(value);
}

export function formatHours(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(1);
}

const pad = (n: number) => String(n).padStart(2, "0");

/** Stable UTC clock string, e.g. "21:14:03". */
export function formatUtcClock(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

/** Stable UTC date+time, e.g. "2026-08-25 21:14 UTC". */
export function formatUtcStamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown time";
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

/** Relative age label; only rendered client-side after mount (depends on Date.now()). */
export function formatAge(iso: string, nowMs: number): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.round((nowMs - t) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  if (m < 60) return rest ? `${m}m ${rest}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/** "3 days, 7 hours" — the age of something born at `iso`, for the born-ago line. */
export function formatBornAge(iso: string, nowMs: number): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const totalHours = Math.max(0, Math.floor((nowMs - t) / 3_600_000));
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  const d = days === 1 ? "1 day" : `${days} days`;
  const h = hours === 1 ? "1 hour" : `${hours} hours`;
  if (days === 0) return h;
  return `${d}, ${h}`;
}

/**
 * Renders a base-unit integer string (CONTRACTS-PREDICTIONS §1: amounts are always
 * `numeric(38,18)` in the database and `bigint` base units in code) as a display amount.
 *
 * String math only — a reward routed through `Number` loses precision above 2^53 and a reward
 * that rounds is a reward that lies. `decimals` defaults to 18, the DB-wide convention (not an
 * asset-specific constant: every `numeric(38,18)` column uses it, whatever `reward_asset` says).
 */
export function formatBaseUnits(
  value: string | null | undefined,
  decimals = 18,
  maxFractionDigits = 4
): string {
  if (value === null || value === undefined || !/^-?\d+$/.test(value)) return "—";
  const negative = value.startsWith("-");
  const digits = negative ? value.slice(1) : value;
  const padded = digits.padStart(decimals + 1, "0");
  const whole = padded.slice(0, padded.length - decimals).replace(/^0+(?=\d)/, "");
  const fracFull = padded.slice(padded.length - decimals);
  const frac = fracFull.slice(0, maxFractionDigits).replace(/0+$/, "");
  const groupedWhole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}${groupedWhole}${frac ? `.${frac}` : ""}`;
}

/** True when a base-unit string is present and non-zero — the "is there really a pool/balance
 *  here" check used to decide whether to show an amount or an honest "not funded" state. */
export function hasBaseUnits(value: string | null | undefined): boolean {
  return typeof value === "string" && /^-?\d+$/.test(value) && BigInt(value) !== BigInt(0);
}

/** "4:32" under an hour, "1:04:32" at or past one hour. Counts down to zero, never negative. */
export function formatCountdown(msRemaining: number): string {
  const s = Math.max(0, Math.round(msRemaining / 1000));
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return hh > 0 ? `${hh}:${pad(mm)}:${pad(ss)}` : `${mm}:${pad(ss)}`;
}

