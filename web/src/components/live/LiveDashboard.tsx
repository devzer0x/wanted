"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { StreamEmbed } from "@/components/StreamEmbed";
import { Counters } from "@/components/live/Counters";
import { Feed } from "@/components/live/Feed";
import { GoalCard } from "@/components/live/GoalCard";
import { Hud } from "@/components/live/Hud";
import { OfflineBanner } from "@/components/live/OfflineBanner";
import { DECISION_COLUMNS, EVENT_COLUMNS, STATS_COLUMNS } from "@/lib/columns";
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

// Fallback polls. Realtime is the fast path; these exist because a postgres_changes subscription
// can report SUBSCRIBED and then deliver nothing — observed on this very project during
// replication-slot warm-up (docs/STATUS.md, "First-ever subscription failed silently"). Before
// this, only `stats` had a safety poll, so a silent socket left the transmissions feed frozen
// forever with no recovery path. Both queries are incremental (`id > last seen`) and return an
// empty body in the common case.
const FEED_POLL_MS = 15_000;
// Shorter than HEARTBEAT_STALE_MS (60 s) by enough that ON/OFF flips within one poll of the
// heartbeat resuming, even with the realtime socket down.
const STATS_POLL_MS = 10_000;
// Relative ages are shown to the second under a minute ("12s ago"), so the clock has to tick at
// that resolution; it is paused while the tab is hidden and resynced on the way back.
const CLOCK_TICK_MS = 1_000;

/**
 * Collapses concurrent triggers of one refetch into a single in-flight request plus at most one
 * trailing re-run. Realtime callbacks, the fallback poll and focus/visibility/online handlers can
 * all fire in the same instant; without this a burst of INSERTs turns into a request storm.
 */
function useCoalesced(fn: () => Promise<void>): () => void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const running = useRef(false);
  const queued = useRef(false);
  return useCallback(() => {
    if (running.current) {
      queued.current = true;
      return;
    }
    running.current = true;
    void (async () => {
      try {
        do {
          queued.current = false;
          await fnRef.current();
        } while (queued.current);
      } finally {
        running.current = false;
      }
    })();
  }, []);
}

// Everything rendered here derives from fetched table rows. Realtime postgres_changes messages
// are used only as "something changed" signals that trigger a row refetch keyed on the last-seen
// id — so a later migration to coalesced broadcast digests changes nothing in the UI (D6), and
// there is no gap after reconnect (postgres_changes has no gap-fill; on re-SUBSCRIBED, on every
// poll and on every return to the tab we refetch everything newer than the last-seen id).
export function LiveDashboard({
  initialDecisions,
  initialEvents,
  initialStats,
  stream,
  initialLinkDown,
  serverNowMs,
}: {
  initialDecisions: DecisionRow[];
  initialEvents: EventRow[];
  initialStats: StatsRow | null;
  stream: StreamConfig | null;
  initialLinkDown: boolean;
  /** Server render clock. Seeding state with it (instead of Date.now() on both sides) keeps the
   *  first client render byte-identical to the server's across the 60 s staleness boundary. */
  serverNowMs: number;
}) {
  const [decisions, setDecisions] = useState(initialDecisions);
  const [events, setEvents] = useState(initialEvents);
  const [stats, setStats] = useState(initialStats);
  const [linkDown, setLinkDown] = useState(initialLinkDown);
  const [nowMs, setNowMs] = useState(serverNowMs);
  const [mounted, setMounted] = useState(false);

  const lastDecisionId = useRef(initialDecisions.reduce((m, r) => Math.max(m, r.id), 0));
  const lastEventId = useRef(initialEvents.reduce((m, r) => Math.max(m, r.id), 0));

  const refetchDecisions = useCoalesced(
    useCallback(async () => {
      const supabase = getBrowserSupabase();
      if (!supabase) return;
      try {
        const { data, error } = await supabase
          .from("decisions")
          .select(DECISION_COLUMNS)
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
    }, [])
  );

  const refetchEvents = useCoalesced(
    useCallback(async () => {
      const supabase = getBrowserSupabase();
      if (!supabase) return;
      try {
        const { data, error } = await supabase
          .from("events")
          .select(EVENT_COLUMNS)
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
    }, [])
  );

  const refetchStats = useCoalesced(
    useCallback(async () => {
      const supabase = getBrowserSupabase();
      if (!supabase) return;
      try {
        const { data, error } = await supabase
          .from("stats")
          .select(STATS_COLUMNS)
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
    }, [])
  );

  const refetchAll = useCallback(() => {
    refetchDecisions();
    refetchEvents();
    refetchStats();
  }, [refetchDecisions, refetchEvents, refetchStats]);

  // Realtime: row changes on decisions/events/stats (CONTRACTS §5 publication set).
  //
  // `stats` listens to "*", not just UPDATE: the harness upserts one row per session, so the very
  // first write of a new session is an INSERT. Listening for UPDATE alone meant the ON/OFF flip at
  // the start of a session waited for the poll instead of the socket.
  useEffect(() => {
    const supabase = getBrowserSupabase();
    if (!supabase) return;
    const channel = supabase
      .channel("wasted-live")
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "decisions" },
        () => refetchDecisions()
      )
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "events" },
        () => refetchEvents()
      )
      .on("postgres_changes", { event: "*", schema: "public", table: "stats" }, () =>
        refetchStats()
      )
      .subscribe((status) => {
        if (status === "SUBSCRIBED") refetchAll();
      });
    return () => {
      void supabase.removeChannel(channel);
    };
  }, [refetchAll, refetchDecisions, refetchEvents, refetchStats]);

  // Fallback polls: the feed and the heartbeat must stay honest even when the realtime socket is
  // silent while REST is reachable.
  useEffect(() => {
    const feed = setInterval(refetchAll, FEED_POLL_MS);
    const statsTimer = setInterval(refetchStats, STATS_POLL_MS);
    return () => {
      clearInterval(feed);
      clearInterval(statsTimer);
    };
  }, [refetchAll, refetchStats]);

  // Coming back to a backgrounded tab must not show a frozen feed and a stale age: browsers throttle
  // timers and may have dropped the websocket while hidden, so resync the clock and the rows at once.
  useEffect(() => {
    const resync = () => {
      if (document.visibilityState !== "visible") return;
      setNowMs(Date.now());
      refetchAll();
    };
    document.addEventListener("visibilitychange", resync);
    window.addEventListener("focus", resync);
    window.addEventListener("online", resync);
    return () => {
      document.removeEventListener("visibilitychange", resync);
      window.removeEventListener("focus", resync);
      window.removeEventListener("online", resync);
    };
  }, [refetchAll]);

  // Clock tick for staleness/age display. Runs only while the tab is visible; the visibility
  // handler above resyncs it on return, so an age can never sit still at the value it had when
  // the tab was hidden.
  useEffect(() => {
    setMounted(true);
    setNowMs(Date.now());
    let clock: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (clock === null) clock = setInterval(() => setNowMs(Date.now()), CLOCK_TICK_MS);
    };
    const stop = () => {
      if (clock !== null) {
        clearInterval(clock);
        clock = null;
      }
    };
    const onVisibility = () => (document.visibilityState === "visible" ? start() : stop());
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

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
          <GoalCard stats={stats} />
        </div>
      </div>
    </div>
  );
}
