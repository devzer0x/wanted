"""Recovery handlers: stuck, flipped, stranded, bridge-down, API backoff, Supabase-down.

Each handler is small state + a decision: what to do NOW, without a model call.
They are the harness's spinal reflexes — they keep the show alive when the
brain, the bridge, or the network is having a moment.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from ..bridge_client import BridgeApiError, BridgeClient, BridgeDownError, GameState
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
        """Calls /unstick; returns meters moved, or None when the bridge refuses
        (409 unstick_conditions_not_met) — refusal is normal and final."""
        try:
            result = bridge.unstick()
            return result.distance_m if result.moved else None
        except BridgeApiError as exc:
            if exc.status == 409:
                log.info("unstick refused by bridge", extra={"kv": {"error": exc.error}})
                return None
            raise


def flipped_action(state: GameState) -> dict[str, Any] | None:
    """Upside-down and not moving → get out (the engine rights nothing for us)."""
    v = state.vehicle
    if v and v.upside_down and v.speed < 0.5:
        return {"type": "exit_vehicle", "params": {}}
    return None


def stranded_action(state: GameState) -> dict[str, Any] | None:
    """On foot, nothing driveable near, no task running → go find wheels."""
    if state.player.in_vehicle or state.last_task.status == "running":
        return None
    has_candidate = any(nb.driver != "player" for nb in state.nearby.vehicles)
    if has_candidate:
        return {"type": "enter_nearest_vehicle", "params": {"prefer": "any", "search_radius_m": 50}}
    # Nothing in scan range: walk toward the street grid until traffic appears.
    return None


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


# --- Claude API backoff -------------------------------------------------------


@dataclass
class ApiBackoff:
    """Exponential backoff with jitter for Claude API failures.

    While backing off, the reflex layer keeps control (long-running bridge
    tasks + idle behaviors); no model call is attempted before `ready()`.
    """

    base_s: float = 2.0
    cap_s: float = 120.0
    rng: random.Random = field(default_factory=random.Random)
    clock: Any = time.monotonic
    failures: int = 0
    _blocked_until: float = 0.0

    def record_failure(self) -> float:
        self.failures += 1
        delay = min(self.cap_s, self.base_s * (2 ** (self.failures - 1)))
        delay *= self.rng.uniform(0.8, 1.2)
        self._blocked_until = self.clock() + delay
        log.warning(
            "brain call failed; backing off",
            extra={"kv": {"failures": self.failures, "backoff_s": round(delay, 1)}},
        )
        return delay

    def record_success(self) -> None:
        self.failures = 0
        self._blocked_until = 0.0

    def ready(self) -> bool:
        return self.clock() >= self._blocked_until
