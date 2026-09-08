"use client";

import { useEffect, useState } from "react";
import { apiErrorMessage } from "@/components/apiError";
import type { LeaderboardRow, LeaderboardWindow } from "@/lib/prediction/types";

export type LeaderboardFetch =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; rows: LeaderboardRow[] };

interface LeaderboardResponse {
  window: LeaderboardWindow;
  rows: LeaderboardRow[];
}

export function useLeaderboard(window: LeaderboardWindow): LeaderboardFetch {
  const [fetched, setFetched] = useState<LeaderboardFetch>({ state: "loading" });

  useEffect(() => {
    let cancelled = false;
    setFetched({ state: "loading" });
    (async () => {
      try {
        const res = await fetch(`/api/leaderboard?window=${window}`, {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (cancelled) return;
        if (!res.ok) {
          setFetched({ state: "error", message: await apiErrorMessage(res, "Couldn't load the leaderboard") });
          return;
        }
        const body = (await res.json()) as LeaderboardResponse;
        if (!Array.isArray(body?.rows)) {
          setFetched({ state: "error", message: "The leaderboard service returned something unexpected." });
          return;
        }
        setFetched({ state: "ready", rows: body.rows });
      } catch {
        if (!cancelled) setFetched({ state: "error", message: "Couldn't reach the leaderboard service." });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [window]);

  return fetched;
}
