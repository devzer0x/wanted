"use client";

import { useCallback, useEffect, useState } from "react";
import { apiErrorMessage, enterConflictMessage } from "@/components/apiError";
import { pctOf } from "@/components/predict/distribution";
import { outcomeFill } from "@/components/predict/outcomeStyle";
import { PredictionCountdown } from "@/components/predict/PredictionCountdown";
import { useWallet } from "@/components/wallet/WalletProvider";
import type { MyEntry, Prediction, PredictionDistribution } from "@/lib/prediction/types";

/**
 * Sticky mobile action bar: a viewer scrolled past the stream into THOUGHTS or RESULTS can still
 * answer the live prediction without scrolling back up. Renders only while there is something
 * real to act on — an open prediction the viewer hasn't already entered — never a placeholder
 * bar with nothing behind it.
 */
export function MobilePredictBar({
  prediction,
  distribution,
  mine,
  onEntered,
}: {
  prediction: Prediction | undefined;
  distribution: PredictionDistribution | undefined;
  mine: MyEntry | undefined;
  onEntered: () => void;
}) {
  const wallet = useWallet();
  const [voting, setVoting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Ported from LivePredictionCard: `status` is only as fresh as the last poll (8 s), so without
  // this the outcome buttons stayed tappable after `locks_at` until the next poll landed — every
  // such tap was correctly refused by the server (409), but the bar was offering something it
  // could not deliver. The server remains the only authority on the lock.
  const [lockPassed, setLockPassed] = useState(false);
  const locksAt = prediction?.locks_at;
  useEffect(() => {
    // A new round (a new locks_at) starts clean: the bar stays mounted between rounds, so a
    // "just locked" from the last one would otherwise sit under this one's live buttons.
    setError(null);
    if (!locksAt) {
      setLockPassed(false);
      return;
    }
    const ms = new Date(locksAt).getTime() - Date.now();
    const id = setTimeout(() => setLockPassed(true), Math.max(0, Number.isFinite(ms) ? ms : 0));
    return () => {
      clearTimeout(id);
      setLockPassed(false);
    };
  }, [locksAt]);

  const vote = useCallback(
    async (outcomeKey: string) => {
      if (!prediction) return;
      setVoting(outcomeKey);
      setError(null);
      try {
        const res = await fetch(`/api/predictions/${prediction.id}/enter`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ outcome: outcomeKey }),
        });
        if (res.status === 409) setError(await enterConflictMessage(res, "Just locked — a beat too late."));
        else if (!res.ok) setError(await apiErrorMessage(res, "Couldn't submit your pick"));
        onEntered();
      } catch {
        setError("Couldn't reach the prediction service.");
      } finally {
        setVoting(null);
      }
    },
    [prediction, onEntered]
  );

  if (!prediction || prediction.status !== "open") return null;

  const ready = !mine && !lockPassed && wallet.address && wallet.signIn === "signed-in";

  return (
    <div
      data-testid="mobile-predict-bar"
      className="pointer-events-none fixed inset-x-0 bottom-0 z-50 px-3 pt-3 lg:hidden"
      style={{
        paddingBottom: "calc(10px + env(safe-area-inset-bottom, 0px))",
        background: "linear-gradient(180deg, rgba(255,246,229,0) 0%, var(--cream) 30%)",
      }}
    >
      <div
        className="pointer-events-auto mx-auto max-w-[640px] rounded-[18px] border-[3px] border-ink bg-white px-3 py-2.5"
        style={{ boxShadow: "0 6px 0 var(--ink)" }}
      >
        <div className="mb-2 flex items-center justify-between gap-2 text-ink">
          <span className="min-w-0 flex-1 truncate text-[13px] font-black">
            {prediction.question}
          </span>
          <PredictionCountdown target={prediction.locks_at} label="to lock" />
        </div>

        {mine ? (
          <p className="text-xs font-bold text-muted">
            You picked{" "}
            {prediction.outcomes.find((o) => o.key === mine.outcome)?.label ?? mine.outcome}.
            Waiting to lock.
          </p>
        ) : lockPassed ? (
          <p className="text-xs font-bold text-muted" role="status">
            Locked — waiting for the result.
          </p>
        ) : ready ? (
          <div className="flex gap-2">
            {prediction.outcomes.map((outcome, index) => {
              const pct = pctOf(distribution, outcome.key);
              return (
                <button
                  key={outcome.key}
                  type="button"
                  disabled={voting !== null}
                  onClick={() => void vote(outcome.key)}
                  className="flex min-h-[48px] flex-1 items-center justify-center gap-2 rounded-[14px] border-[3px] border-ink px-2.5 font-display text-base text-ink transition-transform active:translate-y-[3px] disabled:opacity-60"
                  style={{
                    background: outcomeFill(index),
                    boxShadow: "0 4px 0 var(--ink)",
                  }}
                >
                  <span className="min-w-0 truncate">{outcome.label}</span>
                  {pct !== null && <span className="flex-none text-[13px] opacity-75">{pct}%</span>}
                </button>
              );
            })}
          </div>
        ) : (
          <button
            type="button"
            onClick={() => {
              if (!wallet.address) wallet.beginConnect();
              else if (wallet.status === "wrong-network") void wallet.switchNetwork();
              else void wallet.requestSignIn();
            }}
            className="btn btn-coral w-full"
            style={{ padding: "10px 16px", fontSize: "14px" }}
          >
            {!wallet.address
              ? "Connect wallet to predict"
              : wallet.status === "wrong-network"
                ? "Switch network to predict"
                : "Sign in to predict"}
          </button>
        )}

        {error && (
          <p className="mt-1.5 text-[11px] font-bold text-coral" role="status">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
