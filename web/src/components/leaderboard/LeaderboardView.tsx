"use client";

import { useState, type ReactNode } from "react";
import { LeaderboardPodium } from "@/components/leaderboard/LeaderboardPodium";
import { LeaderboardTable } from "@/components/leaderboard/LeaderboardTable";
import { useLeaderboard } from "@/components/leaderboard/useLeaderboard";
import type { LeaderboardWindow } from "@/lib/prediction/types";

const TABS: { key: LeaderboardWindow; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "week", label: "Week" },
  { key: "all", label: "All time" },
];

const PANEL_ID = "leaderboard-panel";
const tabId = (key: LeaderboardWindow) => `leaderboard-tab-${key}`;

/** `heading` is the server-rendered <h1> block; the window control sits on the same line as it at
 *  desktop width, which is why the header lives in this client component rather than the page. */
export function LeaderboardView({ heading }: { heading: ReactNode }) {
  const [window_, setWindow] = useState<LeaderboardWindow>("today");
  const fetched = useLeaderboard(window_);

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-4">
        {heading}
        <div
          role="tablist"
          aria-label="Leaderboard window"
          className="inline-flex gap-1 rounded-full border-[3px] border-ink bg-white p-1 shadow-[0_4px_0_var(--ink)]"
        >
          {TABS.map((tab) => {
            const active = window_ === tab.key;
            return (
              <button
                key={tab.key}
                id={tabId(tab.key)}
                type="button"
                role="tab"
                aria-selected={active}
                aria-controls={PANEL_ID}
                onClick={() => setWindow(tab.key)}
                className={`cursor-pointer rounded-full px-3.5 py-1.5 font-display text-[13px] uppercase tracking-[0.04em] transition-colors sm:text-sm ${
                  active ? "bg-ink text-cream" : "text-ink hover:bg-sand"
                }`}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      </header>

      <div
        id={PANEL_ID}
        role="tabpanel"
        tabIndex={0}
        aria-labelledby={tabId(window_)}
        className="flex flex-col gap-4 outline-none focus-visible:outline-[3px] focus-visible:outline-offset-4 focus-visible:outline-coral"
      >
        {fetched.state === "loading" && (
          <div
            className="skeleton-pulse h-64 rounded-[22px]"
            role="status"
            aria-label="Loading the leaderboard"
          />
        )}

        {fetched.state === "error" && (
          <div
            className="panel rounded-[22px] px-5 py-14 text-center shadow-[0_7px_0_var(--ink)]"
            data-testid="leaderboard-error"
          >
            <p className="m-0">
              <span className="panel-title inline-block -rotate-1 rounded-[14px] border-[3px] border-ink bg-coral px-4 py-2 text-[clamp(20px,4vw,28px)] leading-none text-white shadow-[0_5px_0_var(--ink)]">
                Data link down
              </span>
            </p>
            <p className="mx-auto mt-5 max-w-md text-[15px] leading-relaxed text-dim">
              {fetched.message}
            </p>
          </div>
        )}

        {fetched.state === "ready" && (
          <>
            <LeaderboardPodium rows={fetched.rows.slice(0, 3)} />
            <LeaderboardTable rows={fetched.rows} window={window_} />
          </>
        )}
      </div>
    </div>
  );
}
