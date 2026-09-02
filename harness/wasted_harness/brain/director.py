"""Director tier: Sonnet 5, 60-120 s cadence or big events, occasional vision.

Vision is allowed ONLY on the contracted screenshot triggers — the §4 events
whose screenshot column says yes: death, busted, mission_end, mission_fail,
stunt, and wanted_change reaching >= 3. Everything else is text-only; a raw
frame is never attached "for flavor" (D5: 768-px JPEG ≈ 448 visual tokens).

`stunt` is in the contract's list but the harness cannot emit it yet
(events.UNPRODUCED_EVENT_TYPES explains why), so it is subtracted from the live
trigger sets rather than left sitting in them looking wired up.
"""

from __future__ import annotations

import base64
import random
import time
from collections.abc import Callable
from typing import Any

import anthropic
import pydantic

from ..budget import Pricing
from ..events import UNPRODUCED_EVENT_TYPES
from ..logsetup import get_logger
from .prompts import director_static_prefix
from .tactical import (
    DIRECTOR_MAX_DECISION_TOKENS,
    BilledCall,
    DecisionFailedError,
    DecisionResult,
    _retry_correction,
)

log = get_logger("wasted.brain.director")

DIRECTOR_TIMER_RANGE_S = (60.0, 120.0)

# §4 events with screenshot: yes — the contract's list, kept verbatim so the
# copy can be checked against docs/CONTRACTS.md. wanted_change qualifies only
# when `to` >= 3; the caller enforces that before naming it as a trigger.
#
# mission_start was added in CONTRACTS v1.3 after the first live session. /state carries no
# mission name and no objective text (the field does not exist anywhere in the pipeline), so
# the screen is the only honest source of what a mission actually wants. Without this the
# director could only ever look AFTER something went wrong — death, busted, mission_fail —
# never at the moment the game draws the objective.
CONTRACT_VISION_EVENTS: frozenset[str] = frozenset(
    {
        "death",
        "busted",
        "mission_start",  # v1.3
        "mission_end",
        "mission_fail",
        "stunt",
        "wanted_change",
    }
)

# Events big enough to wake the director early (harness policy, not contract).
CONTRACT_BIG_EVENTS: frozenset[str] = frozenset(
    {
        "death",
        "busted",
        "mission_start",
        "mission_end",
        "mission_fail",
        "governor_level",
        "break",
        "stunt",
    }
)

# What the harness actually arms: the contract lists minus the events nothing
# emits yet. Subtracting keeps the trigger sets honest instead of listing a
# trigger that can never fire.
VISION_TRIGGERS: frozenset[str] = CONTRACT_VISION_EVENTS - UNPRODUCED_EVENT_TYPES
BIG_EVENTS: frozenset[str] = CONTRACT_BIG_EVENTS - UNPRODUCED_EVENT_TYPES


class DirectorCadence:
    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._last_fire = 0.0
        self._next_timer = self._rng.uniform(*DIRECTOR_TIMER_RANGE_S)

    def should_fire(self, now: float, big_event: str | None, governor_level: int) -> str | None:
        if governor_level >= 3:
            return None  # L3: asleep in the car; nothing thinks until the hour resets
        if big_event is not None and big_event in BIG_EVENTS:
            return f"event:{big_event}"
        if now - self._last_fire >= self._next_timer:
            return "timer"
        return None

    def fired(self, now: float) -> None:
        self._last_fire = now
        self._next_timer = self._rng.uniform(*DIRECTOR_TIMER_RANGE_S)


class DirectorBrain:
    def __init__(
        self,
        client: anthropic.Anthropic,
        pricing: Pricing,
        on_cost: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self._pricing = pricing
        self._prefix = director_static_prefix()
        # Same billed-call wrapper as the tactical tier: the cost of a response
        # is recorded when it arrives, not when the caller manages to use it.
        self._call_api = BilledCall(client, pricing, on_cost)

    def decide(
        self,
        dynamic_context: str,
        screenshot_jpeg: bytes | None = None,
        screenshot_trigger: str | None = None,
    ) -> DecisionResult:
        """One director decision. A screenshot is attached only when its trigger
        is contracted; passing one without a valid trigger is a programming
        error and raises immediately (no silent extra vision spend)."""
        if screenshot_jpeg is not None and screenshot_trigger not in VISION_TRIGGERS:
            raise ValueError(
                f"screenshot attached with trigger {screenshot_trigger!r}, which is "
                f"not a contracted vision trigger ({sorted(VISION_TRIGGERS)})"
            )
        last_exc: Exception | None = None
        context = dynamic_context
        for attempt in (1, 2):
            try:
                return self._call(context, screenshot_jpeg)
            except (anthropic.APIError, pydantic.ValidationError, ValueError) as exc:
                last_exc = exc
                log.warning(
                    "director decision attempt failed",
                    extra={"kv": {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}},
                )
                if attempt == 1:
                    # Same fix as the tactical tier: a blind retry repeats the same
                    # schema violation. Feed the validator's complaint back.
                    context = dynamic_context + _retry_correction(exc)
                    time.sleep(1.0)
        raise DecisionFailedError(
            f"director decision failed twice (last error: "
            f"{type(last_exc).__name__}: {last_exc})"
        ) from last_exc

    def _call(self, dynamic_context: str, screenshot_jpeg: bytes | None) -> DecisionResult:
        content: list[dict[str, Any]] = [{"type": "text", "text": dynamic_context}]
        if screenshot_jpeg is not None:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(screenshot_jpeg).decode("ascii"),
                    },
                }
            )
        return self._call_api.run(
            self._pricing.director.id, self._prefix, content,
            max_tokens=DIRECTOR_MAX_DECISION_TOKENS,
        )
