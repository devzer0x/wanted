"""Tactical tier: Haiku 4.5, structured decisions, cached static prefix, thinking off.

Cadence (CONTRACTS §3 + WP-H): a tactical decision fires on task-finished,
danger, objective-change, or a jittered 8-25 s timer, and never sooner than
MIN_TACTICAL_GAP_S after the previous one — that floor is what makes the 8 s end
of the contracted band real and caps the tier at 450 calls/h (≈ $0.77/h at the
measured warm price). The static prefix carries cache_control (5-min TTL; the
cadence keeps it warm — each read refreshes free). `tool_choice` is never set,
so nothing can vary it between calls.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anthropic
import pydantic
from pydantic import TypeAdapter

from ..behavior.humanizer import MOOD_TIMER_RANGE_S
from ..budget import Pricing, cost_of_usage
from ..logsetup import get_logger
from ..perception import Delta
from ..settings import ConfigError, Settings
from .prompts import director_static_prefix, tactical_static_prefix
from .schemas import (
    DecisionModel,
    DecisionValidationContext,
    DecisionViolation,
    sanitize_decision,
    validate_decision_content,
)

log = get_logger("wasted.brain.tactical")

TACTICAL_TIMER_RANGE_S = (8.0, 25.0)
# Governor L1: slower tactical timers (CONTRACTS §7).
L1_TIMER_SCALE = 2.0
MAX_DECISION_TOKENS = 500

#: The director needs far more room than the tactical tier, and the reason is not prompt length.
#: MEASURED against the real API 2026-09-02: Sonnet 5 emits a **thinking block before the text
#: block**, and that thinking is billed against the same `max_tokens` budget. At 500 the thinking
#: consumed the whole allowance and the JSON was cut off mid-`params` — `stop_reason: max_tokens`,
#: `output_tokens: 500`, text present but truncated. Every director call failed twice and the
#: reflex layer kept control, so the agent played with no strategic layer at all. Haiku does not think,
#: which is exactly why the tactical tier never showed the bug.
#:
#: Measured completions at the same prompt: cap 1200 -> 443 tokens, `end_turn`, parses.
#: cap 2000 -> 599 tokens (the model spends more thinking when offered more).
#:
#: RAISED 1200 -> 3000 after the v1.10 deploy, 2026-09-02, from the live log: the FIRST director
#: call of the session failed with `response hit max_tokens (1200) and the decision JSON is
#: truncated`. The 1200 measurement was taken against the pre-v1.10 prompt; the dynamic context has
#: since grown (retrieved knowledge, mission state hint, mission.script identity), and a bigger
#: context is exactly what makes the model think longer. The old headroom was measured against a
#: prompt that no longer exists, which is why it stopped holding. 3000 restores multiples of
#: headroom over the largest completion ever observed (599) and costs nothing extra when unused:
#: `max_tokens` is a ceiling, and billing is per token GENERATED, not per token allowed.
DIRECTOR_MAX_DECISION_TOKENS = 3000

#: Hard floor between two tactical calls, applied to EVERY trigger — the event
#: ones included, not just the timer. This is what actually bounds the bill:
#: `delta.danger` is true on every poll while `wanted > 0`, so without a floor a
#: police chase fires a decision per poll (3 Hz = 10,800 calls/h). CONTRACTS §3
#: specifies an 8-25 s cadence; this makes the 8 s end real.
MIN_TACTICAL_GAP_S = TACTICAL_TIMER_RANGE_S[0]

#: The enforced ceiling: 3600 / 8 = 450 tactical calls per hour, reached only
#: when something is triggering continuously. Measured warm tactical cost is
#: $0.001714/call (docs/STATUS.md, 2026-08-25, real API), so the ceiling is
#: ≈ $0.77/h. Timer-only cadence is much lower and mood-dependent — see
#: `calls_per_hour_by_mood()`.
MAX_TACTICAL_CALLS_PER_HOUR = 3600.0 / MIN_TACTICAL_GAP_S

#: Real measured cost of one warm (prefix served from cache) tactical call.
#: Source: docs/STATUS.md 2026-08-25 brain verification against the live API
#: ($0.011331 cold / $0.001714 warm). Used for honest projections only; the
#: books are kept from per-call `usage` (budget.cost_of_usage), never from this.
MEASURED_WARM_TACTICAL_CALL_USD = 0.001714


def calls_per_hour_by_mood() -> dict[str, float]:
    """Expected timer-only tactical calls/hour per mood (uniform draw ⇒ mean interval).

    Mood genuinely changes this — narrowing the window to 8-15 s (hyped) raises
    the expected rate to ~313/h against ~180/h for bored. What mood cannot
    change is the ceiling, which :data:`MIN_TACTICAL_GAP_S` enforces.
    """
    return {
        mood: 3600.0 / ((lo + hi) / 2.0) for mood, (lo, hi) in MOOD_TIMER_RANGE_S.items()
    }


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


def verify_model_ids(
    client: anthropic.Anthropic,
    pricing: Pricing,
    on_cost: Callable[[float], None] | None = None,
) -> None:
    """Startup re-verification (CONTRACTS §3): a 1-token live call per model.

    Fails loudly on a bad/renamed model ID or a dead key. The two probes are
    real billed calls, so their usage goes into the books like any other —
    small, but the books are meant to be a measurement, not an estimate.
    """
    tiers = [pricing.tactical, pricing.director]
    if pricing.tactical_mission is not None:
        tiers.append(pricing.tactical_mission)
    for mp in tiers:
        try:
            response = client.messages.create(
                model=mp.id,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            if on_cost is not None:
                on_cost(cost_of_usage(pricing, mp.id, response.usage))
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
    checks: list[tuple[str, Any, str]] = [
        ("tactical", pricing.tactical, tactical_static_prefix()),
        ("director", pricing.director, director_static_prefix()),
    ]
    if pricing.tactical_mission is not None:
        # The mission tier sends the SAME static prefix the tactical tier
        # does (it is still a tactical decision, just on a smarter model
        # mid-mission — see TacticalBrain._call) — verify it against ITS OWN
        # cache minimum and model id, not assumed from the tactical result.
        checks.append(("tactical_mission", pricing.tactical_mission, tactical_static_prefix()))
    for tier, mp, prefix in checks:
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
    """Decides *when* the tactical tier fires.

    Two separate things, and an earlier version of this docstring conflated them:

    * **Mood changes the rate.** Narrowing the timer window inside the global
      8-25 s band (behavior.humanizer.MOOD_TIMER_RANGE_S) moves the expected
      timer-only rate from ~180 calls/h (bored, 15-25 s) to ~313 calls/h (hyped,
      8-15 s). At the measured warm cost of $0.001714/call that is ~$0.31/h to
      ~$0.54/h. Mood is *supposed* to cost differently; a chase is worth more
      words than a motorway.
    * **The ceiling is enforced separately**, by :data:`MIN_TACTICAL_GAP_S`. The
      event triggers (task_finished / danger / objective_change) do not consult
      the timer at all, and `danger` is true on *every* poll while wanted > 0 —
      so without a floor between calls the real ceiling was the poll rate
      (3 Hz = 10,800 calls/h ≈ $18/h), not the timer band. The floor makes the
      ceiling 3600/8 = 450 calls/h ≈ $0.77/h, and governor L1 doubles the floor
      to 16 s (225 calls/h ≈ $0.39/h).
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._last_fire = 0.0
        self._next_timer = self._draw_timer(0, "chill")

    def _draw_timer(self, governor_level: int, mood: str) -> float:
        lo, hi = MOOD_TIMER_RANGE_S.get(mood, TACTICAL_TIMER_RANGE_S)
        lo = max(lo, TACTICAL_TIMER_RANGE_S[0])
        hi = min(hi, TACTICAL_TIMER_RANGE_S[1])
        t = self._rng.uniform(lo, hi)
        if governor_level >= 1:
            t *= L1_TIMER_SCALE
        return t

    @staticmethod
    def min_gap_s(governor_level: int) -> float:
        """The enforced floor between two tactical calls at this governor level."""
        gap = MIN_TACTICAL_GAP_S
        if governor_level >= 1:
            gap *= L1_TIMER_SCALE
        return gap

    def should_fire(self, now: float, delta: Delta, governor_level: int) -> str | None:
        """Returns the trigger name, or None. Governor L2+ silences this tier."""
        if governor_level >= 2:
            return None
        # The floor comes first, so an event trigger can shorten the wait but can
        # never make the tier fire faster than the contracted 8 s cadence.
        if now - self._last_fire < self.min_gap_s(governor_level):
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

    def fired(self, now: float, governor_level: int, mood: str = "chill") -> None:
        self._last_fire = now
        self._next_timer = self._draw_timer(governor_level, mood)



#: The substring every word-limit ValidationError carries (schemas.py builds the
#: message as "... is N words; contract max is M"). It is the only reliable way to
#: tell a word-limit rejection from any other schema rejection without re-deriving
#: pydantic's error structure.
WORD_LIMIT_MARKER = "contract max is"

_WORD_LIMIT_COACHING = (
    "Fix exactly that and return the decision again. Word limits are hard limits, "
    "counted by a validator, not guidance: thought <= 40 words, say <= 20 words, "
    "goal <= 12 words. Being under the limit matters more than being thorough — a "
    "rejected decision means the agent does nothing at all this tick.\n"
)
_GENERIC_COACHING = (
    "Fix exactly that and return the decision again. A rejected decision means the agent "
    "does nothing at all this tick.\n"
)


def _retry_correction(exc: Exception) -> str:
    """A short corrective note appended to the dynamic context on the single retry.

    An API error carries nothing the model can act on, so it gets no note. A schema
    violation does: the message names the offending field and the limit it broke, and
    repeating that back is what turns a guaranteed second failure into a valid decision.

    The coaching that follows the echoed error has to match the error. A missing
    coordinate ("action 'drive_to' requires numeric 'x' in params") answered with a
    lecture about word counts tells the model the wrong thing about the wrong field,
    on the one attempt left before the reflex layer takes the tick; the word-limit
    paragraph is therefore attached only to word-limit rejections.
    """
    if isinstance(exc, anthropic.APIError):
        return ""
    coaching = _WORD_LIMIT_COACHING if WORD_LIMIT_MARKER in str(exc) else _GENERIC_COACHING
    return (
        "\n\nRETRY — your previous response was REJECTED by the schema validator:\n"
        f"  {type(exc).__name__}: {exc}\n" + coaching
    )


def _content_retry_correction(violation: DecisionViolation) -> str:
    """T4's corrective note: the single regenerate a content violation gets.

    Distinct from :func:`_retry_correction` because the fix on offer is
    different — nothing here is a word-count problem, so the word-limit
    coaching paragraph never belongs on it. If the regenerate ALSO violates,
    the caller sanitizes (drops the line, keeps the action) rather than
    treating it like a schema failure and discarding the whole decision.
    """
    return (
        "\n\nRETRY — your previous response was REJECTED by the output validator:\n"
        f"  {violation.describe()}\n"
        "Fix exactly that and return the decision again. Recheck: every name in "
        "`say`/`thought` must belong to someone in STATE right now; a mission name "
        "you use must match the identified mission exactly; do not use a banned "
        "phrase; do not repeat a recent line under a new wording; and if a ROAM "
        "AVAILABLE menu was shown, `goal` must be exactly one of the offered ids, "
        "nothing else. A second miss means this line is dropped and only the "
        "action goes out.\n"
    )


#: The decision schema in the wire form the API wants, built once. This is the
#: exact transform `client.messages.parse()` applies internally
#: (anthropic 1.0.0, resources/messages/messages.py); `transform_schema` is
#: exported from the package root, so this is the SDK's own public helper and
#: not a re-implementation of it.
def decision_output_config() -> dict[str, Any]:
    schema = anthropic.transform_schema(TypeAdapter(DecisionModel).json_schema())
    return {"format": {"type": "json_schema", "schema": schema}}


class BilledCall:
    """One structured-decision call, with the bill observed before the verdict.

    `client.messages.parse()` validates inside the SDK and raises before the
    caller ever sees `response.usage` — so every schema-rejected decision was a
    call Anthropic billed and the governor never heard about. The rejected first
    attempt of a retried decision, and BOTH attempts of a decision that failed
    outright, were free as far as the budget was concerned; the governor
    throttles on that figure, so real spend could pass the hourly cap while the
    site still published L0 and a cost that was a floor, not a measurement.

    This does what `parse()` does — `messages.create` with the same
    `output_config`, then validate the returned JSON — split at the one point
    that matters: the usage is recorded the moment the response arrives,
    whatever happens to the content afterwards.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        pricing: Pricing,
        on_cost: Callable[[float], None] | None,
    ) -> None:
        self._client = client
        self._pricing = pricing
        self._on_cost = on_cost
        self._output_config = decision_output_config()

    def run(
        self,
        model_id: str,
        prefix: str,
        content: list[dict[str, Any]] | str,
        max_tokens: int = MAX_DECISION_TOKENS,
    ) -> DecisionResult:
        response = self._client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": prefix,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
            output_config=self._output_config,
        )
        usage = response.usage
        cost = cost_of_usage(self._pricing, model_id, usage)
        # Before validation, on purpose: the call is billed either way.
        if self._on_cost is not None:
            self._on_cost(cost)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
        if not text:
            raise ValueError(
                f"model returned no text block (stop_reason={response.stop_reason}, "
                f"blocks={[getattr(b, 'type', '?') for b in response.content]})"
            )
        if response.stop_reason == "max_tokens":
            # The text is present but cut off mid-JSON, so `model_validate_json` below would fail
            # with an opaque parse error. Name the real cause instead: this cost a live debugging
            # session when Sonnet 5's thinking block ate a 500-token budget.
            raise ValueError(
                f"response hit max_tokens ({max_tokens}) and the decision JSON is truncated "
                f"after {usage.output_tokens} output tokens; raise the tier's token cap"
            )
        decision = DecisionModel.model_validate_json(text)
        return DecisionResult(
            decision=decision,
            model=model_id,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_creation_tokens=usage.cache_creation_input_tokens or 0,
            cost_usd=cost,
        )


class TacticalBrain:
    def __init__(
        self,
        client: anthropic.Anthropic,
        pricing: Pricing,
        on_cost: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self._pricing = pricing
        self._prefix = tactical_static_prefix()
        self._call_api = BilledCall(client, pricing, on_cost)

    def decide(
        self,
        dynamic_context: str,
        mission_active: bool = False,
        validation_ctx: DecisionValidationContext | None = None,
    ) -> DecisionResult:
        """One structured decision. Retries once on validation/API failure, then
        raises DecisionFailedError (reflex keeps control, per CONTRACTS §2).

        The retry is NOT blind: a schema violation is fed back to the model so it can
        correct it. Observed live against the real game — the model returned a 56-word
        `thought` against the 40-word contract cap, the retry sent the identical prompt,
        it returned 57 words, and the whole decision was discarded. The agent stood still
        mid-mission because nobody ever told him what was wrong.

        `mission_active` — the `mission.active` flag of the state THIS decision is
        being made on — selects the mission-time tactical model (WP-C) when the
        caller's pricing.yaml configures one. Absent config or `mission_active=False`
        falls straight back through to the normal tactical tier: identical to
        today's behaviour.

        `validation_ctx` (T4, findings.md) — when given, a decision that PARSES
        but whose content violates the output validator (a name not in STATE,
        a wrong mission name, a banned phrase, a near-repeat, or — with a roam
        menu on offer — a `goal` that names no offered id) gets the SAME one
        regenerate a schema violation gets, using the second (and last)
        attempt. If the regenerate still violates, the line is sanitized
        (`say` dropped, `goal` left for the roam engine's own fallback) and
        the decision is returned rather than discarded — the action always
        goes out. `None` skips content validation entirely (tests, or a
        caller with no world snapshot to check against)."""
        last_exc: Exception | None = None
        context = dynamic_context
        for attempt in (1, 2):
            try:
                result = self._call(context, mission_active)
            except (anthropic.APIError, pydantic.ValidationError, ValueError) as exc:
                last_exc = exc
                log.warning(
                    "tactical decision attempt failed",
                    extra={"kv": {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}},
                )
                if attempt == 1:
                    context = dynamic_context + _retry_correction(exc)
                    time.sleep(0.5)
                continue
            if validation_ctx is not None:
                violation = validate_decision_content(result.decision, validation_ctx)
                if violation:
                    log.warning(
                        "tactical decision content rejected by the output validator",
                        extra={"kv": {"attempt": attempt, "violation": violation.describe()}},
                    )
                    if attempt == 1:
                        context = dynamic_context + _content_retry_correction(violation)
                        time.sleep(0.5)
                        continue
                    result.decision = sanitize_decision(result.decision, violation)
            return result
        raise DecisionFailedError(
            f"tactical decision failed twice; reflex layer keeps control "
            f"(last error: {type(last_exc).__name__}: {last_exc})"
        ) from last_exc

    def _call(self, dynamic_context: str, mission_active: bool = False) -> DecisionResult:
        mission_tier = self._pricing.tactical_mission
        if mission_active and mission_tier is not None:
            # Mid-mission: swap in the smarter (and pricier) model for the
            # SAME tactical decision, on the SAME static prefix — only the
            # model changes. `max_tokens` MUST be raised to the director's own
            # floor: Sonnet 5 emits a thinking block billed against
            # `max_tokens` before its text, and the tactical tier's normal cap
            # (500) is exactly the value that truncated the director's JSON
            # mid-`params` when this was first measured (see
            # DIRECTOR_MAX_DECISION_TOKENS above) — the same failure mode
            # would silently eat every mission-tier decision at the default.
            return self._call_api.run(
                mission_tier.id, self._prefix, dynamic_context,
                max_tokens=DIRECTOR_MAX_DECISION_TOKENS,
            )
        return self._call_api.run(
            self._pricing.tactical.id, self._prefix, dynamic_context
        )
