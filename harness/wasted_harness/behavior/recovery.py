"""Recovery handlers: stuck, flipped, stranded, game restart, bridge-down, API backoff.

Each handler is small state + a decision: what to do NOW, without a model call.
They are the harness's spinal reflexes — they keep the show alive when the
brain, the bridge, or the network is having a moment.

The failure modes the real machine implies, and who covers them:

===========================  =================================================
game crashed / relaunching   BridgeDownTracker (connection refused → backoff →
                             one `bridge_down` event) + GameRestartDetector
                             (tick counter went backwards ⇒ new game process ⇒
                             every stateful observer must be reset before it
                             invents a death or a wanted change)
bridge loaded but online      OnlineSessionActiveError, handled in main
bridge up, game not ticking   BridgeStallTracker (v1.2 transient 503s:
                              not_ready / game_thread_stalled / queue_full, and
                              any code this version does not know) — wait and
                              retry, never a bridge_down event
API rate limit / overload     ApiBackoff with a per-cause floor; the reflex
                             layer drives while the brain is blocked
Supabase down                 events.SupabaseWriter offline queue (locked,
                             capped, flushed on reconnect)
stuck on geometry             StuckDetector → reverse_out → bridge `unstick`
flipped                       flipped_action → exit_vehicle
stranded on foot              StrandedEscalator → widening vehicle search
===========================  =================================================
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from ..bridge_client import (
    BridgeApiError,
    BridgeClient,
    BridgeDownError,
    BridgeTransientError,
    GameState,
)
from ..logsetup import get_logger

log = get_logger("wasted.recovery")


# --- stuck / flipped / stranded ----------------------------------------------


@dataclass
class StuckDetector:
    """Speed ~0 for >20 s while a drive task runs → escalate: reverse_out, then unstick."""

    threshold_s: float = 20.0
    _reverse_tried_at: float | None = None
    clock: Any = time.monotonic

    def check(self, state: GameState) -> str | None:
        """Returns 'reverse_out', 'unstick', or None."""
        v = state.vehicle
        driving = state.last_task.status == "running" and state.last_task.type in (
            "drive_to",
            "wander_drive",
            "flee_police",
        )
        if not (v and driving and v.stopped_for_s > self.threshold_s):
            self._reverse_tried_at = None
            return None
        now = self.clock()
        if self._reverse_tried_at is None or now - self._reverse_tried_at > 45.0:
            self._reverse_tried_at = now
            return "reverse_out"
        # reverse_out already tried recently and we're still parked on geometry:
        # ask the bridge for the logged, contract-limited nudge.
        return "unstick"

    def try_unstick(self, bridge: BridgeClient) -> float | None:
        """Calls /unstick; returns meters moved, or None when it did not happen.

        A 409 ``unstick_conditions_not_met`` is normal and final — the bridge
        self-enforces its own preconditions. A transient 503 (`not_ready`,
        `game_thread_stalled`, `queue_full`, or a code this version has never
        heard of) just means "not now"; the detector will ask again on the next
        tick. Neither is worth killing the loop for, and a three-metre nudge
        never is.
        """
        try:
            result = bridge.unstick()
            return result.distance_m if result.moved else None
        except BridgeTransientError as exc:
            log.info(
                "unstick deferred: bridge not ready",
                extra={"kv": {"error": exc.error, "status": exc.status}},
            )
            return None
        except BridgeApiError as exc:
            if exc.status == 409:
                log.info("unstick refused by bridge", extra={"kv": {"error": exc.error}})
                return None
            log.warning(
                "unstick failed with an unexpected bridge error; skipping the nudge",
                extra={"kv": {"status": exc.status, "error": exc.error}},
            )
            return None


def flipped_action(state: GameState) -> dict[str, Any] | None:
    """Upside-down and not moving → get out (the engine rights nothing for us)."""
    v = state.vehicle
    if v and v.upside_down and v.speed < 0.5:
        return {"type": "exit_vehicle", "params": {}}
    return None


#: Vehicle-search radii, in order, as being stranded drags on. The bridge
#: clamps whatever it considers unreasonable; escalating here just stops the
#: harness from asking the same failing question forever.
STRANDED_RADII_M: tuple[float, ...] = (50.0, 90.0, 140.0)


@dataclass
class StrandedEscalator:
    """On foot with no task: widen the vehicle search, then hand back to the brain.

    Returns an action for the first few attempts, then None — at which point
    the decision is genuinely a creative one (walk somewhere? wait for
    traffic?) and belongs to the model, not to a reflex.
    """

    attempts: int = 0
    _last_attempt_at: float = 0.0
    retry_gap_s: float = 12.0
    clock: Any = time.monotonic

    def reset(self) -> None:
        self.attempts = 0
        self._last_attempt_at = 0.0

    def check(self, state: GameState) -> dict[str, Any] | None:
        if state.player.in_vehicle or state.last_task.status == "running":
            self.reset()
            return None
        now = self.clock()
        if now - self._last_attempt_at < self.retry_gap_s:
            return None
        if self.attempts >= len(STRANDED_RADII_M):
            return None
        radius = STRANDED_RADII_M[self.attempts]
        self._last_attempt_at = now
        self.attempts += 1
        log.info(
            "stranded on foot; widening vehicle search",
            extra={"kv": {"attempt": self.attempts, "radius_m": radius}},
        )
        return {
            "type": "enter_nearest_vehicle",
            "params": {"prefer": "any", "search_radius_m": radius},
        }


# --- game restart -------------------------------------------------------------


@dataclass
class GameRestartDetector:
    """Spots a new game process behind the same bridge URL.

    The bridge's `tick` counter starts over when GTA5.exe restarts (watchdog
    relaunch after a crash). Without this, the perception layer compares the
    first post-restart snapshot against a pre-crash one and emits fabricated
    events: a `death` because the player was dead when the game died, a
    `wanted_change` from a stale star count, a `task_finished` for a task that
    no longer exists. Everything stateful must be reset first.
    """

    last_tick: int | None = None
    last_bridge_version: str | None = None
    restarts: int = 0

    def check(self, state: GameState) -> bool:
        """True exactly once per detected restart."""
        tick, version = state.tick, state.bridge.version
        restarted = self.last_tick is not None and (
            tick < self.last_tick or version != self.last_bridge_version
        )
        if restarted:
            self.restarts += 1
            log.warning(
                "game/bridge restart detected; resetting perception state",
                extra={
                    "kv": {
                        "previous_tick": self.last_tick,
                        "tick": tick,
                        "previous_version": self.last_bridge_version,
                        "version": version,
                        "restarts": self.restarts,
                    }
                },
            )
        self.last_tick = tick
        self.last_bridge_version = version
        return restarted


# --- bridge down --------------------------------------------------------------


@dataclass
class BridgeDownTracker:
    """Counts consecutive failures; escalates to a §4 bridge_down event at 3."""

    event_threshold: int = 3
    clock: Any = time.monotonic
    consecutive_failures: int = 0
    _down_since: float | None = None
    _event_emitted: bool = False

    def record_failure(self, exc: BridgeDownError) -> dict[str, Any] | None:
        """Returns a bridge_down §4 payload exactly once per outage."""
        self.consecutive_failures += 1
        if self._down_since is None:
            self._down_since = self.clock()
        log.warning(
            "bridge poll failed",
            extra={"kv": {"consecutive": self.consecutive_failures, "error": str(exc)[:120]}},
        )
        if self.consecutive_failures >= self.event_threshold and not self._event_emitted:
            self._event_emitted = True
            return {"consecutive_failures": self.consecutive_failures}
        return None

    def record_success(self) -> dict[str, Any] | None:
        """Returns a bridge_up §4 payload when an outage just ended."""
        if self._down_since is None:
            self.consecutive_failures = 0
            return None
        downtime = self.clock() - self._down_since
        was_reported = self._event_emitted
        self.consecutive_failures = 0
        self._down_since = None
        self._event_emitted = False
        if was_reported:
            log.info("bridge recovered", extra={"kv": {"downtime_s": round(downtime, 1)}})
            return {"downtime_s": round(downtime, 1)}
        return None

    def backoff_s(self) -> float:
        """Poll backoff while down: 1,2,4,8,… capped at 15 s."""
        return min(15.0, 2 ** min(self.consecutive_failures, 4))


# --- bridge answering but not ready (CONTRACTS v1.2 transient 503s) -----------


#: While the bridge is answering "not now", back off gently: it usually clears
#: within a loading screen. 0.5, 1, 2, 4, then 5 s.
_STALL_BACKOFF_CAP_S = 5.0


@dataclass
class BridgeStallTracker:
    """The bridge answered, but has no snapshot to give yet.

    `not_ready` / `game_thread_stalled` / `queue_full` (and any code this
    contract version does not know) are the bridge working correctly while the
    game is on a loading screen or streaming the world in. They are deliberately
    NOT reported as `bridge_down`: §4's `bridge_down` means the harness cannot
    reach the bridge at all, and claiming an outage that is not happening would
    put a false line on the site. They are logged when a stall starts, every
    `log_every_s` while it lasts, and once when it clears.
    """

    clock: Any = time.monotonic
    log_every_s: float = 10.0
    consecutive: int = 0
    _since: float | None = None
    _last_log: float = 0.0

    def record_failure(self, exc: BridgeApiError) -> float:
        """Log the stall (rate-limited) and return how long to wait before retrying."""
        now = self.clock()
        self.consecutive += 1
        first = self._since is None
        if first:
            self._since = now
        if first or now - self._last_log >= self.log_every_s:
            self._last_log = now
            log.warning(
                "bridge has no snapshot yet; waiting (normal during startup/loading)",
                extra={
                    "kv": {
                        "error": exc.error,
                        "status": exc.status,
                        "consecutive": self.consecutive,
                        "stalled_for_s": round(now - (self._since or now), 1),
                        "detail": exc.detail[:120],
                    }
                },
            )
        return self.backoff_s()

    def record_success(self) -> float | None:
        """Returns how long the stall lasted when one just ended, else None."""
        if self._since is None:
            self.consecutive = 0
            return None
        stalled_for = self.clock() - self._since
        self.consecutive = 0
        self._since = None
        self._last_log = 0.0
        log.info("bridge is publishing snapshots again", extra={"kv": {"stalled_for_s": round(stalled_for, 1)}})
        return stalled_for

    def backoff_s(self) -> float:
        return min(_STALL_BACKOFF_CAP_S, 0.5 * 2 ** min(max(self.consecutive - 1, 0), 4))


# --- Claude API backoff -------------------------------------------------------


#: Minimum backoff per failure cause. A 429 that comes back in two seconds is
#: just another 429; an overloaded (529) upstream wants real room. Anything
#: else (network blip, a bad response) retries fast.
BACKOFF_FLOOR_S: dict[str, float] = {
    "rate_limit": 30.0,
    "overloaded": 15.0,
    "other": 0.0,
}
#: Cause names this backoff understands. `classify_api_failure` maps exceptions
#: onto them so main never has to reason about HTTP status codes.
API_FAILURE_CAUSES: tuple[str, ...] = ("rate_limit", "overloaded", "other")


def classify_api_failure(exc: BaseException | None) -> str:
    """Map an exception (or a DecisionFailedError's __cause__) onto a cause.

    Kept string-based and import-light so it works whether the SDK raised a
    typed error or a plain transport error.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        name = type(exc).__name__
        if name == "RateLimitError":
            return "rate_limit"
        if name in ("InternalServerError", "APIStatusError"):
            status = getattr(exc, "status_code", None)
            if status == 429:
                return "rate_limit"
            if status in (500, 502, 503, 529):
                return "overloaded"
        status = getattr(exc, "status_code", None)
        if status == 429:
            return "rate_limit"
        if status == 529:
            return "overloaded"
        if status == 400 and _is_quota_message(str(exc)):
            # An org spend cap / exhausted credit arrives as a 400, not a 429,
            # and it stays true for hours or days. Retrying it every two
            # seconds is pure log noise.
            return "rate_limit"
        exc = exc.__cause__
    return "other"


def _is_quota_message(text: str) -> bool:
    """Heuristic, and safe if it misses: an unmatched message just backs off less."""
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in ("usage limit", "credit balance", "quota", "spend limit")
    )


def _retry_after_s(exc: BaseException | None) -> float | None:
    """Honour a server-sent retry-after header when the SDK exposed one."""
    while exc is not None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is not None:
            for key in ("retry-after", "anthropic-ratelimit-requests-reset"):
                raw = headers.get(key)
                if raw:
                    try:
                        return max(0.0, float(raw))
                    except (TypeError, ValueError):
                        pass
        exc = exc.__cause__
    return None


@dataclass
class ApiBackoff:
    """Exponential backoff with jitter for Claude API failures.

    While backing off, the reflex layer keeps control (long-running bridge
    tasks + idle behaviors); no model call is attempted before `ready()`.
    Rate limits and upstream overload get a floor so the harness stops hammering
    an endpoint that has already told it to wait.
    """

    base_s: float = 2.0
    cap_s: float = 300.0
    rng: random.Random = field(default_factory=random.Random)
    clock: Any = time.monotonic
    failures: int = 0
    last_cause: str = "other"
    _blocked_until: float = 0.0

    def record_failure(self, cause: str = "other", exc: BaseException | None = None) -> float:
        if cause not in BACKOFF_FLOOR_S:
            cause = "other"
        self.failures += 1
        self.last_cause = cause
        delay = min(self.cap_s, self.base_s * (2 ** (self.failures - 1)))
        delay = max(delay, BACKOFF_FLOOR_S[cause])
        retry_after = _retry_after_s(exc)
        if retry_after is not None:
            delay = max(delay, retry_after)
        delay = min(self.cap_s, delay * self.rng.uniform(0.8, 1.2))
        self._blocked_until = self.clock() + delay
        log.warning(
            "brain call failed; backing off",
            extra={
                "kv": {
                    "failures": self.failures,
                    "cause": cause,
                    "retry_after_s": retry_after,
                    "backoff_s": round(delay, 1),
                }
            },
        )
        return delay

    def record_success(self) -> None:
        self.failures = 0
        self.last_cause = "other"
        self._blocked_until = 0.0

    def ready(self) -> bool:
        return self.clock() >= self._blocked_until

    def blocked_for_s(self) -> float:
        return max(0.0, self._blocked_until - self.clock())
