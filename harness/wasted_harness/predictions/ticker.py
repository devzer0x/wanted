"""Drives the prediction lifecycle: `open -> locked -> resolving -> settled|void`.

docs/CONTRACTS-PREDICTIONS.md §4 "What actually drives the lifecycle": a
prediction only advances when something calls the two Postgres RPCs
`public.lock_due_predictions()` and `public.settle_due_predictions_serialized()`.
The harness is the contract's named PRIMARY driver — it already runs a 2-4 Hz loop
on the game machine and already holds the service-role key, and this
package's own windows run as short as 30s, shorter than a Vercel cron's
1-minute (Pro) / 1-day (Hobby) granularity can track. A Vercel cron remains
the BACKSTOP for the one case this ticker cannot cover (the harness itself
dying) — nothing here duplicates or replaces it.

Both RPCs are verified, not guessed (CLAUDE.md rule 6): read directly from
`infra/supabase/migrations/20260908120000_predictions.sql`, and the settlement
wrapper from `20260914000001_settlement_guards.sql` — both parameterless,
`security definer`, `returns integer`, and both granted to `service_role`. The
contract states outright that "a double tick from both [drivers] at once is
harmless" (idempotent by construction), so this module's only real job is
cadence and never taking the main loop down with it — there is nothing to
retry-with-backoff or queue: an RPC call is a side effect, not a row, and a
failed tick is simply tried again next tick.

Reuses the SAME `SupabaseWriter`/Supabase client the rest of the package
already writes through (CLAUDE.md rule 1 / "reuse this writer, do not stand
up a second client") — see the same `writer._get_client()` reuse rationale in
`writer.py`'s module docstring; `SupabaseWriter` has no public "call an RPC"
method, so this reaches the same private, already-established seam.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from wasted_harness.events import SupabaseWriter
from wasted_harness.logsetup import get_logger

log = get_logger("wasted.predictions.ticker")

LOCK_FN = "lock_due_predictions"
#: NOT the bare `settle_due_predictions()`. `20260914000001_settlement_guards.sql:89`
#: revokes EXECUTE on that one from `service_role`; the only way in is the wrapper at
#: :66-86, which takes `pg_try_advisory_xact_lock(7741300101)` so two drivers cannot
#: each spend the full daily cap (FM-10). Calling the bare function raised
#: `42501 permission denied` on every tick, swallowed by `_call_one`'s broad `except`
#: and reported as `settled: None` — so the harness locked predictions and then never
#: settled one, leaving the 1-minute Vercel cron as the sole driver and silently
#: breaking this module's own 30s windows.
SETTLE_FN = "settle_due_predictions_serialized"

#: The coordinator's own number: "5 s is fine — they are cheap and idempotent".
DEFAULT_INTERVAL_S = 5.0


@dataclass
class PredictionTicker:
    """Call `maybe_tick(session_id)` once per main-loop iteration (2-4 Hz is
    fine — it self-paces to `interval_s` on the injected clock, so calling it
    far more often than it actually fires costs nothing).

    Never raises: every RPC call is wrapped broadly, matching the writer's own
    failure posture (log, carry on) — see the module docstring. `session_id`
    (not a bare bool) so the caller does not have to compute "is a session
    live" twice; it matches `PredictionGenerator.generate`'s own signature.
    """

    writer: SupabaseWriter
    interval_s: float = DEFAULT_INTERVAL_S
    clock: Callable[[], float] = time.monotonic

    _last_tick_at: float = field(default=float("-inf"), init=False, repr=False)

    def maybe_tick(self, session_id: str | None) -> tuple[int | None, int | None] | None:
        """Returns `(locked, settled)` when a tick actually ran — either entry
        is `None`, not `0`, when that particular RPC call failed (a real
        failure and "genuinely nothing was due" must stay distinguishable in
        the log) — or the whole call is `None` when this was a no-op (no live
        session, not configured, or the cadence has not elapsed)."""
        if session_id is None:
            return None
        if not self.writer.configured:
            return None  # nothing to call without a real Supabase connection
        now = self.clock()
        if now - self._last_tick_at < self.interval_s:
            return None
        self._last_tick_at = now
        return self._call_both()

    def _call_both(self) -> tuple[int | None, int | None]:
        locked = self._call_one(LOCK_FN)
        settled = self._call_one(SETTLE_FN)
        log.info(
            "predictions lifecycle ticked",
            extra={"kv": {"locked": locked, "settled": settled}},
        )
        return locked, settled

    def _call_one(self, fn: str) -> int | None:
        try:
            # Reaches SupabaseWriter's own client builder directly — see the
            # module docstring for why (no public "call an RPC" method exists).
            client = self.writer._get_client()
            resp = client.rpc(fn, {}).execute()
            return int(resp.data) if resp.data is not None else 0
        except Exception as exc:  # broad by design: never kill the main loop
            log.warning(
                "predictions lifecycle RPC failed",
                extra={"kv": {"fn": fn, "error": f"{type(exc).__name__}: {exc}"[:200]}},
            )
            return None
