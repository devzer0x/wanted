"""Humanizer: reaction jitter, mood model, idle behaviors, breaks.

The point is that the agent reads as a person: he reacts ~half a second late, his
mood drifts instead of flipping, he fidgets when idle, and he takes a break
every 40-70 minutes like anyone glued to a chair.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from ..events import EMITTED_EVENT_TYPES
from ..logsetup import get_logger

log = get_logger("wasted.humanizer")

REACTION_JITTER_S = (0.300, 0.900)
BREAK_EVERY_S = (40 * 60.0, 70 * 60.0)
BREAK_LENGTH_S = (3 * 60.0, 7 * 60.0)

#: §4 event → the mood it forces. The intent for every event lives here…
_EVENT_MOOD_INTENT: dict[str, str] = {
    "death": "scared",
    "busted": "bored",
    "mission_end": "smug",
    "mission_fail": "chill",
    "stunt": "hyped",
}
#: …but only the ones something actually emits are armed. A mood rule keyed off
#: an event that never arrives is dead wiring that reads like a working feature
#: (`stunt` was exactly that). Wiring the event up in events.py re-arms its mood
#: rule automatically — see events.UNPRODUCED_EVENT_REASONS and the README.
EVENT_MOOD: dict[str, str] = {
    event: mood
    for event, mood in _EVENT_MOOD_INTENT.items()
    if event in EMITTED_EVENT_TYPES
}

MOOD_DRIVING_STYLE: dict[str, str] = {
    "chill": "normal",
    "bored": "normal",
    "hyped": "rushed",
    "scared": "avoid_traffic",  # scared avoids traffic (CONTRACTS §2 note)
    "smug": "normal",
}

#: Mood → tactical-timer window, in seconds. Mood has to be visible in the
#: PACING, not only in the words: hyped the agent talks over a chase, bored the agent
#: lets a long drive breathe.
#:
#: Both ends stay inside the global 8-25 s band, but be honest about what that
#: does and does not buy: narrowing the window DOES change calls/hour (hyped
#: ~313/h vs bored ~180/h — brain.tactical.calls_per_hour_by_mood). The ceiling
#: is held by brain.tactical.MIN_TACTICAL_GAP_S, not by these windows.
MOOD_TIMER_RANGE_S: dict[str, tuple[float, float]] = {
    "hyped": (8.0, 15.0),
    "scared": (8.0, 17.0),
    "smug": (10.0, 20.0),
    "chill": (12.0, 25.0),
    "bored": (15.0, 25.0),
}

#: Mood → cruising speed for harness-issued drive tasks (m/s). The reflex layer
#: and the activity runner use this so an unattended stretch still looks like a
#: person in a mood rather than a constant-velocity robot.
MOOD_CRUISE_MPS: dict[str, float] = {
    "chill": 15.0,
    "bored": 17.0,
    "hyped": 26.0,
    "scared": 12.0,
    "smug": 19.0,
}


def reaction_delay(rng: random.Random | None = None) -> float:
    """Seconds to wait before acting on a decision. Uniform 300-900 ms."""
    return (rng or random).uniform(*REACTION_JITTER_S)


# --- mood ---------------------------------------------------------------------


@dataclass
class MoodModel:
    """Event-driven mood with slow decay toward chill.

    The model's decision carries its own mood; this tracker is the reflex
    layer's view (used when the brain is silenced by the governor) and the
    sanity check that keeps mood from flipping every decision.
    """

    mood: str = "chill"
    rng: random.Random = field(default_factory=random.Random)
    clock: Any = time.monotonic
    _changed_at: float = field(default=0.0, init=False)
    _min_hold_s: float = 90.0

    def __post_init__(self) -> None:
        # Must be "now", not 0.0: with a monotonic clock, 0.0 makes the very
        # first quiet tick look like the mood has been held for the machine's
        # entire uptime, so the agent booted straight into `bored`.
        self._changed_at = self.clock()

    def observe(self, event: str) -> str:
        """Feed a §4 event type (or 'quiet' for a no-event tick)."""
        now = self.clock()
        if event in EVENT_MOOD:
            self._set(EVENT_MOOD[event], now, force=True)
        elif event == "wanted_high":
            self._set("scared", now, force=True)
        elif event == "wanted_clear" and self.mood == "scared":
            self._set("smug", now, force=True)
        elif event == "quiet":
            self._decay(now)
        return self.mood

    def _set(self, mood: str, now: float, force: bool = False) -> None:
        if mood == self.mood:
            return
        if not force and now - self._changed_at < self._min_hold_s:
            return
        self.mood = mood
        self._changed_at = now

    def _decay(self, now: float) -> None:
        # After ~8 quiet minutes anything drifts to bored; smug/scared relax to
        # chill after ~3 minutes. Boredom is the engine of content.
        held = now - self._changed_at
        if self.mood in ("smug", "scared", "hyped") and held > 180.0:
            self._set("chill", now, force=True)
        elif self.mood == "chill" and held > 480.0:
            self._set("bored", now, force=True)

    def driving_style(self) -> str:
        return MOOD_DRIVING_STYLE[self.mood]

    def cruise_speed_mps(self) -> float:
        return MOOD_CRUISE_MPS[self.mood]

    def timer_range_s(self) -> tuple[float, float]:
        return MOOD_TIMER_RANGE_S[self.mood]

    def held_for_s(self) -> float:
        return max(0.0, self.clock() - self._changed_at)


# --- idle behaviors -----------------------------------------------------------


@dataclass(frozen=True)
class IdleBehavior:
    name: str
    weight: float
    cooldown_s: float
    action: dict[str, Any]  # decision-schema action dict
    moods: frozenset[str] = frozenset({"chill", "bored", "hyped", "scared", "smug"})


IDLE_BEHAVIORS: tuple[IdleBehavior, ...] = (
    IdleBehavior(
        "look_around", 4.0, 45.0, {"type": "look_around", "params": {}}
    ),
    IdleBehavior(
        "radio_flip",
        2.0,
        240.0,
        {"type": "radio", "params": {"station": "Radio Los Santos"}},
        frozenset({"chill", "bored", "smug"}),
    ),
    IdleBehavior(
        "radio_off_nervous",
        1.5,
        300.0,
        {"type": "radio", "params": {"station": "off"}},
        frozenset({"scared"}),
    ),
    IdleBehavior(
        "sit_a_beat", 3.0, 60.0, {"type": "wait", "params": {"seconds": 6}}
    ),
    IdleBehavior(
        "horn_tap",
        0.5,
        420.0,
        {"type": "horn", "params": {"ms": 150}},
        frozenset({"bored", "hyped"}),
    ),
)


class IdlePicker:
    def __init__(self, rng: random.Random | None = None, clock=time.monotonic) -> None:
        self._rng = rng or random.Random()
        self._clock = clock
        self._last_used: dict[str, float] = {}

    def pick(self, mood: str) -> IdleBehavior | None:
        now = self._clock()
        ready = [
            b
            for b in IDLE_BEHAVIORS
            if mood in b.moods and now - self._last_used.get(b.name, -1e9) >= b.cooldown_s
        ]
        if not ready:
            return None
        total = sum(b.weight for b in ready)
        r = self._rng.uniform(0, total)
        acc = 0.0
        for b in ready:
            acc += b.weight
            if r <= acc:
                self._last_used[b.name] = now
                return b
        self._last_used[ready[-1].name] = now
        return ready[-1]


# --- breaks -------------------------------------------------------------------


@dataclass
class BreakScheduler:
    """A break every 40-70 min, 3-7 min long, announced via the §4 `break` event."""

    rng: random.Random = field(default_factory=random.Random)
    clock: Any = time.monotonic
    _next_break_at: float = field(init=False)
    _break_until: float | None = None
    _planned_s: float = 0.0

    def __post_init__(self) -> None:
        self._next_break_at = self.clock() + self.rng.uniform(*BREAK_EVERY_S)

    @property
    def on_break(self) -> bool:
        return self._break_until is not None and self.clock() < self._break_until

    def due(self) -> bool:
        return self._break_until is None and self.clock() >= self._next_break_at

    def start(self) -> float:
        """Begin a break; returns the planned length in seconds."""
        self._planned_s = self.rng.uniform(*BREAK_LENGTH_S)
        self._break_until = self.clock() + self._planned_s
        log.info("break started", extra={"kv": {"planned_s": round(self._planned_s)}})
        return self._planned_s

    def finish_if_over(self) -> float | None:
        """Returns the planned length when the break just ended, else None."""
        if self._break_until is not None and self.clock() >= self._break_until:
            planned, self._break_until = self._planned_s, None
            self._next_break_at = self.clock() + self.rng.uniform(*BREAK_EVERY_S)
            log.info("break ended", extra={"kv": {"planned_s": round(planned)}})
            return planned
        return None
