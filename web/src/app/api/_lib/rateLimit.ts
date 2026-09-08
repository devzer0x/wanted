// In-process, per-instance, fixed-window rate limiter.
//
// DELIBERATE CHOICE, documented per the task brief's §18 instruction to "choose deliberately and
// say which you chose and why":
//
// A DB-backed counter is the right answer for Vercel, which runs multiple instances — an
// in-process Map is only a soft speed bump there, since a client load-balanced across instances
// effectively gets a fresh budget per instance. But a durable limiter needs its own table (e.g.
// `api_rate_limits`), and:
//   - this workstream owns only `web/src/app/api/` — a new table is a migration, which lives in
//     `infra/supabase/migrations/**`, outside this directory;
//   - the task brief names exactly five SQL entry points this workstream may call
//     (`enter_prediction`, `lock_due_predictions`, `settle_due_predictions`, `leaderboard`,
//     `prediction_distribution`) and none of them is a rate-limit counter.
// So a DB-backed limiter is the correct target but not buildable from inside this directory today;
// the exact schema change needed is called out in the top-level report for this task, addressed to
// whichever workstream owns infra/supabase/migrations.
//
// Until that lands, this in-process limiter is what's achievable here. It still does real work:
// it slows a single dev/preview instance and a low-traffic early deployment, and every write this
// app makes underneath it is independently, atomically enforced in Postgres regardless of this
// limiter (nonce single-use + expiry, `UNIQUE(prediction_id, wallet)` on entries, the partial
// unique index on in-flight reward_claims) — so this limiter is defense-in-depth, not the sole
// guard against abuse.

interface Bucket {
  count: number;
  resetAt: number;
}

const buckets = new Map<string, Bucket>();
let lastPrune = Date.now();

function pruneIfDue(now: number): void {
  if (now - lastPrune < 60_000) return;
  lastPrune = now;
  for (const [key, bucket] of buckets) {
    if (bucket.resetAt <= now) buckets.delete(key);
  }
}

export interface RateLimitResult {
  allowed: boolean;
  /** Milliseconds until the caller may retry; 0 when allowed. */
  retryAfterMs: number;
}

export interface RateLimitOptions {
  /** Window length in milliseconds. */
  windowMs: number;
  /** Max requests allowed per window per key. */
  max: number;
}

export function rateLimit(key: string, opts: RateLimitOptions): RateLimitResult {
  const now = Date.now();
  pruneIfDue(now);
  const existing = buckets.get(key);
  if (!existing || existing.resetAt <= now) {
    buckets.set(key, { count: 1, resetAt: now + opts.windowMs });
    return { allowed: true, retryAfterMs: 0 };
  }
  if (existing.count >= opts.max) {
    return { allowed: false, retryAfterMs: existing.resetAt - now };
  }
  existing.count += 1;
  return { allowed: true, retryAfterMs: 0 };
}

/** Named windows per route, kept in one place so the limits are easy to audit. */
export const RATE_LIMITS = {
  nonce: { windowMs: 60_000, max: 10 },
  verify: { windowMs: 60_000, max: 10 },
  enter: { windowMs: 60_000, max: 30 },
  claim: { windowMs: 60_000, max: 5 },
} as const;
