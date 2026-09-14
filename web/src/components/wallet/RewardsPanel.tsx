"use client";

import { useCallback, useEffect, useState } from "react";

import { apiErrorMessage } from "@/components/apiError";
import { formatBaseUnits, hasBaseUnits } from "@/lib/format";
import type { Claim, ClaimStatus, ClaimsResponse, RewardBalance } from "@/lib/prediction/types";

type Fetch =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "unavailable"; message: string }
  | { state: "ready"; balance: RewardBalance };

// Plain-language readings of `src/lib/policy/index.ts`'s reason codes — real reasons from the
// server, just not shown as raw snake_case. An unrecognized code (a future policy addition)
// still renders, verbatim, rather than being swallowed.
const REASON_TEXT: Record<string, string> = {
  rewards_disabled: "rewards are turned off right now",
  no_wallet: "no wallet is attached to this session",
  region_unconfirmed: "this wallet's region couldn't be confirmed",
  region_blocked: "this wallet's region can't receive rewards",
  terms_not_confirmed: "the current rules (wanted.money/rules) haven't been accepted — sign out and sign in again to accept them",
  reward_limits_misconfigured: "claims are paused while the operator fixes a configuration problem",
  wallet_blocked: "this wallet has been excluded from rewards under the Fair play rules",
};

function reasonText(reason: string): string {
  return REASON_TEXT[reason] ?? reason;
}

// One word per outbox state (CONTRACTS-PREDICTIONS §10.2). These describe what the SERVER says the
// claim is — the panel never infers a state, and never says "paid" without a confirmed row.
const STATUS_TEXT: Record<ClaimStatus, string> = {
  queued: "queued",
  signed: "sending",
  broadcast: "sent",
  confirmed: "paid",
  failed: "returned to balance",
  needs_review: "held for a check",
};

// A queued claim is paid by the payout worker on its next cron tick (about a minute), never on this
// page's schedule, so polling faster than this buys nothing. Polling runs only while a claim is in
// flight and stops the moment the server says it is not.
const POLL_MS = 15_000;

/** A small sticker that carries a sentence the server said — never a sentence we made up. */
function Note({ tone, children }: { tone: "quiet" | "warn" | "bad"; children: React.ReactNode }) {
  const skin =
    tone === "bad"
      ? "border-ink bg-coral text-white"
      : tone === "warn"
        ? "border-ink bg-yellow-pale text-ink"
        : "border-ink bg-sand text-ink";
  return (
    <p className={`rounded-[14px] border-[3px] px-3 py-2.5 text-[11.5px] leading-[1.45] font-bold ${skin}`}>
      {children}
    </p>
  );
}

/**
 * Balance + claim, scoped to the signed-in wallet. Every number here is server-computed
 * (CONTRACTS-PREDICTIONS §6) — this panel only formats and gates the button; it never guesses a
 * balance or invents a reason. `min_claim`, `eligible`/`reasons` and `in_flight_claim` are why the
 * claim button can be legitimately absent or disabled — those are real states, not a design fallback.
 */
export function RewardsPanel({ signedIn }: { signedIn: boolean }) {
  const [fetched, setFetched] = useState<Fetch>({ state: "loading" });
  const [claims, setClaims] = useState<Claim[] | null>(null);
  const [claim, setClaim] = useState<
    { state: "idle" } | { state: "submitting" } | { state: "queued" } | { state: "failed"; message: string }
  >({ state: "idle" });

  // `quiet` refreshes keep the current balance on screen instead of flashing the skeleton — used by
  // the in-flight poll and after a claim, where the numbers change but the panel should not blink.
  const load = useCallback(async (quiet = false) => {
    if (!quiet) setFetched({ state: "loading" });
    try {
      const res = await fetch("/api/rewards/balance", { credentials: "same-origin" });
      if (res.status === 503) {
        // No reward asset (or no treasury) configured on this deployment — a real, expected state,
        // not a fault. A "0" balance would be a claim about reality with no correct scale behind it,
        // so this is rendered as unavailable rather than zero.
        setFetched({ state: "unavailable", message: await apiErrorMessage(res, "Rewards aren't configured on this deployment yet") });
        return;
      }
      if (!res.ok) {
        setFetched({ state: "error", message: await apiErrorMessage(res, "Couldn't load your balance") });
        return;
      }
      const balance = (await res.json()) as RewardBalance;
      setFetched({ state: "ready", balance });
    } catch {
      if (!quiet) setFetched({ state: "error", message: "Couldn't reach the rewards service." });
    }
  }, []);

  // The claim history is supplementary: if it cannot load, the balance above still tells the truth,
  // so a failure here keeps the last list rather than replacing the panel with an error.
  const loadClaims = useCallback(async () => {
    try {
      const res = await fetch("/api/rewards/claims", { credentials: "same-origin" });
      if (!res.ok) return;
      const body = (await res.json()) as ClaimsResponse;
      setClaims(body.claims);
    } catch {
      // keep the last list
    }
  }, []);

  useEffect(() => {
    if (!signedIn) return;
    void load();
    void loadClaims();
  }, [signedIn, load, loadClaims]);

  const inFlight = fetched.state === "ready" ? fetched.balance.in_flight_claim : null;

  useEffect(() => {
    if (!signedIn || !inFlight) return;
    const timer = setInterval(() => {
      void load(true);
      void loadClaims();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [signedIn, inFlight, load, loadClaims]);

  const submitClaim = useCallback(async () => {
    setClaim({ state: "submitting" });
    try {
      const res = await fetch("/api/rewards/claim", { method: "POST", credentials: "same-origin" });
      if (!res.ok) {
        setClaim({ state: "failed", message: await apiErrorMessage(res, "Claim was refused") });
        return;
      }
      // 202: the claim is queued, not paid. The worker signs it on its next tick; the balance and
      // history reloads below pick up in_flight_claim, which starts the poll.
      setClaim({ state: "queued" });
      void load(true);
      void loadClaims();
    } catch {
      setClaim({ state: "failed", message: "Couldn't reach the claim service." });
    }
  }, [load, loadClaims]);

  if (!signedIn) {
    return (
      <p className="mt-3.5 text-[11.5px] leading-[1.45] font-bold text-muted">
        Sign in with this wallet to see your prediction balance.
      </p>
    );
  }

  if (fetched.state === "loading") {
    return (
      <p className="skeleton-pulse mt-3.5 rounded-[14px] px-3 py-2.5 text-[11.5px] font-bold text-muted">
        Checking your balance…
      </p>
    );
  }

  // A 503 is "we cannot tell you", not "you have nothing". It never becomes a zero.
  if (fetched.state === "unavailable") {
    return (
      <div className="mt-3.5">
        <Note tone="quiet">{fetched.message}</Note>
      </div>
    );
  }

  if (fetched.state === "error") {
    return (
      <div className="mt-3.5">
        <Note tone="bad">{fetched.message}</Note>
      </div>
    );
  }

  const { balance } = fetched;
  const claimableFunded = hasBaseUnits(balance.claimable);
  const claimableAmount = claimableFunded ? BigInt(balance.claimable) : BigInt(0);
  const minAmount = hasBaseUnits(balance.min_claim) ? BigInt(balance.min_claim) : BigInt(0);
  const belowMin = claimableFunded && claimableAmount < minAmount;

  return (
    <div className="mt-3.5 flex flex-col gap-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="ticker text-[10px] text-muted">Claimable</span>
        {claimableFunded ? (
          <span className="font-display text-[26px] leading-none">
            {formatBaseUnits(balance.claimable)}{" "}
            <span className="text-[14px] text-muted">{balance.asset}</span>
          </span>
        ) : (
          <span className="font-display text-[16px] leading-none text-muted">nothing yet</span>
        )}
      </div>

      <div className="flex items-baseline justify-between gap-2">
        <span className="ticker text-[10px] text-muted">Lifetime earned</span>
        <span className="text-[13px] font-black">
          {hasBaseUnits(balance.lifetime) ? `${formatBaseUnits(balance.lifetime)} ${balance.asset}` : "—"}
        </span>
      </div>

      {!balance.eligible && balance.reasons.length > 0 && (
        <Note tone="warn">
          Rewards are not available to this wallet: {balance.reasons.map(reasonText).join("; ")}.
        </Note>
      )}

      {balance.eligible && !claimableFunded && !inFlight && (
        <p className="text-[11.5px] leading-[1.45] font-bold text-muted">
          Nothing to claim yet — correct predictions credit this balance when they settle.
        </p>
      )}

      {balance.eligible && belowMin && !inFlight && (
        <p className="text-[11.5px] leading-[1.45] font-bold text-muted">
          Below the minimum claim of {formatBaseUnits(balance.min_claim)} {balance.asset}.
        </p>
      )}

      {claim.state === "failed" && <Note tone="bad">{claim.message}</Note>}

      {inFlight ? (
        <Note tone="quiet">
          {inFlight.status === "needs_review"
            ? "Your claim is held for a manual check. Nothing is lost — it is either paid or returned to your balance."
            : `Your claim is ${STATUS_TEXT[inFlight.status]} and pays out within a couple of minutes. Claim comes back when it lands.`}
        </Note>
      ) : (
        claim.state === "queued" && (
          <p className="text-[11.5px] leading-[1.45] font-bold text-ink">Claim queued — it pays out within a couple of minutes.</p>
        )
      )}

      <button
        type="button"
        onClick={() => void submitClaim()}
        disabled={!balance.eligible || !claimableFunded || belowMin || inFlight !== null || claim.state === "submitting"}
        className="btn btn-teal mt-1 w-full"
      >
        {claim.state === "submitting" ? "Submitting…" : "Claim rewards"}
      </button>

      {claims && claims.length > 0 && (
        <div className="flex flex-col gap-1.5 pt-1">
          <span className="ticker text-[10px] text-muted">Recent claims</span>
          {claims.slice(0, 3).map((c) => (
            <div key={c.id} className="flex items-baseline justify-between gap-2 text-[11.5px] font-bold">
              <span>
                {formatBaseUnits(c.amount)} <span className="text-muted">{c.asset}</span>
              </span>
              <span className={c.status === "confirmed" ? "text-ink" : "text-muted"}>
                {STATUS_TEXT[c.status]}
                {c.explorer_url ? (
                  <>
                    {" · "}
                    <a href={c.explorer_url} target="_blank" rel="noreferrer" className="underline">
                      receipt
                    </a>
                  </>
                ) : null}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
