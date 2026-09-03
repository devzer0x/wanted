"""T1/T2 (docs/findings.md R1, R5): the drive-start sequence, F6, and the wheel.

WHAT WENT WRONG, from the log this file exists to make impossible again. On
2026-09-03 the bridge log carried `task t-0000NN (enter_nearest_vehicle)
started` → ~1.0 s → `failed: cleared_by_game`, **111 times in two hours**,
re-posted within 300 ms each time by whichever harness owner got the wheel
next. `/state` at the same moment said `in_vehicle: false` with an EMPTY
Prairie 2.84 m away, and a story call had been CONNECTED the whole time — the
game takes the player ped's task for the phone UI.

Three separate defects made that possible, and the tests below are grouped by
which one they guard:

* **A.** `bridge/src/TaskEngine.cs` never confirmed the DRIVER'S SEAT (only
  `ped.IsInVehicle()`, true in a passenger seat and true mid-entry), never
  called `SET_VEHICLE_ENGINE_ON`, never set a cruise speed, and never checked
  after issuing a drive task that anything had actually happened.
* **B.** Nothing measured F6 — "after the game hands the controls back,
  somebody moves him within 3 s". Every edge (respawn, cutscene end, mission
  end, protagonist switch, interior exit, `control_enabled` back) was noticed
  and reacted to; none was ever timed.
* **C.** One path in the harness put a CONTRACTS §1 movement task on the wire
  without asking the movement wheel.

ON THE C# TESTS IN SECTION A, and please read this before trusting them. They
are **source-structure guards, not behavioural verification.** Nothing on a dev
machine can call a GTA native: whether `IS_PED_IN_VEHICLE(ped, veh, false)` is
true on the frame `TASK_ENTER_VEHICLE` reports done, whether
`SET_VEHICLE_ENGINE_ON` starts a car whose driver is the agent, and what the game
actually does one second after a phone call takes the ped's task, are all
server-only facts and are listed as NOT VERIFIED in this package's report. What
these tests DO buy is real: they fail if somebody deletes the seat check, drops
the engine call, reorders the sequence so the cruise speed is set before the
task exists, or lets the re-issue cap grow past one. That is exactly the class
of regression that produced the 111 lines.
"""

from __future__ import annotations

import ast
import pathlib
import random
from typing import Any

import pytest

from tests.support.states import VEHICLE, make_state
from wasted_harness.behavior.vehicle import (
    CONTROL_REGAINED_DEADLINE_S,
    CONTROL_REGAINED_EDGES,
    CONTROL_REGAINED_FALLBACK_S,
    MOVEMENT_OWNER_TABLE,
    NON_MOVING_TASKS,
    OWNERS_BY_NAME,
    ControlRegained,
    MovementWheel,
    resume_action,
)
from wasted_harness.brain.schemas import MOVEMENT_TASKS, PHONE_TASKS

REPO = pathlib.Path(__file__).resolve().parents[2]
TASK_ENGINE = REPO / "bridge" / "src" / "TaskEngine.cs"
BRIDGE_ROUTER = REPO / "bridge" / "src" / "BridgeRouter.cs"
MAIN_PY = pathlib.Path(__file__).resolve().parents[1] / "wasted_harness" / "main.py"


class FakeClock:
    """A clock a test drives by hand, so a 3 s deadline costs no wall time."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


# --- A. the bridge drive-start sequence --------------------------------------


def csharp_method(source: str, signature_fragment: str) -> str:
    """The body of the first method whose signature contains the fragment.

    Brace-matched rather than regex'd, so a nested block or a string literal
    with a brace in it cannot truncate the body and make a missing call look
    present (or vice versa).
    """
    start = source.index(signature_fragment)
    open_brace = source.index("{", start)
    depth = 0
    for i in range(open_brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : i + 1]
    raise AssertionError(f"unbalanced braces after {signature_fragment!r}")


#: Each drive-issuing method and the exact native CALL inside it. The argument
#: text is part of the key on purpose: `IssueWanderDrive`'s own doc comment
#: quotes `ped.Task.CruiseWithVehicle(veh, 13 m/s, style)` as the thing that
#: went wrong, and a bare method name would match the prose before the code.
DRIVE_ISSUERS: dict[str, str] = {
    "private void IssueDriveTo(": "ped.Task.DriveTo(veh,",
    "private void IssueWanderDrive(": "ped.Task.CruiseWithVehicle(veh, WanderCruiseSpeedMps",
    "private void IssueFollowVehicleMission(": "ped.Task.StartVehicleMission(veh,",
}


@pytest.fixture(scope="module")
def task_engine() -> str:
    return TASK_ENGINE.read_text(encoding="utf-8")


def test_a1_the_seat_check_is_both_halves_the_ticket_asks_for(task_engine: str) -> None:
    """ACCEPTANCE (1), half one: seat confirmation exists and is BOTH reads.

    `ped.IsInVehicle()` was the whole check before this. It is true in a
    passenger seat and true while the ped is still climbing in — which is how a
    drive task got issued at a man 2.84 m from the car.
    """
    body = csharp_method(task_engine, "private static bool InDriversSeat(")
    assert "Hash.IS_PED_IN_VEHICLE" in body, "the seat check lost IS_PED_IN_VEHICLE"
    # atGetIn MUST be false: the wrapper Ped.IsInVehicle(Vehicle) is documented
    # in the pinned lib/Docs/ScriptHookVDotNet3.xml as "sitting in OR GETTING
    # OUT", which is the opposite of what this check needs.
    assert "ped.Handle, veh.Handle, false" in body, "atGetIn must be passed explicitly false"
    # GET_PED_IN_VEHICLE_SEAT(veh, -1) == ped, through the wrapper whose body was
    # read from SHVDN source during research; VehicleSeat.Driver is -1 in the
    # pinned DLL.
    assert "GetPedOnSeat(VehicleSeat.Driver)" in body
    assert "driver.Handle == ped.Handle" in body


def test_a2_no_drive_task_is_issued_without_the_seat(task_engine: str) -> None:
    """ACCEPTANCE (1): seat not confirmed → no drive posted, and the task fails
    with a reason rather than sitting on `running` forever."""
    prepare = csharp_method(task_engine, "private bool PrepareToDrive(")
    assert "InDriversSeat(ped, veh)" in prepare
    assert 'Fail("not_in_drivers_seat")' in prepare
    # ...and the failure short-circuits, so nothing below it can issue a task.
    assert prepare.index('Fail("not_in_drivers_seat")') < prepare.index("SET_VEHICLE_ENGINE_ON")

    for issuer in (
        "private void IssueDriveTo(",
        "private void IssueWanderDrive(",
        "private void IssueFollowVehicleMission(",
    ):
        body = csharp_method(task_engine, issuer)
        assert "PrepareToDrive(ped, veh)" in body, f"{issuer} does not confirm the seat"
        assert "return;" in body[: body.index("PrepareToDrive")] + body, issuer


def test_a3_the_engine_is_started_before_the_drive_task(task_engine: str) -> None:
    """ACCEPTANCE (2): engine off → engine on BEFORE the drive.

    Ordering is the whole assertion. `SET_VEHICLE_ENGINE_ON` lives in
    `PrepareToDrive`, and `PrepareToDrive` is called before the task-issuing
    native in all three issuers — so a car whose engine the game left off gets
    the key turned first, rather than a drive order it silently ignores.
    """
    prepare = csharp_method(task_engine, "private bool PrepareToDrive(")
    # value: true, instantly: true, disableAutoStart: false — the raw native, so
    # all four arguments are named here rather than hidden in a wrapper.
    assert "Hash.SET_VEHICLE_ENGINE_ON, veh.Handle, true, true, false" in prepare

    for issuer, native in DRIVE_ISSUERS.items():
        body = csharp_method(task_engine, issuer)
        assert body.index("PrepareToDrive") < body.index(native), (
            f"{issuer} issues {native} before the seat/engine sequence"
        )


def test_a4_the_cruise_speed_is_set_after_the_task_exists(task_engine: str) -> None:
    """SET_DRIVE_TASK_CRUISE_SPEED, and the ORDER the pinned XML requires.

    `Ped.DrivingSpeed`'s own documentation in lib/Docs/ScriptHookVDotNet3.xml:
    "the drive task running on this Ped must be active before setting the value
    can actually affect". So it goes after the task-issuing native, not before —
    the same rule the pre-existing driver-competence call already followed.
    """
    tuning = csharp_method(task_engine, "private static void ApplyDriveTuning(")
    assert "ped.DrivingSpeed = cruiseSpeedMps" in tuning
    assert "Hash.SET_DRIVER_ABILITY" in tuning

    for issuer, native in DRIVE_ISSUERS.items():
        body = csharp_method(task_engine, issuer)
        assert body.index(native) < body.index("ApplyDriveTuning("), (
            f"{issuer} tunes the drive task before the task exists"
        )
        assert "ArmDriveVerification()" in body, f"{issuer} is never graded"


def test_a5_one_re_issue_then_a_reasoned_failure_never_a_third(task_engine: str) -> None:
    """ACCEPTANCE (3): cleared 1 s in → ONE re-post, then a failure. Never a third.

    The cap is structural, not a comment: `_driveStarts` is incremented in
    exactly one place, both failure paths refuse to retry once it has reached
    `MaxDriveStarts`, and `MaxDriveStarts` is 1.
    """
    assert "private const int MaxDriveStarts = 1;" in task_engine

    increments = task_engine.count("_driveStarts++")
    assert increments == 1, (
        f"_driveStarts is incremented in {increments} places; the cap is only "
        "meaningful if RestartDrive is the single door"
    )
    restart = csharp_method(task_engine, "private void RestartDrive(")
    assert "_driveStarts++" in restart
    assert "ReissueVehicleTask(ped, veh)" in restart

    # Path 1: the game cleared the task (the measured `cleared_by_game` storm).
    liveness = csharp_method(task_engine, "private void CheckLiveness(")
    assert "_driveStarts >= MaxDriveStarts" in liveness
    assert 'Fail("cleared_by_game")' in liveness
    assert "RestartDrive(ped, veh)" in liveness
    # Path 2: the task is alive and the wheels never turned.
    verify = csharp_method(task_engine, "private void UpdateDriveStart(")
    assert "_driveStarts < MaxDriveStarts" in verify
    assert 'Fail("drive_did_not_start")' in verify
    assert "RestartDrive(ped, veh)" in verify


def test_a6_the_verification_grades_task_liveness_and_real_motion(task_engine: str) -> None:
    """"It started" means the script task is live AND the car is moving.

    Either alone is a lie: a live task in a car that never moved is the exact
    2026-09-02 "sat in the convertible" failure, and a rolling car under a dead
    task is somebody else's push.
    """
    verify = csharp_method(task_engine, "private void UpdateDriveStart(")
    assert "ScriptTaskIsLive(ped)" in verify
    assert "veh.Speed > 0f" in verify
    assert "DriveStartVerifyMs" in verify

    live = csharp_method(task_engine, "private bool ScriptTaskIsLive(")
    # Vacant is what ScriptTaskNameHash.Invalid resolves to (pinned XML), and
    # Finished is over. WaitingToStart and Dormant are legal live states.
    assert "ScriptTaskStatus.Vacant" in live
    assert "ScriptTaskStatus.Finished" in live


def test_a7_cleared_by_game_keeps_the_detail_the_harness_matches_on(task_engine: str) -> None:
    """`cleared_by_game` must surface as EXACTLY that, once.

    `behavior/recovery.py`'s ClearedByGameBackoff compares
    `(task.detail or "") != "cleared_by_game"` — an exact match. Renaming the
    detail for drive tasks would silently disarm the harness-side backoff, which
    is the other half of this same fix, so the two failure modes keep separate
    details: `cleared_by_game` when the game took the task, and
    `drive_did_not_start` when the task was alive and nothing moved.
    """
    assert task_engine.count('Fail("cleared_by_game")') >= 1
    assert 'Fail("drive_did_not_start")' in task_engine


def test_a8_every_movement_step_has_a_no_progress_watchdog(task_engine: str) -> None:
    """10 s without moving → escalate once → fail the step with a reason.

    walk_to's own timeout is five minutes; that is not a watchdog, that is a
    roam goal dying of old age around a ped stood against a wall.
    """
    assert "private const int NoProgressWindowMs = 10000;" in task_engine
    watched = csharp_method(task_engine, "private bool IsProgressWatchedTask()")
    for step in ("walk_to", "enter_nearest_vehicle", "flee_ped", "flee_police",
                 "drive_to", "wander_drive"):
        assert f'"{step}"' in watched, f"{step} is not under the no-progress watchdog"

    progress = csharp_method(task_engine, "private void UpdateProgress(")
    assert "_progressEscalations == 0" in progress
    assert "ReissueMovementTask(ped)" in progress
    assert 'Fail("no_progress")' in progress
    # A red light is not a stall: a vehicle task only becomes eligible once the
    # engine's own jam ladder has spent its attempt budget.
    assert "_stuckAttempts < MaxStuckAttemptsPerEpisode" in progress


def test_a9_the_on_foot_equivalents_are_wired(task_engine: str) -> None:
    """`run_to` and `flee`, expressed the way the ticket describes them.

    `run_to` did not need a new task name: CONTRACTS §1 already gives `walk_to`
    a frozen `run` flag, and what that flag MEANS bridge-side (the move-blend
    ratio) is this file's business in exactly the way the driving-style bit
    values are. `flee` did need one, and ships here as `flee_ped` — see the
    report for the harness change and the CONTRACTS entry it still needs.
    """
    walk = csharp_method(task_engine, "private void IssueWalkTo(")
    assert "Hash.SET_PED_MOVE_RATE_OVERRIDE, ped.Handle, 1f" in walk, (
        "the move-rate override must be RESTORED to the game's own 1.0 default"
    )
    assert "PedMoveBlendRatio.Sprint" in walk, "run: true must be the 3.0 blend ratio"
    assert "ScriptTaskNameHash.FollowNavMeshToCoord" in walk

    flee = csharp_method(task_engine, "private void StartFleePed(")
    assert "ped.Task.FleeFrom(target, FleeSafeDistanceM, -1)" in flee
    assert "ScriptTaskNameHash.SmartFleePed" in flee, (
        "the PED overload polls as SmartFleePed, not flee_police's SmartFleePoint"
    )


def test_a10_the_move_rate_override_is_never_a_speed_cheat(task_engine: str) -> None:
    """CLAUDE.md rule 5. Every SET_PED_MOVE_RATE_OVERRIDE in the bridge passes
    exactly 1.0 — the game's own default — so this restores a normal walking
    speed and can never grant one a human could not reach."""
    calls = [
        line.strip()
        for line in task_engine.splitlines()
        if "SET_PED_MOVE_RATE_OVERRIDE" in line and "Function.Call" in line
    ]
    assert calls, "the move-rate restore disappeared"
    for line in calls:
        assert line.endswith("ped.Handle, 1f);"), f"move rate is not 1.0: {line}"


def test_a11_flee_ped_is_validated_like_every_other_handle_task() -> None:
    """The router half. Verified for real against the compiled BridgeRouter in
    this package's report (202 with a handle, 400 without); this is the source
    guard that keeps it wired."""
    router = BRIDGE_ROUTER.read_text(encoding="utf-8")
    body = csharp_method(router, 'case "flee_ped":')
    assert 'TryInt(p, "handle", out req.Handle)' in body
    assert "flee_ped requires integer handle" in body


# --- B. F6: control came back, and somebody moved him ------------------------


def test_b1_every_documented_edge_arms_the_stopwatch() -> None:
    """ACCEPTANCE (4), part one: all six control-regained edges are detected."""
    clock = FakeClock()
    seen: list[str] = []

    def fire(before: dict[str, Any], after: dict[str, Any], **feed: Any) -> str | None:
        watch = ControlRegained(clock=clock)
        watch.feed(make_state(**before))
        return watch.feed(make_state(**after), **feed)

    seen.append(fire({}, {}, respawned=True) or "")
    seen.append(fire({"interior": (7, 5.0)}, {}) or "")
    seen.append(fire({"cutscene_active": True}, {}) or "")
    seen.append(fire({"mission_active": True}, {}) or "")
    seen.append(fire({"switch_in_progress": True}, {}) or "")
    seen.append(fire({"control_enabled": False}, {}) or "")

    assert seen == list(CONTROL_REGAINED_EDGES), seen


def test_b2_a_quiet_tick_is_not_an_edge() -> None:
    """The stopwatch must not arm itself out of a field that simply has not
    been read before, or every session would open with a false F6 miss."""
    clock = FakeClock()
    watch = ControlRegained(clock=clock)
    for _ in range(10):
        assert watch.feed(make_state()) is None
    clock.tick(60.0)
    assert watch.overdue() is None
    assert watch.pending is None


def test_b3_movement_inside_the_window_answers_the_edge() -> None:
    """ACCEPTANCE (4), the passing half: somebody moved him, so no fallback."""
    clock = FakeClock()
    watch = ControlRegained(clock=clock)
    watch.feed(make_state(cutscene_active=True))
    assert watch.feed(make_state()) == "cutscene_end"

    clock.tick(CONTROL_REGAINED_FALLBACK_S - 0.1)
    assert watch.overdue() is None, "still inside the fallback window"
    watch.moved("enter_nearest_vehicle")
    clock.tick(600.0)
    assert watch.overdue() is None, "answered edges never come back"
    assert watch.pending is None


def test_b4_nothing_moving_him_fires_the_fallback_exactly_once() -> None:
    """ACCEPTANCE (4), the failing half — and it reports ONCE.

    A rung that re-fires at the poll rate is the storm this whole package is
    about; `overdue()` answers a given arming a single time.
    """
    clock = FakeClock()
    watch = ControlRegained(clock=clock)
    watch.feed(make_state(dead=True))
    assert watch.feed(make_state(), respawned=True) == "respawn"

    clock.tick(CONTROL_REGAINED_FALLBACK_S + 0.01)
    assert watch.overdue() == "respawn"
    for _ in range(50):
        clock.tick(1.0)
        assert watch.overdue() is None, "the F6 miss is reported once, not at 3 Hz"


def test_b5_a_second_edge_restarts_the_clock() -> None:
    """A cutscene that ends into a mission that ends is TWO moments a viewer
    starts waiting, and the deadline is measured from the later one."""
    clock = FakeClock()
    watch = ControlRegained(clock=clock)
    watch.feed(make_state(cutscene_active=True))
    assert watch.feed(make_state(mission_active=True)) == "cutscene_end"
    clock.tick(2.5)
    assert watch.feed(make_state()) == "mission_end"
    clock.tick(CONTROL_REGAINED_FALLBACK_S - 0.1)
    assert watch.overdue() is None, (
        "4.4 s since the first edge, 1.9 s since the last: the deadline is "
        "measured from the moment a viewer most recently started waiting"
    )
    clock.tick(0.2)
    assert watch.overdue() == "mission_end"


def test_b6_a_stop_is_not_movement() -> None:
    """`stop` and `set_waypoint` belong to the wheel (they preempt) but move
    nobody. Letting one answer F6 would make the measurement agree that a man
    standing still is moving."""
    assert set(MOVEMENT_TASKS) >= NON_MOVING_TASKS
    clock = FakeClock()
    watch = ControlRegained(clock=clock)
    watch.feed(make_state(cutscene_active=True))
    watch.feed(make_state())
    for task in sorted(NON_MOVING_TASKS):
        watch.moved(task)
    clock.tick(CONTROL_REGAINED_FALLBACK_S + 0.01)
    assert watch.overdue() == "cutscene_end"


def test_b7_the_fallback_is_the_cheapest_honest_thing_that_moves_him() -> None:
    """What `resume` actually posts, in each of the three worlds it can wake in."""
    seated = make_state(in_vehicle=True)
    assert resume_action(seated, "rushed") == {
        "type": "wander_drive",
        "params": {"style": "rushed"},
    }

    on_foot = resume_action(make_state(), "normal")
    assert on_foot is not None and on_foot["type"] == "enter_nearest_vehicle"
    assert on_foot["params"]["prefer"] == "any"

    # Indoors: widening a vehicle search from a living room picks a car behind
    # MORE walls. `exit_interior` outranks this rung and owns walking him out.
    assert resume_action(make_state(interior=(7, 5.0)), "normal") is None
    # A car that cannot go anywhere: `flip` owns getting him out of it.
    upside_down = dict(VEHICLE, upside_down=True)
    assert resume_action(make_state(in_vehicle=True, vehicle=upside_down), "normal") is None
    sunk = dict(VEHICLE, in_water=True)
    assert resume_action(make_state(in_vehicle=True, vehicle=sunk), "normal") is None


def test_b8_resume_is_the_last_rung_and_yields_to_every_real_reason() -> None:
    """The ladder is the specification. `resume` fires only when nothing above
    it wanted the wheel, and it still outranks every planner — which are
    precisely the layers that just failed to move him."""
    resume = OWNERS_BY_NAME["resume"]
    reflexes = [o for o in MOVEMENT_OWNER_TABLE if o.klass == resume.klass]
    assert min(o.rank for o in reflexes) == resume.rank, "resume must be the last reflex"
    for other in reflexes:
        if other.name != "resume":
            assert other.rank > resume.rank, other.name
    planners = [o for o in MOVEMENT_OWNER_TABLE if o.klass < resume.klass]
    assert planners, "the ladder lost its planner classes"
    for planner in planners:
        assert (resume.klass, resume.rank) > (planner.klass, planner.rank), planner.name


# --- C. T2: the wheel is the ONLY path to movement ---------------------------


def _execute_action_calls() -> list[ast.Call]:
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_execute_action"
    ]


def test_c1_nothing_reaches_the_bridge_except_through_the_choke_point() -> None:
    """T2 audit: `bridge.post_task` is called from ONE place in `main.py`.

    This is the test that would have caught the hole this package closed:
    `_handle_breaks` used to call `self.bridge.post_task("stop", {})` directly.
    `stop` is a movement task — it clears whatever the current holder had
    running — so a break starting mid-goal cancelled a roam step out from under
    an owner that never found out.
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "post_task"
                and fn.name != "_execute_action"
            ):
                offenders.append(f"{fn.name}:{node.lineno}")
    assert offenders == [], (
        "POST /task must go through _execute_action, which is where the "
        f"movement gate lives; found {offenders}"
    )


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Line number → name of the innermost function containing it."""
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            line = getattr(node, "lineno", None)
            if line is None:
                continue
            # Innermost wins: a later, more deeply nested function overwrites.
            if line not in owner or len(fn.name) > len(owner[line]):
                owner[line] = fn.name
    return owner


#: The token-free `_execute_action` call sites that are legitimate, and WHY.
#: Every one of them passes a COMPUTED action type, so the audit below cannot
#: read the type off the AST — it proves the guard instead. Line numbers are
#: deliberately not used: three other executors are editing `main.py`.
TOKEN_FREE_CALL_SITES: dict[str, str] = {
    # Guarded by an explicit `if ... not in MOVEMENT_TASKS` immediately above.
    "_apply_decision": "MOVEMENT_TASKS",
    "_reflex_act": "MOVEMENT_TASKS",
    # Guarded by `not in BRIDGE_TASKS`, a strict superset of MOVEMENT_TASKS.
    "_issue_activity_step": "BRIDGE_TASKS",
    # NOT guarded syntactically. `IdlePicker` yields only §2 primitives
    # (look_around / radio / wait / horn), and the runtime gate inside
    # `_execute_action` refuses a movement task without the holder's token —
    # loudly, at ERROR — so a picker that ever grew a movement behaviour would
    # be caught rather than silently posting over the wheel. See test_c3.
    "run": None,
}


def test_c2_every_execute_action_call_site_carries_a_token_or_cannot_move_him() -> None:
    """T2 audit: prove there is no path that posts a movement task without a token.

    Every `self._execute_action(...)` in `main.py` either passes the movement
    wheel's token as its third argument, or names a literal action type that is
    not a movement task, or lives in one of the four functions listed in
    :data:`TOKEN_FREE_CALL_SITES` — each of which either guards on
    `not in MOVEMENT_TASKS`/`not in BRIDGE_TASKS` right above the call, or is
    documented above as relying on the runtime gate.

    A NEW token-free call site anywhere else fails this test by name.
    """
    source = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    bodies = {
        fn.name: ast.get_source_segment(source, fn) or ""
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    unguarded: list[str] = []
    for call in _execute_action_calls():
        if len(call.args) >= 3:
            continue  # carries the token
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value in MOVEMENT_TASKS:
                unguarded.append(f"line {call.lineno}: literal {first.value!r}, no token")
            continue  # a literal primitive or phone task: moves nobody
        where = owner.get(call.lineno, "<unknown>")
        if where not in TOKEN_FREE_CALL_SITES:
            unguarded.append(f"line {call.lineno}: computed type in {where}(), no token")
            continue
        guard = TOKEN_FREE_CALL_SITES[where]
        if guard is not None and f"not in {guard}" not in bodies.get(where, ""):
            unguarded.append(f"{where}() lost its `not in {guard}` guard")

    assert unguarded == [], (
        "a movement task can reach the bridge without the wheel: " + "; ".join(unguarded)
    )


def test_c2b_the_f6_stopwatch_is_wired_to_the_one_line_that_posts() -> None:
    """F6 is only a measurement if it is measured in the right place.

    `moved()` must be called inside `_execute_action`, after the POST, and the
    `resume` rung must be driven by `overdue()` inside `_reflex`. Wiring it
    anywhere else (on the acquire, on the attempt) would report a task lost to
    a bridge blip as movement — the exact mistake `VehicleController`'s motion
    verification exists to refuse.
    """
    source = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    bodies = {
        fn.name: ast.get_source_segment(source, fn) or ""
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    execute = bodies["_execute_action"]
    assert "self.control_regained.moved(action_type)" in execute
    assert execute.index("self.bridge.post_task(") < execute.index(
        "self.control_regained.moved("
    ), "F6 must be answered by a task that actually landed, not by the attempt"

    reflex = bodies["_reflex"]
    assert "self.control_regained.feed(state" in reflex
    assert "self.control_regained.overdue()" in reflex
    assert 'self._reflex_act(\n' in reflex or '"resume"' in reflex
    assert "resume_action(state" in reflex


def test_c3_the_gate_itself_refuses_a_movement_task_without_the_holder() -> None:
    """The runtime half of C1/C2, exercised through the real gate.

    `PHONE_TASKS` are exempt by design (CONTRACTS v1.13: they inject one phone
    control per frame and move nobody), which is why the gate classifies on
    MOVEMENT_TASKS rather than on BRIDGE_TASKS.
    """
    assert set(PHONE_TASKS).isdisjoint(MOVEMENT_TASKS)
    wheel = MovementWheel()
    wheel.begin_tick()
    assert wheel.holds(None) is False
    token = wheel.acquire("roam", "a goal step")
    assert token is not None
    assert wheel.holds(token) is True
    wheel.acquire("threat", "being shot at")
    assert wheel.holds(token) is False, "a preempted token must stop working"


def test_c4_two_hours_of_ticks_never_produce_two_movement_owners_in_one() -> None:
    """ACCEPTANCE (5): over a 2 h synthetic replay, no tick has two movement owners.

    Two hours at the harness's 3 Hz poll is 21600 ticks. Every owner in the
    table asks on every tick, in a randomised order — deliberately NOT the
    production order, because the guarantee has to hold structurally rather
    than because the tick happens to reach the layers in a helpful sequence.
    Whoever is granted posts. The assertion is that a tick never carries a post
    from two different owners.
    """
    rng = random.Random(20260903)
    wheel = MovementWheel()
    names = [o.name for o in MOVEMENT_OWNER_TABLE]
    posts_per_tick: dict[int, set[str]] = {}
    granted = 0

    for tick in range(21600):
        wheel.begin_tick()
        if rng.random() < 0.02:
            # The two overrides that bypass the ladder entirely: a mission
            # starting takes the wheel unconditionally, and a cutscene/death
            # forces it to idle. Both must respect the one-post-per-tick latch.
            if rng.random() < 0.5:
                wheel.mission_active(True)
            else:
                wheel.force_idle("cutscene")
        askers = names[:]
        rng.shuffle(askers)
        for owner in askers:
            if rng.random() > 0.35:
                continue
            lease = None if rng.random() < 0.2 else 1
            token = wheel.acquire(owner, "synthetic", lease_ticks=lease)
            if token is None:
                continue
            granted += 1
            if rng.random() < 0.6:
                wheel.mark_posted(token, "wander_drive")
                posts_per_tick.setdefault(tick, set()).add(owner)
            if rng.random() < 0.1:
                wheel.release(token)
        wheel.end_tick()
        if rng.random() < 0.01:
            wheel.mission_active(False)

    assert granted > 5000, f"the replay barely exercised the wheel ({granted} grants)"
    assert posts_per_tick, "the replay never posted anything"
    worst = max(posts_per_tick.values(), key=len)
    assert len(worst) == 1, f"a tick carried movement from two owners: {worst}"
    assert max(len(v) for v in posts_per_tick.values()) == 1


def test_b9_the_fallback_fires_inside_the_bar_it_is_protecting() -> None:
    """The number that made 16 synthetic edges read as missed.

    `tools/funcheck.py` counts an edge answered only when a movement task is
    posted in the CLOSED window `[edge, edge + 3s]`. A fallback armed at
    exactly 3.0 s therefore posts late by construction — it is checked on a
    poll tick, so it lands at 3.0 s plus up to half a second of poll interval,
    plus the wheel arbitration and the HTTP round trip. Firing at 2.0 s leaves
    a full second of margin and still gives every ordinary owner six-to-eight
    ticks to claim the wheel on its own terms first.
    """
    assert CONTROL_REGAINED_FALLBACK_S < CONTROL_REGAINED_DEADLINE_S
    # A whole second of margin, not a rounding error's worth.
    assert CONTROL_REGAINED_DEADLINE_S - CONTROL_REGAINED_FALLBACK_S >= 1.0
    assert ControlRegained().deadline_s == CONTROL_REGAINED_FALLBACK_S
