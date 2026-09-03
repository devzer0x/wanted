"""The fake brain: deterministic, zero-network, and pluggable into the REAL
`TacticalBrain`/`DirectorBrain` retry loop by replacing only `_call_api`
(see `tests/support/fakebrain.py`'s module docstring for why that is the seam).
"""

from __future__ import annotations

from pathlib import Path

import anthropic
import pytest
from support.fakebrain import (
    FakeBilledCall,
    FakeBrainContextError,
    install,
    parse_state_from_context,
)
from support.states import make_state

from wasted_harness.brain.director import DirectorBrain
from wasted_harness.brain.schemas import BRIDGE_TASKS, PRIMITIVES
from wasted_harness.brain.tactical import DecisionFailedError, TacticalBrain
from wasted_harness.budget import Pricing

PRICING_PATH = Path(__file__).resolve().parents[1] / "config" / "pricing.yaml"


def _pricing() -> Pricing:
    return Pricing.load(PRICING_PATH)


def _client() -> anthropic.Anthropic:
    # Never called: TacticalBrain/DirectorBrain.__init__ store this client but
    # do not use it until `.decide()` reaches `self._call_api.run(...)`, and
    # every test here has already replaced `_call_api` with a fake before
    # calling `.decide()`. Building the real client makes no network call.
    return anthropic.Anthropic(api_key="sk-fake-not-used")


def _context_for(state, *, roam_current: str | None = None, roam_offered: tuple[str, ...] = ()) -> str:
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


# --- parse_state_from_context --------------------------------------------------


def test_parse_state_from_context_recovers_the_real_gamestate() -> None:
    state = make_state(pos=(12.0, 34.0, 0.0), wanted=2)
    text = _context_for(state)
    recovered_text, recovered_state = parse_state_from_context(text)
    assert recovered_text == text
    assert recovered_state.player.pos.x == 12.0
    assert recovered_state.player.wanted == 2


def test_parse_state_from_context_works_on_director_content_blocks() -> None:
    state = make_state()
    text = _context_for(state)
    content = [{"type": "text", "text": text}, {"type": "image", "source": {}}]
    _, recovered = parse_state_from_context(content)
    assert recovered.tick == state.tick


def test_parse_state_from_context_refuses_to_invent_a_state() -> None:
    with pytest.raises(FakeBrainContextError):
        parse_state_from_context("no state block here at all")


def test_parse_state_from_context_refuses_a_director_block_with_no_text() -> None:
    with pytest.raises(FakeBrainContextError):
        parse_state_from_context([{"type": "image", "source": {}}])


# --- policy mode: the brief's own three rules ----------------------------------


def test_policy_picks_available_index_zero_in_roam() -> None:
    state = make_state()
    text = _context_for(state, roam_offered=("drive_to_landmark", "roam_the_block"))
    call = FakeBilledCall(mode="policy")
    result = call.run("claude-haiku-4-5-20251001", "prefix", text)
    assert result.decision.goal == "drive_to_landmark"
    assert result.decision.action.type not in BRIDGE_TASKS or result.decision.action.type in PRIMITIVES


def test_policy_echoes_the_locked_roam_goal_exactly() -> None:
    state = make_state()
    text = _context_for(state, roam_current="bike_hills")
    call = FakeBilledCall(mode="policy")
    result = call.run("claude-haiku-4-5-20251001", "prefix", text)
    assert result.decision.goal == "bike_hills"


def test_policy_drives_toward_the_objective_in_a_vehicle_mission() -> None:
    state = make_state(
        mission_active=True, in_vehicle=True, objective_blip={"pos": (100.0, 200.0, 0.0)}
    )
    text = _context_for(state)
    call = FakeBilledCall(mode="policy")
    result = call.run("claude-haiku-4-5-20251001", "prefix", text)
    assert result.decision.action.type == "drive_to"
    assert result.decision.action.params.x == 100.0
    assert result.decision.action.params.y == 200.0


def test_policy_walks_toward_the_objective_on_foot_in_a_mission() -> None:
    state = make_state(
        mission_active=True, in_vehicle=False, objective_blip={"pos": (5.0, 6.0, 0.0)}
    )
    text = _context_for(state)
    call = FakeBilledCall(mode="policy")
    result = call.run("claude-haiku-4-5-20251001", "prefix", text)
    assert result.decision.action.type == "walk_to"
    assert result.decision.action.params.x == 5.0


def test_policy_waits_in_a_cutscene() -> None:
    state = make_state(cutscene_active=True, mission_active=True)
    text = _context_for(state)
    call = FakeBilledCall(mode="policy")
    result = call.run("claude-haiku-4-5-20251001", "prefix", text)
    assert result.decision.action.type == "wait"


def test_policy_is_deterministic_across_repeated_runs() -> None:
    state = make_state(pos=(7.0, 8.0, 0.0), wanted=1)
    text = _context_for(state, roam_offered=("roam_the_block",))
    a = FakeBilledCall(mode="policy").run("m", "p", text)
    b = FakeBilledCall(mode="policy").run("m", "p", text)
    assert a.decision.model_dump() == b.decision.model_dump()


def test_policy_action_types_are_always_in_the_real_catalog() -> None:
    from wasted_harness.brain.schemas import ACTION_TYPES

    for over in (
        {"cutscene_active": True, "mission_active": True},
        {"dead": True},
        {"mission_active": True, "objective_blip": {"pos": (1.0, 1.0, 0.0)}},
        {},
    ):
        state = make_state(**over)
        text = _context_for(state, roam_offered=("roam_the_block",))
        result = FakeBilledCall(mode="policy").run("m", "p", text)
        assert result.decision.action.type in ACTION_TYPES


# --- misbehave mode: stresses the harness-level guards, not the schema ---------


def test_misbehave_thought_gets_soft_truncated_by_the_real_validator() -> None:
    state = make_state()
    text = _context_for(state, roam_offered=("roam_the_block",))
    result = FakeBilledCall(mode="misbehave").run("m", "p", text)
    words = result.decision.thought.split()
    assert len(words) == 40  # soft-truncated to the contract cap
    assert result.decision.thought.endswith("…")


def test_misbehave_goal_is_prose_matching_no_offered_id() -> None:
    state = make_state()
    text = _context_for(state, roam_offered=("drive_to_landmark", "roam_the_block"))
    result = FakeBilledCall(mode="misbehave").run("m", "p", text)
    assert " " in result.decision.goal.strip()  # prose, not a bare id
    assert "drive_to_landmark" not in result.decision.goal
    assert "roam_the_block" not in result.decision.goal


def test_misbehave_names_someone_absent_from_state() -> None:
    from wasted_harness.brain.characters import absent_names_mentioned, present_names

    state = make_state()  # no nearby peds, no entity blips
    text = _context_for(state)
    result = FakeBilledCall(mode="misbehave").run("m", "p", text)
    offenders = absent_names_mentioned(result.decision.say, present_names(state))
    assert offenders, "the misbehaviour line must name someone not present"


def test_misbehave_names_someone_genuinely_absent_even_with_a_crew_nearby() -> None:
    """The picked name must avoid whoever IS present, or the check it exists to
    trip would (correctly) let the line through."""
    from wasted_harness.brain.characters import absent_names_mentioned, present_names

    state = make_state(
        nearby_peds=[
            {
                "handle": 1, "model": "lamardavis", "distance": 3.0,
                "relationship": "friendly", "pos": {"x": 1.0, "y": 1.0, "z": 0.0},
            }
        ]
    )
    text = _context_for(state)
    result = FakeBilledCall(mode="misbehave").run("m", "p", text)
    assert "Lamar" not in result.decision.say
    offenders = absent_names_mentioned(result.decision.say, present_names(state))
    assert offenders


def test_misbehave_output_is_still_pydantic_valid() -> None:
    """The whole point: it stresses harness-level guards, not the schema."""
    state = make_state()
    text = _context_for(state, roam_offered=("roam_the_block",))
    result = FakeBilledCall(mode="misbehave").run("m", "p", text)
    assert 0.0 <= result.decision.confidence <= 1.0


# --- reject_once mode: exercises the REAL pydantic rejection + retry path -----


def test_reject_once_fails_pydantic_on_the_first_attempt() -> None:
    import pydantic

    state = make_state()
    text = _context_for(state, roam_offered=("roam_the_block",))
    with pytest.raises(pydantic.ValidationError):
        FakeBilledCall(mode="reject_once").run("m", "p", text)


def test_reject_once_succeeds_once_the_retry_marker_is_present() -> None:
    state = make_state()
    text = _context_for(state, roam_offered=("roam_the_block",)) + (
        "\n\nRETRY — your previous response was REJECTED by the schema validator:\n"
        "  ValidationError: say is 25 words; contract max is 20\n"
    )
    result = FakeBilledCall(mode="reject_once").run("m", "p", text)
    assert result.decision.goal == "roam_the_block"


def test_unknown_mode_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown fake-brain mode"):
        FakeBilledCall(mode="not-a-real-mode")


# --- install(): plugged into the REAL TacticalBrain/DirectorBrain retry loop --


def test_install_makes_tactical_brain_decide_end_to_end_with_no_network() -> None:
    pricing = _pricing()
    tactical = TacticalBrain(_client(), pricing, on_cost=None)
    fake, _ = install(tactical, DirectorBrain(_client(), pricing, on_cost=None), mode="policy")

    state = make_state()
    context = _context_for(state, roam_offered=("roam_the_block",))
    result = tactical.decide(context, mission_active=False)

    assert result.decision.goal == "roam_the_block"
    assert fake.calls, "the fake was never reached"


def test_install_makes_director_brain_decide_end_to_end_with_no_network() -> None:
    pricing = _pricing()
    director = DirectorBrain(_client(), pricing, on_cost=None)
    _, fake = install(TacticalBrain(_client(), pricing, on_cost=None), director, mode="policy")

    state = make_state(mission_active=True, in_vehicle=True, objective_blip={"pos": (1.0, 2.0, 0.0)})
    context = _context_for(state)
    result = director.decide(context)

    assert result.decision.action.type == "drive_to"
    assert fake.calls


def test_install_exercises_the_real_retry_loop_and_recovers() -> None:
    """`TacticalBrain.decide`'s own two-attempt loop, not a re-description of it:
    attempt 1 is schema-rejected by the real validator, attempt 2 (fed the
    real `_retry_correction` coaching) succeeds."""
    pricing = _pricing()
    tactical = TacticalBrain(_client(), pricing, on_cost=None)
    install(tactical, DirectorBrain(_client(), pricing, on_cost=None), mode="reject_once")

    state = make_state()
    context = _context_for(state, roam_offered=("roam_the_block",))
    result = tactical.decide(context, mission_active=False)  # must NOT raise
    assert result.decision.goal == "roam_the_block"


def test_install_exhausts_the_retry_and_raises_when_both_attempts_misbehave(monkeypatch) -> None:
    """A mode that fails BOTH attempts must surface as `DecisionFailedError`
    (CONTRACTS §2: "retried once, then the reflex layer keeps control"), the
    same as two real API failures."""
    import support.fakebrain as fakebrain_mod

    def _always_bad(text: str, state) -> dict:
        return {
            "thought": "still bad",
            "say": fakebrain_mod._OVERLONG_SAY.strip(),
            "mood": "chill",
            "action": {"type": "wait", "params": {"seconds": 1}},
            "goal": "still over the limit",
            "confidence": 0.5,
        }

    monkeypatch.setitem(fakebrain_mod._MODES, "always_bad", _always_bad)
    pricing = _pricing()
    tactical = TacticalBrain(_client(), pricing, on_cost=None)
    install(tactical, DirectorBrain(_client(), pricing, on_cost=None), mode="always_bad")

    state = make_state()
    context = _context_for(state, roam_offered=("roam_the_block",))
    with pytest.raises(DecisionFailedError):
        tactical.decide(context, mission_active=False)
