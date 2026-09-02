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
            action={"type": "wander_drive", "params": {"style": style}},
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


# --- controller ownership (brief 55) -----------------------------------------

#: Every layer that can post a movement task, in the order a tick reaches them.
#: Used only for logging and for the assertion in the tests that nobody claims
#: under a name the log will not explain.
MOVEMENT_OWNERS: tuple[str, ...] = (
    "threat",  # recovery.threat_action — survival, outranks everything
    "vehicle",  # this module — drive away / get the car moving
    "physical",  # StuckDetector / flipped_action — unwedge, get out of a flip
    "stranded",  # StrandedEscalator — find a car at all
    "governor",  # governor L2 wander / L3 scenic park
    "brain",  # a tactical/director decision
    "idle",  # IdlePicker
    "day_plan",  # DayPlanner's trip to a mission marker
    "activity",  # ActivityRunner's current step
    "mission",  # MissionFollower's objective navigation
)


@dataclass
class MovementWheel:
    """Exactly one owner of movement per tick, and a log line whenever it changes.

    `main._threat_has_the_wheel` was the seed of this: a single bool set by the
    reflex layer so the three planners that run later in the SAME tick stand
    down (their own "someone else has the wheel" checks read `state.last_task`,
    which predates the reflex's POST and therefore cannot see it). It worked,
    and it was invisible: when the agent did nothing there was no way to tell which
    layer had the wheel and declined to use it, so "he just sits there" could
    only be diagnosed by guessing.

    This makes it explicit. First claim in a tick wins, which is exactly the
    existing precedence because the tick already reaches the layers in priority
    order (reflex → brain → day plan → activities → mission). A refused claim is
    logged at DEBUG; a CHANGE of owner between ticks is logged at INFO with the
    reason, so the log answers "who was driving, and why did nobody else get a
    turn" without a rerun.
    """

    clock: Any = time.monotonic
    _owner: str | None = None
    _reason: str = ""
    _last_owner: str | None = None
    _owner_since: float = 0.0

    def begin_tick(self) -> None:
        """Called once per poll tick, before any layer may claim."""
        self._owner = None
        self._reason = ""

    def claim(self, owner: str, reason: str = "") -> bool:
        """Try to take the wheel for this tick. True = granted."""
        if self._owner is not None:
            log.debug(
                "movement claim refused",
                extra={"kv": {"wanted": owner, "held_by": self._owner, "reason": reason}},
            )
            return False
        self._owner = owner
        self._reason = reason
        now = self.clock()
        if owner != self._last_owner:
            log.info(
                "movement owner changed",
                extra={
                    "kv": {
                        "from": self._last_owner,
                        "to": owner,
                        "reason": reason,
                        "held_for_s": round(now - self._owner_since, 1) if self._last_owner else None,
                    }
                },
            )
            self._last_owner = owner
            self._owner_since = now
        return True

    def force(self, owner: str, reason: str = "") -> None:
        """Record a movement post from a layer that is not arbitrated here.

        The brain's own decision, the idle picker, the day plan, the activity
        runner and the mission follower each already have their own stand-down
        rules (`main._threat_has_the_wheel`, `MissionFollower
        ._someone_else_has_the_wheel`, `ActivityRunner.bind_step_task`), and
        those rules are older and better tested than this class. Rewriting
        their precedence from here would be a silent behaviour change, so they
        REGISTER instead of asking permission — and if that registration
        collides with a claim already made this tick, it is logged as a
        conflict at WARNING. That is the diagnosis the old single bool could
        never produce: "two layers posted movement on the same tick, here is
        which two".
        """
        if self._owner is not None and self._owner != owner:
            log.warning(
                "two layers posted movement on the same tick",
                extra={
                    "kv": {
                        "held_by": self._owner,
                        "held_reason": self._reason,
                        "also_posted": owner,
                        "reason": reason,
                    }
                },
            )
            return
        self.claim(owner, reason)

    @property
    def owner(self) -> str | None:
        """Who claimed movement on the tick in progress, if anyone."""
        return self._owner

    @property
    def reason(self) -> str:
        return self._reason

    def taken(self) -> bool:
        return self._owner is not None

    def taken_by_reflex(self) -> bool:
        """Did a reflex-layer owner take this tick?

        This is the successor to `main._threat_has_the_wheel` and it means the
        same thing to the planners that read it: a survival or vehicle-recovery
        action was posted milliseconds ago and `state.last_task` cannot show it
        yet, so do not post navigation over the top of it.
        """
        return self._owner in ("threat", "vehicle", "physical")

    def end_tick(self) -> None:
        """Called once per tick after the last layer has had its turn.

        A tick where nobody claimed is the interesting one — it is the shape of
        the observed failure — so it is recorded as a change to `idle_tick`
        rather than silently leaving the previous owner on the board.
        """
        if self._owner is None and self._last_owner != "nobody":
            log.debug(
                "movement owner changed",
                extra={"kv": {"from": self._last_owner, "to": "nobody"}},
            )
            self._last_owner = "nobody"
            self._owner_since = self.clock()

    def reset(self) -> None:
        self._owner = None
        self._reason = ""
        self._last_owner = None
        self._owner_since = 0.0
