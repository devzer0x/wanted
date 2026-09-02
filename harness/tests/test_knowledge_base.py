"""brain/knowledge_base.py: retrieval, ranking, rendering, mission-state hints.

Every JSON fixture below is written inline to `tmp_path` and STRUCTURALLY DERIVED from the
domain-file schema in the brief (`{"domain": ..., "items": [{"id", "category", "cue", "context",
"meaning", "suggested_action", "avoid", "urgency", "confidence", "exceptions", "sources"}],
"notes": ...}`) and the `mission_states.json` schema (`{"missions": [{"name", "order", "states":
[{"id", "objective_text", "visible_cues", "expected_action", "success_signal", "failure_signal",
"next_state", "recovery", "has_marker", "kind"}], "notes"}]}`). These are unit-test inputs, not
fabricated game recordings — `tests/fixtures/` is reserved for those (CLAUDE.md rule 1).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from wasted_harness.brain import knowledge_base as kb

# --- fixtures -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    """`load_domain`/`load_all`/`_load_mission_states` are lru_cache'd by name/no-args, so a stale
    hit from a previous test's tmp_path would otherwise leak into the next one."""
    kb.load_domain.cache_clear()
    kb.load_all.cache_clear()
    kb._load_mission_states.cache_clear()
    yield
    kb.load_domain.cache_clear()
    kb.load_all.cache_clear()
    kb._load_mission_states.cache_clear()


@pytest.fixture
def kdir(tmp_path, monkeypatch):
    """Point the module at an empty tmp knowledge dir (domain files + mission_states.json)."""
    monkeypatch.setattr(kb, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(kb, "MISSION_STATES_FILE", tmp_path / "mission_states.json")
    return tmp_path


def _write_domain(kdir, name: str, items: list[dict[str, Any]]) -> None:
    doc = {"domain": name, "items": items, "notes": "test fixture"}
    (kdir / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")


def _item(id_, *, category="general", cue="a cue", context="a context", meaning="a meaning",
          suggested_action="do the thing", avoid="avoid the thing", urgency="normal",
          confidence="medium") -> dict[str, Any]:
    return {
        "id": id_, "category": category, "cue": cue, "context": context, "meaning": meaning,
        "suggested_action": suggested_action, "avoid": avoid, "urgency": urgency,
        "confidence": confidence, "exceptions": "", "sources": ["test"],
    }


def _state(
    *,
    wanted=0, dead=False, arrested=False, in_vehicle=False, control_enabled=True,
    mission_active=False, cutscene_active=False, objective_blip=None,
    peds=(), vehicle=None, tick=0,
) -> SimpleNamespace:
    return SimpleNamespace(
        tick=tick,
        player=SimpleNamespace(
            wanted=wanted, dead=dead, arrested=arrested,
            in_vehicle=in_vehicle, control_enabled=control_enabled,
        ),
        mission=SimpleNamespace(
            active=mission_active, cutscene_active=cutscene_active, objective_blip=objective_blip,
        ),
        nearby=SimpleNamespace(peds=list(peds)),
        vehicle=vehicle,
    )


def _ids(items: list[dict[str, Any]]) -> set[str]:
    return {it["id"] for it in items}


# --- loading: missing / corrupt files never raise ------------------------------------------------


def test_missing_domain_file_returns_empty(kdir):
    assert kb.load_domain("hud_icons") == []


def test_corrupt_json_returns_empty_no_exception(kdir):
    (kdir / "combat.json").write_text("{not valid json at all", encoding="utf-8")
    assert kb.load_domain("combat") == []


def test_malformed_structure_returns_empty_no_exception(kdir):
    (kdir / "driving.json").write_text(json.dumps({"domain": "driving", "items": "not-a-list"}),
                                        encoding="utf-8")
    assert kb.load_domain("driving") == []


def test_load_all_mixes_present_and_absent_domains(kdir):
    _write_domain(kdir, "police_system", [_item("p1")])
    everything = kb.load_all()
    assert everything["police_system"] == [_item("p1")]
    assert everything["hud_icons"] == []
    assert set(everything.keys()) == set(kb.DOMAINS)


def test_bare_list_document_also_loads(kdir):
    (kdir / "world_common_sense.json").write_text(json.dumps([_item("w1")]), encoding="utf-8")
    assert kb.load_domain("world_common_sense") == [_item("w1")]


def test_missing_mission_states_file_gives_none(kdir):
    assert kb.mission_state_hint("Any Mission", _state()) is None


# --- select(): situational rules ------------------------------------------------------------------


def test_wanted_selects_police(kdir):
    _write_domain(kdir, "police_system", [_item("p1", urgency="critical")])
    _write_domain(kdir, "world_common_sense", [_item("w1")])
    result = kb.select(_state(wanted=3))
    assert "p1" in _ids(result)


def test_higher_wanted_pulls_more_police_items(kdir):
    items = [_item(f"p{i}", urgency="normal") for i in range(6)]
    _write_domain(kdir, "police_system", items)
    low = kb.select(_state(wanted=1))
    high = kb.select(_state(wanted=5))
    assert len(_ids(high) & {i["id"] for i in items}) > len(_ids(low) & {i["id"] for i in items})


def test_hostile_nearby_selects_combat_and_npc(kdir):
    _write_domain(kdir, "combat", [_item("c1")])
    _write_domain(kdir, "npc_entities", [_item("n1")])
    state = _state(peds=[{"relationship": "hostile", "distance": 4.0}])
    result = kb.select(state)
    assert {"c1", "n1"} <= _ids(result)


def test_damage_taken_selects_combat_without_a_hostile_ped(kdir):
    _write_domain(kdir, "combat", [_item("c1")])
    result = kb.select(_state(), damage_taken=True)
    assert "c1" in _ids(result)


def test_no_hostile_no_damage_does_not_select_combat(kdir):
    _write_domain(kdir, "combat", [_item("c1")])
    _write_domain(kdir, "activities_freeroam", [_item("a1")])
    result = kb.select(_state())
    assert "c1" not in _ids(result)


def test_in_vehicle_car_selects_driving_not_vehicles_or_aircraft(kdir):
    _write_domain(kdir, "driving", [_item("d1")])
    _write_domain(kdir, "vehicles", [_item("v1")])
    _write_domain(kdir, "aircraft_water", [_item("aw1")])
    vehicle = SimpleNamespace(display_name="Sentinel", **{"class": "Sedans"})
    result = kb.select(_state(in_vehicle=True, vehicle=vehicle))
    assert "d1" in _ids(result)
    assert "v1" not in _ids(result)
    assert "aw1" not in _ids(result)


def test_in_vehicle_bike_selects_vehicles_domain(kdir):
    _write_domain(kdir, "driving", [_item("d1")])
    _write_domain(kdir, "vehicles", [_item("v1")])
    vehicle = SimpleNamespace(display_name="Sanchez", **{"class": "Motorcycles"})
    result = kb.select(_state(in_vehicle=True, vehicle=vehicle))
    assert "v1" in _ids(result)
    assert "d1" not in _ids(result)


def test_in_vehicle_boat_selects_aircraft_water_domain(kdir):
    _write_domain(kdir, "driving", [_item("d1")])
    _write_domain(kdir, "aircraft_water", [_item("aw1")])
    vehicle = SimpleNamespace(display_name="Dinghy", **{"class": "Boats"})
    result = kb.select(_state(in_vehicle=True, vehicle=vehicle))
    assert "aw1" in _ids(result)
    assert "d1" not in _ids(result)


def test_mission_active_selects_failure_recovery_and_map_markers(kdir):
    _write_domain(kdir, "failure_recovery", [_item("f1")])
    _write_domain(kdir, "map_markers", [_item("m1")])
    result = kb.select(_state(mission_active=True, objective_blip=SimpleNamespace(pos={}, kind="coord")))
    assert {"f1", "m1"} <= _ids(result)


def test_mission_active_with_no_marker_prefers_follow_phase_items(kdir):
    """Within one urgency tier, the no-marker item wins on a tight budget."""
    normal = _item("m_normal", category="marker", cue="yellow blip",
                   urgency="normal", confidence="high")
    follow = _item("m_follow", category="follow", cue="no marker, follow the blue dot",
                    urgency="normal", confidence="low")
    _write_domain(kdir, "map_markers", [normal, follow])
    _write_domain(kdir, "failure_recovery", [])
    result = kb.select(_state(mission_active=True, objective_blip=None), budget_chars=120)
    # budget_chars=120 caps select() to a single item. `m_follow` has WORSE confidence, so it
    # only survives the cap because the no-marker preference beats confidence inside the tier.
    ids = [it["id"] for it in result]
    assert ids == ["m_follow"], ids


def test_the_follow_phase_preference_does_not_outrank_urgency(kdir):
    """Four stars during a follow phase: "you are being hunted" reaches him first.

    The preference is a tie-break inside an urgency tier, not a trump card over it. If it
    overrode urgency, a `low`-urgency map note would push a `critical` police line out of a
    tight budget — which would make the `urgency` field decorative in the one situation it
    exists to arbitrate.
    """
    _write_domain(kdir, "map_markers", [
        _item("m_follow", category="follow", cue="no marker, follow the blue dot",
              urgency="low", confidence="high"),
    ])
    _write_domain(kdir, "police_system", [
        _item("p_crit", category="wanted", cue="four stars, helicopter overhead",
              urgency="critical", confidence="high"),
    ])
    _write_domain(kdir, "failure_recovery", [])
    result = kb.select(
        _state(mission_active=True, objective_blip=None, wanted=4), budget_chars=120
    )
    assert [it["id"] for it in result] == ["p_crit"]


def test_cutscene_selects_only_the_cutscene_set(kdir):
    _write_domain(kdir, "controls_interactions",
                   [_item("cut1", category="cutscene", cue="cutscene playing, black bars")])
    _write_domain(kdir, "police_system", [_item("p1")])
    state = _state(wanted=5, cutscene_active=True)
    result = kb.select(state)
    assert _ids(result) == {"cut1"}


def test_locked_control_selects_only_the_cutscene_set(kdir):
    _write_domain(kdir, "controls_interactions",
                   [_item("cut1", category="cutscene", cue="control disabled during a scripted event")])
    _write_domain(kdir, "combat", [_item("c1")])
    state = _state(control_enabled=False, peds=[{"relationship": "hostile", "distance": 2.0}])
    result = kb.select(state)
    assert _ids(result) == {"cut1"}


def test_dead_selects_failure_recovery_only(kdir):
    _write_domain(kdir, "failure_recovery", [_item("f1")])
    _write_domain(kdir, "police_system", [_item("p1")])
    result = kb.select(_state(dead=True, wanted=5))
    assert _ids(result) == {"f1"}


def test_arrested_selects_failure_recovery_only(kdir):
    _write_domain(kdir, "failure_recovery", [_item("f1")])
    _write_domain(kdir, "combat", [_item("c1")])
    result = kb.select(_state(arrested=True), damage_taken=True)
    assert _ids(result) == {"f1"}


def test_free_roam_selects_activities_and_world_common_sense(kdir):
    _write_domain(kdir, "activities_freeroam", [_item("a1")])
    _write_domain(kdir, "world_common_sense", [_item("w1")])
    result = kb.select(_state())
    assert {"a1", "w1"} <= _ids(result)


def test_free_roam_rotates_across_ticks_but_is_stable_within_a_window(kdir):
    pool = [_item(f"x{i}") for i in range(8)]
    _write_domain(kdir, "activities_freeroam", pool)
    _write_domain(kdir, "world_common_sense", [])
    first = _ids(kb.select(_state(tick=0)))
    again = _ids(kb.select(_state(tick=1)))  # same rotation window
    later = _ids(kb.select(_state(tick=1000)))  # a different rotation window
    assert first == again, "the same short window must not flap tick to tick"
    assert first != later, "a later window must rotate to a different slice"


def test_world_common_sense_floor_present_even_when_something_else_fired(kdir):
    _write_domain(kdir, "police_system", [_item("p1")])
    _write_domain(kdir, "world_common_sense", [_item("w1"), _item("w2")])
    result = kb.select(_state(wanted=1))
    assert {"w1", "w2"} <= _ids(result)


# --- ranking / dedup -----------------------------------------------------------------------------


def test_ranking_order_urgency_then_confidence_then_id():
    items = [
        _item("z_high_high", urgency="high", confidence="high"),
        _item("a_high_high", urgency="high", confidence="high"),
        _item("normal_high", urgency="normal", confidence="high"),
        _item("critical_low", urgency="critical", confidence="low"),
        _item("high_medium", urgency="high", confidence="medium"),
    ]
    ranked = kb._rank(items)
    assert [it["id"] for it in ranked] == [
        "critical_low",       # urgency wins over everything else
        "a_high_high",        # high/high, id tiebreak: a < z
        "z_high_high",
        "high_medium",        # high/medium ranks after high/high
        "normal_high",        # normal loses to every high, regardless of confidence
    ]


def test_dedup_keeps_first_occurrence_by_id():
    first = _item("dup", cue="first copy")
    second = _item("dup", cue="second copy")
    other = _item("unique")
    out = kb._dedup([first, second, other])
    assert [it["id"] for it in out] == ["dup", "unique"]
    assert out[0]["cue"] == "first copy"


def test_dedup_does_not_collapse_items_missing_an_id():
    a = {"cue": "a", "meaning": "m"}
    b = {"cue": "b", "meaning": "m"}
    out = kb._dedup([a, b])
    assert out == [a, b]


# --- render() -------------------------------------------------------------------------------------


def test_render_empty_list_is_empty_string():
    assert kb.render([]) == ""


def test_render_includes_cue_meaning_do_avoid():
    text = kb.render([_item("i1", cue="red blip", meaning="cop is close",
                             suggested_action="slow down", avoid="ramming")])
    assert text.startswith(kb.KNOWLEDGE_HEADER)
    assert "red blip -> cop is close" in text
    assert "do: slow down" in text
    assert "avoid: ramming" in text


def test_render_respects_budget_and_never_splits_an_item():
    items = [_item(f"i{i}", cue=f"cue{i}", meaning=f"meaning{i}") for i in range(20)]
    full = kb.render(items, budget_chars=10_000)
    one_line = full.splitlines()[1]
    # a budget that fits the header plus exactly one full line, plus a little slack that is
    # NOT enough for a second full line.
    tight_budget = len(kb.KNOWLEDGE_HEADER) + 1 + len(one_line) + 3
    truncated = kb.render(items, budget_chars=tight_budget)
    assert len(truncated) <= tight_budget
    lines = truncated.splitlines()
    assert lines[0] == kb.KNOWLEDGE_HEADER
    assert lines[1] == one_line, "must be a whole line, never a mid-word fragment"
    assert len(lines) == 2, "the slack must not have been enough to sneak in a second item"


def test_render_returns_empty_when_budget_too_small_for_even_the_header_and_one_item():
    items = [_item("i1", cue="a very very very very long cue that will not fit", meaning="m")]
    assert kb.render(items, budget_chars=5) == ""


# --- mission_state_hint() --------------------------------------------------------------------------


def _write_mission_states(kdir, missions: list[dict[str, Any]]) -> None:
    kdir_mission_states = kdir / "mission_states.json"
    kdir_mission_states.write_text(json.dumps({"missions": missions}), encoding="utf-8")


def _mission_state(id_, *, objective_text="", has_marker=True, expected_action="", recovery="",
                    kind="drive") -> dict[str, Any]:
    return {
        "id": id_, "objective_text": objective_text, "visible_cues": "", "expected_action":
        expected_action, "success_signal": "", "failure_signal": "", "next_state": None,
        "recovery": recovery, "has_marker": has_marker, "kind": kind,
    }


def test_mission_state_hint_prefers_no_marker_state_when_no_objective_blip(kdir):
    marker_state = _mission_state("s1", objective_text="Drive to the meet", has_marker=True)
    follow_state = _mission_state(
        "s2", objective_text="Follow Lamar's car", has_marker=False,
        expected_action="follow_entity nearest friendly", recovery="look for the blue dot",
    )
    _write_mission_states(kdir, [{"name": "Franklin and Lamar", "order": 2,
                                   "states": [marker_state, follow_state], "notes": ""}])
    state = _state(mission_active=True, objective_blip=None)
    hint = kb.mission_state_hint("Franklin and Lamar", state)
    assert hint is not None
    assert "Follow Lamar's car" in hint
    assert "follow_entity nearest friendly" in hint
    assert "look for the blue dot" in hint


def test_mission_state_hint_falls_back_to_first_state_when_marker_present(kdir):
    marker_state = _mission_state("s1", objective_text="Drive to the meet", has_marker=True)
    follow_state = _mission_state("s2", objective_text="Follow Lamar's car", has_marker=False)
    _write_mission_states(kdir, [{"name": "Franklin and Lamar", "order": 2,
                                   "states": [marker_state, follow_state], "notes": ""}])
    state = _state(mission_active=True, objective_blip=SimpleNamespace(pos={}, kind="coord"))
    hint = kb.mission_state_hint("Franklin and Lamar", state)
    assert hint is not None
    assert "Drive to the meet" in hint


def test_mission_state_hint_falls_back_to_first_state_when_no_marker_state_exists(kdir):
    only_marker_state = _mission_state("s1", objective_text="Chase the target", has_marker=True)
    _write_mission_states(kdir, [{"name": "Chop Shop", "order": 10,
                                   "states": [only_marker_state], "notes": ""}])
    state = _state(mission_active=True, objective_blip=None)
    hint = kb.mission_state_hint("Chop Shop", state)
    assert hint == "MISSION STATE: Chase the target"


def test_mission_state_hint_unknown_mission_returns_none(kdir):
    _write_mission_states(kdir, [{"name": "Chop Shop", "order": 10,
                                   "states": [_mission_state("s1")], "notes": ""}])
    assert kb.mission_state_hint("Some Other Mission", _state()) is None
    assert kb.mission_state_hint(None, _state()) is None


def test_mission_state_hint_is_case_and_whitespace_insensitive(kdir):
    _write_mission_states(kdir, [{"name": "Franklin  and Lamar", "order": 2,
                                   "states": [_mission_state("s1", objective_text="Go")],
                                   "notes": ""}])
    hint = kb.mission_state_hint("  franklin AND   lamar ", _state())
    assert hint == "MISSION STATE: Go"

def test_mission_only_items_stay_out_of_free_roam(kdir):
    """Measured against the real 651-item base: `do_not_abandon_mission_vehicle` is tagged
    `critical`, urgency is the first sort key, and free roam has no mission — so without this
    filter the same two mission-only lines won the top slots on every idle tick and the
    rotation below them never got seen."""
    _write_domain(kdir, "world_common_sense", [
        _item("wcs_mission_only", urgency="critical",
              context="Missions that hand you a specific vehicle, like a getaway car."),
        _item("wcs_general", urgency="normal",
              context="Any time you approach a parked car on the street."),
    ])
    _write_domain(kdir, "activities_freeroam", [])
    ids = _ids(kb.select(_state(), budget_chars=1800))
    assert "wcs_general" in ids
    assert "wcs_mission_only" not in ids, "a mission-only rule has no business in free roam"


def test_a_mission_only_item_comes_back_once_a_mission_starts(kdir):
    _write_domain(kdir, "world_common_sense", [
        _item("wcs_mission_only", urgency="critical",
              context="Missions that hand you a specific vehicle, like a getaway car."),
    ])
    _write_domain(kdir, "map_markers", [])
    _write_domain(kdir, "failure_recovery", [])
    assert "wcs_mission_only" in _ids(kb.select(_state(mission_active=True), budget_chars=1800))


def test_an_item_that_says_it_applies_in_free_roam_too_is_kept(kdir):
    """The filter reads the item's own scope prose. "In free roam or on a mission" is not
    mission-gated, and dropping it would be the expensive direction of this heuristic."""
    _write_domain(kdir, "world_common_sense", [
        _item("wcs_both", urgency="high",
              context="In free roam or during a mission, sirens mean police are close."),
    ])
    _write_domain(kdir, "activities_freeroam", [])
    assert "wcs_both" in _ids(kb.select(_state(), budget_chars=1800))


def test_an_unexecutable_state_says_so_before_it_says_what_to_do():
    """25 mission states need an action the agent does not have (an aimed shot at one named thing,
    a menu purchase, sustained melee). Rendering "do: shoot the alarm box" at an agent with no
    aim and no fire action is how he narrates a shot he never took."""
    hint = kb._render_state_hint({
        "objective_text": "Aim at the hostages to move them",
        "expected_action": "aim and herd them",
        "executable": False,
        "blocked_reason": "No aim primitive exists",
        "recovery": "stand still and let the script run",
    })
    assert "YOU CANNOT DO THIS STEP DIRECTLY" in hint
    assert "No aim primitive exists" in hint
    assert hint.index("CANNOT") < hint.index("do:"), "the incapacity has to land before the action"
    assert "do not claim you did it" in hint


def test_a_bounded_wait_state_carries_its_ceiling():
    hint = kb._render_state_hint({
        "objective_text": "Wait for Trevor",
        "expected_action": "hold position",
        "kind": "wait",
        "max_wait_s": 120,
        "recovery": "reposition then escalate",
    })
    assert "up to ~120s, then escalate" in hint


def test_a_verbose_hint_is_trimmed_on_a_word_boundary():
    hint = kb._render_state_hint({
        "objective_text": "x",
        "expected_action": "y",
        "executable": False,
        "blocked_reason": "reason " * 400,
        "recovery": "z",
    })
    assert len(hint) <= kb.MISSION_HINT_MAX_CHARS + 4
    assert hint.endswith(" ...")
    assert not hint[:-4].endswith("reaso"), "must not cut mid-word"


def test_a_verbose_item_keeps_its_action_instead_of_losing_it():
    """Clamping the joined LINE kept the description and threw away `do:` — the only part that
    changes what he does. Clamping each field keeps all four parts and trims the wordy ones."""
    line = kb._render_line({
        "cue": "c " * 200,
        "meaning": "m " * 200,
        "suggested_action": "follow_entity on the moving blip",
        "avoid": "drive_to a stale coordinate",
    })
    assert "| do: follow_entity on the moving blip" in line
    assert "| avoid: drive_to a stale coordinate" in line
    assert " ..." in line, "the wordy fields must be visibly trimmed"
    assert len(line) < 700


def test_one_wordy_item_cannot_eat_the_whole_budget(kdir):
    """Measured on the real base: the naive-colour-rule refutation runs past 500 chars, and
    unclamped it crowded every other item out of a 1800-char budget."""
    _write_domain(kdir, "world_common_sense", [
        _item("wordy", urgency="critical", context="Any time.",
              meaning="x " * 400, cue="y " * 400),
        _item("short_a", urgency="critical", context="Any time."),
        _item("short_b", urgency="critical", context="Any time."),
    ])
    _write_domain(kdir, "activities_freeroam", [])
    out = kb.render(kb.select(_state(), budget_chars=1800), budget_chars=1800)
    assert out.count("\n") >= 3, "the wordy item must not be the only one that fits"

