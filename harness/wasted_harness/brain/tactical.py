"""Tactical tier: Haiku 4.5, structured decisions, cached static prefix, thinking off.

Cadence (CONTRACTS §3 + WP-H): a tactical decision fires on task-finished,
danger, objective-change, or a jittered 8-25 s timer. The static prefix carries
cache_control (5-min TTL; the cadence keeps it warm — each read refreshes free).
`tool_choice` is never set, so nothing can vary it between calls.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import anthropic
import pydantic

from ..budget import Pricing, cost_of_usage
from ..logsetup import get_logger
from ..perception import Delta
from ..settings import ConfigError, Settings
from .prompts import director_static_prefix, tactical_static_prefix
from .schemas import DecisionModel

log = get_logger("wasted.brain.tactical")

TACTICAL_TIMER_RANGE_S = (8.0, 25.0)
# Governor L1: slower tactical timers (CONTRACTS §7).
L1_TIMER_SCALE = 2.0
MAX_DECISION_TOKENS = 500


class BrainUnavailableError(ConfigError):
    """The brain cannot run (no API key / failed startup verification)."""


class DecisionFailedError(RuntimeError):
    """Both the call and its single retry failed; the reflex layer keeps control."""


def make_client(settings: Settings) -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise BrainUnavailableError(
            "ANTHROPIC_API_KEY is not set. The brain refuses to start without it — "
            "there is no offline/demo mode. Set it in harness/.env."
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def verify_model_ids(client: anthropic.Anthropic, pricing: Pricing) -> None:
    """Startup re-verification (CONTRACTS §3): a 1-token live call per model.

    Fails loudly on a bad/renamed model ID or a dead key.
    """
    for mp in (pricing.tactical, pricing.director):
        try:
            client.messages.create(
                model=mp.id,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
        except anthropic.APIError as exc:
            raise BrainUnavailableError(
                f"startup model check failed for {mp.id!r}: {exc}. "
                f"Fix pricing.yaml (model id) or the API key before running."
            ) from exc
        log.info("model verified", extra={"kv": {"model": mp.id}})


def verify_prefix_cacheable(client: anthropic.Anthropic, pricing: Pricing) -> dict[str, int]:
    """Count the static prefixes and fail loudly if under the cache minimums.

    Haiku 4.5 needs > 4096 tokens or caching silently does nothing; Sonnet 5
    needs > 1024. Requires an API key (count_tokens is a free endpoint); the
    caller skips this with a clear message when no key is configured.
    """
    counts: dict[str, int] = {}
    for tier, mp, prefix in (
        ("tactical", pricing.tactical, tactical_static_prefix()),
        ("director", pricing.director, director_static_prefix()),
    ):
        try:
            result = client.messages.count_tokens(
                model=mp.id,
                system=[{"type": "text", "text": prefix}],
                messages=[{"role": "user", "content": "x"}],
            )
        except anthropic.APIError as exc:
            raise BrainUnavailableError(
                f"count_tokens failed for {mp.id!r} while verifying the {tier} "
                f"static prefix: {exc}"
            ) from exc
        counts[tier] = result.input_tokens
        if result.input_tokens < mp.min_cacheable_prefix_tokens:
            raise BrainUnavailableError(
                f"{tier} static prefix is {result.input_tokens} tokens on {mp.id}, "
                f"below the {mp.min_cacheable_prefix_tokens}-token cache minimum — "
                f"caching would silently do nothing and cost ~10x. Extend the "
                f"prompt files in wasted_harness/brain/prompts/."
            )
        log.info(
            "static prefix verified cacheable",
            extra={"kv": {"tier": tier, "tokens": result.input_tokens}},
        )
    return counts


@dataclass
class DecisionResult:
    decision: DecisionModel
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float

    @property
    def cached_tokens(self) -> int:
        """The decisions-table `cached_tokens` column: tokens served from cache."""
        return self.cache_read_tokens


class TacticalCadence:
    """Decides *when* the tactical tier fires."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._last_fire = 0.0
        self._next_timer = self._draw_timer(0)

    def _draw_timer(self, governor_level: int) -> float:
        lo, hi = TACTICAL_TIMER_RANGE_S
        t = self._rng.uniform(lo, hi)
        if governor_level >= 1:
            t *= L1_TIMER_SCALE
        return t

    def should_fire(self, now: float, delta: Delta, governor_level: int) -> str | None:
        """Returns the trigger name, or None. Governor L2+ silences this tier."""
        if governor_level >= 2:
            return None
        if delta.task_finished:
            return "task_finished"
        if delta.danger:
            return "danger"
        if delta.objective_changed:
            return "objective_change"
        if now - self._last_fire >= self._next_timer:
            return "timer"
        return None

    def fired(self, now: float, governor_level: int) -> None:
        self._last_fire = now
        self._next_timer = self._draw_timer(governor_level)


class TacticalBrain:
    def __init__(self, client: anthropic.Anthropic, pricing: Pricing) -> None:
        self._client = client
        self._pricing = pricing
        self._prefix = tactical_static_prefix()

    def decide(self, dynamic_context: str) -> DecisionResult:
        """One structured decision. Retries once on validation/API failure, then
        raises DecisionFailedError (reflex keeps control, per CONTRACTS §2)."""
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                return self._call(dynamic_context)
            except (anthropic.APIError, pydantic.ValidationError, ValueError) as exc:
                last_exc = exc
                log.warning(
                    "tactical decision attempt failed",
                    extra={"kv": {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}},
                )
                if attempt == 1:
                    time.sleep(0.5)
        raise DecisionFailedError(
            f"tactical decision failed twice; reflex layer keeps control "
            f"(last error: {type(last_exc).__name__}: {last_exc})"
        ) from last_exc

    def _call(self, dynamic_context: str) -> DecisionResult:
        mp = self._pricing.tactical
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
            messages=[{"role": "user", "content": dynamic_context}],
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
