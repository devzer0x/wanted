"""Director tier: Sonnet 5, 60-120 s cadence or big events, occasional vision.

Vision is allowed ONLY on the contracted screenshot triggers — the §4 events
whose screenshot column says yes: death, busted, mission_end, mission_fail,
stunt, and wanted_change reaching >= 3. Everything else is text-only; a raw
frame is never attached "for flavor" (D5: 768-px JPEG ≈ 448 visual tokens).
"""

from __future__ import annotations

import base64
import random
import time

import anthropic
import pydantic

from ..budget import Pricing, cost_of_usage
from ..logsetup import get_logger
from .prompts import director_static_prefix
from .schemas import DecisionModel
from .tactical import MAX_DECISION_TOKENS, DecisionFailedError, DecisionResult

log = get_logger("wasted.brain.director")

DIRECTOR_TIMER_RANGE_S = (60.0, 120.0)

# §4 events with screenshot: yes. wanted_change qualifies only when `to` >= 3;
# the caller enforces that before naming it as a trigger.
VISION_TRIGGERS: frozenset[str] = frozenset(
    {"death", "busted", "mission_end", "mission_fail", "stunt", "wanted_change"}
)

# Events big enough to wake the director early.
BIG_EVENTS: frozenset[str] = frozenset(
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
    def __init__(self, client: anthropic.Anthropic, pricing: Pricing) -> None:
        self._client = client
        self._pricing = pricing
        self._prefix = director_static_prefix()

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
        for attempt in (1, 2):
            try:
                return self._call(dynamic_context, screenshot_jpeg)
            except (anthropic.APIError, pydantic.ValidationError, ValueError) as exc:
                last_exc = exc
                log.warning(
                    "director decision attempt failed",
                    extra={"kv": {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}},
                )
                if attempt == 1:
                    time.sleep(1.0)
        raise DecisionFailedError(
            f"director decision failed twice (last error: "
            f"{type(last_exc).__name__}: {last_exc})"
        ) from last_exc

    def _call(self, dynamic_context: str, screenshot_jpeg: bytes | None) -> DecisionResult:
        mp = self._pricing.director
        content: list[dict] = [{"type": "text", "text": dynamic_context}]
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
        response = self._client.messages.parse(
            model=mp.id,
            max_tokens=MAX_DECISION_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": self._prefix,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
            output_format=DecisionModel,
        )
        decision = response.parsed_output
        if decision is None:
            raise ValueError("model returned no parseable decision object")
        usage = response.usage
        return DecisionResult(
            decision=decision,
            model=mp.id,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_creation_tokens=usage.cache_creation_input_tokens or 0,
            cost_usd=cost_of_usage(self._pricing, mp.id, usage),
        )
