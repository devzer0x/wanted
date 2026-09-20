"use client";

import { useCallback, useEffect, useState } from "react";
import { apiErrorMessage } from "@/components/apiError";
import { OutcomeBar } from "@/components/predict/OutcomeBar";
import { PredictionCountdown } from "@/components/predict/PredictionCountdown";
import { pctOf } from "@/components/predict/distribution";
import { measuredOdds, settlementRule } from "@/components/predict/rule";
import { useWallet } from "@/components/wallet/WalletProvider";
import { formatBaseUnits, hasBaseUnits } from "@/lib/format";
import type { MyEntry, Prediction, PredictionDistribution } from "@/lib/prediction/types";

/** The header strip: colour and words both come from the real status, nothing else. */
function headerFor(prediction: Prediction): { tone: string; label: string } {
  switch (prediction.status) {
    case "open":
      return {
        tone: "bg-yellow text-ink",
        label: prediction.is_event ? "Wanted event" : "Live prediction",
      };
    case "locked":
      return { tone: "bg-ink text-cream", label: "Locked · resolving" };
    case "resolving":
      return { tone: "bg-ink text-cream", label: "Resolving" };
    case "settled":
      return { tone: "bg-teal text-ink", label: "Settled" };
    case "void":
      return { tone: "bg-yellow text-ink", label: "Void" };
  }
}

const PILL_BG = { background: "var(--yellow-pale)" } as const;
// `.btn` sets its own padding/size outside any cascade layer, so a Tailwind utility cannot trim it.
const SMALL_BTN = { padding: "8px 16px", fontSize: "13px" } as const;

export function LivePredictionCard({
  prediction,
  distribution,
  mine,
  onEntered,
  layout = "sidebar",
}: {
  prediction: Prediction;
  distribution: PredictionDistribution | undefined;
  mine: MyEntry | undefined;
  onEntered: () => void;
  /** "page" spreads the outcome rows across two columns on the wide /predict layout. */
  layout?: "sidebar" | "page";
}) {
  const wallet = useWallet();
  const [voting, setVoting] = useState<string | null>(null);
  const [voteError, setVoteError] = useState<string | null>(null);

  // `status` is only as fresh as the last poll (8 s), and the row itself only flips to `locked`
  // when a lifecycle tick runs. Without this the outcome bars stayed clickable after the countdown
  // above them read 0:00 — every such click is refused by the server (409), correctly, but the card
  // was offering something it could not deliver. The server remains the only authority on the lock.
  const [lockPassed, setLockPassed] = useState(false);
  useEffect(() => {
    const ms = new Date(prediction.locks_at).getTime() - Date.now();
    const id = setTimeout(() => setLockPassed(true), Math.max(0, Number.isFinite(ms) ? ms : 0));
    return () => {
      clearTimeout(id);
      setLockPassed(false);
    };
  }, [prediction.locks_at]);

  const canVote = prediction.status === "open" && !lockPassed && !mine;
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
  const { tone, label: headLabel } = headerFor(prediction);
  const rule = settlementRule(prediction);
  const odds = measuredOdds(prediction);

  return (
    <section
      className="panel flex flex-col overflow-hidden lg:h-full"
      style={{ boxShadow: "0 7px 0 var(--ink)" }}
      aria-label="Live prediction"
      data-testid="live-prediction"
    >
      <div
        className={`flex items-center justify-between gap-2.5 border-b-[3px] border-ink px-4 py-3 ${tone}`}
        data-testid="prediction-status"
        data-status={prediction.status}
      >
        <h2 className="panel-title min-w-0 truncate text-[15px] tracking-[0.06em] sm:text-base">
          {headLabel}
        </h2>
        {prediction.status === "open" && (
          <PredictionCountdown
            target={prediction.locks_at}
            from={prediction.opened_at}
            label="to lock"
          />
        )}
        {(prediction.status === "locked" || prediction.status === "resolving") && (
          <PredictionCountdown
            target={prediction.resolves_at}
            from={prediction.locks_at}
            label="to resolve"
          />
        )}
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        <p className="font-display text-[22px] leading-[1.1] text-ink [overflow-wrap:anywhere] sm:text-[26px]">
          {prediction.question}
        </p>

        <div
          className={
            layout === "page" ? "grid gap-3 sm:grid-cols-2" : "flex flex-col gap-2.5"
          }
        >
          {prediction.outcomes.map((outcome, index) => (
            <OutcomeBar
              key={outcome.key}
              index={index}
              label={outcome.label}
              pct={pctOf(distribution, outcome.key)}
              active={mine?.outcome === outcome.key}
              won={settled ? outcome.key === prediction.result : undefined}
              busy={voting === outcome.key}
              onClick={readyToVote ? () => void vote(outcome.key) : undefined}
            />
          ))}
        </div>

        <div className="mt-auto flex flex-wrap items-center gap-2">
          <span className="pill" style={PILL_BG}>
            {distribution && distribution.total > 0
              ? `${distribution.total} predicting`
              : "No predictions yet"}
          </span>
          <span className="pill" style={PILL_BG}>
            {poolFunded
              ? `Pot ${formatBaseUnits(prediction.reward_pool)} ${prediction.reward_asset}`
              : "No pot funded yet"}
          </span>
          {rule && (
            <span className="pill" style={PILL_BG} title={rule.title}>
              {rule.text}
            </span>
          )}
          {odds && (
            <span className="pill" style={PILL_BG} title={odds.title} data-testid="measured-odds">
              {odds.text}
            </span>
          )}
        </div>

        <p className="text-[11px] font-bold leading-snug text-muted">
          Free to play. Settles from game telemetry
          {poolFunded ? ", split evenly among correct picks." : "."}
        </p>

        {void_ && (
          <p
            className="rounded-xl border-2 border-ink px-3 py-2 text-xs font-bold leading-relaxed text-ink"
            style={PILL_BG}
          >
            Voided — the window didn&apos;t produce a clean signal, so nothing settles and nobody is
            charged or paid.
          </p>
        )}

        {mine && (
          <p className="panel-sand rounded-xl border-2 border-ink px-3 py-2 text-xs font-bold leading-relaxed text-ink">
            Your pick:{" "}
            {prediction.outcomes.find((o) => o.key === mine.outcome)?.label ?? mine.outcome}.
            {settled &&
              (mine.correct
                ? ` Correct — ${hasBaseUnits(mine.reward) ? `${formatBaseUnits(mine.reward)} ${prediction.reward_asset} credited.` : "no reward was credited."}`
                : " Not this time.")}
          </p>
        )}

        {canVote && !readyToVote && (
          <div className="flex flex-wrap items-center gap-2 text-xs font-bold text-muted">
            {!wallet.address ? (
              <>
                <span>Connect a wallet to predict.</span>
                <button
                  type="button"
                  onClick={() => wallet.beginConnect()}
                  className="btn btn-coral"
                  style={SMALL_BTN}
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
                  className="btn btn-yellow"
                  style={SMALL_BTN}
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
                  className="btn btn-coral"
                  style={SMALL_BTN}
                >
                  {wallet.signIn === "signing-in" ? "Check your wallet…" : "Sign in"}
                </button>
              </>
            )}
          </div>
        )}

        {voteError && (
          <p className="text-xs font-bold text-coral" role="status">
            {voteError}
          </p>
        )}
      </div>
    </section>
  );
}
