// Shared prediction-layer types. Frozen by docs/CONTRACTS-PREDICTIONS.md v2.0 §2-§3.
//
// This file is the single shared vocabulary between the API routes, the settlement reader and
// the UI. It is owned by the orchestrator: parallel workstreams import from it and do not edit it.
//
// Amounts are ALWAYS strings of base units (18 decimals for TTWO), never JavaScript numbers.
// A `number` cannot hold 1e18 without loss, and a reward that rounds is a reward that lies.

/** Lifecycle per CONTRACTS-PREDICTIONS §2. `settled` and `void` are terminal. */
export type PredictionStatus = "open" | "locked" | "resolving" | "settled" | "void";

/** Settlement rule kinds the engine understands. An unknown kind VOIDS — never guesses. */
export type TelemetryRuleKind =
  | "event_occurs"
  | "wanted_reaches"
  | "wanted_clears"
  | "wanted_gained"
  | "survives_window"
  | "vehicle_entered"
  | "vehicle_exited"
  | "mission_outcome"
  | "activity_outcome";

export interface TelemetryRule {
  kind: TelemetryRuleKind;
  /** Outcome key credited when the rule's condition holds within the window. */
  outcome_if_true: string;
  /** Outcome key credited when it demonstrably does not hold. */
  outcome_if_false: string;
  /** Rule-specific parameters; shape is validated per-kind at settlement time. */
  params?: Record<string, unknown>;
}

export interface PredictionOutcome {
  key: string;
  label: string;
}

/** A prediction as the browser is allowed to see it. Never includes treasury internals. */
export interface Prediction {
  id: string;
  session_id: string;
  question: string;
  prediction_type: string;
  state_context: Record<string, unknown> | null;
  opened_at: string;
  locks_at: string;
  resolves_at: string;
  outcomes: PredictionOutcome[];
  status: PredictionStatus;
  result: string | null;
  /** Base-unit string. */
  reward_pool: string;
  reward_asset: string;
  entry_count: number;
  correct_count: number;
  is_event: boolean;
  settled_at: string | null;
}

/** Public participation split. Derived from a security-definer view — raw entries stay private. */
export interface PredictionDistribution {
  prediction_id: string;
  /** outcome key -> entry count. */
  counts: Record<string, number>;
  total: number;
}

/** The viewer's own entry, only ever returned for the authenticated wallet. */
export interface MyEntry {
  prediction_id: string;
  outcome: string;
  created_at: string;
  /** null until the prediction settles. */
  correct: boolean | null;
  /** Base-unit string; "0" when nothing was credited (wrong, void, or ineligible). */
  reward: string;
}

export interface LivePredictionsResponse {
  live: Prediction[];
  resolved: Prediction[];
  distributions: Record<string, PredictionDistribution>;
  /** Present only when a wallet session cookie is attached. */
  mine: Record<string, MyEntry>;
  /** False when no session is live — the UI must say the agent is offline, never invent a card. */
  session_live: boolean;
}

export interface RewardBalance {
  asset: string;
  /** Base-unit strings. */
  claimable: string;
  lifetime: string;
  /** Server-side policy verdict; the UI reflects it and never decides it. */
  eligible: boolean;
  reasons: string[];
  /** Below CLAIM_MIN_AMOUNT the claim button must be disabled, with the minimum shown. */
  min_claim: string;
}

export type ClaimStatus = "pending" | "submitted" | "confirmed" | "failed";

export interface Claim {
  id: string;
  amount: string;
  asset: string;
  status: ClaimStatus;
  tx_hash: string | null;
  created_at: string;
  /** Populated on failure so the UI can show a real reason, not a generic error. */
  error: string | null;
}

export interface LeaderboardRow {
  rank: number;
  wallet: string;
  /**
   * Fraction in [0, 1], NOT a percentage — `public.leaderboard()` returns `correct / total`
   * rounded to 4 places (observed: 0.6667 for 2 correct of 3). Multiply by 100 to display.
   */
  accuracy: number;
  correct: number;
  total: number;
  current_streak: number;
  best_streak: number;
  /** Base-unit string. */
  earned: string;
}

export type LeaderboardWindow = "today" | "week" | "all";
