"""Governor L3 — "asleep in the car": parks somewhere SCENIC (CONTRACTS §7).

L3 used to be cosmetic. Two things made it so:

1. it issued `stop` wherever the agent happened to be, which at the moment an hourly
   cap trips is as likely to be lane three of the Los Santos Freeway as anywhere
   worth looking at; and
2. the reflex layer's L2 "keep a wander task alive" branch was `level >= 2`, so
   the tick after the stop it posted `wander_drive` again — L3 never actually
   put him to sleep at all.

These tests drive the real `Harness` methods (bound to a minimal stand-in for
the harness's collaborators) rather than re-describing them.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from wasted_harness.behavior.activities import (
    LANDMARKS,
    SCENIC_PARK_SPOTS,
    ActivityPicker,
    ActivityRunner,
    nearest_scenic_spot,
    scenic_park_plan,
)
from wasted_harness.behavior.humanizer import MoodModel
from wasted_harness.behavior.missions import MissionTracker
from wasted_harness.behavior.planner import DayPlanner
from wasted_harness.behavior.recovery import (
    ClearedByGameBackoff,
    DamageTracker,
    DeathArrestRecovery,
    IdleBreaker,
    JackHandoffGate,
    RoadDodge,
    StrandedEscalator,
    StuckDetector,
    TaskStallDetector,
    ThreatLatch,
    WaterEscalator,
)
from wasted_harness.behavior.roam import HouseEscape, InteriorEscape, RoamEngine
from wasted_harness.behavior.vehicle import (
    ControlRegained,
    MovementWheel,
    VehicleController,
)
from wasted_harness.bridge_client import GameState
from wasted_harness.main import Harness
from wasted_harness.perception import Delta

VEHICLE = {
    "handle": 1,
    "model": "adder",
    "display_name": "Adder",
    "class": "Super",
    "speed": 30.0,
    "health": 1000.0,
    "upside_down": False,
    "in_water": False,
    "stopped_for_s": 0.0,
}


def make_state(**over: Any) -> GameState:
    """Contract-shaped /state. Same explicit builder style as test_recovery."""
    pos = over.pop("pos", (0.0, 0.0, 0.0))
    in_vehicle = over.pop("in_vehicle", True)
    body = {
        "ts": "2026-08-29T00:00:00Z",
        "tick": over.pop("tick", 1000),
        "player": {
            "pos": {"x": pos[0], "y": pos[1], "z": pos[2]},
            "heading": 0.0,
            "health": 200,
            "max_health": 200,
            "armor": 0,
            "wanted": 0,
            "cash": 0,
            "dead": over.pop("dead", False),
            "arrested": over.pop("arrested", False),
            "in_vehicle": in_vehicle,
            "control_enabled": True,
        },
        "vehicle": dict(VEHICLE) if in_vehicle else None,
        "location": {"street": "Vinewood Blvd", "zone": "Downtown Vinewood"},
        "world": {"clock": "13:45", "weather": "CLEAR", "timescale": 1.0},
        "mission": {"active": False, "random_event_active": False, "cutscene_active": False},
        "nearby": {"vehicles": [], "peds": []},
        "last_task": {
            "id": over.pop("task_id", None),
            "type": over.pop("task_type", None),
            "status": over.pop("task_status", "idle"),
            "detail": "",
        },
        "bridge": {"version": "1.0.0", "edition": "legacy"},
    }
    assert not over, f"unused overrides: {sorted(over)}"
    return GameState.model_validate(body)


class FakeGovernor:
    def __init__(self, level: int = 3) -> None:
        self.level = level


class StubHarness:
    """Just enough harness for the methods under test. Nothing is faked away
    that the assertions depend on — the methods themselves are the real ones."""

    def __init__(self, level: int = 3) -> None:
        self.governor = FakeGovernor(level)
        self.mood = MoodModel(rng=random.Random(1))
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.said: list[str] = []
        self.ended: list[str] = []
        self._park_task_id: str | None = None
        self._park_deadline = 0.0
        self._quiet_until = 0.0
        self._next_task_id: str | None = "t-park-1"
        # collaborators used by _reflex
        self.activity_runner = ActivityRunner(ActivityPicker(random.Random(1)), random.Random(1))
        self.missions = MissionTracker()
        # `_reflex` gates the stranded escalator and the L2 wander on the day
        # plan's mission block, so the stub carries the real planner.
        self.planner = DayPlanner(random.Random(1))
        self.stuck = StuckDetector()
        self.task_stall = TaskStallDetector()
        self.idle_breaker = IdleBreaker()
        self.stranded = StrandedEscalator()
        # T9 (findings.md R1/R5): fed every tick by `_reflex`, same as every
        # other stateful reflex tracker on this list.
        self.water = WaterEscalator()
        self.road_dodge = RoadDodge()
        self.jack_handoff = JackHandoffGate()
        # `_reflex` tries the house-escape ladder BEFORE the stranded ladder and
        # measures how long he has stood still with the roam engine, so both are
        # real here — a stub would hide the ordering that is the point of them.
        self.roam = RoamEngine(random.Random(1))
        self.house_escape = HouseEscape()
        self.interior_escape = InteriorEscape()
        self._interior_token = None
        self.threat_latch = ThreatLatch()
        self.damage = DamageTracker()
        self.vehicle = VehicleController()
        #: F6's stopwatch (behavior/vehicle.ControlRegained). Real, because
        #: `_reflex` feeds it every tick and its `resume` rung is part of the
        #: ladder these governor tests assert the ORDER of.
        self.control_regained = ControlRegained()
        self.wheel = MovementWheel()
        self.wheel.begin_tick()
        self.wheel.on_preempt("governor", self._governor_preempted)
        self.wheel.on_preempt("exit_interior", self._interior_preempted)
        self._governor_token = None
        self._roam_token = None
        self._pending_park = False

        class _Breaks:
            on_break = False

        self.breaks = _Breaks()
        self._threat_has_the_wheel = False
        self._screen_blocked = False
        self.death_recovery = DeathArrestRecovery()
        self.primitives = None
        self.bridge = None
        self.counters = {"deaths": 0, "busted": 0, "missions_passed": 0}
        self.writer = _Recorder()
        self.bus = _Recorder()
        self.memory = _Recorder()
        self.commentary = _Recorder()

    #: The real movement arbitration: the L3 park holds the wheel from the
    #: scenic drive until the `stop` at the end of it, and `_reflex`'s ladder
    #: is what these tests are about.
    _game_owns_controls = Harness._game_owns_controls
    _game_control_reason = Harness._game_control_reason
    _reflex_act = Harness._reflex_act
    _break_the_idle = Harness._break_the_idle
    _governor_preempted = Harness._governor_preempted
    _interior_preempted = Harness._interior_preempted
    _release_interior_wheel = Harness._release_interior_wheel
    _run_interior_escape = Harness._run_interior_escape

    # the two methods under test call these
    def _execute_action(
        self, action_type: str, params: dict[str, Any], token: Any = None
    ) -> str | None:
        from wasted_harness.brain.schemas import BRIDGE_TASKS

        if action_type in BRIDGE_TASKS:
            assert self.wheel.holds(token), f"{action_type} posted without the wheel"
            self.wheel.mark_posted(token, action_type)
            self.posted.append((action_type, dict(params)))
            return self._next_task_id
        self.posted.append((action_type, dict(params)))
        return None

    def _vehicle_hold(self, state: GameState) -> str | None:
        # The real one: governor L3 ("asleep in the car, parked somewhere
        # scenic", CONTRACTS §7) is one of the reasons it returns a hold, and
        # that is exactly what these tests are about.
        return Harness._vehicle_hold(self, state)

    def _say(self, text: str, mood: str | None = None) -> None:
        self.said.append(text)

    def _end_activity_if_running(self, outcome: str, *, by: str | None = None) -> None:
        self.ended.append(outcome)

    def _capture_screenshot(self, hint: str):
        return None, None

    def _capture_clip_async(self, event_type: str, caption: str) -> None:
        pass

    def _handle_mission_events(self, state, delta) -> None:
        # Borrow the REAL wiring (extracted from _reflex) so this stub keeps exercising
        # the production mission-event path.
        Harness._handle_mission_events(self, state, delta)


class _Recorder:
    """Absorbs the bookkeeping calls _reflex makes; records nothing we assert on."""
    #: Stands in for `SupabaseWriter`, which carries this flag; `Harness._heartbeat`
    #: reads it to refuse publishing a heartbeat over an unflushed backlog.
    unflushed = False


    def __getattr__(self, _name: str):
        return lambda *a, **k: None


def begin(stub: StubHarness, state: GameState) -> None:
    Harness._begin_scenic_park(stub, state)


def finish(stub: StubHarness, state: GameState) -> None:
    Harness._finish_scenic_park_if_arrived(stub, state)


# --- the scenic spot itself ----------------------------------------------------


def test_scenic_spots_are_real_landmarks() -> None:
    for name in SCENIC_PARK_SPOTS:
        assert name in LANDMARKS, f"{name} is not a landmark this package knows"


def test_nearest_scenic_spot_picks_the_nearest_one() -> None:
    for name in SCENIC_PARK_SPOTS:
        x, y, z = LANDMARKS[name]
        chosen, coords, dist = nearest_scenic_spot((x + 5.0, y + 5.0, z))
        assert chosen == name, f"standing at {name}, picked {chosen}"
        assert coords == LANDMARKS[name]
        assert dist == pytest.approx(50**0.5, abs=0.01)


def test_scenic_park_plan_drives_then_stops() -> None:
    spot, plan = scenic_park_plan(LANDMARKS["del_perro_pier"], "normal")
    assert spot == "del_perro_pier"
    assert [step["type"] for step in plan] == ["drive_to", "stop"]
    target = plan[0]["params"]
    assert (target["x"], target["y"], target["z"]) == LANDMARKS["del_perro_pier"]
    assert target["style"] == "normal"


# --- L3 behaviour --------------------------------------------------------------


def test_l3_drives_to_a_scenic_spot_instead_of_stopping_where_he_is() -> None:
    """The §7 wording is 'parks somewhere scenic', not 'stops'."""
    stub = StubHarness()
    near_observatory = LANDMARKS["galileo_observatory"]
    state = make_state(pos=(near_observatory[0] + 300, near_observatory[1] + 300, 0.0))
    begin(stub, state)
    assert stub.ended == ["governor_l3"]
    assert [t for t, _ in stub.posted] == ["drive_to"], (
        "L3 must drive somewhere scenic first, not stop mid-freeway"
    )
    params = stub.posted[0][1]
    assert (params["x"], params["y"], params["z"]) == near_observatory
    assert stub._park_task_id == "t-park-1"


def test_l3_stops_once_the_scenic_drive_finishes() -> None:
    stub = StubHarness()
    begin(stub, make_state(pos=(0.0, 0.0, 0.0)))
    stub.posted.clear()
    # Still driving: nothing yet.
    finish(stub, make_state(task_id="t-park-1", task_type="drive_to", task_status="running"))
    assert stub.posted == []
    # Arrived.
    finish(stub, make_state(task_id="t-park-1", task_type="drive_to", task_status="done"))
    assert [t for t, _ in stub.posted] == ["stop"]
    assert stub._park_task_id is None
    assert any("Parked" in line for line in stub.said)


def test_l3_matches_the_authoritative_park_task_id_not_a_stale_one() -> None:
    """Same 3 Hz-vs-60 Hz race as the activity runner: a stale `done` must not
    be read as "arrived" and stop him halfway there."""
    stub = StubHarness()
    begin(stub, make_state())
    stub.posted.clear()
    finish(stub, make_state(task_id="t-park-0", task_type="stop", task_status="done"))
    assert stub.posted == [], "the PREVIOUS task's done is not our arrival"
    assert stub._park_task_id == "t-park-1"


def test_l3_gives_up_on_an_unreachable_scenic_spot() -> None:
    """Curated coordinates are unverified until Phase 3; one bad number must not
    leave him circling for the whole budget window."""
    import wasted_harness.main as main_mod

    stub = StubHarness()
    begin(stub, make_state())
    stub.posted.clear()
    stub._park_deadline = main_mod.time.monotonic() - 1.0  # deadline already passed
    finish(stub, make_state(task_id="t-park-1", task_type="drive_to", task_status="running"))
    assert [t for t, _ in stub.posted] == ["stop"]
    assert stub._park_task_id is None


def test_l3_on_foot_just_stops_and_says_so() -> None:
    stub = StubHarness()
    begin(stub, make_state(in_vehicle=False))
    assert [t for t, _ in stub.posted] == ["stop"]
    assert stub._park_task_id is None
    assert any("Standing right here" in line for line in stub.said)


def test_l3_stops_where_he_is_when_the_drive_cannot_be_posted() -> None:
    stub = StubHarness()
    stub._next_task_id = None  # bridge down / not ready at that moment
    begin(stub, make_state())
    assert [t for t, _ in stub.posted] == ["drive_to", "stop"]
    assert stub._park_task_id is None


def test_l3_releases_the_park_when_the_hour_resets() -> None:
    stub = StubHarness()
    begin(stub, make_state())
    stub.posted.clear()
    stub.governor.level = 0  # hourly window reset; he is awake again
    finish(stub, make_state(task_id="t-park-1", task_type="drive_to", task_status="done"))
    assert stub.posted == [], "he is awake; do not stop his drive"
    assert stub._park_task_id is None


# --- the reflex layer must not undo it ----------------------------------------


def _reflex(stub: StubHarness, state: GameState) -> None:
    Harness._reflex(stub, state, Delta(wanted_from=0, wanted_to=0))


def test_reflex_wander_drives_at_l2() -> None:
    stub = StubHarness(level=2)
    _reflex(stub, make_state(task_status="idle"))
    assert [t for t, _ in stub.posted] == ["wander_drive"]


def test_reflex_does_not_wander_at_l3() -> None:
    """Regression: `level >= 2` meant L3 posted a wander every tick, so the
    scenic park (and the whole 'asleep' idea) lasted exactly one tick."""
    stub = StubHarness(level=3)
    _reflex(stub, make_state(task_status="idle"))
    assert stub.posted == [], f"L3 must stay parked, posted {stub.posted}"

# The real `_reflex` now consults the phone reflex and the game-cleared backoff every tick.
# Stubs get a quiet phone and an open backoff so every existing scenario is unchanged.
def _quiet_phone(self, state):
    return False


StubHarness._phone_reflex = _quiet_phone  # type: ignore[attr-defined]
StubHarness.cleared_backoff = ClearedByGameBackoff()  # type: ignore[attr-defined]
