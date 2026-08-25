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

function failure<T>(scope: string, error: unknown): Fetched<T> {
  const message = error instanceof Error ? error.message : String(error);
  console.error(
    JSON.stringify({ level: "error", scope: `data.${scope}`, message })
  );
  return { ok: false, error: message };
}

async function selectRows<T>(
  scope: string,
  run: (client: SupabaseClient) => PromiseLike<{ data: unknown; error: { message: string } | null }>
): Promise<Fetched<T>> {
  const client = createServerSupabase();
  if (!client) return { ok: false, error: "supabase_env_missing" };
  try {
    const { data, error } = await run(client);
    if (error) return failure<T>(scope, error.message);
    return { ok: true, rows: (data ?? []) as T[] };
  } catch (err) {
    return failure<T>(scope, err);
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

/**
 * Sum of decision tokens since UTC midnight via a PostgREST aggregate. Aggregates may be
 * disabled on a given Supabase project; in that case (or offline) this resolves null and the
 * UI shows an honest em dash instead of a number.
 */
export async function fetchTokensToday(): Promise<TokensToday | null> {
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
  if (!fetched.ok || fetched.rows.length === 0) return null;
  const row = fetched.rows[0];
  return {
    input: row.input ?? 0,
    output: row.output ?? 0,
    cached: row.cached ?? 0,
  };
}
