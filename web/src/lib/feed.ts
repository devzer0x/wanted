import type { DecisionRow, EventRow } from "@/lib/types";

export type FeedItem =
  | { kind: "decision"; key: string; ts: string; row: DecisionRow }
  | { kind: "event"; key: string; ts: string; row: EventRow };

const tsMs = (iso: string) => {
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? 0 : t;
};

// Goal lifecycle is telemetry, not commentary. The public feed showed a "Started: <goal_id>"
// and a "Stuck, dropped: <goal_id>" for every free-roam goal, which read as a wall of internal
// churn with raw ids (operator, 2026-09-03: "doesn't look clean ... it shows what it is doing").
// The CURRENT GOAL card already shows what he is doing in human words, so the feed keeps only his
// spoken lines and the dramatic beats (death, busted, wanted, mission, stunt, clip, session).
const FEED_HIDDEN_EVENTS = new Set(["activity_start", "activity_end"]);

/** Interleave decisions + events, newest first. Pure derivation from rows. */
export function buildFeed(
  decisions: DecisionRow[],
  events: EventRow[],
  limit = 120
): FeedItem[] {
  const items: FeedItem[] = [
    ...decisions.map<FeedItem>((row) => ({
      kind: "decision",
      key: `d-${row.id}`,
      ts: row.ts,
      row,
    })),
    ...events
      .filter((row) => !FEED_HIDDEN_EVENTS.has(row.type))
      .map<FeedItem>((row) => ({
        kind: "event",
        key: `e-${row.id}`,
        ts: row.ts,
        row,
      })),
  ];
  items.sort((a, b) => tsMs(b.ts) - tsMs(a.ts) || b.row.id - a.row.id);
  return items.slice(0, limit);
}

/** Merge newly fetched rows into an existing list, deduped by id, sorted id-desc. */
export function mergeRows<T extends { id: number }>(existing: T[], incoming: T[], cap: number): T[] {
  if (incoming.length === 0) return existing;
  const byId = new Map<number, T>();
  for (const row of existing) byId.set(row.id, row);
  for (const row of incoming) byId.set(row.id, row);
  return [...byId.values()].sort((a, b) => b.id - a.id).slice(0, cap);
}
