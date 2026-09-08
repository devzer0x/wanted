"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiErrorMessage } from "@/components/apiError";
import type { LivePredictionsResponse } from "@/lib/prediction/types";

export type PredictionsFetch =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; data: LivePredictionsResponse };

// No realtime publication covers the prediction tables (CONTRACTS-PREDICTIONS §2 lists only
// decisions/events/stats for that); a short poll is the honest way to stay current, same pattern
// as the live dashboard's own fallback polls.
const POLL_MS = 8_000;

export function usePredictions(): { fetched: PredictionsFetch; refresh: () => void } {
  const [fetched, setFetched] = useState<PredictionsFetch>({ state: "loading" });
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const res = await fetch("/api/predictions/live", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!res.ok) {
        setFetched({ state: "error", message: await apiErrorMessage(res, "Couldn't load predictions") });
        return;
      }
      const data = (await res.json()) as LivePredictionsResponse;
      setFetched({ state: "ready", data });
    } catch {
      setFetched({ state: "error", message: "Couldn't reach the prediction service." });
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    window.addEventListener("online", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
      window.removeEventListener("online", onVisible);
    };
  }, [load]);

  return { fetched, refresh: () => void load() };
}
