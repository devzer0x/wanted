"use client";

import { useCallback, useState } from "react";
import { apiErrorMessage } from "@/components/apiError";
import { OutcomeBar } from "@/components/predict/OutcomeBar";
import { PredictionCountdown } from "@/components/predict/PredictionCountdown";
import { pctOf } from "@/components/predict/distribution";
import { useWallet } from "@/components/wallet/WalletProvider";
import { formatBaseUnits, hasBaseUnits } from "@/lib/format";
import type { MyEntry, Prediction, PredictionDistribution } from "@/lib/prediction/types";

const STATUS_LABEL: Record<Prediction["status"], string> = {
  open: "open",
  locked: "locked",
  resolving: "resolving",
  settled: "settled",
  void: "void",
};

export function LivePredictionCard({
  prediction,
  distribution,
  mine,
  onEntered,
}: {
  prediction: Prediction;
  distribution: PredictionDistribution | undefined;
  mine: MyEntry | undefined;
  onEntered: () => void;
}) {
  const wallet = useWallet();
  const [voting, setVoting] = useState<string | null>(null);
  const [voteError, setVoteError] = useState<string | null>(null);

  const canVote = prediction.status === "open" && !mine;
  const readyToVote = canVote && wallet.address && wallet.signIn === "signed-in";

  const vote = useCallback(
    async (outcomeKey: string) => {
      setVoting(outcomeKey);
      setVoteError(null);
      try {
        const res = await fetch(`/api/predictions/${prediction.id}/enter`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ outcome: outcomeKey }),
        });
        if (res.status === 409) {
          setVoteError("Predictions just locked — a beat too late.");
        } else if (!res.ok) {
          setVoteError(await apiErrorMessage(res, "Couldn't submit your pick"));
        }
        onEntered();
      } catch {
        setVoteError("Couldn't reach the prediction service.");
      } finally {
        setVoting(null);
      }
    },
    [prediction.id, onEntered]
  );

  const settled = prediction.status === "settled";
  const void_ = prediction.status === "void";
  const poolFunded = hasBaseUnits(prediction.reward_pool) && Boolean(prediction.reward_asset);

  return (
    <section className="panel flex flex-col p-3 sm:p-4" aria-label="Live prediction" data-testid="live-prediction">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="panel-title">
          {prediction.is_event ? "Wanted event" : "Live prediction"}
        </h2>
        <span
          className={`ticker border px-1.5 py-0.5 text-[0.55rem] ${
            prediction.status === "open"
              ? "border-blood text-ember"
              : settled
                ? "border-bone text-bone"
                : void_
                  ? "border-hazard text-hazard"
                  : "border-ash text-smoke"
          }`}
          data-testid="prediction-status"
        >
          {STATUS_LABEL[prediction.status]}
        </span>
      </div>

      <p className="mb-3 font-display text-lg leading-tight text-bone [overflow-wrap:anywhere]">
        {prediction.question}
      </p>

      <div className="flex flex-col gap-1.5">
        {prediction.outcomes.map((outcome) => (
          <OutcomeBar
            key={outcome.key}
            label={outcome.label}
            pct={pctOf(distribution, outcome.key)}
            active={mine?.outcome === outcome.key}
            won={settled ? outcome.key === prediction.result : undefined}
            busy={voting === outcome.key}
            onClick={readyToVote ? () => void vote(outcome.key) : undefined}
          />
        ))}
      </div>

      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[0.62rem] text-smoke">
        <span>
          {distribution && distribution.total > 0
            ? `${distribution.total} predicting`
            : "no predictions yet"}
        </span>
        {prediction.status === "open" && (
          <PredictionCountdown target={prediction.locks_at} label="to lock" />
        )}
        {(prediction.status === "locked" || prediction.status === "resolving") && (
          <PredictionCountdown target={prediction.resolves_at} label="to resolve" />
        )}
      </div>

      <p className="mt-1 text-[0.6rem] text-smoke">
        {poolFunded
          ? `Pool: ${formatBaseUnits(prediction.reward_pool)} ${prediction.reward_asset}, split evenly among correct picks.`
          : "No reward pool funded for this one yet."}
      </p>

      {void_ && (
        <p className="mt-2 border-l-2 border-hazard pl-2 text-[0.62rem] leading-relaxed text-hazard">
          Voided — the window didn&apos;t produce a clean signal, so nothing settles and nobody is
          charged or paid.
        </p>
      )}

      {mine && (
        <p className="mt-2 border-l-2 border-ash pl-2 text-[0.62rem] leading-relaxed text-smoke">
          Your pick: {prediction.outcomes.find((o) => o.key === mine.outcome)?.label ?? mine.outcome}.
          {settled &&
            (mine.correct
              ? ` Correct — ${hasBaseUnits(mine.reward) ? `${formatBaseUnits(mine.reward)} ${prediction.reward_asset} credited.` : "no reward was credited."}`
              : " Not this time.")}
        </p>
      )}

      {canVote && !readyToVote && (
        <div className="mt-2 flex items-center gap-2 text-[0.62rem] text-smoke">
          {!wallet.address ? (
            <>
              <span>Connect a wallet to predict.</span>
              <button
                type="button"
                onClick={() => void wallet.connect()}
                className="ticker border border-ash px-2 py-1 text-[0.58rem] text-bone hover:border-blood hover:text-ember"
              >
                Connect
              </button>
            </>
          ) : wallet.status === "wrong-network" ? (
            <>
              <span>Wrong network.</span>
              <button
                type="button"
                onClick={() => void wallet.switchNetwork()}
                className="ticker border border-hazard px-2 py-1 text-[0.58rem] text-hazard hover:bg-hazard hover:text-void"
              >
                Switch to Robinhood Chain
              </button>
            </>
          ) : (
            <>
              <span>Sign in to predict.</span>
              <button
                type="button"
                onClick={() => void wallet.requestSignIn()}
                disabled={wallet.signIn === "signing-in"}
                className="ticker border border-blood px-2 py-1 text-[0.58rem] text-ember hover:bg-blood hover:text-bone disabled:opacity-60"
              >
                {wallet.signIn === "signing-in" ? "Check your wallet…" : "Sign in"}
              </button>
            </>
          )}
        </div>
      )}

      {voteError && <p className="mt-2 text-[0.62rem] text-ember">{voteError}</p>}
    </section>
  );
}
