"use client";

import { useState } from "react";
import { LeaderboardTable } from "@/components/leaderboard/LeaderboardTable";
import { useLeaderboard } from "@/components/leaderboard/useLeaderboard";
import type { LeaderboardWindow } from "@/lib/prediction/types";

const TABS: { key: LeaderboardWindow; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "week", label: "Week" },
  { key: "all", label: "All time" },
];

export function LeaderboardView() {
  const [window_, setWindow] = useState<LeaderboardWindow>("today");
  const fetched = useLeaderboard(window_);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-1.5" role="tablist" aria-label="Leaderboard window">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={window_ === tab.key}
            onClick={() => setWindow(tab.key)}
            className={`ticker border px-3 py-1.5 text-[0.62rem] transition-colors ${
              window_ === tab.key
                ? "border-blood text-ember"
                : "border-ash text-smoke hover:border-bone hover:text-bone"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {fetched.state === "loading" && (
        <div className="panel skeleton-pulse h-64" aria-hidden="true" />
      )}
      {fetched.state === "error" && (
        <div className="panel px-4 py-12 text-center" data-testid="leaderboard-error">
          <p className="wordmark text-2xl text-ember" data-text="DATA LINK DOWN">
            DATA LINK DOWN
          </p>
          <p className="mt-2 text-xs text-smoke">{fetched.message}</p>
        </div>
      )}
      {fetched.state === "ready" && <LeaderboardTable rows={fetched.rows} />}
    </div>
  );
}
