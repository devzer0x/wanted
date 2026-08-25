"use client";

import { useCallback, useRef, useState } from "react";
import { describeEvent, isLoudEvent } from "@/lib/events";
import type { FeedItem } from "@/lib/feed";
import { formatAge, formatUtcClock } from "@/lib/format";
import type { DecisionRow, EventRow } from "@/lib/types";

const MOOD_CLASSES: Record<string, string> = {
  chill: "border-ash text-smoke",
  bored: "border-ash text-smoke",
  hyped: "border-ember text-ember",
  scared: "border-hazard text-hazard",
  smug: "border-blood text-ember",
};

function Stamp({ ts, nowMs, mounted }: { ts: string; nowMs: number; mounted: boolean }) {
  return (
    <time dateTime={ts} title={ts} className="font-mono text-[0.6rem] text-smoke shrink-0">
      {mounted ? formatAge(ts, nowMs) : formatUtcClock(ts)}
    </time>
  );
}

function DecisionEntry({
  row,
  nowMs,
  mounted,
}: {
  row: DecisionRow;
  nowMs: number;
  mounted: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const mood = row.mood ?? "chill";
  return (
    <article className="feed-in border-b border-ash/60 px-3 py-2.5">
      <div className="flex items-baseline gap-2">
        <span
          className={`ticker shrink-0 border px-1 py-px text-[0.55rem] ${MOOD_CLASSES[mood] ?? "border-ash text-smoke"}`}
        >
          {mood}
        </span>
        <p className="min-w-0 flex-1 text-sm leading-snug text-bone">{row.say || "…"}</p>
        <Stamp ts={row.ts} nowMs={nowMs} mounted={mounted} />
      </div>
      {row.thought && (
        <div className="mt-1 pl-1">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="ticker text-[0.58rem] text-smoke transition-colors hover:text-ember"
          >
            {expanded ? "− hide thought" : "+ what he was thinking"}
          </button>
          {expanded && (
            <p className="mt-1 border-l-2 border-blood pl-2 font-mono text-xs leading-relaxed text-smoke">
              {row.thought}
              <span className="mt-0.5 block text-[0.55rem] uppercase tracking-widest text-ash">
                {row.layer} layer · AI-generated
              </span>
            </p>
          )}
        </div>
      )}
    </article>
  );
}

function EventEntry({
  row,
  nowMs,
  mounted,
}: {
  row: EventRow;
  nowMs: number;
  mounted: boolean;
}) {
  const loud = isLoudEvent(row.type);
  return (
    <article className="feed-in border-b border-ash/60 px-3 py-2">
      <div className="flex items-baseline gap-2">
        <span aria-hidden="true" className={loud ? "text-blood" : "text-ash"}>
          ▮
        </span>
        <p
          className={`min-w-0 flex-1 font-mono text-xs leading-snug ${
            loud ? "font-semibold text-ember" : "text-smoke"
          }`}
        >
          {describeEvent(row)}
          {row.screenshot_url && (
            <>
              {" "}
              <a
                href={row.screenshot_url}
                target="_blank"
                rel="noreferrer"
                className="text-bone underline decoration-blood underline-offset-2 hover:text-ember"
              >
                [shot]
              </a>
            </>
          )}
        </p>
        <Stamp ts={row.ts} nowMs={nowMs} mounted={mounted} />
      </div>
    </article>
  );
}

export function Feed({
  items,
  nowMs,
  mounted,
  linkDown,
}: {
  items: FeedItem[];
  nowMs: number;
  mounted: boolean;
  linkDown: boolean;
}) {
  // Auto-scroll keeps the newest entry pinned at the top. When the reader scrolls down into
  // history, updates pause (the visible list freezes) until they return to the top.
  const scrollRef = useRef<HTMLDivElement>(null);
  const pausedRef = useRef(false);
  const [paused, setPaused] = useState(false);
  const [frozen, setFrozen] = useState<FeedItem[] | null>(null);
  const itemsRef = useRef(items);
  itemsRef.current = items;

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const shouldPause = el.scrollTop > 16;
    if (shouldPause === pausedRef.current) return;
    pausedRef.current = shouldPause;
    setPaused(shouldPause);
    setFrozen(shouldPause ? itemsRef.current : null);
  }, []);

  const resume = useCallback(() => {
    scrollRef.current?.scrollTo({ top: 0 });
    pausedRef.current = false;
    setPaused(false);
    setFrozen(null);
  }, []);

  const shown = paused && frozen ? frozen : items;
  const pending = paused && frozen ? Math.max(0, items.length - frozen.length) : 0;

  return (
    <section className="panel flex min-h-0 flex-col" aria-label="Live commentary feed">
      <div className="flex items-center justify-between border-b border-ash px-3 py-2">
        <h2 className="panel-title">Transmissions</h2>
        <span className="ticker text-[0.55rem] text-smoke">AI-generated · newest first</span>
      </div>
      <div className="relative min-h-0 flex-1">
        {pending > 0 && (
          <button
            type="button"
            onClick={resume}
            className="ticker absolute left-1/2 top-2 z-10 -translate-x-1/2 border border-blood bg-void px-3 py-1 text-[0.62rem] text-ember hover:bg-blood hover:text-bone"
          >
            {pending} new — back to top
          </button>
        )}
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="h-[420px] overflow-y-auto overscroll-contain lg:h-[560px]"
        >
          {shown.length === 0 ? (
            <div data-testid="feed-empty" className="px-4 py-10 text-center">
              {linkDown ? (
                <>
                  <p className="wordmark text-xl text-ember" data-text="DATA LINK DOWN">
                    DATA LINK DOWN
                  </p>
                  <p className="mt-2 text-xs text-smoke">
                    No transmissions available — the telemetry database is unreachable.
                  </p>
                </>
              ) : (
                <>
                  <p className="wordmark text-xl" data-text="ALL QUIET">
                    ALL QUIET
                  </p>
                  <p className="mt-2 text-xs text-smoke">
                    No transmissions on record yet. The agent hasn&apos;t said a word.
                  </p>
                </>
              )}
            </div>
          ) : (
            shown.map((item) =>
              item.kind === "decision" ? (
                <DecisionEntry key={item.key} row={item.row} nowMs={nowMs} mounted={mounted} />
              ) : (
                <EventEntry key={item.key} row={item.row} nowMs={nowMs} mounted={mounted} />
              )
            )
          )}
        </div>
      </div>
    </section>
  );
}
