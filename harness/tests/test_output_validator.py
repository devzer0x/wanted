"""T4 (findings.md R4): the output validator.

names in `say`/`thought` (subset of STATE), the mission name (must equal the
identified mission), banned phrases (curated in commentary_style.md), dedupe
(>= 0.6 normalized overlap against the last 5 shown lines), and — with a roam
menu on offer — `goal` (must be one of the offered ids). On any rejection:
exactly ONE regenerate, then the line is dropped (`say` -> "") and the ACTION
is kept; a bad `goal` is left for `RoamEngine`'s own `available[0]` fallback.

Two layers of test: pure unit tests of `validate_decision_content`/
`sanitize_decision` (schemas.py), then the real `TacticalBrain.decide`/
`DirectorBrain.decide` retry loop exercised end-to-end with the fake brain
(`tests/support/fakebrain.py`) — no network, no live key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anthropic
from support.fakebrain import install
from support.states import make_state

from wasted_harness.behavior.roam import RoamEngine
from wasted_harness.brain.director import DirectorBrain
from wasted_harness.brain.prompts import banned_phrases
from wasted_harness.brain.schemas import (
    DEDUPE_OVERLAP_THRESHOLD,
    ActionModel,
    DecisionModel,
    DecisionValidationContext,
    DecisionViolation,
    sanitize_decision,
    validate_decision_content,
)
from wasted_harness.brain.tactical import TacticalBrain
from wasted_harness.budget import Pricing

PRICING_PATH = Path(__file__).resolve().parents[1] / "config" / "pricing.yaml"


def _pricing() -> Pricing:
    return Pricing.load(PRICING_PATH)


def _client() -> anthropic.Anthropic:
    # Never called: see test_fakebrain.py's own `_client()` for why building
    # the object makes no network call.
    return anthropic.Anthropic(api_key="sk-fake-not-used")


def _decision(
    say: str = "Clean line, nothing wrong with it.",
    thought: str = "Reading the road, nothing unusual.",
    goal: str = "wake up, find wheels, see what the day wants",
    action_type: str = "wait",
) -> DecisionModel:
    return DecisionModel(
        thought=thought,
        say=say,
        mood="chill",
        action=ActionModel(type=action_type, params={}),
        goal=goal,
        confidence=0.6,
    )


def _context_for(
    state, *, roam_current: str | None = None, roam_offered: tuple[str, ...] = ()
) -> str:
    """A minimal `main._dynamic_context`-shaped string — same idiom
    `test_fakebrain.py` uses (STATE block + the ROAM menu text
    `RoamEngine.note()` actually renders, so `RoamEngine.model_choice` and the
    fake brain's own `_roam_hint` both parse it correctly)."""
    parts = ["TRIGGER: timer", "GOAL: wake up, find wheels, see what the day wants"]
    if roam_current is not None:
        parts.append(
            f'ROAM CURRENT: {roam_current} — some reason ("why"). 4s of 180s. '
            f'Set your "goal" field to exactly: {roam_current} — nothing else. Any other value is ignored.'
        )
    elif roam_offered:
        ids = " | ".join(roam_offered)
        menu = "; ".join(f'{i} ("why")' for i in roam_offered)
        parts.append(
            f'ROAM AVAILABLE: {menu}. Set your "goal" field to EXACTLY ONE of: '
            f"{ids} — the bare id, no sentence. First listed is the live opportunity. "
            f"A goal that is not one of these ids is thrown away and picked for you."
        )
    parts.append("STATE: " + state.model_dump_json(by_alias=True))
    parts.append("CHANGES: none")
    return "\n\n".join(parts)


# --- validate_decision_content: one rule at a time ------------------------------


def test_a_clean_decision_has_no_violation() -> None:
    ctx = DecisionValidationContext()
    v = validate_decision_content(_decision(), ctx)
    assert not v
    assert v.say_reason is None
    assert v.goal_reason is None


def test_banned_phrase_is_caught() -> None:
    ctx = DecisionValidationContext(banned_phrases=("as an ai",))
    v = validate_decision_content(_decision(say="As an AI, I cannot do that."), ctx)
    assert v.say_reason is not None
    assert "banned phrase" in v.say_reason


def test_banned_phrase_in_thought_is_also_caught() -> None:
    ctx = DecisionValidationContext(banned_phrases=("objective marker",))
    v = validate_decision_content(
        _decision(thought="Heading for the objective marker now."), ctx
    )
    assert v.say_reason is not None


def test_the_curated_banned_list_from_commentary_style_md_is_loaded() -> None:
    """T4 asks for the list to be curated in commentary_style.md and LOADED
    from there, not hand-duplicated — this proves the parser actually found
    it, not just that a hardcoded tuple works."""
    phrases = banned_phrases()
    assert "as an ai" in phrases
    assert "rockstar" in phrases
    assert "objective marker" in phrases


def test_a_name_not_present_in_state_is_caught() -> None:
    ctx = DecisionValidationContext(present_names=frozenset({"Franklin"}))
    v = validate_decision_content(_decision(say="Trevor's got the rifle."), ctx)
    assert v.say_reason is not None
    assert "Trevor" in v.say_reason


def test_a_present_name_is_not_caught() -> None:
    ctx = DecisionValidationContext(present_names=frozenset({"Franklin", "Lamar"}))
    v = validate_decision_content(_decision(say="Lamar's riding shotgun."), ctx)
    assert v.say_reason is None


def test_names_check_suspended_lets_an_unidentified_friendly_pass() -> None:
    ctx = DecisionValidationContext(present_names=frozenset(), names_check_suspended=True)
    v = validate_decision_content(_decision(say="Trevor's got the rifle."), ctx)
    assert v.say_reason is None


def test_a_mismatched_mission_name_is_caught() -> None:
    ctx = DecisionValidationContext(
        mission_name="Franklin and Lamar",
        known_mission_names=frozenset({"Franklin and Lamar", "Complications", "Marriage Counseling"}),
    )
    v = validate_decision_content(_decision(say="This is obviously Complications."), ctx)
    assert v.say_reason is not None
    assert "Complications" in v.say_reason


def test_the_identified_missions_own_name_is_never_flagged() -> None:
    ctx = DecisionValidationContext(
        mission_name="Complications",
        known_mission_names=frozenset({"Complications", "Marriage Counseling"}),
    )
    v = validate_decision_content(_decision(say="Complications, same as the card says."), ctx)
    assert v.say_reason is None


def test_no_mission_identified_skips_the_mission_name_rule() -> None:
    ctx = DecisionValidationContext(
        mission_name=None,
        known_mission_names=frozenset({"Complications"}),
    )
    v = validate_decision_content(_decision(say="Feels like Complications to me."), ctx)
    assert v.say_reason is None


def test_dedupe_catches_a_near_repeat_of_a_recent_line() -> None:
    ctx = DecisionValidationContext(recent_lines=("Two stars and a hatchback. We'll call it even.",))
    v = validate_decision_content(
        _decision(say="Two stars and a hatchback, we'll call it even."), ctx
    )
    assert v.say_reason is not None
    assert "overlaps" in v.say_reason


def test_dedupe_threshold_is_point_six() -> None:
    assert DEDUPE_OVERLAP_THRESHOLD == 0.6


def test_dedupe_only_checks_the_last_five_lines() -> None:
    # The near-duplicate sits 6 lines back — outside DEDUPE_RECENT_WINDOW (5) —
    # followed by five unrelated fillers, so it must NOT trip the rule.
    recent = (
        "Two stars and a hatchback. We'll call it even.",
        *(f"filler line number {i}" for i in range(5)),
    )
    assert len(recent) == 6
    ctx = DecisionValidationContext(recent_lines=recent)
    v = validate_decision_content(
        _decision(say="Two stars and a hatchback, we'll call it even."), ctx
    )
    assert v.say_reason is None


def test_a_different_line_does_not_trip_dedupe() -> None:
    ctx = DecisionValidationContext(recent_lines=("Third green light in a row. Something's wrong.",))
    v = validate_decision_content(_decision(say="Six days to Sunday and I'm still parked."), ctx)
    assert v.say_reason is None


def test_a_roam_goal_naming_no_offered_id_is_caught() -> None:
    engine = RoamEngine()
    ctx = DecisionValidationContext(
        roam_offered_ids=("roam_the_block", "drive_to_landmark"),
        roam_model_choice=engine.model_choice,
    )
    # `model_choice` reads `self._offered`, which only `available()` sets —
    # mirror that here the same way `RoamEngine.note()` itself is rendered.
    engine._offered = ctx.roam_offered_ids
    v = validate_decision_content(
        _decision(goal="wandering around looking for something worth doing"), ctx
    )
    assert v.goal_reason is not None
    assert "names no offered roam id" in v.goal_reason


def test_a_roam_goal_naming_an_offered_id_is_not_caught() -> None:
    engine = RoamEngine()
    ctx = DecisionValidationContext(
        roam_offered_ids=("roam_the_block", "drive_to_landmark"),
        roam_model_choice=engine.model_choice,
    )
    engine._offered = ctx.roam_offered_ids
    v = validate_decision_content(_decision(goal="roam_the_block"), ctx)
    assert v.goal_reason is None


def test_no_roam_menu_offered_skips_the_goal_rule_entirely() -> None:
    ctx = DecisionValidationContext(roam_offered_ids=(), roam_model_choice=None)
    v = validate_decision_content(_decision(goal="pure invented prose"), ctx)
    assert v.goal_reason is None


# --- sanitize_decision: drop the line, keep the action --------------------------


def test_sanitize_blanks_say_and_keeps_everything_else() -> None:
    d = _decision(say="A line that must not survive.", action_type="wander_drive")
    v = DecisionViolation(say_reason="banned phrase 'rockstar'")
    sanitized = sanitize_decision(d, v)
    assert sanitized.say == ""
    assert sanitized.action.type == "wander_drive"
    assert sanitized.goal == d.goal
    assert sanitized.thought == d.thought


def test_sanitize_leaves_the_goal_alone_for_a_goal_only_violation() -> None:
    """Goal-id fallback is `main._begin_roam_goal`'s job (`available[0]`,
    logged as `goal_fallback`) — sanitizing here would be a second, competing
    fallback for the same problem."""
    d = _decision(say="Perfectly fine line.", goal="invented prose")
    v = DecisionViolation(goal_reason="goal 'invented prose' names no offered roam id (x)")
    sanitized = sanitize_decision(d, v)
    assert sanitized.say == "Perfectly fine line."
    assert sanitized.goal == "invented prose"


def test_sanitize_is_a_no_op_when_nothing_violated() -> None:
    d = _decision()
    assert sanitize_decision(d, DecisionViolation()) is d


# --- the real retry loop, via the fake brain (T4 acceptance) --------------------


def _roam_menu(state: Any) -> tuple[RoamEngine, tuple[str, ...]]:
    """A real, LIVE roam menu — not hand-picked ids that might drift out of
    the catalog. Returns the engine itself (so `.model_choice` reads the
    `_offered` `.available()` just populated, exactly as production's
    `RoamEngine.observe`/`note` does every tick) alongside the ids."""
    engine = RoamEngine()
    offers = engine.available(state)
    assert offers, "RoamEngine.available must never return an empty menu"
    return engine, tuple(o.id for o in offers)


def test_misbehave_mode_gets_exactly_one_regenerate_then_the_line_is_dropped() -> None:
    """The fake brain's `misbehave` mode never fixes itself (it does not
    check the RETRY marker), so this is the worst case: attempt 1 violates,
    attempt 2 (the one regenerate) ALSO violates — and the caller must not
    raise. It sanitizes and returns, action intact."""
    pricing = _pricing()
    tactical = TacticalBrain(_client(), pricing, on_cost=None)
    fake, _ = install(tactical, DirectorBrain(_client(), pricing, on_cost=None), mode="misbehave")

    state = make_state()
    engine, offered = _roam_menu(state)
    context = _context_for(state, roam_offered=offered)
    ctx = DecisionValidationContext(
        present_names=frozenset(),  # nobody present -> the misbehave `say` always trips this
        banned_phrases=banned_phrases(),
        recent_lines=(),
        roam_offered_ids=offered,
        roam_model_choice=engine.model_choice,
    )

    result = tactical.decide(context, mission_active=False, validation_ctx=ctx)

    assert len(fake.calls) == 2, "exactly one regenerate — not zero, not more"
    assert result.decision.say == "", "the line must be dropped after the second miss"
    # The action is NEVER discarded — misbehave mode's action is always `wait`.
    assert result.decision.action.type == "wait"
    # The goal is left as whatever the model said; `main._begin_roam_goal`'s
    # own `available[0]` fallback (unchanged, existing code — see
    # test_roam.py / findings.md R4) is what recovers from this, not this
    # function. It is still provably NOT an offered id:
    assert result.decision.goal not in offered


def test_a_content_violation_that_is_fixed_on_retry_succeeds_with_no_sanitizing() -> None:
    """Mirrors `test_fakebrain.py`'s `reject_once` schema-retry test, but for
    a CONTENT violation: first attempt names an absent character, second
    attempt (detects the real `_content_retry_correction` marker this ticket
    adds) plays it straight."""
    import support.fakebrain as fakebrain_mod

    def _bad_then_good(text: str, state) -> dict[str, Any]:
        if "RETRY —" in text:
            return {
                "thought": "Fixed it.",
                "say": "Quiet moment, nothing to report.",
                "mood": "chill",
                "action": {"type": "wait", "params": {"seconds": 2}},
                "goal": "wake up, find wheels, see what the day wants",
                "confidence": 0.6,
            }
        return {
            "thought": "Naming someone who never showed up.",
            "say": "Trevor's got the rifle covering me.",
            "mood": "chill",
            "action": {"type": "wait", "params": {"seconds": 2}},
            "goal": "wake up, find wheels, see what the day wants",
            "confidence": 0.6,
        }

    fakebrain_mod._MODES["bad_then_good"] = _bad_then_good
    try:
        pricing = _pricing()
        tactical = TacticalBrain(_client(), pricing, on_cost=None)
        install(
            tactical,
            DirectorBrain(_client(), pricing, on_cost=None),
            mode="bad_then_good",
        )
        state = make_state()
        context = _context_for(state)
        ctx = DecisionValidationContext(present_names=frozenset())
        result = tactical.decide(context, mission_active=False, validation_ctx=ctx)
        assert result.decision.say == "Quiet moment, nothing to report."
    finally:
        del fakebrain_mod._MODES["bad_then_good"]


def test_director_decide_also_runs_content_validation() -> None:
    """T4 applies to both tiers — the director is the one that actually
    WRITES `current_goal` (main._apply_decision), so a bad `goal` matters
    there too, not just for tactical."""
    pricing = _pricing()
    director = DirectorBrain(_client(), pricing, on_cost=None)
    install(TacticalBrain(_client(), pricing, on_cost=None), director, mode="misbehave")
    state = make_state()
    context = _context_for(state)
    ctx = DecisionValidationContext(present_names=frozenset())
    result = director.decide(context, None, None, ctx)
    assert result.decision.say == ""


def test_no_validation_ctx_means_no_content_validation_at_all() -> None:
    """Backward compatibility: a caller that passes nothing (tests, or a
    caller with no world snapshot) gets exactly today's behaviour — the
    misbehaving line comes straight through unsanitized."""
    pricing = _pricing()
    tactical = TacticalBrain(_client(), pricing, on_cost=None)
    fake, _ = install(tactical, DirectorBrain(_client(), pricing, on_cost=None), mode="misbehave")
    state = make_state()
    context = _context_for(state)
    result = tactical.decide(context, mission_active=False)
    assert len(fake.calls) == 1, "no validation_ctx -> no regenerate for content at all"
    assert result.decision.say != ""
