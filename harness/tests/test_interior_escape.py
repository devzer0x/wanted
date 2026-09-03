"""CONTRACTS v1.12 — the interior escape, and the proof that it is REACHED.

The bug this closes was never "the house-escape code is wrong". `HouseEscape`
is real code with real tests. The bug was that the live loop essentially never
called it: `/state` had no field that said "he is indoors", so the only way in
was a heuristic needing 20 s of stillness AND a failed `enter_nearest_vehicle`
on the same snapshot. After a mission ended, a respawn, or a character switch
inside a safehouse, the agent stood in a living room while every outdoor movement
task failed against a nav mesh disconnected by doors.

So these tests deliberately do NOT test `InteriorEscape` in isolation and stop
there. Every acceptance test below drives the REAL `Harness._reflex` (and, for
the free-roam interaction, the real `Harness._drive_activities`) with the real
movement wheel, exactly as the live tick does — because "the ladder produces an
action" was already true before this fix and was worth nothing.

The rig (`_ReflexStub`, `make_state`, `_reflex`) is imported from
test_mission_following.py, the same way test_vehicle.py already borrows it: the
methods under test are the production ones, only their collaborators are
stand-ins.

WHAT THESE TESTS CANNOT PROVE, and no test on a dev machine can: that a real
`InteriorProxy` handle appears where we think it does, and that the game's nav
mesh really does route a `walk_to` through a given safehouse door. Both are
live-only facts (CLAUDE.md "where things run"). What is proven here is the
harness's half — reachability, ordering, arbitration, and that nothing here
teleports.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import pytest
from test_mission_following import _reflex, _ReflexStub, make_state

from wasted_harness.behavior.roam import (
    INTERIOR_EXIT_MAX_M,
    INTERIOR_SETTLE_S,
    INTERIOR_STEP_TIMEOUT_S,
    HouseEscape,
    InteriorEscape,
)
from wasted_harness.bridge_client import GameState
from wasted_harness.main import Harness

# --- the rig ------------------------------------------------------------------


class _LoopStub(_ReflexStub):
    """`_ReflexStub` plus the free-roam selection half of a real tick.

    Acceptance check (b) is about what free roam does WHILE the escape holds
    the wheel, so the roam-selection methods have to be the production ones
    too — a re-description of `_drive_activities` here would drift from the
    thing being asserted about.
    """

    _drive_activities = Harness._drive_activities
    _operator_choice = Harness._operator_choice
    _judge_roam_goal = Harness._judge_roam_goal
    _advance_roam_goal = Harness._advance_roam_goal
    _begin_roam_goal = Harness._begin_roam_goal
    _issue_activity_step = Harness._issue_activity_step

    def __init__(self, governor_level: int = 0) -> None:
        super().__init__(governor_level=governor_level)
        self.wheel.on_preempt("roam", self._roam_preempted)
        self.rng = random.Random(7)
        # Operator overrides read by the roam pick path; never armed here.
        self._operator_goal = None
        self._operator_goal_until = 0.0
        self._operator_force_pick = False


def tick(stub: _ReflexStub, state: GameState) -> None:
    """One live tick's worth of the parts under test, in production order."""
    _reflex(stub, state)


def full_tick(stub: _LoopStub, state: GameState) -> None:
    """`_reflex` then free-roam selection — the production order in `run()`."""
    _reflex(stub, state)
    stub._drive_activities(state)


def indoors(**over: Any) -> GameState:
    """On foot, settled inside interior 7, with a door behind him."""
    over.setdefault("interior", (7, INTERIOR_SETTLE_S + 2.0))
    over.setdefault("last_outdoor", (28.0, 0.0, 0.0))
    over.setdefault("pos", (0.0, 0.0, 0.0))
    return make_state(**over)


def logged(caplog: pytest.LogCaptureFixture, needle: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if needle in r.message]


def kv(record: logging.LogRecord) -> dict[str, Any]:
    return dict(getattr(record, "kv", {}) or {})


# --- ACCEPTANCE (a): he goes in, he comes out, and the log says so ------------


def test_a_he_is_walked_out_of_an_interior_and_the_log_proves_the_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole fix, end to end, through the real `_reflex`.

    The log must read `interior_detected` -> `exit_interior step` ->
    `left_interior`, in that order, and he must be out well inside the ladder's
    own budget (two rungs of :data:`INTERIOR_STEP_TIMEOUT_S`).
    """
    stub = _LoopStub()
    with caplog.at_level(logging.INFO, logger="wasted.main"):
        # Tick 1: settled indoors. Detection, then the first rung.
        tick(stub, indoors())
        assert [t for t, _ in stub.posted] == ["walk_to"], stub.posted
        target = stub.posted[0][1]
        assert (target["x"], target["y"]) == (28.0, 0.0), (
            "rung 1 walks to player.last_outdoor — where the bridge saw him "
            "standing the tick before the door"
        )

        # Tick 2: still inside, the walk is running. Nothing new is posted:
        # every POST /task preempts, so re-posting would restart the walk at
        # 3 Hz forever.
        stub.clock.tick(1.0)
        tick(stub, indoors(task_id="t-reflex-1", task_type="walk_to", task_status="running"))
        assert len(stub.posted) == 1, stub.posted

        # Tick 3: he is through the door. `interior` -> null.
        stub.clock.tick(4.0)
        tick(
            stub,
            make_state(
                interior=None,
                last_outdoor=(28.0, 0.0, 0.0),
                pos=(28.0, 0.0, 0.0),
                task_id="t-reflex-1",
                task_type="walk_to",
                task_status="done",
            ),
        )

    assert stub.clock.t - 1000.0 < 2 * INTERIOR_STEP_TIMEOUT_S, "outside the ladder's budget"

    order = [
        r.message
        for r in caplog.records
        if r.message in ("interior_detected", "exit_interior step", "left_interior")
    ]
    assert order == ["interior_detected", "exit_interior step", "left_interior"], order
    assert kv(logged(caplog, "interior_detected")[0])["id"] == 7
    assert kv(logged(caplog, "left_interior")[0])["interior_id"] == 7
    assert kv(logged(caplog, "left_interior")[0])["was_escaping"] is True
    assert stub._interior_token is None, "the wheel must go back on the way out"
    assert stub.wheel.owner != "exit_interior"


def test_a_rung_two_runs_when_there_is_no_last_outdoor_to_walk_back_to(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The case the fix actually exists for.

    A respawn or a character switch drops him INSIDE without an
    outdoor->indoor transition, so the bridge has no `last_outdoor` to offer
    and rung 1 has nothing to walk to. Rung 2 is the learned way out of this
    same interior — real observed data from this run (the position the game
    itself reported him at when he last came out of interior 7), never a
    curated coordinate table.
    """
    stub = _LoopStub()
    # He walked out of interior 7 once earlier in this run: that is the
    # observation rung 2 is built from.
    tick(stub, indoors(last_outdoor=None))
    stub.posted.clear()
    stub.clock.tick(1.0)
    tick(stub, make_state(interior=None, pos=(41.0, 3.0, 0.0)))
    stub.posted.clear()

    with caplog.at_level(logging.INFO, logger="wasted.main"):
        stub.clock.tick(1.0)
        tick(stub, indoors(last_outdoor=None, pos=(2.0, 1.0, 0.0)))

    assert [t for t, _ in stub.posted] == ["walk_to"], stub.posted
    params = stub.posted[0][1]
    assert (params["x"], params["y"]) == (41.0, 3.0), (
        "with no last_outdoor, the ladder walks to the place the game itself "
        "reported him leaving this interior"
    )
    assert kv(logged(caplog, "exit_interior step")[0])["step"] == 2


def test_a_giving_up_is_loud_and_hands_the_wheel_back_without_teleporting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rung (c). There is no legal teleport (CLAUDE.md rule 5)."""
    stub = _LoopStub()
    with caplog.at_level(logging.ERROR, logger="wasted.main"):
        # No `last_outdoor` and nothing learned: both rungs have no target, so
        # the ladder is exhausted on the first tick it runs.
        tick(stub, indoors(last_outdoor=None))
    # Giving up hands the wheel back and "the rest of the loop carries on" — the
    # escape's own log line says so — so an ordinary roam action afterwards is the
    # designed outcome, not a leak. What must NEVER appear is a teleport: rule 5
    # allows a few metres when wedged, not relocating through a wall.
    assert all(
        t not in ("teleport", "set_position", "unstick") for t, _ in stub.posted
    ), f"there is no legal teleport out of a building; got {stub.posted}"
    assert logged(caplog, "exit_interior GAVE UP"), "a silent give-up is the failure mode"
    assert stub._interior_token is None
    assert stub.wheel.owner != "exit_interior", "it must not sit on the wheel after giving up"


# --- ACCEPTANCE (b): free roam stands down until he is out --------------------


def test_b_roam_picks_no_goal_while_he_is_indoors_and_picks_normally_after(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mission ends while he is inside a safehouse — the reported failure.

    Free roam must not lock a goal it could not possibly walk to from a living
    room, and it must resume the moment he is out. The gate is structural: the
    escape holds the movement wheel as a reflex-class owner, and
    `_drive_activities` stands down on `wheel.taken_by_reflex()`.
    """
    stub = _LoopStub()

    # A mission is running, and it is running INDOORS. Nothing escapes a
    # mission: missions happen indoors on purpose.
    full_tick(stub, indoors(mission_active=True))
    assert stub.posted == [], "a mission must never be escaped from"
    assert stub.roam.current is None

    # The mission ends. He is still inside.
    stub.clock.tick(1.0)
    with caplog.at_level(logging.INFO, logger="wasted.main"):
        full_tick(stub, indoors(mission_active=False))
    assert logged(caplog, "interior_detected"), "the mission ended; now he is just stuck"
    assert [t for t, _ in stub.posted] == ["walk_to"]
    assert stub.roam.current is None, "free roam must not lock a goal from inside a house"

    # Several ticks of being inside: still no goal.
    for _ in range(4):
        stub.clock.tick(1.0)
        full_tick(
            stub,
            indoors(task_id="t-reflex-1", task_type="walk_to", task_status="running"),
        )
        assert stub.roam.current is None, "still indoors, still no goal"

    # Out. The block lifts, and free roam picks again promptly — but not
    # necessarily on this exact tick: RoamEngine keeps a jittered 2-12 s gap
    # between goals so the show is not a metronome. What this test pins is that
    # being outdoors REMOVES the block, which is the thing that was broken; the
    # gap itself is `test_roam`'s business.
    outdoors_now = make_state(
        interior=None,
        pos=(28.0, 0.0, 0.0),
        task_id="t-reflex-1",
        task_type="walk_to",
        task_status="done",
    )
    for _ in range(20):
        stub.clock.tick(1.0)
        full_tick(stub, outdoors_now)
        if stub.roam.current is not None:
            break
    assert stub.roam.current is not None, "free roam must resume once he is outdoors"
    assert not stub.wheel.taken_by_reflex(), "the escape must have let go of the wheel"


# --- ACCEPTANCE (c): an outdoor respawn is not an interior --------------------


def test_c_a_hospital_respawn_outdoors_never_fires_interior_detected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """He dies, the game respawns him on the pavement outside a hospital.

    `interior` is null throughout, so the escape must never engage — this is
    the false-positive class the old heuristic (a failed `enter_nearest_vehicle`
    plus 20 s of stillness, both of which a fresh respawn produces) fired on.
    """
    stub = _LoopStub()
    with caplog.at_level(logging.INFO, logger="wasted.main"):
        tick(stub, make_state(interior=None, dead=True, health=0))
        stub.clock.tick(1.0)
        # Back up, standing still outside the hospital, having just failed to
        # reach a car: the exact shape the old heuristic mistook for a house.
        for _ in range(6):
            stub.clock.tick(6.0)
            tick(
                stub,
                make_state(
                    interior=None,
                    pos=(300.0, -600.0, 43.0),
                    task_id="t-9",
                    task_type="enter_nearest_vehicle",
                    task_status="failed",
                    task_detail="timeout",
                    nearby_vehicles=[
                        {
                            "handle": 5,
                            "model": "sultan",
                            "display_name": "Sultan",
                            "class": "Sedans",
                            "distance": 22.0,
                            "driver": "empty",
                            "pos": {"x": 320.0, "y": -600.0, "z": 43.0},
                        }
                    ],
                ),
            )
    assert not logged(caplog, "interior_detected"), "outdoors is outdoors"
    assert not logged(caplog, "exit_interior step")
    assert stub._interior_token is None
    assert "walk_to" not in [t for t, _ in stub.posted], stub.posted


# --- ACCEPTANCE (d): a pre-v1.12 bridge still gets the old cover --------------


def test_d_a_pre_v1_12_bridge_does_not_crash_and_keeps_the_old_heuristic() -> None:
    """The box may still be running bridge 1.4.0 when this ships.

    A snapshot with no `interior` key at all must parse, must not engage the
    ground-truth ladder (there is no ground truth to engage on), and must leave
    `HouseEscape`'s heuristic exactly as able to fire as it was before.
    """
    from test_roam import make_state as roam_state  # no `interior` key at all

    old_bridge = roam_state(
        task_type="enter_nearest_vehicle",
        task_status="failed",
        task_detail="timeout",
        nearby_vehicles=[
            {
                "handle": 1,
                "model": "sultan",
                "display_name": "Sultan",
                "class": "Sedans",
                "distance": 22.0,
                "driver": "empty",
                "pos": {"x": 20.0, "y": 12.0, "z": 0.0},
            }
        ],
    )
    assert old_bridge.player.interior is None
    assert not old_bridge.player.interior_reported, "the key was never on the wire"

    # The v1.12 ladder has nothing to act on and says so quietly.
    escape = InteriorEscape()
    assert escape.applies(old_bridge) is False
    assert escape.check(old_bridge) is None
    assert escape.active() is False
    assert escape.observe(old_bridge) is None

    # The pre-v1.12 heuristic is untouched and still covers this bridge.
    house = HouseEscape()
    assert house.looks_indoors(old_bridge, still_for_s=25.0) is True
    assert house.check(old_bridge, still_for_s=25.0) is not None


def test_d_a_v1_12_bridge_reporting_outdoors_silences_the_old_heuristic() -> None:
    """`interior: null` is a FACT, not a missing field.

    A v1.12 bridge saying "he is outdoors" must beat the heuristic's guess —
    that guess firing on a failed vehicle entry in the open street is exactly
    the false positive the contract change removed.
    """
    outdoors = make_state(
        interior=None,
        task_type="enter_nearest_vehicle",
        task_status="failed",
        task_detail="timeout",
        nearby_vehicles=[
            {
                "handle": 1,
                "model": "sultan",
                "display_name": "Sultan",
                "class": "Sedans",
                "distance": 22.0,
                "driver": "empty",
                "pos": {"x": 20.0, "y": 12.0, "z": 0.0},
            }
        ],
    )
    assert outdoors.player.interior_reported is True
    assert HouseEscape().looks_indoors(outdoors, still_for_s=999.0) is False


# --- the ladder's own rules ---------------------------------------------------


def test_a_doorway_frame_is_not_a_trap() -> None:
    """`since_s` under the settle window is walking through a door, not living
    in a house. The complaint about the old heuristic was that it fired late;
    the answer is not to make this one fire on every threshold he crosses."""
    escape = InteriorEscape()
    assert escape.applies(indoors(interior=(7, INTERIOR_SETTLE_S - 0.5))) is False
    assert escape.applies(indoors(interior=(7, INTERIOR_SETTLE_S + 0.1))) is True


@pytest.mark.parametrize(
    ("label", "over"),
    [
        ("in a vehicle", {"in_vehicle": True}),
        ("dead", {"dead": True}),
        ("arrested", {"arrested": True}),
        ("mission active", {"mission_active": True}),
        ("cutscene", {"cutscene_active": True}),
        ("retry in flight", {"retry_in_flight": True}),
        ("protagonist switch", {"switch_in_progress": True}),
        ("controls taken", {"control_enabled": False}),
    ],
)
def test_the_escape_stands_down_when_it_could_not_or_should_not_run(
    label: str, over: dict[str, Any]
) -> None:
    assert InteriorEscape().applies(indoors(**over)) is False, label


def test_a_way_out_on_the_other_side_of_the_map_is_discarded_not_walked_to() -> None:
    """`last_outdoor` and a learned exit are both keyed on things this harness
    cannot verify the stability of (a latched transition, an InteriorProxy pool
    handle). A door he actually walked through is metres away, not kilometres."""
    escape = InteriorEscape()
    far = indoors(last_outdoor=(INTERIOR_EXIT_MAX_M + 500.0, 0.0, 0.0))
    assert escape.check(far) is None, "nothing sane to walk to, so nothing is posted"
    assert escape.active() is False, "and it gives up rather than inventing a target"


def test_every_action_the_ladder_can_produce_is_a_documented_walk() -> None:
    """No teleport, on purpose. `GET_SAFE_COORD_FOR_PED` plus a warp would work
    and is a cheat under CLAUDE.md rule 5 — the narrow unstick exception is a
    few metres when wedged, not relocating a body out of a building."""
    escape = InteriorEscape()
    seen: list[dict[str, Any]] = []
    state = indoors()
    for _ in range(8):
        action = escape.check(state)
        if action is not None:
            seen.append(action)
            escape.posted("t-1")
        escape.clock = lambda: 0.0
        state = indoors(interior=(7, INTERIOR_SETTLE_S + 2.0))
    assert seen, "the ladder produced nothing at all"
    assert {a["type"] for a in seen} == {"walk_to"}


def test_a_rung_that_never_reached_the_game_does_not_burn_its_timeout() -> None:
    """`_execute_action` has honest refusals of its own (a modal screen, a task
    type the stall detector just blocked). A rung lost to one of them must be
    asked for again, not counted as tried."""
    escape = InteriorEscape()
    state = indoors()
    first = escape.check(state)
    assert first is not None and first["params"]["x"] == 28.0
    escape.retry_step()
    again = escape.check(state)
    assert again == first, "the same rung is offered again"


def test_the_stall_detector_stands_down_while_the_escape_owns_the_timing() -> None:
    """The in-goal stuck watchdog must not `stop` the escape's own walk: from a
    higher wheel owner it would kill the rung, and the ladder would never
    finish one. The escape has its own 30 s per-rung timeouts."""
    stub = _LoopStub()
    tick(stub, indoors())
    assert stub.interior_escape.active() is True
    assert [t for t, _ in stub.posted] == ["walk_to"]
    # Twenty-plus seconds of a running walk that moves him nowhere: exactly the
    # shape TaskStallDetector abandons a task for.
    for _ in range(6):
        stub.clock.tick(5.0)
        tick(stub, indoors(task_id="t-reflex-1", task_type="walk_to", task_status="running"))
    assert "stop" not in [t for t, _ in stub.posted], stub.posted


def test_a_second_interior_is_a_new_problem_with_a_new_way_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Walking from one interior straight into another restarts the ladder: the
    way out of THIS building is not the way out of the last one."""
    stub = _LoopStub()
    with caplog.at_level(logging.INFO, logger="wasted.main"):
        tick(stub, indoors(interior=(7, 5.0)))
        stub.clock.tick(1.0)
        tick(stub, indoors(interior=(9, 5.0)))
    detections = [kv(r)["id"] for r in logged(caplog, "interior_detected")]
    assert detections == [7, 9], detections
    assert [t for t, _ in stub.posted] == ["walk_to", "walk_to"]


def test_survival_still_outranks_getting_out_of_a_house() -> None:
    """Being shot at indoors is worse than being indoors. The wheel's ladder is
    the specification and `exit_interior` sits below the survival rungs."""
    from wasted_harness.behavior.vehicle import OWNERS_BY_NAME

    assert OWNERS_BY_NAME["exit_interior"].klass == OWNERS_BY_NAME["threat"].klass
    for above in ("flip", "threat", "vehicle", "physical"):
        assert OWNERS_BY_NAME[above].rank > OWNERS_BY_NAME["exit_interior"].rank, above
    for below in ("house_escape", "stranded"):
        assert OWNERS_BY_NAME[below].rank < OWNERS_BY_NAME["exit_interior"].rank, below


def test_the_step_timeout_is_the_operators_thirty_seconds() -> None:
    assert INTERIOR_STEP_TIMEOUT_S == 30.0
