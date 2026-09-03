"""Recovery handlers: stuck, flipped, stranded, game restart, bridge-down, API backoff.

Each handler is small state + a decision: what to do NOW, without a model call.
They are the harness's spinal reflexes — they keep the show alive when the
brain, the bridge, or the network is having a moment.

The failure modes the real machine implies, and who covers them:

===========================  =================================================
game crashed / relaunching   BridgeDownTracker (connection refused → backoff →
                             one `bridge_down` event) + GameRestartDetector
                             (tick counter went backwards ⇒ new game process ⇒
                             every stateful observer must be reset before it
                             invents a death or a wanted change)
bridge loaded but online      OnlineSessionActiveError, handled in main
bridge up, game not ticking   BridgeStallTracker (v1.2 transient 503s:
                              not_ready / game_thread_stalled / queue_full, and
                              any code this version does not know) — wait and
                              retry, never a bridge_down event
API rate limit / overload     ApiBackoff with a per-cause floor; the reflex
                             layer drives while the brain is blocked
Supabase down                 events.SupabaseWriter offline queue (locked,
                             capped, flushed on reconnect)
stuck on geometry             StuckDetector → reverse_out → bridge `unstick`
                              (vehicles only: it needs `state.vehicle` AND a
                              driving task, so an on-foot pin is invisible
                              to it)
pinned by a task that never   TaskStallDetector → `stop`, then a short refusal
finishes                      to re-post that same task type. Confirmed live:
                              a stray CAT is a `nearby.peds[]` entry with
                              `relationship: "hostile"` (animals are peds in
                              this engine), so `combat_hated_targets_around`
                              never completed — 20 s of RUNNING, 0.2 m of
                              movement, full health, a story mission waiting
flipped                       flipped_action → exit_vehicle
stuck in the water             WaterEscalator (T9, findings.md R1/R5) → exit_vehicle
(vehicle.in_water > 10 s)      once the timeout clears, then one walk toward
                               player.last_outdoor as the best available "land"
                               bearing - /state has no shoreline data at all
car incoming while on foot     RoadDodge (T9) → walk_to a point stepped away from
                               a closing NPC vehicle, derived from two ticks of
                               nearby.vehicles[].pos (no lane geometry, no
                               per-vehicle velocity in /state either)
jacked out of his car          threat_action's fight_ped rung answers the fight
                               (threat.being_jacked_by); JackHandoffGate (T9)
                               then holds `stranded` off of the SAME tick so
                               roam's own take_my_car_back (behavior.roam.py)
                               gets first refusal at the same car instead of
                               `stranded` grabbing whatever "any" car is nearest
stranded on foot              StrandedEscalator → widening vehicle search
attacked / shot at / cornered DamageTracker (effective HP falling inside a
                              short window ⇒ he is being hit RIGHT NOW,
                              whatever the engine's relationship groups say)
                              → threat_action → reflex, no model call: the
                              survival ladder (break contact when hurt >
                              **leave, if he is in a working car and parked** >
                              fight back on foot at whatever is in reach >
                              fight a close hostile when healthy, or leave if
                              the car works > flee_police, and only outside a
                              mission) + ThreatLatch, so the
                              chosen action is POSTED once instead of every
                              tick (a re-post preempts and restarts the engine
                              task, which is what made him stutter and never
                              land a shot)
screen capture wedged         OffLoopGrab → the grab runs on a daemon worker
                              with a hard deadline, so a dxcam device stuck in
                              its own recovery loop cannot freeze the tick;
                              after a run of failures capture is given up on
                              and the show continues without screenshots
model answered, our schema    classify_api_failure → "invalid_output": a local
said no                       rejection is NOT an API outage and must not be
                              charged to the outage backoff
died / arrested               DeathArrestRecovery → suppress tasks, wait for
                              the game's own respawn, clear stale mission/goal
                              state
script thread frozen on a     BlockingScreenWatchdog → detects `state.tick`
blocking screen (pause menu,  not advancing from OUTSIDE the frozen script
MISSION FAILED, retry prompt) thread, presses a bounded, escalating key
                              sequence via real SendInput to clear it
===========================  =================================================

These close the exact gaps this module used to admit: it handled brain-call
failures, bridge stalls and API backoff, but NOT in-game death, arrest,
mission failure, or a script thread that stops ticking outright. Observed
live, twice: (1) a firefight left the agent standing still because the only
thing driving combat was a 1-2 s model call; a death that flipped
`player.dead` / `mission.active` was never detected by anything, so he sat
there dead with no recovery. (2) A mission failure put the game on a
"MISSION FAILED" / retry screen and the SHVDN script thread stopped ticking
entirely — `/health.tick_hz` pinned at 0.0, `/state` frozen — and
`System.Windows.Forms.SendKeys` was confirmed to do nothing at all (GTA V
reads raw/DirectInput and silently discards synthetic window messages); only
a real SendInput keypress gets out of that. (3) A pedestrian walked up on the
freeway and beat him to death while he stood there — the combat reflex keyed
only off `nearby.peds[].relationship == "hostile"`, and a ped that simply
starts swinging is usually still `neutral` in the snapshot (that field is the
engine's relationship GROUP, not "is currently hitting me"), so nothing ever
fired. His own recorded thought at the time: "Something hostile nearby—cat,
weird—but no objective blip yet. Hold position." :class:`DamageTracker` is
the fix: losing health is the one signal that cannot lie about being under
attack, and it is already in every `/state`.
"""

from __future__ import annotations

import math
from collections import deque
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..bridge_client import (
    BridgeApiError,
    BridgeClient,
    BridgeDownError,
    BridgeTransientError,
    GameState,
)
from ..logsetup import get_logger
from ..perception import Delta
from .vehicle import SEATED_SPEED_MPS, vehicle_can_drive_away

log = get_logger("wasted.recovery")


# --- stuck / flipped / stranded ----------------------------------------------


@dataclass
class StuckDetector:
    """Speed ~0 for >20 s while a drive task runs → escalate: reverse_out, then unstick."""

    threshold_s: float = 20.0
    #: Minimum gap between two /unstick ATTEMPTS (moved or refused). Without this the
    #: detector returned "unstick" on every tick while the car stayed stopped, and the
    #: real server log shows 7 nudges in 34 s - five of them inside 1.35 s - for ~17.6 m
    #: of cumulative displacement. That is a teleport in all but name, and CLAUDE.md
    #: rule 5 allows exactly one thing: "an unstick nudge of a few meters when wedged".
    unstick_cooldown_s: float = 30.0
    #: Hard cap per stuck episode. Two nudges that do not free the car mean the car is
    #: not merely wedged; hand the problem back to the brain instead of drifting away.
    max_nudges_per_episode: int = 2
    _reverse_tried_at: float | None = None
    _last_unstick_at: float | None = None
    _episode_nudges: int = 0
    clock: Any = time.monotonic

    def check(self, state: GameState) -> str | None:
        """Returns 'reverse_out', 'unstick', or None."""
        v = state.vehicle
        driving = state.last_task.status == "running" and state.last_task.type in (
            "drive_to",
            "wander_drive",
            "flee_police",
        )
        if not (v and driving and v.stopped_for_s > self.threshold_s):
            # Not stuck (or moving again): the episode is over, reset the ladder.
            self._reverse_tried_at = None
            self._episode_nudges = 0
            return None
        now = self.clock()
        if self._reverse_tried_at is None or now - self._reverse_tried_at > 45.0:
            self._reverse_tried_at = now
            return "reverse_out"
        # reverse_out already tried recently and we're still parked on geometry:
        # ask the bridge for the logged, contract-limited nudge - but rate-limited,
        # and never more than max_nudges_per_episode until the car actually moves.
        if self._episode_nudges >= self.max_nudges_per_episode:
            return None
        if (
            self._last_unstick_at is not None
            and now - self._last_unstick_at < self.unstick_cooldown_s
        ):
            return None
        return "unstick"

    def try_unstick(self, bridge: BridgeClient) -> float | None:
        """Calls /unstick; returns meters moved, or None when it did not happen.

        A 409 ``unstick_conditions_not_met`` is normal and final — the bridge
        self-enforces its own preconditions. A transient 503 (`not_ready`,
        `game_thread_stalled`, `queue_full`, or a code this version has never
        heard of) just means "not now"; the detector will ask again on the next
        tick. Neither is worth killing the loop for, and a three-metre nudge
        never is.
        """
        # Stamp the ATTEMPT (not just a success): a refused nudge still counts against the
        # cooldown, otherwise a 409 loop would hammer the bridge every tick.
        self._last_unstick_at = self.clock()
        self._episode_nudges += 1
        try:
            result = bridge.unstick()
            return result.distance_m if result.moved else None
        except BridgeTransientError as exc:
            log.info(
                "unstick deferred: bridge not ready",
                extra={"kv": {"error": exc.error, "status": exc.status}},
            )
            return None
        except BridgeApiError as exc:
            if exc.status == 409:
                log.info("unstick refused by bridge", extra={"kv": {"error": exc.error}})
                return None
            log.warning(
                "unstick failed with an unexpected bridge error; skipping the nudge",
                extra={"kv": {"status": exc.status, "error": exc.error}},
            )
            return None


# --- a task that runs forever pins him (on foot OR in a car) ------------------
#
# Confirmed live, measured off /state at 4 s intervals:
#
#     last_task = combat_hated_targets_around / RUNNING   (the whole window)
#     player.pos moved 0.2 m in 20 s, in_vehicle = False
#     health 200 (nothing was damaging him), wanted 0, mission.active = true
#
# Root cause of that particular instance: a stray CAT appears in
# `nearby.peds[]` with `relationship: "hostile"` — animals are peds in this
# engine — so the bridge's done-check for `combat_hated_targets_around` ("done
# when no hated targets remain in radius", CONTRACTS §1) never came true, and
# he stood in a hedge fighting a cat while a story mission waited. Filtering
# animals out of `nearby.peds` removes that trigger and is the bridge's job;
# the DEADLOCK CLASS is this module's, because any engine task that never
# completes pins him the same way and nothing here noticed for 20+ seconds.
#
# This is NOT a duplicate of StuckDetector above: that one needs
# `state.vehicle` AND a driving task, so it cannot see an on-foot pin at all,
# and it answers with reverse_out/unstick (move the car) where this answers
# with `stop` (abandon the task and let the ordinary layers choose again).

#: How far the player may move and still count as not moving. The measured
#: deadlock was 0.2 m in 20 s. 2 m is comfortably above the shuffle an idle
#: ped animation produces and the metre a combat task's aim-stance costs, and
#: far below anything a walk, a drive or a real fight covers in 20 s.
STALL_MOVE_M = 2.0

#: How long he must stay inside that circle, with a task RUNNING, before it
#: counts as deadlocked rather than merely slow. Long enough to sit out a
#: traffic light, an engine-side re-path or a door animation; short enough
#: that this is the most a story mission ever loses to a pin. Deliberately the
#: same 20 s StuckDetector uses for the vehicle case, for the same reason.
STALL_WINDOW_S = 20.0

#: Minimum gap between two interventions. One `stop` per half-minute at the
#: very worst: past that this is not a stall to be broken every tick, it is a
#: situation, and the ordinary layers (and the brain) should own it.
STALL_INTERVENTION_GAP_S = 30.0

#: How long the exact task type that just deadlocked him is refused
#: afterwards. Longer than STALL_INTERVENTION_GAP_S on purpose: if the refusal
#: expired first, whichever layer chose that task would simply choose it again
#: and walk straight back into the identical deadlock — one `stop` every 30 s,
#: forever. Short enough that a type blocked by a false positive is available
#: again inside one activity step.
STALL_TYPE_BLOCK_S = 45.0

#: Task types where standing still IS the task, so a stationary window proves
#: nothing: `seek_cover` (the entire point is to be behind cover and stay
#: there), `follow_entity` (a stationary target legitimately means a
#: stationary follower, and the bridge fails it on its own with `target_lost`
#: when the entity is gone), and `stop` / `set_waypoint`, which do not move
#: him at all. Every other type in CONTRACTS §1's table is supposed to change
#: where he is.
STATIONARY_TASK_TYPES: frozenset[str] = frozenset(
    {"seek_cover", "follow_entity", "stop", "set_waypoint"}
)



#: A task the GAME cleared is a "not now", and re-posting it instantly is a storm.
#: Measured live 2026-09-03 (bridge log): `enter_nearest_vehicle` started and
#: `failed: cleared_by_game` ~1 s later, then was re-posted within 300 ms by
#: whichever owner got the wheel next — day plan, brain, roam in turn — with an
#: empty car 2.8 m away, for minutes. The game clears ped tasks for reasons the
#: harness cannot see (a ringing phone taking the ped, a scripted moment, a
#: cutscene fade). Backing off is the only honest response; the cause is not
#: in our hands. First clear: a short pause; repeats: longer, capped.
CLEARED_BACKOFF_FIRST_S = 4.0
CLEARED_BACKOFF_MAX_S = 20.0
#: Forget the history once the type has been quiet this long — a clear from ten
#: minutes ago says nothing about now.
CLEARED_BACKOFF_FORGET_S = 60.0


@dataclass
class ClearedByGameBackoff:
    """Per-task-type backoff after the game itself clears a task.

    Fed `state.last_task` every tick; consulted by the action funnel before any
    bridge task is posted. It never blocks a DIFFERENT type, so a cleared
    `enter_nearest_vehicle` still lets `walk_to` or `look_around` through —
    which is exactly the variety a stuck ped needs.
    """

    clock: Any = time.monotonic
    _seen_id: str | None = None
    _until: dict[str, float] = field(default_factory=dict)
    _strikes: dict[str, int] = field(default_factory=dict)
    _last_clear: dict[str, float] = field(default_factory=dict)

    def feed(self, state: GameState) -> str | None:
        """Record a fresh `cleared_by_game` failure. Returns the type when one
        was just recorded (for logging), else None."""
        task = state.last_task
        if task is None or task.id is None or task.id == self._seen_id:
            return None
        self._seen_id = task.id
        if task.status != "failed" or (task.detail or "") != "cleared_by_game":
            return None
        now = self.clock()
        t = task.type or ""
        if now - self._last_clear.get(t, -1e12) > CLEARED_BACKOFF_FORGET_S:
            self._strikes[t] = 0
        self._strikes[t] = self._strikes.get(t, 0) + 1
        self._last_clear[t] = now
        wait = min(CLEARED_BACKOFF_FIRST_S * (2 ** (self._strikes[t] - 1)), CLEARED_BACKOFF_MAX_S)
        self._until[t] = now + wait
        log.warning(
            "the game cleared a task; backing off that type",
            extra={"kv": {"type": t, "strike": self._strikes[t], "backoff_s": round(wait, 1)}},
        )
        return t

    def refuses(self, action_type: str) -> float:
        """Seconds of backoff remaining for this type, or 0.0 when it may post."""
        return max(0.0, self._until.get(action_type, 0.0) - self.clock())

    def reset(self) -> None:
        self._until.clear()
        self._strikes.clear()
        self._last_clear.clear()

@dataclass
class TaskStallDetector:
    """A RUNNING task that has not moved him for :data:`STALL_WINDOW_S`.

    Position across ticks is kept here rather than read off
    :class:`perception.Delta`, which carries no movement signal at all (it has
    `died`, `wanted_*`, task and mission transitions, `big_health_drop` — no
    position, and `perception.py` is not this work package's to widen). The
    idiom is the same self-contained previous-tick memory
    :class:`GameRestartDetector` and :class:`BlockingScreenWatchdog` already
    use.

    The measure is an ANCHOR, not a sum of per-tick steps: "he has not been
    more than :data:`STALL_MOVE_M` from where he was N seconds ago". A ped
    shuffling on the spot can accumulate metres of per-tick movement while
    going nowhere, which is exactly the thing being detected.

    Answering with `stop` and nothing else is deliberate. `stop` is CONTRACTS
    §1's own "clear current task → idle"; once the task is gone the layers
    that were already waiting for the wheel (the day plan, the activity
    runner, the mission follower, the brain) choose something on the very next
    tick. Choosing FOR them here would be a second, competing planner.
    """

    move_m: float = STALL_MOVE_M
    window_s: float = STALL_WINDOW_S
    intervention_gap_s: float = STALL_INTERVENTION_GAP_S
    block_s: float = STALL_TYPE_BLOCK_S
    clock: Any = time.monotonic
    #: Where he was when the current no-movement window started, and when.
    _anchor: tuple[float, float] | None = None
    _anchor_at: float = 0.0
    _task_id: str | None = None
    _last_intervention_at: float = 0.0
    _blocked_type: str | None = None
    _blocked_until: float = 0.0
    _block_logged: bool = False

    def feed(
        self, state: GameState, *, under_attack: bool = False, suspended: bool = False
    ) -> str | None:
        """One tick. Returns the stalled task type when it should be abandoned.

        `under_attack` (:meth:`DamageTracker.feed`'s answer) restarts the
        window: HP moving means the fight is real and going somewhere, and
        `stop` is the wrong answer to being shot — the survival ladder owns
        that case. `suspended` is the caller's "he is standing still on
        purpose" — a deliberate `wait` (the brain's own action, and the beat
        in every park-and-watch style activity) or governor L3, which is
        literally asleep in a parked car (CONTRACTS §7).
        """
        now = self.clock()
        p, lt = state.player, state.last_task
        if (
            suspended
            or under_attack
            or p.dead
            or p.arrested
            or state.mission.cutscene_active
            or state.player.switch_in_progress
            or state.mission.retry_in_flight
            or lt.status != "running"
            or lt.type is None
            or lt.type in STATIONARY_TASK_TYPES
        ):
            self._anchor = None
            return None

        if lt.id != self._task_id:
            # A different task: it gets its own full window to show movement.
            self._task_id = lt.id
            self._anchor = None

        here = (p.pos.x, p.pos.y)
        if self._anchor is None or math.dist(here, self._anchor) > self.move_m:
            self._anchor = here
            self._anchor_at = now
            return None
        stalled_for = now - self._anchor_at
        if stalled_for < self.window_s:
            return None
        if now - self._last_intervention_at < self.intervention_gap_s:
            return None

        self._last_intervention_at = now
        self._anchor_at = now  # the next window starts here, not at the old anchor
        self._blocked_type = lt.type
        self._blocked_until = now + self.block_s
        self._block_logged = False
        log.warning(
            "task is running but has pinned him in place; abandoning it with `stop`",
            extra={
                "kv": {
                    "type": lt.type,
                    "task_id": lt.id,
                    "stalled_for_s": round(stalled_for, 1),
                    "moved_under_m": self.move_m,
                    "in_vehicle": p.in_vehicle,
                }
            },
        )
        return lt.type

    def blocked(self, task_type: str) -> bool:
        """Is this the task type that just deadlocked him, still inside its refusal?

        `stop` is never blocked: it is how this detector itself gets out, and
        it cannot pin anyone.
        """
        if self._blocked_type is None or task_type == "stop":
            return False
        if self.clock() >= self._blocked_until:
            self._blocked_type = None
            return False
        if task_type != self._blocked_type:
            return False
        if not self._block_logged:
            self._block_logged = True
            log.info(
                "refusing to re-post the task type that just deadlocked him",
                extra={"kv": {"type": task_type, "for_s": self.block_s}},
            )
        return True

    def reset(self) -> None:
        """Forget everything (new game process behind the same bridge URL)."""
        self._anchor = None
        self._task_id = None
        self._blocked_type = None
        self._block_logged = False


def flipped_action(state: GameState) -> dict[str, Any] | None:
    """Upside-down and not moving → get out (the engine rights nothing for us).

    T9 (findings.md R1/R5) asked this to "right it if the bridge has a lever,
    else exit". CONTRACTS v1.13 §1's task table is the whole set of things
    this bridge can be asked to do (`drive_to`, `walk_to`,
    `enter_nearest_vehicle`, `exit_vehicle`, `wander_drive`, `flee_police`,
    `combat_hated_targets_around`, `seek_cover`, `follow_entity`, `fight_ped`,
    `set_waypoint`, `stop`, `answer_call`, `reject_call`) and none of them
    rights a vehicle — there is no lever. Coordinated with fix-opus-a (who
    owns `bridge/src/TaskEngine.cs`'s driving cases): a `right_vehicle` verb
    over a native such as `SET_VEHICLE_ON_GROUND_PROPERLY` would need a new
    §1 task type, which is a CONTRACTS change this package cannot make
    unilaterally (frozen at v1.13) — proposed as a changelog entry in this
    ticket's report rather than built here. `exit_vehicle` stays the only
    honest answer until that lands.
    """
    v = state.vehicle
    if v and v.upside_down and v.speed < 0.5:
        return {"type": "exit_vehicle", "params": {}}
    return None


#: How long `vehicle.in_water` has to read continuously true before
#: :class:`WaterEscalator` gets him out of it. Long enough that fording a
#: shallow crossing or a bridge's edge is not misread as "stuck"; the
#: contract's `in_water` is measured on the game thread every tick, so this
#: window only needs to absorb the 2-4 Hz poll gap, not the flag's own noise.
WATER_TIMEOUT_S = 10.0

#: After `exit_vehicle` is issued for being stuck in the water, how long the
#: "walk toward known land" rung stays armed even though `/state` can no
#: longer confirm anything (there is no on-foot water flag — CONTRACTS §1
#: only exposes `vehicle.in_water`, and `state.vehicle` itself goes null the
#: moment he is on foot). Generous on purpose: this fires at most once per
#: bout, so a stale arm costs one walk order, never a loop.
WATER_ESCAPE_WINDOW_S = 30.0


@dataclass
class WaterEscalator:
    """Stuck in the water past the timeout: get out, then head for known land.

    WHAT THIS HONESTLY CANNOT DO: CONTRACTS §1 exposes no shoreline, no
    waterline and no per-tile terrain of any kind — "walk to the nearest
    shore point" is not a computation `/state` supports, and this class does
    not pretend otherwise. The best available substitute, once
    `vehicle.in_water` has read true for more than :data:`WATER_TIMEOUT_S`:
    exit the vehicle (a boat/car sitting in the water is not going anywhere
    useful on its own), then — if `player.last_outdoor` (CONTRACTS v1.12: the
    position on the last outdoor→indoor transition, which is dry land almost
    everywhere it is ever set) has been recorded this session — walk toward
    it as a "probably land" bearing. When `last_outdoor` is unknown there is
    genuinely nothing to aim at; this logs that honestly, once, rather than
    guessing a direction, and the gap is called out in this ticket's NOT
    VERIFIED list.

    :meth:`check` is fed every tick, whether or not it fires (the same idiom
    :class:`StuckDetector`/:class:`TaskStallDetector` use), so its own timers
    stay accurate regardless of what else claims the wheel that tick.
    """

    timeout_s: float = WATER_TIMEOUT_S
    escape_window_s: float = WATER_ESCAPE_WINDOW_S
    clock: Any = time.monotonic
    _in_water_since: float | None = None
    _exit_issued_at: float | None = None
    _walk_issued: bool = False

    def check(self, state: GameState) -> dict[str, Any] | None:
        now = self.clock()
        v = state.vehicle
        if v is not None and v.in_water:
            if self._in_water_since is None:
                self._in_water_since = now
            elapsed = now - self._in_water_since
            if elapsed < self.timeout_s or not state.player.in_vehicle:
                return None
            log.info(
                "in the water past the timeout; getting out",
                extra={"kv": {"elapsed_s": round(elapsed, 1)}},
            )
            self._exit_issued_at = now
            self._walk_issued = False
            return {"type": "exit_vehicle", "params": {}}

        # Not currently reading as in the water (on foot, dry, or no vehicle
        # to read the flag from at all).
        self._in_water_since = None
        if self._exit_issued_at is None:
            return None
        if now - self._exit_issued_at > self.escape_window_s:
            # Grace window closed: assume he made it out, and let the
            # ordinary reflexes/roam take it from here.
            self._exit_issued_at = None
            return None
        if self._walk_issued or state.player.in_vehicle:
            return None
        last_land = state.player.last_outdoor
        if last_land is None:
            log.warning(
                "climbed out of the water with nowhere known to walk to — "
                "/state has no shore data and last_outdoor was never "
                "recorded this session (see WaterEscalator's own docstring)"
            )
            self._walk_issued = True  # do not repeat the warning every tick
            return None
        self._walk_issued = True
        log.info(
            "walking toward the last known outdoor position as the best "
            "available 'land' bearing",
            extra={"kv": {"target": [round(last_land.x, 1), round(last_land.y, 1)]}},
        )
        return {
            "type": "walk_to",
            "params": {"x": last_land.x, "y": last_land.y, "z": last_land.z, "run": True},
        }

    def reset(self) -> None:
        """Forget the bout (new game process, or a fresh respawn's clean slate)."""
        self._in_water_since = None
        self._exit_issued_at = None
        self._walk_issued = False


#: A vehicle inside this many metres counts as close enough for
#: :class:`RoadDodge` to take seriously. `nearby.vehicles` (CONTRACTS §1)
#: truncates at the 8 nearest, so anything inside this radius is already one
#: of the closest vehicles around him — narrower than
#: :data:`HOSTILE_CLOSE_RADIUS_M` on purpose: a car this close and closing is
#: seconds from a hit, not background traffic.
ROAD_DODGE_RADIUS_M = 12.0

#: Relative closing speed that counts as "coming at him" rather than
#: "drifting past" or "parked". Derived from two ticks of
#: `nearby.vehicles[].pos` (CONTRACTS §1 v1.6 — there is no per-vehicle
#: velocity field) because that is the only honest way to tell the two apart;
#: an ordinary driving-style car in this game closes well above this when it
#: is actually headed for him.
ROAD_DODGE_CLOSING_MPS = 5.0

#: How far he steps when this fires.
ROAD_DODGE_STEP_M = 5.0

#: Minimum gap between two step-offs — long enough for one `walk_to` to
#: actually cover :data:`ROAD_DODGE_STEP_M`, short enough that a second car a
#: few seconds later gets its own dodge rather than waiting out a long cooldown.
ROAD_DODGE_COOLDOWN_S = 6.0


@dataclass
class RoadDodge:
    """On foot, an NPC vehicle closing fast nearby: step off, at reflex speed.

    `/state` has no lane geometry, no "on a road" flag, and no per-vehicle
    velocity — CONTRACTS §1 `nearby.vehicles[]` is `{handle, model,
    display_name, class, distance, driver, pos}`, position only (v1.6). So
    "on a road" is inferred the only honest way available: an NPC-driven
    vehicle being nearby AT ALL, in this game, means he is standing somewhere
    traffic reaches — there is no better signal to gate on. "Closing fast" is
    the DERIVATIVE of that vehicle's own `pos` between this tick and the
    last, which is exactly what the brief asks for ("derive from two ticks of
    pos").

    There is also no lane centreline to step perpendicular to, so the escape
    direction is the honest substitute available: straight away from the
    vehicle's CURRENT position, extended :data:`ROAD_DODGE_STEP_M` past where
    he is already standing. That increases the miss distance in every case
    except a vehicle already bearing down a line that passes exactly through
    him, where it is still strictly better than standing still — and it is
    what "step off" can honestly mean without a nav-mesh query this class
    does not have.

    :meth:`check` is fed every tick regardless of whether it fires: the
    two-tick derivative needs last tick's positions cached even on ticks that
    do not fire (the same idiom :class:`WaterEscalator` uses).
    """

    danger_radius_m: float = ROAD_DODGE_RADIUS_M
    closing_mps: float = ROAD_DODGE_CLOSING_MPS
    step_m: float = ROAD_DODGE_STEP_M
    cooldown_s: float = ROAD_DODGE_COOLDOWN_S
    clock: Any = time.monotonic
    #: handle -> (observed_at, x, y)
    _prev: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    _last_fired_at: float = -1e9

    def check(self, state: GameState) -> dict[str, Any] | None:
        now = self.clock()
        prev = self._prev
        self._prev = {
            v.handle: (now, v.pos.x, v.pos.y)
            for v in state.nearby.vehicles
            if v.pos is not None
        }
        if state.player.in_vehicle:
            return None
        if now - self._last_fired_at < self.cooldown_s:
            return None

        px, py = state.player.pos.x, state.player.pos.y
        worst: tuple[float, Any] | None = None
        for v in state.nearby.vehicles:
            if v.driver != "npc" or v.pos is None or v.distance > self.danger_radius_m:
                continue
            last = prev.get(v.handle)
            if last is None:
                continue
            last_t, lx, ly = last
            dt = now - last_t
            if dt <= 0.0:
                continue
            prev_dist = math.hypot(lx - px, ly - py)
            closing = (prev_dist - v.distance) / dt
            if closing < self.closing_mps:
                continue
            if worst is None or closing > worst[0]:
                worst = (closing, v)
        if worst is None:
            return None
        closing, v = worst

        away_x, away_y = px - v.pos.x, py - v.pos.y
        length = math.hypot(away_x, away_y)
        if length < 0.1:
            # Standing on top of it: no direction to derive honestly.
            return None
        ux, uy = away_x / length, away_y / length
        target_x, target_y = px + ux * self.step_m, py + uy * self.step_m
        self._last_fired_at = now
        log.info(
            "vehicle closing fast on foot; stepping off",
            extra={
                "kv": {
                    "vehicle": v.handle,
                    "closing_mps": round(closing, 1),
                    "distance_m": round(v.distance, 1),
                }
            },
        )
        return {
            "type": "walk_to",
            "params": {"x": target_x, "y": target_y, "z": state.player.pos.z, "run": True},
        }

    def reset(self) -> None:
        """Forget cached vehicle positions (new game process, ephemeral handles)."""
        self._prev.clear()
        self._last_fired_at = -1e9


#: How long after `threat.being_jacked_by` clears (the fight over one way or
#: another) :class:`JackHandoffGate` keeps `stranded` standing down, so
#: roam's own `take_my_car_back` goal (behavior.roam.py — triggered off
#: `RoamView.stolen_from`, offered at the TOP of the menu by its own
#: `TRIGGERS` entry) gets first refusal at the SAME car. `main._reflex` runs
#: before `main._drive_activities` in the tick order (see `Harness.run`), so
#: on the exact tick the jacker is dealt with, roam has not picked anything
#: yet from THIS snapshot — this grace window is what stops that one-tick gap
#: from letting `stranded` grab the first "any" car in its own widening
#: search instead of the one that was actually his.
JACK_HANDOFF_GRACE_S = 6.0


@dataclass
class JackHandoffGate:
    """Was he being jacked recently? If so, let roam's `take_my_car_back` answer it.

    Deliberately does nothing to the actual jacker — `threat_action`'s own
    `fight_ped` rung already answers `threat.being_jacked_by` (v1.11) at
    reflex speed, preferring the named handle over a relationship guess. This
    class is the OTHER half T9 asks for: once that fight is over, "then
    re-enter the car" is `take_my_car_back`'s job (it already exists in
    `behavior/roam.py`, complete with its own `done_when` graded on the exact
    vehicle HANDLE, not merely "some car") — this class's only job is to keep
    `stranded` (which runs first, in `_reflex`, and would otherwise widen a
    vehicle search for whatever "any" car is nearest) from beating roam to
    the post in that one-tick gap.
    """

    grace_s: float = JACK_HANDOFF_GRACE_S
    clock: Any = time.monotonic
    _cleared_at: float | None = None

    def feed(self, state: GameState) -> bool:
        """Call once per tick. True while `stranded` should stand down for roam."""
        if state.threat.being_jacked_by is not None:
            self._cleared_at = self.clock()
            return True
        if self._cleared_at is None:
            return False
        return (self.clock() - self._cleared_at) < self.grace_s

    def reset(self) -> None:
        """Forget the jacking (new game process, or a clean respawn)."""
        self._cleared_at = None


# --- combat / threat reflex (observed live: no reflex drove combat, so a
# 1-2 s model call under fire meant standing still and dying) -----------------

#: A hostile ped within this many metres counts as an active threat worth
#: engaging. Revised UP from an original 15 m ("in your face") after live
#: feedback: at 15 m he sat in a car taking fire from hostiles further out
#: than that and did nothing at all until either they closed to melee range
#: or he was already badly hurt — "he just sits in car and dies". GTA's own
#: gunfights routinely happen at 20-40 m; `nearby.peds` is already the top 8
#: BY DISTANCE (CONTRACTS §1), so anything hostile that makes that list is
#: already one of the closest entities around him, not something merely
#: visible in the distance. Tunable; not yet verified against measured
#: engagement ranges on the server.
HOSTILE_CLOSE_RADIUS_M = 40.0

#: How close a ped has to be before mere PROXIMITY, plus damage arriving right
#: now, is enough to treat it as the thing hitting him. Deliberately far
#: shorter than :data:`HOSTILE_CLOSE_RADIUS_M`: a `hostile` relationship is
#: evidence on its own, plain proximity is not, and at 40 m every bystander on
#: the pavement would become an "attacker" the moment he fell off a kerb.
#: Melee in GTA V lands from about a metre; 8 m leaves room for the metre or
#: two an attacking ped covers between two snapshots at a 2-4 Hz poll, and for
#: a shove that knocks him back a step. Tunable; not yet measured on the
#: server.
ATTACKER_CLOSE_RADIUS_M = 8.0

#: Effective HP (health + armor) lost inside :data:`DAMAGE_WINDOW_S` that
#: counts as "someone is hitting me right now".
#:
#: Why a window rather than the per-tick signal that already exists:
#: :attr:`perception.Delta.big_health_drop` needs >= 25 HP to disappear
#: BETWEEN TWO SNAPSHOTS, and the fists that killed him on the freeway arrived
#: a few HP at a time — under that bar on every single tick, so it never fired
#: once during the whole beating. Nothing in ordinary play REMOVES effective HP
#: in ones and twos (regeneration only adds, armor pickups only add), so this
#: bar only has to clear the noise floor rather than identify a weapon: 10 is
#: two or three punches, or one glancing hit.
DAMAGE_ATTACK_HP = 4.0
#: LOWERED 10.0 -> 4.0 after live feedback 2026-09-02 ("why can't he fight back
#: quickly the moment he is punched"). At 10 HP he had to absorb two or three
#: punches before the reflex would even look at retaliating, and a GTA melee
#: exchange is decided in about that many. 4 HP is one clean punch: above the
#: noise floor this bar exists to clear (nothing in ordinary play REMOVES
#: effective HP at all - regeneration and pickups only add), and below the cost
#: of the second hit. The radius gate (:data:`ATTACKER_CLOSE_RADIUS_M`, 8 m) is
#: what keeps this from firing on scrapes: damage ALONE never triggers combat,
#: damage plus somebody standing next to him does.

#: How far back the loss is accumulated. Long enough that a slow melee
#: exchange adds up at a 2-4 Hz poll (a punch lands roughly once a second),
#: short enough that a beating survived ten seconds ago does not keep him
#: swinging at an empty street. Also the reason this is peak-to-now inside the
#: window rather than a running total: health regenerating back up between
#: hits must not be counted as more damage.
DAMAGE_WINDOW_S = 4.0

#: Health at or below this fraction of max counts as "hurt enough to break
#: contact rather than trade more hits". The brain's own prompt (situations.md
#: "Health & damage") treats under-35-of-200 (~17%) as "stop taking risks";
#: this reflex fires a little earlier because it has to win a race against a
#: 1-2 s model call, not merely advise one.
LOW_HEALTH_FRACTION = 0.30

#: `nearby.peds[].weapon_class` values that mean "hurts from range". The
#: attacker's class decides two things in :func:`threat_action`: whether fists
#: (or an empty gun) are any answer at all (rung 3), and whether "break
#: contact" means run or take cover (rung 1). `projectile` sits with `gun`: a
#: grenade is not something to run at either.
RANGED_WEAPON_CLASSES: frozenset[str] = frozenset({"gun", "projectile"})
#: ...and the two that mean "he has to reach me to hurt me". `unknown`/None
#: are deliberately in neither set: an attacker the snapshot cannot classify
#: gets the pre-existing answer (fight at full health, cover when hurt).
MELEE_WEAPON_CLASSES: frozenset[str] = frozenset({"unarmed", "melee"})

#: The bridge's own `weapon: "armed"` selection rule
#: (`WeaponState.SelectForRange`, bridge 1.7.0): pump shotgun in his hands at
#: or inside this range, pistol beyond it. Mirrored here ONLY so the harness
#: can predict which of the two the bridge is about to pick and check that
#: one for rounds; the selection itself stays bridge-side.
SHOTGUN_RANGE_M = 10.0
#: The attacker is closing, the snapshot is up to a poll period old, and the
#: bridge re-measures the range when the task starts. Inside this margin the
#: shotgun is assumed to be the pick, so a dry shotgun plus a loaded pistol at
#: 12 m reads as "not armed" rather than gambling on the boundary.
SHOTGUN_RANGE_MARGIN_M = 4.0


def loaded_gun_for(state: GameState, distance_m: float | None) -> bool:
    """Would ``fight_ped {weapon: "armed"}`` / ``shoot_at`` put a gun WITH
    ROUNDS in his hands against a target ``distance_m`` away?

    Why this exists (live, 2026-09-03): `player.weapon.owned` maps a weapon
    NAME to its AMMO COUNT, and the bridge's `HAS_PED_GOT_WEAPON` guard is
    satisfied by a weapon with zero rounds — a Busted strips the magazines
    and leaves the guns. So "he owns a pistol" was true while he had nothing
    to fire, and every armed goal (and any `weapon: "armed"` request) would
    have put an empty gun in his hands. The bridge picks
    (`WeaponState.SelectForRange`): pump shotgun if owned and the target is
    inside :data:`SHOTGUN_RANGE_M`, else pistol if owned, else whatever is
    already in his hands. This predicts that pick from the snapshot and asks
    whether THAT weapon has a round in it.

    ``distance_m`` is None when the target is not in `nearby.peds` (beyond the
    top 8 by distance); the shotgun is then assumed, the conservative reading.
    False on a pre-1.7.0 bridge (`player.weapon` absent): it cannot vouch for
    rounds it cannot see, and the caller falls back to the unarmed answer.
    """
    w = state.player.weapon
    if w is None:
        return False
    owned = w.owned or {}
    shotgun = owned.get("PumpShotgun")
    pistol = owned.get("Pistol")
    close = distance_m is None or distance_m <= SHOTGUN_RANGE_M + SHOTGUN_RANGE_MARGIN_M
    if shotgun is not None and close:
        return shotgun > 0
    if pistol is not None:
        return pistol > 0
    # Owns neither tracked handgun: the bridge leaves whatever he is holding.
    return w.weapon_class == "gun" and w.ammo > 0


@dataclass
class DamageTracker:
    """Is he losing health right now? The one attack signal that cannot lie.

    `nearby.peds[].relationship` is the engine's relationship GROUP, not "is
    currently hitting me". A random pedestrian who decides to swing is still
    `neutral` in the snapshot while the punches land — observed live, and the
    reason :func:`threat_action` never fired while a ped beat the agent to death
    on the freeway. Health going down, on the other hand, is unambiguous, and
    it is in every `/state` already (CONTRACTS §1 `player.health` /
    `player.armor`).

    `health + armor` rather than health alone: armor absorbs damage FIRST in
    GTA V, so an armoured the agent being shot shows a perfectly flat `health`
    while he is very much under fire. Both fields are contract fields; the sum
    is only ever used as "did the number go down", never as a health value.

    The measure is PEAK-to-now inside a :data:`DAMAGE_WINDOW_S` sliding
    window, not a sum of per-tick differences: health regenerates between
    hits, and a running total would keep counting the same 20 HP long after it
    had been healed back. It also means a tick that is simply missed (a bridge
    stall, a dead/arrested gap) degrades to "no evidence" rather than to a
    fabricated spike — old samples age out on their timestamps, and one lone
    sample can never be a drop.

    :meth:`feed` is called once per poll tick with the fresh snapshot and
    returns whether the window now shows an attack.
    """

    window_s: float = DAMAGE_WINDOW_S
    attack_hp: float = DAMAGE_ATTACK_HP
    clock: Any = time.monotonic
    #: (timestamp, effective HP) inside the window. Bounded by the window, so
    #: this cannot grow: at 4 Hz over 4 s it holds ~16 entries.
    _samples: list[tuple[float, float]] = field(default_factory=list)
    #: Effective HP lost from the window's peak, exposed for logs/commentary.
    lost_hp: float = 0.0

    def feed(self, state: GameState) -> bool:
        """Record this tick's effective HP; True when the window shows an attack."""
        now = self.clock()
        effective = float(state.player.health + state.player.armor)
        cutoff = now - self.window_s
        self._samples = [s for s in self._samples if s[0] >= cutoff]
        self._samples.append((now, effective))
        peak = max(hp for _, hp in self._samples)
        self.lost_hp = max(0.0, peak - effective)
        return self.lost_hp >= self.attack_hp

    def reset(self) -> None:
        """Forget the window (respawn, or a new game process behind the bridge).

        A death is a 200 HP drop and a respawn is a 200 HP jump; carrying
        either across the gap would have him come back swinging at nobody.
        """
        self._samples.clear()
        self.lost_hp = 0.0


def _attacker_close(state: GameState) -> bool:
    """Is anything that could be swinging at him within arm's reach?

    Any relationship EXCEPT `friendly` counts: `friendly` (CONTRACTS §1, v1.5)
    is the engine's own Companion/Like/Respect group — mission crew standing
    next to him — and a crewmate is the one ped that is definitely not the one
    hitting him. `neutral` explicitly counts, because that is what a random
    attacker looks like in the snapshot at the moment it swings. `nearby.peds`
    is the top 8 BY DISTANCE (§1), so whoever is punching him is in that list.
    """
    return any(
        ped.relationship != "friendly" and ped.distance <= ATTACKER_CLOSE_RADIUS_M
        for ped in state.nearby.peds
    )


def threat_action(
    state: GameState,
    delta: Delta,
    under_attack: bool = False,
    *,
    vehicle_blocked: bool = False,
    heat_wanted: bool = False,
) -> dict[str, Any] | None:
    """Immediate combat/threat response — NO model call.

    `heat_wanted`: the locked free-roam goal exists to attract the police
    (`Goal.wants_heat` — `earn_two_stars`, `three_star_survival`, ...). Rung 6
    stands down for it: measured in the soak, the drive-by earned exactly the
    star the goal wanted and this rung fled from it on the next poll,
    preempting the goal. Every rung above — being shot, being beaten, a
    hostile in reach, low health — still fires; only "stars alone" yields.

    This is the reflex layer's whole reason to exist: a brain call costs
    1-2 s, which is fatal under fire — confirmed live as the top-priority gap:
    "he can see enemies on the map, he just sits in car and dies". Survival
    ladder, highest priority first:

    1. Hurt — health at/under :data:`LOW_HEALTH_FRACTION` of max -> break
       contact, above everything else: ``seek_cover`` on foot, or
       ``wander_drive`` (style ``avoid_traffic``) in a WORKING vehicle. There
       is no "just drive away" bridge task without a destination, so widening
       distance via ordinary driving is the honest equivalent of "drive
       away". Survival always wins the ladder. In a car that cannot leave
       (upside down, in the water, a burnt-out shell, or one the vehicle
       state machine has measured as not moving) breaking contact by driving
       is a lie, so that case falls back to ``seek_cover``.

       On foot with a NAMED attacker (`threat.attacker_handle`, v1.11) whose
       `weapon_class` is in :data:`MELEE_WEAPON_CLASSES`, "break contact" is
       ``flee_ped`` at that handle (v1.14) rather than ``seek_cover``. The
       bridge's ``seek_cover`` is TASK_SEEK_COVER_FROM_POS from his OWN
       position when he is not wanted — cover from bullets, which a man with
       fists does not fire. Crouching behind a wall while the puncher walks
       round it is how a "break contact" rung produced a death; running from
       the specific ped is what the rung always meant. A gunman keeps the
       cover answer (running across open ground from a gun is worse), and
       so does an attacker the snapshot cannot classify or does not list.
       This is NOT cowardice by design: at full health rung 3 below still
       fights, and a loaded gun makes that fight short — this rung only
       exists for the fight that has already gone wrong.
    2. **Being beaten in a car that can leave** — `under_attack`,
       `player.in_vehicle`, the car is drivable and STATIONARY, and no
       mission is active -> ``wander_drive``. THE OPERATOR'S RULE, and a
       deliberate reversal of what the rungs below used to do: *"ped punches
       me while I'm on foot -> fight back. Ped attacks me while I'm already
       in a working stolen car -> FLOOR IT."* Driving away is faster and
       safer than getting out to trade punches with the man whose car it is,
       and it is the difference between a joke and the five WASTED counted on
       stream on 2026-09-02 while he sat in a stolen convertible and let the
       owner beat him to death through the open door.

       Three guards, each closing a case the old rungs got right:

       * *stationary only* (`vehicle.speed` at/below
         :data:`vehicle.SEATED_SPEED_MPS`). HP lost while actually driving is
         overwhelmingly collision damage — a kerb, a lamppost, a head-on — and
         this module's older note is still true: answering an ordinary crash
         by preempting the drive is thrash. A crash victim is MOVING; a man
         being beaten in a parked car is not, and `/state` tells the two
         apart for free.
       * *drivable only* (:func:`vehicle.vehicle_can_drive_away`, plus the
         caller's `vehicle_blocked`). Telling a wedged or wrecked car to drive
         away is a way of doing nothing while being hit. Those fall through to
         the fight/cover rungs, which is what the brief asks for.
       * *outside a mission*. prompts/situations.md ships the opposite order
         for mission firefights in strong terms — "A mission firefight is not
         a car chase — fight, don't flee", "Fleeing a scripted firefight fails
         the mission" — and a reflex preempts a prompt every time. Same
         precedent as rung 6's `flee_police`.
    3. **Being hit, by something in reach** — `under_attack` (from
       :class:`DamageTracker`, or the single-tick
       :attr:`perception.Delta.big_health_drop`), on foot, and any
       non-`friendly` ped within :data:`ATTACKER_CLOSE_RADIUS_M` ->
       ``combat_hated_targets_around``. This is the rung the live bug needed
       and did not have: a pedestrian who walks up and starts swinging is
       normally still `neutral` in the snapshot, so rung 4 below never fired
       and he stood there and died. The action is byte-identical to rung 4's
       (same type, same radius) so :class:`ThreatLatch` treats the two as one
       intent and a ped that flips `neutral` -> `hostile` mid-fight cannot
       cause a re-post.

       On foot, or in a car that CANNOT leave. Rung 2 above is the
       working-car half of the operator's rule and this is the other half:
       leaving is always the better answer when it is available, and when it
       is not — upside down, in the water, a burnt-out shell, or a car the
       state machine has measured as immobile — fighting back is what is
       left. (Before this rung was widened, a `neutral` ped beating him in a
       wedged car produced NOTHING at all: the old rung was `not
       player.in_vehicle`, and a neutral attacker never reaches the hostile
       rung.) A `neutral` attacker counts here because
       :class:`DamageTracker` is the evidence and `relationship` is only the
       engine's relationship GROUP.

       What the engine does with this is the engine's business: the contract's
       ``combat_hated_targets_around`` hands off to the game's own combat AI,
       which picks its own target among the peds it considers hated. Whether
       it will engage a specific ped that `/state` still reports as `neutral`
       cannot be established from here — see the module note; it needs the
       live game. If it finds nothing to fight the task simply reports `done`
       (CONTRACTS §1) and the latch's hold-down keeps the retry to one post
       every :data:`THREAT_HOLD_S`, which is a cheap way to be wrong.
    4. Healthy, and a hostile ped is present within
       :data:`HOSTILE_CLOSE_RADIUS_M` -> ``combat_hated_targets_around``, the
       engine's own combat task. This fires REGARDLESS of `wanted`. It no
       longer fires regardless of `in_vehicle`, which is the second half of
       the operator's reversal, and the change has to be spelled out because
       the old behaviour was deliberate and documented: the bug it closed was
       "sitting in a car near visible hostiles, healthy, doing nothing at
       all". The answer to that bug is still "stop doing nothing" — it is just
       no longer "get out and fight". In a WORKING vehicle, outside a mission:

       * already moving, or already under a running drive order -> fall
         through to the rungs below. He is already doing the best available
         thing; posting here would preempt a working escape, and falling
         through means stars still get ``flee_police`` instead of aimless
         wandering. It also leaves the wheel free for the ordinary layers
         instead of freezing them out with `_threat_has_the_wheel`.
       * stationary, nothing driving -> ``wander_drive``. Start leaving.

       In a car that cannot leave, or during a mission, it fights exactly as
       before. `combat_hated_targets_around` hands off entirely to the game's
       own combat AI (it aims and shoots; nothing here aims manually —
       CONTRACTS §1's own description). Engaging a hostile cop this way is a
       response to an already-hostile encounter, never an initiation — the
       brain's own hard rule ("never initiate combat with police", rules.md
       rule 6) is about starting one, not defending against one already close
       enough to be a `nearby.peds` hostile.
    5. Taking damage with nothing in reach to hit back at — a sniper, a fire,
       drowning, a beating he has already backed away from -> break contact,
       same two actions as rung 1. In a VEHICLE only the single-tick cliff
       (`Delta.big_health_drop`, >= 25 HP between snapshots) counts here, for
       the collision reason given in rung 2; on foot the sliding window counts
       too.
    6. `wanted > 0`, **no mission active**, and no engageable hostile present
       (stars accumulating from range, nobody actually in range yet, or
       fighting is not survivable) -> ``flee_police``, the ordinary
       evade-by-driving-or-running response (situations.md's whole
       wanted-level playbook). The `not mission.active` half is deliberate:
       plenty of story missions ARE a scripted firefight with police, and
       prompts/situations.md's own rule for that case is "fight, don't
       flee" — a reflex that drives him away from a mission gunfight the
       moment a star appears contradicts the prompt shipping beside it and
       fails the mission. Outside a mission, stars mean the ordinary
       free-roam chase and fleeing is right.

    Returns ``None`` when nothing here needs to fire, so the tick falls
    through to the ordinary reflexes/brain. This function itself is pure and
    stateless — it re-evaluates from the current signals every tick — so once
    a threat passes it simply stops firing. Whether a returned action is
    actually POSTed is :class:`ThreatLatch`'s decision, because posting the
    same action at 3 Hz preempts (and therefore restarts) the engine task it
    just asked for. Mission-following and the activity runner notice their
    task was preempted and back off on their own (the same "someone else has
    the wheel, don't fight it" idiom already in
    :class:`missions.MissionFollower` / :class:`activities.ActivityRunner`),
    then resume once nothing here is claiming the wheel. That is what keeps
    one gunshot from permanently abandoning a mission.

    Dead/arrested is not this function's problem: `main._reflex` does not
    call it in that state (nothing left to defend), and it returns `None` as
    a belt-and-braces guard if it ever is. A cutscene is refused outright for
    the same reason: it is a scripted beat, the game has the wheel, and
    `main._execute_action` would refuse the task anyway — returning `None`
    here additionally stops the reflex from claiming the tick and from
    spending the latch's hold-down on a post that cannot happen.

    `under_attack` is :meth:`DamageTracker.feed`'s answer for this tick. It
    defaults to False so the relationship-driven rungs can still be reasoned
    about (and tested) on their own; production always passes it.

    `vehicle_blocked` is :class:`vehicle.VehicleController`'s measured answer
    to "this car has been told to drive and has not moved". It is the only
    honest way to know a car is a trap rather than an escape, because
    `/state` has no engine-health, no `driveable` and no obstruction field —
    the observation that it did not move IS the evidence. Defaults to False so
    the function stays testable on its own; production always passes it.

    **CONTRACTS v1.11 — `state.threat`.** `threat.attacker_handle` (the ped
    currently attacking) and `threat.being_jacked_by` (the ped pulling him out
    of a car) are target-explicit facts a pre-v1.11 bridge cannot send — both
    are `None` on one, so every rung below degrades exactly to its pre-v1.11
    behaviour. When either is set it is PREFERRED over :class:`DamageTracker`'s
    damage-accumulation heuristic at rung 3: instead of handing the engine a
    radius and hoping `combat_hated_targets_around` finds a target whose
    relationship happens to be Neutral/Dislike/Hate (a carjack victim is
    plausibly still Respect/Like, which is why that action silently no-oped
    while he was beaten to death on stream), it posts `fight_ped` at the named
    handle — no relationship lookup required. `DamageTracker` stays the
    fallback for a pre-v1.11 bridge, unchanged. Rung 2 (the working-car
    reversal) still outranks this: a threat handle in a car that can drive
    away is answered by leaving, never by `fight_ped` — the bridge already
    tells the engine not to let him get out to fight, and this module is the
    other half of that rule.

    **Bridge 1.7.0 — the weapon, and the fight he cannot win.** Being attacked
    is lethal, so the named-handle `fight_ped` asks for ``weapon: "armed"``
    whenever :func:`loaded_gun_for` says the gun the bridge would select
    actually has rounds in it. That is the difference between "answer in
    kind" (the v1.11 ``auto``: a puncher gets fists, a fight that takes ten
    seconds and can be lost) and shooting the man who started it, which is
    the required behaviour: fight back FAST and actually kill the
    attacker. The rounds check is against AMMO, not ownership —
    `player.weapon.owned` is a name->ammo map and a Busted leaves every gun
    owned with 0 rounds, which is exactly what he was carrying when he lost a
    `fight_ped` on 2026-09-03. With nothing loaded the param is omitted
    (``auto``), so a pre-1.7.0 bridge sees the v1.11 wire shape.

    Two consequences worth knowing about. A ped who is attacking him and
    happens to be police IS fought this way — that is defending against an
    attack already under way, which rules.md rule 6 ("never initiate combat
    with police") permits; rung 4 already engaged a hostile cop through the
    engine's own combat task before this change, so no new police policy is
    introduced here, only a named target and a chosen weapon. And a ped he
    picked a FIST fight with (`roam.pick_a_fight`, ``weapon: "unarmed"``)
    who punches back is, by the bridge's `attacking_me`, an attacker: while
    the goal's own `fight_ped` is running, :class:`ThreatLatch` holds this
    rung's armed one (same verb already running), so the bit stays a fist
    fight; only if the engine drops that task mid-brawl does this rung
    re-post with a gun. Acceptable — the man is hitting him — but it is a
    real edge and it is written down.

    The other half: a named attacker with a `weapon_class` in
    :data:`RANGED_WEAPON_CLASSES` when he has NOTHING loaded is not a fight,
    it is a charge across open ground at a gun (the bridge's ranged arm plus
    CanFightArmedPedsWhenNotArmed). Rung 3 takes cover for that case instead
    — rung 5's own answer to damage he cannot return.
    """
    player = state.player
    if player.dead or player.arrested:
        return None
    if state.mission.cutscene_active:
        return None
    if state.player.switch_in_progress:
        return None
    if state.mission.retry_in_flight:
        return None

    #: A car he could actually leave in: right way up, out of the water, not a
    #: shell, and not one the state machine has already measured as immobile.
    can_leave = vehicle_can_drive_away(state) and not vehicle_blocked
    #: Parked, in the terms the bridge itself uses (its "stopped" bar is
    #: 0.2 m/s). A man taking damage while ROLLING is crashing; a man taking
    #: damage while STOPPED in a car is being beaten.
    v = state.vehicle
    stationary = v is None or v.speed <= SEATED_SPEED_MPS
    #: Missions get the prompt's order, not the operator's: situations.md says
    #: fight a scripted firefight, and a reflex beats a prompt every time.
    free_roam = not state.mission.active
    #: The engine has already been told to drive somewhere. Posting a second
    #: drive over the top would preempt and restart it (CONTRACTS §1) — and
    #: whether that order is actually producing motion is
    #: :class:`vehicle.VehicleController`'s job to grade, not this function's.
    already_leaving = state.last_task.status == "running" and state.last_task.type in (
        "drive_to",
        "wander_drive",
        "flee_police",
    )

    def break_contact() -> dict[str, Any]:
        if player.in_vehicle and can_leave:
            return {"type": "wander_drive", "params": {"style": "avoid_traffic"}}
        # On foot, or in a car that is upside down / in the water / wrecked /
        # measured immobile: driving away is not on offer, so take cover.
        return {"type": "seek_cover", "params": {"duration_s": 10}}

    fight = {
        "type": "combat_hated_targets_around",
        "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M},
    }
    leave = {"type": "wander_drive", "params": {"style": "avoid_traffic"}}

    # v1.11: a target-explicit handle, when the bridge sends one. Preferred
    # over the relationship-blind `fight` above at rung 3 — see the docstring.
    # `attacker_handle` (currently swinging) outranks `being_jacked_by`
    # (pulling him out) when, in some rare frame, both are set.
    target_handle = state.threat.attacker_handle
    if target_handle is None:
        target_handle = state.threat.being_jacked_by

    # Who the named attacker is, as far as this snapshot knows him. `nearby.
    # peds` is the top 8 by distance and the NEAREST attacker is what the
    # bridge names, so he is almost always in it; when he is not, both facts
    # below are simply unknown and the rungs use their pre-existing answers.
    attacker = None
    if target_handle is not None:
        attacker = next((p for p in state.nearby.peds if p.handle == target_handle), None)
    attacker_distance = attacker.distance if attacker is not None else None
    attacker_ranged = attacker is not None and attacker.weapon_class in RANGED_WEAPON_CLASSES
    attacker_melee = attacker is not None and attacker.weapon_class in MELEE_WEAPON_CLASSES
    #: `weapon: "armed"` would put a LOADED gun in his hands (see
    #: :func:`loaded_gun_for` for why "owns a pistol" is not that).
    armed = loaded_gun_for(state, attacker_distance)

    def fight_action() -> dict[str, Any]:
        if target_handle is not None:
            params: dict[str, Any] = {"handle": target_handle}
            if armed:
                # Bridge 1.7.0: select the loadout gun by range BEFORE the
                # combat task. Left out (the v1.11 `auto`) when he has nothing
                # to fire — `auto` never touches what is in his hands.
                params["weapon"] = "armed"
            return {"type": "fight_ped", "params": params}
        return fight

    # 1. Too hurt to trade more hits. On foot with a NAMED attacker who has to
    #    reach him to hurt him, breaking contact means running from THAT ped
    #    (`flee_ped`, v1.14): cover is for bullets, and a man crouched behind a
    #    wall is still a man being punched. A gunman, or an attacker the
    #    snapshot cannot classify, keeps the cover answer.
    max_health = player.max_health if player.max_health > 0 else 200
    if player.health <= max_health * LOW_HEALTH_FRACTION:
        if not player.in_vehicle and target_handle is not None and attacker_melee:
            return {"type": "flee_ped", "params": {"handle": target_handle}}
        return break_contact()

    # Damage arriving right now, from any detector: the sliding window (a
    # beating, a few HP at a time), the single-tick cliff, or — v1.11, and
    # earlier than either of the other two — the bridge naming an attacker or
    # a jacker directly. `attacking_me` is true the frame the game tasks a ped
    # into combat against him, before the first punch lands or any HP moves.
    taking_damage = under_attack or delta.big_health_drop or target_handle is not None

    # 2. THE REVERSAL. Being beaten (or jacked) in a parked, working car
    #    outside a mission: floor it. Faster and safer than getting out to
    #    fight the owner, and the exact death this rung was written from. This
    #    is also the v1.11 hard rule from the brief: `fight_ped` is never
    #    posted while `player.in_vehicle` and the vehicle is drivable — this
    #    rung is what answers that case instead.
    if taking_damage and player.in_vehicle and can_leave and stationary and free_roam:
        return leave

    # 3. Something is hitting him (or jacking him) and he cannot just drive
    #    away — on foot, or in a car that cannot leave (upside down, in the
    #    water, a shell, or one the state machine has measured as immobile).
    #    A named handle needs no proximity check of its own: the bridge has
    #    already vouched for it (v1.11 `attacking_me`/`threat`). Without one,
    #    the pre-v1.11 rule applies unchanged — damage plus somebody in reach.
    #
    #    One exception, and it is the one that kills him: a named attacker
    #    who SHOOTS, when he has nothing loaded to shoot back with. `fight_ped`
    #    would answer a gun with fists (the bridge's ranged arm plus
    #    CanFightArmedPedsWhenNotArmed is a charge across open ground), so
    #    that case takes cover instead, rung 5's own answer to damage he
    #    cannot return.
    if (
        target_handle is not None
        and not (player.in_vehicle and can_leave)
        and attacker_ranged
        and not armed
    ):
        if player.wanted > 0 and free_roam:
            # The gun is a police gun (or he has stars regardless): rung 6's
            # own answer. The bridge's `seek_cover` hides from the last spot
            # the police saw him, which is how a man gets arrested, not away.
            return {"type": "flee_police", "params": {}}
        return break_contact()
    if not (player.in_vehicle and can_leave) and (
        (taking_damage and target_handle is not None)
        or (taking_damage and _attacker_close(state))
    ):
        return fight_action()

    # 4. A hostile in engagement range, healthy: fight — unless he is in a
    #    working car outside a mission, where leaving outranks fighting.
    if any(
        ped.relationship == "hostile" and ped.distance <= HOSTILE_CLOSE_RADIUS_M
        for ped in state.nearby.peds
    ):
        if player.in_vehicle and can_leave and free_roam:
            if stationary and not already_leaving:
                return leave
            # Already moving, or already under a drive order. Do not stop a
            # working escape to punch someone, and do not preempt the order
            # either — fall through, because a lower rung may still have
            # something better to say (stars mean `flee_police`, which beats
            # aimless wandering).
        else:
            return fight

    # 5. Still being hurt, nothing in reach to answer: get away from it.
    if delta.big_health_drop or (under_attack and not player.in_vehicle):
        return break_contact()

    # 6. Stars, outside a mission, nothing engageable: the ordinary chase.
    if player.wanted > 0 and not state.mission.active and not heat_wanted:
        return {"type": "flee_police", "params": {}}

    return None


# --- threat latch: one post per threat, not one per tick ----------------------

#: Minimum gap between two IDENTICAL threat actions. Belt-and-braces behind the
#: "the engine is already running exactly this task" check below: a task can
#: legitimately report `done`/`failed` for a tick or two mid-fight (the combat
#: task ends the moment no hated target is in radius, then a fresh one walks
#: into it), and re-posting at 3 Hz through that window is the same thrash by
#: another route.
THREAT_HOLD_S = 4.0


@dataclass
class ThreatLatch:
    """Decides whether a :func:`threat_action` result is worth POSTing.

    Observed live, and the reason this exists: the threat reflex re-evaluates
    at the 3-4 Hz poll rate and returned the same action every tick, and
    CONTRACTS §1 says every POST /task preempts the running one (old task ->
    `failed`, `detail: "preempted"`). So the engine's combat task was torn down
    and rebuilt three times a second: it never got past the start of its
    aim/approach cycle. On stream that is "walks like someone is pressing W
    constantly, stuttering" and "fires but not at the cops" — the ped restarts
    before a shot lands.

    The rule is deliberately dumb and stateless-ish, so it cannot itself latch
    the agent into doing nothing:

    * if the engine is ALREADY running a task of the type we would post, say
      nothing — the game is doing it;
    * if we posted this same type within :data:`THREAT_HOLD_S`, say nothing;
    * anything else (task finished/failed, someone else took the wheel, or the
      situation changed class — fight -> break contact when health drops) posts
      immediately.

    Nothing here holds across a change of intent: the action TYPE is the
    intent, so a health drop that turns `combat_hated_targets_around` into
    `seek_cover`/`wander_drive` is issued on the very next tick.

    One refinement for the target-explicit verbs (`fight_ped`/`flee_ped`,
    v1.11/v1.14): the named `handle` is PART of the intent, but only once the
    engine has stopped running the last one. The bridge ends `fight_ped` on
    its own when its target is dead or gone, and with two men on him the
    second attacker becomes `threat.attacker_handle` the same tick — holding
    that for :data:`THREAT_HOLD_S` is four seconds of being hit for nothing.
    A running task is never preempted to switch targets, though: that would
    be the restart-before-a-shot-lands thrash under a new name.
    """

    hold_s: float = THREAT_HOLD_S
    clock: Any = time.monotonic
    _last_type: str | None = None
    _last_handle: int | None = None
    _last_issued_at: float = 0.0

    def should_issue(self, action: dict[str, Any], state: GameState) -> bool:
        action_type = str(action["type"])
        lt = state.last_task
        if lt.status == "running" and lt.type == action_type:
            # The engine is already doing exactly this. Posting again would
            # preempt it (CONTRACTS §1) and restart it from scratch. This
            # holds even when the NAMED target differs: switching targets
            # mid-fight by preempting is the exact restart-before-a-shot-lands
            # thrash this class exists to stop, and the bridge's `fight_ped`
            # ends on its own the moment its target is dead or gone.
            return False
        if self._last_type == action_type:
            now = self.clock()
            if now - self._last_issued_at < self.hold_s:
                handle = _target_handle(action)
                if handle is None or self._last_handle is None or handle == self._last_handle:
                    return False
                # Same verb, DIFFERENT named target, and the engine is not
                # running the last one (the check above would have held): the
                # first fight ended and somebody else is on him. That is a new
                # intent, not a retry, so it goes out now rather than after
                # the hold-down — measured in seconds, that is the difference
                # between answering the second attacker and absorbing him.
        return True

    def issued(self, action: dict[str, Any]) -> None:
        self._last_type = str(action["type"])
        self._last_handle = _target_handle(action)
        self._last_issued_at = self.clock()

    def reset(self) -> None:
        """Forget the last threat action (new game process, respawn)."""
        self._last_type = None
        self._last_handle = None
        self._last_issued_at = 0.0


def _target_handle(action: dict[str, Any]) -> int | None:
    """The named ped a threat action is aimed at (`fight_ped`/`flee_ped`), or None."""
    params = action.get("params") or {}
    handle = params.get("handle")
    return int(handle) if handle is not None else None


# --- death / arrest recovery --------------------------------------------------

#: A normal GTA V death fade-to-respawn is seconds, not minutes; past this the
#: game has not handed control back and something is actually wrong (a stuck
#: loading screen, a crashed process the watchdog has not caught yet).
DEAD_STUCK_TIMEOUT_S = 90.0
#: An arrest runs a longer scripted sequence (cuffs, ride, drop-off) than a
#: death fade, so it gets a longer ceiling before this calls it stuck.
ARRESTED_STUCK_TIMEOUT_S = 180.0
#: Re-announce a stuck recovery this often so it does not disappear the moment
#: it stops being brand new, without logging it every tick either.
STUCK_RENOTIFY_S = 60.0


@dataclass
class DeathArrestRecovery:
    """Detects `dead`/`arrested` transitions and the game's own respawn.

    What this is for, precisely: while `player.dead` or `player.arrested` is
    true there is nothing useful to decide — he cannot move, fight or drive —
    so every bridge task issued in that state is pointless and a wasted API
    call to produce it. `main._reflex`/`main.run` gate on
    `state.player.dead`/`.arrested` directly for that suppression at each call
    site (and `main._execute_action` refuses any bridge task outright while
    either is true, the same single choke point it already uses for a
    cutscene); this class's job is the two things that need memory across
    ticks:

    1. noticing the *moment* he comes back so stale mission/goal state can be
       cleared before normal behaviour resumes (never inventing a respawn —
       only the game's own `dead`/`arrested` clearing counts, per CLAUDE.md
       rule 5: no cheating death); and
    2. saying so loudly if "down" runs past a sane ceiling instead of polling
       forever in silence.

    Self-contained transition tracking (its own previous-tick memory, the
    same idiom as :class:`GameRestartDetector` above) rather than reaching
    into :class:`perception.Delta`: `Delta` already tracks `died`/`respawned`
    (dead-flag transitions) but has no `arrested -> not arrested` field, and
    this needs both — one consistent mechanism for both is simpler than two.

    **Not this class's job (superseded by observation from the real
    server):** an earlier version of this class also fired one mission-retry
    keypress once `dead` cleared. Confirmed live: a mission failure freezes
    the SHVDN script thread entirely on the "MISSION FAILED"/retry screen
    (`/health.tick_hz` pinned at 0.0, `/state` frozen on the last snapshot),
    so `player.dead` never clears on its own to wait for — there is nothing
    for THIS class to observe until something outside the frozen bridge
    presses a key. That is now :class:`BlockingScreenWatchdog` below, which
    detects the freeze independently of `dead`/`arrested` and drives the
    keypress; once it succeeds, the game resumes ticking, the real respawn
    happens, and this class's ordinary `respawned` detection below still
    fires exactly the same way it always did — this class's job did not
    change, only who unblocks it in the frozen case.
    """

    clock: Any = time.monotonic
    dead_timeout_s: float = DEAD_STUCK_TIMEOUT_S
    arrested_timeout_s: float = ARRESTED_STUCK_TIMEOUT_S
    renotify_s: float = STUCK_RENOTIFY_S
    _down_since: float | None = None
    _down_cause: str | None = None  # "dead" | "arrested"
    _last_stuck_log: float = 0.0

    def feed(self, state: GameState) -> dict[str, Any]:
        """Advance from the current dead/arrested flags for this tick.

        Returns ``{"respawned": bool, "respawn_cause": "dead"|"arrested"|None}``.
        A stuck-timeout is logged internally (loudly, rate-limited) rather
        than returned — nothing outside this class needs to act on it, only
        to know it happened.
        """
        dead, arrested = state.player.dead, state.player.arrested
        down_now = dead or arrested
        if down_now and self._down_since is None:
            self._down_cause = "dead" if dead else "arrested"
            self._down_since = self.clock()
            self._last_stuck_log = 0.0
            log.info(
                "player is down; suppressing tasks until the game's own respawn",
                extra={"kv": {"cause": self._down_cause}},
            )

        result: dict[str, Any] = {"respawned": False, "respawn_cause": None}
        if self._down_since is None:
            return result

        if down_now:
            timeout = (
                self.dead_timeout_s if self._down_cause == "dead" else self.arrested_timeout_s
            )
            elapsed = self.clock() - self._down_since
            if elapsed >= timeout and self.clock() - self._last_stuck_log >= self.renotify_s:
                self._last_stuck_log = self.clock()
                log.error(
                    "still down well past a sane timeout; waiting on the game's "
                    "own respawn (never faking one, never spinning)",
                    extra={"kv": {"cause": self._down_cause, "down_for_s": round(elapsed, 1)}},
                )
            return result

        result["respawned"] = True
        result["respawn_cause"] = self._down_cause
        self._down_since = None
        self._down_cause = None
        return result


# --- blocking-screen watchdog (pause menu / MISSION FAILED / retry prompt) ---
#
# Confirmed live, twice, with DIFFERENT button wording:
#     MISSION FAILED            MISSION FAILED
#     T. died.                  Franklin lost Lamar.
#     Restart [Tab]  Retry [Enter]      Skip [Tab]   Restart [Enter]
# The label on each key varies by failure type; what does NOT vary is that
# ENTER is the non-destructive one (retry/restart this attempt) and TAB is
# the destructive one (skip the mission entirely, or throw the attempt away).
# TAB IS THEREFORE NEVER SENT BY THIS WATCHDOG, under any label.
# In both cases the SHVDN script thread stopped ticking entirely — `/health.tick_hz`
# pinned at 0.0 with `game_fps` frozen at the exact same reading sample after
# sample (a real, still-ticking FPS reading fluctuates; an identical value
# every sample means nothing is refreshing it), `/state.tick`/`.ts` frozen,
# and GTA5.exe's resident memory dropped from ~2300 MB (in-world) to ~600 MB
# (on this menu/failed screen) — corroborating, not depended on alone. Also
# confirmed live, twice: `System.Windows.Forms.SendKeys.SendWait` does
# nothing at all against this game (it reads raw/DirectInput, the same
# reason RDP mouse/keyboard input does nothing) — the ONLY input path that
# reaches it is the harness's own SendInput primitives (`primitives.py`),
# which is why this watchdog calls `Primitives.press_key` directly rather
# than posting anything through the bridge (bridge tasks cannot help either:
# the game is not running them while its script thread is not ticking).

#: `state.tick` may hold the exact same value for a legitimate reason (poll
#: landed twice inside one 60 Hz game frame); past this many seconds
#: unchanged is well past that — the game runs at ~60 Hz (CONTRACTS
#: /health.tick_hz), so even one normal poll interval ordinarily moves this
#: counter by dozens. This is the same underlying signal as the confirmed
#: `tick_hz: 0.0` observation (both derive from the same SHVDN `Tick` event
#: not firing); `state.tick` is used here because the harness already polls
#: it every loop iteration for free, where `/health` would be an extra call.
SCRIPT_STALL_TIMEOUT_S = 3.0

#: Escalating, bounded key sequence tried to clear a blocking screen.
#: CONFIRMED live for the "MISSION FAILED" screen, in both wordings seen
#: (`Retry [Enter]` and `Restart [Enter]`): ENTER is the non-destructive
#: choice in both, so Enter is tried first and is not a guess for that
#: screen. TAB is never in this sequence and must never be added: it is
#: `Skip` in one wording (abandons the mission objective outright) and
#: `Restart` in the other (throws away checkpoint progress).
#: Escape is the second, less-certain attempt for any OTHER blocking screen
#: this watchdog might also encounter (e.g. the pause menu, which Escape
#: ordinarily closes) — that part has not been watched happening.
BLOCKING_SCREEN_KEYS: tuple[str, ...] = ("enter", "esc")

#: Whole attempts (one key from the sequence each) before this gives up and
#: goes loud instead of spamming keys forever.
MAX_STALL_RECOVERY_ATTEMPTS = 3

#: Gap between attempts — long enough for a keypress to actually land and the
#: next poll to show whether the tick moved, short enough that three attempts
#: do not themselves take minutes.
STALL_RECOVERY_RETRY_GAP_S = 4.0


@dataclass
class BlockingScreenWatchdog:
    """Detects a frozen SHVDN script thread from OUTSIDE it, and tries to
    clear it with a small, bounded, escalating keypress sequence.

    This is a different failure mode from :class:`BridgeStallTracker` above:
    that one covers the bridge *answering* `503 not_ready`/`game_thread_
    stalled` — an explicit "not now" the bridge is still able to say. Here
    the bridge keeps answering `200` with a snapshot, but the snapshot itself
    stops advancing, because GTA V is on a modal/blocking screen (pause menu,
    a failure screen, a retry prompt) and the same SHVDN `Tick` event that
    refreshes `/state` and applies queued bridge tasks has stopped firing
    entirely. Nothing routed through the bridge can help; only a real
    keypress can.

    Distinguishing a legitimate cutscene (fine — wait it out, the world and
    the script thread keep ticking through those) from this: this watchdog
    only ever acts on the LAST snapshot actually seen ticking, and refuses to
    intervene if that last-known snapshot had `mission.cutscene_active` true.
    A frozen tick whose last known moment was mid-cutscene is left alone; one
    whose last known moment was NOT a cutscene (a failure screen, a menu) is
    what this intervenes on.

    One call per poll tick, `feed(state)`, returns the key to press right
    now or `None`. Bounded to :data:`MAX_STALL_RECOVERY_ATTEMPTS` whole
    attempts; past that it logs loudly exactly once and stands down rather
    than pressing keys forever — a stuck server needs a human at that point,
    not a bot hammering Enter.
    """

    clock: Any = time.monotonic
    stall_timeout_s: float = SCRIPT_STALL_TIMEOUT_S
    retry_gap_s: float = STALL_RECOVERY_RETRY_GAP_S
    max_attempts: int = MAX_STALL_RECOVERY_ATTEMPTS
    _last_tick: int | None = None
    #: Wall-clock time `_last_tick` was last SEEN TO CHANGE — the stall clock
    #: runs from here, not from the first repeated observation, because the
    #: tick was already sitting at this value for the whole gap between that
    #: change and the poll that noticed it repeating.
    _last_tick_changed_at: float | None = None
    _last_cutscene_active: bool = False
    _attempts: int = 0
    _last_attempt_at: float = 0.0
    _gave_up: bool = False
    #: True while this watchdog believes the game is sitting on a modal screen
    #: RIGHT NOW — set the moment the stall passes `stall_timeout_s`, cleared
    #: the moment `state.tick` moves again, and deliberately still true after
    #: `_gave_up` (giving up on the keypresses does not un-block the screen).
    #:
    #: It exists because the screen is user-visible: measured on the broadcast,
    #: the game was showing "MISSION FAILED / Franklin lost Lamar" while the
    #: live commentary read "Alpha's right there. Staying on his six." for over
    #: a minute. `/state` is frozen at that point, so every world fact the
    #: brain is given is a stale lie; main uses this flag to say so in the
    #: prompt, to stop posting tasks nothing will run, and to drop the stale
    #: mission context.
    blocked: bool = False

    def feed(self, state: GameState) -> str | None:
        """Returns a key name from :data:`BLOCKING_SCREEN_KEYS` to press via
        `Primitives.press_key` right now, or `None`. :attr:`blocked` carries
        the standing answer to "is the game on a modal screen"."""
        now = self.clock()
        if self._last_tick is None or state.tick != self._last_tick:
            # A fresh tick: the script thread is (or is again) alive.
            if self._attempts > 0:
                log.info(
                    "script thread ticking again; blocking-screen watchdog stands down",
                    extra={"kv": {"attempts": self._attempts}},
                )
            self._last_tick = state.tick
            self._last_tick_changed_at = now
            self._last_cutscene_active = state.mission.cutscene_active
            self._attempts = 0
            self._gave_up = False
            self.blocked = False
            return None

        # Same tick as last observed: possibly stalled.
        if self._last_cutscene_active:
            self.blocked = False
            return None  # legitimate cutscene: the world is allowed to hold still
        elapsed = now - (self._last_tick_changed_at or now)
        if elapsed < self.stall_timeout_s:
            return None
        self.blocked = True
        if self._gave_up:
            return None
        if self._attempts >= self.max_attempts:
            self._gave_up = True
            log.error(
                "blocking-screen watchdog gave up: the script thread is still "
                "not ticking after every bounded key-press attempt — needs a "
                "human on the server",
                extra={"kv": {"attempts": self._attempts, "stalled_for_s": round(elapsed, 1)}},
            )
            return None
        if self._attempts > 0 and now - self._last_attempt_at < self.retry_gap_s:
            return None

        key = BLOCKING_SCREEN_KEYS[min(self._attempts, len(BLOCKING_SCREEN_KEYS) - 1)]
        self._attempts += 1
        self._last_attempt_at = now
        log.warning(
            "script thread appears stalled on a blocking screen; pressing a "
            "key to try to clear it (see BLOCKING_SCREEN_KEYS)",
            extra={"kv": {"key": key, "attempt": self._attempts, "stalled_for_s": round(elapsed, 1)}},
        )
        return key


#: Vehicle-search radii, in order, as being stranded drags on. The bridge
#: clamps whatever it considers unreasonable; escalating here just stops the
#: harness from asking the same failing question forever.
STRANDED_RADII_M: tuple[float, ...] = (50.0, 90.0, 140.0)


@dataclass
class StrandedEscalator:
    """On foot with no task: widen the vehicle search, then hand back to the brain.

    Returns an action for the first few attempts, then None — at which point
    the decision is genuinely a creative one (walk somewhere? wait for
    traffic?) and belongs to the model, not to a reflex.
    """

    attempts: int = 0
    _last_attempt_at: float = 0.0
    retry_gap_s: float = 12.0
    clock: Any = time.monotonic

    def reset(self) -> None:
        self.attempts = 0
        self._last_attempt_at = 0.0

    def check(self, state: GameState) -> dict[str, Any] | None:
        if state.player.in_vehicle or state.last_task.status == "running":
            self.reset()
            return None
        now = self.clock()
        if now - self._last_attempt_at < self.retry_gap_s:
            return None
        if self.attempts >= len(STRANDED_RADII_M):
            return None
        radius = STRANDED_RADII_M[self.attempts]
        self._last_attempt_at = now
        self.attempts += 1
        log.info(
            "stranded on foot; widening vehicle search",
            extra={"kv": {"attempt": self.attempts, "radius_m": radius}},
        )
        return {
            "type": "enter_nearest_vehicle",
            "params": {"prefer": "any", "search_radius_m": radius},
        }


# --- game restart -------------------------------------------------------------


@dataclass
class GameRestartDetector:
    """Spots a new game process behind the same bridge URL.

    The bridge's `tick` counter starts over when GTA5.exe restarts (watchdog
    relaunch after a crash). Without this, the perception layer compares the
    first post-restart snapshot against a pre-crash one and emits fabricated
    events: a `death` because the player was dead when the game died, a
    `wanted_change` from a stale star count, a `task_finished` for a task that
    no longer exists. Everything stateful must be reset first.
    """

    last_tick: int | None = None
    last_bridge_version: str | None = None
    restarts: int = 0

    def check(self, state: GameState) -> bool:
        """True exactly once per detected restart."""
        tick, version = state.tick, state.bridge.version
        restarted = self.last_tick is not None and (
            tick < self.last_tick or version != self.last_bridge_version
        )
        if restarted:
            self.restarts += 1
            log.warning(
                "game/bridge restart detected; resetting perception state",
                extra={
                    "kv": {
                        "previous_tick": self.last_tick,
                        "tick": tick,
                        "previous_version": self.last_bridge_version,
                        "version": version,
                        "restarts": self.restarts,
                    }
                },
            )
        self.last_tick = tick
        self.last_bridge_version = version
        return restarted


# --- bridge down --------------------------------------------------------------


@dataclass
class BridgeDownTracker:
    """Counts consecutive failures; escalates to a §4 bridge_down event at 3."""

    event_threshold: int = 3
    clock: Any = time.monotonic
    consecutive_failures: int = 0
    _down_since: float | None = None
    _event_emitted: bool = False

    def record_failure(self, exc: BridgeDownError) -> dict[str, Any] | None:
        """Returns a bridge_down §4 payload exactly once per outage."""
        self.consecutive_failures += 1
        if self._down_since is None:
            self._down_since = self.clock()
        log.warning(
            "bridge poll failed",
            extra={"kv": {"consecutive": self.consecutive_failures, "error": str(exc)[:120]}},
        )
        if self.consecutive_failures >= self.event_threshold and not self._event_emitted:
            self._event_emitted = True
            return {"consecutive_failures": self.consecutive_failures}
        return None

    def record_success(self) -> dict[str, Any] | None:
        """Returns a bridge_up §4 payload when an outage just ended."""
        if self._down_since is None:
            self.consecutive_failures = 0
            return None
        downtime = self.clock() - self._down_since
        was_reported = self._event_emitted
        self.consecutive_failures = 0
        self._down_since = None
        self._event_emitted = False
        if was_reported:
            log.info("bridge recovered", extra={"kv": {"downtime_s": round(downtime, 1)}})
            return {"downtime_s": round(downtime, 1)}
        return None

    def backoff_s(self) -> float:
        """Poll backoff while down: 1,2,4,8,… capped at 15 s."""
        return min(15.0, 2 ** min(self.consecutive_failures, 4))


# --- bridge answering but not ready (CONTRACTS v1.2 transient 503s) -----------


#: While the bridge is answering "not now", back off gently: it usually clears
#: within a loading screen. 0.5, 1, 2, 4, then 5 s.
_STALL_BACKOFF_CAP_S = 5.0


@dataclass
class BridgeStallTracker:
    """The bridge answered, but has no snapshot to give yet.

    `not_ready` / `game_thread_stalled` / `queue_full` (and any code this
    contract version does not know) are the bridge working correctly while the
    game is on a loading screen or streaming the world in. They are deliberately
    NOT reported as `bridge_down`: §4's `bridge_down` means the harness cannot
    reach the bridge at all, and claiming an outage that is not happening would
    put a false line on the site. They are logged when a stall starts, every
    `log_every_s` while it lasts, and once when it clears.
    """

    clock: Any = time.monotonic
    log_every_s: float = 10.0
    consecutive: int = 0
    _since: float | None = None
    _last_log: float = 0.0

    def record_failure(self, exc: BridgeApiError) -> float:
        """Log the stall (rate-limited) and return how long to wait before retrying."""
        now = self.clock()
        self.consecutive += 1
        first = self._since is None
        if first:
            self._since = now
        if first or now - self._last_log >= self.log_every_s:
            self._last_log = now
            log.warning(
                "bridge has no snapshot yet; waiting (normal during startup/loading)",
                extra={
                    "kv": {
                        "error": exc.error,
                        "status": exc.status,
                        "consecutive": self.consecutive,
                        "stalled_for_s": round(now - (self._since or now), 1),
                        "detail": exc.detail[:120],
                    }
                },
            )
        return self.backoff_s()

    def record_success(self) -> float | None:
        """Returns how long the stall lasted when one just ended, else None."""
        if self._since is None:
            self.consecutive = 0
            return None
        stalled_for = self.clock() - self._since
        self.consecutive = 0
        self._since = None
        self._last_log = 0.0
        log.info("bridge is publishing snapshots again", extra={"kv": {"stalled_for_s": round(stalled_for, 1)}})
        return stalled_for

    def backoff_s(self) -> float:
        return min(_STALL_BACKOFF_CAP_S, 0.5 * 2 ** min(max(self.consecutive - 1, 0), 4))


# --- screen capture that cannot own the loop thread ---------------------------

#: How long the loop thread will wait for a frame before giving up on it for
#: this tick. A dxcam grab is normally a few milliseconds; anything past this is
#: the capture device in trouble, and the loop has perception, reflexes and a
#: heartbeat to run.
GRAB_TIMEOUT_S = 0.5
#: Failures are counted inside a sliding window: ten bad grabs spread over an
#: afternoon is a flaky display, ten in a minute is a display that is gone.
GRAB_FAILURE_WINDOW_S = 60.0
#: Consecutive failures inside the window before screen capture is given up on
#: for the rest of the session.
GRAB_FAILURES_BEFORE_DISABLE = 10


class OffLoopGrab:
    """Runs one slow, blocking call on a worker thread with a hard deadline.

    Built for exactly one job: `dxcam` screen capture. Observed live on the
    real server — the display flipped to exclusive fullscreen, dxcam logged
    "Output change/access loss detected", and its INTERNAL recovery loop
    retried inside `grab()` for 172 s (16:53:53 -> 16:56:35, "Output recovery
    succeeded after 90 attempt(s)"), with a second block of 76 s earlier. That
    call was made straight from the main loop, so for those three minutes the
    harness produced no perception, no reflex, no decision and no heartbeat:
    the show was frozen because a screenshot was slow.

    Contract:

    * :meth:`poll` never blocks longer than ``timeout_s``;
    * at most one worker is ever in flight — a wedged grab is left alone
      rather than piled on with more grabs of the same device;
    * the worker is a daemon thread, so a permanently wedged capture device
      can never hold up shutdown;
    * a run of failures flips :attr:`dead`, and the owner is expected to drop
      screen capture for the session (the show keeps running, screenshots
      degrade to unavailable).

    A late result from a timed-out attempt IS used when it eventually arrives:
    a capture that takes 0.6 s on a 0.5 s deadline is still telling the truth
    about the screen, just one tick later, and discarding it would mean a
    permanently blind harness on a merely slow machine.
    """

    def __init__(
        self,
        work: Any,
        *,
        name: str = "grab",
        timeout_s: float = GRAB_TIMEOUT_S,
        window_s: float = GRAB_FAILURE_WINDOW_S,
        max_failures: int = GRAB_FAILURES_BEFORE_DISABLE,
        clock: Any = time.monotonic,
    ) -> None:
        self._work = work
        self._name = name
        self._timeout_s = timeout_s
        self._window_s = window_s
        self._max_failures = max_failures
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._value: Any = None
        self._error: BaseException | None = None
        self._timed_out = False
        self._window_started = 0.0
        self.failures = 0
        self.dead = False
        self.last_reason = ""

    def poll(self) -> Any:
        """Return this tick's result, or None if there is not one to be had."""
        if self.dead:
            return None
        if self._thread is None:
            self._done.clear()
            self._value = None
            self._error = None
            self._timed_out = False
            self._thread = threading.Thread(
                target=self._run, name=self._name, daemon=True
            )
            self._thread.start()
            ready = self._done.wait(self._timeout_s)
        else:
            # A previous attempt is still out there. Never wait on it twice:
            # that is how a 172 s grab turns into a 172 s stall one 0.5 s slice
            # at a time.
            ready = self._done.is_set()
        if not ready:
            if not self._timed_out:
                self._timed_out = True
                self._record_failure(
                    f"screen grab did not return within {self._timeout_s:.2f}s"
                )
            return None
        self._thread = None
        self._timed_out = False
        error, self._error = self._error, None
        value, self._value = self._value, None
        if error is not None:
            self._record_failure(f"{type(error).__name__}: {error}")
            return None
        self.failures = 0
        self._window_started = 0.0
        return value

    def _run(self) -> None:
        try:
            self._value = self._work()
        except BaseException as exc:
            # Handed back to the loop thread as a counted failure. Nothing may
            # escape a worker thread: an unhandled exception here would kill
            # capture silently and leave the loop waiting on an Event forever.
            self._error = exc
        finally:
            self._done.set()

    def _record_failure(self, reason: str) -> None:
        now = self._clock()
        if self._window_started == 0.0 or now - self._window_started > self._window_s:
            self._window_started = now
            self.failures = 0
        self.failures += 1
        self.last_reason = reason
        log.warning(
            "screen grab failed",
            extra={"kv": {"reason": reason[:200], "failures": self.failures}},
        )
        if self.failures >= self._max_failures:
            self.dead = True


# --- Claude API backoff -------------------------------------------------------


#: Minimum backoff per failure cause. A 429 that comes back in two seconds is
#: just another 429; an overloaded (529) upstream wants real room. Anything
#: else (network blip, a bad response) retries fast.
BACKOFF_FLOOR_S: dict[str, float] = {
    "rate_limit": 30.0,
    "overloaded": 15.0,
    # A decision the model produced but OUR schema rejected. The API is
    # healthy; the only thing wrong is one response. There is nothing to wait
    # out, so the floor is zero — and `main._think` does not even record it as
    # a failure (see `invalid_output` there). The entry exists so that if any
    # other caller does record it, it is not silently downgraded to "other"
    # and given the outage escalation.
    "invalid_output": 0.0,
    "other": 0.0,
}
#: Cause names this backoff understands. `classify_api_failure` maps exceptions
#: onto them so main never has to reason about HTTP status codes.
API_FAILURE_CAUSES: tuple[str, ...] = (
    "rate_limit",
    "overloaded",
    "invalid_output",
    "other",
)


def _is_anthropic_api_error(exc: BaseException) -> bool:
    """True when `exc` is one of the SDK's own error types.

    Checked by walking the class MRO by name/module rather than importing
    anthropic, so this module keeps working (and keeps classifying) whether the
    SDK raised a typed error or a plain transport error.
    """
    return any(
        base.__name__ in ("APIError", "AnthropicError")
        and base.__module__.split(".")[0] == "anthropic"
        for base in type(exc).__mro__
    )


def _is_invalid_output(exc: BaseException | None) -> bool:
    """True when the chain is a LOCAL rejection of the model's output.

    That means a `pydantic.ValidationError` (a `ValueError` subclass) or a bare
    `ValueError` — the shapes `brain.tactical`/`brain.director` raise when a
    response fails the decision schema or comes back unparseable — and NO
    `anthropic` API error anywhere in the chain. If the SDK complained, the
    call itself failed and this is not a schema problem.
    """
    seen: set[int] = set()
    local = False
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if _is_anthropic_api_error(exc):
            return False
        if isinstance(exc, ValueError):
            local = True
        exc = exc.__cause__
    return local


def classify_api_failure(exc: BaseException | None) -> str:
    """Map an exception (or a DecisionFailedError's __cause__) onto a cause.

    Kept string-based and import-light so it works whether the SDK raised a
    typed error or a plain transport error.
    """
    root = exc
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        name = type(exc).__name__
        if name == "RateLimitError":
            return "rate_limit"
        if name in ("InternalServerError", "APIStatusError"):
            status = getattr(exc, "status_code", None)
            if status == 429:
                return "rate_limit"
            if status in (500, 502, 503, 529):
                return "overloaded"
        status = getattr(exc, "status_code", None)
        if status == 429:
            return "rate_limit"
        if status == 529:
            return "overloaded"
        if status == 400 and _is_quota_message(str(exc)):
            # An org spend cap / exhausted credit arrives as a 400, not a 429,
            # and it stays true for hours or days. Retrying it every two
            # seconds is pure log noise.
            return "rate_limit"
        exc = exc.__cause__
    # Nothing in the chain said the API was unhappy. Before calling this an
    # unknown transport failure, check whether it is OUR schema rejecting the
    # model's answer — observed live for minutes at a stretch while the API was
    # perfectly healthy, escalating the outage backoff 1.8 -> 4.1 -> 6.8 ->
    # 16.5 s and leaving the agent standing still between attempts for a bug that
    # had nothing to do with the network.
    if _is_invalid_output(root):
        return "invalid_output"
    return "other"


def _is_quota_message(text: str) -> bool:
    """Heuristic, and safe if it misses: an unmatched message just backs off less."""
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in ("usage limit", "credit balance", "quota", "spend limit")
    )


def _retry_after_s(exc: BaseException | None) -> float | None:
    """Honour a server-sent retry-after header when the SDK exposed one."""
    while exc is not None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is not None:
            for key in ("retry-after", "anthropic-ratelimit-requests-reset"):
                raw = headers.get(key)
                if raw:
                    try:
                        return max(0.0, float(raw))
                    except (TypeError, ValueError):
                        pass
        exc = exc.__cause__
    return None


@dataclass
class ApiBackoff:
    """Exponential backoff with jitter for Claude API failures.

    While backing off, the reflex layer keeps control (long-running bridge
    tasks + idle behaviors); no model call is attempted before `ready()`.
    Rate limits and upstream overload get a floor so the harness stops hammering
    an endpoint that has already told it to wait.
    """

    base_s: float = 2.0
    cap_s: float = 300.0
    rng: random.Random = field(default_factory=random.Random)
    clock: Any = time.monotonic
    failures: int = 0
    last_cause: str = "other"
    _blocked_until: float = 0.0

    def record_failure(self, cause: str = "other", exc: BaseException | None = None) -> float:
        if cause not in BACKOFF_FLOOR_S:
            cause = "other"
        self.failures += 1
        self.last_cause = cause
        delay = min(self.cap_s, self.base_s * (2 ** (self.failures - 1)))
        delay = max(delay, BACKOFF_FLOOR_S[cause])
        retry_after = _retry_after_s(exc)
        if retry_after is not None:
            delay = max(delay, retry_after)
        delay = min(self.cap_s, delay * self.rng.uniform(0.8, 1.2))
        self._blocked_until = self.clock() + delay
        log.warning(
            "brain call failed; backing off",
            extra={
                "kv": {
                    "failures": self.failures,
                    "cause": cause,
                    "retry_after_s": retry_after,
                    "backoff_s": round(delay, 1),
                }
            },
        )
        return delay

    def record_success(self) -> None:
        self.failures = 0
        self.last_cause = "other"
        self._blocked_until = 0.0

    def ready(self) -> bool:
        return self.clock() >= self._blocked_until

    def blocked_for_s(self) -> float:
        return max(0.0, self._blocked_until - self.clock())


# --- the anti-idle breaker ------------------------------------------------------

#: How far he has to actually travel for this not to count as standing still.
IDLE_MOVE_M = 6.0

#: Radius inside which a run of positions counts as "the same place" for
#: :meth:`IdleBreaker.going_nowhere_s`. Bigger than IDLE_MOVE_M on purpose: this
#: is not "did he twitch", it is "has he actually got anywhere".
GOING_NOWHERE_RADIUS_M = 15.0

#: Nothing older than this is kept, so the answer is always about the recent
#: past rather than the whole session.
GOING_NOWHERE_WINDOW_S = 60.0

#: Positions are recorded no more often than this. The poll runs several times
#: a second and the question is measured in tens of seconds; storing every
#: sample would just make the scan longer for no extra truth.
GOING_NOWHERE_SAMPLE_S = 0.5
#: How long he may stand inside that circle before the breaker fires. Longer
#: than the bridge's own no-progress cycle (10 s, one re-issue, 10 s more) on
#: purpose: a task that is quietly failing gets its full chance to say so before
#: this rung overrules everybody. Still a fraction of the ten minutes in one
#: spot that made it necessary.
IDLE_WINDOW_S = 25.0
#: How long a rung is given to work before the next, different one is tried.
IDLE_RUNG_HOLD_S = 9.0
#: How far the "just go somewhere" rungs aim. Comfortably inside the bridge's
#: own nav-mesh leg length, so the walk is one order the game will honour.
IDLE_WALK_NEAR_M = 55.0
IDLE_WALK_FAR_M = 110.0
#: A task type that was running while he stood still is not tried again for
#: this long. Observed live: `enter_nearest_vehicle` failed and was re-issued
#: for ten minutes at a car in a garage he could not reach.
IDLE_TYPE_BAN_S = 30.0


@dataclass
class IdleBreaker:
    """He is not actually moving. Do something DIFFERENT — and keep doing things.

    Everything else that watches for "stuck" is bookkeeping: a goal's own
    watchdog, the step machine's timeout, the bridge's per-task no-progress
    check. All of them can be satisfied while the man on screen stands in one
    spot, because each only judges its own task and the goal engine happily
    re-plans into the same impossible step. Observed live 2026-09-03: ten
    minutes beside a Buffalo in a garage, `enter_nearest_vehicle` failing and
    being re-issued, the brain narrating "taking it" the whole time.

    So this measures the only thing that cannot be argued with — how far the
    player has actually travelled — and when the answer is "nowhere", it forces
    a rung that is deliberately NOT what he was just doing. The rungs cycle, so
    a spot that defeats one is escaped by the next rather than retried:

    0. go somewhere near, on foot (a plain `walk_to`, the one movement order
       measured to work from anywhere);
    1. start a fight with whoever is nearest — loud, always available, and it
       moves him;
    2. go somewhere far, in a different direction;
    3. shoot at the nearest ped if he is holding a gun, else walk again.

    It also reports the task type that was running while he stood still, so the
    caller can refuse that type for a while: re-issuing the exact order that
    was not working is the loop this class exists to break.
    """

    clock: Any = time.monotonic
    rng: Any = field(default_factory=random.Random)
    _anchor: tuple[float, float] | None = None
    _anchor_at: float = 0.0
    _rung: int = 0
    _fired_at: float = 0.0
    _health: int | None = None
    #: Task types seen running while he was stuck, and when they may be tried again.
    _banned: dict[str, float] = field(default_factory=dict)
    #: Recent (t, x, y) samples, for `going_nowhere_s`. Bounded by
    #: GOING_NOWHERE_WINDOW_S in `observe`, so it never grows with session length.
    _trail: deque[tuple[float, float, float]] = field(default_factory=deque)

    def reset(self) -> None:
        self._anchor = None
        self._anchor_at = self.clock()
        self._rung = 0
        self._fired_at = 0.0
        self._trail.clear()

    def bans(self, task_type: str) -> bool:
        """Is this task type refused right now because it left him standing?"""
        return self.clock() < self._banned.get(task_type, 0.0)

    def ban(self, task_type: str, seconds: float) -> None:
        """Refuse `task_type` for `seconds` from now. The operator's `nudge`
        (wasted_harness.operator) uses this so a human "do something else"
        and the watchdog's own verdict share one ban list and one gate."""
        self._banned[task_type] = self.clock() + seconds

    def going_nowhere_s(self) -> float:
        """How long he has been inside one :data:`GOING_NOWHERE_RADIUS_M` circle.

        WHY THIS EXISTS ALONGSIDE `still_for_s`. `still_for_s` is time since he
        last moved six metres from an anchor, so ANY six-metre hop resets it to
        zero. A man pacing between two points eight metres apart therefore reads
        as "moving" forever, and the commentary gate that keys off it never
        fires — which is how the feed filled up with a line every poll about the
        same car while he got nowhere at all: repeated narration about one
        subject while he is doing nothing. This
        answers the different question the feed actually cares about: has he
        BEEN anywhere, not did he just twitch.
        """
        if not self._trail:
            return 0.0
        now, hx, hy = self._trail[-1]
        oldest = now
        for t, x, y in reversed(self._trail):
            dx, dy = x - hx, y - hy
            if math.sqrt((dx * dx) + (dy * dy)) > GOING_NOWHERE_RADIUS_M:
                break
            oldest = t
        return now - oldest

    def observe(self, state: GameState) -> None:
        """Track real displacement. Called every tick, before :meth:`check`."""
        here = (state.player.pos.x, state.player.pos.y)
        now = self.clock()
        if not self._trail or (now - self._trail[-1][0]) >= GOING_NOWHERE_SAMPLE_S:
            self._trail.append((now, here[0], here[1]))
            while self._trail and (now - self._trail[0][0]) > GOING_NOWHERE_WINDOW_S:
                self._trail.popleft()
        health = state.player.health
        losing_health = self._health is not None and health < self._health
        self._health = health
        if self._anchor is None:
            self._anchor, self._anchor_at = here, now
            return
        dx, dy = here[0] - self._anchor[0], here[1] - self._anchor[1]
        if math.sqrt((dx * dx) + (dy * dy)) >= IDLE_MOVE_M or losing_health:
            # He genuinely moved — or something is happening TO him, which is
            # not the dead-quiet nothing this rung exists for. Standing still
            # while a ped works him over is the survival ladder's business, and
            # it is directly above this one. Re-anchor and forgive the ladder.
            self._anchor, self._anchor_at = here, now
            self._rung = 0

    def still_for_s(self) -> float:
        """Seconds inside the same six-metre circle. Zero until first observed —
        an instance that has never seen a snapshot has not been standing still
        since the epoch, which is what an unset anchor would otherwise mean."""
        if self._anchor is None:
            return 0.0
        return self.clock() - self._anchor_at

    def check(self, state: GameState) -> dict[str, Any] | None:
        """The next thing to try, or None while he is moving or unable to act.

        Feeds itself: the displacement anchor and the health reading are updated
        here rather than relying on a separate per-tick call, so any caller gets
        honest numbers and a fresh instance can never believe it has been
        standing still since the epoch.
        """
        self.observe(state)
        p = state.player
        if p.dead or p.arrested or not p.control_enabled or state.mission.cutscene_active:
            self.reset()
            return None
        if p.in_vehicle:
            # In a car, "not moving" already has an owner: `VehicleController`
            # drives away from a seat that has been idle, and runs its own
            # BLOCKED/STUCK ladder for a car that will not go. Walking away from
            # a working car would be worse television than either. This rung is
            # the ON-FOOT case, which is where the ten-minute freeze happened.
            self.reset()
            return None
        now = self.clock()
        if self.still_for_s() < IDLE_WINDOW_S:
            return None
        if now - self._fired_at < IDLE_RUNG_HOLD_S:
            return None
        # A task that FAILED while he stood there does not get another go for a
        # bit. Only a failure: a task merely running while he is stuck may be
        # the very thing about to move him, and banning that was worse than the
        # freeze — it locked `enter_nearest_vehicle` out permanently and left
        # him on foot for good (observed immediately after shipping the ban).
        lt = state.last_task
        if lt is not None and lt.type and lt.status == "failed":
            self._banned[lt.type] = now + IDLE_TYPE_BAN_S
        self._fired_at = now
        rung, self._rung = self._rung % 4, (self._rung + 1) % 4
        action = self._rung_action(state, rung)
        log.warning(
            "not moving; forcing something different",
            extra={
                "kv": {
                    "still_for_s": round(self.still_for_s(), 1),
                    "rung": rung,
                    "action": action["type"],
                    "banned": lt.type if lt is not None else None,
                }
            },
        )
        return action

    def _rung_action(self, state: GameState, rung: int) -> dict[str, Any]:
        if rung == 1:
            mark = self._nearest_ped(state)
            if mark is not None:
                return {"type": "fight_ped", "params": {"handle": mark, "weapon": "unarmed"}}
            return self._walk(state, IDLE_WALK_NEAR_M)
        if rung == 3:
            mark = self._nearest_ped(state)
            weapon = getattr(state.player, "weapon", None)
            armed = weapon is not None and getattr(weapon, "class_", None) == "gun"
            if mark is not None and armed:
                return {"type": "shoot_at", "params": {"handle": mark, "duration_s": 5.0}}
            return self._walk(state, IDLE_WALK_NEAR_M)
        return self._walk(state, IDLE_WALK_FAR_M if rung == 2 else IDLE_WALK_NEAR_M)

    def _walk(self, state: GameState, distance: float) -> dict[str, Any]:
        """A point `distance` away on a bearing he is not already facing."""
        bearing = math.radians(state.player.heading + self.rng.uniform(60.0, 300.0))
        p = state.player.pos
        return {
            "type": "walk_to",
            "params": {
                "x": p.x - (distance * math.sin(bearing)),
                "y": p.y + (distance * math.cos(bearing)),
                "z": p.z,
                "run": True,
            },
        }

    def _nearest_ped(self, state: GameState) -> int | None:
        best, best_d = None, 1e9
        for ped in state.nearby.peds:
            model = (ped.model or "").lower()
            if any(bad in model for bad in ("player_zero", "player_one", "player_two")):
                continue
            if ped.distance < best_d:
                best, best_d = ped.handle, ped.distance
        return best if best_d <= HOSTILE_CLOSE_RADIUS_M else None
