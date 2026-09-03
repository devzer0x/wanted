"""`tests/support/replayer.py`: the REAL selection code, driven offline.

Not exhaustive re-tests of `_reflex`/`_drive_activities`/etc — those are
`test_mission_following.py`/`test_interior_escape.py`'s job. This file proves
the DRIVER itself: that a full tick runs end to end with no network and no
AttributeError, that the fake brain actually gets reached and its decision
actually gets applied, and that the structured log captures what the brief
asks for (posted tasks, say lines, events, wheel transitions).
"""

from __future__ import annotations

from support.replayer import ReplayHarness, replay
from support.states import make_state


def _run(h: ReplayHarness, state, ticks: int, step_s: float = 0.33) -> None:
    for _ in range(ticks):
        h.clock.advance(step_s)
        h.feed(state)


def test_a_bare_tick_runs_with_no_network_and_no_attribute_error() -> None:
    h = ReplayHarness(seed=1)
    h.feed(make_state())
    assert h._tick_n == 1


def test_idle_car_produces_a_roam_pick_and_a_posted_task() -> None:
    h = ReplayHarness(seed=1)
    _run(h, make_state(in_vehicle=True), 3)
    kinds = {r["kind"] for r in h.log.records}
    assert "posted_task" in kinds
    assert "event" in kinds  # activity_start
    assert h.roam.current is not None


def test_the_fake_brain_is_actually_reached_and_applied() -> None:
    h = ReplayHarness(seed=1, brain_mode="policy")
    _run(h, make_state(in_vehicle=True), 90)
    assert h.fake_tactical.calls or h.fake_director.calls
    decisions = [r for r in h.log.records if r["kind"] == "decision"]
    assert decisions, "no decision was ever recorded"
    says = [r for r in h.log.records if r["kind"] == "say"]
    assert says, "the brain's own say line never reached the bus"


def test_wheel_transitions_are_logged() -> None:
    h = ReplayHarness(seed=1)
    _run(h, make_state(in_vehicle=True), 3)
    wheel_events = [r for r in h.log.records if r["kind"] == "wheel"]
    assert wheel_events
    assert wheel_events[0]["owner"] == "roam"


def test_cutscene_produces_no_posted_movement_task() -> None:
    h = ReplayHarness(seed=1)
    _run(h, make_state(cutscene_active=True, mission_active=True), 5)
    movement_posts = [
        r for r in h.log.records if r["kind"] == "posted_task" and r.get("movement")
    ]
    assert movement_posts == []


def test_death_then_respawn_is_captured_as_real_events() -> None:
    h = ReplayHarness(seed=1)
    _run(h, make_state(in_vehicle=True), 2)
    _run(h, make_state(dead=True, health=0), 2)
    _run(h, make_state(dead=False, health=200, pos=(50.0, 50.0, 0.0)), 2)
    events = [r["type"] for r in h.log.records if r["kind"] == "event"]
    assert "death" in events


def test_states_are_captured_for_funcheck_window_checks() -> None:
    h = ReplayHarness(seed=1)
    _run(h, make_state(in_vehicle=True), 4)
    assert len(h.log.states) == 4
    assert h.log.states[0]["state"]["player"]["in_vehicle"] is True


def test_replay_helper_advances_the_clock_from_recorded_ts_deltas() -> None:
    states = [make_state(tick=i) for i in range(3)]
    ts = [
        "2026-09-03T00:00:00.000Z",
        "2026-09-03T00:00:04.000Z",
        "2026-09-03T00:00:09.000Z",
    ]
    log = replay(states, ts=ts, seed=1)
    clocks = [s["clock_t"] for s in log.states]
    assert clocks == [0.0, 4.0, 9.0]


def test_replay_helper_is_deterministic_given_the_same_seed_and_states() -> None:
    states = [make_state(in_vehicle=True, tick=i) for i in range(20)]
    a = replay(states, seed=7, default_tick_s=0.33)
    b = replay(states, seed=7, default_tick_s=0.33)
    a_types = [(r["kind"], r.get("type"), r.get("text")) for r in a.records]
    b_types = [(r["kind"], r.get("type"), r.get("text")) for r in b.records]
    assert a_types == b_types
