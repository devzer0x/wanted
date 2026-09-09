"use client";

import { useCallback, useEffect, useState } from "react";

import { apiErrorMessage } from "@/components/apiError";
import { formatBaseUnits, hasBaseUnits } from "@/lib/format";
import type { Claim, RewardBalance } from "@/lib/prediction/types";

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
  terms_not_confirmed: "terms haven't been accepted yet",
};

function reasonText(reason: string): string {
  return REASON_TEXT[reason] ?? reason;
}

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
 * balance or invents a reason. `min_claim` and `eligible`/`reasons` are why the claim button can
 * be legitimately absent or disabled — those are real states, not a design fallback.
 */
export function RewardsPanel({ signedIn }: { signedIn: boolean }) {
  const [fetched, setFetched] = useState<Fetch>({ state: "loading" });
  const [claim, setClaim] = useState<
    { state: "idle" } | { state: "submitting" } | { state: "done"; claim: Claim } | { state: "failed"; message: string }
  >({ state: "idle" });

  const load = useCallback(async () => {
    setFetched({ state: "loading" });
    try {
      const res = await fetch("/api/rewards/balance", { credentials: "same-origin" });
      if (res.status === 503) {
        // No reward asset (or no treasury) configured on this deployment — a real, expected state
        // right now (CONTRACTS-PREDICTIONS §1: "$WANTED has no contract address"), not a fault. A
        // "0" balance would be a claim about reality with no correct scale behind it, so this is
        // rendered as unavailable rather than zero.
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
      setFetched({ state: "error", message: "Couldn't reach the rewards service." });
    }
  }, []);

  useEffect(() => {
    if (signedIn) void load();
  }, [signedIn, load]);

  const submitClaim = useCallback(async () => {
    setClaim({ state: "submitting" });
    try {
      const res = await fetch("/api/rewards/claim", { method: "POST", credentials: "same-origin" });
      if (!res.ok) {
        setClaim({ state: "failed", message: await apiErrorMessage(res, "Claim was refused") });
        return;
      }
      const body = (await res.json()) as { claimId: string; status: string };
      setClaim({
        state: "done",
        claim: { id: body.claimId, amount: "0", asset: "", status: body.status as Claim["status"], tx_hash: null, created_at: "", error: null },
      });
      void load();
    } catch {
      setClaim({ state: "failed", message: "Couldn't reach the claim service." });
    }
  }, [load]);

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

      {balance.eligible && !claimableFunded && (
        <p className="text-[11.5px] leading-[1.45] font-bold text-muted">
          Nothing to claim yet — correct predictions credit this balance when they settle.
        </p>
      )}

      {balance.eligible && belowMin && (
        <p className="text-[11.5px] leading-[1.45] font-bold text-muted">
          Below the minimum claim of {formatBaseUnits(balance.min_claim)} {balance.asset}.
        </p>
      )}

      {claim.state === "failed" && <Note tone="bad">{claim.message}</Note>}
      {claim.state === "done" && (
        <p className="text-[11.5px] leading-[1.45] font-bold text-ink">
          Claim submitted — status: {claim.claim.status}.
        </p>
      )}

      <button
        type="button"
        onClick={() => void submitClaim()}
        disabled={!balance.eligible || !claimableFunded || belowMin || claim.state === "submitting"}
        className="btn btn-teal mt-1 w-full"
      >
        {claim.state === "submitting" ? "Submitting…" : "Claim rewards"}
      </button>
    </div>
  );
}
