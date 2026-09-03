"""The vehicle-entry state machine, the drive-away reflex and the priority reversal.

These are the pass criteria from the brief this work package comes from, written
as tests rather than as prose:

* **57** — he steals a car, reaches the driver's seat, and BEGINS MOVING inside
  the watchdog window with no model call; the same failed action cannot repeat
  endlessly.
* **58** — a mission tells him to drive, he is seated, he moves; "seated + drive
  objective + stationary + no cutscene/wait" is not a state that persists.
* **59** — a ped attacks him ON FOOT and he fights back, at reflex speed rather
  than waiting on commentary.
* **49** — a ped attacks him while he is IN A WORKING CAR and he drives away
  instead of switching to combat.

Plus the guards: cutscene / locked controls / a legitimate wait still suppress
the drive-away; motion verification FAILS an action the game did not act on; and
every rung of the recovery ladder is rate-limited.

Game states here are constructed pydantic objects built by explicit builders
from the documented `/state` shape (CONTRACTS §1) — `tests/fixtures/` is
reserved for real recordings of real sessions and stays empty by design.

`_ReflexStub`, `make_state` and `_reflex` are imported from
test_mission_following rather than copied: that stub wires the REAL, unmodified
`Harness._reflex` to real behaviour objects, and a second copy of it would drift
from the first the first time production wiring changed.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from test_mission_following import (  # the real _reflex wiring, not a re-description
    FakeClock,
    _PrimitivesRecorder,
    _reflex,
    _ReflexStub,
    make_state,
)

from wasted_harness.behavior.recovery import HOSTILE_CLOSE_RADIUS_M, threat_action
from wasted_harness.behavior.vehicle import (
    DRIVE_AWAY_HOLD_S,
    RECOVERY_LADDER,
    RECOVERY_RUNG_GAP_S,
    SEATED_GRACE_S,
    VEHICLE_NOT_MOVING,
    VEHICLE_START_WATCHDOG_S,
    WRITE_OFF_COOLDOWN_S,
    MovementWheel,
    VehicleController,
    VehiclePhase,
    vehicle_can_drive_away,
    vehicle_is_moving,
)
from wasted_harness.bridge_client import GameState
from wasted_harness.perception import Delta

NO_DANGER = Delta(wanted_from=0, wanted_to=0)

#: A civilian convertible, healthy, right way up. The car from the screenshots.
CONVERTIBLE: dict[str, Any] = {
    "handle": 77,
    "model": "surano",
    "display_name": "Surano",
    "class": "Sports",
    "speed": 0.0,
    "health": 950.0,
    "upside_down": False,
    "in_water": False,
    "stopped_for_s": 0.0,
}


def seated(**over: Any) -> GameState:
    """In the driver's seat of the convertible, stationary unless told otherwise."""
    speed = over.pop("speed", 0.0)
    stopped_for_s = over.pop("stopped_for_s", 3.0 if speed <= 0.2 else 0.0)
    vehicle = {**CONVERTIBLE, "speed": speed, "stopped_for_s": stopped_for_s}
    vehicle.update(over.pop("vehicle", {}))
    return make_state(in_vehicle=True, vehicle=vehicle, **over)


def on_foot(**over: Any) -> GameState:
    return make_state(in_vehicle=False, **over)


def owner(distance: float = 1.5, relationship: str = "neutral") -> dict[str, Any]:
    """The man whose car he just took. `neutral` on purpose: that is what a
    random attacker really looks like in `nearby.peds` while the punches land —
    `relationship` is the engine's relationship GROUP, not "is hitting me"."""
    return {"handle": 9, "model": "a_m_y_business_01", "distance": distance,
            "relationship": relationship}


class _NoBrain:
    """Tripwire: any touch of the model layer inside a reflex tick is a failure.

    The whole point of the drive-away living in the reflex layer is that a
    decision costs 1-2 s and real money, and the man in the screenshots did not
    have 1-2 s. So the tests that claim "no model call" enforce it rather than
    asserting it in a docstring.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"the reflex layer touched the brain ({name})")

    def __call__(self, *a: Any, **k: Any):
        raise AssertionError("the reflex layer made a model call")


def stub(**over: Any) -> _ReflexStub:
    s = _ReflexStub(**over)
    s.anthropic = _NoBrain()
    s._think = _NoBrain()
    s.tactical_cadence = _NoBrain()
    s.director_cadence = _NoBrain()
    return s


# --- the state machine itself -------------------------------------------------


def test_the_phases_follow_the_fields_state_actually_has() -> None:
    clock = FakeClock()
    vc = VehicleController(clock=clock)

    assert vc.feed(on_foot()) is None
    assert vc.phase is VehiclePhase.ON_FOOT

    vc.feed(on_foot(task_type="enter_nearest_vehicle", task_status="running"))
    assert vc.phase is VehiclePhase.ENTERING

    vc.feed(seated(task_type="enter_nearest_vehicle", task_status="done"))
    assert vc.phase is VehiclePhase.SEATED

    vc.feed(seated(speed=6.0, task_type="wander_drive", task_status="running"))
    assert vc.phase is VehiclePhase.MOVING

    # Slowing for a junction is still MOVING: the hysteresis band between
    # SEATED_SPEED_MPS and MOVING_SPEED_MPS is what stops every timer in the
    # file from re-arming on alternate ticks.
    vc.feed(seated(speed=1.0, task_type="wander_drive", task_status="running"))
    assert vc.phase is VehiclePhase.MOVING

    vc.feed(on_foot(task_type="exit_vehicle", task_status="done"))
    assert vc.phase is VehiclePhase.ON_FOOT


def test_moving_is_read_from_both_signals_the_snapshot_carries() -> None:
    assert vehicle_is_moving(seated(speed=6.0)) is True
    assert vehicle_is_moving(seated(speed=0.0, stopped_for_s=12.0)) is False
    # `stopped_for_s == 0` is the GAME's own statement that the wheels are
    # turning (the bridge resets it above 0.2 m/s), computed on the game thread
    # rather than sampled at 3 Hz.
    assert vehicle_is_moving(seated(speed=0.9, stopped_for_s=0.0)) is True
    assert vehicle_is_moving(on_foot()) is False


def test_drivability_is_only_what_the_contract_really_exposes() -> None:
    assert vehicle_can_drive_away(seated()) is True
    assert vehicle_can_drive_away(seated(vehicle={"upside_down": True})) is False
    assert vehicle_can_drive_away(seated(vehicle={"in_water": True})) is False
    assert vehicle_can_drive_away(seated(vehicle={"health": 10.0})) is False
    assert vehicle_can_drive_away(on_foot()) is False


def test_a_new_car_starts_a_clean_machine() -> None:
    """Handles are ephemeral by contract, so every per-car timer is keyed on
    `vehicle.handle`: swapping cars must not inherit the last one's stopwatch."""
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    vc.feed(seated())
    clock.tick(SEATED_GRACE_S + 1.0)
    assert vc.feed(seated()) is not None  # first car: drive away

    other = {**CONVERTIBLE, "handle": 78}
    assert vc.feed(seated(vehicle=other)) is None, "a fresh car gets a fresh grace window"
    clock.tick(SEATED_GRACE_S + 0.1)
    assert vc.feed(seated(vehicle=other)) is not None


# --- 57: he steals a car and MOVES, with no model call ------------------------


def test_57_a_stolen_car_starts_moving_within_the_watchdog_and_without_a_brain() -> None:
    """The observed death, from the other end. He reaches the driver's seat of a
    car he has just taken; nobody else claims the wheel; he pulls away."""
    s = stub()
    clock = s.clock
    just_seated = seated(task_type="enter_nearest_vehicle", task_status="done", task_id="t-jack")

    _reflex(s, just_seated)
    assert s.posted == [], "the ordinary layers get first refusal inside the grace window"
    assert s.vehicle.phase is VehiclePhase.SEATED

    # Four more ticks at ~2.5 Hz. Nobody has posted anything; he is still sat in
    # the convertible with the owner walking round the back of it.
    for _ in range(4):
        clock.tick(0.4)
        _reflex(s, just_seated)

    assert [t for t, _ in s.posted] == ["wander_drive"], (
        "seated, stationary, nothing running, nobody holding him: he must drive off"
    )
    assert s.wheel.owner == "vehicle"
    assert s._threat_has_the_wheel is True, (
        "the planners must stand down for the vehicle reflex the same way they do "
        "for survival — their own last_task check cannot see this tick's POST"
    )

    # And the game answers: the next snapshots show the car under way.
    clock.tick(0.4)
    _reflex(s, seated(speed=7.5, task_type="wander_drive", task_status="running"))
    assert s.vehicle.phase is VehiclePhase.MOVING
    assert [t for t, _ in s.posted] == ["wander_drive"], "nothing further is posted once he is moving"


def test_57_the_drive_away_cannot_repeat_endlessly() -> None:
    """Every POST preempts the running task (CONTRACTS §1), so a drive-away
    re-issued at the poll rate would restart the engine's wander task three
    times a second and he would never actually pull away — the same stutter
    ThreatLatch was built to stop."""
    s = stub()
    clock = s.clock
    parked = seated(task_status="idle", task_id=None, task_type=None)
    for _ in range(40):  # 16 s at 2.5 Hz, all of it stationary and idle
        clock.tick(0.4)
        _reflex(s, parked)
    posts = [t for t, _ in s.posted]
    assert posts == ["wander_drive"] * len(posts)
    assert len(posts) <= int(16.0 / DRIVE_AWAY_HOLD_S) + 1, (
        f"one post per {DRIVE_AWAY_HOLD_S}s at the very most, got {len(posts)}"
    )


# --- 58: a mission drive is never a resting state -----------------------------


def test_58_a_mission_drive_that_never_moves_does_not_persist() -> None:
    """"Seated + drive objective + stationary + no cutscene/wait" must not be a
    state he can sit in. The mission layer owns WHERE to drive; this owns
    noticing that the drive it posted did not move anything."""
    s = stub()
    clock = s.clock
    driving = seated(
        mission_active=True, task_type="drive_to", task_status="running", task_id="t-obj"
    )
    for _ in range(int(VEHICLE_START_WATCHDOG_S / 0.4) + 3):
        clock.tick(0.4)
        _reflex(s, driving)

    assert s.vehicle.phase is VehiclePhase.BLOCKED
    assert s.vehicle.last_signal == VEHICLE_NOT_MOVING
    assert [t for t, _ in s.primitives.executed] == ["reverse_out"], (
        "the first rung of the ladder is a keypress, not a task, so it does not "
        "preempt the mission's own drive_to"
    )


def test_58_the_watchdog_stays_quiet_when_the_car_really_is_moving() -> None:
    s = stub()
    clock = s.clock
    for i in range(30):
        clock.tick(0.4)
        _reflex(
            s,
            seated(
                mission_active=True,
                speed=12.0 + i * 0.1,
                task_type="drive_to",
                task_status="running",
                task_id="t-obj",
            ),
        )
    assert s.vehicle.phase is VehiclePhase.MOVING
    assert s.vehicle.last_signal is None
    assert s.posted == []
    assert s.primitives.executed == []


def test_58_a_mission_wait_in_a_car_is_left_alone() -> None:
    """The exception the brief demands, and the one `/state` cannot see for
    itself: a mission beat that legitimately parks him has no field
    distinguishing it from a stall, so free-roam drive-away simply does not run
    during a mission. The watchdog above still covers the case that matters,
    because it only ever grades a drive task that is already running."""
    s = stub()
    clock = s.clock
    waiting = seated(mission_active=True, task_status="idle", task_id=None, task_type=None)
    for _ in range(40):
        clock.tick(0.4)
        _reflex(s, waiting)
    assert s.posted == [], "no wander_drive may be posted away from a live mission"
    assert s.primitives.executed == []


# --- 59 / 49: the priority reversal ------------------------------------------


def _beat_him(s: _ReflexStub, state_at: Any, *, ticks: int = 4, hp_per_tick: int = 6) -> None:
    """Feed `_reflex` a run of snapshots with health falling a few points a tick.

    This is what a beating really looks like in `/state`: punches arrive at a
    few HP each, which is exactly why `Delta.big_health_drop` (>= 25 HP between
    two snapshots) never fired once while he was killed on the freeway.
    """
    health = 200
    for _ in range(ticks):
        s.clock.tick(0.4)
        _reflex(s, state_at(health=health))
        health -= hp_per_tick


def test_59_a_ped_attacking_him_on_foot_is_fought_at_reflex_speed() -> None:
    s = stub()
    # A walk already running, so the (unrelated) stranded-on-foot reflex cannot
    # also fire and make this assertion about something else entirely.
    _beat_him(
        s,
        lambda health: on_foot(
            health=health, task_type="walk_to", task_status="running", task_id="t-walk",
            nearby_peds=[owner()],
        ),
    )
    assert [t for t, _ in s.posted] == ["combat_hated_targets_around"], (
        "on foot, a neutral ped whose punches are landing must be fought back at"
    )
    assert s.wheel.owner == "threat"


def test_49_a_ped_attacking_him_in_a_working_car_makes_him_drive_off() -> None:
    """THE OPERATOR'S RULE. "Ped attacks me while I'm already in a working
    stolen car -> FLOOR IT." This is the exact frame from the screenshots: in
    the driver's seat, engine fine, the previous owner swinging through the open
    door."""
    s = stub()
    _beat_him(
        s,
        lambda health: seated(
            health=health, task_status="idle", task_id=None, task_type=None,
            nearby_peds=[owner()],
        ),
    )
    posts = [t for t, _ in s.posted]
    assert "combat_hated_targets_around" not in posts, (
        "getting out to fight the man whose car it is, is how he died"
    )
    assert posts[0] == "wander_drive"
    assert s.wheel.owner == "threat"


def test_49_a_car_that_cannot_move_falls_back_to_the_old_ladder() -> None:
    """"In a vehicle that cannot move (blocked/wrecked) while taking damage, the
    existing ladder is right." Driving away from a wedged car is a way of doing
    nothing while being hit."""
    s = stub()
    _beat_him(
        s,
        lambda health: seated(
            health=health,
            vehicle={"upside_down": False, "in_water": True},  # cannot leave
            task_status="idle", task_id=None, task_type=None,
            nearby_peds=[owner()],
        ),
    )
    assert "combat_hated_targets_around" in [t for t, _ in s.posted]


def test_49_a_car_measured_as_immobile_also_falls_back_to_fighting() -> None:
    """The other route to "cannot move", and the only honest one for a car with
    healthy bodywork: the state machine told us it did not move. `/state` has no
    engine health, no `driveable` and no obstruction flag."""
    parked = seated(nearby_peds=[owner()], task_type="wander_drive", task_status="running")
    hurt = Delta(wanted_from=0, wanted_to=0, big_health_drop=True)
    assert threat_action(parked, hurt, True, vehicle_blocked=False) == {
        "type": "wander_drive",
        "params": {"style": "avoid_traffic"},
    }
    assert threat_action(parked, hurt, True, vehicle_blocked=True) == {
        "type": "combat_hated_targets_around",
        "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M},
    }


def test_49_a_working_escape_is_never_stopped_to_punch_someone() -> None:
    """Taking damage while ROLLING is a crash, not a beating — the collision
    rationale this module has always had. It must not become a combat order."""
    crashing = seated(speed=18.0, task_type="drive_to", task_status="running",
                      nearby_peds=[owner(distance=3.0, relationship="hostile")])
    assert threat_action(crashing, NO_DANGER, True) is None


# --- the suppression list -----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "prepare"),
    [
        ("cutscene", lambda s: None),
        ("control_disabled", lambda s: None),
        ("blocking_screen", lambda s: setattr(s, "_screen_blocked", True)),
        ("deliberate_wait", lambda s: setattr(s, "_quiet_until", 1e18)),
        ("governor_l3", lambda s: setattr(s.governor, "level", 3)),
        ("on_break", lambda s: setattr(s.breaks, "on_break", True)),
    ],
)
def test_sitting_still_is_not_overridden_when_it_is_correct(name: str, prepare: Any) -> None:
    s = stub()
    prepare(s)
    kwargs: dict[str, Any] = {"task_status": "idle", "task_id": None, "task_type": None}
    if name == "cutscene":
        kwargs["cutscene_active"] = True
        kwargs["mission_active"] = True
    if name == "control_disabled":
        kwargs["control_enabled"] = False
    parked = seated(**kwargs)
    for _ in range(30):
        s.clock.tick(0.4)
        _reflex(s, parked)
    assert s.posted == [], f"{name}: sitting still is the correct behaviour here"
    assert s.primitives.executed == []


def test_the_hold_reason_is_the_one_the_machine_reports() -> None:
    """Each suppression carries its reason so a future "why did he sit there"
    is answerable from the log rather than by re-deriving it."""
    s = stub()
    assert s._vehicle_hold(seated(cutscene_active=True, mission_active=True)) == "cutscene"
    assert s._vehicle_hold(seated(control_enabled=False)) == "control_disabled"
    assert s._vehicle_hold(seated(dead=True)) == "player_down"
    s._screen_blocked = True
    assert s._vehicle_hold(seated()) == "blocking_screen"
    s._screen_blocked = False
    s.governor.level = 3
    assert s._vehicle_hold(seated()) == "governor_l3"
    s.governor.level = 0
    s.breaks.on_break = True
    assert s._vehicle_hold(seated()) == "on_break"
    s.breaks.on_break = False
    assert s._vehicle_hold(seated()) is None


# --- motion verification and the recovery ladder ------------------------------


def test_an_action_is_not_successful_because_the_task_was_accepted() -> None:
    """The whole thesis of the motion-verification half. `POST /task` returning
    a task id and `last_task.status == "running"` are both true in the failure
    case; only `vehicle.speed` / `stopped_for_s` can tell the difference."""
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    driving = seated(task_type="wander_drive", task_status="running", task_id="t-1")

    vc.feed(driving)
    assert vc.last_signal is None, "an accepted task is not yet a failure"

    clock.tick(VEHICLE_START_WATCHDOG_S - 0.5)
    assert vc.feed(driving) is None
    assert vc.last_signal is None

    clock.tick(1.0)
    intent = vc.feed(driving)
    assert vc.last_signal == VEHICLE_NOT_MOVING
    assert intent is not None and intent.rung == "reverse_out"
    assert "moved 0.00 m" in intent.reason


def test_the_recovery_ladder_is_graduated_rate_limited_and_bounded() -> None:
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    driving = seated(task_type="wander_drive", task_status="running", task_id="t-1")
    vc.feed(driving)
    clock.tick(VEHICLE_START_WATCHDOG_S + 0.1)

    rungs: list[str] = []
    for _ in range(400):  # 160 s at 2.5 Hz: far longer than the whole ladder
        clock.tick(0.4)
        intent = vc.feed(driving)
        if intent is not None:
            rungs.append(intent.rung)
    # Every rung in order, cheapest first, and the ladder does not loop for
    # free: after `exit_vehicle` it stands down for the cooldown.
    assert rungs[: len(RECOVERY_LADDER)] == list(RECOVERY_LADDER)
    ladders = len(rungs) / len(RECOVERY_LADDER)
    assert ladders <= 160.0 / (
        len(RECOVERY_LADDER) * RECOVERY_RUNG_GAP_S + WRITE_OFF_COOLDOWN_S
    ) + 1, f"the ladder must not loop for free; it ran {ladders} times in 160 s"
    # The car was written off exactly once, however many times the ladder ran.
    assert vc.written_off == (CONVERTIBLE["handle"],)


def test_the_rungs_are_spaced_by_the_gap_not_by_the_poll_rate() -> None:
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    driving = seated(task_type="wander_drive", task_status="running", task_id="t-1")
    vc.feed(driving)
    clock.tick(VEHICLE_START_WATCHDOG_S + 0.1)
    assert vc.feed(driving) is not None  # rung 1
    for _ in range(int(RECOVERY_RUNG_GAP_S / 0.4) - 1):
        clock.tick(0.4)
        assert vc.feed(driving) is None, "a rung must not fire again at the poll rate"
    clock.tick(0.6)
    assert vc.feed(driving) is not None  # rung 2


def test_a_car_that_moved_once_belongs_to_the_stuck_detector_not_here() -> None:
    """A car that has moved and then stopped is a traffic light, a junction or a
    kerb mid-drive — `StuckDetector`'s 20 s ladder already owns that and is
    tuned for it. Firing `reverse_out` at every red light would be wrong, and on
    stream, idiotic."""
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    vc.feed(seated(speed=9.0, task_type="drive_to", task_status="running"))
    at_the_lights = seated(speed=0.0, stopped_for_s=15.0, task_type="drive_to",
                           task_status="running")
    for _ in range(60):  # 24 s: four times the start watchdog
        clock.tick(0.4)
        assert vc.feed(at_the_lights) is None
    assert vc.last_signal is None


def test_every_recovery_attempt_is_logged_with_the_measured_displacement(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """"Log each attempt with the measured displacement so the next person can
    see what actually happened." The number is an anchor-to-now distance, not a
    sum of per-tick steps: a car rocking on the spot accumulates metres while
    going nowhere."""
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    driving = seated(task_type="wander_drive", task_status="running", task_id="t-1")
    vc.feed(driving)
    clock.tick(VEHICLE_START_WATCHDOG_S + 0.1)
    with caplog.at_level(logging.WARNING, logger="wasted.vehicle"):
        vc.feed(driving)
        clock.tick(RECOVERY_RUNG_GAP_S + 0.1)
        # He has been shoved 0.4 m by the reverse; still nowhere.
        vc.feed(seated(pos=(0.4, 0.0, 0.0), task_type="wander_drive",
                       task_status="running", task_id="t-1"))
    kvs = [r.kv for r in caplog.records if hasattr(r, "kv")]
    assert any(kv.get("signal") == VEHICLE_NOT_MOVING for kv in kvs)
    attempts = [kv for kv in kvs if "rung" in kv]
    assert [kv["rung"] for kv in attempts] == ["reverse_out", "swerve"]
    assert attempts[0]["moved_m"] == 0.0
    assert attempts[1]["moved_m"] == pytest.approx(0.4)


def test_a_write_off_gets_him_out_of_the_car() -> None:
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    driving = seated(task_type="wander_drive", task_status="running", task_id="t-1")
    vc.feed(driving)
    clock.tick(VEHICLE_START_WATCHDOG_S + 0.1)
    actions = []
    for _ in range(len(RECOVERY_LADDER)):
        intent = vc.feed(driving)
        assert intent is not None
        actions.append(intent.action["type"])
        clock.tick(RECOVERY_RUNG_GAP_S + 0.1)
    assert actions == ["reverse_out", "swerve", "wander_drive", "exit_vehicle"]


# --- controller ownership -----------------------------------------------------


def test_exactly_one_layer_owns_movement_per_tick() -> None:
    """The wheel GATES. A lower owner gets `None` back, not a bool it may ignore."""
    wheel = MovementWheel(clock=FakeClock())
    wheel.begin_tick()
    survival = wheel.acquire("threat", "being shot")
    assert survival is not None
    assert wheel.acquire("vehicle", "seated and idle") is None
    assert wheel.owner == "threat"
    assert wheel.taken_by_reflex() is True
    assert wheel.holds(survival) is True

    # A one-tick lease: the reflex layer is re-evaluated from scratch every
    # tick, so it simply asks again — and if it does not, the wheel is free.
    wheel.begin_tick()
    assert wheel.owner is None
    objective = wheel.acquire("mission", "drive_to the objective")
    assert objective is not None
    assert wheel.taken_by_reflex() is False
    assert wheel.holds(survival) is False, "the old token stops working"


def test_every_acquire_release_and_refusal_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """"He does nothing" must be diagnosable from the log rather than by
    guessing which layer declined the wheel. Owner, reason and tick, every
    time."""
    wheel = MovementWheel(clock=FakeClock())
    with caplog.at_level(logging.INFO, logger="wasted.vehicle"):
        wheel.begin_tick()
        token = wheel.acquire("mission", "drive_to", lease_ticks=None)
        wheel.acquire("roam", "steal_nice_car")  # refused: outranked
        wheel.begin_tick()
        wheel.release(token)
    lines = [(r.message, getattr(r, "kv", {})) for r in caplog.records]
    kinds = [m for m, _ in lines]
    assert kinds == ["wheel acquired", "wheel refused", "wheel released"]
    assert all("tick" in kv for _, kv in lines), "every line names the tick"
    acquired, refused, released = (kv for _, kv in lines)
    assert (acquired["owner"], acquired["reason"]) == ("mission", "drive_to")
    assert (refused["owner"], refused["held_by"], refused["why"]) == (
        "roam",
        "mission",
        "outranked",
    )
    assert (released["owner"], released["next_in_line"]) == ("mission", "roam")


def test_a_task_already_posted_this_tick_refuses_even_a_higher_owner() -> None:
    """The structural half of "no two movement tasks in one tick": priority
    alone is not enough, because a high-priority layer that runs LATE in a tick
    could otherwise preempt one that has already put an order on the wire."""
    wheel = MovementWheel(clock=FakeClock())
    wheel.begin_tick()
    roam = wheel.acquire("roam", "roam_the_block", lease_ticks=None)
    assert roam is not None
    wheel.mark_posted(roam, "walk_to")
    assert wheel.acquire("threat", "being shot") is None, (
        "survival outranks roam, but the tick already has an order on the wire"
    )
    assert wheel.acquire("roam", "next step") is not None, "its own owner may continue"
    # Next tick the ladder works normally again.
    wheel.begin_tick()
    assert wheel.acquire("threat", "being shot") is not None


def test_the_vehicle_reflex_never_outranks_survival() -> None:
    """Both are reflexes and both want the wheel in the same tick. Survival wins
    and the vehicle intent is dropped rather than posted on top of it."""
    s = stub()
    clock = s.clock
    # Seated and idle. The grace window opens on the first tick he is seen in
    # the seat, so it takes a second tick past it for the drive-away to fire.
    parked = seated(task_status="idle", task_id=None, task_type=None)
    _reflex(s, parked)
    clock.tick(SEATED_GRACE_S + 1.0)
    _reflex(s, parked)
    assert [t for t, _ in s.posted] == ["wander_drive"]
    assert s.wheel.owner == "vehicle"

    # The same situation with him badly hurt and a hostile close: survival's
    # answer is what gets posted, and it owns the tick.
    s2 = stub()
    hurt = seated(health=40, task_status="idle", task_id=None, task_type=None,
                  nearby_peds=[owner(relationship="hostile")])
    _reflex(s2, hurt)
    s2.clock.tick(SEATED_GRACE_S + 1.0)
    _reflex(s2, hurt)
    # One post, not two: ThreatLatch's hold-down covers the second tick, and
    # the vehicle reflex does not sneak a duplicate in behind it.
    assert [t for t, _ in s2.posted] == ["wander_drive"]
    assert s2.wheel.owner == "threat", "survival, not the vehicle reflex, owns the tick"


def test_a_restart_or_a_respawn_forgets_the_car() -> None:
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    vc.feed(seated(task_type="wander_drive", task_status="running"))
    clock.tick(VEHICLE_START_WATCHDOG_S + 0.1)
    vc.feed(seated(task_type="wander_drive", task_status="running"))
    assert vc.phase is VehiclePhase.BLOCKED
    vc.reset()
    assert vc.phase is VehiclePhase.ON_FOOT
    assert vc.last_signal is None
    assert vc.written_off == ()


def test_the_primitives_recorder_is_the_only_sendinput_here() -> None:
    """Guard against a test that silently stops exercising the keypress half:
    the stub routes primitives the way production's single choke point does."""
    s = stub()
    assert isinstance(s.primitives, _PrimitivesRecorder)


# --- the grace window is a run, not a stopwatch since he got in ---------------


def test_arriving_somewhere_is_not_ten_minutes_of_doing_nothing() -> None:
    """The grace window measures the CURRENT run of sitting still with nothing
    running. Measured from the moment he got in, a car driven for ten minutes
    and then parked would trigger an instant drive-away the tick its drive task
    completed — every arrival at every destination."""
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    driving = seated(speed=14.0, task_type="drive_to", task_status="running", task_id="t-trip")
    for _ in range(50):  # a long drive
        clock.tick(0.4)
        assert vc.feed(driving) is None
    # Arrived: the task reports done and the car is stationary.
    arrived = seated(task_type="drive_to", task_status="done", task_id="t-trip")
    clock.tick(0.4)
    assert vc.feed(arrived) is None, "the tick he arrives on is not a drive-away"
    clock.tick(SEATED_GRACE_S - 0.5)
    assert vc.feed(arrived) is None
    clock.tick(1.0)
    assert vc.feed(arrived) is not None, "past the window with nobody steering, he goes"


def test_a_task_starting_again_restarts_the_window() -> None:
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    idle = seated(task_status="idle", task_id=None, task_type=None)
    vc.feed(idle)
    clock.tick(SEATED_GRACE_S - 0.4)
    vc.feed(idle)
    # Somebody claims the wheel just before the window closes.
    vc.feed(seated(task_type="drive_to", task_status="running", task_id="t-x"))
    clock.tick(1.0)
    assert vc.feed(idle) is None, "the window restarts when the wheel comes free again"


def test_a_still_beat_inside_an_activity_is_not_overridden() -> None:
    """`park_and_watch` is drive_to → stop → look_around → wait 25. The gap
    between `stop` completing and the `wait` that follows it is exactly the
    shape the drive-away fires on, and driving off mid-set-piece would make the
    scenic activities unwatchable."""
    s = stub()
    started = s.activity_runner.start("chill", "normal", "park_and_watch")
    assert started is not None and started[0].name == "park_and_watch"
    parked = seated(task_type="stop", task_status="done", task_id="t-stop")
    for _ in range(30):
        s.clock.tick(0.4)
        _reflex(s, parked)
    assert s.posted == [], "an activity mid-plan owns the beat"
    assert s._vehicle_hold(parked) == "activity_step"


def test_an_activity_that_has_declared_victory_is_not_a_hold() -> None:
    """The other side of the same gate, and the whole point of this work:
    `steal_nicer_car`'s entire plan is one `enter_nearest_vehicle`, so the
    runner marks it finished the instant the seat is reached. That is not a
    reason to sit there."""
    s = stub()
    started = s.activity_runner.start("hyped", "rushed", "steal_nicer_car")
    assert started is not None and started[0].name == "steal_nicer_car"
    s.activity_runner.bind_step_task("t-jack")
    s.activity_runner.next_step("done", "t-jack")
    assert s.activity_runner.current is not None and s.activity_runner.current.finished
    just_seated = seated(task_type="enter_nearest_vehicle", task_status="done", task_id="t-jack")
    assert s._vehicle_hold(just_seated) is None
    _reflex(s, just_seated)
    s.clock.tick(SEATED_GRACE_S + 0.5)
    _reflex(s, just_seated)
    assert [t for t, _ in s.posted] == ["wander_drive"]


def test_the_repost_rung_hands_a_mission_back_instead_of_hijacking_it() -> None:
    """`wander_drive` never completes (CONTRACTS §1), so re-posting it during a
    mission would hold the wheel forever and abandon the objective. During a
    mission the rung is `stop` — the contract's own "clear current task" — and
    `MissionFollower` re-posts its own `drive_to` on the next tick."""
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    on_a_job = seated(
        mission_active=True, task_type="drive_to", task_status="running", task_id="t-obj"
    )
    vc.feed(on_a_job)
    clock.tick(VEHICLE_START_WATCHDOG_S + 0.1)
    rungs = []
    for _ in range(len(RECOVERY_LADDER)):
        intent = vc.feed(on_a_job)
        assert intent is not None
        rungs.append(intent.action["type"])
        clock.tick(RECOVERY_RUNG_GAP_S + 0.1)
    assert rungs == ["reverse_out", "swerve", "stop", "exit_vehicle"]


def test_coasting_to_a_halt_gets_a_fresh_window_too() -> None:
    """The MOVING phase clears the idle stopwatch as well as the running-task
    path does, so a car that idled for a moment, drove, and then rolled to a
    stop does not fire a drive-away on the tick it stops."""
    clock = FakeClock()
    vc = VehicleController(clock=clock)
    idle_in_the_seat = seated(task_status="idle", task_id=None, task_type=None)
    vc.feed(idle_in_the_seat)
    clock.tick(1.0)
    vc.feed(idle_in_the_seat)  # inside the grace window, nothing posted yet
    for _ in range(30):
        clock.tick(0.4)
        vc.feed(seated(speed=11.0, task_status="idle", task_id=None, task_type=None))
    clock.tick(0.4)
    assert vc.feed(idle_in_the_seat) is None, "the window restarts when he stops"
    clock.tick(SEATED_GRACE_S + 0.1)
    assert vc.feed(idle_in_the_seat) is not None
