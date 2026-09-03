"""The vehicle-entry state machine: getting in is the START of something, not the end.

WHY THIS FILE EXISTS — observed on stream 2026-09-02, with screenshots. The agent
walked up to a civilian convertible, jacked it, reached the driver's seat, and
then **sat there**. The owner walked round, opened the door and beat him to
death through it. The counter went to 5 WASTED. He never touched the throttle.

Every layer in the harness was, individually, behaving as designed:

* the `steal_nicer_car` activity's whole plan is one `enter_nearest_vehicle`
  step, so the activity reported SUCCESS the instant the seat was reached
  (`activities.py`) — acquisition was the goal, driving was nobody's;
* the only reflex that posts `wander_drive` is gated on governor level 2, so
  at L0/L1 it does not exist (`main._reflex`);
* `IdlePicker`'s entire menu is look_around / radio / `wait` / horn — a menu of
  ways to sit still (`humanizer.py`);
* `StuckDetector` needs a RUNNING drive task before it will even look at
  `vehicle.stopped_for_s` (`recovery.py`), and `TaskStallDetector` returns
  `None` whenever `last_task.status != "running"` — a man sitting in a car with
  no task running was invisible to every observer in the package;
* the threat ladder's "something is hitting me, hit back" rung is explicitly
  `not player.in_vehicle`, and the rung that CAN fire in a car needs him at 30%
  health — three-quarters dead before anything happens.

So nothing was watching the one window that mattered: between
`enter_nearest_vehicle → done` and *the car actually moving*.

WHAT THIS MODULE ADDS, and what it deliberately does not:

* a phase machine over the fields `/state` really carries (`player.in_vehicle`,
  `vehicle.{handle,speed,stopped_for_s,health,upside_down,in_water}`,
  `last_task.{type,status}`) — no new bridge field, no contract change;
* a drive-away intent the moment he is seated with nobody else steering,
  posted from the reflex layer at tick speed and costing nothing. The action is
  `wander_drive`: CONTRACTS §1's one drive task that needs no coordinates and
  never completes, which is exactly "start moving, work out where later";
* motion VERIFICATION. An action is not successful because `POST /task`
  returned 200 — only because the game moved. `vehicle.speed` and
  `vehicle.stopped_for_s` are the evidence, and they are already in every
  snapshot;
* a graduated, rate-limited recovery when the car does not move, ending in
  writing the car off and getting out.

It does NOT invent a throttle. There is no raw driving input in this project's
vocabulary and none is added here (CONTRACTS §1/§2 are frozen; `brain/schemas.py`
enforces them). It does NOT steer during a story mission: mission navigation
belongs to `missions.MissionFollower`, and a mission that legitimately wants him
to sit in a car and wait is not distinguishable from a stall in any field
`/state` exposes — so the honest thing is to leave missions alone and let the
motion watchdog (which only ever fires on a drive task that is ALREADY running)
cover the mission case.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..bridge_client import GameState
from ..logsetup import get_logger

log = get_logger("wasted.vehicle")


# --- thresholds ---------------------------------------------------------------

#: At or below this speed he is parked, not driving. The bridge's own "stopped"
#: bar is 0.2 m/s (`SnapshotBuilder.cs`: below it `stopped_for_s` starts
#: counting); 0.5 m/s sits just above it so a snapshot caught mid-roll, or a car
#: creeping on a camber, is not read as "he is driving".
SEATED_SPEED_MPS = 0.5

#: At or above this speed the car is genuinely under way — the throttle took.
#: ~1.5 m/s is 5 km/h: slower than a jog, far faster than anything a parked car
#: does on its own, and reached within a second of a real pull-away. The gap
#: between this and :data:`SEATED_SPEED_MPS` is hysteresis: without it a car
#: rolling at 0.6 m/s would flap between SEATED and MOVING at the poll rate and
#: re-arm every timer in this file on every other tick.
MOVING_SPEED_MPS = 1.5

#: How long he may sit in the driver's seat, stationary, with NOTHING running,
#: before the reflex starts him moving. Not zero, deliberately: the ordinary
#: layers (the activity runner's next step, the day plan's trip, the mission
#: follower) run later in the same tick and post real destinations, and driving
#: away from under them would be a second competing planner. At a 2-4 Hz poll
#: 1.5 s is four-to-six ticks — long enough for any of them to claim the wheel,
#: short enough that the previous owner has not landed three punches yet.
#:
#: Measured from the start of the CURRENT idle-and-stationary run, not from the
#: moment he got in: a car he drove for ten minutes and then parked must get the
#: same fresh window as one he has just jacked, or every arrival at a
#: destination would trigger an instant drive-away.
SEATED_GRACE_S = 1.5

#: Minimum gap between two drive-away posts. Same reasoning as
#: `recovery.THREAT_HOLD_S`: every `POST /task` preempts the running task
#: (CONTRACTS §1), so re-posting `wander_drive` at 3 Hz would restart the
#: engine's own wander task three times a second and he would never pull away —
#: the exact stutter the threat latch was built to stop.
DRIVE_AWAY_HOLD_S = 4.0

#: How long a drive task may be RUNNING, in a car that has not moved once since
#: he sat in it, before this calls it a failure to start. Deliberately far
#: shorter than `StuckDetector`'s 20 s, which is the right number for a car
#: wedged on geometry MID-DRIVE and much too long for a car that never started:
#: 20 s parked next to the man you just robbed is long enough to be beaten to
#: death, which is the measurement this whole file comes from. 6 s is several
#: poll ticks past the engine's own start-up (door close, ignition, handbrake)
#: and still inside the window where leaving is survivable.
VEHICLE_START_WATCHDOG_S = 6.0

#: Minimum gap between two rungs of the recovery ladder. A `reverse_out` holds
#: the key for up to 1.4 s and the result only shows up in the NEXT snapshot, so
#: anything shorter would grade a rung before the game had answered. Four
#: seconds is roughly a rung plus two poll ticks of evidence.
RECOVERY_RUNG_GAP_S = 4.0

#: After the whole ladder has run without the car moving, this long before the
#: ladder may run again. Bounded retrying, not a loop: the same "hand the
#: problem back rather than drift" rule `StuckDetector.max_nudges_per_episode`
#: applies to the bridge's own nudge.
WRITE_OFF_COOLDOWN_S = 30.0

#: `vehicle.health` is BODY health (`veh.HealthFloat`, ~1000 on an undamaged
#: car); at 0 the car is destroyed/burning. 100 keeps a 10% margin above that.
#: This is NOT engine health — `Vehicle.EngineHealth`, `IsEngineRunning` and
#: `IsDriveable` are not in `/state` at all, so "the engine is dead" and "we
#: never told it to move" read identically here. Treated as a floor for "this
#: is obviously a write-off", never as evidence that a car WILL move.
WRECKED_VEHICLE_HEALTH = 100.0

#: The CONTRACTS §1 task types that are supposed to make a vehicle move. A car
#: sitting still under one of these is the failure this module watches for;
#: under anything else (or under nothing) standing still proves nothing about
#: the throttle.
DRIVE_TASK_TYPES: frozenset[str] = frozenset({"drive_to", "wander_drive", "flee_police"})

#: The diagnosis this module emits when a drive task is running and the world
#: did not move. Logged (WARNING, with the measured displacement) and exposed as
#: :attr:`VehicleController.last_signal`; it is NOT written to the `events`
#: table because CONTRACTS §4's type enum is closed and this package may not
#: bump the contract. See the report accompanying this work package.
VEHICLE_NOT_MOVING = "VEHICLE_NOT_MOVING"

#: The graduated response to a car that will not start, cheapest and least
#: destructive first. Every rung is real vocabulary that already exists:
#: `reverse_out`/`swerve` are CONTRACTS §2 SendInput primitives, `wander_drive`
#: and `exit_vehicle` are §1 bridge tasks.
#:
#: 1. `reverse_out` — back off whatever the nose is buried in. The single most
#:    common physical cause, and the same first rung `StuckDetector` uses.
#: 2. `swerve` — put the wheels somewhere other than straight into the wall the
#:    reverse just backed away from.
#: 3. `repost_drive` — the drive task itself may be the thing that is wrong
#:    (preempted, lost to a bridge blip, or an engine task that quietly gave
#:    up). Ask again before blaming the car. During a MISSION this rung is
#:    `stop` instead: `wander_drive` never completes, so posting one would hold
#:    the wheel indefinitely and abandon the objective, whereas `stop` hands the
#:    slot straight back to `MissionFollower` on the next tick.
#: 4. `exit_vehicle` — write the car off and get out. `nearby.vehicles[]` is a
#:    menu of others with `pos` and `driver` for whoever picks next; this module
#:    does not pick one, because choosing what to do next is the ordinary
#:    layers' job, not a reflex's.
RECOVERY_LADDER: tuple[str, ...] = ("reverse_out", "swerve", "repost_drive", "exit_vehicle")


#: F6, the operator's own acceptance number: after control comes back, SOMETHING has
#: to move him within this long. Three seconds is roughly ten poll ticks at 3 Hz —
#: long enough for the ordinary owners (the reflex ladder, the mission follower, the
#: day plan, a roam goal, a brain decision) to claim the wheel on their own terms, and
#: short enough that a viewer watching a respawn or the end of a cutscene never sees
#: the agent stand still long enough to wonder whether the stream has frozen.
#:
#: This is the number the acceptance check MEASURES. It is not the number the
#: fallback fires on — see :data:`CONTROL_REGAINED_FALLBACK_S`.
CONTROL_REGAINED_DEADLINE_S = 3.0

#: When the `resume` rung actually fires, and it is deliberately INSIDE the
#: deadline above rather than equal to it.
#:
#: A fallback that fires AT the deadline can never satisfy it: `tools/funcheck.py`
#: counts an edge answered only when a movement task is posted in the closed
#: window `[edge, edge + 3s]`, so a post at 3.0 s + one poll interval is late by
#: construction — measured, on a 2 h synthetic replay, as 16 edges "missed" that
#: the fallback had in fact answered a fraction of a second too late. Two seconds
#: leaves a full second of margin for the poll interval (2-4 Hz), the wheel
#: arbitration and the HTTP round trip, and still gives every ordinary owner
#: six-to-eight ticks to claim the wheel first.
CONTROL_REGAINED_FALLBACK_S = 2.0

#: How wide the F6 fallback looks for a car. Matches `navigation.VEHICLE_SEARCH_RADIUS_M`
#: rather than the contract's 30 m default: this fires precisely when nothing else
#: wanted the wheel, so a slightly wider look costs nothing and fails less often.
RESUME_SEARCH_RADIUS_M = 40.0

#: The control-regained edges F6 is measured across. Named rather than boolean so the
#: log and the funcheck can say WHICH edge went unanswered — "he stands still after a
#: respawn" and "he stands still when a cutscene ends" are different bugs.
#: Movement-class bridge tasks that do not, in fact, move him — so they never
#: answer an F6 deadline. `stop` is CONTRACTS §1's "clear current task → idle"
#: (the threat rung's answer to a task that has him pinned, and what a break
#: posts on its way out) and `set_waypoint` is a map marker that completes in
#: the same tick without touching the ped. Both belong to the wheel, because
#: both preempt whatever is running; neither is evidence that anybody moved.
NON_MOVING_TASKS: frozenset[str] = frozenset({"stop", "set_waypoint"})

CONTROL_REGAINED_EDGES: tuple[str, ...] = (
    "respawn",
    "interior_exit",
    "cutscene_end",
    "mission_end",
    "switch_end",
    "control_restored",
)


class VehiclePhase(StrEnum):
    """Where he is in the get-in-and-drive sequence.

    Grounded entirely in fields `/state` really has. There is no seat field and
    no engine field in the contract, so this machine deliberately never claims
    to know whether he is the driver or whether the engine runs — see the module
    docstring and the accompanying report.
    """

    #: Not in a vehicle, and not currently getting into one.
    ON_FOOT = "on_foot"
    #: `enter_nearest_vehicle` is RUNNING and he is not in the seat yet.
    ENTERING = "entering"
    #: `player.in_vehicle`, a `vehicle` in the snapshot, speed at/below
    #: :data:`SEATED_SPEED_MPS`. This is the phase the man in the screenshots
    #: died in.
    SEATED = "seated"
    #: Speed at/above :data:`MOVING_SPEED_MPS`. The only phase that proves an
    #: action worked.
    MOVING = "moving"
    #: A drive task has been running for :data:`VEHICLE_START_WATCHDOG_S` and
    #: the car has never moved since he got in. The recovery ladder is running.
    BLOCKED = "blocked"
    #: The ladder ran out. The car is a write-off; get out.
    STUCK = "stuck"


@dataclass(frozen=True)
class VehicleIntent:
    """One thing the vehicle reflex wants done this tick.

    `action` is a ready-to-execute `{"type", "params"}` pair from the frozen
    vocabulary. `kind` says which half of this module produced it so the caller
    can log and arbitrate; `reason` is one human line for the log, because the
    next person debugging "he did nothing" should be able to read it rather than
    guess.
    """

    kind: str  # "drive_away" | "recover"
    action: dict[str, Any]
    reason: str
    rung: str = ""


def vehicle_can_drive_away(state: GameState) -> bool:
    """Is he sitting in a car that could plausibly leave right now?

    True means: in a vehicle, the snapshot has one, it is the right way up, it
    is not in the water, and its body is not a burnt-out shell. Every one of
    those is a real CONTRACTS §1 field.

    What it deliberately does NOT mean is "the engine will start". `/state`
    carries body health only; `Vehicle.EngineHealth` / `IsEngineRunning` /
    `IsDriveable` are not exposed, so an engine-dead car reads as perfectly
    healthy here. That gap is what :class:`VehicleController`'s motion
    watchdog exists to catch empirically: if the car does not move, it does not
    matter WHY it does not move.
    """
    v = state.vehicle
    return bool(
        state.player.in_vehicle
        and v is not None
        and not v.upside_down
        and not v.in_water
        and v.health > WRECKED_VEHICLE_HEALTH
    )


def vehicle_is_moving(state: GameState) -> bool:
    """Is the car he is in actually under way?

    Two independent pieces of evidence, either of which is enough:
    `vehicle.speed` at/above :data:`MOVING_SPEED_MPS`, or `stopped_for_s` at
    zero — the bridge resets that counter the moment speed clears its own
    0.2 m/s bar, so a zero there is the GAME's statement that the wheels are
    turning, computed on the game thread rather than sampled at 3 Hz.
    """
    v = state.vehicle
    if not state.player.in_vehicle or v is None:
        return False
    return v.speed >= MOVING_SPEED_MPS or (v.speed > SEATED_SPEED_MPS and v.stopped_for_s <= 0.0)


def resume_action(state: GameState, style: str) -> dict[str, Any] | None:
    """F6's fallback: the cheapest honest way to be moving again, or None.

    Deliberately not a plan and not a destination — those belong to the layers
    that were supposed to claim the wheel and did not. This is one task that
    makes the next three seconds not-standing-still:

    * seated in a car that could plausibly leave → `wander_drive`, the one drive
      task in CONTRACTS §1 that needs no coordinates;
    * on foot outdoors → `enter_nearest_vehicle`, because on foot with no car is
      the state every other layer wants him out of anyway.

    Returns None — on purpose, and it is not a failure — in the two cases where
    no honest fallback exists:

    * **indoors** (`player.interior` is not null, CONTRACTS v1.12). Widening a
      vehicle search from inside a building picks a car behind more walls; that
      is the exact failure `exit_interior` was built to stop, and walking him out
      is that owner's job, not this one's. Firing here would re-open it.
    * **in a car that cannot drive away** (upside down, in the water, a burnt-out
      shell). `flip` outranks this rung and owns getting him out.
    """
    if state.player.interior is not None:
        return None
    if state.player.in_vehicle:
        if not vehicle_can_drive_away(state):
            return None
        # `rushed`, not the mood: this fires because nothing moved him for two
        # seconds, and a driver who has just got the wheel back pulls out — he
        # does not ease into traffic. The mood style is for the layers that
        # were meant to claim the wheel and did not.
        return {"type": "wander_drive", "params": {"style": "rushed"}}
    return {
        "type": "enter_nearest_vehicle",
        "params": {"prefer": "any", "search_radius_m": RESUME_SEARCH_RADIUS_M},
    }


@dataclass
class ControlRegained:
    """F6: the game just handed the controls back — did anybody use them?

    WHY THIS EXISTS. Every one of these edges is a moment the harness already
    notices and already reacts to, and none of them was ever *measured*. The
    respawn handler clears stale mission state; the interior watcher logs
    `left_interior` and releases the wheel; the cutscene/switch/mission flags
    gate `_execute_action`. What none of them did was ask the next question:
    **and then did he move?** The 2 h of logs behind docs/findings.md are full
    of goals that ended `duration_s=0.3` and ticks where nobody held the wheel
    at all, which is the same failure seen from the other end.

    So this is a stopwatch, not a policy. `feed()` watches the five contract
    fields that mean "the game had the controls and now it does not", arms a
    deadline on the transition, and `moved()` — called from the ONE place a
    movement task actually reaches the bridge — stops it. `overdue()` answers
    once per arming, so a missed deadline is one fallback and one log line, not
    a rung that re-fires at the poll rate.

    The edges, all from `/state`:

    * ``respawn`` — the caller's own `DeathRecovery` verdict, which is stronger
      than `dead` going false (it knows an arrest release from a respawn).
    * ``interior_exit`` — `player.interior` non-null → null (CONTRACTS v1.12).
    * ``cutscene_end`` — `mission.cutscene_active` true → false.
    * ``mission_end`` — `mission.active` true → false.
    * ``switch_end`` — `player.switch_in_progress` true → false (v1.11).
    * ``control_restored`` — `player.control_enabled` false → true.

    A second edge while one is already armed simply re-arms: the deadline is
    measured from the LATEST moment the game gave the controls back, because
    that is the moment a viewer starts waiting.
    """

    clock: Any = time.monotonic
    #: When `overdue()` starts answering — the FALLBACK time, not F6's own bar.
    deadline_s: float = CONTROL_REGAINED_FALLBACK_S

    #: Previous-tick values of the five flags. Seeded to "the game is not holding
    #: anything", so the first tick of a session cannot manufacture an edge out of
    #: a field that has simply never been read before.
    _prev_cutscene: bool = False
    _prev_mission: bool = False
    _prev_switch: bool = False
    _prev_control: bool = True
    _prev_interior: bool = False

    _edge: str | None = None
    _armed_at: float = 0.0
    _answered: bool = True
    _reported: bool = False

    def feed(self, state: GameState, *, respawned: bool = False) -> str | None:
        """One tick. Returns the edge name when one fired, else None."""
        edge: str | None = None
        if respawned:
            edge = "respawn"
        interior = state.player.interior is not None
        if self._prev_interior and not interior:
            edge = "interior_exit"
        if self._prev_cutscene and not state.mission.cutscene_active:
            edge = "cutscene_end"
        if self._prev_mission and not state.mission.active:
            edge = "mission_end"
        if self._prev_switch and not state.player.switch_in_progress:
            edge = "switch_end"
        if not self._prev_control and state.player.control_enabled:
            edge = "control_restored"

        self._prev_interior = interior
        self._prev_cutscene = state.mission.cutscene_active
        self._prev_mission = state.mission.active
        self._prev_switch = state.player.switch_in_progress
        self._prev_control = state.player.control_enabled

        if edge is None:
            return None
        self._edge = edge
        self._armed_at = self.clock()
        self._answered = False
        self._reported = False
        log.info(
            "control regained",
            extra={"kv": {"edge": edge, "deadline_s": self.deadline_s}},
        )
        return edge

    def moved(self, action_type: str) -> None:
        """A movement task actually went out. The deadline is met.

        :data:`NON_MOVING_TASKS` do not count: `stop` and `set_waypoint` are
        movement-CLASS tasks (they preempt, so they belong to the wheel) that
        move nobody, and letting a `stop` answer F6 would make the measurement
        agree that a man standing still is moving.
        """
        if self._answered or self._edge is None or action_type in NON_MOVING_TASKS:
            return
        self._answered = True
        log.info(
            "control regained: answered",
            extra={
                "kv": {
                    "edge": self._edge,
                    "action": action_type,
                    "after_s": round(self.clock() - self._armed_at, 2),
                }
            },
        )

    def overdue(self) -> str | None:
        """The edge nobody answered, once. None while inside the deadline."""
        if self._answered or self._edge is None or self._reported:
            return None
        if self.clock() - self._armed_at < self.deadline_s:
            return None
        self._reported = True
        log.warning(
            "control regained and nothing moved him",
            extra={
                "kv": {
                    "edge": self._edge,
                    "deadline_s": self.deadline_s,
                    "waited_s": round(self.clock() - self._armed_at, 2),
                }
            },
        )
        return self._edge

    @property
    def pending(self) -> str | None:
        """The armed, still-unanswered edge, for logs and the funcheck."""
        return None if self._answered else self._edge

    def reset(self) -> None:
        self._edge = None
        self._armed_at = 0.0
        self._answered = True
        self._reported = False
        self._prev_cutscene = False
        self._prev_mission = False
        self._prev_switch = False
        self._prev_control = True
        self._prev_interior = False


@dataclass
class VehicleController:
    """ON_FOOT → ENTERING → SEATED → MOVING, plus BLOCKED and STUCK.

    One instance, fed once per poll tick with the fresh snapshot. It answers two
    questions the rest of the harness could not previously ask:

    * **"He is in a car and nothing is happening — why is he still here?"**
      → :data:`SEATED_GRACE_S` after the seat, with no task running and nobody
      holding him, post `wander_drive`. No model call, no coordinates, no cost.
    * **"We asked the car to move. Did it?"** → a drive task RUNNING for
      :data:`VEHICLE_START_WATCHDOG_S` in a car that has not moved once since he
      sat down is :data:`VEHICLE_NOT_MOVING`, and gets the
      :data:`RECOVERY_LADDER`, rate-limited, each rung logged with the measured
      displacement.

    The watchdog is armed ONLY while the car has never moved since entry. Once
    it has moved once, a later stop is an ordinary traffic stop, a junction or a
    kerb mid-drive — `StuckDetector`'s 20 s ladder already owns that case and is
    tuned for it, and firing `reverse_out` at every red light would be both
    wrong and, on stream, idiotic.

    `hold` (a caller-supplied string) is the whole suppression story and it is
    the caller's to compute, because every reason lives in `main`: a cutscene,
    `player.control_enabled == False`, a deliberate `wait`, governor L3 asleep
    in a parked car, a blocking screen, the mission follower holding position.
    A non-None `hold` still advances the machine — it only withholds the intent.
    """

    clock: Any = time.monotonic
    seated_grace_s: float = SEATED_GRACE_S
    watchdog_s: float = VEHICLE_START_WATCHDOG_S
    rung_gap_s: float = RECOVERY_RUNG_GAP_S
    hold_s: float = DRIVE_AWAY_HOLD_S
    cooldown_s: float = WRITE_OFF_COOLDOWN_S

    phase: VehiclePhase = VehiclePhase.ON_FOOT
    #: The last diagnosis emitted, for logs and for the brain's context line.
    last_signal: str | None = None

    #: `vehicle.handle` of the car he is in. Every per-car timer is keyed on it,
    #: so swapping cars starts a clean machine (handles are ephemeral by
    #: contract, valid only while the entity exists).
    _handle: int | None = None
    #: When he first sat in THIS car (for the log line only).
    _seated_since: float | None = None
    #: When the current "seated, stationary, nothing running, nobody holding
    #: him" run started. This — not `_seated_since` — is what the grace window
    #: is measured from, so parking at the end of a ten-minute drive gets the
    #: same fresh window as a car he has just jacked, and a task that ends
    #: cleanly is not treated as ten minutes of doing nothing.
    _idle_since: float | None = None
    #: Has THIS car moved at all since he got into it? The single most important
    #: bit in the file: it is what separates "never started" from "stopped".
    _ever_moved: bool = False
    #: When the current "a drive task is running and nothing is moving" window
    #: opened, and where he was when it did.
    _nomotion_since: float | None = None
    _nomotion_anchor: tuple[float, float] | None = None
    _last_drive_away_at: float = 0.0
    _rung: int = 0
    _last_rung_at: float = 0.0
    _cooldown_until: float = 0.0
    _signalled: bool = False
    #: The id `POST /task` returned for a task THIS module issued. Bound by the
    #: caller, same rule as everywhere else in the package: only the id the POST
    #: returned is authoritative, never `last_task.id` from the next snapshot.
    _own_task_id: str | None = None
    #: Handles this module has already written off, so a later tick does not
    #: report the same dead car twice. Bounded by :data:`_MAX_WRITE_OFFS`.
    _written_off: list[int] = field(default_factory=list)

    #: Enough to remember the last few write-offs for logging without growing a
    #: list for the lifetime of a 24/7 show.
    _MAX_WRITE_OFFS = 8

    # -- lifecycle ------------------------------------------------------------

    def reset(self) -> None:
        """Forget everything (a new game process behind the same bridge URL, or
        a respawn — the car he was in no longer exists either way)."""
        self.phase = VehiclePhase.ON_FOOT
        self.last_signal = None
        self._handle = None
        self._seated_since = None
        self._idle_since = None
        self._ever_moved = False
        self._nomotion_since = None
        self._nomotion_anchor = None
        self._last_drive_away_at = 0.0
        self._rung = 0
        self._last_rung_at = 0.0
        self._cooldown_until = 0.0
        self._signalled = False
        self._own_task_id = None
        self._written_off.clear()

    def bind_task(self, task_id: str | None) -> None:
        """Record the id `POST /task` returned for an intent this module issued."""
        self._own_task_id = task_id

    @property
    def written_off(self) -> tuple[int, ...]:
        """Vehicle handles this module gave up on, most recent last."""
        return tuple(self._written_off)

    # -- the machine ----------------------------------------------------------

    def feed(
        self,
        state: GameState,
        *,
        hold: str | None = None,
        style: str = "normal",
    ) -> VehicleIntent | None:
        """One tick. Updates the phase; returns what to do, or None.

        `hold` is the caller's "sitting still is correct right now" and its
        value is the reason, logged on the transition. `style` is the mood's
        driving style, passed straight through to `wander_drive`.
        """
        now = self.clock()
        previous = self.phase
        self._track_vehicle(state, now)
        base = self._base_phase(state)

        if base in (VehiclePhase.ON_FOOT, VehiclePhase.ENTERING):
            self.phase = base
            self._log_transition(previous, state, hold)
            return None

        if base is VehiclePhase.MOVING:
            # The one phase that proves an action worked. Everything the
            # watchdog was counting is now answered: stand the ladder down.
            self._ever_moved = True
            self._nomotion_since = None
            self._nomotion_anchor = None
            # He is driving, so he is not sitting still: the grace window must
            # start again from the next time he actually stops. Without this a
            # car that idled for a moment, drove for a minute and then coasted
            # to a halt would trigger a drive-away on the very tick it stopped.
            self._idle_since = None
            self._rung = 0
            self._signalled = False
            self.phase = VehiclePhase.MOVING
            self._log_transition(previous, state, hold)
            return None

        # --- SEATED and stationary from here on ------------------------------
        intent = self._seated_tick(state, now, hold, style)
        self._log_transition(previous, state, hold)
        return intent

    def _seated_tick(
        self, state: GameState, now: float, hold: str | None, style: str
    ) -> VehicleIntent | None:
        lt = state.last_task
        driving = lt.status == "running" and lt.type in DRIVE_TASK_TYPES

        # Motion verification. Armed only while this car has NEVER moved since
        # he got in — see the class docstring for why a car that moved and then
        # stopped belongs to StuckDetector instead.
        if driving and not self._ever_moved:
            if self._nomotion_since is None:
                self._nomotion_since = now
                self._nomotion_anchor = (state.player.pos.x, state.player.pos.y)
        else:
            self._nomotion_since = None
            self._nomotion_anchor = None
            self._signalled = False

        if self._nomotion_since is not None and now - self._nomotion_since >= self.watchdog_s:
            self.phase = VehiclePhase.BLOCKED
            return self._recover(state, now, hold, style)

        self.phase = VehiclePhase.SEATED
        if hold is not None or lt.status == "running":
            # Held on purpose, or somebody already has the wheel. Either way the
            # grace window has not started: it measures the current run of
            # doing nothing, and a task that just ended must not inherit the
            # stopwatch of the drive before it.
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = now

        if hold is not None:
            return None
        if lt.status == "running":
            # Whatever they asked for, the watchdog above is what grades it;
            # posting a second movement task on the same tick is the thrash this
            # package keeps designing against (CONTRACTS §1: every POST
            # preempts).
            return None
        if state.mission.active:
            # Mission navigation belongs to MissionFollower, and a mission beat
            # that legitimately wants him parked in a car (waiting for a trigger,
            # waiting for a passenger) is indistinguishable from a stall in every
            # field /state exposes. `wander_drive` here would drive him away from
            # the objective and fail the job. The watchdog above still covers the
            # mission case, because it only ever grades a drive task that is
            # ALREADY running.
            return None
        if not vehicle_can_drive_away(state):
            # Upside down, in the water, or a burnt-out shell. `recovery
            # .flipped_action` owns getting him out of an upside-down car and
            # the threat ladder owns being attacked in a car that cannot move.
            return None
        if self._idle_since is None or now - self._idle_since < self.seated_grace_s:
            return None
        if now - self._last_drive_away_at < self.hold_s:
            return None
        idle_for = now - self._idle_since
        self._last_drive_away_at = now
        return VehicleIntent(
            kind="drive_away",
            # Same reasoning as `resume_action`: sat still with nothing running
            # is the failure this exists to end, so leave like you mean it.
            action={"type": "wander_drive", "params": {"style": "rushed"}},
            reason=(
                f"seated and stationary for {idle_for:.1f}s with nothing running "
                f"(in the seat {now - (self._seated_since or now):.1f}s)"
            ),
        )

    # -- recovery ladder ------------------------------------------------------

    def _recover(
        self, state: GameState, now: float, hold: str | None, style: str
    ) -> VehicleIntent | None:
        moved_m = 0.0
        if self._nomotion_anchor is not None:
            moved_m = math.dist((state.player.pos.x, state.player.pos.y), self._nomotion_anchor)
        stalled_for = now - (self._nomotion_since or now)
        v = state.vehicle

        if not self._signalled:
            self._signalled = True
            self.last_signal = VEHICLE_NOT_MOVING
            log.warning(
                "VEHICLE_NOT_MOVING: a drive task is running and the car has not "
                "moved since he got in",
                extra={
                    "kv": {
                        "signal": VEHICLE_NOT_MOVING,
                        "task_type": state.last_task.type,
                        "task_status": state.last_task.status,
                        "stalled_for_s": round(stalled_for, 1),
                        "moved_m": round(moved_m, 2),
                        "speed_mps": round(v.speed, 2) if v else None,
                        "stopped_for_s": round(v.stopped_for_s, 1) if v else None,
                        "vehicle": v.display_name if v else None,
                        "handle": self._handle,
                        # Whose drive order stalled: ours, or a planner's.
                        "our_task_id": self._own_task_id,
                        "task_id": state.last_task.id,
                    }
                },
            )

        if hold is not None:
            return None
        if now < self._cooldown_until:
            return None
        if now - self._last_rung_at < self.rung_gap_s:
            return None
        if self._rung >= len(RECOVERY_LADDER):
            # The whole ladder ran and he is still parked. Back off for a while
            # rather than loop: bounded retrying is the rule this package uses
            # everywhere (StuckDetector.max_nudges_per_episode, the unstick
            # cooldown), because an unbounded nudge ladder is a teleport.
            self._rung = 0
            self._cooldown_until = now + self.cooldown_s
            log.warning(
                "the recovery ladder did not get this car moving; standing down",
                extra={
                    "kv": {
                        "handle": self._handle,
                        "moved_m": round(moved_m, 2),
                        "cooldown_s": self.cooldown_s,
                    }
                },
            )
            return None

        rung = RECOVERY_LADDER[self._rung]
        self._rung += 1
        self._last_rung_at = now
        log.warning(
            "vehicle-start recovery",
            extra={
                "kv": {
                    "rung": rung,
                    "rung_index": self._rung,
                    "handle": self._handle,
                    "stalled_for_s": round(stalled_for, 1),
                    # The measured displacement since the window opened. This is
                    # the number that says whether the PREVIOUS rung did
                    # anything; "the task was accepted" is not evidence.
                    "moved_m": round(moved_m, 2),
                    "speed_mps": round(v.speed, 2) if v else None,
                }
            },
        )
        if rung == "reverse_out":
            action = {"type": "reverse_out", "params": {"ms": 1400}}
        elif rung == "swerve":
            # Direction from the handle rather than a coin flip: same car, same
            # answer, so a replayed log is reproducible.
            direction = "left" if (self._handle or 0) % 2 == 0 else "right"
            action = {"type": "swerve", "params": {"direction": direction}}
        elif rung == "repost_drive":
            if state.mission.active:
                # `stop`, not a replacement drive: `wander_drive` NEVER
                # completes (CONTRACTS §1), so posting it during a mission would
                # hold the wheel indefinitely and abandon the objective.
                # `stop` is the contract's own "clear current task -> idle", and
                # `MissionFollower` re-posts its own `drive_to` with fresh
                # coordinates on the very next tick — the same hand-back idiom
                # `TaskStallDetector` uses.
                action = {"type": "stop", "params": {}}
            else:
                action = {"type": "wander_drive", "params": {"style": style}}
        else:
            self.phase = VehiclePhase.STUCK
            self._note_write_off()
            action = {"type": "exit_vehicle", "params": {}}
        return VehicleIntent(
            kind="recover",
            action=action,
            reason=f"{VEHICLE_NOT_MOVING} for {stalled_for:.1f}s, moved {moved_m:.2f} m",
            rung=rung,
        )

    def _note_write_off(self) -> None:
        if self._handle is None or self._handle in self._written_off:
            return
        self._written_off.append(self._handle)
        del self._written_off[: max(0, len(self._written_off) - self._MAX_WRITE_OFFS)]
        log.warning(
            "writing this car off: it never moved. Getting out.",
            extra={"kv": {"handle": self._handle}},
        )

    # -- bookkeeping ----------------------------------------------------------

    def _track_vehicle(self, state: GameState, now: float) -> None:
        """Key every per-car timer on `vehicle.handle`, and start them on entry."""
        v = state.vehicle
        handle = v.handle if (state.player.in_vehicle and v is not None) else None
        if handle == self._handle:
            if handle is not None and not self._ever_moved and vehicle_is_moving(state):
                self._ever_moved = True
            return
        # A different car (or none): everything the old one accumulated is void.
        self._handle = handle
        self._seated_since = now if handle is not None else None
        self._idle_since = None
        self._ever_moved = bool(handle is not None and vehicle_is_moving(state))
        self._nomotion_since = None
        self._nomotion_anchor = None
        self._rung = 0
        self._last_rung_at = 0.0
        self._cooldown_until = 0.0
        self._signalled = False
        self._last_drive_away_at = 0.0
        self.last_signal = None

    def _base_phase(self, state: GameState) -> VehiclePhase:
        p, v, lt = state.player, state.vehicle, state.last_task
        if not p.in_vehicle or v is None:
            if lt.type == "enter_nearest_vehicle" and lt.status == "running":
                return VehiclePhase.ENTERING
            return VehiclePhase.ON_FOOT
        if v.speed >= MOVING_SPEED_MPS:
            return VehiclePhase.MOVING
        if self.phase is VehiclePhase.MOVING and v.speed > SEATED_SPEED_MPS:
            # Hysteresis band: still rolling, do not re-arm the start watchdog
            # for a car that is merely slowing for a junction.
            return VehiclePhase.MOVING
        return VehiclePhase.SEATED

    def _log_transition(self, previous: VehiclePhase, state: GameState, hold: str | None) -> None:
        if self.phase is previous:
            return
        v = state.vehicle
        log.info(
            "vehicle phase",
            extra={
                "kv": {
                    "from": previous.value,
                    "to": self.phase.value,
                    "handle": self._handle,
                    "vehicle": v.display_name if v else None,
                    "speed_mps": round(v.speed, 2) if v else None,
                    "stopped_for_s": round(v.stopped_for_s, 1) if v else None,
                    "task": f"{state.last_task.type}/{state.last_task.status}",
                    "hold": hold,
                }
            },
        )

    def note(self) -> str:
        """One line for the log / the brain's dynamic context."""
        if self.phase is VehiclePhase.STUCK:
            return "VEHICLE: this car never moved. Written off — get out and take another."
        if self.phase is VehiclePhase.BLOCKED:
            return "VEHICLE: told it to drive and it has not moved. Working the recovery ladder."
        if self.phase is VehiclePhase.MOVING:
            return "VEHICLE: rolling."
        if self.phase is VehiclePhase.SEATED:
            return "VEHICLE: in the driver's seat, stationary."
        if self.phase is VehiclePhase.ENTERING:
            return "VEHICLE: getting in."
        return "VEHICLE: on foot."


# --- movement ownership: the wheel GATES, it does not merely record ----------
#
# WHAT CHANGED AND WHY (the operator's words, 2026-09-03): "Free roam registers
# with MovementWheel after posting its task. Two owners can issue movement in
# the same tick (roam + reflex, roam + mission), which is the deadlock class
# that produced 'follow Lamar while standing next to the objective car'."
#
# The first version of this class was ADVISORY. `claim()` returned a bool the
# caller was free to ignore and `force()` only logged a WARNING when a second
# layer posted on the same tick. It recorded history; it prevented nothing. So
# a mission-follow `follow_entity` and a roam-goal `walk_to` could both reach
# `POST /task` inside one tick, and CONTRACTS §1 is explicit that "posting a
# new task preempts the running one" — the two orders cancelled each other out
# at the poll rate and the agent stood still next to the thing he was meant to be
# driving to.
#
# This version is a GATE. `acquire()` hands out a token or refuses, `release()`
# gives it back, and `main._execute_action` — the single funnel every bridge
# task in this harness passes through — will not POST a task whose token is not
# the current holder's.
#
# WHAT IT ARBITRATES, precisely: **CONTRACTS §1 bridge tasks**, all twelve, and
# nothing else. That is the whole deadlock currency, because posting any one of
# them preempts whatever task is running (§1). The §2 primitives are NOT
# arbitrated: `look_around` is a mouse sweep, `wait` posts nothing at all,
# `radio`/`horn` are their own endpoints, `press_prompt_key` is one 100 ms "E",
# and `brake_tap`/`swerve`/`reverse_out` are 200–2000 ms key holds that exist
# *specifically* to nudge a car while somebody else's drive task keeps running
# — that is why the stuck ladder's first rungs are keypresses rather than
# tasks, and gating them here would re-open the reflex-starvation bug that
# design fixed. A reflex whose action is a primitive calls :meth:`note`
# instead: it is logged, it keeps `taken_by_reflex()` honest, and it refuses
# nobody.

#: The four ownership CLASSES of the operator's ladder. Higher wins.
WHEEL_REFLEX = 3
WHEEL_MISSION = 2
WHEEL_ROAM = 1
WHEEL_IDLE = 0

WHEEL_CLASS_NAMES: dict[int, str] = {
    WHEEL_REFLEX: "reflex",
    WHEEL_MISSION: "mission",
    WHEEL_ROAM: "roam",
    WHEEL_IDLE: "idle",
}


@dataclass(frozen=True)
class MovementOwner:
    """One layer that can post a bridge task, and where it sits in the ladder.

    `klass` is the operator's four-rung ladder (reflex > mission > roam >
    idle). `rank` breaks ties *inside* a class, because this harness has more
    layers than the ladder has rungs and their relative order is already known
    and already tested — survival outranks the drive-away reflex, the
    drive-away reflex outranks the stranded search, and so on. The pair
    `(klass, rank)` is a strict total order, so "higher or equal priority
    wins" only ever means "the same owner is renewing", which is exactly what
    it should mean.
    """

    name: str
    klass: int
    rank: int
    what: str


#: Every layer that can post a bridge task, highest priority first. The order
#: of this table is the specification: a tick must reach these layers in this
#: order, because the first bridge task posted in a tick is the only one.
MOVEMENT_OWNER_TABLE: tuple[MovementOwner, ...] = (
    MovementOwner("flip", WHEEL_REFLEX, 10, "upside down / in the water: get out"),
    # T9 (findings.md R1/R5), owned by the reflex package but ranked here
    # because this table is the ladder: a car sunk in the water is the same
    # class of physical emergency as one on its roof, and strictly less urgent
    # than the roof (you get out of the thing that is upside down first).
    MovementOwner("water", WHEEL_REFLEX, 9, "in the water past the timeout: get out and swim"),
    # T9: on foot with a vehicle closing fast. Above the ordinary survival
    # rungs on purpose — a car is seconds from a hit and stepping off costs
    # nothing that `threat_action` cannot ask for again on the next tick.
    MovementOwner("road_dodge", WHEEL_REFLEX, 8, "on foot, a vehicle closing fast: step off"),
    MovementOwner("threat", WHEEL_REFLEX, 7, "survival, and clearing a task that has him pinned"),
    MovementOwner("vehicle", WHEEL_REFLEX, 6, "drive away / get the car moving"),
    MovementOwner("physical", WHEEL_REFLEX, 5, "wedged: reverse_out / swerve / unstick"),
    MovementOwner("governor", WHEEL_REFLEX, 4, "budget override: L2 wander, L3 scenic park"),
    # CONTRACTS v1.12: `player.interior` says he is indoors, so he is. Above
    # `house_escape` (which is the pre-v1.12 GUESS at the same thing) and above
    # `stranded` (whose whole answer is to widen a vehicle search, which from
    # inside a building only ever picks a car behind more walls). Below the
    # survival and physical rungs: being shot at indoors is still worse than
    # being indoors. It holds an OPEN lease — the ladder runs across many ticks
    # with its own per-step timeouts — which is also what stops free roam
    # picking a goal it could not possibly walk to while he is in a living room.
    MovementOwner("exit_interior", WHEEL_REFLEX, 3, "indoors (player.interior): walk out"),
    MovementOwner("house_escape", WHEEL_REFLEX, 2, "apparently indoors: walk out"),
    MovementOwner("stranded", WHEEL_REFLEX, 1, "on foot with no car"),
    # F6. The LAST reflex rung, and the only one whose trigger is the absence of
    # everything else: control came back, three seconds passed, and not one layer
    # in this table posted a movement task. It is bottom of the reflex class
    # rather than a class of its own because it must never outrank a real reason
    # to be standing still (a firefight, a wedged car, a walk out of a building),
    # and must always outrank the planners, which are exactly the layers that
    # just failed to act. See `ControlRegained` and `resume_action`.
    MovementOwner("resume", WHEEL_REFLEX, 0, "F6: control came back and nobody moved him"),
    MovementOwner("brain", WHEEL_MISSION, 2, "a tactical/director decision"),
    MovementOwner("mission", WHEEL_MISSION, 1, "MissionFollower's objective navigation"),
    MovementOwner("day_plan", WHEEL_MISSION, 0, "DayPlanner's trip to a mission-start marker"),
    MovementOwner("roam", WHEEL_ROAM, 0, "the locked free-roam goal's plan steps"),
    MovementOwner("idle", WHEEL_IDLE, 0, "IdlePicker — posts no bridge task at all"),
)

OWNERS_BY_NAME: dict[str, MovementOwner] = {o.name: o for o in MOVEMENT_OWNER_TABLE}

#: Kept as a flat tuple of names because that is what a log grep and a test
#: assertion actually want ("nobody claims under a name the log will not
#: explain").
MOVEMENT_OWNERS: tuple[str, ...] = tuple(o.name for o in MOVEMENT_OWNER_TABLE)


class UnknownMovementOwner(KeyError):
    """An owner name that is not in :data:`MOVEMENT_OWNER_TABLE`.

    Raised rather than tolerated: an unknown owner has no place in the ladder,
    so it could neither be refused nor preempted correctly, and a typo'd owner
    name is exactly the bug this class exists to make impossible.
    """


def _priority(owner: str) -> tuple[int, int]:
    try:
        entry = OWNERS_BY_NAME[owner]
    except KeyError as exc:  # pragma: no cover - defensive, see the docstring
        raise UnknownMovementOwner(owner) from exc
    return (entry.klass, entry.rank)


@dataclass(frozen=True)
class MovementToken:
    """Proof that the bearer holds the wheel.

    `id` is a plain incrementing integer. It is NOT a security boundary and is
    not meant to be one — the only holders of a token are our own layers in
    our own process, and the thing being defended against is one of them
    posting a task it was refused (a bug), not an attacker forging one. An
    incrementing id plus the owner name is enough to catch that, costs nothing
    and reads clearly in the log.
    """

    id: int
    owner: str
    reason: str
    tick: int


@dataclass
class MovementWheel:
    """One owner of bridge-task movement at a time, enforced, and logged.

    THE RULES (the operator's spec, implemented literally):

    * ``token = wheel.acquire(owner, reason)`` returns a token, or ``None``.
      A refusal means **the caller must do nothing this tick** — no task, no
      timers, no goal pick, no commentary.
    * Higher priority than the current holder wins and the current holder is
      PREEMPTED (its token stops working and its preempt hook runs). The same
      owner asking again simply renews and gets its token back. Lower priority
      is refused.
    * ``wheel.release(token)`` when that owner's work completes, fails or times
      out. It returns the name of the highest-priority owner that was refused
      while the wheel was held, so the log says who is next in line.
    * Every posted task carries its token and `main._execute_action` refuses to
      post when the token is not the current holder's.
    * On ``mission.active`` false→true the wheel goes to ``mission``
      unconditionally. On true→false ``mission`` releases.
    * Death, a cutscene, a protagonist switch, a checkpoint reload, or
      ``control_enabled`` false: the holder is forced to ``idle`` and whatever
      it was running is cancelled through its preempt hook.
    * Every acquire, release, renew, refusal, preemption and forced idle is
      logged with owner, reason and tick. This log is how a deadlock gets
      diagnosed on stream, so it is complete and greppable rather than tidy:
      grep ``wheel``.

    LEASES. A token is either a **one-tick lease** (the default) or an **open
    lease** (``lease_ticks=None``). The reflex layer is a set of pure functions
    re-evaluated from scratch every tick, so it takes one-tick leases and
    simply asks again next tick if it still needs the wheel; nothing there can
    forget to release. The layers that own something across many ticks — a
    locked roam goal, an active mission, the day plan's trip to a marker, the
    governor's scenic park — take an open lease and release explicitly. A
    missing ``release()`` on an open lease therefore cannot wedge the show:
    everything above that owner can still preempt it, and the owner itself can
    still renew. Holding one for a very long time is reported as a WARNING
    rather than force-released, because force-releasing mid-mission would be a
    worse failure than the one being reported.

    ONE TASK PER TICK, STRUCTURALLY. Priority alone does not give that: a
    high-priority layer that runs late in the tick could preempt a low one that
    has already posted, and both tasks would be on the wire. So the wheel also
    remembers that a task was posted this tick (``mark_posted``) and refuses
    every acquire by a DIFFERENT owner for the rest of it, whatever the
    priority. The ladder is therefore enforced by the ORDER the tick reaches
    the layers in, and :data:`MOVEMENT_OWNER_TABLE` is that order written down.
    """

    clock: Any = time.monotonic
    #: How long a single holder may keep an open lease before the log starts
    #: complaining. Not a release: see the class docstring.
    long_hold_warn_s: float = 300.0

    _holder: MovementToken | None = None
    _lease_until: int | None = None
    _next_id: int = 0
    _tick: int = 0
    _held_since: float = 0.0
    _warned_at: float = 0.0
    _posted_tick: int = -1
    _posted_owner: str | None = None
    _noted_reflex_tick: int = -1
    _waiting: dict[str, str] = field(default_factory=dict)
    _hooks: dict[str, Any] = field(default_factory=dict)
    _mission_active: bool = False
    _forced_idle: bool = False

    # -- wiring ----------------------------------------------------------------

    def on_preempt(self, owner: str, hook: Any) -> None:
        """Register what to cancel when `owner` loses the wheel.

        The wheel cannot reach the roam engine or the activity runner from
        here without an import cycle, and it should not try: "what does losing
        the wheel mean for this layer" is that layer's business. `main` wires
        the hooks in its constructor. The hook is called with
        ``(by: str, reason: str)`` AFTER the new holder is installed, so a hook
        that touches the wheel sees the new truth.
        """
        _priority(owner)  # reject a typo at wiring time, not at 3 Hz
        self._hooks[owner] = hook

    # -- the tick --------------------------------------------------------------

    def begin_tick(self, tick: int | None = None) -> None:
        """Open a movement tick. Expires one-tick leases; nothing else."""
        self._tick = self._tick + 1 if tick is None else tick
        holder = self._holder
        if holder is not None and self._lease_until is not None and self._tick > self._lease_until:
            # Not a preemption: the owner simply stopped asking. No hook runs —
            # nothing was taken from it.
            log.debug(
                "wheel lease expired",
                extra={"kv": {"owner": holder.owner, "reason": holder.reason, "tick": self._tick}},
            )
            self._holder = None
            self._lease_until = None
            self._forced_idle = False
            self._waiting.clear()

    def end_tick(self) -> None:
        """Close the movement tick. Records who ended it holding the wheel.

        A tick that ends with nobody holding and nobody having posted is the
        exact shape of the observed "he just stands there" failure, so it is
        recorded rather than passed over in silence.
        """
        if self._holder is None:
            if self._posted_tick != self._tick:
                log.debug("wheel idle: nobody moved him this tick", extra={"kv": {"tick": self._tick}})
            return
        held_for = self.clock() - self._held_since
        if (
            self._lease_until is None
            and held_for >= self.long_hold_warn_s
            and self.clock() - self._warned_at >= self.long_hold_warn_s
        ):
            self._warned_at = self.clock()
            log.warning(
                "wheel held for a long time; nothing below this owner can move him",
                extra={
                    "kv": {
                        "owner": self._holder.owner,
                        "reason": self._holder.reason,
                        "held_for_s": round(held_for, 1),
                        "tick": self._tick,
                        "waiting": ",".join(sorted(self._waiting)) or None,
                    }
                },
            )

    # -- acquire / release -----------------------------------------------------

    def acquire(
        self, owner: str, reason: str = "", *, lease_ticks: int | None = 1
    ) -> MovementToken | None:
        """Take (or renew) the wheel for `owner`. `None` means REFUSED.

        A refusal is an instruction: post nothing, start no timer, pick no
        goal, say nothing about it. `lease_ticks=None` is an open lease held
        until `release()` or a preemption.
        """
        priority = _priority(owner)
        holder = self._holder

        if self._posted_tick == self._tick and self._posted_owner != owner:
            # A bridge task is already on the wire this tick and it belongs to
            # somebody else. Priority does not get to undo that: preempting it
            # now would put two orders in front of the engine in one tick,
            # which is the whole failure this class exists to stop.
            return self._refuse(owner, reason, "a task is already posted this tick", holder)

        if holder is not None and holder.owner == owner:
            renewed = MovementToken(id=holder.id, owner=owner, reason=reason or holder.reason, tick=holder.tick)
            self._holder = renewed
            self._lease_until = None if lease_ticks is None else self._tick + lease_ticks - 1
            self._waiting.pop(owner, None)
            log.debug(
                "wheel renewed",
                extra={"kv": {"owner": owner, "reason": reason, "tick": self._tick, "token": renewed.id}},
            )
            return renewed

        if holder is not None and priority < _priority(holder.owner):
            return self._refuse(owner, reason, "outranked", holder)

        preempted = holder
        token = self._grant(owner, reason, lease_ticks)
        if preempted is not None:
            log.info(
                "wheel preempted",
                extra={
                    "kv": {
                        "owner": preempted.owner,
                        "held_reason": preempted.reason,
                        "by": owner,
                        "reason": reason,
                        "held_for_s": round(self.clock() - self._held_since, 1),
                        "tick": self._tick,
                    }
                },
            )
            self._fire_hook(preempted.owner, owner, reason)
        return token

    def release(self, token: MovementToken | None) -> str | None:
        """Give the wheel back. Returns the next-highest owner that was refused.

        A stale token (the owner was preempted and did not notice) releases
        nothing and is logged — that is a bug worth seeing, not a crash.
        """
        if token is None:
            return None
        if self._holder is None or self._holder.id != token.id:
            log.debug(
                "wheel release ignored: not the holder",
                extra={
                    "kv": {
                        "owner": token.owner,
                        "token": token.id,
                        "held_by": None if self._holder is None else self._holder.owner,
                        "tick": self._tick,
                    }
                },
            )
            return None
        return self._free(token.owner, "released")

    def release_owner(self, owner: str) -> str | None:
        """Release whatever `owner` is holding, if it is holding anything."""
        if self._holder is None or self._holder.owner != owner:
            return None
        return self._free(owner, "released")

    def holds(self, token: MovementToken | None) -> bool:
        """Is this token the current holder's? The check `_execute_action` makes."""
        return token is not None and self._holder is not None and self._holder.id == token.id

    def token_for(self, owner: str) -> MovementToken | None:
        """The live token for `owner`, or None when it is not holding."""
        if self._holder is not None and self._holder.owner == owner:
            return self._holder
        return None

    # -- the two overrides -----------------------------------------------------

    def mission_active(self, active: bool, reason: str = "mission.active") -> MovementToken | None:
        """The mission flag drives the wheel directly (the operator's rule).

        On false→true the wheel goes to `mission` UNCONDITIONALLY: a story
        mission starting outranks anything free roam had in mind, and whatever
        held the wheel is preempted and cancelled. While it stays true the
        mission keeps the wheel whenever nothing above it wants it. On
        true→false `mission` releases.
        """
        if active and not self._mission_active:
            self._mission_active = True
            preempted = self._holder
            token = self._grant("mission", reason, None)
            log.info(
                "wheel: mission started, taking the wheel unconditionally",
                extra={
                    "kv": {
                        "preempted": None if preempted is None else preempted.owner,
                        "reason": reason,
                        "tick": self._tick,
                    }
                },
            )
            if preempted is not None:
                self._fire_hook(preempted.owner, "mission", reason)
            return token
        if active:
            return self.acquire("mission", reason, lease_ticks=None)
        if self._mission_active:
            self._mission_active = False
            self.release_owner("mission")
        return None

    def force_idle(self, reason: str) -> None:
        """The game owns the controls: nobody drives, and what was running dies.

        Death, arrest, a cutscene, a protagonist switch, a checkpoint reload
        and `control_enabled == false` all mean the same thing — a task posted
        now reaches nobody (`main._execute_action` refuses them for exactly
        this reason). The holder is dropped to `idle` so that the ladder starts
        from scratch the moment control comes back.
        """
        if self._forced_idle and self._holder is not None and self._holder.owner == "idle":
            self._lease_until = None
            return
        preempted = self._holder
        self._grant("idle", reason, None)
        self._forced_idle = True
        log.info(
            "wheel forced to idle",
            extra={
                "kv": {
                    "reason": reason,
                    "cancelled": None if preempted is None else preempted.owner,
                    "tick": self._tick,
                }
            },
        )
        if preempted is not None and preempted.owner != "idle":
            self._fire_hook(preempted.owner, "idle", reason)

    # -- posting ---------------------------------------------------------------

    def mark_posted(self, token: MovementToken, action_type: str) -> None:
        """`main._execute_action` calls this the moment a bridge task goes out.

        This is what makes "no two movement tasks from different owners in the
        same tick" structural rather than a consequence of call order.
        """
        self._posted_tick = self._tick
        self._posted_owner = token.owner
        log.debug(
            "wheel: task posted",
            extra={"kv": {"owner": token.owner, "type": action_type, "tick": self._tick}},
        )

    def note(self, owner: str, reason: str) -> None:
        """A reflex acted this tick WITHOUT posting a bridge task.

        The stuck ladder's first rungs are `reverse_out` / `swerve` keypresses
        and `POST /unstick`; none of them preempts a running task, so none of
        them takes the wheel — but the planners that run later in the tick
        still must not post navigation over a recovery in progress, and that is
        what `taken_by_reflex()` tells them. Logged like an acquire so the two
        are equally greppable.
        """
        if OWNERS_BY_NAME[owner].klass == WHEEL_REFLEX:
            self._noted_reflex_tick = self._tick
        log.debug(
            "wheel note: reflex acted without a task",
            extra={"kv": {"owner": owner, "reason": reason, "tick": self._tick}},
        )

    # -- reporting -------------------------------------------------------------

    @property
    def owner(self) -> str | None:
        """Who holds the wheel right now, if anyone."""
        return None if self._holder is None else self._holder.owner

    @property
    def reason(self) -> str:
        return "" if self._holder is None else self._holder.reason

    @property
    def tick(self) -> int:
        return self._tick

    @property
    def next_in_line(self) -> str | None:
        """The highest-priority owner refused while the current holder has held it.

        Deliberately NOT reset per tick: "who is waiting on this holder" is a
        property of the HOLD, and a refusal on the tick a mission started is
        still the answer to "who gets it back" ninety seconds later when the
        mission ends. Cleared when the wheel changes hands.
        """
        if not self._waiting:
            return None
        return max(self._waiting, key=_priority)

    def waiting(self) -> dict[str, str]:
        return dict(self._waiting)

    def taken(self) -> bool:
        return self._holder is not None

    @property
    def posted_this_tick(self) -> bool:
        """Has a bridge task already gone out on this tick?

        The read-only half of what `acquire()` already enforces internally, for
        the one caller that does NOT go through `acquire()`: CONTRACTS v1.13's
        phone reflex posts `reject_call`/`answer_call` without a token (they
        move nobody, so they take no wheel), but `POST /task` is still one slot
        at a time bridge-side — so it must not fire on a tick where the
        survival ladder has just posted, or it would preempt the very action
        that was chosen over it. Deferring costs one poll interval and the
        phone is still ringing.
        """
        return self._posted_tick == self._tick

    def taken_by_reflex(self) -> bool:
        """Did the reflex layer act this tick? The successor to
        `main._threat_has_the_wheel`, and it means the same thing to the three
        planners that read it: a survival or physical-recovery action was
        issued milliseconds ago and `state.last_task` cannot show it yet, so do
        not post navigation over the top of it."""
        if self._noted_reflex_tick == self._tick:
            return True
        holder = self._holder
        return holder is not None and OWNERS_BY_NAME[holder.owner].klass == WHEEL_REFLEX

    def reset(self) -> None:
        self._holder = None
        self._lease_until = None
        self._waiting.clear()
        self._posted_tick = -1
        self._posted_owner = None
        self._noted_reflex_tick = -1
        self._mission_active = False
        self._forced_idle = False
        self._held_since = self.clock()
        self._warned_at = 0.0

    # -- internals -------------------------------------------------------------

    def _grant(self, owner: str, reason: str, lease_ticks: int | None) -> MovementToken:
        # A new holder starts with an empty queue: everyone refused by the
        # previous one gets to ask this one on its own merits.
        self._waiting.clear()
        self._next_id += 1
        token = MovementToken(id=self._next_id, owner=owner, reason=reason, tick=self._tick)
        self._holder = token
        self._lease_until = None if lease_ticks is None else self._tick + lease_ticks - 1
        self._held_since = self.clock()
        self._warned_at = 0.0
        self._forced_idle = owner == "idle" and self._forced_idle
        self._waiting.pop(owner, None)
        log.info(
            "wheel acquired",
            extra={
                "kv": {
                    "owner": owner,
                    "class": WHEEL_CLASS_NAMES[OWNERS_BY_NAME[owner].klass],
                    "reason": reason,
                    "tick": self._tick,
                    "token": token.id,
                    "lease": "open" if lease_ticks is None else lease_ticks,
                }
            },
        )
        return token

    def _refuse(
        self, owner: str, reason: str, why: str, holder: MovementToken | None
    ) -> None:
        self._waiting[owner] = reason
        log.info(
            "wheel refused",
            extra={
                "kv": {
                    "owner": owner,
                    "reason": reason,
                    "why": why,
                    "held_by": None if holder is None else holder.owner,
                    "held_reason": None if holder is None else holder.reason,
                    "posted_by": self._posted_owner if self._posted_tick == self._tick else None,
                    "tick": self._tick,
                }
            },
        )
        return None

    def _free(self, owner: str, why: str) -> str | None:
        held_for = self.clock() - self._held_since
        self._holder = None
        self._lease_until = None
        self._forced_idle = False
        nxt = self.next_in_line
        self._waiting.clear()
        log.info(
            "wheel released",
            extra={
                "kv": {
                    "owner": owner,
                    "why": why,
                    "held_for_s": round(held_for, 1),
                    "next_in_line": nxt,
                    "tick": self._tick,
                }
            },
        )
        return nxt

    def _fire_hook(self, owner: str, by: str, reason: str) -> None:
        hook = self._hooks.get(owner)
        if hook is None:
            return
        try:
            hook(by, reason)
        except Exception as exc:  # pragma: no cover - a hook must never take the loop
            log.error(
                "wheel preempt hook failed",
                extra={"kv": {"owner": owner, "by": by, "error": f"{type(exc).__name__}: {exc}"[:160]}},
            )
