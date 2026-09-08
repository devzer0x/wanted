"use client";

import { useCallback, useState } from "react";
import { apiErrorMessage } from "@/components/apiError";
import { pctOf } from "@/components/predict/distribution";
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
        if (res.status === 409) setError("Just locked — a beat too late.");
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

  const ready = !mine && wallet.address && wallet.signIn === "signed-in";

  return (
    <div className="predict-bar lg:hidden">
      <div className="panel border-t-2 border-blood px-3 py-2">
        <p className="mb-1.5 truncate font-mono text-[0.7rem] text-bone">{prediction.question}</p>

        {mine ? (
          <p className="text-[0.62rem] text-smoke">
            You picked {prediction.outcomes.find((o) => o.key === mine.outcome)?.label ?? mine.outcome}
            . Waiting to lock.
          </p>
        ) : ready ? (
          <div className="flex gap-1.5">
            {prediction.outcomes.map((o) => (
              <button
                key={o.key}
                type="button"
                disabled={voting !== null}
                onClick={() => void vote(o.key)}
                className="flex-1 border border-ash py-1.5 text-center font-mono text-xs text-bone transition-colors hover:border-blood disabled:opacity-60"
              >
                {o.label}
                {pctOf(distribution, o.key) !== null && (
                  <span className="ml-1 text-smoke">{pctOf(distribution, o.key)}%</span>
                )}
              </button>
            ))}
          </div>
        ) : (
          <button
            type="button"
            onClick={() => {
              if (!wallet.address) void wallet.connect();
              else if (wallet.status === "wrong-network") void wallet.switchNetwork();
              else void wallet.requestSignIn();
            }}
            className="w-full border border-blood py-1.5 text-center font-mono text-xs text-ember"
          >
            {!wallet.address
              ? "Connect wallet to predict"
              : wallet.status === "wrong-network"
                ? "Switch network to predict"
                : "Sign in to predict"}
          </button>
        )}

        {error && <p className="mt-1 text-[0.58rem] text-ember">{error}</p>}
      </div>
    </div>
  );
}
