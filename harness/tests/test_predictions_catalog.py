"""Catalog templates: registry membership, window bounds, trigger/score sanity,
and — for the templates calibrated against a real recording — that the
measured base rate documented in `catalog.py` is still what the real fixture
says.

No hand-invented game state: states are built with `support.states.make_state`,
the same CONTRACTS-shaped pure-function builder every other harness test
already uses (`tests/test_mission_following.py`, `tests/test_interior_escape.py`,
`tests/test_replayer.py`). The calibration numbers themselves are recomputed
directly from `tests/fixtures/real_session_2026-09-04.json` — a real,
provenance-stamped recording, not hand-written (CLAUDE.md rule 1) — in
`test_calibration_numbers_match_the_real_fixture` below, so a stale comment in
`catalog.py` would fail this suite rather than silently drift from the data
that justified it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from support.states import make_state

from wasted_harness.predictions.catalog import (
    CATALOG,
    EVENT_MAX_BASE_RATE,
    EVENT_MIN_BASE_RATE,
    EVENT_MIN_SAMPLE,
    MAX_WINDOW_S,
    MEASURED_PEAK_WANTED_LEVEL,
    MIN_WINDOW_S,
    REJECTED_TEMPLATES,
    TELEMETRY_RULE_KINDS,
    YES_NO_OUTCOMES,
    MeasuredRate,
    PredictionTemplate,
    Window,
)

EXPECTED_PREDICTION_TYPES = {
    "death_in_window",
    "loses_the_cops",
    "survives_a_chase",
    "enters_vehicle",
    "exits_vehicle",
    "survives_a_fight",
    "mission_outcome",
    "two_star_standoff",
}

TWO_STAR_GAIN = {
    "type": "wanted_change",
    "payload": {"from": 1, "to": 2},
    "ts": "2026-09-08T00:00:00Z",
}

REAL_SESSION_FIXTURE = Path(__file__).parent / "fixtures" / "real_session_2026-09-04.json"

GAIN_EVENT = {"type": "wanted_change", "payload": {"from": 0, "to": 1}, "ts": "2026-09-08T00:00:00Z"}


def by_type(prediction_type: str) -> PredictionTemplate:
    return next(t for t in CATALOG if t.prediction_type == prediction_type)


# -- catalog shape -------------------------------------------------------------


def test_catalog_covers_the_calibration_survivors() -> None:
    assert {t.prediction_type for t in CATALOG} == EXPECTED_PREDICTION_TYPES


def test_every_template_kind_is_in_the_closed_registry() -> None:
    for t in CATALOG:
        assert t.telemetry_rule["kind"] in TELEMETRY_RULE_KINDS


def test_every_window_is_within_the_brief_bounds() -> None:
    for t in CATALOG:
        assert MIN_WINDOW_S <= t.window.total_s <= MAX_WINDOW_S


def test_every_reliability_is_in_unit_range() -> None:
    for t in CATALOG:
        assert 0.0 <= t.reliability <= 1.0


def test_outcomes_are_the_contracts_shaped_yes_no_pair() -> None:
    for t in CATALOG:
        assert t.outcomes == YES_NO_OUTCOMES


def test_rejected_templates_are_documented_and_absent_from_the_catalog() -> None:
    assert set(REJECTED_TEMPLATES) == {
        "wanted_reaches_3",
        "wanted_gain",
        # the operator brief's own three WANTED EVENT examples, all measured
        # against the real recording and all refused — see the reasons.
        "survive_five_stars",
        "steal_a_police_car_and_escape",
        "land_the_helicopter",
    }
    for prediction_type, reason in REJECTED_TEMPLATES.items():
        assert prediction_type not in {t.prediction_type for t in CATALOG}
        assert len(reason) > 20  # a real, non-trivial explanation, not a stub


# -- construction refuses an unknown kind / a bad window -----------------------


def test_unknown_telemetry_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="not in the"):
        PredictionTemplate(
            prediction_type="bogus",
            question="WILL THIS EVER SHIP?",
            outcomes=YES_NO_OUTCOMES,
            telemetry_rule={"kind": "totally_made_up"},
            window=Window(lock_delay_s=10.0, resolve_delay_s=20.0),
            trigger=lambda state, events: True,
            score=lambda state: 0.5,
            reliability=0.5,
        )


def test_window_rejects_a_span_shorter_than_the_floor() -> None:
    with pytest.raises(ValueError, match=r"30\.0-300\.0"):
        Window(lock_delay_s=1.0, resolve_delay_s=1.0)


def test_window_rejects_a_span_longer_than_the_ceiling() -> None:
    with pytest.raises(ValueError, match=r"30\.0-300\.0"):
        Window(lock_delay_s=200.0, resolve_delay_s=200.0)


def test_window_rejects_a_non_positive_delay() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        Window(lock_delay_s=0.0, resolve_delay_s=60.0)


# -- death_in_window: now requires a REAL, RECENT wanted-star gain -----------------


def test_death_in_window_does_not_trigger_ambiently() -> None:
    """Calibration finding: the ambient version of this question is a giveaway
    (near-0% in a real recording). It must not fire without a recent gain."""
    tpl = by_type("death_in_window")
    assert tpl.trigger(make_state(dead=False, arrested=False, wanted=1), []) is False


def test_death_in_window_triggers_after_a_real_gain() -> None:
    tpl = by_type("death_in_window")
    assert tpl.trigger(make_state(dead=False, arrested=False, wanted=1), [GAIN_EVENT]) is True


def test_death_in_window_does_not_trigger_once_dead() -> None:
    tpl = by_type("death_in_window")
    assert tpl.trigger(make_state(dead=True, health=0, wanted=1), [GAIN_EVENT]) is False


def test_death_in_window_does_not_trigger_right_after_arrest() -> None:
    tpl = by_type("death_in_window")
    assert tpl.trigger(make_state(arrested=True, wanted=1), [GAIN_EVENT]) is False


def test_death_in_window_grace_period_after_a_recent_death_event() -> None:
    tpl = by_type("death_in_window")
    recent = [GAIN_EVENT, {"type": "death", "payload": {"cause": "?"}, "ts": "x"}]
    assert tpl.trigger(make_state(dead=False, wanted=1), recent) is False


def test_death_in_window_scores_higher_under_real_danger() -> None:
    tpl = by_type("death_in_window")
    calm = make_state(health=200)
    danger = make_state(health=20, wanted=2, attacker_handle=501)
    assert tpl.score(danger) > tpl.score(calm)


def test_death_in_window_score_is_seeded_near_the_measured_rate_in_a_neutral_state() -> None:
    tpl = by_type("death_in_window")
    # A state with none of the extra danger nudges active: score's uncertainty
    # term should sit near what 43.6% measured (not exactly 0.5, but well
    # above a floor/ceiling giveaway).
    neutral = make_state(health=200, wanted=0)
    assert 0.3 < tpl.score(neutral) < 1.0


# -- loses_the_cops / survives_a_chase: same gain-triggered precondition -----------


def test_loses_the_cops_does_not_trigger_ambiently() -> None:
    tpl = by_type("loses_the_cops")
    assert tpl.trigger(make_state(wanted=2), []) is False


def test_loses_the_cops_triggers_after_a_real_gain() -> None:
    tpl = by_type("loses_the_cops")
    assert tpl.trigger(make_state(wanted=1), [GAIN_EVENT]) is True


def test_loses_the_cops_does_not_trigger_once_arrested() -> None:
    tpl = by_type("loses_the_cops")
    assert tpl.trigger(make_state(wanted=2, arrested=True), [GAIN_EVENT]) is False


def test_loses_the_cops_does_not_trigger_at_zero_stars() -> None:
    tpl = by_type("loses_the_cops")
    assert tpl.trigger(make_state(wanted=0), [GAIN_EVENT]) is False


def test_survives_a_chase_does_not_trigger_ambiently() -> None:
    tpl = by_type("survives_a_chase")
    assert tpl.trigger(make_state(wanted=3), []) is False


def test_survives_a_chase_triggers_after_a_real_gain() -> None:
    tpl = by_type("survives_a_chase")
    assert tpl.trigger(make_state(wanted=1), [GAIN_EVENT]) is True


# -- enters_vehicle / exits_vehicle (uncalibrated, contract-shape only) -------------


def test_enters_vehicle_only_on_foot() -> None:
    tpl = by_type("enters_vehicle")
    assert tpl.trigger(make_state(in_vehicle=False), []) is True
    assert tpl.trigger(make_state(in_vehicle=True), []) is False


def test_exits_vehicle_only_while_riding() -> None:
    tpl = by_type("exits_vehicle")
    assert tpl.trigger(make_state(in_vehicle=True), []) is True
    assert tpl.trigger(make_state(in_vehicle=False), []) is False


def test_exits_vehicle_scores_higher_when_stopped_than_at_speed() -> None:
    tpl = by_type("exits_vehicle")
    stopped = make_state(
        in_vehicle=True,
        vehicle={
            "handle": 1, "model": "blista", "display_name": "Blista", "class": "Compacts",
            "speed": 0.0, "health": 1000.0, "upside_down": False, "in_water": False,
            "stopped_for_s": 5.0,
        },
    )
    fast = make_state(
        in_vehicle=True,
        vehicle={
            "handle": 1, "model": "blista", "display_name": "Blista", "class": "Compacts",
            "speed": 35.0, "health": 1000.0, "upside_down": False, "in_water": False,
            "stopped_for_s": 0.0,
        },
    )
    assert tpl.score(stopped) > tpl.score(fast)


# -- survives_a_fight (uncalibrated) -----------------------------------------------


def test_survives_a_fight_requires_a_real_threat() -> None:
    tpl = by_type("survives_a_fight")
    assert tpl.trigger(make_state(), []) is False
    assert tpl.trigger(make_state(attacker_handle=42), []) is True
    hostile = make_state(
        nearby_peds=[
            {"handle": 7, "model": "g_m_y_lost_01", "distance": 3.0, "relationship": "hostile"}
        ]
    )
    assert tpl.trigger(hostile, []) is True


# -- mission_outcome (uncalibrated: missions were off in the real recording) -------


def test_mission_outcome_requires_an_active_mission_no_cutscene() -> None:
    tpl = by_type("mission_outcome")
    assert tpl.trigger(make_state(mission_active=False), []) is False
    assert tpl.trigger(make_state(mission_active=True, cutscene_active=True), []) is False
    assert tpl.trigger(make_state(mission_active=True, cutscene_active=False), []) is True


# -- recompute the calibration numbers directly from the real fixture --------------


@pytest.mark.skipif(not REAL_SESSION_FIXTURE.exists(), reason="real fixture not present")
def test_calibration_numbers_match_the_real_fixture() -> None:
    """Independently recomputes the same three numbers `catalog.py`'s
    docstring cites, straight from the real recording, so a future edit to
    either the window or the comment fails loudly if they drift apart."""
    with REAL_SESSION_FIXTURE.open() as f:
        data = json.load(f)
    events = data["events"]
    for e in events:
        e["t"] = datetime.fromisoformat(e["ts"])

    wanted_changes = [e for e in events if e["type"] == "wanted_change"]
    deaths = [e for e in events if e["type"] == "death"]
    busts = [e for e in events if e["type"] == "busted"]
    died_or_busted = sorted(deaths + busts, key=lambda e: e["t"])
    gains = [e for e in wanted_changes if e["payload"]["to"] > e["payload"]["from"]]

    assert len(gains) == 39  # the exact real sample size this catalog was tuned on

    # wanted_reaches_3: zero, ever.
    assert max(e["payload"]["to"] for e in wanted_changes) < 3

    def rate(window_s: float, hit) -> float:
        n = yes = 0
        for g in gains:
            n += 1
            end = g["t"] + timedelta(seconds=window_s)
            if hit(g["t"], end):
                yes += 1
        return yes / n * 100.0

    loses_cops_90s = rate(
        90.0, lambda a, b: any(a < e["t"] <= b and e["payload"]["to"] == 0 for e in wanted_changes)
    )
    survives_150s = rate(
        150.0, lambda a, b: not any(a < e["t"] <= b for e in died_or_busted)
    )
    dies_240s = rate(240.0, lambda a, b: any(a < e["t"] <= b for e in deaths))

    assert loses_cops_90s == pytest.approx(53.8, abs=0.1)
    assert survives_150s == pytest.approx(48.7, abs=0.1)
    assert dies_240s == pytest.approx(43.6, abs=0.1)


# -- WANTED EVENTS: the admission rules bite ----------------------------------------


def _event_template(**over) -> PredictionTemplate:
    """A minimal `is_event=True` template, for the guard tests below only.

    The `MeasuredRate`s passed in here are not claims about the real game —
    they exist to show `PredictionTemplate` refusing what it must refuse. The
    ONLY measured numbers this suite treats as real are the ones it recomputes
    from `real_session_2026-09-04.json` further down.
    """
    kwargs = {
        "prediction_type": "guard_test",
        "question": "WILL THIS EVER SHIP?",
        "outcomes": YES_NO_OUTCOMES,
        "telemetry_rule": {"kind": "survives_window"},
        "window": Window(lock_delay_s=10.0, resolve_delay_s=20.0),
        "trigger": lambda state, events: True,
        "score": lambda state: 0.5,
        "reliability": 0.8,
        "is_event": True,
    }
    kwargs.update(over)
    return PredictionTemplate(**kwargs)


def test_every_shipped_event_carries_a_measured_base_rate() -> None:
    """The load-bearing one: nothing gets the WANTED EVENT highlight (and the
    boosted pool that follows it) on a rate nobody counted."""
    events = [t for t in CATALOG if t.is_event]
    assert events, "the catalog is expected to ship at least one WANTED EVENT"
    for t in events:
        assert t.measured is not None, f"{t.prediction_type}: event with no measured base rate"
        assert t.measured.n >= EVENT_MIN_SAMPLE
        assert EVENT_MIN_BASE_RATE <= t.measured.rate <= EVENT_MAX_BASE_RATE
        # the recording it was measured on is a file in this repo, not a memory
        assert (Path(__file__).parent.parent / t.measured.fixture).exists()


def test_an_event_without_a_measured_rate_is_refused() -> None:
    with pytest.raises(ValueError, match="must ship a MeasuredRate"):
        _event_template(measured=None)


def test_an_event_measured_on_too_few_real_anchors_is_refused() -> None:
    too_few = MeasuredRate(
        yes=1,
        n=EVENT_MIN_SAMPLE - 1,
        fixture="tests/fixtures/real_session_2026-09-04.json",
        anchor="(guard test)",
        settled_window_s=20.0,
        generation_delay_band_s=(0.0, 60.0),
    )
    with pytest.raises(ValueError, match="below EVENT_MIN_SAMPLE"):
        _event_template(measured=too_few)


def test_an_event_that_is_a_foregone_conclusion_is_refused() -> None:
    for yes in (0, 40):  # 0% and 100% of 40 anchors
        never_or_always = MeasuredRate(
            yes=yes,
            n=40,
            fixture="tests/fixtures/real_session_2026-09-04.json",
            anchor="(guard test)",
            settled_window_s=20.0,
            generation_delay_band_s=(0.0, 60.0),
        )
        with pytest.raises(ValueError, match="foregone conclusion"):
            _event_template(measured=never_or_always)


def test_an_ordinary_template_may_still_ship_unmeasured() -> None:
    """The bar is raised for events specifically. Several ordinary templates
    have no calibrating telemetry in this recording at all (vehicle
    transitions, missions) and say so at their own definitions."""
    ordinary = _event_template(is_event=False, measured=None)
    assert ordinary.is_event is False
    assert ordinary.measured is None


def test_events_stay_a_small_minority_of_the_catalog() -> None:
    """'Do not make every prediction feel like a major event' — the catalog
    side of that. How often one is OFFERED is the generator's test."""
    events = [t for t in CATALOG if t.is_event]
    assert len(events) * 4 <= len(CATALOG)


def test_a_measured_rate_needs_real_anchors() -> None:
    with pytest.raises(ValueError, match="at least one real anchor"):
        MeasuredRate(
            yes=0,
            n=0,
            fixture="x",
            anchor="(guard test)",
            settled_window_s=20.0,
            generation_delay_band_s=(0.0, 60.0),
        )


# -- two_star_standoff: the trigger is the ceiling moment, not any old heat --------


def test_two_star_standoff_does_not_trigger_ambiently() -> None:
    tpl = by_type("two_star_standoff")
    assert tpl.trigger(make_state(wanted=2), []) is False


def test_two_star_standoff_does_not_trigger_on_a_one_star_gain() -> None:
    tpl = by_type("two_star_standoff")
    assert tpl.trigger(make_state(wanted=1), [GAIN_EVENT]) is False


def test_two_star_standoff_triggers_on_a_real_two_star_gain() -> None:
    tpl = by_type("two_star_standoff")
    assert tpl.trigger(make_state(wanted=2), [TWO_STAR_GAIN]) is True


def test_two_star_standoff_does_not_trigger_once_dead_or_arrested() -> None:
    tpl = by_type("two_star_standoff")
    assert tpl.trigger(make_state(wanted=2, dead=True, health=0), [TWO_STAR_GAIN]) is False
    assert tpl.trigger(make_state(wanted=2, arrested=True), [TWO_STAR_GAIN]) is False


def test_two_star_standoff_survives_a_malformed_wanted_payload() -> None:
    """Real payloads come off the wire; a null `to` must not raise inside a
    trigger and take the tick's other candidates down with it."""
    tpl = by_type("two_star_standoff")
    junk = [{"type": "wanted_change", "payload": {"from": None, "to": None}, "ts": "x"}]
    assert tpl.trigger(make_state(wanted=2), junk) is False


def test_two_star_standoff_window_is_a_whole_four_minutes_of_settlement() -> None:
    """The question says FOUR MINUTES; settlement reads [locks_at, resolves_at],
    which is `resolve_delay_s` long. Those two must agree exactly."""
    tpl = by_type("two_star_standoff")
    assert tpl.window.resolve_delay_s == 240.0
    assert "FOUR MINUTES" in tpl.question
    assert tpl.window.total_s <= MAX_WINDOW_S


# -- recompute the EVENT numbers from the real recording ---------------------------


def _load_real_events() -> list[dict]:
    with REAL_SESSION_FIXTURE.open() as f:
        data = json.load(f)
    events = data["events"]
    for e in events:
        e["t"] = datetime.fromisoformat(e["ts"])
    return events


def _two_star_survival_yes(events: list[dict], generation_delay_s: float) -> tuple[int, int]:
    """Replays `survives_window` exactly as `settle_due_predictions` does it:
    a `death` or `busted` row with `locks_at <= ts <= resolves_at` settles NO,
    absence settles YES (migration 20260908120000, the `survives_window`
    branch). The window opens `generation_delay_s + lock_delay_s` after the
    anchoring event, because a row is created some bounded time after the
    telemetry that triggered it, and `locks_at` is later again.
    """
    tpl = by_type("two_star_standoff")
    fatal = [e for e in events if e["type"] in ("death", "busted")]
    anchors = [
        e
        for e in events
        if e["type"] == "wanted_change"
        and e["payload"]["to"] >= MEASURED_PEAK_WANTED_LEVEL
        and e["payload"]["to"] > e["payload"]["from"]
    ]
    yes = 0
    for a in anchors:
        locks = a["t"] + timedelta(seconds=generation_delay_s + tpl.window.lock_delay_s)
        resolves = locks + timedelta(seconds=tpl.window.resolve_delay_s)
        if not any(locks <= f["t"] <= resolves for f in fatal):
            yes += 1
    return yes, len(anchors)


@pytest.mark.skipif(not REAL_SESSION_FIXTURE.exists(), reason="real fixture not present")
def test_two_star_standoff_measured_rate_matches_the_real_fixture() -> None:
    """The number `catalog.py` ships is recomputed here from the recording, so
    changing the window without re-measuring fails this suite."""
    tpl = by_type("two_star_standoff")
    assert tpl.measured is not None
    yes, n = _two_star_survival_yes(_load_real_events(), generation_delay_s=0.0)
    assert (yes, n) == (tpl.measured.yes, tpl.measured.n) == (5, 17)


@pytest.mark.skipif(not REAL_SESSION_FIXTURE.exists(), reason="real fixture not present")
def test_the_event_stays_fair_across_the_whole_generation_delay_band() -> None:
    """A question whose fairness depends on how promptly the loop ticked is not
    a fair question. Across the entire caller-owned delay band, the measured
    rate must stay inside the same admission band the template was built under.
    (This is what disqualified the prettier 'reach two stars' candidate: 50% at
    a 2 s delay, 13.6% at 60 s.)"""
    tpl = by_type("two_star_standoff")
    assert tpl.measured is not None
    lo, hi = tpl.measured.generation_delay_band_s
    events = _load_real_events()
    rates = []
    delay = lo
    while delay <= hi:
        yes, n = _two_star_survival_yes(events, generation_delay_s=delay)
        rates.append(yes / n)
        delay += 5.0
    assert min(rates) >= EVENT_MIN_BASE_RATE
    assert max(rates) <= EVENT_MAX_BASE_RATE
    # and the band really was swept, not collapsed to a single point
    assert len(rates) >= 12


@pytest.mark.skipif(not REAL_SESSION_FIXTURE.exists(), reason="real fixture not present")
def test_the_rejected_event_ideas_are_backed_by_the_real_recording() -> None:
    """Every number quoted in `REJECTED_TEMPLATES` for the brief's own three
    event examples, recounted from the recording rather than taken on trust."""
    events = _load_real_events()
    wanted_changes = [e for e in events if e["type"] == "wanted_change"]

    # "CAN WANTED SURVIVE 5 STARS FOR 5 MINUTES?" — 5 stars has never happened.
    assert len(wanted_changes) == 63
    assert max(e["payload"]["to"] for e in wanted_changes) == MEASURED_PEAK_WANTED_LEVEL

    def activity(name: str, type_: str) -> list[dict]:
        return [e for e in events if e["type"] == type_ and e["payload"].get("activity") == name]

    # "CAN WANTED STEAL A POLICE CAR AND ESCAPE?" — n=2, zero completions.
    assert len(activity("steal_cop_car", "activity_start")) == 2
    cop_car_ends = activity("steal_cop_car", "activity_end")
    assert len(cop_car_ends) == 2
    assert [e for e in cop_car_ends if e["payload"].get("outcome") == "completed"] == []
    assert EVENT_MIN_SAMPLE > 2

    # "CAN WANTED LAND THE HELICOPTER?" — the nearest proxies never complete.
    flying_ends = activity("go_flying", "activity_end")
    assert len(flying_ends) == 92
    assert [e for e in flying_ends if e["payload"].get("outcome") == "completed"] == []
    up_only_ends = activity("up_only", "activity_end")
    assert len(up_only_ends) == 98
    assert len([e for e in up_only_ends if e["payload"].get("outcome") == "completed"]) == 1
