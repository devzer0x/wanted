"""DecisionModel enforces CONTRACTS §2 exactly (pure-function tests, constructed inputs)."""

import pytest
from pydantic import ValidationError

from wasted_harness.brain.schemas import (
    ACTION_TYPES,
    BRIDGE_TASKS,
    MOODS,
    PRIMITIVES,
    ActionModel,
    DecisionModel,
)


def _valid(**overrides) -> dict:
    base = {
        "thought": "Two stars, tank's fine, freeway's right there.",
        "say": "Relax. I've done this twice.",
        "mood": "hyped",
        "action": {"type": "flee_police", "params": {}},
        "goal": "lose the cops by the port",
        "confidence": 0.7,
    }
    base.update(overrides)
    return base


def test_mood_enum_exact() -> None:
    assert MOODS == ("chill", "bored", "hyped", "scared", "smug")
    for mood in MOODS:
        DecisionModel.model_validate(_valid(mood=mood))
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(_valid(mood="angry"))


def test_action_catalog_exact() -> None:
    assert len(BRIDGE_TASKS) == 11
    assert PRIMITIVES == (
        "look_around",
        "brake_tap",
        "swerve",
        "reverse_out",
        "press_prompt_key",
        "wait",
        "radio",
        "horn",
    )
    for t in ACTION_TYPES:
        ActionModel.model_validate({"type": t, "params": {}})
    with pytest.raises(ValidationError):
        ActionModel.model_validate({"type": "teleport", "params": {}})
    with pytest.raises(ValidationError):
        ActionModel.model_validate({"type": "god_mode", "params": {}})


def test_word_limits_enforced() -> None:
    with pytest.raises(ValidationError, match="40"):
        DecisionModel.model_validate(_valid(thought=" ".join(["word"] * 41)))
    with pytest.raises(ValidationError, match="20"):
        DecisionModel.model_validate(_valid(say=" ".join(["word"] * 21)))
    with pytest.raises(ValidationError, match="12"):
        DecisionModel.model_validate(_valid(goal=" ".join(["word"] * 13)))
    # At the limits is fine.
    DecisionModel.model_validate(
        _valid(
            thought=" ".join(["w"] * 40),
            say=" ".join(["w"] * 20),
            goal=" ".join(["w"] * 12),
        )
    )


def test_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(_valid(confidence=1.2))
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(_valid(confidence=-0.1))
    DecisionModel.model_validate(_valid(confidence=0.0))
    DecisionModel.model_validate(_valid(confidence=1.0))


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(_valid(cheat_mode=True))


def test_prefixes_exceed_cache_minimums_by_char_heuristic() -> None:
    """Offline sanity floor only: ~4 chars/token is conservative for English.

    The REAL check is count_tokens at startup (verify_prefix_cacheable); this
    guards against someone gutting the prompt files without running --check.
    """
    from wasted_harness.brain.prompts import director_static_prefix, tactical_static_prefix

    assert len(tactical_static_prefix()) > 4096 * 4
    assert len(director_static_prefix()) > 1024 * 4
