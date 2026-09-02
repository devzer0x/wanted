// Row shapes per docs/CONTRACTS.md §5 (frozen). Rendering derives from these rows only.
//
// DecisionRow / EventRow / StatsRow describe what the site SELECTS (src/lib/columns.ts), which is
// a deliberate subset of the table. The spend columns the contract also defines —
// `decisions.cost_usd`, `decisions.*_tokens`, `stats.cost_today_usd`, `stats.cost_per_hour_usd` —
// are intentionally absent: the public site does not publish running costs, so it does not fetch
// them, and typing them away here means a future component cannot render a value that was never
// requested. The columns are untouched in the database and still drive the harness's budget
// governor.

export interface DecisionRow {
  id: number;
  ts: string;
  layer: "tactical" | "director";
  thought: string | null;
  say: string | null;
  mood: string | null;
}

export interface EventRow {
  id: number;
  ts: string;
  type: string; // CONTRACTS §4 enum; render defensively for forward-compat
  payload: Record<string, unknown> | null;
  screenshot_url: string | null;
}

export interface MissionRow {
  id: number;
  session_id: string;
  name: string;
  started_at: string;
  ended_at: string | null;
  outcome: "passed" | "failed" | "skipped" | null;
  attempts: number | null;
  deaths: number | null;
  tokens: number | null;
  summary: string | null;
}

export interface ClipRow {
  id: number;
  session_id: string;
  ts: string;
  event_id: number | null;
  storage_path: string;
  duration_s: number | null;
  caption: string | null;
}

// stats.hud shape is frozen in CONTRACTS §5.
export interface HudState {
  health: number;
  armor: number;
  wanted: number;
  cash: number;
  vehicle: string | null;
  street: string;
  zone: string;
  clock: string;
  weather: string;
}

export interface StatsRow {
  deaths: number | null;
  busted: number | null;
  missions_passed: number | null;
  hours_alive: number | null;
  governor_level: number | null;
  heartbeat_at: string | null;
  current_goal: string | null;
  hud: HudState | null;
}

export interface SiteConfigRow {
  key: string;
  value: Record<string, unknown> | null;
  updated_at: string | null;
}

// site_config "stream" row value, CONTRACTS §5 v1.1.
export interface StreamConfig {
  provider: "twitch" | "youtube";
  channel: string | null;
  video_id: string | null;
}

export function parseStreamConfig(value: unknown): StreamConfig | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as Record<string, unknown>;
  if (v.provider !== "twitch" && v.provider !== "youtube") return null;
  return {
    provider: v.provider,
    channel: typeof v.channel === "string" && v.channel.length > 0 ? v.channel : null,
    video_id: typeof v.video_id === "string" && v.video_id.length > 0 ? v.video_id : null,
  };
}

export function parseHud(value: unknown): HudState | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as Record<string, unknown>;
  if (typeof v.health !== "number" || typeof v.wanted !== "number") return null;
  return {
    health: v.health,
    armor: typeof v.armor === "number" ? v.armor : 0,
    wanted: v.wanted,
    cash: typeof v.cash === "number" ? v.cash : 0,
    vehicle: typeof v.vehicle === "string" ? v.vehicle : null,
    street: typeof v.street === "string" ? v.street : "",
    zone: typeof v.zone === "string" ? v.zone : "",
    clock: typeof v.clock === "string" ? v.clock : "",
    weather: typeof v.weather === "string" ? v.weather : "",
  };
}

/** A fetch outcome that never throws: either rows or an honest error (with the PostgREST code
 *  when the failure came from the API rather than the transport). */
export type Fetched<T> =
  | { ok: true; rows: T[] }
  | { ok: false; error: string; code?: string };

export const MOODS = ["chill", "bored", "hyped", "scared", "smug"] as const;
