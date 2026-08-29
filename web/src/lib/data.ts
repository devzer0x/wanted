import type { SupabaseClient } from "@supabase/supabase-js";
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
    c.from("decisions").select("*").order("id", { ascending: false }).limit(limit)
  );
}

export function fetchEvents(limit = FEED_EVENT_LIMIT): Promise<Fetched<EventRow>> {
  return selectRows<EventRow>("events", (c) =>
    c.from("events").select("*").order("id", { ascending: false }).limit(limit)
  );
}

/** Latest-heartbeat stats row = the current/most recent session. */
export async function fetchStats(): Promise<Fetched<StatsRow>> {
  return selectRows<StatsRow>("stats", (c) =>
    c
      .from("stats")
      .select("*")
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

export interface TokensToday {
  input: number;
  output: number;
  cached: number;
}

// PostgREST rejects aggregate functions unless the project has `db-aggregates-enabled` turned on
// (error PGRST123). That verdict cannot change within a request, so once seen it is remembered
// for the life of the server process rather than paying a failed round trip on every render.
// Enabling aggregates on the Supabase project turns the number on with no code change.
const AGGREGATES_DISABLED_CODE = "PGRST123";
let aggregatesDisabled = false;

/**
 * Sum of decision tokens since UTC midnight via a PostgREST aggregate. Aggregates may be
 * disabled on a given Supabase project; in that case (or offline) this resolves null and the
 * UI shows an honest em dash instead of a number.
 */
export async function fetchTokensToday(): Promise<TokensToday | null> {
  if (aggregatesDisabled) return null;
  const midnightUtc = new Date();
  midnightUtc.setUTCHours(0, 0, 0, 0);
  const fetched = await selectRows<{
    input: number | null;
    output: number | null;
    cached: number | null;
  }>("tokens_today", (c) =>
    c
      .from("decisions")
      .select("input:input_tokens.sum(), output:output_tokens.sum(), cached:cached_tokens.sum()")
      .gte("ts", midnightUtc.toISOString())
  );
  if (!fetched.ok) {
    if (fetched.code === AGGREGATES_DISABLED_CODE) aggregatesDisabled = true;
    return null;
  }
  if (fetched.rows.length === 0) return null;
  const row = fetched.rows[0];
  return {
    input: row.input ?? 0,
    output: row.output ?? 0,
    cached: row.cached ?? 0,
  };
}
