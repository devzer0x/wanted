"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { StreamEmbed } from "@/components/StreamEmbed";
import { BrainBill } from "@/components/live/BrainBill";
import { Counters } from "@/components/live/Counters";
import { Feed } from "@/components/live/Feed";
import { GoalCard } from "@/components/live/GoalCard";
import { Hud } from "@/components/live/Hud";
import { OfflineBanner } from "@/components/live/OfflineBanner";
import type { TokensToday } from "@/lib/data";
import { buildFeed, mergeRows } from "@/lib/feed";
import { isAgentOffline } from "@/lib/offline";
import { getBrowserSupabase } from "@/lib/supabase/client";
import {
  type DecisionRow,
  type EventRow,
  type StatsRow,
  type StreamConfig,
  parseHud,
} from "@/lib/types";

const DECISION_CAP = 200;
const EVENT_CAP = 150;
const REFETCH_BATCH = 200;
const STATS_POLL_MS = 30_000;
const CLOCK_TICK_MS = 10_000;

// Everything rendered here derives from fetched table rows. Realtime postgres_changes messages
// are used only as "something changed" signals that trigger a row refetch keyed on the last-seen
// id — so a later migration to coalesced broadcast digests changes nothing in the UI (D6), and
// there is no gap after reconnect (postgres_changes has no gap-fill; on re-SUBSCRIBED we refetch
// everything newer than the last-seen id).
export function LiveDashboard({
  initialDecisions,
  initialEvents,
  initialStats,
  initialTokens,
  stream,
  initialLinkDown,
  serverNowMs,
}: {
  initialDecisions: DecisionRow[];
  initialEvents: EventRow[];
  initialStats: StatsRow | null;
  initialTokens: TokensToday | null;
  stream: StreamConfig | null;
  initialLinkDown: boolean;
  /** Server render clock. Seeding state with it (instead of Date.now() on both sides) keeps the
   *  first client render byte-identical to the server's across the 60 s staleness boundary. */
  serverNowMs: number;
}) {
  const [decisions, setDecisions] = useState(initialDecisions);
  const [events, setEvents] = useState(initialEvents);
  const [stats, setStats] = useState(initialStats);
  const [tokens] = useState(initialTokens);
  const [linkDown, setLinkDown] = useState(initialLinkDown);
  const [nowMs, setNowMs] = useState(serverNowMs);
  const [mounted, setMounted] = useState(false);

  const lastDecisionId = useRef(initialDecisions.reduce((m, r) => Math.max(m, r.id), 0));
  const lastEventId = useRef(initialEvents.reduce((m, r) => Math.max(m, r.id), 0));

  const refetchDecisions = useCallback(async () => {
    const supabase = getBrowserSupabase();
    if (!supabase) return;
    try {
      const { data, error } = await supabase
        .from("decisions")
        .select("*")
        .gt("id", lastDecisionId.current)
        .order("id", { ascending: true })
        .limit(REFETCH_BATCH);
      if (error) throw new Error(error.message);
      setLinkDown(false);
      const rows = (data ?? []) as DecisionRow[];
      if (rows.length === 0) return;
      lastDecisionId.current = rows[rows.length - 1].id;
      setDecisions((prev) => mergeRows(prev, rows, DECISION_CAP));
    } catch (err) {
      console.error("refetch decisions failed:", err instanceof Error ? err.message : err);
      setLinkDown(true);
    }
  }, []);

  const refetchEvents = useCallback(async () => {
    const supabase = getBrowserSupabase();
    if (!supabase) return;
    try {
      const { data, error } = await supabase
        .from("events")
        .select("*")
        .gt("id", lastEventId.current)
        .order("id", { ascending: true })
        .limit(REFETCH_BATCH);
      if (error) throw new Error(error.message);
      setLinkDown(false);
      const rows = (data ?? []) as EventRow[];
      if (rows.length === 0) return;
      lastEventId.current = rows[rows.length - 1].id;
      setEvents((prev) => mergeRows(prev, rows, EVENT_CAP));
    } catch (err) {
      console.error("refetch events failed:", err instanceof Error ? err.message : err);
      setLinkDown(true);
    }
  }, []);

  const refetchStats = useCallback(async () => {
    const supabase = getBrowserSupabase();
    if (!supabase) return;
    try {
      const { data, error } = await supabase
        .from("stats")
        .select("*")
        .order("heartbeat_at", { ascending: false, nullsFirst: false })
        .limit(1);
      if (error) throw new Error(error.message);
      setLinkDown(false);
      const row = (data ?? [])[0] as StatsRow | undefined;
      if (row) setStats(row);
    } catch (err) {
      console.error("refetch stats failed:", err instanceof Error ? err.message : err);
      setLinkDown(true);
    }
  }, []);

  // Realtime: INSERTs on decisions/events, UPDATEs on stats (CONTRACTS §5 publication set).
  useEffect(() => {
    const supabase = getBrowserSupabase();
    if (!supabase) return;
    const channel = supabase
      .channel("wasted-live")
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "decisions" },
        () => void refetchDecisions()
      )
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "events" },
        () => void refetchEvents()
      )
      .on(
        "postgres_changes",
        { event: "UPDATE", schema: "public", table: "stats" },
        () => void refetchStats()
      )
      .subscribe((status) => {
        if (status === "SUBSCRIBED") {
          void refetchDecisions();
          void refetchEvents();
          void refetchStats();
        }
      });
    return () => {
      void supabase.removeChannel(channel);
    };
  }, [refetchDecisions, refetchEvents, refetchStats]);

  // Clock tick for staleness/age display; stats poll keeps the offline banner honest even if
  // the realtime socket is down while REST is reachable.
  useEffect(() => {
    setMounted(true);
    setNowMs(Date.now());
    const clock = setInterval(() => setNowMs(Date.now()), CLOCK_TICK_MS);
    const poll = setInterval(() => void refetchStats(), STATS_POLL_MS);
    return () => {
      clearInterval(clock);
      clearInterval(poll);
    };
  }, [refetchStats]);

  const feedItems = useMemo(() => buildFeed(decisions, events), [decisions, events]);
  const hud = useMemo(() => parseHud(stats?.hud ?? null), [stats]);
  const offline = isAgentOffline(stats, nowMs);

  // Layout: the feed is the star. On mobile it sits directly under the stream and counters and
  // above the secondary panels; on lg it becomes a sticky right-hand column that stays in view
  // while the rest scrolls. The lg placement is explicit (col-start/row-start), so the `order-*`
  // classes only take effect in the single-column stack.
  return (
    <div className="flex flex-col gap-3 pt-3">
      <OfflineBanner stats={stats} nowMs={nowMs} mounted={mounted} linkDown={linkDown} />
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="order-1 min-w-0 lg:col-start-1 lg:row-start-1">
          <StreamEmbed config={stream} offline={offline} />
        </div>
        <div className="order-2 min-w-0 lg:col-start-1 lg:row-start-2">
          <Counters stats={stats} />
        </div>
        <div className="order-3 min-w-0 lg:sticky lg:top-16 lg:col-start-2 lg:row-start-1 lg:row-span-3 lg:self-start">
          <Feed items={feedItems} nowMs={nowMs} mounted={mounted} linkDown={linkDown} />
        </div>
        <div className="order-4 grid min-w-0 items-start gap-3 sm:grid-cols-2 lg:col-start-1 lg:row-start-3">
          <Hud hud={hud} />
          <div className="flex min-w-0 flex-col gap-3">
            <GoalCard stats={stats} />
            <BrainBill stats={stats} tokens={tokens} />
          </div>
        </div>
      </div>
    </div>
  );
}
