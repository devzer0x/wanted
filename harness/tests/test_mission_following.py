"""Mission-objective following (WP-H item 1) and the cutscene/mission guards
around it.

The specific regression this closes: while a mission was active (including
mid-cutscene), nothing steered toward `mission.objective_blip` and the
free-roam reflexes kept running — observed on the real server as a "find a
vehicle" search repeatedly widening (50m -> 90m -> 140m) while the agent stood
inside a scripted cutscene.

Game states here are constructed pydantic objects (pure-function inputs), same
style as test_recovery.py / test_governor_l3.py — the fixtures directory stays
empty by design (CLAUDE.md rule 1: no hand-invented recordings).
"""

from __future__ import annotations

import json
import random
import time
from types import SimpleNamespace
from typing import Any

import httpx
from support import throwaway_learned_scripts, throwaway_totals

from wasted_harness.behavior.activities import ActivityPicker, ActivityRunner
from wasted_harness.behavior.humanizer import MoodModel
from wasted_harness.behavior.missions import (
    DRIVE_ARRIVE_RADIUS_M,
    FOLLOW_CHASE_SPEED_MPS,
    FOLLOW_GAP_WIDEN_M,
    FOLLOW_GAP_WINDOW_S,
    FOLLOW_RECOVERY_EPISODE_RESET_S,
    FOLLOW_RECOVERY_EXHAUSTED_S,
    FOLLOW_RECOVERY_RUNG_GAP_S,
    FOLLOW_RECOVERY_VEHICLE_RADIUS_M,
    MUTUAL_STALL_WINDOW_S,
    STUCK_WINDOW_S,
    WALK_ARRIVE_RADIUS_M,
    WALK_SWITCH_RADIUS_M,
    MissionEvent,
    MissionFollower,
    MissionTracker,
)
from wasted_harness.behavior.planner import ROAM_BLOCK_S, DayPlanner
from wasted_harness.behavior.recovery import (
    THREAT_HOLD_S,
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
from wasted_harness.brain.schemas import BRIDGE_TASKS, MOVEMENT_TASKS
from wasted_harness.brain.vision import MissionOutcome
from wasted_harness.bridge_client import BridgeApiError, BridgeClient, GameState
from wasted_harness.main import INITIAL_GOAL, PHONE_HANGUP_AFTER_S, Harness
from wasted_harness.perception import Delta

VEHICLE = {
    "handle": 1,
    "model": "adder",
    "display_name": "Adder",
    "class": "Super",
    "speed": 20.0,
    "health": 1000.0,
    "upside_down": False,
    "in_water": False,
    "stopped_for_s": 0.0,
}


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


def make_state(**over: Any) -> GameState:
    """A minimal, contract-shaped /state body. Every field explicit."""
    pos = over.pop("pos", (0.0, 0.0, 0.0))
    blip = over.pop("objective_blip", None)
    in_vehicle = over.pop("in_vehicle", False)
    vehicle_override = over.pop("vehicle", None)
    mission: dict[str, Any] = {
        "active": over.pop("mission_active", False),
        "random_event_active": over.pop("random_event_active", False),
        "cutscene_active": over.pop("cutscene_active", False),
        "retry_in_flight": over.pop("retry_in_flight", False),
        # CONTRACTS v1.7 mission-start markers: [((x, y, z), protagonist), ...]
        "starts": [
            {"pos": {"x": m[0][0], "y": m[0][1], "z": m[0][2]}, "protagonist": m[1]}
            for m in over.pop("starts", [])
        ],
        # CONTRACTS v1.8 route blips: [((x, y, z), kind, handle), ...]
        "route_blips": [
            {
                "pos": {"x": r[0][0], "y": r[0][1], "z": r[0][2]},
                "kind": r[1],
                "handle": r[2],
                "color": "Yellow",
            }
            for r in over.pop("route_blips", [])
        ],
        # CONTRACTS v1.11 entity blips: [((x, y, z), handle, name), ...]
        "entity_blips": [
            {
                "pos": {"x": e[0][0], "y": e[0][1], "z": e[0][2]},
                "handle": e[1],
                "color": "Blue",
                "is_route": False,
                "distance": ((e[0][0] - pos[0]) ** 2 + (e[0][1] - pos[1]) ** 2) ** 0.5,
                "name": e[2] if len(e) > 2 else None,
            }
            for e in over.pop("entity_blips", [])
        ],
    }
    if blip is not None:
        bx, by, bz = blip.get("pos", (0.0, 0.0, 0.0))
        mission["objective_blip"] = {
            "pos": {"x": bx, "y": by, "z": bz},
            "kind": blip.get("kind", "coord"),
            "handle": blip.get("handle", 1),
        }
    body = {
        "ts": "2026-08-25T21:14:03.221Z",
        "tick": over.pop("tick", 1000),
        "player": {
            "pos": {"x": pos[0], "y": pos[1], "z": pos[2]},
            "heading": 0.0,
            "health": over.pop("health", 200),
            "max_health": 200,
            "armor": 0,
            "wanted": over.pop("wanted", 0),
            "cash": 0,
            "dead": over.pop("dead", False),
            "arrested": over.pop("arrested", False),
            "in_vehicle": in_vehicle,
            "control_enabled": over.pop("control_enabled", True),
            "switch_in_progress": over.pop("switch_in_progress", False),
            # CONTRACTS v1.12: `interior` is `(id, since_s)` or None; the key is
            # ALWAYS present here because that is what a >= 1.5.0 bridge sends,
            # and "key absent" is a different, separately tested case.
            "interior": (
                None
                if (interior := over.pop("interior", None)) is None
                else {"id": interior[0], "since_s": interior[1]}
            ),
            "last_outdoor": (
                None
                if (last_outdoor := over.pop("last_outdoor", None)) is None
                else {"x": last_outdoor[0], "y": last_outdoor[1], "z": last_outdoor[2]}
            ),
        },
        "vehicle": (vehicle_override or dict(VEHICLE)) if in_vehicle else None,
        "location": {"street": "Cavalry Blvd", "zone": "North Yankton"},
        "world": {"clock": "05:00", "weather": "SNOWING", "timescale": 1.0},
        "mission": mission,
        # CONTRACTS v1.13: the phone. Quiet unless a test says otherwise.
        "phone": over.pop("phone", {"ringing": False, "in_call": False}),
        "nearby": {
            "vehicles": over.pop("nearby_vehicles", []),
            "peds": over.pop("nearby_peds", []),
        },
        "last_task": {
            "id": over.pop("task_id", None),
            "type": over.pop("task_type", None),
            "status": over.pop("task_status", "idle"),
            "detail": over.pop("task_detail", ""),
        },
        "bridge": {"version": "1.0.0", "edition": "legacy"},
    }
    assert not over, f"unused overrides: {sorted(over)}"
    return GameState.model_validate(body)


# --- MissionFollower: gating -------------------------------------------------


def test_no_mission_active_means_no_navigation() -> None:
    f = MissionFollower()
    state = make_state(mission_active=False, objective_blip={"pos": (500.0, 0.0, 0.0)})
    assert f.plan(state) is None


def test_cutscene_active_means_no_navigation_even_with_a_blip() -> None:
    """The exact regression: a live objective blip must not turn into a task
    while the game is ignoring tasks."""
    f = MissionFollower()
    state = make_state(
        mission_active=True, cutscene_active=True, objective_blip={"pos": (500.0, 0.0, 0.0)}
    )
    assert f.plan(state) is None


def test_mission_active_but_no_blip_means_no_navigation() -> None:
    f = MissionFollower()
    state = make_state(mission_active=True, objective_blip=None)
    assert f.plan(state) is None


# --- MissionFollower: drive vs walk vs get-a-car -----------------------------


def test_drives_when_in_vehicle_and_far() -> None:
    f = MissionFollower()
    state = make_state(
        mission_active=True,
        in_vehicle=True,
        pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
    )
    step = f.plan(state)
    assert step is not None
    assert step["type"] == "drive_to"
    assert (step["params"]["x"], step["params"]["y"], step["params"]["z"]) == (300.0, 0.0, 0.0)
    assert step["params"]["arrive_radius_m"] == DRIVE_ARRIVE_RADIUS_M


def test_walks_when_on_foot_and_close() -> None:
    f = MissionFollower()
    state = make_state(
        mission_active=True,
        in_vehicle=False,
        pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (30.0, 0.0, 0.0)},
    )
    step = f.plan(state)
    assert step is not None
    assert step["type"] == "walk_to"
    assert WALK_SWITCH_RADIUS_M >= 30.0


def test_seeks_a_vehicle_when_on_foot_and_far() -> None:
    f = MissionFollower()
    state = make_state(
        mission_active=True,
        in_vehicle=False,
        pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (WALK_SWITCH_RADIUS_M + 50.0, 0.0, 0.0)},
    )
    step = f.plan(state)
    assert step is not None
    assert step["type"] == "enter_nearest_vehicle"
    assert step["params"]["prefer"] == "any"


def test_arrived_within_radius_stops_pushing() -> None:
    f = MissionFollower()
    state = make_state(
        mission_active=True,
        in_vehicle=False,
        pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (WALK_ARRIVE_RADIUS_M - 0.5, 0.0, 0.0)},
    )
    assert f.plan(state) is None


# --- MissionFollower: does not fight whoever has the wheel -------------------


def test_does_not_reissue_while_its_own_task_is_running() -> None:
    f = MissionFollower()
    far = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
    )
    first = f.plan(far)
    assert first is not None
    f.bind_task("t-mission-1")
    # Same target, our own task still running: no re-post.
    still_far = make_state(
        mission_active=True, in_vehicle=True, pos=(10.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
        task_id="t-mission-1", task_status="running",
    )
    assert f.plan(still_far) is None


def test_reissues_once_its_own_task_reaches_a_terminal_state() -> None:
    f = MissionFollower()
    far = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
    )
    f.plan(far)
    f.bind_task("t-mission-1")
    done = make_state(
        mission_active=True, in_vehicle=True, pos=(50.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
        task_id="t-mission-1", task_status="done",
    )
    step = f.plan(done)
    assert step is not None
    assert step["type"] == "drive_to"


def test_a_task_cleared_by_the_game_reissues_just_like_any_other_failed_task() -> None:
    """CONTRACTS v1.10: when the game itself clears a task (mission scripted
    beats, cutscenes), `last_task` reports `failed` + `detail:
    "cleared_by_game"` instead of hanging in `running` forever. Nothing in
    this package may special-case that detail string — a `failed` status
    already re-plans regardless of WHY it failed, and this must keep being
    true without any new code path."""
    f = MissionFollower()
    far = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
    )
    f.plan(far)
    f.bind_task("t-mission-1")
    cleared = make_state(
        mission_active=True, in_vehicle=True, pos=(50.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
        task_id="t-mission-1", task_status="failed", task_detail="cleared_by_game",
    )
    step = f.plan(cleared)
    assert step is not None
    assert step["type"] == "drive_to"


def test_defers_to_a_foreign_running_task() -> None:
    """A running task the follower never bound (the brain, an activity, an
    idle pick) must not be preempted by the reflex."""
    f = MissionFollower()
    state = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
        task_id="t-brain-99", task_type="combat_hated_targets_around", task_status="running",
    )
    assert f.plan(state) is None


def test_a_bound_task_preempted_by_someone_else_is_not_fought() -> None:
    f = MissionFollower()
    far = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
    )
    f.plan(far)
    f.bind_task("t-mission-1")
    preempted = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
        task_id="t-brain-1", task_status="running",
    )
    assert f.plan(preempted) is None


# --- MissionFollower: stuck / failure handling -------------------------------


def test_gives_up_after_the_stuck_window_then_resumes_if_progress_improves() -> None:
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    blip = {"pos": (300.0, 0.0, 0.0)}
    state = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0), objective_blip=blip
    )
    assert f.plan(state) is not None  # first sight: chase it

    # No progress for longer than the stuck window: back off.
    clock.tick(STUCK_WINDOW_S + 1.0)
    stuck_state = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0), objective_blip=blip
    )
    assert f.plan(stuck_state) is None

    # Something (brain, a human on the couch) gets him meaningfully closer:
    # the follower must notice and resume on its own.
    clock.tick(1.0)
    closer_state = make_state(
        mission_active=True, in_vehicle=True, pos=(250.0, 0.0, 0.0), objective_blip=blip
    )
    step = f.plan(closer_state)
    assert step is not None, "progress must un-stick the follower automatically"


def test_objective_moving_resets_the_stuck_clock() -> None:
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    state = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
    )
    f.plan(state)
    clock.tick(STUCK_WINDOW_S - 1.0)
    # The blip moves far enough to count as a new sub-objective just before
    # the old one would have timed out.
    moved_state = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 200.0, 0.0)},
    )
    step = f.plan(moved_state)
    assert step is not None
    clock.tick(STUCK_WINDOW_S - 1.0)
    still_state = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 200.0, 0.0)},
    )
    # Only ~2x window - 2 elapsed since first sight, but the clock reset when
    # the blip moved, so this must still be pursuing, not given up.
    assert f.plan(still_state) is not None


def test_reset_clears_all_tracking() -> None:
    f = MissionFollower()
    state = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0),
        objective_blip={"pos": (300.0, 0.0, 0.0)},
    )
    f.plan(state)
    f.bind_task("t-1")
    f.reset()
    assert f._target is None
    assert f._bound_task_id is None
    assert "not pursuing" in f.note()


def test_note_reports_backing_off_after_giving_up() -> None:
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    blip = {"pos": (300.0, 0.0, 0.0)}
    state = make_state(
        mission_active=True, in_vehicle=True, pos=(0.0, 0.0, 0.0), objective_blip=blip
    )
    f.plan(state)
    clock.tick(STUCK_WINDOW_S + 1.0)
    f.plan(state)
    assert "gave up" in f.note()


# --- wiring: main.py's single cutscene choke point ---------------------------


class _FakeBridge:
    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.radio: list[str] = []
        self.horn_calls: list[int] = []

    def post_task(self, task_type: str, params: dict[str, Any]) -> str:
        self.posted.append((task_type, dict(params)))
        return "t-fake-1"

    def set_radio(self, station: str) -> None:
        self.radio.append(station)

    def horn(self, ms: int) -> None:
        self.horn_calls.append(ms)


def _bare_harness(cutscene_active: bool, player_down: bool = False) -> Harness:
    """Just enough attributes for the REAL `_execute_action` to run."""
    h = Harness.__new__(Harness)
    h.bridge = _FakeBridge()
    h.primitives = None
    h._cutscene_active = cutscene_active
    h._switch_in_progress = False
    h._retry_in_flight = False
    h._player_down = player_down
    h._mission_active = False
    h._wanted_now = 0
    h._quiet_until = 0.0
    h._screen_blocked = False
    #: `_execute_action` also refuses a task type the stall detector has just
    #: measured pinning him in place; nothing has stalled in these tests.
    h.task_stall = TaskStallDetector()
    #: THE GATE. A §1 bridge task without the wheel's current token never
    #: reaches the bridge, so a harness that can post one has to have a wheel.
    h.wheel = MovementWheel()
    #: F6's stopwatch. `_execute_action` stops it on the one line that posts a
    #: movement task, so the real method cannot run without one.
    h.control_regained = ControlRegained()
    h.cleared_backoff = ClearedByGameBackoff()
    h.idle_breaker = IdleBreaker()
    return h


def _holding(h: Harness, owner: str = "brain") -> Any:
    """Open a movement tick and give `owner` the wheel, as production does.

    Every §1 bridge task in this harness carries a token and
    `_execute_action` refuses it without one — so a test that posts a task
    must hold the wheel exactly the way the real caller does, rather than
    reaching past the gate it is meant to be exercising.
    """
    h.wheel.begin_tick()
    token = h.wheel.acquire(owner, "test")
    assert token is not None
    return token


def test_execute_action_refuses_any_bridge_task_without_the_wheel() -> None:
    """CONTRACTS v1.13: `answer_call`/`reject_call` issue no ped task and move nobody, so
    they are exempt from movement arbitration by design — the gate is MOVEMENT_TASKS, not
    every bridge task.

    The gate itself. The operator's spec put this assertion in the bridge;
    it lives here instead because the harness is the bridge's only client and
    this method is the only funnel — same guarantee, no wire change."""
    h = _bare_harness(cutscene_active=False)
    h.wheel.begin_tick()
    for task_type in MOVEMENT_TASKS:
        assert Harness._execute_action(h, task_type, {}) is None
    assert h.bridge.posted == [], "no task may reach the game without the wheel"

    # A token that WAS valid and has since been preempted is no better than none.
    stale = _holding(h, "roam")
    h.wheel.acquire("threat", "being shot")
    assert Harness._execute_action(h, "walk_to", {}, stale) is None
    assert h.bridge.posted == []


def test_execute_action_suppresses_every_bridge_task_during_a_cutscene() -> None:
    h = _bare_harness(cutscene_active=True)
    token = _holding(h)
    for task_type in BRIDGE_TASKS:
        result = Harness._execute_action(h, task_type, {}, token)
        assert result is None, f"{task_type} must not be posted mid-cutscene"
    assert h.bridge.posted == [], "no bridge task may reach the bridge during a cutscene"


def test_execute_action_still_posts_bridge_tasks_outside_a_cutscene() -> None:
    h = _bare_harness(cutscene_active=False)
    result = Harness._execute_action(h, "drive_to", {"x": 1.0, "y": 2.0, "z": 3.0}, _holding(h))
    assert result == "t-fake-1"
    assert h.bridge.posted == [("drive_to", {"x": 1.0, "y": 2.0, "z": 3.0})]


def test_execute_action_still_allows_radio_and_wait_during_a_cutscene() -> None:
    """§1 tasks are the closed POST /task vocabulary; radio/horn/wait are
    separate bridge endpoints / harness primitives, not tasks — the cutscene
    gate is scoped to what CONTRACTS actually calls a task."""
    h = _bare_harness(cutscene_active=True)
    Harness._execute_action(h, "radio", {"station": "off"})
    assert h.bridge.radio == ["off"]
    result = Harness._execute_action(h, "wait", {"seconds": 5})
    assert result is None
    assert h._quiet_until > 0.0


def test_execute_action_ignores_unknown_bridge_errors_gracefully() -> None:
    """Not this brief's main subject, but a quick sanity check that the new
    cutscene branch sits ahead of, not instead of, the existing error handling."""

    class _AngryBridge(_FakeBridge):
        def post_task(self, task_type: str, params: dict[str, Any]) -> str:
            raise BridgeApiError(400, "invalid_params", "bad params")

    h = _bare_harness(cutscene_active=False)
    h.bridge = _AngryBridge()
    assert Harness._execute_action(h, "drive_to", {}, _holding(h)) is None


# --- wiring: `_drive_mission_objective` --------------------------------------


class _RecordingStub:
    """Enough of the harness for `_drive_mission_objective` alone."""

    def __init__(self, plan_result: dict[str, Any] | None) -> None:
        class _Follower:
            def __init__(self, result: dict[str, Any] | None) -> None:
                self.result = result
                self.bound: str | None = "unset"

            def plan(self, state: GameState) -> dict[str, Any] | None:
                return self.result

            def bind_task(self, task_id: str | None) -> None:
                self.bound = task_id

        self.mission_follower = _Follower(plan_result)
        self._quiet_until = 0.0
        self._threat_has_the_wheel = False
        #: Every movement post is ARBITRATED by the wheel (main.wheel): the
        #: follower's step carries the holder's token or it never reaches the
        #: game, and the log can answer "who was driving on that tick".
        self.wheel = MovementWheel()
        self.cleared_backoff = ClearedByGameBackoff()
        self.idle_breaker = IdleBreaker()
        self.wheel.begin_tick()
        self._mission_token = None
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def _execute_action(
        self, action_type: str, params: dict[str, Any], token: Any = None
    ) -> str | None:
        assert self.wheel.holds(token), f"{action_type} posted without the wheel"
        self.wheel.mark_posted(token, action_type)
        self.posted.append((action_type, dict(params)))
        return "t-mission-99"


def test_drive_mission_objective_posts_the_plan_and_binds_the_task_id() -> None:
    stub = _RecordingStub({"type": "drive_to", "params": {"x": 1.0, "y": 2.0, "z": 3.0}})
    state = make_state(mission_active=True, in_vehicle=True, objective_blip={"pos": (1.0, 2.0, 3.0)})
    Harness._drive_mission_objective(stub, state)
    assert stub.posted == [("drive_to", {"x": 1.0, "y": 2.0, "z": 3.0})]
    assert stub.mission_follower.bound == "t-mission-99"


def test_drive_mission_objective_does_nothing_when_the_follower_has_no_plan() -> None:
    stub = _RecordingStub(None)
    state = make_state(mission_active=False)
    Harness._drive_mission_objective(stub, state)
    assert stub.posted == []
    assert stub.mission_follower.bound == "unset"


def test_drive_mission_objective_honours_a_pending_quiet_period() -> None:
    """A brain-issued `wait` must not be immediately overridden by the reflex."""
    stub = _RecordingStub({"type": "drive_to", "params": {}})
    stub._quiet_until = float("inf")
    state = make_state(mission_active=True, in_vehicle=True, objective_blip={"pos": (1.0, 2.0, 3.0)})
    Harness._drive_mission_objective(stub, state)
    assert stub.posted == []


# --- wiring: `_reflex` must not let free-roam reflexes fight a mission -------


class _Recorder:
    """Absorbs bookkeeping calls `_reflex` makes; records nothing asserted on."""

    def __getattr__(self, _name: str):
        return lambda *a, **k: None


class _PrimitivesRecorder:
    """Stands in for SendInput (Windows-only) and records what was pressed."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.keys: list[str] = []

    def execute(self, action_type: str, params: dict[str, Any]) -> None:
        self.executed.append((action_type, dict(params)))

    def press_key(self, key: str) -> None:
        self.keys.append(key)


class _ReflexStub:
    """Just enough of the harness for `Harness._reflex` — same idiom as
    test_governor_l3.py's StubHarness: the methods under test are the real,
    unmodified `Harness` ones; only their collaborators are stand-ins."""

    def __init__(self, governor_level: int = 0) -> None:
        class _Governor:
            def __init__(self, level: int) -> None:
                self.level = level

        self.clock = FakeClock()
        self.governor = _Governor(governor_level)
        self.mood = MoodModel(rng=random.Random(1))
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.activity_runner = ActivityRunner(ActivityPicker(random.Random(1)), random.Random(1))
        self.missions = MissionTracker()
        self.mission_follower = MissionFollower()
        #: `_reflex` gates the free-roam reflexes on the day plan too, and
        #: `_handle_mission_events` feeds it the mission events it counts.
        #: On the stub's own fake clock, so a test can roll a roam block over.
        self.planner = DayPlanner(random.Random(1), self.clock)
        self.stuck = StuckDetector()
        self.task_stall = TaskStallDetector(clock=self.clock)
        self.stranded = StrandedEscalator()
        #: T9 (findings.md R1/R5), on the stub's fake clock so a test can
        #: advance time deliberately rather than race the wall clock.
        self.water = WaterEscalator(clock=self.clock)
        self.road_dodge = RoadDodge(clock=self.clock)
        self.jack_handoff = JackHandoffGate(clock=self.clock)
        self.threat_latch = ThreatLatch(clock=self.clock)
        self.damage = DamageTracker(clock=self.clock)
        #: The vehicle-entry machine and the movement arbiter, both on the
        #: stub's fake clock so a test can advance time deliberately rather
        #: than race the wall clock.
        self.vehicle = VehicleController(clock=self.clock)
        self.wheel = MovementWheel(clock=self.clock)
        #: F6's stopwatch, on the same fake clock: a test that lets three
        #: seconds pass after a control-regained edge is asserting production
        #: behaviour, not racing the wall clock.
        self.control_regained = ControlRegained(clock=self.clock)
        self.cleared_backoff = ClearedByGameBackoff()
        self.idle_breaker = IdleBreaker()
        #: Production wiring: what losing the wheel MEANS for each owner.
        self.wheel.on_preempt("roam", self._roam_preempted)
        self.wheel.on_preempt("mission", self._mission_preempted)
        self.wheel.on_preempt("day_plan", self._day_plan_preempted)
        self.wheel.on_preempt("governor", self._governor_preempted)
        self.wheel.on_preempt("exit_interior", self._interior_preempted)
        self._roam_token = None
        self._mission_token = None
        self._day_plan_token = None
        self._governor_token = None
        self._park_task_id = None
        self._park_deadline = 0.0
        #: `(tick, owner, action_type)` for every bridge task that reached the
        #: game. The acceptance check "no two movement tasks from different
        #: owners in the same tick" reads this.
        self.posted_owners: list[tuple[int, str, str]] = []
        #: Free roam's single owner. `_reflex` reads it for the "how long has he
        #: been standing still" measure the house-escape ladder gates on, and
        #: `_handle_mission_events` tells it when a job starts and ends.
        self.roam = RoamEngine(random.Random(1), clock=self.clock)
        self.house_escape = HouseEscape(clock=self.clock)
        #: CONTRACTS v1.12's ground-truth escape. Real, on the stub's clock, so
        #: the acceptance tests for "he is indoors" drive the production ladder.
        self.interior_escape = InteriorEscape(clock=self.clock)
        self._interior_token = None

        class _Breaks:
            on_break = False

        #: `_vehicle_hold` asks whether he is on a humanizer break.
        self.breaks = _Breaks()
        self._threat_has_the_wheel = False
        self._screen_blocked = False
        self.death_recovery = DeathArrestRecovery()
        self.current_goal = "placeholder goal"
        self._quiet_until = 0.0
        #: Real SendInput is Windows-only, so the reflex ladder's primitive half
        #: (`reverse_out`) is recorded here instead. Without this the whole
        #: stuck ladder was invisible to these tests.
        self.primitives = _PrimitivesRecorder()
        self.bridge = None
        # Real LifetimeTotals in a throwaway dir: the counters ARE the thing
        # under test, so they are not stood in for.
        self.totals = throwaway_totals()
        self.counters = self.totals.counters()
        self.clips = None  # no OBS here, so events go down the batched path
        self._mission_started_iso = None
        self._mission_tokens = 0
        self.current_mission = None
        self.learned_scripts = throwaway_learned_scripts()
        self.writer = _Recorder()
        self.bus = _Recorder()
        self.memory = _Recorder()
        self.commentary = _Recorder()
        self.activities = _Recorder()

    #: The real movement arbitration. These ARE the behaviour under test — a
    #: re-description of the ladder in the stub would drift from production the
    #: first time the ladder changed.
    _game_owns_controls = Harness._game_owns_controls
    _game_control_reason = Harness._game_control_reason
    _reflex_act = Harness._reflex_act
    _break_the_idle = Harness._break_the_idle
    _roam_preempted = Harness._roam_preempted
    _mission_preempted = Harness._mission_preempted
    _day_plan_preempted = Harness._day_plan_preempted
    _governor_preempted = Harness._governor_preempted
    #: The v1.12 escape path, verbatim from production — these tests are the
    #: reachability proof for it, so nothing about it may be re-described here.
    _interior_preempted = Harness._interior_preempted
    _release_interior_wheel = Harness._release_interior_wheel
    _run_interior_escape = Harness._run_interior_escape

    def _execute_action(
        self, action_type: str, params: dict[str, Any], token: Any = None
    ) -> str | None:
        # Mirrors production's routing: the real `_execute_action` is the single
        # choke point for BOTH bridge tasks and SendInput primitives, and the
        # reflex ladder now sends `reverse_out` through it rather than straight
        # at SendInput. Collapsing the two here would file a keypress as a
        # posted task and hide exactly the arbitration this is testing.
        if action_type not in BRIDGE_TASKS:
            self.primitives.execute(action_type, dict(params))
            return None
        # The production gate, reproduced exactly: a bridge task without the
        # wheel's current token never reaches the game. Recording the owner is
        # what lets a test assert "no two movement tasks from different owners
        # in the same tick" structurally rather than by eyeballing a log.
        assert self.wheel.holds(token), (
            f"{action_type} posted without the movement wheel "
            f"(token={token}, held_by={self.wheel.owner})"
        )
        self.wheel.mark_posted(token, action_type)
        # Production stops F6's stopwatch on the line that posts, and so does
        # this: a stub that skipped it would report every edge unanswered.
        self.control_regained.moved(action_type)
        self.posted.append((action_type, dict(params)))
        self.posted_owners.append((self.wheel.tick, token.owner, action_type))
        return "t-reflex-1"

    def _vehicle_hold(self, state: GameState) -> str | None:
        # The real one: the list of reasons sitting still is CORRECT is the
        # behaviour under test, not scaffolding.
        return Harness._vehicle_hold(self, state)

    def _say(self, text: str, mood: str | None = None) -> None:
        pass

    def _end_activity_if_running(self, outcome: str, *, by: str | None = None) -> None:
        # The real one: releasing free roam's wheel token when its goal ends is
        # half of the arbitration, and a stub that skipped it would leave a hold
        # nothing gives back.
        Harness._end_activity_if_running(self, outcome, by=by)

    def _capture_screenshot(self, hint: str):
        return None, None

    def _capture_clip_async(
        self, event_type: str, caption: str, event_id: int | None = None
    ) -> None:
        pass

    def _record_big_event(self, type_, payload, screenshot_url=None):
        # The real one, so the death/busted write path stays under test.
        return Harness._record_big_event(self, type_, payload, screenshot_url)

    def _write_mission_row(self, event_type, payload) -> None:
        # The real one too: a mission that ends has to reach the missions table.
        Harness._write_mission_row(self, event_type, payload)


    def _handle_mission_events(self, state, delta) -> None:
        # Borrow the REAL wiring (extracted from _reflex) so these tests keep exercising
        # the production mission-event path, including the v1.3 mission_start screenshot.
        Harness._handle_mission_events(self, state, delta)

def _reflex(stub: _ReflexStub, state: GameState) -> None:
    Harness._reflex(stub, state, Delta(wanted_from=0, wanted_to=0))


def test_stranded_escalator_does_not_widen_search_during_a_cutscene() -> None:
    """The exact story from the brief: on foot, no task, standing inside a
    mission cutscene — the free-roam stranded reflex must stay off the wheel."""
    stub = _ReflexStub(governor_level=0)
    state = make_state(
        mission_active=True, cutscene_active=True, in_vehicle=False, task_status="idle"
    )
    _reflex(stub, state)
    assert stub.posted == [], f"stranded escalator must not act mid-cutscene, posted {stub.posted}"


def test_stranded_escalator_does_not_act_during_the_objective_phase_either() -> None:
    """Mission intent outranks free-roam intent for the whole mission, not
    only its cutscenes — `_drive_mission_objective` owns getting him a car."""
    stub = _ReflexStub(governor_level=0)
    state = make_state(
        mission_active=True, cutscene_active=False, in_vehicle=False, task_status="idle"
    )
    _reflex(stub, state)
    assert stub.posted == []


def test_stranded_escalator_still_works_outside_any_mission() -> None:
    """Regression guard the other way: the mission gate must not silence the
    reflex when there is no mission to protect."""
    stub = _ReflexStub(governor_level=0)
    state = make_state(mission_active=False, in_vehicle=False, task_status="idle")
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["enter_nearest_vehicle"]


def test_l2_wander_reflex_does_not_override_mission_navigation() -> None:
    stub = _ReflexStub(governor_level=2)
    state = make_state(
        mission_active=True, cutscene_active=False, in_vehicle=True, task_status="idle"
    )
    _reflex(stub, state)
    assert stub.posted == [], f"L2 wander must yield to the mission, posted {stub.posted}"


def test_l2_wander_reflex_still_works_outside_a_mission() -> None:
    stub = _ReflexStub(governor_level=2)
    state = make_state(mission_active=False, in_vehicle=True, task_status="idle")
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["wander_drive"]


def _send_the_day_plan_to_a_job(stub: _ReflexStub) -> None:
    """Roll the stub's roam block over onto a marker, so the planner is in a
    mission block and owns the wheel."""
    stub.clock.tick(ROAM_BLOCK_S[1] + 1.0)
    stub.planner.update(
        make_state(in_vehicle=True, starts=[((300.0, 0.0, 0.0), "unknown")]), "bored", False
    )
    assert stub.planner.in_mission_block, "the fixture must actually be on a job"


def test_stranded_escalator_does_not_fight_the_day_plans_mission_block() -> None:
    """No mission is ACTIVE yet — he is walking to the marker that starts one.
    The free-roam stranded reflex would post its own `enter_nearest_vehicle`
    straight over the planner's navigation, every tick."""
    stub = _ReflexStub(governor_level=0)
    _send_the_day_plan_to_a_job(stub)
    _reflex(stub, make_state(mission_active=False, in_vehicle=False, task_status="idle"))
    assert stub.posted == [], f"stranded escalator must yield to the day plan, posted {stub.posted}"


def test_l2_wander_reflex_does_not_fight_the_day_plans_mission_block() -> None:
    stub = _ReflexStub(governor_level=2)
    _send_the_day_plan_to_a_job(stub)
    _reflex(stub, make_state(mission_active=False, in_vehicle=True, task_status="idle"))
    assert stub.posted == [], f"L2 wander must yield to the day plan, posted {stub.posted}"


# --- MissionTracker: mission_fail / retry-attempt bookkeeping ----------------
#
# The observed regression this section closes: a real /state showed
# `player.health == 0`, `player.dead == true`, and `mission.active` flipping
# true -> false in the same snapshot — a mission failure — and nothing
# detected it, recorded it, or retried it.


def _tick(
    tracker: MissionTracker,
    *,
    mission_active: bool,
    dead: bool = False,
    arrested: bool = False,
    cutscene_active: bool = False,
) -> list:
    """One MissionTracker.feed() call from plain booleans; Delta is derived
    the same way perception.Perceptor would (mission flag edge)."""
    was_active = tracker.in_mission
    delta = Delta(
        mission_started=mission_active and not was_active,
        mission_ended=was_active and not mission_active,
    )
    return tracker.feed(mission_active, cutscene_active, False, delta, dead, arrested)


def test_mission_start_emits_unknown_name_at_attempt_one() -> None:
    tracker = MissionTracker()
    events = _tick(tracker, mission_active=True)
    assert [(e.type, e.payload) for e in events] == [("mission_start", {"name": "unknown"})]
    assert tracker.attempt == 1


def test_mission_ending_alive_is_not_a_failure() -> None:
    """A mission ending while the agent is neither dead nor arrested is not a
    signal this package can honestly call a pass, a skip, or a fail — it
    stays a silent (logged) transition, exactly as before this change."""
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    events = _tick(tracker, mission_active=False, dead=False, arrested=False)
    assert events == []


def test_mission_ending_while_dead_emits_mission_fail() -> None:
    """The exact regression: mission.active flips false while player.dead is
    true — that is a failure, and now it is reported as one."""
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    events = _tick(tracker, mission_active=False, dead=True)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "mission_fail"
    assert ev.payload == {"name": "unknown", "reason_text": "died", "attempt": 1}


def test_mission_ending_while_arrested_emits_mission_fail_with_that_reason() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    events = _tick(tracker, mission_active=False, arrested=True)
    assert events[0].payload["reason_text"] == "arrested"


def test_a_retry_after_a_failure_increments_the_attempt_counter() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False, dead=True)  # fail, attempt 1
    events = _tick(tracker, mission_active=True)  # the game's own retry
    assert tracker.attempt == 2
    assert events[0].payload == {"name": "unknown"}  # mission_start carries no attempt


def test_a_clean_ending_resets_the_attempt_counter_for_the_next_mission() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False, dead=True)  # fail, attempt 1
    _tick(tracker, mission_active=True)  # retry -> attempt 2
    _tick(tracker, mission_active=False, dead=False)  # this one ends clean
    events = _tick(tracker, mission_active=True)  # a genuinely new mission
    assert tracker.attempt == 1
    assert events[0].payload == {"name": "unknown"}


def test_brain_note_mentions_the_attempt_only_once_it_is_a_retry() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    assert "attempt" not in tracker.brain_note()
    _tick(tracker, mission_active=False, dead=True)
    _tick(tracker, mission_active=True)
    assert "attempt 2" in tracker.brain_note()


# --- MissionTracker: resolve_outcome — the screen-read pass/fail (THE BUG) ---
#
# The reported live bug, root-caused: a mission that fails without killing or
# arresting anyone ("MISSION FAILED / Franklin lost Lamar" on a follow mission
# whose target drove away) fell through `feed()`'s dead/arrested check and
# emitted NOTHING. `pending_outcome_read` is `feed()`'s honest "I don't know
# yet" signal for exactly that ending; `resolve_outcome` is what a screen read
# (brain/vision.py's `read_mission_outcome`) turns it into.


def test_ambiguous_ending_arms_a_pending_outcome_read_and_still_emits_nothing_itself() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    events = _tick(tracker, mission_active=False, dead=False, arrested=False)
    assert events == []  # feed() itself never guesses; unchanged from before this feature
    assert tracker.pending_outcome_read is True


def test_resolve_outcome_is_a_no_op_when_nothing_is_pending() -> None:
    """Dead/arrested endings resolve inside `feed()` itself and never arm a
    pending read; a stray `resolve_outcome` call must not invent an event."""
    tracker = MissionTracker()
    assert tracker.resolve_outcome("passed", None) is None
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False, dead=True)  # the fast path, not this one
    assert tracker.resolve_outcome("passed", None) is None


def test_resolve_outcome_failed_emits_mission_fail_with_the_screens_own_reason() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False)  # alive: the ambiguous ending
    ev = tracker.resolve_outcome("failed", "Franklin lost Lamar")
    assert ev is not None
    assert ev.type == "mission_fail"
    assert ev.payload == {"name": "unknown", "reason_text": "Franklin lost Lamar", "attempt": 1}
    assert tracker.pending_outcome_read is False, "consumed, not left armed for next tick"


def test_resolve_outcome_failed_with_no_reason_line_does_not_fabricate_one() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False)
    ev = tracker.resolve_outcome("failed", None)
    assert ev.payload["reason_text"] == "no reason shown on screen"


def test_resolve_outcome_passed_emits_mission_end_with_outcome_passed() -> None:
    clock = FakeClock()
    tracker = MissionTracker(clock=clock)
    _tick(tracker, mission_active=True)
    clock.tick(42.0)
    _tick(tracker, mission_active=False)
    ev = tracker.resolve_outcome("passed", None)
    assert ev is not None
    assert ev.type == "mission_end"
    assert ev.payload == {
        "name": "unknown",
        "outcome": "passed",
        "duration_s": 42.0,
        "deaths": 0,
        "attempts": 1,
    }


def test_resolve_outcome_unknown_emits_nothing_and_consumes_the_pending_flag() -> None:
    """The exact honesty rule the brief calls out: an unread screen is not a
    pass, and reporting one anyway would put a false number on the public
    site's `missions_passed` counter."""
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False)
    assert tracker.resolve_outcome("unknown", None) is None
    assert tracker.pending_outcome_read is False


def test_a_screen_detected_failure_still_counts_as_a_retry_attempt() -> None:
    """The brief's own note: `_last_ended_in_failure` drives the `attempt`
    counter, so a screen-detected failure must set it too or retries stop
    being counted."""
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False)  # alive; ambiguous
    tracker.resolve_outcome("failed", "Franklin lost Lamar")
    events = _tick(tracker, mission_active=True)  # the game's own retry
    assert tracker.attempt == 2
    assert events[0].payload == {"name": "unknown"}  # mission_start carries no attempt


def test_a_screen_detected_pass_resets_the_attempt_counter_for_the_next_job() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False)
    tracker.resolve_outcome("passed", None)
    events = _tick(tracker, mission_active=True)  # a genuinely new mission
    assert tracker.attempt == 1
    assert events[0].payload == {"name": "unknown"}


def test_deaths_accumulate_across_failed_attempts_and_land_on_the_eventual_pass() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False, dead=True)  # attempt 1: a real death
    _tick(tracker, mission_active=True)  # attempt 2
    _tick(tracker, mission_active=False)  # ends alive this time
    ev = tracker.resolve_outcome("passed", None)
    assert ev.payload == {
        "name": "unknown",
        "outcome": "passed",
        "duration_s": ev.payload["duration_s"],
        "deaths": 1,
        "attempts": 2,
    }


def test_an_arrest_does_not_count_as_a_death() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False, arrested=True)  # attempt 1: cuffed, not killed
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False)
    ev = tracker.resolve_outcome("passed", None)
    assert ev.payload["deaths"] == 0


def test_brain_note_carries_the_screens_own_reason_after_a_screen_detected_failure() -> None:
    """The other half of the reported bug: "keeps talking even after mission
    has failed" — the brain needs to be told, in the dynamic context, that
    the job it might still be narrating is actually over."""
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False)
    tracker.resolve_outcome("failed", "Franklin lost Lamar")
    note = tracker.brain_note()
    assert "Franklin lost Lamar" in note
    assert "none active" in note


def test_brain_note_reason_is_cleared_once_a_new_mission_starts() -> None:
    tracker = MissionTracker()
    _tick(tracker, mission_active=True)
    _tick(tracker, mission_active=False)
    tracker.resolve_outcome("failed", "Franklin lost Lamar")
    _tick(tracker, mission_active=True)
    assert "Franklin lost Lamar" not in tracker.brain_note()


# --- wiring: main.py reads the mission-end screen (THE BUG, end to end) ------
#
# `_handle_mission_events` must capture the mission-end screenshot, run the
# vision read, and turn the result into the right event — reusing the exact
# `MissionTracker` state machine above, not a synthetic stand-in, so this is
# the real regression path: a follow-mission failure with nobody dead or
# arrested must now actually reach `record_event("mission_fail", ...)`.


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        self.published.append((kind, dict(payload)))


def _mission_end_harness(outcome: MissionOutcome) -> Harness:
    h = Harness.__new__(Harness)
    h.missions = MissionTracker()
    h.planner = DayPlanner(random.Random(1))
    # The real roam engine: `_handle_mission_events` is where its "the story has
    # to move" clock is reset, so stubbing it would stop testing the thing that
    # makes a mission actually interrupt free roam.
    h.roam = RoamEngine(random.Random(1))
    h.writer = _FakeWriter()
    h.bus = _FakeBus()
    h._pending_screenshot_trigger = None
    h._pending_big_event = None
    h.shots: list[str] = []
    h._capture_screenshot = lambda hint: (h.shots.append(hint) or (b"jpeg", f"https://x/{hint}.jpg"))
    h.current_mission = {"name": "placeholder, must be cleared on a real end"}
    h._read_mission_title = lambda jpeg: None
    h._read_mission_outcome = lambda jpeg: outcome
    h.totals = throwaway_totals()
    h.counters = h.totals.counters()
    h._mission_started_iso = None
    h._mission_tokens = 0
    h.learned_scripts = throwaway_learned_scripts()
    return h


def _run_mission_to_ambiguous_end(h: Harness) -> None:
    """Drive the REAL `MissionTracker` through mission_start -> mission_ended
    (alive) via the actual `Harness._handle_mission_events` wiring — the same
    two calls `_reflex` makes across two ticks on the real /state transition."""
    started = make_state(mission_active=True)
    Harness._handle_mission_events(
        h, started, Delta(wanted_from=0, wanted_to=0, mission_started=True)
    )
    h.shots.clear()
    ended = make_state(mission_active=False, dead=False, arrested=False)
    Harness._handle_mission_events(
        h, ended, Delta(wanted_from=0, wanted_to=0, mission_ended=True)
    )


def test_a_screen_detected_failure_emits_mission_fail_with_the_real_reason() -> None:
    h = _mission_end_harness(MissionOutcome("failed", "Franklin lost Lamar"))
    _run_mission_to_ambiguous_end(h)
    assert h.shots == ["mission_end"], "reuses the one screenshot, no second grab"
    assert (
        "mission_fail",
        {"name": "unknown", "reason_text": "Franklin lost Lamar", "attempt": 1},
        "https://x/mission_end.jpg",
    ) in h.writer.events
    assert h._pending_screenshot_trigger == "mission_fail"
    assert h._pending_big_event == "mission_fail"
    assert h.current_mission is None
    assert h.counters["missions_passed"] == 0
    assert h.bus.published == [], "no counters publish on a failure"


def test_a_screen_detected_pass_increments_missions_passed_exactly_once() -> None:
    h = _mission_end_harness(MissionOutcome("passed", None))
    _run_mission_to_ambiguous_end(h)
    assert h.counters["missions_passed"] == 1
    assert h.bus.published == [("counters", {"deaths": 0, "busted": 0, "missions_passed": 1})]
    assert h._pending_screenshot_trigger == "mission_end"
    assert h.current_mission is None
    kinds = [e[0] for e in h.writer.events]
    assert kinds == ["mission_start", "mission_end"]
    passed = next(e for e in h.writer.events if e[0] == "mission_end")
    assert passed[1]["outcome"] == "passed"
    assert passed[2] == "https://x/mission_end.jpg"


def test_an_unknown_read_emits_nothing_and_never_touches_the_counter() -> None:
    h = _mission_end_harness(MissionOutcome("unknown", None))
    _run_mission_to_ambiguous_end(h)
    assert h.counters["missions_passed"] == 0
    kinds = [e[0] for e in h.writer.events]
    assert kinds == ["mission_start"], "nothing recorded for the ended-alive tick itself"
    assert h._pending_big_event == "mission_start", "unchanged by an unread ending"
    assert h.bus.published == []


def test_the_dead_path_still_short_circuits_with_no_vision_call() -> None:
    """The fast dead/arrested path is free and certain and must stay that
    way — it must never turn into a vision call."""
    calls: list[bytes] = []
    h = _mission_end_harness(MissionOutcome("passed", None))
    h._read_mission_outcome = lambda jpeg: (calls.append(jpeg), MissionOutcome("passed", None))[1]
    started = make_state(mission_active=True)
    Harness._handle_mission_events(
        h, started, Delta(wanted_from=0, wanted_to=0, mission_started=True)
    )
    h.shots.clear()
    died = make_state(mission_active=False, dead=True)
    Harness._handle_mission_events(
        h, died, Delta(wanted_from=0, wanted_to=0, mission_ended=True, died=True)
    )
    assert calls == [], "dead/arrested is decided from flags alone — no vision call"
    assert h.shots == ["mission_fail"], "the existing dead-path screenshot only"
    assert h.counters["missions_passed"] == 0
    assert [e[0] for e in h.writer.events] == ["mission_start", "mission_fail"]


# --- `_execute_action`: the choke point also refuses tasks while player_down -


def test_execute_action_suppresses_every_bridge_task_while_player_is_down() -> None:
    h = _bare_harness(cutscene_active=False, player_down=True)
    token = _holding(h)
    for task_type in BRIDGE_TASKS:
        result = Harness._execute_action(h, task_type, {}, token)
        assert result is None, f"{task_type} must not be posted while dead/arrested"
    assert h.bridge.posted == []


def test_execute_action_still_allows_press_prompt_key_while_player_is_down() -> None:
    """The one non-bridge-task action this state deliberately still allows:
    the mission-retry keypress fires only after `_player_down` has already
    cleared for the tick (see `main._reflex`), but this checks the choke
    point itself does not block a non-bridge-task primitive too broadly."""
    h = _bare_harness(cutscene_active=False, player_down=True)
    h.primitives = None  # off-platform: degrades to a log line, not a crash
    result = Harness._execute_action(h, "press_prompt_key", {})
    assert result is None  # no crash, no bridge call attempted


# --- `_reflex`: dead/arrested suppresses the physical-recovery reflexes -----


def test_reflex_suppresses_stuck_flip_stranded_wander_while_dead() -> None:
    """A corpse cannot be un-stuck, righted, or sent looking for a car."""
    stub = _ReflexStub(governor_level=2)  # L2 would otherwise wander_drive
    state = make_state(
        dead=True,
        in_vehicle=True,
        task_status="running",
        task_type="drive_to",
        vehicle={
            "handle": 1,
            "model": "adder",
            "display_name": "Adder",
            "class": "Super",
            "speed": 0.0,
            "health": 500.0,
            "upside_down": True,  # would normally fire flipped_action
            "in_water": False,
            "stopped_for_s": 999.0,  # would normally fire StuckDetector
        },
    )
    _reflex(stub, state)
    assert stub.posted == [], f"nothing physical may act on a corpse, posted {stub.posted}"


def test_reflex_suppresses_the_same_reflexes_while_arrested() -> None:
    stub = _ReflexStub()
    state = make_state(arrested=True, in_vehicle=False, task_status="idle")
    _reflex(stub, state)
    assert stub.posted == []


# --- `_reflex`: the threat/combat reflex (survival ladder, no model call) ---


def test_threat_reflex_hurt_overrides_wanted_and_a_close_hostile() -> None:
    """Survival always wins the ladder: hurt beats both a close hostile
    (would otherwise be fought) and wanted stars (would otherwise flee)."""
    stub = _ReflexStub()
    state = make_state(
        wanted=2,
        health=10,
        in_vehicle=False,
        nearby_peds=[{"handle": 9, "model": "s_m_y", "distance": 3.0, "relationship": "hostile"}],
    )
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["seek_cover"]


def test_threat_reflex_fights_a_hostile_even_while_wanted() -> None:
    """Revised per live feedback: healthy + a hostile present now fights
    regardless of `wanted` — "he just sits in car and dies" was the exact
    bug a `wanted`-always-flees rule caused."""
    stub = _ReflexStub()
    state = make_state(
        wanted=3,
        in_vehicle=False,
        nearby_peds=[{"handle": 9, "model": "s_m_y", "distance": 3.0, "relationship": "hostile"}],
    )
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["combat_hated_targets_around"]


def test_threat_reflex_breaks_contact_on_foot_when_hurt() -> None:
    stub = _ReflexStub()
    state = make_state(health=40, in_vehicle=False)  # 20% of 200 max
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["seek_cover"]


def test_threat_reflex_drives_away_when_hurt_in_a_vehicle() -> None:
    stub = _ReflexStub()
    state = make_state(health=40, in_vehicle=True, task_status="idle")
    _reflex(stub, state)
    assert stub.posted[0] == ("wander_drive", {"style": "avoid_traffic"})


def test_threat_reflex_fights_a_close_hostile_on_foot_when_healthy() -> None:
    stub = _ReflexStub()
    state = make_state(
        in_vehicle=False,
        nearby_peds=[{"handle": 9, "model": "s_m_y", "distance": 5.0, "relationship": "hostile"}],
    )
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["combat_hated_targets_around"]


def test_threat_reflex_leaves_rather_than_fights_from_a_working_car() -> None:
    """The operator's reversal, through the real `_reflex` wiring: "he can see
    enemies on the map, he just sits in car and dies" is still the bug, and
    "stop sitting" is still the fix — but from a working car the fix is to
    drive off, not to get out and trade shots."""
    stub = _ReflexStub()
    vehicle = {
        "handle": 1, "model": "adder", "display_name": "Adder", "class": "Super",
        "speed": 0.0, "health": 900.0, "upside_down": False, "in_water": False,
        "stopped_for_s": 3.0,
    }
    state = make_state(
        in_vehicle=True,
        vehicle=vehicle,
        task_status="idle",
        nearby_peds=[{"handle": 9, "model": "s_m_y", "distance": 20.0, "relationship": "hostile"}],
    )
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["wander_drive"]


def test_threat_reflex_ignores_a_distant_hostile() -> None:
    # in_vehicle=True so the (unrelated) stranded-on-foot reflex cannot also
    # fire and make this assertion about something else entirely.
    stub = _ReflexStub()
    state = make_state(
        in_vehicle=True,
        task_status="idle",
        nearby_peds=[{"handle": 9, "model": "s_m_y", "distance": 80.0, "relationship": "hostile"}],
    )
    _reflex(stub, state)
    assert stub.posted == []


def test_a_threat_does_not_starve_the_stuck_ladder() -> None:
    """The reflex-ladder starvation bug: `threat_action` used to short-circuit
    the whole `else:` chain, so while ANY hostile stood within 40 m
    `self.stuck.check()` was never called and the unstick ladder could not fire
    at all — during a firefight, which is exactly when a car ends up wedged on
    a kerb with the engine's combat task unable to do anything about it.

    Both must happen in the same tick. They do not conflict: the threat half
    posts a bridge task, the stuck half sends a `reverse_out` keypress.

    Driven off the hurt rung (health at/under 30% of max) rather than off a
    bare hostile sighting, because a hostile seen from a car already under a
    running drive order no longer preempts that order — see
    `recovery.threat_action` rung 4."""
    stub = _ReflexStub()
    state = make_state(
        in_vehicle=True,
        health=50,  # <= LOW_HEALTH_FRACTION of 200: break contact
        task_status="running",
        task_type="drive_to",
        nearby_peds=[{"handle": 9, "model": "s_m_y", "distance": 10.0, "relationship": "hostile"}],
        vehicle={
            "handle": 1,
            "model": "adder",
            "display_name": "Adder",
            "class": "Super",
            "speed": 0.0,
            "health": 900.0,
            "upside_down": False,
            "in_water": False,
            "stopped_for_s": 999.0,  # > StuckDetector.threshold_s
        },
    )
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["wander_drive"]
    assert [t for t, _ in stub.primitives.executed] == ["reverse_out"]


def test_a_threat_still_keeps_the_free_roam_reflexes_off_the_wheel() -> None:
    """The other half of the same restructure: the reflexes that POST TASKS
    (stranded search, L2 wander) must still yield under fire, or they would
    preempt the survival action issued microseconds earlier.

    The survival action here is `flee_police`: he is already MOVING (the
    default vehicle does 20 m/s), so the hostile rung falls through rather
    than stopping a working escape, and the stars rung answers instead — which
    is a better answer than aimless wandering, and the point of falling
    through rather than returning early."""
    stub = _ReflexStub(governor_level=2)
    state = make_state(
        wanted=1,
        in_vehicle=True,
        task_status="idle",
        nearby_peds=[{"handle": 9, "model": "s_m_y", "distance": 10.0, "relationship": "hostile"}],
    )
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["flee_police"], (
        "the L2 wander reflex must not preempt a survival order"
    )


def test_being_upside_down_wins_the_tick_over_a_threat() -> None:
    """`flipped_action` posts a real task (`exit_vehicle`), so it is the one
    physical reflex that suppresses the threat post rather than running beside
    it: posting combat and then preempting it two lines later would waste the
    post and start the latch hold-down for nothing."""
    stub = _ReflexStub()
    state = make_state(
        in_vehicle=True,
        task_status="idle",
        nearby_peds=[{"handle": 9, "model": "s_m_y", "distance": 10.0, "relationship": "hostile"}],
        vehicle={
            "handle": 1,
            "model": "adder",
            "display_name": "Adder",
            "class": "Super",
            "speed": 0.0,
            "health": 900.0,
            "upside_down": True,
            "in_water": False,
            "stopped_for_s": 0.0,
        },
    )
    _reflex(stub, state)
    assert [t for t, _ in stub.posted] == ["exit_vehicle"]


# --- `_reflex`: the threat latch (one post per threat, not one per tick) -----
#
# Observed on stream with the reflex live: "walks like someone is pressing W
# constantly, stuttering" and "fires but not at the cops". CONTRACTS §1 - every
# POST /task preempts the running one - plus a 3-4 Hz reflex re-posting the same
# combat order = an engine combat task torn down and restarted several times a
# second, so the aim/approach cycle never completes.


def _hostile_state(**over: Any) -> GameState:
    over.setdefault(
        "nearby_peds",
        [{"handle": 9, "model": "s_m_y", "distance": 8.0, "relationship": "hostile"}],
    )
    over.setdefault("in_vehicle", False)
    return make_state(**over)


def test_a_running_combat_task_is_not_re_posted_every_tick() -> None:
    stub = _ReflexStub()
    # Tick 1: nothing running yet.
    _reflex(stub, _hostile_state(task_status="idle"))
    # Ticks 2-10: the engine reports the combat task running, as it would.
    for _ in range(9):
        stub.clock.tick(1.0)  # well past THREAT_HOLD_S by the end
        _reflex(
            stub,
            _hostile_state(
                task_status="running",
                task_type="combat_hated_targets_around",
                task_id="t-1",
            ),
        )
    assert [t for t, _ in stub.posted] == ["combat_hated_targets_around"], (
        f"one threat, one post — got {stub.posted}"
    )


def test_the_hold_down_covers_a_task_that_briefly_reports_finished() -> None:
    """Belt and braces: the engine's combat task ends the moment no hated
    target is in radius, so `last_task` can read `done` for a tick or two
    mid-fight. Re-posting through that window is the same thrash by another
    route."""
    stub = _ReflexStub()
    _reflex(stub, _hostile_state(task_status="idle"))
    stub.clock.tick(THREAT_HOLD_S / 2)
    _reflex(stub, _hostile_state(task_status="done", task_type="combat_hated_targets_around"))
    assert len(stub.posted) == 1
    # Past the hold with the task still not running: he re-engages.
    stub.clock.tick(THREAT_HOLD_S)
    _reflex(stub, _hostile_state(task_status="done", task_type="combat_hated_targets_around"))
    assert [t for t, _ in stub.posted] == [
        "combat_hated_targets_around",
        "combat_hated_targets_around",
    ]


def test_a_change_of_situation_class_is_issued_immediately() -> None:
    """The latch must never hold him in a fight he is losing: when health drops
    the action changes class (fight -> break contact) and goes out on the next
    tick, hold-down or not."""
    stub = _ReflexStub()
    _reflex(stub, _hostile_state(task_status="idle"))
    assert [t for t, _ in stub.posted] == ["combat_hated_targets_around"]
    stub.clock.tick(0.3)  # deep inside the hold-down
    _reflex(
        stub,
        _hostile_state(
            health=20,  # 10% of 200
            task_status="running",
            task_type="combat_hated_targets_around",
            task_id="t-1",
        ),
    )
    assert [t for t, _ in stub.posted] == ["combat_hated_targets_around", "seek_cover"]


def test_someone_else_taking_the_wheel_re_arms_the_threat_reflex() -> None:
    """The latch keys on the running task's TYPE, not on "a task is running":
    if the brain or the mission follower posts a drive, the threat reflex must
    take it back."""
    stub = _ReflexStub()
    _reflex(stub, _hostile_state(task_status="idle"))
    stub.clock.tick(THREAT_HOLD_S + 1.0)
    _reflex(stub, _hostile_state(task_status="running", task_type="drive_to", task_id="t-9"))
    assert [t for t, _ in stub.posted] == [
        "combat_hated_targets_around",
        "combat_hated_targets_around",
    ]


# --- `_reflex`: respawn clears stale mission/goal state ----------------------


def test_respawn_clears_the_mission_follower_and_resets_the_goal() -> None:
    stub = _ReflexStub()
    stub.current_goal = "chase the stale pre-death plan"
    # Give the follower something to forget.
    stub.mission_follower.plan(
        make_state(mission_active=True, in_vehicle=True, objective_blip={"pos": (300.0, 0.0, 0.0)})
    )
    assert stub.mission_follower._target is not None

    _reflex(stub, make_state(dead=True))  # he dies
    _reflex(stub, make_state(dead=False))  # the game's own respawn

    assert stub.mission_follower._target is None
    assert stub.current_goal == INITIAL_GOAL


def test_mission_fail_while_down_captures_a_screenshot_but_presses_nothing() -> None:
    """`_reflex` itself never presses a retry key any more: confirmed live
    that a mission failure freezes the SHVDN script thread on the "MISSION
    FAILED"/retry screen entirely (`/state` stops advancing at all), so
    `player.dead` never clears on its own for this class to wait for. That
    keypress is `BlockingScreenWatchdog`'s job (wired in `main.run()`,
    unit-tested in test_recovery.py), driven off `state.tick` staleness, not
    off this mission_fail event. This only checks `_reflex`'s own half: the
    event is recorded, with a screenshot, and nothing here tries to press
    anything on its own."""
    stub = _ReflexStub()
    Harness._reflex(
        stub,
        make_state(mission_active=True),
        Delta(wanted_from=0, wanted_to=0, mission_started=True),
    )
    Harness._reflex(
        stub,
        make_state(mission_active=False, dead=True),
        Delta(wanted_from=0, wanted_to=0, mission_ended=True, died=True),
    )
    assert not any(t == "press_prompt_key" for t, _ in stub.posted)

    stub.posted.clear()
    Harness._reflex(
        stub, make_state(dead=False), Delta(wanted_from=0, wanted_to=0, respawned=True)
    )  # respawn completes
    assert not any(t == "press_prompt_key" for t, _ in stub.posted)


# --- CONTRACTS v1.3: mission_start carries a screenshot and arms the director ---
#
# Found by the dossier.txt dossier (13.8): mission_start was in VISION_TRIGGERS but
# nothing ever set _pending_screenshot_trigger to it, so the director could never
# see a mission's opening screen - the only place the objective is ever shown.


class _FakeMissions:
    def __init__(self, events):
        self._events = events

    def feed(self, *_a, **_k):
        return list(self._events)


class _FakeWriter:
    def __init__(self):
        self.events = []
        self.missions = []

    def record_event(self, type_, payload, screenshot_url=None, **_k):
        self.events.append((type_, payload, screenshot_url))

    def insert_mission(self, row):
        self.missions.append(dict(row))


def _event_harness(events):
    h = Harness.__new__(Harness)
    h.missions = _FakeMissions(events)
    # The real planner: `_handle_mission_events` feeds it every mission event
    # (that is where its attempt counting and roam-block back-off come from).
    h.planner = DayPlanner(random.Random(1))
    # The real roam engine: free roam's single owner. `_handle_mission_events`
    # resets its "the story has to move" clock and `_reflex` reads its
    # standing-still measure, so stubbing it out would stop testing exactly the
    # machinery that keeps him from standing there.
    h.roam = RoamEngine(random.Random(1))
    h.writer = _FakeWriter()
    h._pending_screenshot_trigger = None
    h._pending_big_event = None
    h.shots = []
    h._capture_screenshot = lambda hint: (h.shots.append(hint) or (b"jpeg", f"https://x/{hint}.jpg"))
    # mission-knowledge seam (see test_mission_knowledge.py for its own coverage)
    h.current_mission = None
    h._read_mission_title = lambda jpeg: None
    h.totals = throwaway_totals()
    h.counters = h.totals.counters()
    h._mission_started_iso = None
    h._mission_tokens = 0
    h.learned_scripts = throwaway_learned_scripts()
    return h


_STATE = SimpleNamespace(
    mission=SimpleNamespace(
        active=True, cutscene_active=False, random_event_active=False, script=None
    ),
    player=SimpleNamespace(dead=False, arrested=False),
    location=SimpleNamespace(zone="Downtown Vinewood", street="Vinewood Blvd"),
)


def test_mission_start_captures_a_screenshot_and_arms_the_director() -> None:
    h = _event_harness([MissionEvent("mission_start", {"name": "unknown"})])
    Harness._handle_mission_events(h, _STATE, None)
    assert h.shots == ["mission_start"]
    assert h._pending_screenshot_trigger == "mission_start"
    assert h._pending_big_event == "mission_start"
    assert h.writer.events == [("mission_start", {"name": "unknown"}, "https://x/mission_start.jpg")]


def test_mission_end_records_the_event_without_arming_vision() -> None:
    h = _event_harness([MissionEvent("mission_end", {"name": "unknown", "outcome": "passed"})])
    Harness._handle_mission_events(h, _STATE, None)
    assert h.shots == []
    assert h._pending_screenshot_trigger is None
    assert h._pending_big_event == "mission_end"
    assert h.writer.events[0][2] is None


def test_a_threat_post_that_never_reached_the_game_does_not_start_the_hold() -> None:
    """`_execute_action` returns None when the bridge refused or suppressed the
    task (cutscene, bridge blip). That post never happened, so the reflex must
    be free to ask again on the very next tick rather than sitting out the
    hold-down for a task the game never received."""
    stub = _ReflexStub()
    stub._execute_action = lambda t, p, tok=None: (stub.posted.append((t, dict(p))), None)[1]
    _reflex(stub, _hostile_state(task_status="idle"))
    stub.clock.tick(0.3)  # deep inside the hold-down
    _reflex(stub, _hostile_state(task_status="idle"))
    assert [t for t, _ in stub.posted] == [
        "combat_hated_targets_around",
        "combat_hated_targets_around",
    ]


# --- MissionFollower: the blue dot IS the objective on a follow mission --------
#
# Measured live: `mission.active = true`, `mission.objective_blip = null`, a
# `friendly` ped 18 m ahead driving away, and the agent emitting "no marker yet,
# waiting for the job to tell me where to go" until the game printed
#     MISSION FAILED
#     Franklin lost Lamar.
# `MissionFollower` returned None outright when the blip was null, so nothing
# in the harness even tried. States below are built from CONTRACTS §1's
# documented /state shape (v1.5 `relationship: "friendly"`, v1.6
# `nearby.peds[].pos`) by the explicit builder above — no invented recordings.


def _friendly_ped(
    distance: float, handle: int = 501, in_vehicle_handle: int | None = None
) -> dict[str, Any]:
    return {
        "handle": handle,
        "model": "ig_lamardavis",
        "distance": distance,
        "relationship": "friendly",
        "pos": {"x": distance, "y": 0.0, "z": 0.0},
        "in_vehicle_handle": in_vehicle_handle,
    }


def _vehicle(
    distance: float, handle: int = 900, driver: str = "empty",
    pos: tuple[float, float, float] | None = None,
) -> dict[str, Any]:
    """A nearby car, contract-shaped — enough for `enter_nearest_vehicle` to be plausible."""
    x, y, z = pos if pos is not None else (distance, 0.0, 0.0)
    return {
        "handle": handle,
        "model": "buffalo",
        "display_name": "Buffalo",
        "class": "Sports",
        "distance": distance,
        "driver": driver,
        "pos": {"x": x, "y": y, "z": z},
    }


def _tail_state(**over: Any) -> GameState:
    over.setdefault("mission_active", True)
    over.setdefault("nearby_peds", [_friendly_ped(18.0)])
    return make_state(**over)


def test_no_marker_and_a_friendly_nearby_gets_followed() -> None:
    f = MissionFollower()
    step = f.plan(_tail_state())
    assert step == {"type": "follow_entity", "params": {"handle": 501, "in_vehicle": False}}


def test_the_follow_uses_the_vehicle_mode_when_he_is_driving() -> None:
    f = MissionFollower()
    step = f.plan(_tail_state(in_vehicle=True))
    assert step == {"type": "follow_entity", "params": {"handle": 501, "in_vehicle": True}}


def test_the_nearest_friendly_wins_when_a_whole_crew_is_around() -> None:
    f = MissionFollower()
    step = f.plan(
        _tail_state(
            nearby_peds=[
                _friendly_ped(30.0, handle=901),
                _friendly_ped(4.7, handle=902),
                _friendly_ped(12.0, handle=903),
            ]
        )
    )
    assert step is not None and step["params"]["handle"] == 902


def test_a_routed_entity_beats_the_nearest_friendly() -> None:
    """CONTRACTS v1.8 `route_blips[kind="entity"]` is the GAME saying what the
    follow target is. Our nearest-friendly guess is an inference, and on a crew
    mission it is frequently the wrong member of the crew — or a ped standing
    next to the car that actually matters. When the game has plotted a route,
    that route wins.
    """
    f = MissionFollower()
    step = f.plan(
        _tail_state(
            nearby_peds=[_friendly_ped(4.7, handle=902)],
            route_blips=[((30.0, 40.0, 0.0), "entity", 771)],
        )
    )
    assert step is not None
    assert step["params"]["handle"] == 771, "the routed entity, not the closer friendly ped"


def test_a_coord_route_does_not_hijack_the_follow() -> None:
    """`kind == "coord"` is a PLACE, not a thing to tail. Passing its handle to
    `follow_entity` would be following a map pin."""
    f = MissionFollower()
    step = f.plan(
        _tail_state(
            nearby_peds=[_friendly_ped(4.7, handle=902)],
            route_blips=[((30.0, 40.0, 0.0), "coord", 771)],
        )
    )
    assert step is not None and step["params"]["handle"] == 902


def test_the_nearest_routed_entity_wins_when_there_are_two() -> None:
    """A follow target plus a drop-off is exactly the case v1.8 shipped the
    list for. Nearest is the one he is on now."""
    f = MissionFollower()
    step = f.plan(
        _tail_state(
            nearby_peds=[],
            route_blips=[
                ((300.0, 0.0, 0.0), "entity", 700),
                ((25.0, 0.0, 0.0), "entity", 701),
            ],
        )
    )
    assert step is not None and step["params"]["handle"] == 701


def test_a_pre_v1_8_bridge_still_falls_back_to_the_blue_dot() -> None:
    """No `route_blips` key at all (an older bridge) must not break the tail."""
    f = MissionFollower()
    step = f.plan(_tail_state(nearby_peds=[_friendly_ped(9.0, handle=902)]))
    assert step is not None and step["params"]["handle"] == 902


def test_a_mission_with_no_friendly_in_range_posts_nothing() -> None:
    """No marker and nobody to follow is a genuinely open question; it belongs
    to the brain, not to a reflex guessing at coordinates."""
    f = MissionFollower()
    assert f.plan(_tail_state(nearby_peds=[])) is None


def test_the_blue_dot_is_not_chased_during_a_cutscene() -> None:
    f = MissionFollower()
    assert f.plan(_tail_state(cutscene_active=True)) is None


def test_the_tail_is_posted_once_over_ten_ticks() -> None:
    """Same preemption rule as combat: every POST /task preempts the running
    task (CONTRACTS §1), so re-posting `follow_entity` at the poll rate would
    restart the engine's follow three times a second."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    posts: list[dict[str, Any]] = []
    task_id, task_status, task_type = None, "idle", None
    for _ in range(10):
        clock.tick(0.3)
        step = f.plan(
            _tail_state(task_id=task_id, task_status=task_status, task_type=task_type)
        )
        if step is not None:
            posts.append(step)
            task_id = f"t-tail-{len(posts)}"
            f.bind_task(task_id)
            task_status, task_type = "running", step["type"]
    assert len(posts) == 1, f"one tail, one post - got {posts}"


def test_an_objective_marker_still_beats_the_blue_dot() -> None:
    """Priority rule: a marker, when the game gives one, wins outright."""
    f = MissionFollower()
    step = f.plan(_tail_state(objective_blip={"pos": (40.0, 0.0, 0.0)}))
    assert step is not None and step["type"] == "walk_to"


def test_a_marker_appearing_mid_tail_preempts_the_follow_immediately() -> None:
    """The follow task is still `running` on that tick; the mode change has to
    drop the binding or the marker would wait for the tail to end."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    assert f.plan(_tail_state()) is not None
    f.bind_task("t-tail-1")
    clock.tick(0.3)
    step = f.plan(
        _tail_state(
            objective_blip={"pos": (40.0, 0.0, 0.0)},
            task_id="t-tail-1",
            task_status="running",
            task_type="follow_entity",
        )
    )
    assert step is not None and step["type"] == "walk_to"


def test_a_widening_gap_on_foot_sends_him_for_a_car() -> None:
    """"It's a car so it's fast": nothing on foot keeps up with something
    steadily pulling away, and the gap widening IS the mission failing."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(5.0)]))
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    gone = [_friendly_ped(5.0 + FOLLOW_GAP_WIDEN_M + 10.0)]
    clock.tick(0.3)
    assert f.plan(_tail_state(nearby_peds=gone, **running)) is None, "one sample is a corner"
    clock.tick(FOLLOW_GAP_WINDOW_S + 0.1)
    step = f.plan(_tail_state(nearby_peds=gone, **running))
    assert step is not None and step["type"] == "enter_nearest_vehicle"


def test_a_widening_gap_while_driving_asks_for_chase_speed_once() -> None:
    """Losing ground in a car escalates to `speed_mps` — ONCE, then holds.

    This test used to assert the opposite (`posts == []`, "nothing faster
    exists to post") and it was right at the time: `follow_entity` took
    `{handle, in_vehicle}` and nothing else, so the ladder could only complain.
    CONTRACTS v1.9 added `speed_mps` precisely because the bridge was tailing at
    a hard-coded 15 m/s in a style that stops at red lights, which no mission
    NPC does. The escalation is in the latch signature, so it must post exactly
    once and then stop — asking for the same speed every tick would be the
    task-thrash this class exists to prevent.
    """
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(5.0)], in_vehicle=True))
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    # A widening gap means the target is genuinely MOVING away — the fixture must move it, or
    # this is a mutual stall (neither party moving), which is a different situation with a
    # different correct answer. `_friendly_ped` derives pos from distance, so growing the
    # distance each tick moves them.
    posts = []
    gap = 5.0 + FOLLOW_GAP_WIDEN_M + 10.0
    for _ in range(10):
        clock.tick(FOLLOW_GAP_WINDOW_S)
        gap += 5.0
        step = f.plan(_tail_state(nearby_peds=[_friendly_ped(gap)], in_vehicle=True, **running))
        if step is not None:
            posts.append(step)
    assert len(posts) == 1, f"escalate once, then hold; got {posts}"
    assert posts[0]["type"] == "follow_entity"
    assert posts[0]["params"]["speed_mps"] == FOLLOW_CHASE_SPEED_MPS
    assert posts[0]["params"]["in_vehicle"] is True
    assert "style" not in posts[0]["params"], (
        "style must never be sent on a follow: the wire default is `normal`, "
        "which stops at red lights — the bridge's ignore_lights default is right"
    )
    assert "PULLING AWAY" in f.note()


# --- MissionFollower: target-lost RECOVERY LADDER (v1.10 work package) ------
#
# Replaces the old single "wait 10s, say `stop` once" behaviour. See the
# module docstring above FOLLOW_RECOVERY_VEHICLE_RADIUS_M for the ladder
# itself; these tests exercise each rung and the episode bookkeeping.


def test_vehicle_handoff_fires_immediately_with_no_wait_when_the_last_in_vehicle_handle_is_known() -> None:
    """The exact live bug: the followed friendly gets into a vehicle and
    disappears. Rung (a) fires on the FIRST missing tick — no 10s wait, no
    `stop` — because the last-seen `in_vehicle_handle` names the car."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(5.0, in_vehicle_handle=None)]))
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    # He gets in a car; v1.10 keeps him in nearby.peds with the handle set —
    # the tail keeps following HIM (he is still directly visible), but the
    # recovery ladder is now armed with a fact it did not have before.
    clock.tick(0.3)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(5.0, in_vehicle_handle=4242)], **running))
    assert f._follow_last_in_vehicle_handle == 4242
    # Now he actually vanishes from nearby.peds entirely (an older bridge, a
    # render-distance edge case — the last-seen handle is what saves this).
    clock.tick(0.3)
    step = f.plan(_tail_state(nearby_peds=[], **running))
    assert step == {"type": "follow_entity", "params": {"handle": 4242, "in_vehicle": False}}
    assert "vehicle" in f.note() and "4242" in f.note()


def test_rungs_never_repeat_within_one_loss_episode() -> None:
    """No known vehicle at all: rung (a) and (c) are inapplicable, so rung
    (d) — reacquire — is the first (and, within the episode, ONLY) thing that
    fires; it must not be posted a second time."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state())  # last-seen pos becomes (18, 0, 0)
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    clock.tick(1.0)
    first = f.plan(_tail_state(nearby_peds=[], **running))
    assert first == {"type": "walk_to", "params": {"x": 18.0, "y": 0.0, "z": 0.0, "run": True}}
    assert f._recovery_tried == {"reacquire"}
    for _ in range(4):
        clock.tick(FOLLOW_RECOVERY_RUNG_GAP_S)
        step = f.plan(_tail_state(nearby_peds=[], **running))
        assert step is None, "reacquire already tried this episode; nothing else applies yet"


def test_nearest_unclaimed_vehicle_within_range_is_tried_when_no_own_handle_is_known() -> None:
    """Rung (c): a pre-v1.10 bridge (or any tick with no `in_vehicle_handle`)
    still gets a real hypothesis — a nearby, unclaimed car close to where he
    was last seen — before falling back to reacquiring on foot."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(5.0)]))  # last-seen pos (5, 0, 0)
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    clock.tick(1.0)
    nearby_vehicles = [
        _vehicle(999.0, handle=1, driver="player", pos=(4.0, 0.0, 0.0)),  # his own car: excluded
        _vehicle(
            999.0, handle=2, driver="empty",
            pos=(5.0 + FOLLOW_RECOVERY_VEHICLE_RADIUS_M + 1.0, 0.0, 0.0),
        ),  # just outside the radius
        _vehicle(999.0, handle=3, driver="empty", pos=(9.0, 0.0, 0.0)),  # within range: this one
    ]
    step = f.plan(_tail_state(nearby_peds=[], nearby_vehicles=nearby_vehicles, **running))
    assert step == {"type": "follow_entity", "params": {"handle": 3, "in_vehicle": False}}
    assert f._recovery_tried == {"nearest_vehicle"}


def test_a_pre_v1_10_state_with_nothing_nearby_degrades_to_reacquire_then_exhausted() -> None:
    """No `in_vehicle_handle`, no nearby vehicle at all: the ladder still
    degrades gracefully through (c) skipped -> (d) reacquire -> (e) exhausted,
    never crashing and never guessing."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state(in_vehicle=True, nearby_peds=[_friendly_ped(5.0)]))
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    clock.tick(1.0)
    step = f.plan(_tail_state(in_vehicle=True, nearby_peds=[], **running))
    assert step is not None and step["type"] == "drive_to", "in a car: reacquire drives, not walks"
    assert step["params"]["style"] == "rushed"
    assert step["params"]["arrive_radius_m"] == DRIVE_ARRIVE_RADIUS_M
    clock.tick(FOLLOW_RECOVERY_EXHAUSTED_S + FOLLOW_RECOVERY_RUNG_GAP_S)
    exhausted = f.plan(_tail_state(in_vehicle=True, nearby_peds=[], **running))
    assert exhausted == {"type": "stop", "params": {}}, "the ladder ran out; one stop, exactly as before"
    for _ in range(3):
        clock.tick(5.0)
        assert f.plan(_tail_state(in_vehicle=True, nearby_peds=[], **running)) is None, "said once"


def test_a_brief_regain_then_loss_continues_the_ladder_rather_than_restarting_it() -> None:
    """The brief's own rule: "if the SAME loss recurs immediately ... do not
    restart the ladder from rung (a) blindly — continue where it left off"."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state())
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    clock.tick(1.0)
    first = f.plan(_tail_state(nearby_peds=[], **running))
    assert first == {"type": "walk_to", "params": {"x": 18.0, "y": 0.0, "z": 0.0, "run": True}}
    assert f._recovery_tried == {"reacquire"}

    clock.tick(0.5)
    # Regained briefly — well under FOLLOW_RECOVERY_EPISODE_RESET_S.
    f.plan(_tail_state(nearby_peds=[_friendly_ped(20.0)], **running))
    assert f._recovery_tried == {"reacquire"}, "a brief regain must not reset ladder progress"

    clock.tick(FOLLOW_RECOVERY_RUNG_GAP_S + 0.1)
    # Lost again: "reacquire" must not fire a second time, and nothing else
    # is applicable (still no known vehicle), so nothing posts.
    again = f.plan(_tail_state(nearby_peds=[], **running))
    assert again is None
    assert f._recovery_tried == {"reacquire"}


def test_holding_the_regained_target_long_enough_resets_the_ladder_for_next_time() -> None:
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state())
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    clock.tick(1.0)
    assert f.plan(_tail_state(nearby_peds=[], **running)) is not None
    assert f._recovery_tried == {"reacquire"}

    clock.tick(1.0)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(20.0)], **running))  # regained
    assert f._recovery_regained_at is not None
    clock.tick(FOLLOW_RECOVERY_EPISODE_RESET_S + 1.0)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(22.0)], **running))  # held continuously
    assert f._recovery_tried == set(), "a genuinely fresh episode resets the ladder"

    clock.tick(1.0)
    again = f.plan(_tail_state(nearby_peds=[], **running))
    assert again is not None and again["type"] == "walk_to", "rung (d) is available again"


def test_a_different_companion_restarts_the_tail() -> None:
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    assert f.plan(_tail_state(nearby_peds=[_friendly_ped(6.0, handle=501)])) is not None
    f.bind_task("t-tail-1")
    clock.tick(0.3)
    step = f.plan(
        _tail_state(
            nearby_peds=[_friendly_ped(6.0, handle=777)],
            task_id="t-tail-1",
            task_status="running",
            task_type="follow_entity",
        )
    )
    assert step is not None and step["params"]["handle"] == 777


def test_the_follow_note_tells_the_brain_what_is_happening() -> None:
    f = MissionFollower()
    assert "not pursuing" in f.note()
    f.plan(_tail_state())
    assert "tailing the friendly" in f.note()
    # He vanishes: the note must stop claiming a tail that is over, AND must
    # never fall back to a bare "no friendly in range" while a loss episode is
    # being actively worked — the whole point of the recovery ladder (brief:
    # "Never again a bare 'no friendly in range' during an active loss
    # episode").
    f.plan(_tail_state(nearby_peds=[], task_status="running", task_type="follow_entity"))
    note = f.note()
    assert "tailing the friendly" not in note
    assert "no friendly in range" not in note
    assert "vanished" in note


# --- `_reflex` wiring: the damage-driven threat path and the stall breaker ----


def _neutral_ped(distance: float, handle: int = 601) -> dict[str, Any]:
    return {
        "handle": handle,
        "model": "a_m_y_skater_01",
        "distance": distance,
        "relationship": "neutral",
    }


#: A task already running, so the free-roam reflexes (stranded escalator, L2
#: wander) stay out of these assertions - they only act on an idle slot.
_BUSY = {"task_id": "t-busy", "task_status": "running", "task_type": "walk_to"}


def test_a_neutral_ped_beating_him_gets_fought() -> None:
    """The live bug end to end: health falling with only `neutral` peds in the
    snapshot must produce a combat post from the reflex layer."""
    stub = _ReflexStub()
    health = 200
    for _ in range(4):
        stub.clock.tick(0.3)
        health -= 5  # a punch: well under Delta.big_health_drop's 25 HP bar
        _reflex(stub, make_state(health=health, nearby_peds=[_neutral_ped(2.0)], **_BUSY))
    assert [t for t, _ in stub.posted] == ["combat_hated_targets_around"]


def test_the_damage_threat_is_posted_once_over_ten_ticks() -> None:
    stub = _ReflexStub()
    health = 200
    task = dict(_BUSY)
    for _ in range(10):
        stub.clock.tick(0.3)
        health -= 5
        _reflex(stub, make_state(health=health, nearby_peds=[_neutral_ped(2.0)], **task))
        if stub.posted:
            task = {
                "task_id": "t-reflex-1",
                "task_status": "running",
                "task_type": stub.posted[-1][0],
            }
    assert [t for t, _ in stub.posted] == ["combat_hated_targets_around"]


def test_no_combat_reflex_during_a_cutscene_however_hard_he_is_hit() -> None:
    stub = _ReflexStub()
    health = 200
    for _ in range(6):
        stub.clock.tick(0.3)
        health -= 10
        _reflex(
            stub,
            make_state(
                health=health,
                mission_active=True,
                cutscene_active=True,
                nearby_peds=[_neutral_ped(1.5)],
                **_BUSY,
            ),
        )
    assert stub.posted == [], "a scripted beat is the game's wheel, not his"


def test_no_combat_reflex_while_he_is_dead() -> None:
    stub = _ReflexStub()
    for _ in range(6):
        stub.clock.tick(0.3)
        _reflex(
            stub,
            make_state(dead=True, health=0, nearby_peds=[_neutral_ped(1.5)], **_BUSY),
        )
    assert stub.posted == []


def test_a_task_that_pins_him_in_place_is_abandoned_once() -> None:
    """Measured live: `combat_hated_targets_around` RUNNING for 20 s, 0.2 m of
    movement, full health — a cat in `nearby.peds` with
    `relationship: "hostile"` that the task can never finish killing."""
    stub = _ReflexStub()
    pinned = {
        "in_vehicle": True,
        "task_id": "t-pin",
        "task_status": "running",
        "task_type": "drive_to",
        "pos": (10.0, 10.0, 0.0),
    }
    for _ in range(30):
        stub.clock.tick(1.0)
        _reflex(stub, make_state(**pinned))
    assert [t for t, _ in stub.posted] == ["stop"], (
        f"one `stop` per stall episode, not one per tick - got {stub.posted}"
    )


def test_the_stall_breaker_leaves_a_deliberate_wait_alone() -> None:
    """A `wait` (the brain's own action, and the beat in every park-and-watch
    activity) is standing still ON PURPOSE."""
    stub = _ReflexStub()
    stub._quiet_until = float("inf")
    pinned = {
        "in_vehicle": True,
        "task_id": "t-pin",
        "task_status": "running",
        "task_type": "drive_to",
        "pos": (10.0, 10.0, 0.0),
    }
    for _ in range(30):
        stub.clock.tick(1.0)
        _reflex(stub, make_state(**pinned))
    assert stub.posted == []


# --- the modal/blocking screen: stop pretending to see the world -------------


def test_execute_action_suppresses_every_bridge_task_on_a_blocking_screen() -> None:
    """The script thread is not ticking, so the game will not apply a queued
    command at all — and the caller would bind to a task that never starts."""
    h = _bare_harness(cutscene_active=False)
    h._screen_blocked = True
    token = _holding(h)
    for task_type in BRIDGE_TASKS:
        assert Harness._execute_action(h, task_type, {}, token) is None, task_type
    assert h.bridge.posted == []


def test_execute_action_suppresses_every_bridge_task_during_a_protagonist_switch() -> None:
    """v1.11 `player.switch_in_progress`, gated exactly like a cutscene at the
    single choke point every bridge task goes through."""
    h = _bare_harness(cutscene_active=False)
    h._switch_in_progress = True
    token = _holding(h)
    for task_type in BRIDGE_TASKS:
        assert Harness._execute_action(h, task_type, {}, token) is None, task_type
    assert h.bridge.posted == []


def test_execute_action_suppresses_every_bridge_task_during_a_mission_retry() -> None:
    """v1.11 `mission.retry_in_flight`."""
    h = _bare_harness(cutscene_active=False)
    h._retry_in_flight = True
    token = _holding(h)
    for task_type in BRIDGE_TASKS:
        assert Harness._execute_action(h, task_type, {}, token) is None, task_type
    assert h.bridge.posted == []


class _ContextStub:
    """Enough of the harness for the REAL `_dynamic_context`."""

    def __init__(self, screen_blocked: bool) -> None:
        self.current_goal = "see the city"
        self.mood = MoodModel(rng=random.Random(1))
        self.governor = SimpleNamespace(level=0)
        self.missions = MissionTracker()
        self.mission_follower = MissionFollower()
        self.planner = DayPlanner(random.Random(1))
        self.activity_runner = ActivityRunner(
            ActivityPicker(random.Random(1)), random.Random(1)
        )
        #: `_dynamic_context` renders the ROAM block (current goal + the menu the
        #: model must pick from) out of this, and reads `goal_text` for the GOAL
        #: line, so the real engine is wired in rather than stood in for.
        self.roam = RoamEngine(random.Random(1))
        self.current_mission = None
        self.memory = SimpleNamespace(context_block=lambda: "")
        self.commentary = SimpleNamespace(
            recent=SimpleNamespace(context_block=lambda: "")
        )
        self._screen_blocked = screen_blocked
        # Set once per tick by `_reflex`; `_dynamic_context` reads it to steer knowledge
        # retrieval toward combat when he is being hit by a ped /state calls `neutral`.
        # `_dynamic_context` now reads `settings.missions_enabled` for the PHONE line.
        self.settings = SimpleNamespace(missions_enabled=True)
        self._under_attack = False
        self._mission_active = False

    def _goal_prompt_line(self) -> str:
        return Harness._goal_prompt_line(self)

    @property
    def goal_text(self) -> str:
        # The real one: "a locked roam goal outranks the director's goal line"
        # is behaviour under test, not scaffolding.
        return Harness.goal_text.fget(self)


def test_a_blocking_screen_is_admitted_to_the_brain() -> None:
    """Measured on the broadcast: "Alpha's right there. Staying on his six."
    over a MISSION FAILED banner, for over a minute. `/state` is frozen at that
    point, so every world fact in the prompt is a stale lie and the model has
    to be told."""
    blocked = Harness._dynamic_context(
        _ContextStub(True), make_state(), Delta(wanted_from=0, wanted_to=0), "poll", "tactical"
    )
    assert "BLOCKED:" in blocked
    assert "do not narrate the world" in blocked
    clear = Harness._dynamic_context(
        _ContextStub(False), make_state(), Delta(wanted_from=0, wanted_to=0), "poll", "tactical"
    )
    assert "BLOCKED:" not in clear


def test_the_threat_and_blip_line_is_short_and_names_the_handle() -> None:
    """v1.11: `_dynamic_context` gives the brain a compact ATTACKER/BLIPS line
    instead of leaving it to find `threat`/`entity_blips` inside the raw
    `STATE:` json."""
    body = make_state(
        nearby_peds=[
            {
                "handle": 9012,
                "model": "a_m_y_business_01",
                "distance": 3.0,
                "relationship": "neutral",
                "pos": {"x": 1.0, "y": 0.0, "z": 0.0},
                "in_vehicle_handle": None,
                "attacking_me": True,
                "weapon_class": "melee",
            }
        ]
    ).model_dump(by_alias=True)
    body["threat"] = {"attacker_handle": 9012, "being_jacked_by": None}
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
    state = GameState.model_validate(body)
    ctx = Harness._dynamic_context(
        _ContextStub(False), state, Delta(wanted_from=0, wanted_to=0), "poll", "tactical"
    )
    assert "ATTACKER: handle 9012, melee — use fight_ped" in ctx
    assert "NAMED BLIPS: Lamar 220m" in ctx


def test_the_mission_tail_stands_down_when_survival_has_the_tick() -> None:
    """Survival outranks the objective. `_drive_mission_objective` runs after
    `_reflex` in the same tick and must not post over the combat/cover task it
    just issued (see the note on `_threat_has_the_wheel` in main)."""
    stub = _RecordingStub({"type": "drive_to", "params": {"x": 1.0, "y": 2.0, "z": 3.0}})
    stub._threat_has_the_wheel = True
    state = make_state(mission_active=True, in_vehicle=True, objective_blip={"pos": (1.0, 2.0, 3.0)})
    Harness._drive_mission_objective(stub, state)
    assert stub.posted == []


# -- the mutual-wait deadlock ---------------------------------------------------------------
# Observed live 2026-09-02: the agent and Lamar stood facing each other in the road for minutes while
# he narrated "Holding position. His call." and "He'll move when he moves." He was waiting for the
# crewmate; the crewmate was a mission NPC waiting for the player to get in the car.


def test_two_men_standing_still_eventually_makes_him_act() -> None:
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(8.0)]))
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    still = [_friendly_ped(8.0)]

    clock.tick(MUTUAL_STALL_WINDOW_S - 2.0)
    assert f.plan(_tail_state(nearby_peds=still, **running)) is None, "not yet — dialogue is long"

    clock.tick(4.0)
    step = f.plan(_tail_state(nearby_peds=still, nearby_vehicles=[_vehicle(6.0)], **running))
    assert step is not None, "after the window he must DO something"
    assert step["type"] == "enter_nearest_vehicle", "getting in the car is the usual trigger"


def test_a_moving_crewmate_is_never_a_deadlock() -> None:
    """The tail is working; leave it alone. Only both-still is the deadlock.

    "Working" means he is KEEPING UP: the crewmate's position keeps changing
    while the gap between them stays put. Distance is what the escalation
    watches, and a tail that holds its distance needs no help. (An earlier
    version of this test walked the target 8 m -> 36 m away and asserted the
    same thing, which stopped being true once the widening-gap thresholds were
    tightened on 2026-09-02: a target steadily pulling away on foot SHOULD send
    him after a car. That is the escalation working, not a deadlock
    misfiring - so the case belongs in the escalation's own test, not here.)
    """
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(8.0)]))
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    for i in range(8):
        clock.tick(5.0)
        # Both of them moving down the street together: the pos changes every
        # tick (so `_mutual_stall` re-anchors), the gap does not (so nothing
        # escalates).
        keeping_up = [_friendly_ped(8.0)]
        keeping_up[0]["pos"] = {"x": 8.0 + i * 20.0, "y": 0.0, "z": 0.0}
        step = f.plan(
            _tail_state(
                nearby_peds=keeping_up,
                pos=(i * 20.0, 0.0, 0.0),
                **running,
            )
        )
        assert step is None or step["type"] != "enter_nearest_vehicle", (
            "a target that is moving, at a steady distance, is not a stall"
        )


def test_a_stalled_driver_drives_to_them_rather_than_getting_out_to_walk() -> None:
    """Telling a man in a car to walk makes him get out first: slower, and it looks broken."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(30.0)], in_vehicle=True))
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    clock.tick(MUTUAL_STALL_WINDOW_S + 1.0)
    step = f.plan(_tail_state(nearby_peds=[_friendly_ped(30.0)], in_vehicle=True, **running))
    assert step is not None and step["type"] == "drive_to"


def test_the_deadlock_breaker_does_not_fire_every_tick() -> None:
    """Re-firing while the world catches up with the task just posted is its own standing still."""
    clock = FakeClock()
    f = MissionFollower(clock=clock)
    f.plan(_tail_state(nearby_peds=[_friendly_ped(8.0)]))
    f.bind_task("t-tail-1")
    running = {"task_id": "t-tail-1", "task_status": "running", "task_type": "follow_entity"}
    still = [_friendly_ped(8.0)]
    clock.tick(MUTUAL_STALL_WINDOW_S + 1.0)
    breaks = 0
    for _ in range(12):
        clock.tick(2.0)
        step = f.plan(_tail_state(nearby_peds=still, nearby_vehicles=[_vehicle(6.0)], **running))
        if step is not None and step["type"] == "enter_nearest_vehicle":
            breaks += 1
    assert breaks <= 2, f"cooldown should hold it to at most a couple of attempts, got {breaks}"


def test_every_drive_to_the_follower_emits_carries_a_numeric_speed() -> None:
    """CONTRACTS §1: `drive_to` params are `{x,y,z, speed_mps, style, arrive_radius_m}`
    and the bridge enforces it — a missing speed is a hard 400.

    OBSERVED LIVE 2026-09-02, on the very first run of the recovery ladder:

        mission follow: target-lost recovery ladder rung=reacquire task=drive_to
        bridge rejected action type=drive_to status=400 error=invalid_params
                              detail="drive_to requires numeric speed_mps"

    Both of this module's hand-built `drive_to` bodies (the mutual-stall
    deadlock breaker and the ladder's reacquire rung) omitted `speed_mps`, so
    NEITHER recovery has ever reached the game — the deadlock breaker has been
    silently 400ing since it was written, which is part of why he thrashed.
    `navigation.navigate_to` always got this right; only the hand-rolled copies
    did not. A structural check pins the whole class rather than the two
    instances: any future hand-built drive_to has to carry a speed too.
    """
    import inspect

    from wasted_harness.behavior import missions as missions_mod

    blocks = inspect.getsource(missions_mod).split('"type": "drive_to"')[1:]
    assert blocks, "expected at least one hand-built drive_to in this module"
    for block in blocks:
        head = block[:320]
        assert "speed_mps" in head, (
            "a drive_to task is built without speed_mps; the bridge rejects it with "
            f"400 invalid_params. Offending block starts: {head[:160]!r}"
        )


def test_the_dot_on_the_map_beats_a_stale_last_seen_position() -> None:
    """OBSERVED LIVE 2026-09-02 (operator screenshot): the crewmate drove off,
    left the ~50 m `nearby.peds` radius, and the agent drove around at RANDOM
    looking for him — "No sign of him anywhere. Widening the search, freeway's
    as good a start as any." — while the game was drawing his dot on the
    minimap the entire time.

    CONTRACTS v1.11 `mission.entity_blips[]` is that dot: entity-attached blips
    survive the ped-scan radius and carry the game's own label ("Lamar"). A LIVE
    blip must therefore outrank the stale `reacquire` rung, which only knows
    where he was when he disappeared.
    """
    clock = FakeClock()
    f = MissionFollower(clock=clock)

    # Tailing him normally at close range.
    seen = _tail_state(nearby_peds=[_friendly_ped(6.0)])
    f.plan(seen)
    f.bind_task("t-tail-1")

    # Now he is gone from nearby.peds entirely — but his blip is 220 m out.
    gone = _tail_state(
        nearby_peds=[],
        entity_blips=[((220.0, 0.0, 0.0), 998877, "Lamar")],
        task_id="t-tail-1",
        task_status="done",
    )
    clock.tick(FOLLOW_RECOVERY_RUNG_GAP_S + 1.0)
    step = f.plan(gone)

    assert step is not None, "he must act on the dot, not stand there"
    assert step["params"].get("handle") == 998877 or (
        round(step["params"].get("x", 0)) == 220
    ), f"must target the blip (handle or its position), got {step}"
    assert "Lamar" in f.note(), f"the note should name him from the blip: {f.note()!r}"

# The real `_reflex` now consults the phone reflex and the game-cleared backoff every tick.
# Stubs get a quiet phone and an open backoff so every existing scenario is unchanged.
def _quiet_phone(self, state):
    return False


_ReflexStub._phone_reflex = _quiet_phone  # type: ignore[attr-defined]
_ReflexStub.cleared_backoff = ClearedByGameBackoff()  # type: ignore[attr-defined]
def _quiet_phone(self, state):
    return False


_RecordingStub._phone_reflex = _quiet_phone  # type: ignore[attr-defined]
_RecordingStub.cleared_backoff = ClearedByGameBackoff()  # type: ignore[attr-defined]


def test_with_missions_off_the_brain_is_told_and_sees_no_job_markers() -> None:
    """Watched on the dashboard with missions off: "walk to Franklin's marker and start
    the job". The planner and roam engine refused missions, but the brain could still
    set that goal itself: it saw mission.starts[] and the director prompt said work."""
    from types import SimpleNamespace

    stub = _ContextStub(False)
    stub.settings = SimpleNamespace(missions_enabled=False)
    state = make_state(starts=[((300.0, 0.0, 0.0), "franklin")])
    ctx = Harness._dynamic_context(stub, state, Delta(wanted_from=0, wanted_to=0), "poll", "tactical")
    assert "MISSIONS ARE OFF" in ctx
    assert '"starts":[]' in ctx.replace(" ", ""), "job markers must not be shown to him"

    stub.settings = SimpleNamespace(missions_enabled=True)
    ctx_on = Harness._dynamic_context(stub, state, Delta(wanted_from=0, wanted_to=0), "poll", "tactical")
    assert "MISSIONS ARE OFF" not in ctx_on
    assert '"starts":[]' not in ctx_on.replace(" ", "")


# -- the phone (T8, findings.md R6, CONTRACTS v1.13) -------------------------------------------
# "it cant cut the call, check or accept whatever" / "calls are entertaining and can start
# story": the reflex now ANSWERS every ring, missions on or off, and — missions off only —
# hangs the call up again after a budget, or immediately during a fight/chase.


def _phone_harness(missions_enabled: bool) -> Harness:
    from types import SimpleNamespace

    h = _bare_harness(cutscene_active=False)
    h.settings = SimpleNamespace(missions_enabled=missions_enabled, phone_enabled=True)
    h._phone_answered_this_ring = False
    h._phone_hung_up_this_call = False
    h._phone_call_connected_at = None
    h.phone_hangup_after_s = PHONE_HANGUP_AFTER_S
    return h


def _phone_state(*, ringing: bool, in_call: bool, wanted: int = 0) -> GameState:
    return make_state(phone={"ringing": ringing, "in_call": in_call}, wanted=wanted)


def _phone_state_under_attack(handle: int = 9012) -> GameState:
    """A connected call while `threat.attacker_handle` is live (a fight is on)."""
    body = make_state(phone={"ringing": False, "in_call": True}).model_dump(by_alias=True)
    body["threat"] = {"attacker_handle": handle, "being_jacked_by": None}
    return GameState.model_validate(body)


def test_a_ringing_phone_gets_one_attempt_per_ring_answer_on_reject_off() -> None:
    """LIVE 2026-09-03 12:29Z: an answered story call froze him for its whole
    length (`phone.in_call` true, control on, every movement task at 0.00 m), so
    with jobs off the ring is REJECTED; with jobs on it is still answered."""
    for missions_enabled, verb in ((False, "reject_call"), (True, "answer_call")):
        h = _phone_harness(missions_enabled=missions_enabled)
        ringing = _phone_state(ringing=True, in_call=False)
        h.wheel.begin_tick()
        assert Harness._phone_reflex(h, ringing) is True
        assert [t for t, _ in h.bridge.posted] == [verb]
        h.wheel.begin_tick()
        assert Harness._phone_reflex(h, ringing) is False, "one attempt per ring, not every tick"
        assert len(h.bridge.posted) == 1
        # the ring ends, a new one starts: the latch re-arms
        h.wheel.begin_tick()
        Harness._phone_reflex(h, _phone_state(ringing=False, in_call=False))
        h.wheel.begin_tick()
        assert Harness._phone_reflex(h, ringing) is True
        assert len(h.bridge.posted) == 2


def test_with_missions_off_a_connected_call_is_hung_up_at_once(monkeypatch) -> None:
    """The 25 s budget used to apply here; measured live (2026-09-03 12:29Z),
    every second of a connected call is a second he cannot move, so a call that
    got connected while jobs are off is hung up immediately."""
    import time as time_mod

    now = {"t": 1_000_000.0}
    monkeypatch.setattr(time_mod, "monotonic", lambda: now["t"])
    h = _phone_harness(missions_enabled=False)
    live = _phone_state(ringing=False, in_call=True)

    h.wheel.begin_tick()
    assert Harness._phone_reflex(h, live) is True, "no budget: hang up now"
    assert [t for t, _ in h.bridge.posted] == ["reject_call"]

    h.wheel.begin_tick()
    assert Harness._phone_reflex(h, live) is False, "latched: one hang-up per call"
    assert len(h.bridge.posted) == 1

    # the call ends and a new one connects: the latch re-arms
    h.wheel.begin_tick()
    Harness._phone_reflex(h, _phone_state(ringing=False, in_call=False))
    h.wheel.begin_tick()
    assert Harness._phone_reflex(h, live) is True, "a fresh call gets its own hang-up"
    assert len(h.bridge.posted) == 2
    assert PHONE_HANGUP_AFTER_S > 0  # the jobs-on documentation constant survives


def test_with_missions_off_a_connected_call_is_hung_up_immediately_during_a_fight() -> None:
    h = _phone_harness(missions_enabled=False)
    live = _phone_state_under_attack()
    h.wheel.begin_tick()
    assert Harness._phone_reflex(h, live) is True, "fighting for his life outranks the budget"
    assert [t for t, _ in h.bridge.posted] == ["reject_call"]


def test_with_missions_off_a_connected_call_is_hung_up_immediately_during_a_chase() -> None:
    h = _phone_harness(missions_enabled=False)
    live = _phone_state(ringing=False, in_call=True, wanted=2)
    h.wheel.begin_tick()
    assert Harness._phone_reflex(h, live) is True, "a live chase outranks the budget too"
    assert [t for t, _ in h.bridge.posted] == ["reject_call"]


def test_with_missions_on_the_phone_is_still_answered_but_never_hung_up(monkeypatch) -> None:
    import time as time_mod

    now = {"t": 2_000_000.0}
    monkeypatch.setattr(time_mod, "monotonic", lambda: now["t"])
    h = _phone_harness(missions_enabled=True)

    h.wheel.begin_tick()
    assert Harness._phone_reflex(h, _phone_state(ringing=True, in_call=False)) is True
    assert [t for t, _ in h.bridge.posted] == ["answer_call"]

    # Connected, and well past what would be the missions-off hang-up budget:
    # missions on means the story owns this call and nothing here ends it.
    now["t"] += PHONE_HANGUP_AFTER_S * 10
    h.wheel.begin_tick()
    assert Harness._phone_reflex(h, _phone_state_under_attack()) is False
    assert [t for t, _ in h.bridge.posted] == ["answer_call"], "no automatic hang-up when jobs are allowed"


def test_a_phone_reflex_post_reaches_the_real_bridge_client_not_just_the_fake() -> None:
    """Regression (found by fix-opus-b): `answer_call`/`reject_call` were
    missing from `bridge_client.BRIDGE_TASK_TYPES`, so the REAL
    `BridgeClient.post_task` raised `ValueError('not a bridge task')` before
    any I/O ever happened — `_phone_reflex` was posting the right verb and
    `BridgeClient` was throwing it away one line later, client-side. That is
    the literal bug behind the operator's "it cant cut the call".

    `_bare_harness`'s `_FakeBridge` records blindly and never exercises that
    validation at all, so it could not have caught this — this test drives a
    REAL `BridgeClient` through an `httpx.MockTransport` instead: a genuine
    recording bridge, on the exact code path the bug was in.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((body["type"], body.get("params", {})))
        return httpx.Response(202, json={"task_id": "t-real-1"})

    client = BridgeClient(timeout_s=0.5, connect_retries=0)
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        h = _phone_harness(missions_enabled=False)
        h.bridge = client
        # A connected call during a chase: hangs up immediately (T8), which
        # means `reject_call` must reach `post_task` on THIS tick.
        live = _phone_state(ringing=False, in_call=True, wanted=2)
        h.wheel.begin_tick()
        assert Harness._phone_reflex(h, live) is True
        assert calls == [("reject_call", {})]

        # And the ring half of the same path: jobs off → rejected (live
        # 2026-09-03: an answered call freezes him for its whole length).
        calls.clear()
        h.wheel.begin_tick()
        assert Harness._phone_reflex(h, _phone_state(ringing=True, in_call=False)) is True
        assert calls == [("reject_call", {})]
        # Jobs on: the same ring is answered through the same real client.
        calls.clear()
        h2 = _phone_harness(missions_enabled=True)
        h2.bridge = client
        h2.wheel.begin_tick()
        assert Harness._phone_reflex(h2, _phone_state(ringing=True, in_call=False)) is True
        assert calls == [("answer_call", {})]
    finally:
        client.close()


def test_a_wait_in_free_roam_with_control_is_ignored() -> None:
    """Seen on stream and in the soak: a completed goal, a stopped car, and the
    brain says `wait` — which held the drive-away AND the next goal pick, and the
    next think said `wait` again. In free roam with control, `wait` is a no-op."""
    h = _bare_harness(cutscene_active=False)
    assert Harness._execute_action(h, "wait", {"seconds": 12}) is None
    assert h._quiet_until == 0.0
    # ...but with something to wait for it is honoured, capped at 30 s.
    h._wanted_now = 1
    Harness._execute_action(h, "wait", {"seconds": 90})
    assert 0.0 < h._quiet_until <= time.monotonic() + 30.0 + 0.01


def test_a_running_enter_is_not_re_posted_and_freezes_him(monkeypatch) -> None:
    """PROVEN IN-GAME 2026-09-03 13:20Z: re-posting `enter_nearest_vehicle` every
    couple of seconds freezes the ped at 0.00 m; posting once walks him to a car.
    So `_execute_action` must not re-post a running `enter_nearest_vehicle` /
    `wander_drive` — it returns the live id and posts nothing new."""
    from wasted_harness.bridge_client import LastTask

    h = _bare_harness(cutscene_active=False)
    # First post reaches the bridge (no last_task cached yet → guard inert).
    first = Harness._execute_action(
        h, "enter_nearest_vehicle", {"prefer": "any", "search_radius_m": 60.0}, _holding(h, "roam")
    )
    assert first == "t-fake-1"
    assert len(h.bridge.posted) == 1
    # The bridge now reports it running. A second post of the SAME task is a no-op.
    h._live_last_task = LastTask(id=first, type="enter_nearest_vehicle", status="running", detail="")
    h.wheel.begin_tick()
    second = Harness._execute_action(
        h, "enter_nearest_vehicle", {"prefer": "any", "search_radius_m": 90.0}, _holding(h, "roam")
    )
    assert second == first, "a running enter must be left alone, not restarted"
    assert len(h.bridge.posted) == 1, "nothing new posted while it runs"
    # Once the bridge fails it, the guard lifts and recovery may re-post.
    h._live_last_task = LastTask(id=first, type="enter_nearest_vehicle", status="failed", detail="no_progress")
    h.wheel.begin_tick()
    Harness._execute_action(
        h, "enter_nearest_vehicle", {"prefer": "any", "search_radius_m": 90.0}, _holding(h, "roam")
    )
    assert len(h.bridge.posted) == 2, "a failed task no longer blocks a fresh post"


def test_the_phone_is_left_alone_by_default() -> None:
    """Operator call 2026-09-03: the phone verbs are POST /task, so each one
    preempts whatever he was doing — with Simeon calling every ~30 s that was
    the biggest interruption in his day. Off unless explicitly enabled."""
    from types import SimpleNamespace

    h = _phone_harness(missions_enabled=False)
    h.settings = SimpleNamespace(missions_enabled=False, phone_enabled=False)
    h.wheel.begin_tick()
    assert Harness._phone_reflex(h, _phone_state(ringing=True, in_call=False)) is False
    h.wheel.begin_tick()
    assert Harness._phone_reflex(h, _phone_state(ringing=False, in_call=True)) is False
    assert h.bridge.posted == [], "the harness must not touch the phone at all"
