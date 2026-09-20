"""RollingBaseRate: the §3 re-measurement, on a clock the test advances by hand.

No wall clock, no sleeping, no invented telemetry. The unit tests below drive
the estimator with explicit `(ts, type, payload)` triples on an injected clock —
the same idiom `test_predictions_generator.py` uses for the generator — and the
fixture-backed ones replay the two real recordings in the repo
(`real_session_2026-09-04.json`, `real_session_2026-09-20.json`), which are
provenance-stamped production pulls, never hand-written (CLAUDE.md rule 1).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from wasted_harness.predictions.baserate import (
    DEFAULT_HISTORY_S,
    MEASURABLE_RULE_KINDS,
    RollingBaseRate,
    payload_contains,
    rule_matcher,
)
from wasted_harness.predictions.catalog import (
    AMBIENT_LOCK_DELAY_S,
    AMBIENT_RESOLVE_DELAY_S,
    CATALOG,
    DEFAULT_ROUND_INTERVAL_S,
    YES_NO_OUTCOMES,
    PredictionTemplate,
    Window,
)

FIXTURES = Path(__file__).parent / "fixtures"
OLD_FIXTURE = FIXTURES / "real_session_2026-09-04.json"
NEW_FIXTURE = FIXTURES / "real_session_2026-09-20.json"


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def ambient_template(
    prediction_type: str = "probe",
    *,
    event_type: str = "activity_end",
    payload_match: dict | None = None,
) -> PredictionTemplate:
    return PredictionTemplate(
        prediction_type=prediction_type,
        question="WILL WANTED DO THE THING?",
        outcomes=YES_NO_OUTCOMES,
        telemetry_rule={
            "kind": "event_matches",
            "params": {
                "event_type": event_type,
                "payload_match": payload_match or {"outcome": "completed"},
            },
            "outcome_if_true": "yes",
            "outcome_if_false": "no",
        },
        window=Window(
            lock_delay_s=AMBIENT_LOCK_DELAY_S, resolve_delay_s=AMBIENT_RESOLVE_DELAY_S
        ),
        trigger=lambda state, events: True,
        score=lambda state: 0.5,
        reliability=0.85,
        ambient=True,
    )


def by_type(prediction_type: str) -> PredictionTemplate:
    return next(t for t in CATALOG if t.prediction_type == prediction_type)


# -- containment, the same test settlement runs in SQL --------------------------


def test_payload_containment_matches_jsonb_semantics() -> None:
    assert payload_contains({"outcome": "completed", "activity": "x"}, {"outcome": "completed"})
    assert not payload_contains({"outcome": "timeout"}, {"outcome": "completed"})
    # A key the payload does not carry at all is not contained. This is the
    # `go_start_a_job` case: 552 of the 2,668 real activity_end rows in the
    # 2026-09-04 recording have no `category` key.
    assert not payload_contains({"outcome": "completed"}, {"category": "trouble"})
    assert not payload_contains(None, {"outcome": "completed"})
    assert not payload_contains({}, {"outcome": "completed"})
    # every key must match, not just one
    assert payload_contains(
        {"outcome": "completed", "category": "trouble"},
        {"outcome": "completed", "category": "trouble"},
    )
    assert not payload_contains(
        {"outcome": "completed", "category": "errand"},
        {"outcome": "completed", "category": "trouble"},
    )


def test_only_the_two_presence_kinds_are_measurable() -> None:
    assert set(MEASURABLE_RULE_KINDS) == {"event_occurs", "event_matches"}
    assert rule_matcher({"kind": "survives_window"}) is None
    assert rule_matcher({"kind": "wanted_clears"}) is None
    assert rule_matcher({"kind": "event_occurs", "params": {"event_type": "death"}}) is not None


def test_an_unmeasurable_rule_returns_no_sample_rather_than_zero_percent() -> None:
    """`n = 0` is "do not ask", and it must never look like a 0% rate."""
    clock = FakeClock(10_000.0)
    est = RollingBaseRate(clock=clock)
    est.observe(9_000.0, "death", {})
    survives = by_type("survives_a_chase")
    calibration = est.measure(survives)
    assert (calibration.yes, calibration.n) == (0, 0)
    assert calibration.rate is None


# -- the sampling grid ----------------------------------------------------------


def test_nothing_observed_means_nothing_measured() -> None:
    est = RollingBaseRate(clock=FakeClock(1000.0))
    c = est.measure(ambient_template())
    assert (c.yes, c.n) == (0, 0)


def test_the_sample_count_grows_one_window_per_round_interval() -> None:
    """The cold-start arithmetic §3's `>= 12` turns into, asserted.

    The first window can only be sampled once it has CLOSED (lock + resolve
    after its anchor), and each `round_interval_s` behind that is one more.
    Twelve of them is 12 x 300 s = 60 minutes, which is exactly the wait the
    warm start in `main.py` exists to avoid.
    """
    clock = FakeClock(0.0)
    est = RollingBaseRate(clock=clock)
    tpl = ambient_template()
    span = AMBIENT_LOCK_DELAY_S + AMBIENT_RESOLVE_DELAY_S  # 240s

    est.observe(0.0, "activity_end", {"outcome": "timeout"})
    clock.t = span - 1.0
    assert est.measure(tpl).n == 0, "no window has closed yet"

    clock.t = span
    assert est.measure(tpl).n == 1

    clock.t = span + DEFAULT_ROUND_INTERVAL_S
    assert est.measure(tpl).n == 2

    clock.t = span + 11 * DEFAULT_ROUND_INTERVAL_S
    assert est.measure(tpl).n == 12
    assert clock.t == pytest.approx(3540.0)  # 59 minutes from the first event


def test_a_matching_event_inside_a_window_counts_and_an_outside_one_does_not() -> None:
    clock = FakeClock(0.0)
    est = RollingBaseRate(clock=clock)
    tpl = ambient_template()
    # One window only: anchor 0, open at 60, close at 240.
    est.observe(0.0, "session_start", {})
    est.observe(120.0, "activity_end", {"outcome": "completed"})
    clock.t = 240.0
    assert (est.measure(tpl).yes, est.measure(tpl).n) == (1, 1)

    other = RollingBaseRate(clock=clock)
    other.observe(0.0, "session_start", {})
    other.observe(30.0, "activity_end", {"outcome": "completed"})  # before locks_at
    assert (other.measure(tpl).yes, other.measure(tpl).n) == (0, 1)


def test_the_payload_filter_is_what_separates_two_otherwise_identical_questions() -> None:
    clock = FakeClock(240.0)
    est = RollingBaseRate(clock=clock)
    est.observe(0.0, "session_start", {})
    est.observe(120.0, "activity_end", {"outcome": "timeout", "category": "errand"})
    completed = ambient_template("a", payload_match={"outcome": "completed"})
    timed_out = ambient_template("b", payload_match={"outcome": "timeout"})
    assert est.measure(completed).yes == 0
    assert est.measure(timed_out).yes == 1


def test_history_bounds_how_far_back_a_window_may_be_sampled() -> None:
    clock = FakeClock(0.0)
    est = RollingBaseRate(clock=clock, history_s=1200.0)
    tpl = ambient_template()
    for ts in range(0, 4000, 100):
        clock.t = float(ts)
        est.observe(float(ts), "activity_end", {"outcome": "completed"})
    clock.t = 4000.0
    # 1200s of history, 240s windows, 300s step: floor((1200-240)/300)+1 = 4.
    assert est.measure(tpl).n == 4
    assert est.observed_count < 40, "rows older than history_s are not retained"


def test_measuring_is_pure_and_repeatable() -> None:
    clock = FakeClock(0.0)
    est = RollingBaseRate(clock=clock)
    tpl = ambient_template()
    for ts in range(0, 3600, 120):
        est.observe(float(ts), "activity_end", {"outcome": "completed" if ts % 240 else "timeout"})
    clock.t = 3600.0
    first = est.measure(tpl)
    for _ in range(5):
        assert est.measure(tpl).as_json() == first.as_json()


def test_two_estimators_fed_the_same_events_agree_exactly() -> None:
    rows = [
        (float(ts), "activity_end", {"outcome": "completed" if ts % 300 < 150 else "gave_up"})
        for ts in range(0, 7200, 90)
    ]
    clock = FakeClock(7200.0)
    a, b = RollingBaseRate(clock=clock), RollingBaseRate(clock=clock)
    a.observe_many(rows)
    b.observe_many(list(reversed(rows)))  # out of order on purpose
    assert a.measure(ambient_template()).as_json() == b.measure(ambient_template()).as_json()


def test_the_calibration_that_goes_on_the_row_says_what_was_measured() -> None:
    clock = FakeClock(0.0)
    est = RollingBaseRate(clock=clock)
    est.observe(0.0, "activity_end", {"outcome": "completed"})
    clock.t = 3600.0
    c = est.measure(ambient_template())
    assert c.as_json() == {
        "yes": c.yes,
        "n": c.n,
        "window_s": AMBIENT_RESOLVE_DELAY_S,
        "history_s": DEFAULT_HISTORY_S,
    }


# -- the real recordings --------------------------------------------------------


def _rows(fixture: Path) -> list[tuple[float, str, dict]]:
    with fixture.open() as f:
        return sorted(
            (datetime.fromisoformat(e["ts"]).timestamp(), e["type"], e["payload"] or {})
            for e in json.load(f)["events"]
        )


@pytest.mark.skipif(not NEW_FIXTURE.exists(), reason="real fixture not present")
def test_the_live_free_roam_recording_measures_a_fair_question() -> None:
    """2026-09-20: 2 h 19 m of real play, no wanted star, no death, no bust.

    Every situational template in the catalogue is unofferable for that whole
    recording; the always-available ones are the only thing that can be asked,
    and two of the three measure inside §3's band.
    """
    rows = _rows(NEW_FIXTURE)
    est = RollingBaseRate(clock=lambda: rows[-1][0], history_s=float("inf"))
    est.observe_many(rows)
    assert (est.measure(by_type("pulls_off_a_goal")).yes, est.measure(by_type("pulls_off_a_goal")).n) == (14, 28)
    assert est.measure(by_type("runs_out_of_time")).yes == 9
    assert est.measure(by_type("gets_into_trouble")).yes == 7


@pytest.mark.skipif(not OLD_FIXTURE.exists(), reason="real fixture not present")
def test_the_older_recording_measures_the_same_question_as_a_giveaway() -> None:
    rows = _rows(OLD_FIXTURE)
    est = RollingBaseRate(clock=lambda: rows[-1][0], history_s=float("inf"))
    est.observe_many(rows)
    c = est.measure(by_type("pulls_off_a_goal"))
    assert c.n == 1043
    assert c.rate is not None and c.rate < 0.10
