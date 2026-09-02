"""Grounding: the agent may only name people who are actually there.

Every line quoted here was said on the live stream on 2026-09-02 while he was
playing `armenian1` with Lamar beside him and neither Dave nor a sniping Trevor
anywhere in the world.
"""

from __future__ import annotations

from wasted_harness.brain.characters import (
    CHECKED_NAMES,
    absent_names_mentioned,
    name_for_model,
    present_names,
    unknown_story_models,
)
from wasted_harness.bridge_client import GameState


def _state(models: list[str], protagonist: str = "franklin") -> GameState:
    body = {
        "ts": "2026-09-02T15:00:00Z",
        "tick": 1000,
        "player": {
            "pos": {"x": 0.0, "y": 0.0, "z": 0.0},
            "heading": 0.0,
            "health": 200,
            "max_health": 200,
            "armor": 0,
            "wanted": 0,
            "cash": 78,
            "dead": False,
            "arrested": False,
            "in_vehicle": False,
            "control_enabled": True,
            "protagonist": protagonist,
        },
        "vehicle": None,
        "location": {"street": "Del Perro Fwy", "zone": "Pacific Bluffs"},
        "world": {"clock": "09:02", "weather": "CLEAR", "timescale": 1.0},
        "mission": {
            "active": True,
            "random_event_active": False,
            "cutscene_active": False,
            "objective_blip": None,
            "starts": [],
            "route_blips": [],
            "script": "Armenian1",
        },
        "nearby": {
            "vehicles": [],
            "peds": [
                {
                    "handle": 500 + i,
                    "model": m,
                    "distance": 2.0 + i,
                    "relationship": "friendly",
                    "pos": {"x": 1.0, "y": 0.0, "z": 0.0},
                    "in_vehicle_handle": None,
                }
                for i, m in enumerate(models)
            ],
        },
        "last_task": {"id": None, "type": None, "status": "idle", "detail": ""},
        "bridge": {"version": "1.2.0", "edition": "legacy"},
    }
    return GameState.model_validate(body)


def test_the_lines_he_actually_said_are_caught() -> None:
    state = _state(["ig_lamardavis"])          # Lamar is here. Nobody else is.
    allowed = present_names(state)
    assert allowed == {"Franklin", "Lamar"}

    for line in (
        "survive the ambush, keep Dave alive",
        "Dave who? I've got Lamar and eight sports cars I'm ignoring.",
        "Wrong body, wrong day. Trevor's got the rifle, I'm babysitting a Rapid GT.",
        "Objective wants Trevor on a sniper rifle.",
    ):
        assert absent_names_mentioned(line, allowed), f"should have been caught: {line!r}"


def test_legitimate_lines_are_left_alone() -> None:
    """The check must never fire on a street, a zone, a car, or himself."""
    state = _state(["ig_lamardavis"])
    allowed = present_names(state)
    for line in (
        "Cabrio's moving. Not losing him this time.",
        "Lamar's already rolling in that 9F. Tail him now.",
        "Del Perro Freeway, Pacific Bluffs, and a Rapid GT I did not pay for.",
        "Franklin doesn't miss this one.",
        "Chasing the blue dot down the freeway.",
    ):
        assert absent_names_mentioned(line, allowed) == [], f"false positive on: {line!r}"


def test_a_character_who_is_present_may_be_named() -> None:
    state = _state(["ig_lamardavis", "ig_davenorton"])
    allowed = present_names(state)
    assert "Dave" in allowed
    assert absent_names_mentioned("keep Dave alive", allowed) == []


def test_the_protagonist_may_always_name_himself() -> None:
    assert "Trevor" in present_names(_state([], protagonist="trevor"))
    assert absent_names_mentioned("Trevor doesn't negotiate.", present_names(_state([], protagonist="trevor"))) == []


def test_unknown_story_models_are_reported_not_guessed() -> None:
    state = _state(["ig_lamardavis", "ig_somebodynew"])
    assert unknown_story_models(state) == ["ig_somebodynew"]
    assert name_for_model("ig_somebodynew") is None
    assert name_for_model("ig_lamardavis") == "Lamar"
    assert name_for_model(None) is None


def test_only_table_names_are_ever_challenged() -> None:
    """A capitalised word that is not one of ours is not our business."""
    assert absent_names_mentioned("Vinewood Boulevard was a mistake", set()) == []
    assert all(n[0].isupper() for n in CHECKED_NAMES)
