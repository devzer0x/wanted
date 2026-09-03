"""Free-roam goal engine: a catalog of goals the CODE can verify he finished.

The operator's bar for free roam is "keep picking random goals and everything so
it's fun to watch, don't stand even for 30 seconds, keep doing nuisance". The
old free-roam layer (:mod:`behavior.activities`) could not meet that bar for one
structural reason: an activity "succeeded" when its last bridge task returned
`done`. `steal_nicer_car` is one `enter_nearest_vehicle`, and the bridge falls
back to the nearest usable car when nothing outranks the current one
(bridge/src/TaskEngine.cs, `Prefer == "nicer" && bestNicer != null ? bestNicer :
best`), so the activity declared victory in a WORSE car and moved on. Nothing
anywhere checked the world.

So every goal here carries a `done_when` predicate evaluated against `/state`.
A goal is finished when the WORLD says so, not when a task returns.

Three more rules follow from that one:

* **`needs` is a live-state gate.** A goal is only offered when the state that
  makes it possible is actually present — a bike within reach, an empty cop car,
  a random event running. Nothing is offered that cannot start.
* **A picked goal LOCKS.** It owns free roam until `done_when` fires, `timeout_s`
  expires, the in-goal stuck watchdog fails it, or the cops override it. A
  decision from the model that names a different goal mid-flight is ignored
  (:meth:`RoamEngine.blocks_foreign_action`). That single rule is the fix for
  "he chose 'roam around' and then stood still": there is no goal id in the
  catalog that means standing still, and he cannot leave the one he has.
* **The model picks from a LIST, by id.** :meth:`RoamEngine.available` is the
  only menu; :meth:`RoamEngine.model_choice` accepts an id only if it is on it.

**What this module deliberately does NOT contain.** The design brief listed
goals that the frozen action vocabulary (CONTRACTS §1/§2) cannot express, and a
goal whose `done_when` can never fire is worse than no goal: it locks the engine
for `timeout_s` and then reports a failure that was never possible. Each is
recorded in :data:`UNBUILDABLE_GOALS` with the capability it needs, rather than
being shipped as something that can never complete.

**Provenance of the pinned model names.** :data:`POLICE_MODELS` and
:data:`BUS_MODELS` are `VehicleHash` member names lowercased, which is exactly
what the bridge emits (`SnapshotBuilder.VehicleModelName`). They are curated,
NOT verified against the running game — the same honest status the landmark
coordinates in :mod:`behavior.activities` carry. A name that is wrong or missing
costs an offer (the goal is simply not available); it can never make a goal
complete falsely, because the same predicate is used for `needs` and for
`done_when`. Verifying them is live-tuning work, listed in the report.

The rank ladder in :data:`VEHICLE_RANK` is NOT curated: it is a transcription of
the bridge's own `VehicleRank.Rank` switch, and it has to stay one, because it
is the ladder the bridge's `prefer: "nicer"` chooser uses. Reading it from the
harness is the only way `done_when` can tell an upgrade from the fallback.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..bridge_client import GameState, NearbyVehicle
from ..logsetup import get_logger
from .activities import CHAOS_BUDGET_PER_HOUR, LANDMARKS, Activity
from .navigation import planar_distance
from .recovery import STALL_MOVE_M

log = get_logger("wasted.roam")


# --- goals the vocabulary cannot express --------------------------------------

#: Goal id -> the capability that is missing. These are NOT in the catalog on
#: purpose: each one's `done_when` could never fire with the fields /state
#: carries and the 19 actions the schema allows, so shipping them would mean
#: locking free roam for `timeout_s` and then reporting a failure that was
#: structurally guaranteed. Every entry names a specific bridge/contract change.
UNBUILDABLE_GOALS: dict[str, str] = {
    "big_jump": (
        "no airtime. /state has no on-ground flag and no vertical velocity, and "
        "at a 2-4 Hz poll a large z-delta is equally a jump, a hill, a car-park "
        "ramp or a lift. The run-up IS expressible (and ships as `freeway_run`); "
        "'he landed it' needs a bridge-side airtime field — the same gap "
        "events.UNPRODUCED_EVENT_REASONS['stunt'] already documents."
    ),
    # `pick_a_fight` and `gang_trouble` WERE here, ruled out because nothing could
    # start violence against a peaceful ped. That was true of the 19-action
    # catalog and is no longer true: CONTRACTS §1 gained `fight_ped{handle}`, and
    # `TaskEngine.StartFightPed` tasks combat against any named ped regardless of
    # relationship. Both now ship. The remaining half of the gang_trouble gap —
    # "entered Davis/Strawberry ARMED" — is still not computable (PlayerState
    # carries no weapon field), so the offer gates on the gang being present
    # rather than on him being armed, and the goal ends on the health floor if
    # that turns out to have been optimistic.
    "rob_store": (
        "a robbery is aim-a-weapon-and-hold. There is no aim, no fire, no weapon "
        "selection, no threaten; `press_prompt_key` is a single E press. A "
        "`player.cash` delta would be a perfect done_when and nothing in the "
        "vocabulary can produce one. Needs aim/fire-at-entity plus weapon state."
    ),
    "buy_gun": (
        "buying is a menu: browse, select, confirm. `press_prompt_key` is one E "
        "press; there is no up/down/select, no shop state in /state, no weapon "
        "field to confirm the purchase. Walking into the shop is a visual beat, "
        "not a purchase, and must not be dressed up as one."
    ),
    "taxi_ride": (
        "no hail action; `enter_nearest_vehicle` always seats him as DRIVER "
        "(VehicleSeat.Driver is hardcoded in TaskEngine), so there is no passenger "
        "seat; and no fare/destination menu. Needs a seat parameter at minimum. "
        "'Steal a taxi and drive it' is `steal_nice_car`, a different goal."
    ),
}


# --- the world, as this module reads it ---------------------------------------

#: Transcribed from bridge/src/TaskEngine.cs `VehicleRank.Rank`. Keys are the
#: SHVDN `VehicleClass` member name lowercased; `/state` emits the member name
#: verbatim (`SnapshotBuilder`: `Class = v.ClassType.ToString()`). Anything not
#: listed ranks 0, which is the switch's own `default` arm — and is why a police
#: car (class `Emergency`) can never be reached by `prefer: "nicer"`.
VEHICLE_RANK: dict[str, int] = {
    "super": 10,
    "sports": 9,
    "sportsclassics": 8,
    "coupes": 7,
    "muscle": 6,
    "openwheel": 6,
    "sedans": 5,
    "suvs": 4,
    "offroad": 3,
    "motorcycles": 3,
    "compacts": 2,
    "vans": 1,
}

#: The classes that make "that's a nice car" a true statement on air.
FAST_CLASSES: frozenset[str] = frozenset({"super", "sports", "sportsclassics", "muscle"})

#: `VehicleHash` member names, lowercased — the exact literals `/state` emits.
#: Curated, not verified against the running game (see the module docstring).
POLICE_MODELS: frozenset[str] = frozenset(
    {
        "police", "police2", "police3", "police4", "policeold1", "policeold2",
        "policet", "policeb", "sheriff", "sheriff2", "fbi", "fbi2", "riot",
        "pranger", "polmav", "predator",
    }
)
#: Belt and braces on the pinned set: a member name this list has never heard of
#: still reads as police if it starts with one of these. Cheap, and it means a
#: drifted or DLC model costs an offer rather than being silently invisible.
POLICE_MODEL_PREFIXES: tuple[str, ...] = ("police", "sheriff", "fbi")

#: `VehicleHash` member names for buses. Same provenance and same caveat.
BUS_MODELS: frozenset[str] = frozenset(
    {"bus", "coach", "airbus", "rentalbus", "tourbus", "pbus", "pbus2"}
)


def vehicle_rank(vehicle_class: str | None) -> int:
    """The bridge's own rank for a `/state` vehicle class string."""
    return VEHICLE_RANK.get((vehicle_class or "").strip().lower(), 0)


def is_police_model(model: str | None) -> bool:
    name = (model or "").strip().lower()
    return name in POLICE_MODELS or name.startswith(POLICE_MODEL_PREFIXES)


def is_bus_model(model: str | None) -> bool:
    return (model or "").strip().lower() in BUS_MODELS


def is_motorcycle(vehicle_class: str | None) -> bool:
    return (vehicle_class or "").strip().lower() == "motorcycles"


def player_pos(state: GameState) -> tuple[float, float, float]:
    p = state.player.pos
    return (p.x, p.y, p.z)


def health_fraction(state: GameState) -> float:
    p = state.player
    if p.max_health <= 0:
        return 1.0
    return p.health / p.max_health


def _vpos(v: NearbyVehicle) -> tuple[float, float, float] | None:
    return None if v.pos is None else (v.pos.x, v.pos.y, v.pos.z)


# --- thresholds ---------------------------------------------------------------

#: How long he may be effectively stationary with nothing progressing before the
#: engine MUST pick a goal and post an action for it. The operator's bar is "don't
#: stand even for 30 seconds"; this sits under it with room for the pick, the
#: POST and the engine actually starting to move him — at a 2-4 Hz poll that is
#: 40-80 snapshots of evidence, which is the same window
#: recovery.STALL_WINDOW_S uses to be sure a stationary ped is really stuck
#: rather than sitting out a red light or a door animation.
BOREDOM_S = 20.0

#: In-goal stuck watchdog: no meaningful movement for this long while a goal is
#: locked. Deliberately half the boredom window and a fifth of
#: activities.STEP_TIMEOUT_S — a goal that is not moving him is not a goal, and
#: the old 210 s step timeout was the only thing that could end one.
GOAL_STUCK_S = 10.0

#: How far he must travel to count as having moved at all. The same constant
#: recovery.TaskStallDetector measures with, imported rather than re-declared so
#: the two watchdogs can never disagree about what "he moved" means.
GOAL_MOVE_M = STALL_MOVE_M

#: Consecutive stuck windows before the goal FAILS. The first one escalates (a
#: fresh plan from where he actually is, plus a physical nudge in a car); the
#: second gives up, because two 10 s windows with an escalation in between is
#: real evidence and 20 s is already most of the operator's budget.
GOAL_STUCK_STRIKES = 2

#: Completed roam goals before `start_nearest_mission` becomes the ONLY option,
#: and the wall-clock equivalent. The story has to move: the show is missions
#: with free roam between them, not the other way round.
GOALS_BEFORE_MISSION = 3
ROAM_BEFORE_MISSION_S = 15 * 60.0

#: A short jittered beat between goals so the show is not a conveyor belt of set
#: pieces. Two orders of magnitude shorter than the old ACTIVITY_GAP_S (150-420 s)
#: because that gap WAS the standing-still the operator is complaining about; it
#: is skipped entirely when he is not moving (see :meth:`RoamEngine.due`).
ROAM_GAP_S = (2.0, 12.0)

#: Below this fraction of max health only `calm` goals are offered ("need a
#: minute"). Deliberately ABOVE recovery.LOW_HEALTH_FRACTION (0.30): that is the
#: SURVIVAL threshold driving the threat ladder, this is an entertainment
#: threshold deciding what he is invited to do next, and choosing to start a
#: police chase should stop being on the menu well before survival is in doubt.
CALM_HEALTH_FRACTION = 0.40

#: How near a thing has to be to be worth a goal. Matches the design brief's
#: "sports car within 40 m" and keeps `walk_to` legs short enough that a stale
#: `pos` is still roughly right when he gets there.
TRIGGER_RADIUS_M = 40.0

#: `enter_nearest_vehicle` takes no handle, so "which car" is expressed as
#: proximity: walk to within `walk_to`'s 2 m arrival radius, then search a
#: circle small enough that the target is the only candidate in it.
PROXIMITY_SEARCH_RADIUS_M = 6.0
#: Past this, walk to the vehicle first instead of trusting the search radius.
VEHICLE_APPROACH_M = 8.0

#: A vehicle whose `pos` has moved less than this between snapshots is treated
#: as stopped. `nearby.vehicles[]` carries no per-vehicle speed, so this is a
#: cross-tick derivation, not a field read.
VEHICLE_STILL_M = 2.0
VEHICLE_STILL_S = 1.5

#: `freeway_run` completes on DISPLACEMENT, not on a speed reading: a single
#: lucky snapshot at 30 m/s proves nothing, and "on a freeway" is not observable
#: at all (`location.street` is a name with no road-type flag). Straight-line
#: distance from where the goal started is the honest proxy, and it is stated as
#: a proxy in the goal's own description.
FREEWAY_RUN_DISTANCE_M = 1500.0

#: The `close()` outcome that means "a higher owner took the movement wheel".
#: It is the one ending that does NOT cost him the between-goals gap, because
#: it was not his decision to stop. `main.ROAM_PREEMPTED` is the same string;
#: it is defined in both places because neither module may import the other.
PREEMPTED_OUTCOME = "preempted"

#: The one goal that is always offerable, exempt from cooldown, category
#: alternation and the health gate. Named here so the forced-mission menu and
#: the ordinary filter cannot disagree about which goal is the floor.
FALLBACK_GOAL_ID = "roam_the_block"

#: How far `roam_the_block` will look for a car before it gives up and walks.
#: 50 m is the same reach the goal used before; the change is that not finding
#: one is now a branch rather than a dead end.
BLOCK_VEHICLE_RADIUS_M = 50.0

#: `roam_the_block` completes on displacement too — it is the never-stand-still
#: fallback, so "he actually went somewhere" is the whole success condition.
BLOCK_RUN_DISTANCE_M = 400.0

#: Landmark arrival, matching the bridge's planar arrival rule.
LANDMARK_ARRIVE_M = 12.0
HILL_ARRIVE_M = 15.0

#: Landmarks with a climb worth riding a bike up.
HILL_LANDMARKS: tuple[str, ...] = ("mount_chiliad", "vinewood_sign", "galileo_observatory")

#: A landmark nearer than this is not a drive, it is a parking manoeuvre.
MIN_LANDMARK_TRIP_M = 300.0


# --- action helpers (frozen vocabulary only) ----------------------------------


def _walk_to(pos: tuple[float, float, float], run: bool = True) -> dict[str, Any]:
    return {"type": "walk_to", "params": {"x": pos[0], "y": pos[1], "z": pos[2], "run": run}}


def _drive_to(
    pos: tuple[float, float, float], speed: float, style: str, radius: float = 8.0
) -> dict[str, Any]:
    return {
        "type": "drive_to",
        "params": {
            "x": pos[0],
            "y": pos[1],
            "z": pos[2],
            "speed_mps": speed,
            "style": style,
            "arrive_radius_m": radius,
        },
    }


def _waypoint(pos: tuple[float, float, float]) -> dict[str, Any]:
    return {"type": "set_waypoint", "params": {"x": pos[0], "y": pos[1]}}


def _enter(prefer: str, radius: float) -> dict[str, Any]:
    return {
        "type": "enter_nearest_vehicle",
        "params": {"prefer": prefer, "search_radius_m": radius},
    }


def _wander(style: str) -> dict[str, Any]:
    return {"type": "wander_drive", "params": {"style": style}}


def _approach_then_enter(
    v: NearbyVehicle, prefer: str, fallback_radius: float
) -> list[dict[str, Any]]:
    """Walk to a specific vehicle, then take the one that is now under his nose.

    `enter_nearest_vehicle` takes no handle (CONTRACTS §1), so this is the only
    way to say WHICH car: get inside `walk_to`'s 2 m arrival radius, then ask for
    a search circle small enough that nothing else is in it. A pre-v1.6 bridge
    emits no `pos` for nearby vehicles at all, in which case the walk is skipped
    and the wider preference-based search is the honest best effort.
    """
    pos = _vpos(v)
    if pos is None:
        return [_enter(prefer, fallback_radius)]
    steps: list[dict[str, Any]] = []
    if v.distance > VEHICLE_APPROACH_M:
        steps.append(_walk_to(pos, run=True))
    steps.append(_enter(prefer, PROXIMITY_SEARCH_RADIUS_M))
    return steps


# --- the view a predicate sees ------------------------------------------------


@dataclass
class RoamView:
    """Everything a `needs`/`plan` predicate may read besides `/state`.

    Cross-tick derivations live here rather than in the predicates so the
    predicates stay pure functions of (state, view) and are testable one at a
    time. Nothing in here is invented: every field is computed from consecutive
    `/state` snapshots.
    """

    mood: str = "bored"
    mood_style: str = "normal"
    rng: random.Random = field(default_factory=random.Random)
    #: Handles of `nearby.vehicles[]` entries whose `pos` has not moved across
    #: the last :data:`VEHICLE_STILL_S`. The only way to know a bus is stopped.
    stationary_handles: frozenset[int] = frozenset()
    #: Seconds since a mission last started, or None if none has this session.
    since_mission_s: float | None = None
    #: The car he was pulled out of: (handle, pos) when `in_vehicle` went
    #: true -> false with no `exit_vehicle` of our own, and that handle is back
    #: in `nearby.vehicles[]` with a driver in it.
    stolen_from: tuple[int, tuple[float, float, float]] | None = None


# --- selectors over /state ----------------------------------------------------


def empty_vehicles(state: GameState, within_m: float = TRIGGER_RADIUS_M) -> list[NearbyVehicle]:
    return [
        v
        for v in state.nearby.vehicles
        if v.driver == "empty" and v.distance <= within_m
    ]


def nicer_vehicle(state: GameState) -> NearbyVehicle | None:
    """The nearest empty car that outranks what he is in — the bridge's own test.

    `needs` and `done_when` share this ladder deliberately. Offering the goal on
    one definition of "nicer" and grading it on another is how the old activity
    could complete in a worse car.
    """
    baseline = vehicle_rank(state.vehicle.vehicle_class) if state.vehicle else -1
    best: NearbyVehicle | None = None
    for v in empty_vehicles(state):
        if vehicle_rank(v.vehicle_class) <= baseline:
            continue
        if best is None or v.distance < best.distance:
            best = v
    return best


def police_vehicle(state: GameState) -> NearbyVehicle | None:
    best: NearbyVehicle | None = None
    for v in empty_vehicles(state):
        if not is_police_model(v.model):
            continue
        if best is None or v.distance < best.distance:
            best = v
    return best


def stopped_bus(state: GameState, view: RoamView) -> NearbyVehicle | None:
    """A bus that is not moving. A bus in service is moving and `walk_to` would
    chase a `pos` that is already stale, so the stationary check is mandatory."""
    best: NearbyVehicle | None = None
    for v in state.nearby.vehicles:
        if not is_bus_model(v.model) or v.distance > TRIGGER_RADIUS_M:
            continue
        if v.handle not in view.stationary_handles:
            continue
        if best is None or v.distance < best.distance:
            best = v
    return best


def motorcycle(state: GameState) -> NearbyVehicle | None:
    best: NearbyVehicle | None = None
    for v in empty_vehicles(state):
        if not is_motorcycle(v.vehicle_class):
            continue
        if best is None or v.distance < best.distance:
            best = v
    return best


def occupied_vehicle(state: GameState, within_m: float = 15.0) -> NearbyVehicle | None:
    """A car with an NPC in it. `enter_nearest_vehicle` seats him as DRIVER, so
    on an occupied car that is a jack — which is the thing that raises stars."""
    best: NearbyVehicle | None = None
    for v in state.nearby.vehicles:
        if v.driver != "npc" or v.distance > within_m:
            continue
        if best is None or v.distance < best.distance:
            best = v
    return best


def mission_marker(state: GameState) -> tuple[float, float, float] | None:
    """The nearest mission-start marker this protagonist can actually use."""
    me = state.player.protagonist
    here = player_pos(state)
    best: tuple[float, float, float] | None = None
    best_d = float("inf")
    for s in state.mission.starts:
        if s.protagonist not in (me, "unknown") and me != "unknown":
            continue
        target = (s.pos.x, s.pos.y, s.pos.z)
        d = planar_distance(here, target)
        if d < best_d:
            best, best_d = target, d
    return best


def _landmark_choice(state: GameState, view: RoamView) -> tuple[str, tuple[float, float, float]]:
    here = player_pos(state)
    far = [n for n in LANDMARKS if planar_distance(here, LANDMARKS[n]) >= MIN_LANDMARK_TRIP_M]
    name = view.rng.choice(far or list(LANDMARKS))
    return name, LANDMARKS[name]


def _nearest_hill(state: GameState) -> tuple[str, tuple[float, float, float]]:
    here = player_pos(state)
    name = min(HILL_LANDMARKS, key=lambda n: planar_distance(here, LANDMARKS[n]))
    return name, LANDMARKS[name]


# --- the goal ------------------------------------------------------------------

ROAM_CATEGORIES: tuple[str, ...] = (
    "acquisition",
    "scenic",
    "stunt",
    "trouble",
    "errand",
    "mission",
)


@dataclass(frozen=True)
class Goal:
    """One thing to do, with a completion test the code can run.

    `plan` returns (ordered actions, snapshot). The snapshot is the pick-time
    memory `done_when` grades against — the vehicle rank he had when he decided
    to upgrade, the coordinate he chose, where he was standing. Without it
    `done_when` has nothing to compare to and degenerates into "is he in a car",
    which is exactly the bug this module exists to fix.
    """

    id: str
    category: str
    #: <=12 words: this is what the dashboard shows and what the `goal` field of
    #: a decision is allowed to be (CONTRACTS §2 caps it at 12).
    description: str
    #: The quotable line for the offer list when no trigger supplied a better one.
    why: str
    needs: Callable[[GameState, RoamView], bool]
    plan: Callable[[GameState, RoamView], tuple[list[dict[str, Any]], dict[str, Any]]]
    done_when: Callable[[GameState, dict[str, Any]], bool]
    timeout_s: float
    cooldown_s: float
    chaos_cost: float = 0.0
    #: Offered when he is hurt. Everything else is withheld below
    #: :data:`CALM_HEALTH_FRACTION`.
    calm: bool = False
    #: Never offered in the ordinary menu; reached only by an override.
    override_only: bool = False
    #: Never filtered out (cooldown, category, health). The one guaranteed
    #: option, so `available()` is never empty and he is never out of ideas.
    fallback: bool = False
    #: This goal's whole point is to attract police attention, so the
    #: wanted-level override must not kill it the moment it starts working.
    #: Without this, `earn_two_stars` is killed at ONE star — the override fires
    #: on `wanted > 0`, which is a state the goal deliberately creates — and it
    #: can never reach its own `done_when` of two. A goal that can never
    #: complete is worse than one that does not exist.
    wants_heat: bool = False
    #: Posts no actions of its own: another owner (the day planner) does the
    #: work and `done_when` watches for its result.
    handoff: bool = False


# --- needs / plan / done_when --------------------------------------------------
#
# One trio per goal, written out rather than generated, because the whole point
# of the module is that each completion test is inspectable.


def _needs_nicer_car(state: GameState, view: RoamView) -> bool:
    return nicer_vehicle(state) is not None


def _plan_nicer_car(state, view):
    v = nicer_vehicle(state)
    assert v is not None  # guarded by _needs_nicer_car; available() checks needs first
    baseline = vehicle_rank(state.vehicle.vehicle_class) if state.vehicle else 0
    return _approach_then_enter(v, "nicer", TRIGGER_RADIUS_M), {
        "rank": baseline,
        "target_model": v.model,
    }


def _done_nicer_car(state: GameState, snap: dict[str, Any]) -> bool:
    # NOT `in_vehicle` alone: the bridge falls back to the nearest usable car
    # when nothing outranks, so a bare in_vehicle check completes this goal in a
    # WORSE car. The rank has to go UP.
    if not state.player.in_vehicle or state.vehicle is None:
        return False
    return vehicle_rank(state.vehicle.vehicle_class) > snap["rank"]


#: Peds the agent may never start on. Story characters are the show — killing Lamar
#: ends the story arc the whole channel is built around — and cops turn a bit of
#: fun into a wanted level the roam engine then has to spend a goal escaping.
#: Matched as substrings because the bridge emits lowercased model names
#: (`SnapshotBuilder.PedModelName`) and the family is what matters, not the variant.
PROTECTED_PED_MODELS: tuple[str, ...] = (
    # The three protagonists ship as `player_zero` (Michael), `player_one`
    # (Franklin) and `player_two` (Trevor) — their in-fiction names appear
    # nowhere in the model, so matching on "michael" protects nobody.
    "player_zero", "player_one", "player_two",
    "lamar", "franklin", "michael", "trevor", "simeon", "jimmy", "tracey",
    "amanda", "lester", "devin", "stretch", "wade", "ron", "chop",
    "cop", "police", "sheriff", "swat", "army", "security", "fbi", "prisguard",
)

#: Gang ped model families, by the neighbourhood they belong to. Curated from the
#: model-name convention (`g_m_y_*`), NOT verified against the running game — the
#: same honest status the landmark coordinates carry. A wrong name costs an offer;
#: it can never make a goal complete falsely, because the same predicate gates
#: `needs` and `done_when`.
GANG_PED_PREFIXES: tuple[str, ...] = ("g_m_y_", "g_m_m_", "g_f_y_")

#: How close a ped has to be before starting something is plausible rather than a
#: cross-street sprint that ends with him losing interest.
FIGHT_RADIUS_M = 20.0

#: Below this he is not looking for a fight, he is looking for a hospital. The
#: operator's own line: bad judgement is funny, dying every four minutes is not.
FIGHT_MIN_HEALTH = 60.0

#: A gang is worth taking on with a bit more in the tank than a single pedestrian.
GANG_MIN_HEALTH = 100.0

#: Gangs come in numbers; one man on a corner is a `pick_a_fight`, not a gang.
GANG_MIN_PEDS = 2

#: How far the gang has to be for "wrong neighbourhood" to mean anything.
GANG_RADIUS_M = 40.0


def _fightable(ped: Any) -> bool:
    """A ped he is allowed to start on: not protected, not already hostile.

    Already-hostile peds are excluded on purpose — that is retaliation, which the
    threat reflex owns and does better (it fires without a model call). This goal
    is only ever about the agent starting it.
    """
    model = (getattr(ped, "model", "") or "").lower()
    if any(bad in model for bad in PROTECTED_PED_MODELS):
        return False
    return getattr(ped, "relationship", "neutral") != "hostile"


def nearest_mark(state: GameState) -> Any | None:
    peds = [
        p for p in state.nearby.peds
        if p.distance <= FIGHT_RADIUS_M and _fightable(p)
    ]
    return min(peds, key=lambda p: p.distance) if peds else None


def gang_nearby(state: GameState) -> list[Any]:
    return [
        p for p in state.nearby.peds
        if p.distance <= GANG_RADIUS_M
        and any((p.model or "").lower().startswith(g) for g in GANG_PED_PREFIXES)
        and not any(bad in (p.model or "").lower() for bad in PROTECTED_PED_MODELS)
    ]


def _needs_pick_a_fight(state: GameState, view: RoamView) -> bool:
    return (
        not state.player.in_vehicle
        and state.player.wanted == 0
        and state.player.health >= FIGHT_MIN_HEALTH
        and nearest_mark(state) is not None
    )


def _plan_pick_a_fight(state, view):
    mark = nearest_mark(state)
    assert mark is not None
    steps: list[dict[str, Any]] = []
    if mark.distance > VEHICLE_APPROACH_M and getattr(mark, "pos", None) is not None:
        steps.append(_walk_to((mark.pos.x, mark.pos.y, mark.pos.z), run=True))
    steps.append({"type": "fight_ped", "params": {"handle": mark.handle}})
    return steps, {"mark": mark.handle}


def _done_pick_a_fight(state: GameState, snap: dict[str, Any]) -> bool:
    """Over when the mark is no longer standing in front of him: dead, fled, or
    streamed out. `nearby.peds` is top-8 by distance, so absence is the honest
    proxy for "he is not a problem any more" — there is no ped-health field."""
    handle = snap.get("mark")
    return all(p.handle != handle for p in state.nearby.peds)


def _needs_gang_trouble(state: GameState, view: RoamView) -> bool:
    return (
        state.player.health >= GANG_MIN_HEALTH
        and state.player.wanted == 0
        and len(gang_nearby(state)) >= GANG_MIN_PEDS
    )


def _plan_gang_trouble(state, view):
    gang = gang_nearby(state)
    assert gang
    mark = min(gang, key=lambda p: p.distance)
    # Start on the nearest one and let the engine's own combat AI take it from
    # there: the rest of the set will join in without being told, which is the
    # whole point of picking a fight with a gang rather than a pedestrian.
    return (
        [{"type": "fight_ped", "params": {"handle": mark.handle}}],
        {"started_with": len(gang)},
    )


def _done_gang_trouble(state: GameState, snap: dict[str, Any]) -> bool:
    """Over when nothing hostile is left near him, or he is in no state to carry
    on. The health floor is the same one the survival ladder uses, so this goal
    stands down before the reflex layer has to drag him out of it."""
    if state.player.health < GANG_MIN_HEALTH * 0.4:
        return True
    return not any(
        p.relationship == "hostile" and p.distance <= GANG_RADIUS_M
        for p in state.nearby.peds
    )


def _needs_cop_car(state: GameState, view: RoamView) -> bool:
    return police_vehicle(state) is not None


def _plan_cop_car(state, view):
    v = police_vehicle(state)
    assert v is not None
    # `prefer: "nicer"` can NEVER pick a cop car: class Emergency hits the
    # default arm of the bridge's rank switch and scores 0, below every civilian
    # car. It has to be "any" with a radius tight enough to mean this one.
    return _approach_then_enter(v, "any", PROXIMITY_SEARCH_RADIUS_M), {"target_model": v.model}


def _done_cop_car(state: GameState, snap: dict[str, Any]) -> bool:
    return (
        state.player.in_vehicle
        and state.vehicle is not None
        and is_police_model(state.vehicle.model)
    )


def _needs_my_car(state: GameState, view: RoamView) -> bool:
    """He was pulled out of a car and it is still here with somebody in it.

    `view.stolen_from` is the cross-tick derivation: `in_vehicle` went true ->
    false with no `exit_vehicle` of ours, and the handle he was in came back in
    `nearby.vehicles[]` with a driver. This is the ONLY half of the design
    brief's retaliation family that the vocabulary can express — he cannot
    attack the man, but `enter_nearest_vehicle` seats him as DRIVER, so taking
    the car straight back off him is an ordinary jack.
    """
    if state.player.in_vehicle or view.stolen_from is None:
        return False
    handle, _pos = view.stolen_from
    return any(v.handle == handle and v.distance <= TRIGGER_RADIUS_M for v in state.nearby.vehicles)


def _plan_my_car(state, view):
    assert view.stolen_from is not None
    handle, _pos = view.stolen_from
    v = next(v for v in state.nearby.vehicles if v.handle == handle)
    return _approach_then_enter(v, "any", PROXIMITY_SEARCH_RADIUS_M), {"handle": handle}


def _done_my_car(state: GameState, snap: dict[str, Any]) -> bool:
    # Graded on the HANDLE, not on being in some car: getting into a different
    # one is a consolation prize, not the goal he announced.
    return (
        state.player.in_vehicle
        and state.vehicle is not None
        and state.vehicle.handle == snap["handle"]
    )


def _needs_true(state: GameState, view: RoamView) -> bool:
    return True


def _plan_landmark(state, view):
    name, pos = _landmark_choice(state, view)
    steps: list[dict[str, Any]] = []
    if not state.player.in_vehicle:
        steps.append(_enter("any", TRIGGER_RADIUS_M))
    steps += [_waypoint(pos), _drive_to(pos, 16.0, view.mood_style, LANDMARK_ARRIVE_M)]
    return steps, {"target": pos, "landmark": name}


def _done_landmark(state: GameState, snap: dict[str, Any]) -> bool:
    return planar_distance(player_pos(state), snap["target"]) <= LANDMARK_ARRIVE_M


def _needs_freeway_run(state: GameState, view: RoamView) -> bool:
    return (
        state.player.in_vehicle
        and state.vehicle is not None
        and (state.vehicle.vehicle_class or "").strip().lower() in FAST_CLASSES
    )


def _plan_freeway_run(state, view):
    here = player_pos(state)
    # The farthest landmark is the longest legal straight-ish haul on the map;
    # there is no "get on the freeway" action, so distance is the whole request.
    name = max(LANDMARKS, key=lambda n: planar_distance(here, LANDMARKS[n]))
    pos = LANDMARKS[name]
    return [_waypoint(pos), _drive_to(pos, 34.0, "rushed", 25.0), _wander("rushed")], {
        "start": here
    }


def _done_freeway_run(state: GameState, snap: dict[str, Any]) -> bool:
    return planar_distance(player_pos(state), snap["start"]) >= FREEWAY_RUN_DISTANCE_M


def _needs_lose_the_cops(state: GameState, view: RoamView) -> bool:
    return state.player.wanted > 0


def _plan_lose_the_cops(state, view):
    steps: list[dict[str, Any]] = []
    if not state.player.in_vehicle:
        steps.append(_enter("any", 50.0))
    steps.append({"type": "flee_police", "params": {}})
    return steps, {}


def _done_lose_the_cops(state: GameState, snap: dict[str, Any]) -> bool:
    return state.player.wanted == 0


def _needs_two_stars(state: GameState, view: RoamView) -> bool:
    if state.player.wanted >= 2:
        return False
    return state.player.in_vehicle or occupied_vehicle(state) is not None


def _plan_two_stars(state, view):
    steps: list[dict[str, Any]] = []
    if not state.player.in_vehicle:
        v = occupied_vehicle(state)
        if v is not None:
            steps += _approach_then_enter(v, "any", 15.0)
    steps.append(_wander("ignore_lights"))
    return steps, {}


def _done_two_stars(state: GameState, snap: dict[str, Any]) -> bool:
    return state.player.wanted >= 2


def _needs_bus(state: GameState, view: RoamView) -> bool:
    return stopped_bus(state, view) is not None


def _plan_bus(state, view):
    v = stopped_bus(state, view)
    assert v is not None
    return _approach_then_enter(v, "any", PROXIMITY_SEARCH_RADIUS_M), {"target_model": v.model}


def _done_bus(state: GameState, snap: dict[str, Any]) -> bool:
    return (
        state.player.in_vehicle and state.vehicle is not None and is_bus_model(state.vehicle.model)
    )


def _needs_random_event(state: GameState, view: RoamView) -> bool:
    # `random_event_active` only flips true once the event is ALREADY running, so
    # this goal can never be picked in anticipation. Anticipation would need a
    # bridge-side blip scan; being late is honest, inventing a blip is not.
    return state.mission.random_event_active and not state.mission.active


def _plan_random_event(state, view):
    target: tuple[float, float, float] | None = None
    blip = state.mission.objective_blip
    if blip is not None:
        target = (blip.pos.x, blip.pos.y, blip.pos.z)
    elif state.mission.route_blips:
        r = state.mission.route_blips[0]
        target = (r.pos.x, r.pos.y, r.pos.z)
    else:
        peds = [p for p in state.nearby.peds if p.pos is not None]
        if peds:
            nearest = min(peds, key=lambda p: p.distance)
            assert nearest.pos is not None
            target = (nearest.pos.x, nearest.pos.y, nearest.pos.z)
    if target is None:
        return [{"type": "look_around", "params": {}}], {}
    if state.player.in_vehicle:
        return [_drive_to(target, 20.0, view.mood_style, 10.0)], {"target": target}
    return [_walk_to(target, run=True)], {"target": target}


def _done_random_event(state: GameState, snap: dict[str, Any]) -> bool:
    return state.mission.active or not state.mission.random_event_active


def _needs_bike_hills(state: GameState, view: RoamView) -> bool:
    # "Scared the agent does not ride" was prose in the situation playbook; here it
    # is the offer gate, which is the only place a rule like that can bite.
    if view.mood == "scared":
        return False
    return motorcycle(state) is not None


def _plan_bike_hills(state, view):
    v = motorcycle(state)
    assert v is not None
    name, pos = _nearest_hill(state)
    steps = _approach_then_enter(v, "any", PROXIMITY_SEARCH_RADIUS_M)
    steps += [_waypoint(pos), _drive_to(pos, 22.0, "normal", HILL_ARRIVE_M)]
    return steps, {"target": pos, "hill": name}


def _done_bike_hills(state: GameState, snap: dict[str, Any]) -> bool:
    if state.vehicle is None or not is_motorcycle(state.vehicle.vehicle_class):
        return False
    return planar_distance(player_pos(state), snap["target"]) <= HILL_ARRIVE_M


def _needs_start_mission(state: GameState, view: RoamView) -> bool:
    if state.player.wanted > 0 or state.player.dead or state.player.arrested:
        return False
    if health_fraction(state) < 0.5:
        return False  # the day planner refuses below this too; do not promise it
    return mission_marker(state) is not None


def _plan_start_mission(state, view):
    # Deliberately EMPTY. The trip to a mission-start marker is built end to end
    # in behavior.planner (the drive/walk/get-a-car ladder plus the 25 m
    # on-foot final approach the corona needs). Re-implementing it here would be
    # a second engine posting into the same slot, which is the failure mode this
    # module was told not to reproduce. Picking this goal ASKS the day planner
    # for a mission block; `done_when` watches for its result.
    return [], {"marker": mission_marker(state)}


def _done_start_mission(state: GameState, snap: dict[str, Any]) -> bool:
    return state.mission.active


def _plan_roam_the_block(state, view):
    """The floor. This goal is on every menu, so it must ALWAYS produce motion.

    The first version posted `enter_nearest_vehicle` then `wander_drive` and was
    unreachable on foot with no car around: the entry fails, `wander_drive`
    needs a vehicle, and the one goal guaranteeing "never stand still" left him
    standing still. So the on-foot branch now depends on whether a car is
    actually there, and walks when one is not.
    """
    steps: list[dict[str, Any]] = []
    here = player_pos(state)
    if not state.player.in_vehicle:
        reachable = [
            v for v in state.nearby.vehicles
            if v.driver == "empty" and v.distance <= BLOCK_VEHICLE_RADIUS_M
        ]
        if reachable:
            # The nearest car is the nearest thing that turns standing into moving.
            steps.append(_enter("any", BLOCK_VEHICLE_RADIUS_M))
            style = "ignore_lights" if view.mood in ("hyped", "bored") else view.mood_style
            steps.append(_wander(style))
            return steps, {"start": here}
        # No car in reach. Walking is slower television than driving, but it is
        # television; standing in an empty street is not. A landmark is used only
        # as a bearing — `done_when` is displacement, so he does not have to
        # arrive for this to count.
        _, target = _landmark_choice(state, view)
        return [_walk_to(target, run=True)], {"start": here}
    style = "ignore_lights" if view.mood in ("hyped", "bored") else view.mood_style
    return [_wander(style)], {"start": here}


def _done_roam_the_block(state: GameState, snap: dict[str, Any]) -> bool:
    return planar_distance(player_pos(state), snap["start"]) >= BLOCK_RUN_DISTANCE_M


CATALOG: tuple[Goal, ...] = (
    Goal(
        id="steal_nice_car",
        category="acquisition",
        description="upgrade the ride to something with a name",
        why="that's a nice car",
        needs=_needs_nicer_car,
        plan=_plan_nicer_car,
        done_when=_done_nicer_car,
        timeout_s=120.0,
        cooldown_s=4 * 60.0,
        chaos_cost=0.25,
    ),
    Goal(
        id="take_my_car_back",
        category="acquisition",
        description="take my car back off whoever took it",
        why="that's my car",
        needs=_needs_my_car,
        plan=_plan_my_car,
        done_when=_done_my_car,
        timeout_s=90.0,
        cooldown_s=60.0,
        chaos_cost=0.5,
    ),
    Goal(
        id="steal_cop_car",
        category="acquisition",
        description="take the cop car nobody is sitting in",
        why="unattended",
        needs=_needs_cop_car,
        plan=_plan_cop_car,
        done_when=_done_cop_car,
        timeout_s=120.0,
        cooldown_s=20 * 60.0,
        chaos_cost=1.0,
        # Getting into a police car is one of the most reliable ways in the game
        # to earn a star, so without this the wanted-override kills the goal at
        # the exact moment it starts working and `done_when` can never fire —
        # the same never-completes bug `earn_two_stars` had. Heat is a
        # CONSEQUENCE here rather than the objective, but the exemption is the
        # same: the goal owns its own stars until it finishes or times out, and
        # `lose_the_cops` takes over the moment it does.
        wants_heat=True,
    ),
    Goal(
        id="pick_a_fight",
        category="trouble",
        description="start something with the nearest man who is not a cop",
        why="he looks like he has opinions",
        needs=_needs_pick_a_fight,
        plan=_plan_pick_a_fight,
        done_when=_done_pick_a_fight,
        timeout_s=60.0,
        cooldown_s=4 * 60.0,
        chaos_cost=1.0,
    ),
    Goal(
        id="gang_trouble",
        category="trouble",
        description="find out whose corner this is",
        why="wrong neighbourhood",
        needs=_needs_gang_trouble,
        plan=_plan_gang_trouble,
        done_when=_done_gang_trouble,
        timeout_s=120.0,
        cooldown_s=15 * 60.0,
        chaos_cost=2.0,
        # A gang fight makes noise and noise makes stars. Same reasoning as
        # `steal_cop_car`: heat is a consequence, not the aim, but the override
        # must not kill the goal the moment it starts working.
        wants_heat=True,
    ),
    Goal(
        id="hijack_bus",
        category="acquisition",
        description="take the bus, and I mean the whole bus",
        why="public transport",
        needs=_needs_bus,
        plan=_plan_bus,
        done_when=_done_bus,
        timeout_s=120.0,
        cooldown_s=30 * 60.0,
        chaos_cost=0.5,
    ),
    Goal(
        id="drive_to_landmark",
        category="scenic",
        description="drive somewhere worth looking at",
        why="somewhere better than here",
        needs=_needs_true,
        plan=_plan_landmark,
        done_when=_done_landmark,
        timeout_s=300.0,
        cooldown_s=8 * 60.0,
        calm=True,
    ),
    Goal(
        id="bike_hills",
        category="scenic",
        description="take a bike up the nearest hill",
        why="that bike is not doing anything",
        needs=_needs_bike_hills,
        plan=_plan_bike_hills,
        done_when=_done_bike_hills,
        timeout_s=360.0,
        cooldown_s=25 * 60.0,
        chaos_cost=0.25,
    ),
    Goal(
        id="freeway_run",
        category="stunt",
        # Says "proxy" on the tin: "on a freeway" is not observable, so this is
        # graded on distance covered, not on which road he covered it on.
        description="open it up and cover real ground",
        # NOT "open road": that is the TRIGGER's line, earned by actually being
        # in a fast car at speed. Two identical strings would make an offer that
        # merely qualified indistinguishable from one the world provoked.
        why="nothing between here and there",
        needs=_needs_freeway_run,
        plan=_plan_freeway_run,
        done_when=_done_freeway_run,
        timeout_s=300.0,
        cooldown_s=15 * 60.0,
        chaos_cost=0.25,
    ),
    Goal(
        id="earn_two_stars",
        category="trouble",
        description="get the police genuinely interested",
        why="it has been quiet",
        needs=_needs_two_stars,
        plan=_plan_two_stars,
        done_when=_done_two_stars,
        # No action GUARANTEES stars, so the timeout is doing real work here: it
        # is the difference between "provoking" and "driving badly forever".
        timeout_s=180.0,
        cooldown_s=60 * 60.0,
        chaos_cost=2.0,
        wants_heat=True,
    ),
    Goal(
        id="lose_the_cops",
        category="trouble",
        description="lose them, then act like it was the plan",
        why="they are on me",
        needs=_needs_lose_the_cops,
        plan=_plan_lose_the_cops,
        done_when=_done_lose_the_cops,
        timeout_s=300.0,
        cooldown_s=0.0,
        calm=True,
        override_only=True,
    ),
    Goal(
        id="random_event",
        category="errand",
        description="see what this is before it stops happening",
        why="something's happening",
        needs=_needs_random_event,
        plan=_plan_random_event,
        done_when=_done_random_event,
        timeout_s=240.0,
        cooldown_s=5 * 60.0,
        calm=True,
    ),
    Goal(
        id="start_nearest_mission",
        category="mission",
        description="go and start the nearest job",
        why="let's get paid",
        needs=_needs_start_mission,
        plan=_plan_start_mission,
        done_when=_done_start_mission,
        # The day planner's own budget for the whole trip is 360 s plus a 45 s
        # wait in the marker; this outlasts both so the planner, not this
        # engine, is the one that decides the trip failed.
        timeout_s=420.0,
        cooldown_s=0.0,
        calm=True,
        handoff=True,
    ),
    Goal(
        id="roam_the_block",
        category="errand",
        description="drive around and see what turns up",
        why="anything beats standing here",
        needs=_needs_true,
        plan=_plan_roam_the_block,
        done_when=_done_roam_the_block,
        timeout_s=180.0,
        cooldown_s=30.0,
        calm=True,
        fallback=True,
    ),
)

GOALS_BY_ID: dict[str, Goal] = {g.id: g for g in CATALOG}
ROAM_GOAL_IDS: tuple[str, ...] = tuple(g.id for g in CATALOG)
#: The ids a day plan / a model preference may name. The overrides are reached
#: by the world being a certain way, never by anybody asking for them.
PICKABLE_GOAL_IDS: tuple[str, ...] = tuple(
    g.id for g in CATALOG if not g.override_only and not g.fallback
)


def as_activity(goal: Goal) -> Activity:
    """The goal as the step-runner's own record type.

    :class:`behavior.activities.ActivityRunner` is kept as the step machine (its
    task-id binding beat a real 3 Hz-poll-vs-60 Hz-game race and is not worth
    re-earning), and `Activity` is the record it carries. Nothing is invented
    here: every field is the goal's own. `weight` is 0.0 and honest — this engine
    does not select by weight, it selects by `needs`, and a weight of 0 is
    exactly what `ActivityPicker.eligible` would refuse to draw, which is correct
    because these goals are not in its catalog and must never be drawn by it.
    """
    return Activity(
        name=goal.id,
        weight=0.0,
        cooldown_s=goal.cooldown_s,
        chaos_cost=goal.chaos_cost,
        category=goal.category,
        mood_affinity={},
        plan=(),
        brief=goal.description,
    )


# --- opportunistic triggers ----------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """A reason to put one goal at the TOP of the offer list, with a quotable why."""

    goal_id: str
    why: str
    match: Callable[[GameState, RoamView], bool]


def _t_nice_car(state: GameState, view: RoamView) -> bool:
    v = nicer_vehicle(state)
    return v is not None and (v.vehicle_class or "").strip().lower() in FAST_CLASSES


def _t_my_car(state: GameState, view: RoamView) -> bool:
    return view.stolen_from is not None


def _t_open_road(state: GameState, view: RoamView) -> bool:
    return _needs_freeway_run(state, view) and state.vehicle is not None and state.vehicle.speed >= 30.0


def _t_get_paid(state: GameState, view: RoamView) -> bool:
    return view.since_mission_s is None or view.since_mission_s >= ROAM_BEFORE_MISSION_S


#: Order matters only as a tiebreak: the first matching trigger wins the top
#: slot. Everything here is computable from `/state` plus a cross-tick diff; the
#: brief's other triggers are recorded in :data:`UNCOMPUTABLE_TRIGGERS`.
TRIGGERS: tuple[Trigger, ...] = (
    Trigger("lose_the_cops", "they are on me", lambda s, v: s.player.wanted > 0),
    Trigger("take_my_car_back", "that's my car", _t_my_car),
    Trigger("steal_cop_car", "unattended", _needs_cop_car),
    Trigger("steal_nice_car", "that's a nice car", _t_nice_car),
    Trigger("random_event", "something's happening", _needs_random_event),
    Trigger("hijack_bus", "public transport", _needs_bus),
    Trigger("freeway_run", "open road", _t_open_road),
    Trigger("start_nearest_mission", "let's get paid", _t_get_paid),
    Trigger(
        "drive_to_landmark",
        "need a minute",
        lambda s, v: health_fraction(s) < CALM_HEALTH_FRACTION,
    ),
)

#: Triggers from the design brief that `/state` cannot support, with the reason.
#: Recorded rather than approximated: a trigger that fires on a guess is worse
#: than one that never fires, because the `why` it quotes on air would be false.
UNCOMPUTABLE_TRIGGERS: dict[str, str] = {
    "a '?' blip appeared": (
        "/state exposes no generic blip list. `mission.starts[]` is M/F/T "
        "mission-start markers only; `route_blips[]` exists only once the GAME "
        "has plotted a GPS route; `random_event_active` flips true only once the "
        "event is already running. Needs a bridge-side blip scan (CONTRACTS §3)."
    ),
    "entered Davis/Strawberry ARMED": (
        "the zone half is computable (`location.zone`); the armed half is not. "
        "PlayerState carries no weapon, no ammo and no weapon-wheel field. "
        "Needs `player.weapon`."
    ),
    "on a FREEWAY": (
        "`location.street` is a name string with no road-type flag. A freeway "
        "check would need a curated street-name list, which is authored data, "
        "not a computation. `freeway_run` substitutes a distance proxy and its "
        "description says so."
    ),
    "a ped just hit him": (
        "computable (behavior.recovery.DamageTracker already computes it) but "
        "deliberately NOT wired to a roam goal: retaliation is owned by the "
        "threat reflex, which outranks free roam for the tick. A second engine "
        "answering the same signal is the two-owners bug, not a feature."
    ),
    "a bus PASSING (vs present)": (
        "`nearby.vehicles[]` carries no per-vehicle speed. Motion is derived by "
        "diffing `pos` per handle across ticks (RoamView.stationary_handles), "
        "which the 8-nearest truncation can break mid-sequence — so the goal is "
        "gated on a bus being STOPPED, which is what `walk_to` needs anyway."
    ),
}


# --- offers and the lock -------------------------------------------------------


@dataclass(frozen=True)
class Offer:
    goal: Goal
    why: str
    triggered: bool = False

    @property
    def id(self) -> str:
        return self.goal.id


@dataclass
class LockedGoal:
    """`roam.current`: the goal that owns free roam until the world says otherwise."""

    goal: Goal
    why: str
    plan: list[dict[str, Any]]
    snapshot: dict[str, Any]
    started_at: float
    #: Replans used up. The first plan-exhaustion without `done_when` gets one
    #: fresh plan from where he actually ended up; after that the goal fails.
    attempts: int = 1
    strikes: int = 0

    def elapsed(self, now: float) -> float:
        return now - self.started_at


#: How many times a goal may re-plan when its actions all completed and
#: `done_when` is still false. One retry: the plan was built from a snapshot that
#: is now old (the car drove off, the bus left), and rebuilding it from where he
#: actually is turns a dead lock into a second honest try. Two would be stubborn.
MAX_GOAL_ATTEMPTS = 2


class RoamEngine:
    """One free-roam owner: offers goals, locks one, and grades it against /state.

    Deliberately does not touch the bridge, the writer or the clock beyond
    `clock()`. It answers questions and returns actions; `main` posts them
    through the single free-roam slot, so there is exactly one engine and
    exactly one place a movement task can come from.
    """

    def __init__(
        self,
        rng: random.Random | None = None,
        clock: Any = time.monotonic,
        *,
        missions_enabled: bool = True,
    ) -> None:
        self._rng = rng or random.Random()
        #: Operator switch (Settings.missions_enabled). Off: `start_nearest_mission`
        #: is never offered and never forced, so free roam is the whole show.
        self._missions_enabled = missions_enabled
        self._clock = clock
        self.current: LockedGoal | None = None

        self._last_run: dict[str, float] = {}
        self._chaos_spent: list[tuple[float, float]] = []
        self._last_category: str | None = None
        self._completed_since_mission = 0
        self._roam_started_at = clock()
        self._next_allowed_at = clock()

        #: The ids most recently OFFERED. The model may only choose from these.
        self._offered: tuple[str, ...] = ()
        self._offers: list[Offer] = []
        #: Exactly one line per lifecycle transition, drained by `note()`.
        self._transition: str | None = None

        # cross-tick derivations
        self._veh_seen: dict[int, tuple[tuple[float, float, float], float]] = {}
        self._veh_still_since: dict[int, float] = {}
        self._anchor: tuple[float, float] | None = None
        self._anchor_at = clock()
        self._was_in_vehicle = False
        self._last_vehicle: tuple[int, tuple[float, float, float]] | None = None
        self._self_exit_at = -1e12
        self._stolen_from: tuple[int, tuple[float, float, float]] | None = None
        self._since_mission_s: float | None = None
        self._last_mission_at: float | None = None

        self.view = RoamView(rng=self._rng)

    # -- things main tells the engine -----------------------------------------

    def note_self_exit(self) -> None:
        """We issued `exit_vehicle`. Without this, every deliberate dismount looks
        exactly like being dragged out of the driver's seat."""
        self._self_exit_at = self._clock()

    def note_mission_started(self) -> None:
        self._last_mission_at = self._clock()
        self._completed_since_mission = 0
        self._roam_started_at = self._clock()

    def note_roam_resumed(self) -> None:
        """A mission block ended; the roam clock for mission-forcing restarts."""
        self._roam_started_at = self._clock()

    # -- per-tick observation ---------------------------------------------------

    def observe(self, state: GameState, *, mood: str = "bored", mood_style: str = "normal") -> None:
        """Fold this snapshot into the cross-tick derivations. Once per tick."""
        now = self._clock()
        self._observe_vehicles(state, now)
        self._observe_carjack(state, now)
        self._observe_movement(state, now)
        self.view = RoamView(
            mood=mood,
            mood_style=mood_style,
            rng=self._rng,
            stationary_handles=frozenset(
                h for h, since in self._veh_still_since.items() if now - since >= VEHICLE_STILL_S
            ),
            since_mission_s=(
                None if self._last_mission_at is None else now - self._last_mission_at
            ),
            stolen_from=self._stolen_from,
        )
        if self.current is None:
            # Refresh the menu on EVERY tick, not only on the tick a pick is
            # attempted. Two things read it and both would otherwise be a tick
            # or more stale: `note()` renders it into the prompt, and
            # `model_choice` validates the model's answer against it — and
            # validating this tick's answer against last tick's menu is how a
            # goal that is no longer on offer gets accepted.
            self.available(state)

    def _observe_vehicles(self, state: GameState, now: float) -> None:
        seen: dict[int, tuple[tuple[float, float, float], float]] = {}
        still: dict[int, float] = {}
        for v in state.nearby.vehicles:
            pos = _vpos(v)
            if pos is None:
                continue
            seen[v.handle] = (pos, now)
            previous = self._veh_seen.get(v.handle)
            if previous is not None and planar_distance(previous[0], pos) < VEHICLE_STILL_M:
                still[v.handle] = self._veh_still_since.get(v.handle, previous[1])
        self._veh_seen = seen
        self._veh_still_since = still

    def _observe_carjack(self, state: GameState, now: float) -> None:
        """`in_vehicle` true -> false with no `exit_vehicle` of ours = he was pulled out."""
        in_vehicle = state.player.in_vehicle
        if in_vehicle and state.vehicle is not None:
            self._last_vehicle = (state.vehicle.handle, player_pos(state))
            self._stolen_from = None
        elif self._was_in_vehicle and not in_vehicle:
            ours = now - self._self_exit_at < 3.0
            previous = self._last_vehicle
            if not ours and previous is not None:
                back = next(
                    (
                        v
                        for v in state.nearby.vehicles
                        if v.handle == previous[0] and v.driver == "npc"
                    ),
                    None,
                )
                if back is not None:
                    self._stolen_from = (previous[0], _vpos(back) or previous[1])
        self._was_in_vehicle = in_vehicle

    def _observe_movement(self, state: GameState, now: float) -> None:
        here = (state.player.pos.x, state.player.pos.y)
        if self._anchor is None:
            # First sample after a reset. The POSITION is new, the CLOCK is not:
            # `reset_movement_anchor` already stamped when the window opened, and
            # re-stamping it here would restart the window on the next snapshot
            # instead of on the reset — which at a 2-4 Hz poll silently gives
            # every stuck window one extra tick, and in a test with a coarse
            # clock gives it the whole interval.
            self._anchor = here
            return
        dx, dy = here[0] - self._anchor[0], here[1] - self._anchor[1]
        if (dx * dx + dy * dy) ** 0.5 >= GOAL_MOVE_M:
            self._anchor, self._anchor_at = here, now

    def still_for_s(self) -> float:
        """How long he has been inside a :data:`GOAL_MOVE_M` circle."""
        return self._clock() - self._anchor_at

    def reset_movement_anchor(self) -> None:
        self._anchor = None
        self._anchor_at = self._clock()

    # -- budgets ----------------------------------------------------------------

    def chaos_available(self) -> float:
        now = self._clock()
        self._chaos_spent = [(t, c) for (t, c) in self._chaos_spent if now - t < 3600.0]
        return CHAOS_BUDGET_PER_HOUR - sum(c for _, c in self._chaos_spent)

    # -- the menu ---------------------------------------------------------------

    def mission_forced(self) -> bool:
        """Has the story waited long enough that a job is the only thing on offer?"""
        if not self._missions_enabled:
            return False
        return (
            self._completed_since_mission >= GOALS_BEFORE_MISSION
            or self._clock() - self._roam_started_at >= ROAM_BEFORE_MISSION_S
        )

    def available(self, state: GameState) -> list[Offer]:
        """The ordered menu. The ONLY goals that may be picked this tick.

        Order of the rules is the specification:
        1. `wanted > 0` overrides everything with `lose_the_cops`.
        2. Three completed goals or fifteen minutes of roam forces the job.
        3. Otherwise: needs + cooldown + chaos + health + category alternation.
        4. A matching trigger sorts its goal to the top with a quotable `why`.
        5. The menu is never empty: the fallback is always legal.
        """
        view = self.view
        now = self._clock()

        # 1. The cops.
        cops = GOALS_BY_ID["lose_the_cops"]
        if cops.needs(state, view):
            self._offers = [Offer(cops, "they are on me", triggered=True)]
            self._offered = (cops.id,)
            return list(self._offers)

        # 2. The story has to move.
        job = GOALS_BY_ID["start_nearest_mission"]
        if self._missions_enabled and self.mission_forced() and job.needs(state, view):
            # The fallback rides along even here, and it is not decoration.
            # `start_nearest_mission` posts NOTHING itself — picking it hands the
            # trip to DayPlanner. If the planner declines (its own health gate)
            # or is between phases, a menu of one goal that posts nothing is a
            # man standing in the street until the 420 s timeout expires. That is
            # the exact failure this whole engine exists to prevent, so the menu
            # is never allowed to be "one goal that cannot move him". The job
            # stays at index 0, so it is still what he picks.
            self._offers = [Offer(job, "let's get paid", triggered=True)]
            fallback = GOALS_BY_ID[FALLBACK_GOAL_ID]
            if fallback.needs(state, view):
                self._offers.append(Offer(fallback, "while I get there", triggered=False))
            self._offered = tuple(o.goal.id for o in self._offers)
            return list(self._offers)

        # 3. The ordinary filter.
        chaos = self.chaos_available()
        hurt = health_fraction(state) < CALM_HEALTH_FRACTION
        offers: list[Offer] = []
        for goal in CATALOG:
            if goal.override_only or goal.fallback:
                continue
            if goal.handoff and not self._missions_enabled:
                continue  # the operator has missions switched off
            if now - self._last_run.get(goal.id, -1e12) < goal.cooldown_s:
                continue
            if goal.chaos_cost > chaos:
                continue
            if hurt and not goal.calm:
                continue
            # Never the same category twice running. The show is not three
            # scenic drives in a row with a personality bolted on.
            if self._last_category is not None and goal.category == self._last_category:
                continue
            if not goal.needs(state, view):
                continue
            offers.append(Offer(goal, goal.why))

        # 4. Triggers: at most one goal is promoted, and it keeps its `why`.
        offers = self._promote_triggered(state, offers)

        # 5. The fallback is ALWAYS legal and always last. Not "when the menu
        # came out empty" — always, so that no combination of cooldowns,
        # category alternation and a quiet street can ever produce a tick on
        # which the honest answer is "nothing to do". It is exempt from every
        # filter above for the same reason.
        fallback = next(g for g in CATALOG if g.fallback)
        offers = [o for o in offers if o.id != fallback.id]
        offers.append(Offer(fallback, fallback.why))

        self._offers = offers
        self._offered = tuple(o.id for o in offers)
        return list(offers)

    def _promote_triggered(self, state: GameState, offers: list[Offer]) -> list[Offer]:
        by_id = {o.id: o for o in offers}
        for trigger in TRIGGERS:
            offer = by_id.get(trigger.goal_id)
            if offer is None:
                continue
            if not trigger.match(state, self.view):
                continue
            promoted = Offer(offer.goal, trigger.why, triggered=True)
            return [promoted] + [o for o in offers if o.id != trigger.goal_id]
        return offers

    def offered_ids(self) -> tuple[str, ...]:
        return self._offered

    def model_choice(self, goal_text: str | None) -> str | None:
        """The goal id the model named, if it named exactly one that is ON the menu.

        CONTRACTS §2's `DecisionModel` has no `goal_id` field — adding one is a
        contract bump and `brain/schemas.py` is not this package's to change — so
        the id is read out of the free-text `goal` field by an EXACT token scan
        against the ids that were actually offered. Strictness is the whole
        point: a text that names no offered id, or names two, returns None and
        the engine picks for itself. Nothing is fuzzy-matched, so this can drop a
        choice but it can never invent one.

        The recommended follow-up is a nullable `goal_id` on `DecisionModel`; it
        goes on the decision, not on `ActionParamsModel`, so it does not touch
        the nullable-parameter ceiling.
        """
        if not goal_text or not self._offered:
            return None
        tokens = set(
            "".join(c if c.isalnum() else " " for c in goal_text.lower()).split()
        )
        # An id is a multi-word token itself ("steal_nice_car"); match on the
        # underscore form AND on its words appearing together, but only exactly.
        named = [
            gid
            for gid in self._offered
            if gid in goal_text.lower() or set(gid.split("_")) <= tokens
        ]
        if len(named) != 1:
            return None
        return named[0]

    # -- picking ----------------------------------------------------------------

    def due(self) -> bool:
        """May a new goal start this tick?

        The jittered beat between goals exists so the show is not a conveyor
        belt — but it is SKIPPED the moment he is actually standing still, which
        is the operator's one hard rule. Being bored is not a reason to wait.
        """
        if self.current is not None:
            return False
        return self._clock() >= self._next_allowed_at or self.still_for_s() >= BOREDOM_S

    def bored(self) -> bool:
        return self.current is None and self.still_for_s() >= BOREDOM_S

    def pick(
        self, state: GameState, *, goal_id: str | None = None, prefer: str | None = None
    ) -> tuple[LockedGoal, dict[str, Any]] | None:
        """Lock one goal off the current menu and return it with its first action.

        `goal_id` is an explicit choice (the model's, already validated by
        :meth:`model_choice`); `prefer` is the day plan's idea, honoured only
        when it is genuinely on the menu. Neither can reach a goal that
        :meth:`available` did not offer — that is the rule that makes "the model
        may only pick from that list" true in code rather than in prose.
        """
        if self.current is not None:
            return None
        offers = self.available(state)
        ordered = self._order(offers, goal_id, prefer)
        for offer in ordered:
            plan, snapshot = offer.goal.plan(state, self.view)
            if not plan and not offer.goal.handoff:
                # A goal that materialised nothing cannot be run. Try the next
                # one rather than locking on an empty plan and timing out.
                continue
            locked = LockedGoal(
                goal=offer.goal,
                why=offer.why,
                plan=plan,
                snapshot=snapshot,
                started_at=self._clock(),
            )
            self._commit(offer.goal)
            self.current = locked
            self.reset_movement_anchor()
            self._transition = (
                f"ROAM GOAL PICKED: {offer.goal.id} — {offer.goal.description} "
                f"(\"{offer.why}\"). Say it once, in your own words, then do it."
            )
            log.info(
                "roam goal picked",
                extra={
                    "kv": {
                        "goal": offer.goal.id,
                        "why": offer.why,
                        "category": offer.goal.category,
                        "steps": len(plan),
                        "offered": ",".join(self._offered),
                    }
                },
            )
            return locked, (plan[0] if plan else {"type": "wait", "params": {"seconds": 1}})
        return None

    def _order(self, offers: list[Offer], goal_id: str | None, prefer: str | None) -> list[Offer]:
        by_id = {o.id: o for o in offers}
        head: list[Offer] = []
        for wanted in (goal_id, prefer):
            if wanted is not None and wanted in by_id:
                offer = by_id.pop(wanted)
                head.append(offer)
        rest = [o for o in offers if o.id in by_id]
        if head:
            return head + rest
        # No explicit choice: a triggered offer already sits at index 0, and
        # otherwise the draw is random so the show does not become a rota.
        if rest and rest[0].triggered:
            return rest
        self._rng.shuffle(rest)
        return rest

    def _commit(self, goal: Goal) -> None:
        now = self._clock()
        self._last_run[goal.id] = now
        self._last_category = goal.category
        if goal.chaos_cost > 0:
            self._chaos_spent.append((now, goal.chaos_cost))

    # -- grading ----------------------------------------------------------------

    def judge(self, state: GameState) -> str | None:
        """Is the locked goal finished? ``"done"``, ``"timeout"``, ``"stuck"`` or None.

        Called before anything else in the free-roam slot, so a goal that the
        world has already completed is never advanced one more pointless step.
        """
        locked = self.current
        if locked is None:
            return None
        if locked.goal.done_when(state, locked.snapshot):
            return "done"
        if state.player.dead or state.player.arrested:
            return "player_down"
        if (
            locked.goal.id != "lose_the_cops"
            and not locked.goal.wants_heat
            and state.player.wanted > 0
        ):
            # The wanted-level override: the goal he has stops being the goal he
            # needs the moment the cops are involved — unless attracting them was
            # the goal. See `Goal.wants_heat`.
            return "wanted_override"
        if locked.elapsed(self._clock()) >= locked.goal.timeout_s:
            return "timeout"
        if self.still_for_s() >= GOAL_STUCK_S:
            locked.strikes += 1
            self.reset_movement_anchor()
            if locked.strikes >= GOAL_STUCK_STRIKES:
                return "stuck"
            return "escalate"
        return None

    def replan(self, state: GameState) -> dict[str, Any] | None:
        """Rebuild the locked goal's plan from where he actually is now.

        Used for the escalate arm of the stuck watchdog and for a plan that ran
        out of steps without `done_when` firing — the plan was built from a
        snapshot that has since gone stale (the car drove off, the bus left).
        Returns the first action of the new plan, or None when the goal has used
        its attempts up and should fail.
        """
        locked = self.current
        if locked is None or locked.attempts >= MAX_GOAL_ATTEMPTS:
            return None
        if not locked.goal.needs(state, self.view):
            return None
        plan, snapshot = locked.goal.plan(state, self.view)
        if not plan:
            return None
        locked.plan = plan
        # The snapshot is NOT replaced wholesale: `steal_nice_car` must still be
        # graded against the rank he had when he decided to upgrade, or a replan
        # after he already got into a better car would move the goalposts.
        for key, value in snapshot.items():
            locked.snapshot.setdefault(key, value)
        locked.attempts += 1
        log.info(
            "roam goal replanned",
            extra={"kv": {"goal": locked.goal.id, "attempt": locked.attempts}},
        )
        return plan[0]

    def close(self, outcome: str) -> dict[str, Any]:
        """End the locked goal and return the extra fields for the §4 payload.

        `roam_goal_picked` / `roam_goal_done` / `roam_goal_failed` are NOT in the
        CONTRACTS §4 closed enum and `events.py` is not this package's to widen,
        so the pair rides on `activity_start`/`activity_end` with the goal id and
        outcome in the free-text payload — the same thing the day planner already
        does with `GO_START_A_JOB`. The brain still gets exactly one line per
        transition, from `note()`.

        `outcome == PREEMPTED_OUTCOME` skips the between-goals gap. Every other
        ending is his own: the goal finished, timed out or gave up, and a few
        seconds of nothing before the next one is deliberate pacing. A
        preemption is not his — a higher owner (a firefight, a mission, a
        budget stop) took the wheel off him mid-goal — so charging him
        :data:`ROAM_GAP_S` of standing in the street for it would turn every
        three-second reflex into a twelve-second stall, which is the exact
        failure this engine exists to prevent.
        """
        locked, self.current = self.current, None
        self._next_allowed_at = (
            self._clock()
            if outcome == PREEMPTED_OUTCOME
            else self._clock() + self._rng.uniform(*ROAM_GAP_S)
        )
        if locked is None:
            return {}
        done = outcome == "completed"
        if done:
            self._completed_since_mission += 1
        if locked.goal.id == "start_nearest_mission" and done:
            self.note_mission_started()
        verb = "DONE" if done else "FAILED"
        self._transition = (
            f"ROAM GOAL {verb}: {locked.goal.id} after "
            f"{locked.elapsed(self._clock()):.0f}s ({outcome}). One line about it, then move on."
        )
        log.info(
            "roam goal ended",
            extra={
                "kv": {
                    "goal": locked.goal.id,
                    "outcome": outcome,
                    "duration_s": round(locked.elapsed(self._clock()), 1),
                    "completed_since_mission": self._completed_since_mission,
                }
            },
        )
        return {
            "goal_id": locked.goal.id,
            "why": locked.why,
            "category": locked.goal.category,
            "verified": done,
        }

    # -- the model's hands ------------------------------------------------------

    def blocks_foreign_action(self, action_type: str, goal_text: str | None) -> bool:
        """True when a decision's bridge task must be DROPPED, not obeyed.

        This is the mechanism, and the prompt is decoration around it. While a
        goal is locked, a movement task that belongs to some OTHER idea is
        exactly how "he picked a goal and then stood still" happens: the model
        narrates a plan, posts `wait` or a drive somewhere else, the goal is
        preempted, and the cycle repeats several times a second.

        Primitives are never blocked — radio, horn, look_around and a short wait
        cost nothing, change no movement, and keep the commentary alive, which is
        the difference between committing to a goal and going mute.
        """
        from ..brain.schemas import BRIDGE_TASKS

        locked = self.current
        if locked is None or action_type not in BRIDGE_TASKS:
            return False
        # The goal id has to be NAMED in the decision's own goal text for the
        # task to survive. Anything else — a new plan, a different destination,
        # an empty goal — is a task for some other idea and is dropped.
        return locked.goal.id not in (goal_text or "").lower()

    # -- reporting --------------------------------------------------------------

    def dashboard_goal(self) -> str | None:
        """CURRENT GOAL for the site, from `roam.current` and never from the model."""
        return None if self.current is None else self.current.goal.description

    def note(self) -> str:
        """The ROAM block for the brain's dynamic context: current, then the menu.

        Drains the one pending transition line, so a pick or a completion is
        announced exactly once rather than every tick until it changes.
        """
        lines: list[str] = []
        transition, self._transition = self._transition, None
        if transition:
            lines.append(transition)
        locked = self.current
        if locked is not None:
            elapsed = locked.elapsed(self._clock())
            # The instruction names the FIELD and the exact value, because the
            # softer wording ("do not switch") produced prose in the goal field on
            # stream every single pick — "cruise around, find a bike, aim for a
            # hill" — which the matcher (rightly) reads as no choice at all.
            lines.append(
                f"ROAM CURRENT: {locked.goal.id} — {locked.goal.description} "
                f'("{locked.why}"). {elapsed:.0f}s of {locked.goal.timeout_s:.0f}s. '
                f'Set your "goal" field to exactly: {locked.goal.id} — nothing else. '
                f"Any other value is ignored."
            )
        elif self._offers:
            ids = " | ".join(o.id for o in self._offers)
            menu = "; ".join(f'{o.id} ("{o.why}")' for o in self._offers)
            lines.append(
                f'ROAM AVAILABLE: {menu}. Set your "goal" field to EXACTLY ONE of: '
                f"{ids} — the bare id, no sentence. First listed is the live opportunity. "
                f"A goal that is not one of these ids is thrown away and picked for you."
            )
        return "\n".join(lines)


# --- getting out of a building -------------------------------------------------
#
# TWO paths live below, and which one runs is decided by the BRIDGE VERSION, not
# by taste:
#
#   * :class:`InteriorEscape` — the v1.12 path. `player.interior` is the game's
#     own answer to "is he indoors", so nothing has to be inferred at all. This
#     is the one that runs against a bridge >= 1.5.0.
#   * :class:`HouseEscape` — the PRE-v1.12 fallback, kept verbatim for a bridge
#     that does not send `player.interior` (the box may still be on 1.4.0 when
#     this ships). It infers being indoors from a failed vehicle entry plus 20 s
#     of stillness — a heuristic that fires late, fires on false positives, and
#     is why the escape was effectively unreachable in the live loop.
#
# Both build the way out from the same documented actions. There is no legal
# teleport (CLAUDE.md rule 5) and `/unstick` is refused on foot, so a walk
# through the door is the only route either of them has.


#: How long `player.interior` must have been the SAME id before the escape
#: starts. It exists only so that walking through a doorway, a lift, or the lip
#: of a car park for a frame or two does not look like being trapped in a
#: living room. Deliberately short — the whole complaint about the old
#: heuristic was that 20 s of stillness fires far too late. Tunable; not yet
#: measured against the real game.
INTERIOR_SETTLE_S = 3.0

#: How long one rung of the interior ladder gets before the next one is tried.
#: The operator's spec: 30 s per step, each step with its own timeout, and the
#: in-goal stuck watchdog stood down for the duration because this ladder does
#: its own timing.
INTERIOR_STEP_TIMEOUT_S = 30.0

#: A sanity bound on the coordinates this ladder is willing to walk to.
#: `player.last_outdoor` is whatever the bridge latched on the last
#: outdoor->indoor transition, and a learned exit is keyed by an InteriorProxy
#: HANDLE — a pool handle, whose reuse across a session is not something this
#: harness can verify. Either could therefore name a door on the other side of
#: the map. A door he actually walked through is metres away, not kilometres,
#: so anything beyond this is discarded as stale rather than walked to.
#: Tunable; not measured against the real game.
INTERIOR_EXIT_MAX_M = 150.0


@dataclass
class InteriorEscape:
    """CONTRACTS v1.12: walk him out of an interior, using the game's own fact.

    WHY THIS EXISTS. After a mission ends, a respawn, or a character switch
    INSIDE a safehouse, outdoor navigation tasks fail — the nav mesh is
    disconnected by doors — and the agent stands in a living room doing nothing.
    `HouseEscape` below has been able to answer that for a while, but `/state`
    had no field that said "he is indoors", so it could only be reached through
    a heuristic that needs 20 s of stillness AND a failed `enter_nearest_vehicle`
    — a combination the live loop essentially never produced. `player.interior`
    is the ground truth, so this runs off a fact.

    THE LADDER (the operator's spec, implemented literally), each rung with its
    own timeout of :data:`INTERIOR_STEP_TIMEOUT_S`:

      1. `walk_to(player.last_outdoor)` — where the bridge saw him standing the
         tick before he came through the door. `walk_to` is
         TASK_FOLLOW_NAV_MESH_TO_COORD and interiors ARE nav-meshed (the ped AI
         opens doors), so a nav-mesh request to an outdoor point is the one
         action that routes back out.
      2. `walk_to` a LEARNED exit for this interior id — the position at which
         the game itself last reported him leaving this same interior. Real
         observed data from this run, never a curated table: no verified
         interior->exit-coord table exists, so none ships (CLAUDE.md rule 6).
         This is the rung that matters for the failure this fix is about,
         because a respawn or a character switch drops him inside WITHOUT an
         outdoor->indoor transition, so rung 1 has nothing to walk to.
      3. Give up, loudly, and hand the wheel back.

    NOT IN THE LADDER, ON PURPOSE: `GET_SAFE_COORD_FOR_PED` + a teleport. It
    would work, and it is a cheat under CLAUDE.md rule 5 — the narrow "unstick"
    exception is a few metres when wedged, not relocating a body out of a
    building. Giving up honestly and saying so on the log is the correct
    failure.

    WHY THE LEARNED TABLE IS NOT PERSISTED TO DISK: the key is an
    `InteriorProxy` handle. CONTRACTS §1 already warns that the game's handles
    are ephemeral, and nothing has verified that an interior-proxy handle means
    the same building in the next process. So the table lives for the life of a
    run, and every use is additionally distance-checked against
    :data:`INTERIOR_EXIT_MAX_M`.
    """

    clock: Any = time.monotonic
    #: 0 = rung 1 has not been posted yet.
    step: int = 0
    #: The interior id being escaped, or None when he is not being escaped from
    #: anywhere. This is the whole "is the escape active" state.
    interior_id: int | None = None
    _step_started_at: float = 0.0
    _step_posted: bool = False
    _task_id: str | None = None
    _gave_up: bool = False
    #: interior id -> the position the game reported him at on the tick he came
    #: OUT of it. Learned from this run only; see the class docstring.
    _exits: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    #: Where he was on the previous tick, so an exit can be learned from the
    #: transition itself.
    _prev_interior_id: int | None = None

    # -- observation -----------------------------------------------------------

    def observe(self, state: GameState) -> int | None:
        """Feed every tick. Returns the interior id he has just LEFT, or None.

        Runs unconditionally, ahead of every gate, because "he came out" has to
        be seen on the tick it happens whatever else the ladder is doing — and
        because the position on that tick is the only honest source for a
        learned exit coordinate.
        """
        interior = state.player.interior
        now_id = None if interior is None else interior.id
        left = None
        if self._prev_interior_id is not None and now_id != self._prev_interior_id:
            # He is out of THAT interior (either outdoors, or into another one).
            left = self._prev_interior_id
            if now_id is None:
                # Outdoors: this position is a real, observed way out of it.
                self._exits[left] = player_pos(state)
        self._prev_interior_id = now_id
        return left

    def active(self) -> bool:
        """True while this class is the reason he is being moved."""
        return self.interior_id is not None and not self._gave_up

    def reset(self) -> None:
        """Forget the current escape. Learned exits are kept — they are facts."""
        self.step = 0
        self.interior_id = None
        self._step_started_at = 0.0
        self._step_posted = False
        self._task_id = None
        self._gave_up = False

    def posted(self, task_id: str | None) -> None:
        """The rung :meth:`check` just returned really did reach the game.

        Called by `main` with `POST /task`'s own task id, which is what starts
        this rung's 30 s clock. Split out from :meth:`check` because
        `_execute_action` has several honest refusals of its own (a modal
        screen, a task type the stall detector has just blocked); a rung that
        never left the harness must not burn a timeout.
        """
        self._step_posted = True
        self._step_started_at = self.clock()
        self._task_id = task_id

    def retry_step(self) -> None:
        """This rung did not stick. Ask for the same one again on the next tick.

        Two callers, one meaning: `_execute_action` refused the post before it
        reached the game, or a higher wheel owner preempted the walk after it
        did. Either way the rung has not had its chance, so its clock is torn
        up rather than counted against it.
        """
        self.step = max(0, self.step - 1)
        self._step_posted = False
        self._step_started_at = 0.0
        self._task_id = None

    # -- the ladder ------------------------------------------------------------

    def check(self, state: GameState) -> dict[str, Any] | None:
        """The next action, or None when the ladder does not apply / is waiting.

        Returning None does NOT mean "stand down" — :meth:`active` is the flag
        for that. A rung that has been posted and is still inside its timeout
        returns None every tick while the walk runs, and the caller keeps
        holding the wheel for it.
        """
        if not self.applies(state):
            self.reset()
            return None
        interior = state.player.interior
        assert interior is not None  # applies() just checked it
        if self.interior_id != interior.id:
            # A new room (or the first detection). Start the ladder from the top:
            # the way out of THIS interior is not the way out of the last one.
            self.reset()
            self.interior_id = interior.id
        if self._gave_up:
            return None

        now = self.clock()
        if self._step_posted:
            task = state.last_task
            ended = (
                self._task_id is not None
                and task.id == self._task_id
                and task.status in ("done", "failed")
            )
            if not ended and now - self._step_started_at < INTERIOR_STEP_TIMEOUT_S:
                return None  # the walk is running; give it its whole 30 s
            # Either the engine finished/abandoned the walk or the rung ran out
            # of time, and he is STILL inside (applies() said so above). Next
            # rung. `walk_to` reaching its coordinate without the interior
            # clearing means the coordinate was not actually a way out.
            self._step_posted = False
            self._task_id = None

        while self.step < 2:
            target = self._target(state, self.step)
            self.step += 1
            if target is None:
                continue
            return _walk_to(target, run=True)

        self._gave_up = True
        log.error(
            "exit_interior GAVE UP: every legal way out of this interior has been "
            "tried and he is still inside. There is no legal teleport (CLAUDE.md "
            "rule 5), so the wheel goes back and the rest of the loop carries on",
            extra={
                "kv": {
                    "interior_id": self.interior_id,
                    "since_s": round(interior.since_s, 1),
                    "had_last_outdoor": state.player.last_outdoor is not None,
                    "had_learned_exit": interior.id in self._exits,
                }
            },
        )
        return None

    def applies(self, state: GameState) -> bool:
        """Is the ground-truth escape the right thing to be doing at all?

        Every refusal here is also a refusal `main._execute_action` would make
        anyway, which is the point: a rung that cannot possibly reach the game
        must not start its 30 s clock, and the ladder must not chew through its
        two rungs while the game owns the controls.
        """
        interior = state.player.interior
        if interior is None:
            return False
        p = state.player
        if p.in_vehicle or p.dead or p.arrested:
            # In a car he can drive out of a garage, and a corpse walks nowhere.
            return False
        if p.switch_in_progress or not p.control_enabled:
            # `main._game_owns_controls`: nothing posted now reaches the player.
            # A character switch INTO a safehouse is one of the three ways he
            # ends up stuck indoors, so this is a WAIT, not a stand-down —
            # `since_s` keeps counting and the ladder starts once it lands.
            return False
        if state.mission.active or state.mission.cutscene_active or state.mission.retry_in_flight:
            # Missions happen indoors on purpose. Walking him out of one would be
            # the harness sabotaging the story, which is the opposite of the bug
            # this fixes; the failure is being left inside AFTER it ends.
            return False
        return interior.since_s >= INTERIOR_SETTLE_S

    def _target(self, state: GameState, step: int) -> tuple[float, float, float] | None:
        here = player_pos(state)
        if step == 0:
            out = state.player.last_outdoor
            if out is None:
                return None
            return self._sane((out.x, out.y, out.z), here, "last_outdoor")
        learned = self._exits.get(self.interior_id or 0)
        if learned is None:
            return None
        return self._sane(learned, here, "learned_exit")

    def _sane(
        self,
        target: tuple[float, float, float],
        here: tuple[float, float, float],
        source: str,
    ) -> tuple[float, float, float] | None:
        distance = planar_distance(here, target)
        if distance > INTERIOR_EXIT_MAX_M:
            log.warning(
                "exit_interior: discarding a way out that is too far to be this "
                "building's door",
                extra={
                    "kv": {
                        "source": source,
                        "interior_id": self.interior_id,
                        "distance_m": round(distance, 1),
                        "max_m": INTERIOR_EXIT_MAX_M,
                    }
                },
            )
            return None
        return target


# --- the PRE-v1.12 fallback, from here to the end of `HouseEscape` -------------
#
# A bridge older than 1.5.0 sends no `player.interior`, so on that bridge being
# stuck inside a house still has to be INFERRED, and the way out still has to be
# built from the same 19 actions as everything else. Kept exactly as it was: it
# is the only cover a pre-v1.12 bridge has. `InteriorEscape` above replaces it
# the moment the field is present.

#: The sharpest indoor tell available: the bridge picked a car it can SEE
#: (`World.GetNearbyVehicles` is an 80 m sphere), fired TASK_ENTER_VEHICLE at it,
#: and the ped could not path to it. A wall is the usual reason.
INDOOR_ENTER_FAILURES: frozenset[str] = frozenset({"timeout", "target_lost"})

#: How long he must be pinned on foot before the ladder starts. Same window the
#: task-stall detector uses, for the same reason: shorter and a door animation
#: or a lift looks like a trap.
INDOOR_STILL_S = 20.0

#: `walk_to` is TASK_FOLLOW_NAV_MESH_TO_COORD, and interiors ARE nav-meshed —
#: the ped AI opens doors, so a nav-mesh request to an outdoor point routes out
#: through the doorway. That is exactly what `enter_nearest_vehicle` cannot do:
#: it fires TASK_ENTER_VEHICLE at the car and lets the (much worse) vehicle-entry
#: pathing try to reach it. But FollowNavMeshTo's path-search radius is limited
#: and one long request out of a multi-room interior can fail to generate a path
#: at all, so the walk goes out in BREADCRUMBS of this length.
BREADCRUMB_M = 15.0

#: Rungs tried before he gives up and says so. There is no legal recovery from a
#: geometry trap on foot; past this it is a human/watchdog escalation, and the
#: run must log that rather than drift silently.
MAX_ESCAPE_RUNGS = 6


@dataclass
class HouseEscape:
    """Get him out of a building and back onto a road, using `walk_to` and doors.

    Lives here rather than inside `recovery.StrandedEscalator` because that
    class's whole answer is to WIDEN the vehicle search (50 -> 90 -> 140 m), and
    indoors that ladder is actively counterproductive: each widening picks a car
    that is further away and behind more walls. This runs BEFORE it and resets
    it, so the widening never happens while an escape is in progress.
    """

    clock: Any = time.monotonic
    rung: int = 0
    _last_at: float = -1e12
    _gap_s: float = 6.0
    _suspected: bool = False
    _walked_from: tuple[float, float, float] | None = None
    _gave_up: bool = False

    def reset(self) -> None:
        self.rung = 0
        self._suspected = False
        self._walked_from = None
        self._gave_up = False

    def looks_indoors(self, state: GameState, still_for_s: float) -> bool:
        """Composite tell — PRE-v1.12 ONLY. Both halves required, neither sufficient.

        **This is the fallback, not the answer.** CONTRACTS v1.12 gives the
        bridge's own `player.interior`, and when that field is on the wire it is
        authoritative: :class:`InteriorEscape` owns the case and this class
        stands down entirely, whatever the heuristic thinks. The heuristic
        survives for exactly one reason — the game box may still be running a
        pre-v1.12 bridge (1.4.0) when this ships, and on that bridge this is the
        only cover there is.

        Deliberately NOT used: "no nearby vehicles at all". The scan is an 80 m
        sphere truncated to the 8 nearest, and cars parked outside a Los Santos
        house are well inside 80 m, so `nearby.vehicles` is usually NOT empty
        indoors — it only discriminates in a rural interior.
        """
        if state.player.interior_reported:
            # A v1.12 bridge answered the question already, in either direction.
            # `interior: null` means OUTDOORS and is a fact; guessing "indoors"
            # over the top of it from a failed vehicle entry is exactly the
            # false-positive behaviour this contract change removed.
            return False
        if state.player.in_vehicle or state.player.dead or state.player.arrested:
            return False
        last = state.last_task
        failed_entry = (
            last.type == "enter_nearest_vehicle"
            and last.status == "failed"
            and last.detail in INDOOR_ENTER_FAILURES
            and bool(state.nearby.vehicles)
        )
        return failed_entry and still_for_s >= INDOOR_STILL_S

    def check(self, state: GameState, still_for_s: float) -> dict[str, Any] | None:
        """The next rung, or None when it does not apply / he is out / he is beaten."""
        if state.player.in_vehicle or state.player.dead or state.player.arrested:
            self.reset()
            return None
        if not self._suspected:
            if not self.looks_indoors(state, still_for_s):
                return None
            self._suspected = True
            log.warning(
                "on foot and apparently indoors: vehicle entry failed with cars "
                "in sight and he has not moved",
                extra={
                    "kv": {
                        "detail": state.last_task.detail,
                        "still_for_s": round(still_for_s, 1),
                        "vehicles": len(state.nearby.vehicles),
                    }
                },
            )
        if self._moved_out(state):
            # He is walking. Let the ordinary layers have him back.
            log.info("house escape: he is moving again", extra={"kv": {"rung": self.rung}})
            self.reset()
            return None
        now = self.clock()
        if now - self._last_at < self._gap_s:
            return None
        if self.rung >= MAX_ESCAPE_RUNGS:
            if not self._gave_up:
                self._gave_up = True
                log.error(
                    "house escape exhausted: he is pinned on foot and no legal "
                    "action moved him. There is no legal teleport (CLAUDE.md "
                    "rule 5) — this needs a human or a watchdog restart",
                    extra={"kv": {"rungs": self.rung}},
                )
            return None
        self._last_at = now
        rung, self.rung = self.rung, self.rung + 1
        return self._rung_action(state, rung)

    def _rung_action(self, state: GameState, rung: int) -> dict[str, Any] | None:
        if rung == 0:
            # Free, and the vision path may catch a door in the frame it grabs.
            return {"type": "look_around", "params": {}}
        if rung == 2:
            # A door that offers a contextual prompt.
            return {"type": "press_prompt_key", "params": {}}
        if rung >= 5:
            # Only once a walk has actually MOVED him is asking for a car worth
            # anything; by here it has not, so this is the last honest attempt.
            return {"type": "enter_nearest_vehicle", "params": {"prefer": "any", "search_radius_m": 15.0}}
        target = self._outdoor_point(state)
        if target is None:
            return {"type": "look_around", "params": {}}
        self._walked_from = player_pos(state)
        return _walk_to(target, run=True)

    def _outdoor_point(self, state: GameState) -> tuple[float, float, float] | None:
        """A guaranteed-outdoor coordinate, free, in every snapshot.

        A parked car is by definition on a road. Using it needs no curated
        safehouse-exit table and therefore adds no unverified-coordinate debt.
        The point returned is a BREADCRUMB toward it, not the car itself.
        """
        candidates = [v for v in state.nearby.vehicles if v.pos is not None]
        if not candidates:
            return None
        target = min(candidates, key=lambda v: v.distance)
        pos = _vpos(target)
        assert pos is not None
        here = player_pos(state)
        distance = planar_distance(here, pos)
        if distance <= BREADCRUMB_M or distance == 0.0:
            return pos
        scale = BREADCRUMB_M / distance
        return (
            here[0] + (pos[0] - here[0]) * scale,
            here[1] + (pos[1] - here[1]) * scale,
            pos[2],
        )

    def _moved_out(self, state: GameState) -> bool:
        if self._walked_from is None:
            return False
        return planar_distance(player_pos(state), self._walked_from) > GOAL_MOVE_M
