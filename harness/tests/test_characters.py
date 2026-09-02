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
    state = _state(["lamardavis"])          # Lamar is here. Nobody else is.
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
    state = _state(["lamardavis"])
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
    state = _state(["lamardavis", "davenorton"])
    allowed = present_names(state)
    assert "Dave" in allowed
    assert absent_names_mentioned("keep Dave alive", allowed) == []


def test_a_blip_only_name_grounds_the_line() -> None:
    """v1.11: `mission.entity_blips[].name` is a legitimate name source even
    when the ped himself is nowhere in `nearby.peds` — the exact case the
    "blue dot" fix exists for: he drove beyond the ~50 m ped-scan radius, but
    his named blip is still on the map."""
    state = _state([])  # nobody in nearby.peds at all
    body = state.model_dump(by_alias=True)
    body["mission"]["entity_blips"] = [
        {
            "pos": {"x": 500.0, "y": 500.0, "z": 20.0},
            "handle": 777,
            "color": "Blue",
            "is_route": False,
            "distance": 220.0,
            "name": "Lamar",
        }
    ]
    far_state = GameState.model_validate(body)
    allowed = present_names(far_state)
    assert "Lamar" in allowed
    assert absent_names_mentioned("Lamar's a ghost, his dot is way out there.", allowed) == []


def test_the_protagonist_may_always_name_himself() -> None:
    assert "Trevor" in present_names(_state([], protagonist="trevor"))
    assert absent_names_mentioned("Trevor doesn't negotiate.", present_names(_state([], protagonist="trevor"))) == []


def test_unknown_story_models_are_reported_not_guessed() -> None:
    state = _state(["lamardavis", "somebodynew"])
    assert unknown_story_models(state) == ["somebodynew"]
    assert name_for_model("somebodynew") is None
    assert name_for_model("ig_lamardavis") == "Lamar"
    assert name_for_model(None) is None


def test_the_bridge_spelling_of_a_model_resolves() -> None:
    """LIVE 2026-09-02: the bridge emits `lamardavis`, SHVDN's own spelling, not
    `ig_lamardavis`. The first version of this table keyed on the prefixed form
    only, so the check dropped a true line about the man 1 m away."""
    assert name_for_model("lamardavis") == "Lamar"
    assert name_for_model("ig_lamardavis") == "Lamar"
    assert name_for_model("IG_LamarDavis") == "Lamar"
    assert name_for_model("genstreet01amy") is None


def test_an_unnamed_friendly_silences_the_check() -> None:
    """A friendly we cannot name could BE the person the line names, so the
    check must not claim anybody is absent while one is standing there."""
    from wasted_harness.brain.characters import has_unidentified_friendly

    assert has_unidentified_friendly(_state(["somebodynew"])) is True
    assert has_unidentified_friendly(_state(["lamardavis"])) is False


def test_only_table_names_are_ever_challenged() -> None:
    """A capitalised word that is not one of ours is not our business."""
    assert absent_names_mentioned("Vinewood Boulevard was a mistake", set()) == []
    assert all(n[0].isupper() for n in CHECKED_NAMES)


def test_narrating_somebody_s_absence_is_allowed() -> None:
    """LIVE 2026-09-02: these two lines were dropped while Lamar had genuinely
    vanished mid-follow. They are TRUE, and they describe the most interesting
    thing on screen. The check exists to stop him asserting that an absent
    character is present and doing things — not to stop him saying they are
    gone."""
    allowed = present_names(_state([]))  # nobody around but Franklin himself
    for line in (
        "Lamar's a ghost now. Widening the loop, see if he turns up.",
        "Empty road, empty Lamar-shaped hole. Keep circling till he shows.",
        "Lost Lamar somewhere on the freeway.",
        "No sign of Lamar.",
    ):
        assert absent_names_mentioned(line, allowed) == [], f"should be allowed: {line!r}"


def test_asserting_an_absent_character_is_doing_something_is_still_caught() -> None:
    allowed = present_names(_state([]))
    for line in (
        "Trevor's got the rifle, I'm babysitting a Rapid GT.",
        "keep Dave alive",
        "Lester's on the phone again.",
    ):
        assert absent_names_mentioned(line, allowed), f"should be caught: {line!r}"
