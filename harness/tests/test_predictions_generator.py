"""PredictionGenerator: session gating, anti-spam, ranking, determinism.

Uses `support.states.make_state` for real `GameState` objects (see
`test_predictions_catalog.py`'s docstring for why: no real `/state` recording
exists yet, and this is the same pure-function idiom the rest of the harness
test suite already uses) plus small, explicitly-constructed
`PredictionTemplate`s for the anti-spam tests, where a controlled, minimal
catalog makes the cooldown/cap behaviour unambiguous to assert on.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from support.states import make_state

from wasted_harness.predictions.catalog import (
    CATALOG,
    TELEMETRY_RULE_KINDS,
    YES_NO_OUTCOMES,
    PredictionTemplate,
    Window,
)
from wasted_harness.predictions.catalog import (
    TPL_TWO_STAR_STANDOFF as REAL_EVENT_TEMPLATE,
)
from wasted_harness.predictions.generator import GeneratorConfig, PredictionGenerator

SESSION_ID = "00000000-0000-0000-0000-0000000000aa"


class FakeClock:
    """Deterministic, explicitly-advanced clock — no wall-clock reads."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _tpl(
    prediction_type: str,
    *,
    kind: str = "survives_window",
    window: Window | None = None,
    trigger=lambda state, events: True,
    score=lambda state: 0.5,
    reliability: float = 0.8,
    is_event: bool = False,
) -> PredictionTemplate:
    """A controlled template for the anti-spam tests.

    An `is_event=True` one borrows the REAL shipped event's `MeasuredRate`
    rather than inventing a base rate: `PredictionTemplate` requires a measured
    rate for every event (that is the point of the catalog-side test), and a
    made-up number here would be a fake measurement even if nothing read it.
    """
    return PredictionTemplate(
        prediction_type=prediction_type,
        question=f"WILL {prediction_type.upper()}?",
        outcomes=YES_NO_OUTCOMES,
        telemetry_rule={"kind": kind},
        window=window or Window(lock_delay_s=10.0, resolve_delay_s=20.0),
        trigger=trigger,
        score=score,
        reliability=reliability,
        is_event=is_event,
        measured=REAL_EVENT_TEMPLATE.measured if is_event else None,
    )


# -- session gating -------------------------------------------------------------


def test_no_session_generates_nothing() -> None:
    gen = PredictionGenerator(catalog=CATALOG, clock=FakeClock(), wall_clock=lambda: 1_893_456_000.0)
    assert gen.generate(make_state(), session_id=None) is None


def test_a_live_session_can_generate() -> None:
    clock = FakeClock()
    gen = PredictionGenerator(catalog=CATALOG, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    row = gen.generate(make_state(), session_id=SESSION_ID)
    assert row is not None
    assert row["session_id"] == SESSION_ID
    assert row["status"] == "open"
    assert row["telemetry_rule"]["kind"] in TELEMETRY_RULE_KINDS


# -- row shape --------------------------------------------------------------------


def test_row_timestamps_are_correctly_ordered() -> None:
    from datetime import datetime

    clock = FakeClock()
    gen = PredictionGenerator(catalog=CATALOG, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    row = gen.generate(make_state(), session_id=SESSION_ID)
    assert row is not None
    opened = datetime.fromisoformat(row["opened_at"])
    locks = datetime.fromisoformat(row["locks_at"])
    resolves = datetime.fromisoformat(row["resolves_at"])
    assert opened < locks < resolves


def test_state_context_matches_the_frozen_hud_shape() -> None:
    clock = FakeClock()
    gen = PredictionGenerator(catalog=CATALOG, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    row = gen.generate(make_state(health=150, wanted=1), session_id=SESSION_ID)
    assert row is not None
    assert set(row["state_context"]) == {
        "health", "armor", "wanted", "cash", "vehicle", "street", "zone", "clock", "weather",
    }
    assert row["state_context"]["wanted"] == 1


def test_never_invents_settlement_owned_fields() -> None:
    """The generator writes what CREATING a prediction requires, and nothing
    settlement owns.

    This test used to assert that `reward_pool` and `reward_asset` were absent.
    That was wrong against the real schema, and provably so: both are
    `NOT NULL` with no default and nothing backfilling them, so a row without
    them is rejected outright — every prediction the harness opened was
    requeued forever, growing the offline queue on the game-loop thread. Funding
    the pool is part of creating the prediction, so the generator owns it; what
    it must never touch is the OUTCOME.
    """
    clock = FakeClock()
    gen = PredictionGenerator(catalog=CATALOG, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    row = gen.generate(make_state(), session_id=SESSION_ID)
    assert row is not None

    for forbidden in (
        "entry_count",
        "correct_count",
        "result",
        "resolution_evidence",
        "settled_at",
    ):
        assert forbidden not in row, f"{forbidden} is settlement's to write, not the generator's"

    # The NOT NULL columns the schema demands, in the shape it demands them.
    assert row["reward_asset"] == "TTWO"
    pool = Decimal(row["reward_pool"])
    assert pool > 0, "predictions.reward_pool has CHECK (reward_pool > 0)"
    assert isinstance(row["reward_pool"], str), "an exact string, never a lossy float"

    # `is_event` is always written, and always a real bool, never a truthy stand-in.
    assert row["is_event"] is False


def test_a_wanted_event_carries_the_boosted_pool() -> None:
    """§13: events "may have boosted TTWO reward pools". The boost is applied
    where the pool is FUNDED — here — because settlement only divides what it
    is given."""
    clock = FakeClock()
    gen = PredictionGenerator(catalog=CATALOG, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    events = [t for t in CATALOG if t.is_event]
    assert events, "the catalogue is expected to ship at least one WANTED EVENT"

    base = Decimal(gen.config.base_reward_pool)
    ordinary = Decimal(gen._pool_for(next(t for t in CATALOG if not t.is_event)))
    boosted = Decimal(gen._pool_for(events[0]))
    assert ordinary == base
    assert boosted == base * gen.config.event_reward_multiplier
    assert boosted > ordinary


def test_created_from_event_is_never_invented() -> None:
    clock = FakeClock()
    gen = PredictionGenerator(catalog=CATALOG, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    row = gen.generate(make_state(), session_id=SESSION_ID)
    assert row is not None
    assert row["created_from_event"] is None


# -- anti-spam: cooldown --------------------------------------------------------


def test_min_gap_blocks_a_second_call_at_the_same_instant() -> None:
    clock = FakeClock()
    gen = PredictionGenerator(catalog=CATALOG, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    first = gen.generate(make_state(), session_id=SESSION_ID)
    assert first is not None
    second = gen.generate(make_state(), session_id=SESSION_ID)
    assert second is None


def test_min_gap_clears_once_enough_time_has_passed() -> None:
    clock = FakeClock()
    catalog = (_tpl("a"), _tpl("b"))
    config = GeneratorConfig(max_concurrent_open=5, min_gap_s=30.0, type_cooldown_s=0.0)
    gen = PredictionGenerator(catalog=catalog, clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config)
    first = gen.generate(make_state(), session_id=SESSION_ID)
    assert first is not None
    clock.advance(30.0)
    second = gen.generate(make_state(), session_id=SESSION_ID)
    assert second is not None


# -- anti-spam: no duplicate prediction_type back-to-back ----------------------


def test_no_duplicate_type_back_to_back_even_when_it_is_the_only_candidate() -> None:
    clock = FakeClock()
    only = (_tpl("only_one"),)
    config = GeneratorConfig(max_concurrent_open=5, min_gap_s=1.0, type_cooldown_s=1000.0)
    gen = PredictionGenerator(catalog=only, clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config)
    first = gen.generate(make_state(), session_id=SESSION_ID)
    assert first is not None
    clock.advance(5.0)  # clears min_gap_s, does NOT clear type_cooldown_s
    second = gen.generate(make_state(), session_id=SESSION_ID)
    assert second is None


def test_a_different_type_is_free_to_run_immediately_after() -> None:
    clock = FakeClock()
    catalog = (_tpl("a"), _tpl("b"))
    config = GeneratorConfig(max_concurrent_open=5, min_gap_s=1.0, type_cooldown_s=1000.0)
    gen = PredictionGenerator(catalog=catalog, clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config)
    first = gen.generate(make_state(), session_id=SESSION_ID)
    assert first is not None
    clock.advance(1.0)
    second = gen.generate(make_state(), session_id=SESSION_ID)
    assert second is not None
    assert second["prediction_type"] != first["prediction_type"]


# -- anti-spam: concurrent-open cap ---------------------------------------------


def test_concurrent_open_cap_blocks_a_third_prediction() -> None:
    clock = FakeClock()
    catalog = (_tpl("a"), _tpl("b"), _tpl("c"))
    config = GeneratorConfig(max_concurrent_open=2, min_gap_s=1.0, type_cooldown_s=1.0)
    gen = PredictionGenerator(catalog=catalog, clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config)
    first = gen.generate(make_state(), session_id=SESSION_ID)
    clock.advance(1.0)
    second = gen.generate(make_state(), session_id=SESSION_ID)
    assert first is not None and second is not None
    assert gen.open_count == 2
    clock.advance(1.0)
    third = gen.generate(make_state(), session_id=SESSION_ID)
    assert third is None  # cap reached, even though cooldown and type both clear


def test_concurrent_open_cap_clears_once_a_window_resolves() -> None:
    clock = FakeClock()
    catalog = (_tpl("a", window=Window(lock_delay_s=10.0, resolve_delay_s=20.0)), _tpl("b"))
    config = GeneratorConfig(max_concurrent_open=1, min_gap_s=1.0, type_cooldown_s=1.0)
    gen = PredictionGenerator(catalog=catalog, clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config)
    first = gen.generate(make_state(), session_id=SESSION_ID)
    assert first is not None
    clock.advance(1.0)
    assert gen.generate(make_state(), session_id=SESSION_ID) is None  # cap=1, still open
    clock.advance(30.0)  # first's window (10+20=30s) has fully elapsed
    third = gen.generate(make_state(), session_id=SESSION_ID)
    assert third is not None


# -- ranking / robustness --------------------------------------------------------


def test_a_misbehaving_trigger_does_not_crash_generation() -> None:
    def _boom(state, events):
        raise RuntimeError("a real bug in someone else's template")

    clock = FakeClock()
    catalog = (_tpl("broken", trigger=_boom), _tpl("fine"))
    gen = PredictionGenerator(catalog=catalog, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    row = gen.generate(make_state(), session_id=SESSION_ID)
    assert row is not None
    assert row["prediction_type"] == "fine"


def test_higher_score_wins_between_two_eligible_templates() -> None:
    clock = FakeClock()
    catalog = (_tpl("low", score=lambda s: 0.1), _tpl("high", score=lambda s: 0.9))
    gen = PredictionGenerator(catalog=catalog, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    row = gen.generate(make_state(), session_id=SESSION_ID)
    assert row is not None
    assert row["prediction_type"] == "high"


def test_a_zero_type_cooldown_does_not_crash_the_picker() -> None:
    """`type_cooldown_s=0` (no per-type cooldown at all) is a legal config, and
    it used to raise ZeroDivisionError out of the novelty term the moment a
    template came up for ranking a second time — escaping `generate` into the
    caller's 2-4 Hz main loop, which nothing in this module is allowed to do."""
    clock = FakeClock()
    catalog = (_tpl("a"), _tpl("b"))
    config = GeneratorConfig(max_concurrent_open=99, min_gap_s=1.0, type_cooldown_s=0.0)
    gen = PredictionGenerator(
        catalog=catalog, clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config
    )
    produced = 0
    for _ in range(10):
        if gen.generate(make_state(), session_id=SESSION_ID) is not None:
            produced += 1
        clock.advance(1.0)
    assert produced >= 5


def test_duplicate_prediction_type_in_a_supplied_catalog_is_refused() -> None:
    import pytest

    dupe = (_tpl("same"), _tpl("same"))
    with pytest.raises(ValueError, match="duplicate prediction_type"):
        PredictionGenerator(catalog=dupe)


# -- WANTED EVENT rarity: the constraint has to actually bind ----------------------


def _event_catalog() -> tuple[PredictionTemplate, ...]:
    """Two ordinary templates and one event, the event scoring highest.

    Two ordinary ones because "no duplicate prediction_type back-to-back" would
    otherwise stall the generator on every other tick and blur what the event
    cooldown is doing.
    """
    return (
        _tpl("ordinary_a", score=lambda s: 0.1),
        _tpl("ordinary_b", score=lambda s: 0.1),
        _tpl("special", score=lambda s: 0.9, is_event=True),
    )


def _run_for(gen: PredictionGenerator, clock: FakeClock, seconds: float, step: float) -> list[dict]:
    rows = []
    elapsed = 0.0
    while elapsed < seconds:
        row = gen.generate(make_state(), session_id=SESSION_ID)
        if row is not None:
            rows.append(row)
        clock.advance(step)
        elapsed += step
    return rows


def test_an_event_is_offered_rarely_even_when_it_always_triggers_and_always_wins() -> None:
    """The headline rarity test. Over six simulated hours with a template that
    triggers on every tick and outranks everything else, the event cooldown —
    not the telemetry, not the ranking — is what keeps events rare."""
    clock = FakeClock()
    config = GeneratorConfig(
        max_concurrent_open=99,
        min_gap_s=30.0,
        type_cooldown_s=0.0,
        event_cooldown_s=3600.0,
    )
    gen = PredictionGenerator(
        catalog=_event_catalog(), clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config
    )

    six_hours = 6 * 3600.0
    rows = _run_for(gen, clock, six_hours, step=30.0)
    events = [r for r in rows if r["is_event"]]

    # Plenty of ordinary predictions happened, so the run really exercised the
    # generator rather than starving it.
    assert len(rows) >= 700
    # ...and yet the event fired only about once an hour: the cooldown binds.
    assert 1 <= len(events) <= int(six_hours / config.event_cooldown_s) + 1
    assert len(events) / len(rows) < 0.02


def test_without_the_event_cooldown_the_same_template_would_flood() -> None:
    """The control for the test above: same catalog, same six hours, cooldown
    removed. If events did not flood here, the rarity test would be proving
    nothing about the cooldown."""
    clock = FakeClock()
    config = GeneratorConfig(
        max_concurrent_open=99, min_gap_s=30.0, type_cooldown_s=0.0, event_cooldown_s=0.0
    )
    gen = PredictionGenerator(
        catalog=_event_catalog(), clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config
    )
    rows = _run_for(gen, clock, 6 * 3600.0, step=30.0)
    events = [r for r in rows if r["is_event"]]
    assert len(events) / len(rows) > 0.4  # every other prediction, in practice


def test_two_events_never_land_back_to_back() -> None:
    """Even with the cooldown at zero, an event is never generated immediately
    after another event — the structural half of "keep them special"."""
    clock = FakeClock()
    catalog = (
        _tpl("ordinary", score=lambda s: 0.1),
        _tpl("special_a", score=lambda s: 0.9, is_event=True),
        _tpl("special_b", score=lambda s: 0.9, is_event=True),
    )
    config = GeneratorConfig(
        max_concurrent_open=99, min_gap_s=1.0, type_cooldown_s=0.0, event_cooldown_s=0.0
    )
    gen = PredictionGenerator(
        catalog=catalog, clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config
    )
    rows = _run_for(gen, clock, 200.0, step=1.0)
    assert len(rows) >= 100
    assert any(r["is_event"] for r in rows)
    flags = [r["is_event"] for r in rows]
    assert not any(a and b for a, b in pairwise(flags))


def test_the_event_cooldown_is_configurable_not_baked_in() -> None:
    clock = FakeClock()
    config = GeneratorConfig(
        max_concurrent_open=99, min_gap_s=1.0, type_cooldown_s=0.0, event_cooldown_s=120.0
    )
    gen = PredictionGenerator(
        catalog=_event_catalog(), clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config
    )
    first = gen.generate(make_state(), session_id=SESSION_ID)
    assert first is not None and first["is_event"] is True

    clock.advance(1.0)
    filler = gen.generate(make_state(), session_id=SESSION_ID)
    assert filler is not None and filler["is_event"] is False  # never back-to-back

    clock.advance(1.0)
    assert gen.generate(make_state(), session_id=SESSION_ID)["is_event"] is False  # still cooling

    clock.advance(120.0)  # cooldown elapsed
    assert gen.generate(make_state(), session_id=SESSION_ID)["is_event"] is True


def test_events_can_be_switched_off_without_touching_the_catalog() -> None:
    clock = FakeClock()
    config = GeneratorConfig(
        max_concurrent_open=99,
        min_gap_s=1.0,
        type_cooldown_s=0.0,
        event_cooldown_s=0.0,
        events_enabled=False,
    )
    gen = PredictionGenerator(
        catalog=_event_catalog(), clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config
    )
    rows = _run_for(gen, clock, 100.0, step=1.0)
    assert len(rows) >= 50
    assert not any(r["is_event"] for r in rows)


def test_an_event_on_cooldown_does_not_block_ordinary_predictions() -> None:
    """The cooldown must gate the event, never the whole generator."""
    clock = FakeClock()
    config = GeneratorConfig(
        max_concurrent_open=99, min_gap_s=1.0, type_cooldown_s=0.0, event_cooldown_s=1_000_000.0
    )
    gen = PredictionGenerator(
        catalog=_event_catalog(), clock=clock, wall_clock=lambda: 1_893_456_000.0, config=config
    )
    rows = _run_for(gen, clock, 100.0, step=1.0)
    assert len([r for r in rows if r["is_event"]]) == 1  # the very first one, then never again
    assert len([r for r in rows if not r["is_event"]]) >= 50


def test_the_real_catalog_flags_its_event_row_and_only_that_row() -> None:
    """End to end on the SHIPPED catalog: a real two-star gain produces a row
    carrying is_event=True; an ordinary moment produces is_event=False."""
    clock = FakeClock()
    gen = PredictionGenerator(catalog=CATALOG, clock=clock, wall_clock=lambda: 1_893_456_000.0)
    two_star_gain = {
        "type": "wanted_change",
        "payload": {"from": 1, "to": 2},
        "ts": "2026-09-08T00:00:00Z",
    }
    row = gen.generate(make_state(wanted=2), session_id=SESSION_ID, recent_events=[two_star_gain])
    assert row is not None
    assert row["prediction_type"] == "two_star_standoff"
    assert row["is_event"] is True

    clock.advance(30.0)
    ordinary = gen.generate(make_state(wanted=0), session_id=SESSION_ID)
    assert ordinary is not None
    assert ordinary["is_event"] is False


# -- determinism -------------------------------------------------------------------


def test_same_inputs_produce_the_same_decision() -> None:
    state = make_state(wanted=1, health=140)
    wall = lambda: 1_893_456_000.0  # noqa: E731

    gen1 = PredictionGenerator(catalog=CATALOG, clock=FakeClock(100.0), wall_clock=wall)
    gen2 = PredictionGenerator(catalog=CATALOG, clock=FakeClock(100.0), wall_clock=wall)

    row1 = gen1.generate(state, session_id=SESSION_ID)
    row2 = gen2.generate(state, session_id=SESSION_ID)
    assert row1 == row2
