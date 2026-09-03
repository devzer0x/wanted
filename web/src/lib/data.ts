import type { SupabaseClient } from "@supabase/supabase-js";
import { DECISION_COLUMNS, EVENT_COLUMNS, STATS_COLUMNS } from "@/lib/columns";
import { createServerSupabase } from "@/lib/supabase/server";
import {
  type ClipRow,
  type DecisionRow,
  type EventRow,
  type Fetched,
  type MissionRow,
  type StatsRow,
  type StreamConfig,
  parseStreamConfig,
} from "@/lib/types";

// Server-side initial-row fetches. Every helper resolves — never throws — so pages can always
// render an honest empty/offline state instead of crashing when Supabase is unreachable.

export const FEED_DECISION_LIMIT = 60;
export const FEED_EVENT_LIMIT = 40;

interface QueryError {
  message: string;
  code?: string;
}

interface QueryResult {
  data: unknown;
  error: QueryError | null;
}

/**
 * The PostgREST query builder, narrowed to what this module uses.
 *
 * `retry` matters: postgrest-js retries network failures three times with backoff and sleeps
 * through that chain even when the caller's AbortSignal has already fired (the sleep listens to
 * the builder's own signal, not ours) — measured at 7.0 s per query against a refused connection
 * with @supabase/postgrest-js 2.109.0. On a server-rendered page that is seven seconds of
 * nothing before an honest "data link down" appears, so the library retry is turned off and the
 * single attempt is bounded by the client's 4 s deadline instead. Recovery is the client's job:
 * the live page re-polls stats every 30 s and refetches rows on every Realtime resubscribe.
 */
interface RetryableQuery extends PromiseLike<QueryResult> {
  retry(enabled: boolean): RetryableQuery;
}

function failure<T>(
  scope: string,
  message: string,
  startedAt: number,
  code?: string
): Fetched<T> {
  console.error(
    JSON.stringify({
      level: "error",
      scope: `data.${scope}`,
      message,
      code,
      duration_ms: Math.round(performance.now() - startedAt),
    })
  );
  return { ok: false, error: message, code };
}

async function selectRows<T>(
  scope: string,
  run: (client: SupabaseClient) => RetryableQuery
): Promise<Fetched<T>> {
  const client = createServerSupabase();
  if (!client) return { ok: false, error: "supabase_env_missing" };
  const startedAt = performance.now();
  try {
    const { data, error } = await run(client).retry(false);
    if (error) return failure<T>(scope, error.message, startedAt, error.code);
    return { ok: true, rows: (data ?? []) as T[] };
  } catch (err) {
    return failure<T>(scope, err instanceof Error ? err.message : String(err), startedAt);
  }
}

export function fetchDecisions(limit = FEED_DECISION_LIMIT): Promise<Fetched<DecisionRow>> {
  return selectRows<DecisionRow>("decisions", (c) =>
    c.from("decisions").select(DECISION_COLUMNS).order("id", { ascending: false }).limit(limit)
  );
}

export function fetchEvents(limit = FEED_EVENT_LIMIT): Promise<Fetched<EventRow>> {
  return selectRows<EventRow>("events", (c) =>
    c.from("events").select(EVENT_COLUMNS).order("id", { ascending: false }).limit(limit)
  );
}

/**
 * When the agent was born: the `started_at` of the very first session ever recorded. Read from
 * `sessions` (anon-readable, RLS `sessions_public_read`) rather than derived from `stats`,
 * because the operator's own verdict on the counters was "statistics are not accurate" — a
 * timestamp the harness wrote once and never touched is the one number here that cannot drift.
 */
export async function fetchBorn(): Promise<Fetched<{ started_at: string }>> {
  return selectRows<{ started_at: string }>("sessions", (c) =>
    c.from("sessions").select("started_at").order("started_at", { ascending: true }).limit(1)
  );
}

/** Latest-heartbeat stats row = the current/most recent session. */
export async function fetchStats(): Promise<Fetched<StatsRow>> {
  return selectRows<StatsRow>("stats", (c) =>
    c
      .from("stats")
      .select(STATS_COLUMNS)
      .order("heartbeat_at", { ascending: false, nullsFirst: false })
      .limit(1)
  );
}

export function fetchMissions(limit = 200): Promise<Fetched<MissionRow>> {
  return selectRows<MissionRow>("missions", (c) =>
    c.from("missions").select("*").order("started_at", { ascending: false }).limit(limit)
  );
}

export function fetchClips(limit = 60): Promise<Fetched<ClipRow>> {
  return selectRows<ClipRow>("clips", (c) =>
    c.from("clips").select("*").order("id", { ascending: false }).limit(limit)
  );
}

export function fetchClip(id: number): Promise<Fetched<ClipRow>> {
  return selectRows<ClipRow>("clip", (c) => c.from("clips").select("*").eq("id", id).limit(1));
}

/**
 * Stream source per CONTRACTS §5 v1.1: the site_config "stream" row, read at request time,
 * with env fallback (STREAM_PROVIDER/STREAM_CHANNEL, server-only) when the row is absent.
 */
export async function resolveStreamConfig(): Promise<StreamConfig | null> {
  const fetched = await selectRows<{ value: unknown }>("site_config.stream", (c) =>
    c.from("site_config").select("value").eq("key", "stream").limit(1)
  );
  if (fetched.ok && fetched.rows.length > 0) {
    const parsed = parseStreamConfig(fetched.rows[0].value);
    if (parsed) return parsed;
  }
  const provider = process.env.STREAM_PROVIDER;
  const channel = process.env.STREAM_CHANNEL;
  if ((provider === "twitch" || provider === "youtube") && channel) {
    // Env fallback carries no video_id by design; the YouTube embed needs one and will render
    // its honest "not configured" panel until the site_config row provides it.
    return { provider, channel, video_id: null };
  }
  return null;
}
