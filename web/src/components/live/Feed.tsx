"use client";

import { useCallback, useRef, useState } from "react";
import { describeEvent, isLoudEvent } from "@/lib/events";
import type { FeedItem } from "@/lib/feed";
import { formatAge, formatUtcClock } from "@/lib/format";
import type { DecisionRow, EventRow } from "@/lib/types";

// Mood drives the dot and the label above the line, so a glance down the feed reads as a mood
// track. Keys are the CONTRACTS §2 enum; anything else falls back to the neutral blue.
const MOOD_COLOR: Record<string, string> = {
  hyped: "var(--coral)",
  chill: "var(--blue)",
  smug: "var(--purple)",
  scared: "var(--yellow)",
  bored: "var(--muted)",
};
const moodColor = (mood: string) => MOOD_COLOR[mood] ?? "var(--blue)";

function Stamp({
  ts,
  nowMs,
  mounted,
  className = "text-[0.68rem] text-muted",
  style,
}: {
  ts: string;
  nowMs: number;
  mounted: boolean;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <time dateTime={ts} title={ts} className={`shrink-0 ${className}`} style={style}>
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
  const color = moodColor(mood);
  return (
    <article data-testid="feed-decision" className="feed-in flex items-start gap-2.5">
      <span
        aria-hidden="true"
        className="mt-4 h-3 w-3 flex-none rounded-full border-2 border-ink"
        style={{ background: color }}
      />
      <div
        className="min-w-0 flex-1 border-[3px] border-ink px-3.5 py-2.5"
        style={{ background: "var(--cream)", borderRadius: "16px 16px 16px 4px" }}
      >
        <div className="mb-1 flex items-center justify-between gap-2">
          {/* Uppercased by `.ticker`; the DOM text stays the raw contract value. */}
          <span className="ticker text-[0.6rem]" style={{ color }}>
            {mood}
          </span>
          <Stamp ts={row.ts} nowMs={nowMs} mounted={mounted} />
        </div>
        <p className="text-[0.95rem] font-extrabold leading-snug [overflow-wrap:anywhere]">
          {row.say}
        </p>
        {row.thought && (
          <>
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              className="mt-1.5 text-[0.75rem] font-black text-purple transition-colors hover:text-coral focus-visible:text-coral"
            >
              {expanded ? "− hide thought" : "+ what he was thinking"}
            </button>
            {expanded && (
              <div
                className="mt-2 border-l-4 border-purple bg-white px-2.5 py-2 text-[0.8rem] font-bold leading-relaxed text-dim [overflow-wrap:anywhere]"
                style={{ borderRadius: "0 10px 10px 0" }}
              >
                {row.thought}
                <span className="ticker mt-1 block text-[0.55rem] text-muted">
                  {row.layer} layer · AI-generated
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </article>
  );
}

function EventEntry({ row, nowMs, mounted }: { row: EventRow; nowMs: number; mounted: boolean }) {
  const loud = isLoudEvent(row.type);
  // Alternating tilt keyed on the row id, not the list index: an index-based tilt would make
  // every sticker jump to the other angle each time a newer row arrives.
  const tilt = row.id % 2 === 0 ? "-1deg" : "1deg";
  return (
    <article data-testid="feed-event" className="feed-in flex justify-center">
      <span
        className="pill pill-solid panel-title"
        style={{
          borderWidth: 3,
          fontSize: 13,
          letterSpacing: "0.06em",
          padding: "5px 14px",
          background: loud ? "var(--coral)" : "#fff",
          color: loud ? "#fff" : "var(--ink)",
          transform: `rotate(${tilt})`,
          maxWidth: "100%",
          whiteSpace: "normal",
          textAlign: "center",
        }}
      >
        {describeEvent(row)}
        {row.screenshot_url && (
          <a
            href={row.screenshot_url}
            target="_blank"
            rel="noreferrer"
            className="underline underline-offset-2"
            style={{ color: "inherit", fontSize: 11 }}
          >
            shot
          </a>
        )}
        <Stamp
          ts={row.ts}
          nowMs={nowMs}
          mounted={mounted}
          className=""
          style={{
            fontFamily: "var(--font-sans)",
            fontSize: 10,
            fontWeight: 800,
            letterSpacing: 0,
            textTransform: "none",
            opacity: 0.75,
          }}
        />
      </span>
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
  // The mood he is in right now is the mood of the newest line he actually said. It is only
  // shown when a real decision row supplies one.
  const latestMood =
    items.find(
      (i): i is Extract<FeedItem, { kind: "decision" }> =>
        i.kind === "decision" && Boolean(i.row.mood)
    )?.row.mood ?? null;

  // Height follows the content up to a cap, then scrolls: a three-line feed on launch night
  // should not paint a screen of empty panel, and a busy feed should not push the page down.
  return (
    <section className="panel flex flex-col overflow-hidden" aria-label="Live commentary feed">
      <div
        className="flex flex-none flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b-[3px] border-ink px-4 py-2.5"
        style={{ background: "var(--yellow-pale)" }}
      >
        <div className="flex min-w-0 items-center gap-2.5">
          <span
            aria-hidden="true"
            className="font-display flex h-[34px] w-[34px] flex-none items-center justify-center rounded-[12px] border-[3px] border-ink text-[16px]"
            style={{ background: "var(--ink)", color: "var(--yellow)" }}
          >
            W
          </span>
          <div className="min-w-0 leading-tight">
            <h2 className="panel-title text-[1.05rem]">WANTED is thinking</h2>
            {/* "AI-generated" is a standing disclosure (CLAUDE.md §7), not a slot to reuse for
                transient status — least of all while a reader has scrolled back to dwell on the
                commentary. The pause note is appended to it, never swapped in for it. */}
            <span
              className="ticker block text-[0.55rem] text-muted"
              data-testid="feed-status"
              data-paused={paused ? "true" : "false"}
            >
              AI-generated · {paused ? "paused — scroll up to resume" : "newest first"}
            </span>
          </div>
        </div>
        {latestMood && (
          <span className="pill" style={{ color: moodColor(latestMood) }}>
            {latestMood}
          </span>
        )}
      </div>
      <div className="relative min-h-0">
        {pending > 0 && (
          <button
            type="button"
            onClick={resume}
            className="btn absolute left-1/2 top-2 z-10 -translate-x-1/2"
            style={{ background: "var(--coral)", color: "#fff", fontSize: 12, padding: "6px 14px" }}
          >
            {pending} new — back to top
          </button>
        )}
        <div
          ref={scrollRef}
          onScroll={onScroll}
          data-testid="feed-scroll"
          className="flex max-h-[62svh] flex-col gap-3 overflow-y-auto overscroll-contain px-4 py-3.5 lg:max-h-[30rem]"
        >
          {shown.length === 0 ? (
            <div
              data-testid="feed-empty"
              className="flex flex-col items-center justify-center gap-3 px-4 py-12 text-center"
            >
              {linkDown ? (
                <>
                  <p className="wordmark text-xl">DATA LINK DOWN</p>
                  <p className="max-w-xs text-[0.8rem] leading-relaxed text-dim">
                    No transmissions available — the telemetry database is unreachable. Nothing is
                    being replayed in its place.
                  </p>
                </>
              ) : (
                <>
                  <p
                    className="wordmark text-xl"
                    style={{ background: "var(--sand)", color: "var(--ink)" }}
                  >
                    ALL QUIET
                  </p>
                  <p className="max-w-xs text-[0.8rem] leading-relaxed text-dim">
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
