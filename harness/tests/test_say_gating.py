"""T3 (findings.md R2): a line only on an EVENT.

Tactical decisions fire on every poll (8-25 s); before this ticket every one
of them published `d.say` unconditionally, so a stationary the agent narrated a
fresh line every cycle with nothing behind it. `_tick_event_reason` names the
event set in code; `Harness._apply_decision` forces `say` to "" before
publish/record when it returns None, even when the model wrote a line.
"""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support.states import make_state

from wasted_harness.behavior.recovery import IdleBreaker
from wasted_harness.behavior.roam import RoamEngine
from wasted_harness.brain.schemas import ActionModel, DecisionModel
from wasted_harness.brain.tactical import DecisionResult
from wasted_harness.commentary import Commentary
from wasted_harness.main import (
    FIGHT_ACTION_TYPES,
    Harness,
    _tick_event_reason,
)
from wasted_harness.perception import Delta

QUIET_DELTA = Delta(wanted_from=0, wanted_to=0)


def _decision(
    say: str = "Right on schedule.",
    goal: str = "see the city",
    action_type: str = "wait",
    params: dict[str, Any] | None = None,
    mood: str = "chill",
) -> DecisionModel:
    return DecisionModel(
        thought="Nothing much going on, just narrating.",
        say=say,
        mood=mood,
        action=ActionModel(type=action_type, params=params or {}),
        goal=goal,
        confidence=0.6,
    )


def _result(decision: DecisionModel) -> DecisionResult:
    return DecisionResult(
        decision=decision,
        model="fake-model",
        input_tokens=10,
        output_tokens=10,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=0.0,
    )


# --- _tick_event_reason: the event set, in isolation ---------------------------


def test_no_event_on_a_plain_timer_tick() -> None:
    assert _tick_event_reason(QUIET_DELTA, None, False, "wait") is None


def test_a_pending_big_event_is_an_event() -> None:
    assert _tick_event_reason(QUIET_DELTA, "death", False, "wait") == "event:death"


def test_a_roam_transition_is_an_event() -> None:
    assert _tick_event_reason(QUIET_DELTA, None, True, "wait") == "roam_goal_transition"


def test_a_vehicle_change_is_an_event() -> None:
    d = Delta(wanted_from=0, wanted_to=0, entered_vehicle=True)
    assert _tick_event_reason(d, None, False, "wait") == "vehicle_change"
    d = Delta(wanted_from=0, wanted_to=0, exited_vehicle=True)
    assert _tick_event_reason(d, None, False, "wait") == "vehicle_change"


def test_a_wanted_change_is_an_event() -> None:
    d = Delta(wanted_from=0, wanted_to=2)
    assert _tick_event_reason(d, None, False, "wait") == "wanted_change"


def test_death_is_an_event_even_without_a_pending_big_event() -> None:
    d = Delta(wanted_from=0, wanted_to=0, died=True)
    assert _tick_event_reason(d, None, False, "wait") == "death"


def test_control_regained_covers_respawn_and_cutscene_end() -> None:
    d = Delta(wanted_from=0, wanted_to=0, respawned=True)
    assert _tick_event_reason(d, None, False, "wait") == "control_regained"
    d = Delta(wanted_from=0, wanted_to=0, cutscene_ended=True)
    assert _tick_event_reason(d, None, False, "wait") == "control_regained"


def test_choosing_a_fight_action_is_an_event() -> None:
    for action_type in FIGHT_ACTION_TYPES:
        assert _tick_event_reason(QUIET_DELTA, None, False, action_type) == "fight"


def test_task_finished_danger_and_objective_change_are_deliberately_not_events() -> None:
    """These are the tactical cadence's OWN trigger reasons, not part of the
    event set T3 names — `danger` alone is true on every poll for the whole
    length of a chase (brain.tactical's own docs), so treating it as an event
    would recreate the "narrating nothing" bug at a different name."""
    d = Delta(wanted_from=2, wanted_to=2, danger=True, task_finished=True, objective_changed=True)
    assert _tick_event_reason(d, None, False, "wait") is None


# --- `_apply_decision`: the gate applied for real -------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, *a: Any, **k: Any) -> None:
        self.calls.append((a, dict(k)))


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        self.published.append((kind, payload))


class _Writer:
    #: Stands in for `SupabaseWriter`, which carries this flag; `Harness._heartbeat`
    #: reads it to refuse publishing a heartbeat over an unflushed backlog.
    unflushed = False

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []

    def record_decision(self, row: dict[str, Any]) -> None:
        self.decisions.append(row)


class _MoodStub:
    """A fixed tracked mood — T5 also checks the published mood comes from
    here, never from `d.mood`."""

    mood = "scared"


class _ApplyDecisionStub:
    """Just enough of the harness for the REAL `Harness._apply_decision`."""

    def __init__(self, tmp_path: Path) -> None:
        self.missions = SimpleNamespace(in_mission=False)
        self._mission_tokens = 0
        self.writer = _Writer()
        self.roam = RoamEngine(random.Random(1))  # `.current` is None: nothing locked
        self.wheel = SimpleNamespace(acquire=lambda *a, **k: None, owner=None, reason=None)
        self.rng = random.Random(1)
        self.commentary = Commentary(tmp_path)
        self.bus = _Bus()
        self.memory = SimpleNamespace(log_day=lambda *a, **k: None)
        self.current_goal = "see the city"
        self.mood = _MoodStub()
        self._tick_roam_transition = False
        self._mission_active = False
        self.idle_breaker = IdleBreaker()
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def _free_roam_owns_movement(self, action_type: str) -> bool:
        return Harness._free_roam_owns_movement(self, action_type)

    def _going_nowhere_quietly(self, state: Any, d: Any) -> bool:
        # The REAL gate, so these tests keep testing the shipped rule.
        return Harness._going_nowhere_quietly(self, state, d)

    def _execute_action(
        self, action_type: str, params: dict[str, Any], token: Any = None
    ) -> str | None:
        self.executed.append((action_type, dict(params)))
        return None

    def _end_activity_if_running(self, outcome: str, *, by: str | None = None) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_apply_decision` sleeps 300-900 ms per reaction; these tests do not
    need real wall-clock time to pass."""
    monkeypatch.setattr("wasted_harness.main.time.sleep", lambda *_a, **_k: None)


def test_a_plain_poll_tick_with_no_event_publishes_nothing(tmp_path: Path) -> None:
    stub = _ApplyDecisionStub(tmp_path)
    state = make_state()
    result = _result(_decision(say="Third green light in a row. Something's wrong."))
    Harness._apply_decision(stub, "tactical", result, state, QUIET_DELTA, None)
    assert stub.bus.published == [], "no event this tick; nothing may reach the bus"
    # The `decisions` table still has the model's raw, unforced line — this is
    # an audit trail, not the published/remembered copy.
    assert stub.writer.decisions[0]["say"] == "Third green light in a row. Something's wrong."


def test_a_tick_with_a_roam_transition_publishes_the_line(tmp_path: Path) -> None:
    stub = _ApplyDecisionStub(tmp_path)
    stub._tick_roam_transition = True
    state = make_state()
    result = _result(_decision(say="New plan. Let's see how this goes."))
    Harness._apply_decision(stub, "tactical", result, state, QUIET_DELTA, None)
    assert len(stub.bus.published) == 1
    kind, payload = stub.bus.published[0]
    assert kind == "say"
    assert payload["text"] == "New plan. Let's see how this goes."


def test_a_big_event_publishes_the_line(tmp_path: Path) -> None:
    stub = _ApplyDecisionStub(tmp_path)
    state = make_state()
    result = _result(_decision(say="That's going to leave a mark."))
    Harness._apply_decision(stub, "tactical", result, state, QUIET_DELTA, "death")
    assert len(stub.bus.published) == 1
    assert stub.bus.published[0][1]["text"] == "That's going to leave a mark."


def test_a_death_delta_publishes_even_with_no_pending_big_event(tmp_path: Path) -> None:
    stub = _ApplyDecisionStub(tmp_path)
    state = make_state()
    delta = Delta(wanted_from=0, wanted_to=0, died=True)
    result = _result(_decision(say="Physics: undefeated."))
    Harness._apply_decision(stub, "tactical", result, state, delta, None)
    assert len(stub.bus.published) == 1


def test_the_no_repeat_memory_sees_the_forced_empty_line_not_the_original(
    tmp_path: Path,
) -> None:
    """`commentary.record_say` must stay honest about what was actually shown
    — a suppressed line does not silently occupy the no-repeat window."""
    stub = _ApplyDecisionStub(tmp_path)
    state = make_state()
    result = _result(_decision(say="A line that never goes out."))
    Harness._apply_decision(stub, "tactical", result, state, QUIET_DELTA, None)
    assert stub.commentary.recent.recent() == [""]


def test_a_movement_task_still_executes_even_with_no_event(tmp_path: Path) -> None:
    """T3 governs `say` only — the action always goes out. A quiet tick must
    not also silence the ACTION, or the show simply stops moving."""
    stub = _ApplyDecisionStub(tmp_path)
    state = make_state()
    result = _result(_decision(say="", action_type="look_around"))
    Harness._apply_decision(stub, "tactical", result, state, QUIET_DELTA, None)
    assert stub.executed == [("look_around", {})]


def test_published_mood_is_the_tracked_mood_not_the_models(tmp_path: Path) -> None:
    """T5: the model's own `mood` field is advisory at most."""
    stub = _ApplyDecisionStub(tmp_path)
    state = make_state()
    result = _result(_decision(say="Hyped about this.", mood="hyped"))
    Harness._apply_decision(stub, "tactical", result, state, QUIET_DELTA, "death")
    assert stub.bus.published[0][1]["mood"] == "scared"  # _MoodStub.mood, not "hyped"
    # The raw model mood is still preserved on the audit row.
    assert stub.writer.decisions[0]["mood"] == "hyped"


def test_in_free_roam_a_model_movement_action_yields_to_the_goal_engine(tmp_path: Path) -> None:
    """LIVE 2026-09-03 12:20Z: the model answered `enter_nearest_vehicle` on every
    think, `brain` (mission class) took the wheel over `roam` each time and the goal
    it had just been offered lasted 4 s. With a menu on offer the model picks the
    goal; the engine moves him. With nothing on offer the action is still obeyed."""
    stub = _ApplyDecisionStub(tmp_path)
    stub.wheel = SimpleNamespace(
        acquire=lambda *a, **k: object(), owner=None, reason=None, release=lambda *a, **k: None,
        token_for=lambda *a, **k: object(),
    )
    on_foot = make_state(in_vehicle=False)
    stub.roam.observe(on_foot)
    assert stub.roam.offered_ids(), "the fallback goal is always on the menu"
    result = _result(
        _decision(action_type="enter_nearest_vehicle", params={"prefer": "any", "search_radius_m": 50.0})
    )
    Harness._apply_decision(stub, "tactical", result, on_foot, QUIET_DELTA, None)
    assert stub.executed == [], "a movement action must not preempt the goal engine's pick"
    # Nothing on offer (a fresh engine that has observed nothing): the model may move him.
    stub2 = _ApplyDecisionStub(tmp_path)
    stub2.wheel = stub.wheel
    Harness._apply_decision(stub2, "tactical", result, on_foot, QUIET_DELTA, None)
    assert [t for t, _ in stub2.executed] == ["enter_nearest_vehicle"]
