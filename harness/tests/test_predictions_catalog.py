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
import re
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from support.states import make_state

from wasted_harness.predictions.baserate import RollingBaseRate
from wasted_harness.predictions.catalog import (
    AMBIENT_LOCK_DELAY_S,
    AMBIENT_MAX_RATE,
    AMBIENT_MIN_RATE,
    AMBIENT_MIN_SAMPLES,
    AMBIENT_RECORDED_RATES,
    AMBIENT_RESOLVE_DELAY_S,
    CATALOG,
    EVENT_MAX_BASE_RATE,
    EVENT_MIN_BASE_RATE,
    EVENT_MIN_SAMPLE,
    MAX_WINDOW_S,
    MEASURED_PEAK_WANTED_LEVEL,
    MIN_ENTRY_WINDOW_S,
    MIN_WINDOW_S,
    REJECTED_TEMPLATES,
    TELEMETRY_RULE_KINDS,
    UNSETTLEABLE_RULE_KINDS,
    YES_NO_OUTCOMES,
    MeasuredRate,
    PredictionTemplate,
    Window,
)

EXPECTED_PREDICTION_TYPES = {
    "death_in_window",
    "loses_the_cops",
    "survives_a_chase",
    "survives_a_fight",
    "mission_outcome",
    "two_star_standoff",
    # the v2.4 always-available questions (CONTRACTS-PREDICTIONS §3)
    "pulls_off_a_goal",
    "runs_out_of_time",
    "gets_into_trouble",
}

#: A minimal §3-shaped rule: kind, both outcome keys, params where needed.
def rule(kind: str, **params: object) -> dict:
    r: dict = {"kind": kind}
    if params:
        r["params"] = dict(params)
    r["outcome_if_true"] = "yes"
    r["outcome_if_false"] = "no"
    return r


#: The smallest window §3 (v2.4) still allows: a 30 s entry floor on top of the
#: 30 s total floor.
SMALLEST_LEGAL_WINDOW = Window(lock_delay_s=30.0, resolve_delay_s=30.0)

RECENT_SESSION_FIXTURE = (
    Path(__file__).parent / "fixtures" / "real_session_2026-09-20.json"
)

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
        # shipped in error, withdrawn: recognised by §3, resolvable by nobody
        "enters_vehicle",
        "exits_vehicle",
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
            telemetry_rule=rule("totally_made_up"),
            window=SMALLEST_LEGAL_WINDOW,
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
    """Re-derives every documented base rate straight from the recording, over
    the window SETTLEMENT actually reads.

    This used to measure `(anchor, anchor + window.total_s]` — an
    approximation that ignores `lock_delay_s` entirely. It could, because the
    locks were 10-15 s and the error was small. CONTRACTS-PREDICTIONS §3
    (v2.4) floors every lock at 30 s, and `settle_due_predictions()` reads
    `[locks_at, resolves_at]` and nothing else, so the approximation is no
    longer close enough to be harmless: on `survives_a_chase` it is the
    difference between 48.7% and 56.4%.

    So this now replays the real thing — the window opens `lock_delay_s` after
    the row is created, and the row is created some bounded time after the
    anchoring event (the caller's `PREDICTION_RECENT_WINDOW_S`, 60 s) — and
    every number in `catalog.py`'s calibration table has to come back out of
    the file.
    """
    events = _load_real_events()
    wanted_changes = [e for e in events if e["type"] == "wanted_change"]
    deaths = [e for e in events if e["type"] == "death"]
    fatal = sorted(
        (e for e in events if e["type"] in ("death", "busted")), key=lambda e: e["t"]
    )
    gains = [e for e in wanted_changes if e["payload"]["to"] > e["payload"]["from"]]

    assert len(gains) == 39  # the exact real sample size this catalog was tuned on

    # wanted_reaches_3: zero, ever.
    assert max(e["payload"]["to"] for e in wanted_changes) < 3

    def settled(prediction_type: str, hit, delay_s: float = 0.0) -> tuple[int, int]:
        """`(yes, n)` over the template's REAL settled window `[locks, resolves]`."""
        window = by_type(prediction_type).window
        yes = 0
        for g in gains:
            opens = g["t"] + timedelta(seconds=delay_s + window.lock_delay_s)
            closes = opens + timedelta(seconds=window.resolve_delay_s)
            if hit(opens, closes):
                yes += 1
        return yes, len(gains)

    cleared = lambda a, b: any(  # noqa: E731
        a <= e["t"] <= b and e["payload"]["to"] == 0 for e in wanted_changes
    )
    survived = lambda a, b: not any(a <= e["t"] <= b for e in fatal)  # noqa: E731
    died = lambda a, b: any(a <= e["t"] <= b for e in deaths)  # noqa: E731

    # The table in `catalog.py`'s "2026-09-21: the entry-window floor" section.
    assert settled("loses_the_cops", cleared) == (21, 39)  # 53.8%
    assert settled("survives_a_chase", survived) == (22, 39)  # 56.4%
    assert settled("death_in_window", died) == (14, 39)  # 35.9%

    # ...and every one of them carries that pair on the template itself, so the
    # comment, the constant and the recording cannot drift apart.
    for prediction_type, hit in (
        ("loses_the_cops", cleared),
        ("survives_a_chase", survived),
        ("death_in_window", died),
    ):
        measured = by_type(prediction_type).measured
        assert measured is not None, prediction_type
        assert (measured.yes, measured.n) == settled(prediction_type, hit)


@pytest.mark.skipif(not REAL_SESSION_FIXTURE.exists(), reason="real fixture not present")
def test_every_calibrated_template_stays_in_band_across_the_delay_band() -> None:
    """The admission test §3's floor could have broken, run on every template
    that ships a measured rate rather than only on the WANTED EVENT.

    Moving a lock moves the settled window against the telemetry, so a
    re-measured rate can leave the band — and the honest response to that is to
    withdraw the template, not to move the window back. None of them did; this
    is what proves it, sweeping the whole 0-60 s band the caller owns.
    """
    events = _load_real_events()
    wanted_changes = [e for e in events if e["type"] == "wanted_change"]
    deaths = [e for e in events if e["type"] == "death"]
    fatal = [e for e in events if e["type"] in ("death", "busted")]
    gains = [e for e in wanted_changes if e["payload"]["to"] > e["payload"]["from"]]
    hits = {
        "loses_the_cops": lambda a, b: any(
            a <= e["t"] <= b and e["payload"]["to"] == 0 for e in wanted_changes
        ),
        "survives_a_chase": lambda a, b: not any(a <= e["t"] <= b for e in fatal),
        "death_in_window": lambda a, b: any(a <= e["t"] <= b for e in deaths),
    }
    for prediction_type, hit in hits.items():
        window = by_type(prediction_type).window
        rates = []
        delay = 0.0
        while delay <= 60.0:
            yes = 0
            for g in gains:
                opens = g["t"] + timedelta(seconds=delay + window.lock_delay_s)
                if hit(opens, opens + timedelta(seconds=window.resolve_delay_s)):
                    yes += 1
            rates.append(yes / len(gains))
            delay += 5.0
        assert len(rates) >= 12, prediction_type
        assert min(rates) >= EVENT_MIN_BASE_RATE, (prediction_type, min(rates))
        assert max(rates) <= EVENT_MAX_BASE_RATE, (prediction_type, max(rates))


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
        "telemetry_rule": rule("survives_window"),
        "window": SMALLEST_LEGAL_WINDOW,
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
        settled_window_s=30.0,
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
            settled_window_s=30.0,
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
            settled_window_s=30.0,
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
    # ...and the §3 entry floor did not come out of the four minutes: it was
    # added to the total (255s -> 270s), which is why the measured rate moved.
    assert tpl.window.lock_delay_s == MIN_ENTRY_WINDOW_S


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
    assert (yes, n) == (tpl.measured.yes, tpl.measured.n) == (7, 17)


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


# -- every shipped question must be one settlement can actually answer ----------------
#
# `PredictionTemplate.__post_init__` only asks "is this kind RECOGNISED?"
# (TELEMETRY_RULE_KINDS). `settle_due_predictions()` separately decides which
# recognised kinds are RESOLVABLE. Those are different sets, and nothing used to
# compare them — which is how `enters_vehicle`/`exits_vehicle` shipped: valid on
# their face, voiding 100% of the time in production, taking real entries the whole
# while. These tests re-derive the resolvable set from the migration itself, so the
# constant in catalog.py cannot drift away from the SQL that owns the answer.

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "infra" / "supabase" / "migrations"

#: The migration that adds `event_matches` to the settlement dispatch
#: (CONTRACTS-PREDICTIONS v2.4's own file name). Owned by `infra/`, not by this
#: package; until it lands, `settle_due_predictions()` does not know the kind
#: and — by the contract's deliberate design — voids rather than mis-settling.
EVENT_MATCHES_MIGRATION = "20260921000000_event_matches.sql"

#: The DEFINITION of the function, not a grant on it. `create function` and
#: `create or replace function` both count; `revoke execute on function` and
#: `grant execute on function` do not, or the grants migrations would shadow
#: the file that owns the dispatch.
SETTLEMENT_DEF_RE = re.compile(
    r"create\s+(?:or\s+replace\s+)?function\s+public\.settle_due_predictions\s*\(",
    re.IGNORECASE,
)


def settlement_sql_path() -> Path:
    """The NEWEST migration that defines `settle_due_predictions()`.

    Not a hardcoded file name. The dispatch table these tests re-derive lives
    in whichever migration most recently redefined the function, and pinning
    the first one would have quietly kept testing a superseded definition the
    moment `infra/` shipped a replacement — which is exactly what v2.4 does.
    Migration file names are date-ordered by convention, so newest is last.
    """
    candidates = sorted(
        path
        for path in MIGRATIONS_DIR.glob("*.sql")
        if SETTLEMENT_DEF_RE.search(path.read_text())
    )
    assert candidates, f"no migration in {MIGRATIONS_DIR} defines settle_due_predictions()"
    return candidates[-1]


SETTLEMENT_SQL = settlement_sql_path()

BRANCH_RE = re.compile(r"^\s*(?:els)?if\s+v_kind\s", re.MULTILINE)
KIND_RE = re.compile(r"v_kind\s*(?:=\s*'(\w+)'|in\s*\(([^)]*)\))")


def dispatched_kinds() -> tuple[set[str], set[str]]:
    """`(mentioned, resolvable)` straight out of the settlement SQL's dispatch."""
    sql = SETTLEMENT_SQL.read_text()
    body = sql[
        sql.index("if v_kind = 'event_occurs'") : sql.index("v_void_reason := 'unknown_rule_kind'")
    ]
    bounds = [m.start() for m in BRANCH_RE.finditer(body)] + [len(body)]
    resolvable: set[str] = set()
    mentioned: set[str] = set()
    for lo, hi in pairwise(bounds):
        branch = body[lo:hi]
        m = KIND_RE.search(branch)
        if not m:
            continue
        kinds = {m.group(1)} if m.group(1) else set(re.findall(r"'(\w+)'", m.group(2)))
        mentioned |= kinds
        if "v_result :=" in branch:
            resolvable |= kinds
    return mentioned, resolvable


def always_void_kinds() -> set[str]:
    """Kinds whose every dispatch branch is incapable of producing an outcome.

    A branch that never assigns `v_result` can only fall through to a void — so a
    kind with no result-assigning branch anywhere can never settle yes or no.
    """
    mentioned, resolvable = dispatched_kinds()
    return mentioned - resolvable


def test_the_settlement_sql_is_parseable_and_the_derivation_is_not_vacuous() -> None:
    """Guards the two tests below: if the parse silently returned nothing, they
    would both pass no matter what shipped."""
    assert SETTLEMENT_SQL.is_file(), SETTLEMENT_SQL
    derived = always_void_kinds()
    assert derived, "parsed no always-void kinds at all — the parser has drifted"
    # event_occurs is the canonical resolvable kind; it must NOT come back as void.
    assert "event_occurs" not in derived


def test_unsettleable_kinds_match_the_migration() -> None:
    assert always_void_kinds() == set(UNSETTLEABLE_RULE_KINDS)


def test_no_shipped_template_uses_a_kind_settlement_can_never_resolve() -> None:
    void_kinds = always_void_kinds()
    offenders = {t.prediction_type: t.telemetry_rule["kind"] for t in CATALOG if t.telemetry_rule["kind"] in void_kinds}
    assert not offenders, (
        f"these shipped templates can never have a winner: {offenders}. "
        f"settle_due_predictions() voids their kind unconditionally, so they would "
        f"collect real entries and settle void 100% of the time."
    )


def test_every_shipped_kind_is_dispatched_by_the_final_settlement_sql() -> None:
    """Every rule this catalogue writes must be one the SQL can act on.

    `TELEMETRY_RULE_KINDS` is this package's copy of §3's table; what settles
    is whatever the newest migration defining `settle_due_predictions()` says.
    Those are different artefacts and only one of them is in this directory.

    SKIPS, loudly, while `infra/` has not shipped the v2.4 migration yet: the
    three ambient templates use `event_matches`, and a settlement function that
    predates it voids them as `unknown_rule_kind`. That is the contract's own
    deliberate design ("a harness ahead of the database fails closed"), so it
    is not a reason to weaken this test into passing — it is a reason to say
    which file is missing.
    """
    mentioned, resolvable = dispatched_kinds()
    shipped = {t.telemetry_rule["kind"] for t in CATALOG}
    missing = shipped - mentioned
    if missing:
        pytest.skip(
            f"{SETTLEMENT_SQL.name} does not dispatch {sorted(missing)}; expected "
            f"{EVENT_MATCHES_MIGRATION} (owned by infra/) to land first. Rows written "
            f"with these kinds void as unknown_rule_kind until it does."
        )
    unresolvable = shipped - resolvable
    assert not unresolvable, (
        f"these shipped kinds are recognised by {SETTLEMENT_SQL.name} but can never "
        f"produce an outcome: {sorted(unresolvable)}"
    )


# -- §3 "Cadence and entry windows" (v2.4) -------------------------------------


def test_every_shipped_template_gives_a_viewer_the_contract_floor_to_enter() -> None:
    """The headline of the v2.4 change, asserted on the shipped catalogue."""
    for t in CATALOG:
        assert t.window.lock_delay_s >= MIN_ENTRY_WINDOW_S, t.prediction_type
        assert t.window.total_s <= MAX_WINDOW_S, t.prediction_type


def test_the_window_refuses_an_entry_window_under_the_floor() -> None:
    with pytest.raises(ValueError, match="whole time a viewer has to enter"):
        Window(lock_delay_s=29.0, resolve_delay_s=60.0)


def test_the_entry_floor_does_not_shadow_the_total_bounds() -> None:
    """A window that is illegal on both counts reports the more basic fault."""
    with pytest.raises(ValueError, match=r"30\.0-300\.0"):
        Window(lock_delay_s=1.0, resolve_delay_s=1.0)


def test_ambient_templates_run_the_scheduled_round_window_exactly() -> None:
    ambient = [t for t in CATALOG if t.ambient]
    assert len(ambient) == 3
    for t in ambient:
        assert t.window.lock_delay_s == AMBIENT_LOCK_DELAY_S == 60.0
        assert t.window.resolve_delay_s == AMBIENT_RESOLVE_DELAY_S == 180.0
        assert t.window.is_scheduled_round
        assert t.is_event is False


def test_an_ambient_template_on_the_wrong_window_is_refused() -> None:
    with pytest.raises(ValueError, match="scheduled-round window exactly"):
        PredictionTemplate(
            prediction_type="wrong_window",
            question="WILL THIS EVER SHIP?",
            outcomes=YES_NO_OUTCOMES,
            telemetry_rule=rule(
                "event_matches", event_type="activity_end", payload_match={"outcome": "completed"}
            ),
            window=SMALLEST_LEGAL_WINDOW,
            trigger=lambda state, events: True,
            score=lambda state: 0.5,
            reliability=0.8,
            ambient=True,
        )


def test_a_template_cannot_be_both_ambient_and_a_wanted_event() -> None:
    with pytest.raises(ValueError, match="both ambient and a WANTED EVENT"):
        PredictionTemplate(
            prediction_type="both",
            question="WILL THIS EVER SHIP?",
            outcomes=YES_NO_OUTCOMES,
            telemetry_rule=rule("survives_window"),
            window=Window(lock_delay_s=AMBIENT_LOCK_DELAY_S, resolve_delay_s=AMBIENT_RESOLVE_DELAY_S),
            trigger=lambda state, events: True,
            score=lambda state: 0.5,
            reliability=0.8,
            ambient=True,
            is_event=True,
            measured=by_type("two_star_standoff").measured,
        )


# -- the rule SHAPE settlement actually reads ----------------------------------


def test_every_shipped_rule_nests_its_params_and_names_its_outcomes() -> None:
    """The break this catalogue shipped with, asserted so it cannot come back.

    `settle_due_predictions()` reads `telemetry_rule -> 'params'` and
    `->> 'outcome_if_true'` / `'outcome_if_false'`. Flat params void
    `malformed_rule`; a missing outcome key makes settlement RAISE on the
    status-transition trigger and abort the whole run.
    """
    for t in CATALOG:
        assert t.telemetry_rule["outcome_if_true"] == "yes", t.prediction_type
        assert t.telemetry_rule["outcome_if_false"] == "no", t.prediction_type
        keys = {o["key"] for o in t.outcomes}
        assert t.telemetry_rule["outcome_if_true"] in keys
        assert t.telemetry_rule["outcome_if_false"] in keys
        # No parameter ever sits at the top level, where the SQL cannot see it.
        assert set(t.telemetry_rule) <= {"kind", "params", "outcome_if_true", "outcome_if_false"}
        params = t.telemetry_rule.get("params", {})
        assert isinstance(params, dict)
        if t.telemetry_rule["kind"] in ("event_occurs", "event_matches"):
            assert params["event_type"]
        if t.telemetry_rule["kind"] == "mission_outcome":
            assert params["expect"] in ("passed", "failed")


def test_a_rule_with_no_outcome_keys_is_refused() -> None:
    with pytest.raises(ValueError, match="outcome_if_true"):
        PredictionTemplate(
            prediction_type="no_outcomes",
            question="WILL THIS EVER SHIP?",
            outcomes=YES_NO_OUTCOMES,
            telemetry_rule={"kind": "survives_window"},
            window=SMALLEST_LEGAL_WINDOW,
            trigger=lambda state, events: True,
            score=lambda state: 0.5,
            reliability=0.8,
        )


def test_a_rule_whose_outcome_is_not_one_of_its_outcomes_is_refused() -> None:
    with pytest.raises(ValueError, match="not one of this template's outcome keys"):
        PredictionTemplate(
            prediction_type="wrong_outcome",
            question="WILL THIS EVER SHIP?",
            outcomes=YES_NO_OUTCOMES,
            telemetry_rule={
                "kind": "survives_window",
                "outcome_if_true": "maybe",
                "outcome_if_false": "no",
            },
            window=SMALLEST_LEGAL_WINDOW,
            trigger=lambda state, events: True,
            score=lambda state: 0.5,
            reliability=0.8,
        )


def test_event_occurs_with_a_flat_event_type_is_refused() -> None:
    """The exact shape every rule in this file used to have."""
    with pytest.raises(ValueError, match=r"requires params\.event_type"):
        PredictionTemplate(
            prediction_type="flat_params",
            question="WILL THIS EVER SHIP?",
            outcomes=YES_NO_OUTCOMES,
            telemetry_rule={
                "kind": "event_occurs",
                "event_type": "death",
                "outcome_if_true": "yes",
                "outcome_if_false": "no",
            },
            window=SMALLEST_LEGAL_WINDOW,
            trigger=lambda state, events: True,
            score=lambda state: 0.5,
            reliability=0.8,
        )


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"event_type": "", "payload_match": {"outcome": "completed"}}, "non-empty string"),
        ({"event_type": "activity_end", "payload_match": {}}, "non-empty JSON object"),
        ({"event_type": "activity_end", "payload_match": "completed"}, "non-empty JSON object"),
        (
            {"event_type": "activity_end", "payload_match": {"outcome": ["a", "b"]}},
            "must be a JSON scalar",
        ),
    ],
)
def test_event_matches_validates_its_payload_filter(params: dict, match: str) -> None:
    """§3: a blank `event_type`, or a `payload_match` that is missing, not an
    object, or `{}`, voids `malformed_rule`. Refused here instead."""
    with pytest.raises(ValueError, match=match):
        PredictionTemplate(
            prediction_type="bad_match",
            question="WILL THIS EVER SHIP?",
            outcomes=YES_NO_OUTCOMES,
            telemetry_rule={
                "kind": "event_matches",
                "params": params,
                "outcome_if_true": "yes",
                "outcome_if_false": "no",
            },
            window=SMALLEST_LEGAL_WINDOW,
            trigger=lambda state, events: True,
            score=lambda state: 0.5,
            reliability=0.8,
        )


@pytest.mark.parametrize(
    "level",
    ["two", "", None, True, False, [2], {"gte": 2}, float("nan"), float("inf"), float("-inf")],
)
def test_wanted_reaches_level_must_be_a_real_number(level: object) -> None:
    """`settle_due_predictions()` (20260921000000_event_matches.sql) does
    `(v_params ->> 'level')::numeric` with NO try/catch around the cast: a
    non-numeric `level` (an earlier bug let `level="two"` pass import-time
    validation, since `_validate_rule` only checked for PRESENCE) raises
    inside the settlement loop and takes the WHOLE BATCH down with it, not
    just this row. `True`/`False` are refused too — `bool` is a `numeric`
    subtype in the JSON sense settlement would accept only by accident
    (`isinstance(True, int)` is `True` in Python), and a wanted level of
    `true` is not a number a viewer would recognise as a star count."""
    with pytest.raises(ValueError, match="must be a real number"):
        PredictionTemplate(
            prediction_type="bad_level",
            question="WILL THIS EVER SHIP?",
            outcomes=YES_NO_OUTCOMES,
            telemetry_rule={
                "kind": "wanted_reaches",
                "params": {"level": level},
                "outcome_if_true": "yes",
                "outcome_if_false": "no",
            },
            window=SMALLEST_LEGAL_WINDOW,
            trigger=lambda state, events: True,
            score=lambda state: 0.5,
            reliability=0.8,
        )


@pytest.mark.parametrize("level", [2, 2.0, 0, -1])
def test_wanted_reaches_accepts_a_real_number_level(level: object) -> None:
    """Control: an actual int/float level (including 0 and a negative one —
    this validator only checks the JSON TYPE, not the game-logic range) builds
    cleanly."""
    tpl = PredictionTemplate(
        prediction_type="good_level",
        question="WILL THIS EVER SHIP?",
        outcomes=YES_NO_OUTCOMES,
        telemetry_rule={
            "kind": "wanted_reaches",
            "params": {"level": level},
            "outcome_if_true": "yes",
            "outcome_if_false": "no",
        },
        window=SMALLEST_LEGAL_WINDOW,
        trigger=lambda state, events: True,
        score=lambda state: 0.5,
        reliability=0.8,
    )
    assert tpl.telemetry_rule["params"]["level"] == level


def test_event_matches_is_in_the_registry_and_used_by_the_ambient_templates() -> None:
    assert "event_matches" in TELEMETRY_RULE_KINDS
    assert "event_matches" not in UNSETTLEABLE_RULE_KINDS
    for t in CATALOG:
        if t.ambient:
            assert t.telemetry_rule["kind"] == "event_matches"
            assert t.telemetry_rule["params"]["event_type"] == "activity_end"


# -- the ambient triggers ------------------------------------------------------


def test_ambient_templates_only_fire_in_ordinary_free_roam() -> None:
    for t in (t for t in CATALOG if t.ambient):
        assert t.trigger(make_state(), []) is True, t.prediction_type
        # every one of these moments belongs to a situational template
        assert t.trigger(make_state(wanted=1), []) is False, t.prediction_type
        assert t.trigger(make_state(dead=True, health=0), []) is False, t.prediction_type
        assert t.trigger(make_state(arrested=True), []) is False, t.prediction_type
        assert t.trigger(make_state(mission_active=True), []) is False, t.prediction_type
        assert (
            t.trigger(make_state(mission_active=True, cutscene_active=True), []) is False
        ), t.prediction_type


# -- the ambient base rates, re-derived through the SHIPPED estimator ----------


def _rolling_pairs(fixture: Path) -> dict[str, tuple[int, int]]:
    with fixture.open() as f:
        rows = sorted(
            (datetime.fromisoformat(e["ts"]).timestamp(), e["type"], e["payload"] or {})
            for e in json.load(f)["events"]
        )
    end = rows[-1][0]
    # history_s wide open so the whole recording is in scope; the point here is
    # the measurement, not the retention window.
    estimator = RollingBaseRate(clock=lambda: end, history_s=float("inf"))
    estimator.observe_many(rows)
    out = {}
    for t in CATALOG:
        if not t.ambient:
            continue
        c = estimator.measure(t)
        out[t.prediction_type] = (c.yes, c.n)
    return out


@pytest.mark.skipif(
    not (REAL_SESSION_FIXTURE.exists() and RECENT_SESSION_FIXTURE.exists()),
    reason="real fixtures not present",
)
def test_the_recorded_ambient_rates_come_back_out_of_the_real_recordings() -> None:
    """`AMBIENT_RECORDED_RATES` recounted by the estimator that will run live."""
    for fixture in (REAL_SESSION_FIXTURE, RECENT_SESSION_FIXTURE):
        pairs = _rolling_pairs(fixture)
        for prediction_type, pair in pairs.items():
            documented = AMBIENT_RECORDED_RATES[prediction_type][fixture.name]
            assert pair == documented, (prediction_type, fixture.name, pair, documented)


@pytest.mark.skipif(
    not (REAL_SESSION_FIXTURE.exists() and RECENT_SESSION_FIXTURE.exists()),
    reason="real fixtures not present",
)
def test_the_same_ambient_question_is_fair_on_one_day_and_a_giveaway_on_the_other() -> None:
    """Why §3 forbids a fixed base rate, stated as an assertion on real files.

    `pulls_off_a_goal` measures inside the band on 2026-09-20 and far outside
    it on 2026-09-04 — the same question, the same window, the same agent. A
    number written into the template would have been a coin flip on one of
    those days and a giveaway on the other.
    """
    recent = _rolling_pairs(RECENT_SESSION_FIXTURE)["pulls_off_a_goal"]
    old = _rolling_pairs(REAL_SESSION_FIXTURE)["pulls_off_a_goal"]
    assert recent[1] >= AMBIENT_MIN_SAMPLES and old[1] >= AMBIENT_MIN_SAMPLES
    assert AMBIENT_MIN_RATE <= recent[0] / recent[1] <= AMBIENT_MAX_RATE
    assert old[0] / old[1] < AMBIENT_MIN_RATE


@pytest.mark.skipif(
    not (REAL_SESSION_FIXTURE.exists() and RECENT_SESSION_FIXTURE.exists()),
    reason="real fixtures not present",
)
def test_activity_end_really_carries_outcome_and_how_often_it_carries_category() -> None:
    """The payload fields the three ambient rules filter on, counted rather
    than assumed — `outcome` on every row, `category` on most of them.

    The shortfall is not a defect to route around: `category` comes from
    `roam.close()`, which returns nothing when no roam goal was locked, so the
    day planner's own `go_start_a_job` rows carry none. A `payload @>
    {"category": "trouble"}` test is simply false for those, which is correct.
    """
    for fixture, expected in (
        (REAL_SESSION_FIXTURE, {"rows": 2668, "outcome": 2668, "category": 2116}),
        (RECENT_SESSION_FIXTURE, {"rows": 112, "outcome": 112, "category": 108}),
    ):
        with fixture.open() as f:
            ends = [e for e in json.load(f)["events"] if e["type"] == "activity_end"]
        assert len(ends) == expected["rows"], fixture.name
        assert sum("outcome" in (e["payload"] or {}) for e in ends) == expected["outcome"]
        assert sum("category" in (e["payload"] or {}) for e in ends) == expected["category"]
        # ...and every row without one is the day planner's mission trip.
        assert {
            e["payload"].get("activity")
            for e in ends
            if "category" not in (e["payload"] or {})
        } <= {"go_start_a_job"}
