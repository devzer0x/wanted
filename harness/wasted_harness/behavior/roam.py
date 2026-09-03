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

import collections
import math
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..bridge_client import GameState, NearbyVehicle
from ..logsetup import get_logger
from .activities import (
    CHAOS_BUDGET_PER_HOUR,
    FREEWAY_ONRAMPS,
    LANDMARKS,
    STUNT_APPROACHES,
    Activity,
)
from .navigation import planar_distance
from .recovery import STALL_MOVE_M, loaded_gun_for

log = get_logger("wasted.roam")


# --- goals the vocabulary cannot express --------------------------------------

#: Goal id -> the capability that is missing. These are NOT in the catalog on
#: purpose: each one's `done_when` could never fire with the fields /state
#: carries and the 19 actions the schema allows, so shipping them would mean
#: locking free roam for `timeout_s` and then reporting a failure that was
#: structurally guaranteed. Every entry names a specific bridge/contract change.
UNBUILDABLE_GOALS: dict[str, str] = {
    # `big_jump` WAS here ("no airtime. /state has no on-ground flag and no
    # vertical velocity"). Bridge 1.7.0 adds `vehicle.in_air` (IS_ENTITY_IN_AIR
    # through the SHVDN `Entity.IsInAir` wrapper), so "the wheels left the
    # ground" is now a field read rather than a z-delta guess, and the goal
    # ships. It is still gated on the bridge actually SENDING the field
    # (:func:`air_reported`) — an older bridge simply does not offer it.
    #
    # `taxi_ride` WAS here ("`enter_nearest_vehicle` always seats him as
    # DRIVER ... Needs a seat parameter at minimum"). Bridge 1.7.0 adds exactly
    # that: `enter_vehicle_seat{handle, seat}` (TASK_ENTER_VEHICLE with a
    # passenger seat index) plus `vehicle.seat`, so riding in the back of a cab
    # to a waypoint is both expressible AND gradeable. The "no hail action" half
    # is worked around the way a human without a phone app does it: walk to a
    # STOPPED cab and get in.
    #
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
        "a robbery is AIM-a-weapon-and-HOLD, and the two halves that arrived in "
        "bridge 1.7.0 are the wrong two. `shoot_at`/`fight_ped{weapon}` fire; "
        "there is still no AIM-without-firing and no THREATEN, and shooting the "
        "clerk empties the till instead of filling it. `player.cash` would be a "
        "perfect done_when and nothing in the vocabulary can produce one. Needs "
        "an aim-at-entity task that does not pull the trigger."
    ),
    "buy_gun": (
        "buying is a menu: browse, select, confirm. `press_prompt_key` is one E "
        "press; there is no up/down/select and no shop state in /state. "
        "`player.weapon.owned` (1.7.0) can now CONFIRM a purchase after the "
        "fact, which is genuinely new — but confirming is not doing, and there "
        "is still no way to work the menu. Walking into the shop is a visual "
        "beat, not a purchase, and must not be dressed up as one."
    ),
    "hold_up_a_driver": (
        "the design brief's 'point a gun at a driver and take the car' needs the "
        "same missing aim-without-firing verb as `rob_store`, plus a way to read "
        "that the ped complied. `jack_a_driver` ships the outcome (he ends up in "
        "the car) without the threat beat; dressing that up as a hold-up would "
        "put a line on air about something the code cannot see happening."
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


#: Things with wheels that have no business on a freeway. Model names are
#: `VehicleHash` members lowercased, with the SAME provenance and the same
#: UNVERIFIED caveat as :data:`BUS_MODELS`: they are read off the bridge's
#: `v.Model` string, and a name that does not match the pinned DLL simply never
#: matches anything.
#:
#: A WRONG NAME HERE IS SAFE, which is why the list is allowed to be generous.
#: `needs` and `done_when` share :func:`is_slow_model`, so a misspelling costs an
#: OFFER (the goal is never proposed) and can never produce a false completion —
#: he cannot be credited with a mower run he did in a Sultan. Both plausible
#: spellings of the lawnmower are listed for exactly that reason: it is the
#: headline vehicle, the pinned enum's member may be `Mower` or `Lawnmower`, and
#: carrying both costs nothing while guessing one risks losing the whole bit.
SLOW_MODELS: frozenset[str] = frozenset(
    {
        "mower", "lawnmower",
        "tractor", "tractor2", "tractor3",
        "forklift", "caddy", "caddy2", "caddy3",
        "faggio", "faggio2", "faggio3",
        "docktug", "airtug", "scrap", "bulldozer", "handler",
    }
)

#: The one vehicle CLASS that is slow all the way through. `utility` and
#: `industrial` are NOT here on purpose: they hold tow trucks and dump trucks,
#: which are freeway-capable and therefore not the joke.
SLOW_CLASSES: frozenset[str] = frozenset({"cycles"})


def is_slow_model(model: str | None, vehicle_class: str | None) -> bool:
    """Is this the slowest thing on the block?"""
    return (
        (vehicle_class or "").strip().lower() in SLOW_CLASSES
        or (model or "").strip().lower() in SLOW_MODELS
    )


def slow_vehicle(state: GameState) -> NearbyVehicle | None:
    """The nearest unattended mower/tractor/bike worth taking on a motorway."""
    for v in empty_vehicles(state, ON_FOOT_RESCUE_RADIUS_M):
        if is_slow_model(v.model, v.vehicle_class):
            return v
    return None


def player_pos(state: GameState) -> tuple[float, float, float]:
    p = state.player.pos
    return (p.x, p.y, p.z)


def _movement_task_running(state: GameState) -> bool:
    """Is a bridge movement task in flight this snapshot?

    Used by `judge()` to defer stillness accounting to the bridge's own
    no_progress watchdog while one is running (see the comment there).
    """
    from ..brain.schemas import MOVEMENT_TASKS

    lt = state.last_task
    return lt is not None and lt.status == "running" and lt.type in MOVEMENT_TASKS


def health_fraction(state: GameState) -> float:
    p = state.player
    if p.max_health <= 0:
        return 1.0
    return p.health / p.max_health


def _vpos(v: NearbyVehicle) -> tuple[float, float, float] | None:
    return None if v.pos is None else (v.pos.x, v.pos.y, v.pos.z)


# --- bridge 1.7.0 fields, read defensively ------------------------------------
#
# `player.weapon`, `vehicle.in_air` and `vehicle.seat` arrive with bridge 1.7.0.
# Every reader below asks whether the field was actually SENT, not just whether
# it is truthy — the same rule `PlayerState.interior_reported` already sets for
# v1.12. A pre-1.7.0 bridge omits the key, the model default reads "no", and a
# goal that depends on it is simply NOT OFFERED. That is the honest degradation:
# the alternative is a goal whose `done_when` can never fire, locking free roam
# for `timeout_s` and reporting a failure that was structurally guaranteed —
# exactly what :data:`UNBUILDABLE_GOALS` exists to prevent.


def supports_v17(state: GameState) -> bool:
    """Is this snapshot from a bridge that speaks 1.7.0?

    `player.weapon` is the marker: a 1.7.0 bridge always sends the object (it is
    never null while a ped exists), an older one omits the key entirely, and
    pydantic records which keys were on the wire. Deliberately NOT a version
    string comparison — `bridge.version` is free text and the field's presence
    is the fact that actually matters. This one works ON FOOT, which
    :func:`air_reported` and :func:`seat_reported` cannot: both live on
    `vehicle`, which is null when he is walking.
    """
    return "weapon" in state.player.model_fields_set


def weapon_reported(state: GameState) -> bool:
    return supports_v17(state) and state.player.weapon is not None


def weapon_class(state: GameState) -> str:
    """``unarmed|melee|gun|projectile|unknown`` for what he is HOLDING, or ``""``."""
    w = getattr(state.player, "weapon", None)
    return "" if w is None else (w.weapon_class or "")


def armed_with_a_gun(state: GameState) -> bool:
    return weapon_class(state) == "gun"


def owns_weapon(state: GameState, name: str) -> bool:
    """Does he own this `WeaponHash` member name (`Pistol`, `MicroSMG`, ...)?

    `player.weapon.owned` is the bridge's HAS_PED_GOT_WEAPON read over the three
    tracked loadout weapons only, so a `False` here means "not one of the three
    we track", never "he is definitely unarmed" — which is why `needs` gates on
    this and `done_when` never does.
    """
    w = getattr(state.player, "weapon", None)
    return bool(w is not None and name in (w.owned or {}))


def weapon_ammo(state: GameState, name: str) -> int | None:
    """Rounds carried for one owned weapon, or None when it is not owned/reported."""
    w = getattr(state.player, "weapon", None)
    if w is None:
        return None
    return (w.owned or {}).get(name)


def smg_loaded(state: GameState) -> bool:
    """The micro SMG is owned AND has rounds — the `drive_by` precondition.

    `owns_weapon` is deliberately not enough: `owned` maps a name to its ammo
    count, a Busted strips the ammo and leaves the gun, and the bridge's
    `SelectForDriveBy` is `HAS_PED_GOT_WEAPON`-guarded, not ammo-guarded, so an
    owned-but-empty SMG would be put in his hands and pointed out of the window.
    """
    return (weapon_ammo(state, "MicroSMG") or 0) > 0


def air_reported(state: GameState) -> bool:
    v = state.vehicle
    return v is not None and "in_air" in v.model_fields_set


def in_air(state: GameState) -> bool:
    v = state.vehicle
    return bool(v is not None and getattr(v, "in_air", False))


def seat_reported(state: GameState) -> bool:
    v = state.vehicle
    return v is not None and "seat" in v.model_fields_set and v.seat is not None


def riding_as_passenger(state: GameState) -> bool:
    """In a vehicle somebody ELSE is driving. Needs `vehicle.seat` (1.7.0)."""
    v = state.vehicle
    return bool(state.player.in_vehicle and v is not None and getattr(v, "seat", None) == "passenger")


# --- the chaos ladder (T7) ------------------------------------------------------
#
# Three tiers of nuisance, and a rule that walks him DOWN one when the show
# turns into a death loop. The operator's bar for free roam is "keep doing
# nuisance"; the counter-bar, in his own words, is that "bad judgement is funny,
# dying every four minutes is not". A fixed catalog cannot serve both, so the
# catalog is tiered and the tier is chosen by measured outcomes rather than by
# mood: nothing here reads the model, and nothing here is a random draw.
#
#   L1  nuisance with no bodies. Cars, hills, buses, distance.
#   L2  trouble that answers back. Fists, gangs, cop cars, two stars, a drive-by.
#   L3  the ones that can genuinely end him. Three stars held, a block shot up,
#       a police helicopter.
#
# A goal is offered only when `goal.level <= RoamEngine.level`. Nothing else in
# the selection rules changes, so every existing filter (cooldown, category
# alternation, health, novelty, chaos budget) still applies on top.

#: The tier free roam starts a session on. L2 rather than L3 on purpose: he
#: begins with no gun he did not earn (bridge 1.7.0 ships the Ammu-Nation
#: loadout OFF by default), so the L3 goals would be gated on a weapon he does
#: not have and the first half hour would be a menu of things he cannot do. He
#: climbs to L3 after a clean 30 minutes, which is also the point by which he
#: has usually picked something up.
CHAOS_START_LEVEL = 2
CHAOS_MIN_LEVEL = 1
CHAOS_MAX_LEVEL = 3

#: Deaths inside free roam, per rolling hour, that force a step DOWN. Six is
#: one death every ten minutes: past that the stream is a respawn montage, and
#: the fix that a human would apply is "stop picking fights for a bit", which is
#: exactly one tier.
DEATHS_PER_HOUR_STEP_DOWN = 6

#: How long he has to go without dying before the tier he lost comes back. Half
#: an hour is long enough that it is a genuine recovery rather than a bounce off
#: the same fight, and short enough to happen inside one stream.
CLEAN_RECOVERY_S = 30 * 60.0

#: The rolling window deaths are counted in.
DEATH_WINDOW_S = 3600.0

#: Chaos budget per hour, per tier. :data:`activities.CHAOS_BUDGET_PER_HOUR` is
#: the L1 figure and stays the ActivityPicker's own budget; the higher tiers buy
#: the headroom their goals cost (a `gang_trouble` alone is 2.0). Without this a
#: single L2 goal would spend the whole hour's budget and the tier would be
#: decoration.
CHAOS_BUDGET_BY_LEVEL: dict[int, float] = {
    1: CHAOS_BUDGET_PER_HOUR,
    2: CHAOS_BUDGET_PER_HOUR * 2.5,
    3: CHAOS_BUDGET_PER_HOUR * 4.0,
}


class ChaosLadder:
    """The tier, and the two measurements that move it.

    Deliberately a plain object with an injected clock and no knowledge of
    `/state`: :meth:`RoamEngine.observe` feeds it the death EDGE it detects, so
    "how do we know he died" lives in one place and this class only does the
    arithmetic.
    """

    def __init__(self, clock: Any, level: int = CHAOS_START_LEVEL) -> None:
        self._clock = clock
        self.level = max(CHAOS_MIN_LEVEL, min(CHAOS_MAX_LEVEL, level))
        self._deaths: list[float] = []
        #: When the current clean run started: a death, or a step, restarts it.
        self._clean_since = clock()
        #: Set on every change so the engine can announce it exactly once.
        self.transition: str | None = None

    def note_death(self, now: float) -> None:
        self._deaths.append(now)
        self._clean_since = now
        self._prune(now)
        if len(self._deaths) >= DEATHS_PER_HOUR_STEP_DOWN and self.level > CHAOS_MIN_LEVEL:
            self.level -= 1
            # The death counter is CLEARED on a step-down: the six deaths bought
            # the step, and leaving them in the window would spend them again on
            # the next death and walk him from L3 to L1 in two deaths.
            self._deaths.clear()
            self.transition = (
                f"CHAOS LEVEL DOWN to L{self.level}: "
                f"{DEATHS_PER_HOUR_STEP_DOWN} deaths in an hour. Ease off."
            )
            log.info(
                "chaos level down",
                extra={"kv": {"level": self.level, "reason": "deaths_per_hour"}},
            )

    def tick(self) -> None:
        """Called once per observation: expire old deaths, promote on a clean run."""
        now = self._clock()
        self._prune(now)
        if self.level >= CHAOS_MAX_LEVEL:
            return
        if now - self._clean_since >= CLEAN_RECOVERY_S:
            self.level += 1
            self._clean_since = now
            self.transition = (
                f"CHAOS LEVEL UP to L{self.level}: "
                f"{CLEAN_RECOVERY_S / 60:.0f} clean minutes. Push it."
            )
            log.info(
                "chaos level up",
                extra={"kv": {"level": self.level, "reason": "clean_run"}},
            )

    def deaths_in_window(self) -> int:
        self._prune(self._clock())
        return len(self._deaths)

    def _prune(self, now: float) -> None:
        self._deaths = [t for t in self._deaths if now - t < DEATH_WINDOW_S]


# --- mission cadence, as configuration -----------------------------------------

#: The DEFAULT mission cadence, kept as module constants because the tests, the
#: day planner and the "let's get paid" trigger all name them. They are now the
#: defaults of :class:`RoamCadence` rather than the rule itself: the live values
#: are on `RoamEngine.cadence` and are environment-overridable.
#:
#: Retuned in T7 from (3 goals OR 15 minutes, both FORCING) because that
#: collapsed the whole menu to one goal after three cars on a stream that is
#: supposed to be free roam. Six completed goals now OFFER the job at the top of
#: the menu; only the forty-minute clock forces it.
GOALS_BEFORE_MISSION = 6
ROAM_BEFORE_MISSION_S = 40 * 60.0



def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "ignoring a non-numeric cadence override",
            extra={"kv": {"var": name, "value": raw[:40], "using": default}},
        )
        return default


@dataclass(frozen=True)
class RoamCadence:
    """When free roam should hand the wheel to the story, as configuration.

    The old numbers were three completed goals OR fifteen minutes, and BOTH
    forced: the menu collapsed to one goal. Measured on 2026-09-03 that meant
    `start_nearest_mission` was 2 of 22 picks in two hours of a stream that is
    supposed to be free roam, and the dashboard said "walk to Franklin's marker
    and start the job" while the operator had missions switched OFF.

    The shape is now two DIFFERENT levers:

    * ``goals_before_offer`` — after this many COMPLETED roam goals the job is
      put at the TOP of the menu with a why, and everything else stays on it.
      An offer he can refuse; the model or the draw may still pick a car.
    * ``force_after_s`` — only this, the wall clock, collapses the menu. Forty
      minutes is long enough that a viewer who tuned in for chaos got a stream
      of it first.

    ``missions_enabled`` is not here on purpose: it is not cadence, it is an
    absolute off switch (``Settings.missions_enabled`` / ``WASTED_MISSIONS_ENABLED``)
    and it is checked BEFORE either lever, so no cadence value can turn it back on.
    """

    goals_before_offer: int = GOALS_BEFORE_MISSION
    force_after_s: float = ROAM_BEFORE_MISSION_S

    @classmethod
    def from_env(cls) -> RoamCadence:
        return cls(
            goals_before_offer=max(
                1, int(_env_float("WASTED_ROAM_GOALS_BEFORE_MISSION", GOALS_BEFORE_MISSION))
            ),
            force_after_s=max(
                60.0, _env_float("WASTED_ROAM_MISSION_FORCE_S", ROAM_BEFORE_MISSION_S)
            ),
        )


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
PROXIMITY_SEARCH_RADIUS_M = 30.0
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

#: How many recent goals count as "just did that". Four is long enough that a
#: fourteen-goal menu still has room to breathe and short enough that a good
#: opportunistic trigger (a supercar, a cop car) comes back around quickly.
NOVELTY_WINDOW = 4

#: The one goal that is always offerable, exempt from cooldown, category
#: alternation and the health gate. Named here so the forced-mission menu and
#: the ordinary filter cannot disagree about which goal is the floor.
FALLBACK_GOAL_ID = "roam_the_block"

#: How far `roam_the_block` will look for a car before it gives up and walks.
#: 50 m is the same reach the goal used before; the change is that not finding
#: one is now a branch rather than a dead end.
BLOCK_VEHICLE_RADIUS_M = 50.0

#: How far `enter_nearest_vehicle` may search when he is on foot with nothing in
#: the snapshot's own nearby list. Proven in-game 2026-09-03: a 60 m search walks
#: him to a car and completes; the old pinpoint radius failed on the spot and left
#: him standing in the street for the whole goal.
ON_FOOT_RESCUE_RADIUS_M = 60.0

#: `shoot_and_run`: how far he aims to get away, and how far counts as away.
SHOOT_RUN_M = 140.0
SHOOT_RUN_DONE_M = 90.0

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

#: How near one of the curated (UNVERIFIED) freeway approach points counts as
#: "there's the on-ramp". Generous, because the points are approximate by
#: construction and the trigger only reorders a menu.
ONRAMP_NEAR_M = 200.0


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


def _fly_to(pos: tuple[float, float, float], altitude_asl_m: float) -> dict[str, Any]:
    """`fly_to` (CONTRACTS §1 proposal, bridge 1.8.0): fly the aircraft he is in.

    Same five keys as `drive_to` minus `style`, so the decision schema gained no
    new param. `z` is NOT the ground at `pos`: it is the cruise altitude above
    sea level the bridge hands the engine as `flightHeight` ("the Z coordinate
    the heli tries to maintain (i.e. 30 == 30 meters above sea level)" — pinned
    SHVDN XML for StartHeliMission/StartPlaneMission), which is why the caller
    computes it from BOTH ends of the trip and not from the destination alone.
    """
    return {
        "type": "fly_to",
        "params": {
            "x": pos[0],
            "y": pos[1],
            "z": altitude_asl_m,
            "speed_mps": FLIGHT_SPEED_MPS,
            "arrive_radius_m": FLIGHT_ARRIVE_M,
        },
    }


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
    #: Chaos tier (T7). Offered only while `RoamEngine.level >= level`. L1 is
    #: nuisance with no bodies, L2 is trouble that answers back, L3 is the ones
    #: that can genuinely end him. See the ladder block at the top of the module.
    level: int = 1


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


#: Peds the ORDINARY fight goals (`pick_a_fight`, `armed_rampage_block`,
#: `shoot_and_run`, `gang_trouble`) never start on. Story characters are the
#: show — killing Lamar ends the story arc the whole channel is built around —
#: and the law is kept out of the ambient menu because a cop turns any bit into
#: a wanted level the engine then has to spend a goal escaping. Starting on
#: police is not forbidden any more; it is its OWN goal, `shoot_a_cop`,
#: gated on chaos tier 3, a
#: loaded gun and full health, so it is a deliberate bit and never the default
#: answer to "there is a man nearby". Matched as substrings because the bridge
#: emits lowercased model names (`SnapshotBuilder.PedModelName`) and the family
#: is what matters, not the variant.
PROTECTED_PED_MODELS: tuple[str, ...] = (
    # The three protagonists ship as `player_zero` (Michael), `player_one`
    # (Franklin) and `player_two` (Trevor) — their in-fiction names appear
    # nowhere in the model, so matching on "michael" protects nobody.
    "player_zero", "player_one", "player_two",
    "lamar", "franklin", "michael", "trevor", "simeon", "jimmy", "tracey",
    "amanda", "lester", "devin", "stretch", "wade", "ron", "chop",
    "cop", "police", "sheriff", "swat", "army", "security", "fbi", "prisguard",
)

#: Ped model families that are the LAW, for `shoot_a_cop`. Substrings of the
#: lowercased model name, same convention as :data:`PROTECTED_PED_MODELS`:
#: `s_m_y_cop_01`, `s_f_y_cop_01`, `s_m_y_hwaycop_01`, `s_m_y_sheriff_01`,
#: `s_m_y_swat_01`, `s_m_y_ranger_01`, `s_m_m_fibsec_01`, `s_m_m_fiboffice_01`.
#: Curated from the model-name convention, NOT verified against the running
#: game — a wrong name costs an offer, never a false completion, because the
#: same predicate gates `needs` and `done_when`. Deliberately NOT `security`,
#: `army` or `prisguard`: a mall guard is not a cop, and Fort Zancudo / Bolingbroke
#: are a different, worse idea.
COP_PED_MODEL_TOKENS: tuple[str, ...] = ("cop", "sheriff", "swat", "ranger", "fibsec", "fiboffice")

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


def is_cop_ped_model(model: str | None) -> bool:
    name = (model or "").strip().lower()
    return any(tok in name for tok in COP_PED_MODEL_TOKENS)


def nearest_cop(state: GameState) -> Any | None:
    """The nearest officer within :data:`FIGHT_RADIUS_M` he could START on.

    Already-`hostile` cops are excluded for the same reason `_fightable`
    excludes hostiles: a cop who is already shooting at him is the threat
    reflex's job, and it does it without a model call. `friendly` is excluded
    because a cop in the engine's Companion/Like/Respect group is a mission
    crewmate wearing a uniform (the police-station missions), and shooting the
    crew fails the job.
    """
    peds = [
        p for p in state.nearby.peds
        if p.distance <= FIGHT_RADIUS_M
        and is_cop_ped_model(getattr(p, "model", None))
        and getattr(p, "relationship", "neutral") == "neutral"
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
    # `weapon: "unarmed"` (bridge 1.7.0): SET_CURRENT_PED_WEAPON to
    # WeaponHash.Unarmed before the combat task, so this is a fist fight
    # whatever he happens to be carrying. Without it, the same goal on a the agent
    # who picked up a pistol is an execution, and the show is not that.
    steps.append({"type": "fight_ped", "params": {"handle": mark.handle, "weapon": "unarmed"}})
    # `task_before`: the task id on the wire at pick time, so `done_when` can
    # tell a `fight_ped` that finished DURING this goal from one that finished
    # before it (the reflex fights too, and its `done` may still be the last
    # task when this goal is picked). Survives `replan` (setdefault merge).
    return steps, {"mark": mark.handle, "task_before": state.last_task.id}


def _done_pick_a_fight(state: GameState, snap: dict[str, Any]) -> bool:
    """Over when the mark is no longer a problem: dead, fled, or streamed out.

    Two signals, because neither alone covers the win. `nearby.peds` is top-8
    by distance, so ABSENCE is the honest proxy for "fled or streamed out" —
    there is no ped-health field. But the bridge's ped scan does not drop dead
    peds, so a mark he actually beat stays in the list as a corpse at his feet
    and absence never fires; the plan then runs out, replans onto the same
    corpse, and the fight he WON is graded as a failure. The bridge's own
    `fight_ped` reports `done` only when its target is dead or gone (CONTRACTS
    §1), so a `fight_ped` that reached `done` after this goal was picked is
    that fact on the wire — the id check keeps a pre-pick `done` (the reflex
    finishing somebody else) from ending the goal before it starts.
    """
    handle = snap.get("mark")
    if all(p.handle != handle for p in state.nearby.peds):
        return True
    last = state.last_task
    return (
        last.type == "fight_ped"
        and last.status == "done"
        and last.id is not None
        and last.id != snap.get("task_before")
    )


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


#: A car worth chasing: fast class, moving (an NPC at the wheel), within reach.
CHASE_CLASSES: tuple[str, ...] = ("Super", "Sports", "SportsClassics", "Muscle", "Motorcycles")
CHASE_RADIUS_M = 60.0
#: How long a tail counts as "done" — long enough to be a bit, short enough that
#: he does not follow one hatchback to Paleto Bay.
CHASE_DONE_S = 45.0
#: An occupied car he can jack must be close: `enter_nearest_vehicle` takes no
#: handle, so proximity is the only way to say WHICH one.
JACK_RADIUS_M = 12.0
#: A honk run: how far he has to have driven, leaning on the horn, before the bit lands.
HONK_RUN_M = 300.0


def _driven_nice_car(state: GameState) -> NearbyVehicle | None:
    cars = [
        v for v in state.nearby.vehicles
        if v.driver == "npc" and v.vehicle_class in CHASE_CLASSES and v.distance <= CHASE_RADIUS_M
    ]
    return min(cars, key=lambda v: v.distance) if cars else None


def _needs_chase_that_car(state: GameState, view: RoamView) -> bool:
    return state.player.in_vehicle and state.player.wanted == 0 and _driven_nice_car(state) is not None


def _plan_chase_that_car(state, view):
    v = _driven_nice_car(state)
    assert v is not None
    # `follow_entity` on a moving car is the one action that keeps pace with a
    # target rather than driving to where it used to be (CONTRACTS v1.9).
    return (
        [{"type": "follow_entity", "params": {"handle": v.handle, "in_vehicle": True}}],
        {"target": v.handle, "started": view.now if hasattr(view, "now") else None},
    )


def _done_chase_that_car(state: GameState, snap: dict[str, Any]) -> bool:
    """Done when the target has gone (lost or streamed out) — the tail ended on
    its own terms — or, via the goal's timeout, when he has had his fun."""
    return all(v.handle != snap.get("target") for v in state.nearby.vehicles)


def _jackable(state: GameState) -> NearbyVehicle | None:
    cars = [v for v in state.nearby.vehicles if v.driver == "npc" and v.distance <= JACK_RADIUS_M]
    return min(cars, key=lambda v: v.distance) if cars else None


def _needs_jack_a_driver(state: GameState, view: RoamView) -> bool:
    return (
        not state.player.in_vehicle
        and state.player.wanted == 0
        and state.player.health >= FIGHT_MIN_HEALTH
        and _jackable(state) is not None
    )


def _plan_jack_a_driver(state, view):
    v = _jackable(state)
    assert v is not None
    # The bridge always seats him as DRIVER; on an occupied car that is a jack —
    # the owner gets pulled out, and usually objects. The vehicle loop then
    # drives away on its own (vehicle.py), which is the whole bit.
    return _approach_then_enter(v, "any", PROXIMITY_SEARCH_RADIUS_M), {"target": v.handle}


def _done_jack_a_driver(state: GameState, snap: dict[str, Any]) -> bool:
    return state.player.in_vehicle and state.vehicle is not None and state.vehicle.handle == snap.get("target")


def _needs_honk_run(state: GameState, view: RoamView) -> bool:
    return state.player.in_vehicle and state.player.wanted == 0


def _plan_honk_run(state, view):
    # A primitive is a legal plan step (activities runner: "a step that posts no
    # bridge task"). Drive, lean on the horn, drive some more. Pointless, and
    # exactly the kind of pointless a stream is for.
    return (
        [
            _wander("ignore_lights"),
            {"type": "horn", "params": {"ms": 1500}},
            {"type": "horn", "params": {"ms": 400}},
            {"type": "horn", "params": {"ms": 2000}},
        ],
        {"start": player_pos(state)},
    )


def _done_honk_run(state: GameState, snap: dict[str, Any]) -> bool:
    return planar_distance(player_pos(state), snap["start"]) >= HONK_RUN_M


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


def _needs_slow_freeway(state: GameState, view: RoamView) -> bool:
    """A mower within reach, and he is not already on one.

    The last clause matters: without it the goal is offered while he is already
    riding the mower down the motorway, and re-picking it mid-run resets the
    displacement snapshot so the 1.5 km can never be completed.
    """
    if state.player.wanted > 0 or state.mission.active:
        return False
    if state.player.in_vehicle:
        v = state.vehicle
        if v is not None and is_slow_model(v.model, v.vehicle_class):
            return False
    return slow_vehicle(state) is not None


def _plan_slow_freeway(state, view):
    v = slow_vehicle(state)
    assert v is not None
    here = player_pos(state)
    # Same haul `freeway_run` takes — the farthest landmark is the longest
    # straight-ish run on the map — because the joke is the VEHICLE, not the
    # route. Asking for `rushed` at 34 m/s on a machine that does 12 is the
    # point: he is giving it everything he has.
    name = max(LANDMARKS, key=lambda n: planar_distance(here, LANDMARKS[n]))
    pos = LANDMARKS[name]
    steps = _approach_then_enter(v, "any", PROXIMITY_SEARCH_RADIUS_M)
    steps += [_waypoint(pos), _drive_to(pos, 34.0, "rushed", 25.0), _wander("rushed")]
    return steps, {"start": here, "target": name, "model": v.model}


def _done_slow_freeway(state: GameState, snap: dict[str, Any]) -> bool:
    """The full freeway distance, still aboard the slow thing.

    Both halves are required. Covering 1.5 km after abandoning the mower for a
    Sultan is `freeway_run`, and crediting it here would be scoring the bit he
    did not do.
    """
    v = state.vehicle
    if not state.player.in_vehicle or v is None:
        return False
    if not is_slow_model(v.model, v.vehicle_class):
        return False
    return float(snap.get("_from_start_m", 0.0)) >= FREEWAY_RUN_DISTANCE_M


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
    """Provoke, then drive: the police are not interested in bad driving alone.

    The first version was get-in-a-car-and-run-red-lights and, measured, sat
    at zero stars for its whole 180 s timeout — the longest silent stretch in
    the soak. Now the plan opens with the thing that actually draws them when
    he has a gun (`drive_by` from the seat, `shoot_at` on foot — bridge 1.7.0,
    the same verbs `drive_by_run`/`armed_rampage_block` use), and only THEN
    drives like a maniac, which is what turns one star into two. Unarmed, it is
    the old plan: jack an occupied car, which is a star by itself.
    """
    steps: list[dict[str, Any]] = []
    # ROUNDS, not ownership: `owned` maps a name to its ammo count and a
    # Busted leaves every loadout gun owned with 0 — the 2026-09-03 state that
    # had every armed goal opening with an empty weapon.
    if state.player.in_vehicle:
        mark = _drive_by_target(state)
        if smg_loaded(state) and mark is not None:
            steps.append({"type": "drive_by", "params": {"handle": mark.handle, "duration_s": 8.0}})
        steps.append(_wander("ignore_lights"))
        return steps, {}
    mark = nearest_mark(state)
    if mark is not None and loaded_gun_for(state, mark.distance):
        steps.append({"type": "shoot_at", "params": {"handle": mark.handle, "duration_s": 5.0}})
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


#: How much altitude he has to gain before the position counts as open. Chosen
#: against the map's own relief rather than a summit height: the observatory and
#: the Vinewood sign sit ~300 m above the city floor and Chiliad ~760 m, so 250 m
#: is a real climb up any of the three and is reachable from the streets below
#: each of them.
UP_ONLY_CLIMB_M = 250.0

#: How much of the climb he has to give back before the position counts as
#: closed. Deliberately most of it: a car park that slopes ten metres is not a
#: drawdown, and the joke only works if he visibly comes all the way down.
UP_ONLY_DROP_M = 200.0


def _needs_up_only(state: GameState, view: RoamView) -> bool:
    """Offered when there is a real climb available from where he is standing.

    The gate is the RELIEF between him and the nearest hill, not his absolute
    altitude, so it cannot be fooled by a coordinate table that has never been
    checked against the running game (`activities.LANDMARKS` is marked
    UNVERIFIED, and the knowledge base disagrees with it about Chiliad by
    thirty metres). If the hill's authored height is wrong, this offer is
    wrong in the same direction as the goal's own completion test, which is
    the consistent failure rather than the confusing one.
    """
    if state.player.wanted > 0 or state.mission.active:
        return False
    if state.player.in_vehicle:
        if state.vehicle is None or (
            (state.vehicle.vehicle_class or "").strip().lower() in AIRCRAFT_CLASSES
        ):
            # Flying up a mountain is not a climb, it is a cutscene.
            return False
    elif not empty_vehicles(state, BLOCK_VEHICLE_RADIUS_M):
        return False
    _, pos = _nearest_hill(state)
    return (pos[2] - player_pos(state)[2]) >= UP_ONLY_CLIMB_M


def _plan_up_only(state, view):
    name, pos = _nearest_hill(state)
    steps: list[dict[str, Any]] = []
    if not state.player.in_vehicle:
        steps.append(_enter("any", BLOCK_VEHICLE_RADIUS_M))
    steps += [_waypoint(pos), _drive_to(pos, 22.0, "normal", HILL_ARRIVE_M)]
    # THE DUMP, as an explicit step. Leaving the descent to `wander_drive`
    # would let him mill about the summit car park until the timeout, and the
    # whole bit is that the way down is not optional.
    down = min(LANDMARKS, key=lambda n: LANDMARKS[n][2])
    steps += [_waypoint(LANDMARKS[down]), _drive_to(LANDMARKS[down], 30.0, "rushed", 25.0)]
    return steps, {
        "hill": name,
        "target": pos,
        "start": player_pos(state),
        "start_z": state.player.pos.z,
        "floor": down,
    }


def _done_up_only(state: GameState, snap: dict[str, Any]) -> bool:
    """Up, and then all the way back down.

    Both halves are read off his OWN trajectory (`_climb_m` / `_drop_from_peak_m`
    in `_fold_goal_progress`), never off an authored summit height. The goal
    called "up only" is therefore the one goal in the catalog that cannot be
    completed without the drawdown, which is the entire joke.
    """
    return (
        float(snap.get("_climb_m", 0.0)) >= UP_ONLY_CLIMB_M
        and float(snap.get("_drop_from_peak_m", 0.0)) >= UP_ONLY_DROP_M
    )


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


# --- T7: the new tiers ---------------------------------------------------------
#
# One trio per goal, same shape as everything above. What is new is that four of
# them read a `/state` field that did not exist before bridge 1.7.0, and every
# one of those four gates its `needs` on the field having actually been SENT —
# so on an older bridge they are simply absent from the menu rather than
# offerable-and-uncompletable.


#: `VehicleHash` member names for cabs, lowercased — same provenance and same
#: caveat as :data:`BUS_MODELS`: curated, not verified against the running game.
TAXI_MODELS: frozenset[str] = frozenset({"taxi"})

#: How far a jump run-up has to have covered before airtime counts as a jump
#: rather than the kerb outside where he started.
BIG_JUMP_RUNUP_M = 100.0
#: ...or this long into the goal. The approach is the NEAREST one, so a goal
#: picked 60 m from the ramp jumps with `_from_start_m` under the run-up bar
#: and the completion was refused (soak trace: airborne at 107 s, timed out at
#: 216 s). Eight seconds in, the wheels leaving the ground is the jump.
BIG_JUMP_MIN_S = 8.0

#: Vehicle classes that are in the air as their NORMAL state. Offering
#: `big_jump` in one would complete it on the first tick.
FLYING_CLASSES: frozenset[str] = frozenset({"helicopters", "planes"})

#: A cab has to be stopped to be walked to, and the ride has to actually go
#: somewhere before "he took a taxi" is a true statement on air.
TAXI_RIDE_M = 250.0

#: How near an empty helicopter has to be to be worth walking to. Wider than
#: :data:`TRIGGER_RADIUS_M` because a parked helicopter is a rare enough sight
#: that a slightly longer walk is worth it, and it does not drive away.
HELI_RADIUS_M = 60.0

#: How far from the shooting he has to get before "and then he left" is true.
DRIVE_BY_ESCAPE_M = 150.0
#: Nothing further away than this is a drive-by target; it is a car window, not
#: a rifle. Matches :data:`TRIGGER_RADIUS_M` by construction.
DRIVE_BY_RADIUS_M = TRIGGER_RADIUS_M

#: Seconds he has to hold three stars for the L3 goal to have happened. The
#: escape is NOT part of this goal: the moment it completes, `available()`'s
#: wanted rule hands the next tick to `lose_the_cops`, which is a better escape
#: than anything this goal could post and is already written.
HOLD_THREE_S = 90.0

#: Seconds of standing his ground for `armed_rampage_block`. Same hand-off: the
#: cops are somebody else's goal once this one is done.
RAMPAGE_S = 60.0

#: `shoot_a_cop`: the longest the goal holds him on ONE officer before it calls
#: itself done and hands the consequences to `lose_the_cops`. Shorter than
#: :data:`RAMPAGE_S` on purpose: the point of the bit is the moment he does it
#: and the chase that follows, not a siege, and every second past the first
#: shot is a second the dispatch is closing in on a man standing still.
COP_FIGHT_S = 45.0

#: Seat index asked for when riding as a passenger. Rear-right, which is where
#: the game's own cab-hailing puts the player; the schema only allows 0/1/2, so
#: no value of this can ever ask for the driver's seat.
PASSENGER_SEAT = 2


def _stationary_taxi(state: GameState, view: RoamView) -> NearbyVehicle | None:
    best: NearbyVehicle | None = None
    for v in state.nearby.vehicles:
        if (v.model or "").strip().lower() not in TAXI_MODELS:
            continue
        if v.distance > TRIGGER_RADIUS_M or v.handle not in view.stationary_handles:
            continue
        if best is None or v.distance < best.distance:
            best = v
    return best


def _parked_helicopter(state: GameState) -> NearbyVehicle | None:
    best: NearbyVehicle | None = None
    for v in empty_vehicles(state, HELI_RADIUS_M):
        if (v.vehicle_class or "").strip().lower() != "helicopters":
            continue
        if best is None or v.distance < best.distance:
            best = v
    return best


def _nearest_approach(state: GameState) -> dict[str, Any]:
    here = player_pos(state)
    return min(STUNT_APPROACHES, key=lambda a: planar_distance(here, a["pos"]))


def _ammo_snapshot(state: GameState) -> dict[str, int]:
    """Rounds carried, per tracked weapon, at the moment a goal is picked.

    Graded later as a TOTAL across the tracked set rather than per weapon,
    because which weapon the bridge selects is a bridge-side decision taken from
    the range at task start (shotgun inside 10 m, pistol beyond) and the harness
    deliberately does not duplicate that rule — see the CONTRACTS proposal.
    """
    w = getattr(state.player, "weapon", None)
    return dict(w.owned or {}) if w is not None else {}


# -- big_jump (L1) ---------------------------------------------------------------


def _needs_big_jump(state: GameState, view: RoamView) -> bool:
    if view.mood == "scared":
        return False
    if not state.player.in_vehicle or state.vehicle is None:
        return False
    if not air_reported(state):
        return False  # pre-1.7.0 bridge: `done_when` could never fire
    return (state.vehicle.vehicle_class or "").strip().lower() not in FLYING_CLASSES


def _plan_big_jump(state, view):
    approach = _nearest_approach(state)
    pos = approach["pos"]
    return (
        [
            _waypoint(pos),
            _drive_to(pos, float(approach["speed_mps"]), "rushed", 20.0),
            _wander("rushed"),
        ],
        {"start": player_pos(state), "approach": approach["name"]},
    )


def _done_big_jump(state: GameState, snap: dict[str, Any]) -> bool:
    """Wheels off the ground, after a run-up long enough to have been a run-up.

    `in_air` is a bridge field (IS_ENTITY_IN_AIR), so this is a READ, not the
    z-delta guess the old UNBUILDABLE entry refused to ship. At a 2-4 Hz poll a
    real jump is 1-8 snapshots long, so it is observable; a jump missed between
    polls costs a completion, never a false one.
    """
    return in_air(state) and (
        snap.get("_from_start_m", 0.0) >= BIG_JUMP_RUNUP_M
        or snap.get("_elapsed", 0.0) >= BIG_JUMP_MIN_S
    )


# -- taxi_ride (L1) --------------------------------------------------------------


def _needs_taxi_ride(state: GameState, view: RoamView) -> bool:
    if not supports_v17(state) or state.player.in_vehicle or state.player.wanted > 0:
        return False
    return _stationary_taxi(state, view) is not None


def _plan_taxi_ride(state, view):
    cab = _stationary_taxi(state, view)
    assert cab is not None
    name, target = _landmark_choice(state, view)
    steps: list[dict[str, Any]] = [_waypoint(target)]
    pos = _vpos(cab)
    if pos is not None and cab.distance > VEHICLE_APPROACH_M:
        steps.append(_walk_to(pos, run=True))
    # The waypoint FIRST, then the seat: the game's own cab AI drives to the
    # active waypoint, so setting it after he is seated would be a ride to
    # wherever the last waypoint happened to be.
    steps.append(
        {"type": "enter_vehicle_seat", "params": {"handle": cab.handle, "seat": PASSENGER_SEAT}}
    )
    return steps, {"taxi": cab.handle, "start": player_pos(state), "landmark": name}


def _done_taxi_ride(state: GameState, snap: dict[str, Any]) -> bool:
    """In the back of THAT cab, and it has actually taken him somewhere.

    `vehicle.seat` (1.7.0) is what makes this honest: without it, "in the taxi"
    is equally true of having jacked it and driven off, which is a different
    goal (`steal_nice_car`) and a different line on air.
    """
    if not riding_as_passenger(state) or state.vehicle is None:
        return False
    if state.vehicle.handle != snap.get("taxi"):
        return False
    return snap.get("_from_start_m", 0.0) >= TAXI_RIDE_M


# -- drive_by_run (L2) -----------------------------------------------------------


def _drive_by_target(state: GameState) -> Any | None:
    """Nearest thing worth leaning out of the window at: a gang member first."""
    gang = [p for p in gang_nearby(state) if p.distance <= DRIVE_BY_RADIUS_M]
    if gang:
        return min(gang, key=lambda p: p.distance)
    marks = [p for p in state.nearby.peds if p.distance <= DRIVE_BY_RADIUS_M and _fightable(p)]
    return min(marks, key=lambda p: p.distance) if marks else None


def _needs_drive_by(state: GameState, view: RoamView) -> bool:
    return (
        smg_loaded(state)
        and state.player.in_vehicle
        and state.player.wanted == 0
        and state.player.health >= GANG_MIN_HEALTH
        and _drive_by_target(state) is not None
    )


def _plan_drive_by(state, view):
    mark = _drive_by_target(state)
    assert mark is not None
    return (
        [
            {"type": "drive_by", "params": {"handle": mark.handle, "duration_s": 12.0}},
            _wander("rushed"),
        ],
        {"target": mark.handle, "start": player_pos(state), "ammo_start": _ammo_snapshot(state)},
    )


def _done_drive_by(state: GameState, snap: dict[str, Any]) -> bool:
    """He FIRED, and then he left.

    Graded on rounds gone, not on the target dying and not on the task returning
    `done`: TASK_DRIVE_BY's behaviour on a PLAYER ped is the one thing the
    combat research brief flags as contradicted between sources, so this goal is
    deliberately built so that a native which quietly does nothing produces a
    TIMEOUT and a log line — the evidence the operator needs — instead of a
    completion he did not earn.
    """
    return (
        snap.get("_ammo_spent", 0) >= 1
        and snap.get("_from_start_m", 0.0) >= DRIVE_BY_ESCAPE_M
    )


# -- three_star_survival (L3) ----------------------------------------------------


def _needs_three_star(state: GameState, view: RoamView) -> bool:
    if state.player.wanted < 2 or state.player.health < GANG_MIN_HEALTH:
        return False
    return state.player.in_vehicle or bool(empty_vehicles(state, 50.0))


def _plan_three_star(state, view):
    steps: list[dict[str, Any]] = []
    if not state.player.in_vehicle:
        steps.append(_enter("any", 50.0))
    steps.append(_wander("ignore_lights"))
    return steps, {"start": player_pos(state)}


def _done_three_star(state: GameState, snap: dict[str, Any]) -> bool:
    """Ninety seconds at three stars. The ESCAPE is not graded here on purpose.

    `available()`'s wanted rule already puts `lose_the_cops` at the top of the
    next tick's menu, and it is a better-written escape than anything this goal
    could post. Two goals chained by the engine's own rules beats one goal with
    a second phase nothing can advance — the plan runner steps on task
    completion, and `wander_drive` never completes.
    """
    return snap.get("_held3_s", 0.0) >= HOLD_THREE_S


# -- armed_rampage_block (L3) ----------------------------------------------------


# --- aircraft: go and fly something --------------------------------------------

#: Where aircraft actually sit in Story Mode. Curated coordinates, the same
#: class of authored data as LANDMARKS and STUNT_APPROACHES: `/state` has no
#: "airfield" flag, so this is written down or it does not exist. Sandy Shores
#: and the LSIA apron park planes; Higgins Helitours parks helicopters. Fort
#: Zancudo is deliberately absent — driving onto a military base is an instant
#: chase and a very short flight.
#: NOT VERIFIED against the running game: if he arrives and there is nothing to
#: take, the goal times out honestly and the log says where he stood.
AIRCRAFT_SITES: tuple[dict[str, Any], ...] = (
    {"name": "sandy_shores_airfield", "pos": (1747.0, 3273.0, 41.1)},
    {"name": "lsia_apron", "pos": (-1336.0, -3044.0, 13.9)},
    {"name": "higgins_helitours", "pos": (-724.0, -1444.0, 5.0)},
)

#: Vehicle classes that count as "he is flying something".
AIRCRAFT_CLASSES: frozenset[str] = frozenset({"planes", "helicopters"})

#: How close to the apron counts as arrived — aprons are big and the parked
#: aircraft are spread over them.
AIRFIELD_ARRIVE_M = 60.0

#: The flight itself (`fly_to`, bridge 1.8.0). Cruise speed and arrival radius
#: go on the wire; the trip length and the altitude margin shape the target.
FLIGHT_SPEED_MPS = 50.0
FLIGHT_ARRIVE_M = 120.0
#: A destination nearer than this (planar, from the APRON he takes off from,
#: not from wherever he was standing when the goal was picked) is a hop, and
#: at FLIGHT_SPEED_MPS it would be over before `done_when` could grade it.
FLIGHT_MIN_TRIP_M = 4000.0
#: Cruise altitude above the HIGHER end of the trip. `flightHeight` is absolute
#: (metres above sea level, see :func:`_fly_to`), so an altitude chosen from the
#: apron alone would fly a Sandy Shores take-off straight into Mount Chiliad.
FLIGHT_ALTITUDE_ABOVE_M = 150.0
#: Continuous seconds with `vehicle.in_air` true, in an aircraft, before the
#: goal calls it a flight. A bounce on the apron is one or two snapshots; a
#: minute in the air is a minute in the air. Measured by the engine in
#: `_fold_goal_progress` as `_airborne_s`, reset the moment the wheels touch.
FLIGHT_AIRBORNE_S = 60.0


def _nearest_aircraft_site(state: GameState) -> dict[str, Any]:
    here = player_pos(state)
    return min(AIRCRAFT_SITES, key=lambda a: planar_distance(here, a["pos"]))


def _in_aircraft(state: GameState) -> bool:
    v = state.vehicle
    return bool(
        state.player.in_vehicle
        and v is not None
        and (v.vehicle_class or "").strip().lower() in AIRCRAFT_CLASSES
    )


def _flight_destination(
    origin: tuple[float, float, float], view: RoamView
) -> tuple[str, tuple[float, float, float]]:
    """Somewhere worth flying to, at least :data:`FLIGHT_MIN_TRIP_M` from the apron.

    The same curated data every other trip uses (LANDMARKS plus the other
    aircraft sites), measured from the take-off point rather than from where he
    was standing when the goal was picked — the drive to the apron is not part
    of the flight. When nothing is far enough (it always is, on this map) the
    farthest candidate is taken rather than a hop.
    """
    candidates: dict[str, tuple[float, float, float]] = dict(LANDMARKS)
    for site in AIRCRAFT_SITES:
        candidates[site["name"]] = site["pos"]
    far = [n for n in candidates if planar_distance(origin, candidates[n]) >= FLIGHT_MIN_TRIP_M]
    if far:
        name = view.rng.choice(far)
    else:  # pragma: no cover - not reachable with the current tables
        name = max(candidates, key=lambda n: planar_distance(origin, candidates[n]))
    return name, candidates[name]


def _needs_go_flying(state: GameState, view: RoamView) -> bool:
    if not supports_v17(state):
        # `done_when` is graded on `vehicle.in_air` (bridge 1.7.0). An older
        # bridge never sends it, so the goal could only ever time out: the
        # honest degradation is to not offer it — see UNBUILDABLE_GOALS.
        return False
    if state.mission.active or state.player.wanted > 0:
        return False
    # Already sitting in an aircraft is not a reason to refuse: it is the
    # shortest possible plan (just fly it). It WAS a refusal when the bar was
    # "be seated in one", because the goal would have been born complete.
    return state.player.health >= GANG_MIN_HEALTH


def _plan_go_flying(state, view):
    """Get to an aircraft, then ACTUALLY FLY IT — the flight is a plan step.

    The old plan ended in `wander_drive`, a ground task: he stole a plane and
    taxied it round the apron. The last step is now `fly_to` (bridge 1.8.0,
    TASK_PLANE_MISSION / TASK_HELI_MISSION chosen bridge-side from the model),
    reached only after he is seated. It is an ordinary step of an ordinary
    goal: it is posted through the same wheel as every drive, so a mission
    block or a survival rung preempts it exactly as it preempts a `drive_to`,
    and nothing here can preempt them.
    """
    steps: list[dict[str, Any]] = []
    if _in_aircraft(state):
        origin = player_pos(state)
        site_name = "current_aircraft"
    else:
        site = _nearest_aircraft_site(state)
        origin = site["pos"]
        site_name = site["name"]
        steps.append(_waypoint(origin))
        if not state.player.in_vehicle:
            # Wheels first: the apron is usually a long way off, and walking
            # there is not television.
            steps.append(_enter("any", ON_FOOT_RESCUE_RADIUS_M))
        steps.append(_drive_to(origin, 30.0, "rushed", AIRFIELD_ARRIVE_M))
        # On the apron: take whatever is parked there — at an airfield that is
        # an aircraft. If it turns out to be a car, `fly_to` fails at once with
        # `not_an_aircraft`, the plan runs out, and the one replan tries again
        # from where he actually is.
        steps.append(_enter("any", ON_FOOT_RESCUE_RADIUS_M))
    dest_name, dest = _flight_destination(origin, view)
    altitude = max(origin[2], dest[2]) + FLIGHT_ALTITUDE_ABOVE_M
    steps.append(_waypoint(dest))
    steps.append(_fly_to(dest, altitude))
    return steps, {
        "site": site_name,
        "start": player_pos(state),
        "destination": dest_name,
        "altitude_m": altitude,
    }


def _done_go_flying(state: GameState, snap: dict[str, Any]) -> bool:
    """He FLEW: :data:`FLIGHT_AIRBORNE_S` continuous seconds off the ground, in
    an aircraft. Being seated in one is where the flight starts, not where the
    goal ends. `_airborne_s` is the engine's fold over `vehicle.in_air`
    (IS_ENTITY_IN_AIR, bridge 1.7.0) and resets the moment the wheels touch, so
    a bounce on the apron cannot fake it. If he never gets off the ground the
    bridge fails the step (`did_not_take_off`) and the goal times out honestly.
    """
    return _in_aircraft(state) and snap.get("_airborne_s", 0.0) >= FLIGHT_AIRBORNE_S


def _needs_shoot_and_run(state: GameState, view: RoamView) -> bool:
    if state.mission.active or not weapon_reported(state):
        return False
    if state.player.in_vehicle or state.player.health < GANG_MIN_HEALTH:
        return False
    mark = nearest_mark(state)
    # `shoot_at` selects by range exactly as `fight_ped{weapon:"armed"}` does,
    # so the same rounds check applies: the weapon the bridge is about to put
    # in his hands must have something in it, or this is a man pointing.
    return mark is not None and loaded_gun_for(state, mark.distance)


def _plan_shoot_and_run(state, view):
    mark = nearest_mark(state)
    assert mark is not None
    here = player_pos(state)
    bearing = math.radians(state.player.heading + view.rng.uniform(120.0, 240.0))
    away = (
        here[0] - (SHOOT_RUN_M * math.sin(bearing)),
        here[1] + (SHOOT_RUN_M * math.cos(bearing)),
        here[2],
    )
    return (
        [
            {"type": "shoot_at", "params": {"handle": mark.handle, "duration_s": 5.0}},
            _walk_to(away, run=True),
        ],
        {"start": here, "ammo_start": _ammo_snapshot(state)},
    )


def _done_shoot_and_run(state: GameState, snap: dict[str, Any]) -> bool:
    """Fired, then put distance between himself and it. Graded on rounds gone
    and on displacement, never on the target dying — the same honesty rule
    `drive_by_run` uses."""
    spent = snap.get("_ammo_spent", 0) >= 1
    return spent and snap.get("_from_start_m", 0.0) >= SHOOT_RUN_DONE_M


def _needs_rampage(state: GameState, view: RoamView) -> bool:
    if not weapon_reported(state) or state.player.in_vehicle:
        return False
    if state.player.wanted > 0 or state.player.health < GANG_MIN_HEALTH:
        return False
    mark = nearest_mark(state)
    # Rounds, not ownership — see `loaded_gun_for`. Offering this with every
    # gun owned and every magazine empty is how one arrest turned a 240 s
    # "rampage" into four minutes of standing still with an empty pistol.
    return mark is not None and loaded_gun_for(state, mark.distance)


def _plan_rampage(state, view):
    mark = nearest_mark(state)
    assert mark is not None
    # `weapon: "armed"` is the bridge-side selection rule (CONTRACTS proposal):
    # pump shotgun inside 10 m, pistol beyond, read from the range at task start
    # rather than from a snapshot that is up to a poll period old.
    return (
        [{"type": "fight_ped", "params": {"handle": mark.handle, "weapon": "armed"}}],
        {"start": player_pos(state), "ammo_start": _ammo_snapshot(state)},
    )


def _done_rampage(state: GameState, snap: dict[str, Any]) -> bool:
    """A minute of it, and rounds actually gone.

    The "one block" half of the brief is enforced by the PLAN (the target is a
    ped already within :data:`FIGHT_RADIUS_M`) and reported, not graded: a
    completion test he can fail by walking twenty metres is a goal that locks
    free roam until its timeout, which is the failure mode this module exists to
    prevent.
    """
    return snap.get("_elapsed", 0.0) >= RAMPAGE_S and snap.get("_ammo_spent", 0) >= 1


# -- shoot_a_cop (L3) -----------------------------------------------------------
#
# THE ONE PLACE HE STARTS ON THE LAW. He could not previously fight back or
# shoot people properly; initiating against police is now a deliberate, gated
# skill. Defending against a cop who is already shooting was always allowed
# (the threat reflex fights any `hostile` in reach, uniform or not). What was
# forbidden — by rules.md rule 6, by PROTECTED_PED_MODELS and by the catalog's
# own "the nearest man who is not a cop" — was INITIATING. This goal is that
# initiation, and the gates are the whole design:
#
#   * chaos tier 3 only, so six deaths in an hour take it off the menu (the
#     ladder's step-down), and it is never offered in the first thirty minutes;
#   * a LOADED gun for the range the bridge will pick (`loaded_gun_for`), never
#     fists against a service pistol;
#   * full-ish health (GANG_MIN_HEALTH), on foot, no stars, not in a mission —
#     starting a police shootout while already wanted is not a bit, it is the
#     chase he was already in;
#   * a cop in `nearby.peds` within FIGHT_RADIUS_M who is still `neutral` — an
#     opportunity the world handed him, not a hunt across the map;
#   * the long cooldown and the 2.0 chaos cost of the other L3 goals.
#
# `calm` is False, so it is withheld below CALM_HEALTH_FRACTION like every other
# fight. `wants_heat` is True: the stars are the CONSEQUENCE this goal exists to
# produce, and without the exemption the wanted override would kill it at the
# first star, before `done_when` could grade the shot. `lose_the_cops` takes over
# the tick this completes, the same hand-off `armed_rampage_block` uses.
#
# What this deliberately is NOT: a change to the threat reflex (it still never
# starts anything), a trigger that outranks a supercar, or a default. The prompt
# (rules.md rule 6, action_catalog.md) says the same thing in the same words.


def _needs_shoot_a_cop(state: GameState, view: RoamView) -> bool:
    if state.mission.active or not weapon_reported(state):
        return False
    if state.player.in_vehicle or state.player.wanted > 0:
        return False
    if state.player.health < GANG_MIN_HEALTH:
        return False
    cop = nearest_cop(state)
    return cop is not None and loaded_gun_for(state, cop.distance)


def _plan_shoot_a_cop(state, view):
    cop = nearest_cop(state)
    assert cop is not None
    # `weapon: "armed"`: the bridge selects the loaded gun for the range at task
    # start (WeaponState.SelectForRange). The threat reflex's rung 4 will keep
    # him fighting whoever answers; this goal only names the first one.
    return (
        [{"type": "fight_ped", "params": {"handle": cop.handle, "weapon": "armed"}}],
        {
            "mark": cop.handle,
            "task_before": state.last_task.id,
            "start": player_pos(state),
            "ammo_start": _ammo_snapshot(state),
        },
    )


def _done_shoot_a_cop(state: GameState, snap: dict[str, Any]) -> bool:
    """He fired at the officer, and that encounter is over — one way or another.

    Rounds gone is the non-negotiable half (`_ammo_spent`, the same fold
    `armed_rampage_block` grades on): a goal called `shoot_a_cop` that
    completes without a shot would put a lie on the dashboard. The other half
    is any of: the bridge's own `fight_ped` reporting `done` AFTER this goal
    was picked (target dead or gone — the id check keeps a pre-pick `done`
    from counting, exactly as `pick_a_fight` does), the officer no longer in
    `nearby.peds` (fled or streamed out; the scan does not drop corpses, so
    absence is honest), or :data:`COP_FIGHT_S` on the clock. The clock arm is
    what keeps this from locking free roam until its timeout while the engine
    trades shots with a man behind a car door.
    """
    if snap.get("_ammo_spent", 0) < 1:
        return False
    handle = snap.get("mark")
    if all(p.handle != handle for p in state.nearby.peds):
        return True
    last = state.last_task
    if (
        last.type == "fight_ped"
        and last.status == "done"
        and last.id is not None
        and last.id != snap.get("task_before")
    ):
        return True
    return snap.get("_elapsed", 0.0) >= COP_FIGHT_S


# -- helicopter_grab (L3) --------------------------------------------------------


def _needs_helicopter(state: GameState, view: RoamView) -> bool:
    return not state.player.in_vehicle and _parked_helicopter(state) is not None


def _plan_helicopter(state, view):
    heli = _parked_helicopter(state)
    assert heli is not None
    return _approach_then_enter(heli, "any", PROXIMITY_SEARCH_RADIUS_M), {
        "target_model": heli.model
    }


def _done_helicopter(state: GameState, snap: dict[str, Any]) -> bool:
    return (
        state.player.in_vehicle
        and state.vehicle is not None
        and (state.vehicle.vehicle_class or "").strip().lower() == "helicopters"
    )


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
        # No car in the snapshot's own list, but `enter_nearest_vehicle` searches
        # the LIVE world at whatever radius it is given and walks him there — and
        # measured in-game (2026-09-03) a 60 m search is what actually gets him
        # off his feet. Ask for that before falling back to walking, because "he
        # is driving" is the show and "he is walking at a landmark" is not.
        steps.append(_enter("any", ON_FOOT_RESCUE_RADIUS_M))
        # ...and if even that finds nothing, he still walks rather than stands:
        # a failed step advances the plan, so the walk below is the floor's floor.
        _, target = _landmark_choice(state, view)
        steps.append(_walk_to(target, run=True))
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
        level=2,
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
        # L2: a fist fight answers back. `weapon: "unarmed"` is the point of the
        # goal, not a detail — the bridge selects WeaponHash.Unarmed before it
        # tasks combat, so starting something with a stranger stays a fist fight
        # even when he is carrying, which is the difference between a bit and a
        # murder.
        level=2,
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
        level=2,
        # A gang fight makes noise and noise makes stars. Same reasoning as
        # `steal_cop_car`: heat is a consequence, not the aim, but the override
        # must not kill the goal the moment it starts working.
        wants_heat=True,
    ),
    Goal(
        id="chase_that_car",
        category="stunt",
        description="tail the nicest thing on the road and see where it goes",
        why="where's he going in that",
        needs=_needs_chase_that_car,
        plan=_plan_chase_that_car,
        done_when=_done_chase_that_car,
        timeout_s=CHASE_DONE_S,
        cooldown_s=6 * 60.0,
    ),
    Goal(
        id="jack_a_driver",
        category="trouble",
        description="take a car that still has someone in it",
        why="he's not using it properly",
        needs=_needs_jack_a_driver,
        plan=_plan_jack_a_driver,
        done_when=_done_jack_a_driver,
        timeout_s=60.0,
        cooldown_s=8 * 60.0,
        chaos_cost=1.0,
        wants_heat=True,
        level=2,
    ),
    Goal(
        id="honk_run",
        category="scenic",
        description="drive through town leaning on the horn for no reason",
        why="they need to know I'm here",
        needs=_needs_honk_run,
        plan=_plan_honk_run,
        done_when=_done_honk_run,
        timeout_s=90.0,
        cooldown_s=10 * 60.0,
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
        id="slowest_thing_fastest_road",
        category="stunt",
        description="take the slowest thing on the block onto the freeway",
        why="it has wheels",
        needs=_needs_slow_freeway,
        plan=_plan_slow_freeway,
        done_when=_done_slow_freeway,
        # 1.5 km at 8-12 m/s is ~150-190 s of driving, before the walk to the
        # mower and the engine's own on-ramp pathing. `up_only` already uses 600
        # for a two-leg goal.
        timeout_s=600.0,
        # Slow vehicles are rare and the bit is one-note: it should be an event,
        # not a rotation staple. Between `steal_cop_car` (20 min) and
        # `up_only` (40 min).
        cooldown_s=30 * 60.0,
        # No bodies, but a real chance of being rear-ended at 120 on the
        # carriageway: above `freeway_run` (0.25), level with `hijack_bus`.
        chaos_cost=0.5,
        # L1 is the ladder's own "nuisance with no bodies. Cars, hills, buses,
        # distance", which is exactly this.
        level=1,
    ),
    Goal(
        id="up_only",
        category="scenic",
        description="drive to the top; the top is always in",
        why="up only",
        needs=_needs_up_only,
        plan=_plan_up_only,
        done_when=_done_up_only,
        # Long: it is a climb out of the city and the whole way back down. The
        # descent is a step in the plan, not an afterthought, so the budget has
        # to cover both legs.
        timeout_s=600.0,
        cooldown_s=40 * 60.0,
        chaos_cost=0.25,
        calm=True,
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
        level=2,
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
    # ---- T7 additions ---------------------------------------------------------
    Goal(
        id="big_jump",
        category="stunt",
        description="find a ramp and get all four wheels off the ground",
        why="that road goes up",
        needs=_needs_big_jump,
        plan=_plan_big_jump,
        done_when=_done_big_jump,
        # The approach is the nearest one, so the drive there is short, and
        # the jump either happens on the run-up or it does not: measured in
        # the soak, 240 s here was the single longest stretch with nothing
        # new on screen. Two minutes is the run-up plus one honest retry.
        timeout_s=120.0,
        cooldown_s=12 * 60.0,
        chaos_cost=0.25,
        level=1,
    ),
    Goal(
        id="taxi_ride",
        category="errand",
        description="get in the back of a cab like a normal person",
        why="not driving for once",
        needs=_needs_taxi_ride,
        plan=_plan_taxi_ride,
        done_when=_done_taxi_ride,
        timeout_s=300.0,
        cooldown_s=20 * 60.0,
        calm=True,
        level=1,
    ),
    Goal(
        id="drive_by_run",
        category="trouble",
        description="lean out at the corner, then be somewhere else",
        why="the SMG is in the car",
        needs=_needs_drive_by,
        plan=_plan_drive_by,
        done_when=_done_drive_by,
        timeout_s=180.0,
        cooldown_s=20 * 60.0,
        chaos_cost=2.0,
        # Shooting from a car earns stars immediately; without the exemption the
        # override kills the goal a second into its own escape and `done_when`
        # can never fire. Same reasoning as `earn_two_stars`.
        wants_heat=True,
        level=2,
    ),
    Goal(
        id="three_star_survival",
        category="trouble",
        description="stay ahead of three stars for a minute and a half",
        why="they brought helicopters",
        needs=_needs_three_star,
        plan=_plan_three_star,
        done_when=_done_three_star,
        timeout_s=300.0,
        cooldown_s=40 * 60.0,
        chaos_cost=2.0,
        wants_heat=True,
        level=3,
    ),
    Goal(
        id="armed_rampage_block",
        category="trouble",
        description="make a scene on this block, then walk away from it",
        why="this block, then gone",
        needs=_needs_rampage,
        plan=_plan_rampage,
        done_when=_done_rampage,
        timeout_s=240.0,
        cooldown_s=30 * 60.0,
        chaos_cost=2.0,
        wants_heat=True,
        level=3,
    ),
    Goal(
        id="helicopter_grab",
        category="acquisition",
        description="take the helicopter nobody is sitting in",
        why="nobody is flying that",
        needs=_needs_helicopter,
        plan=_plan_helicopter,
        done_when=_done_helicopter,
        timeout_s=180.0,
        cooldown_s=45 * 60.0,
        chaos_cost=2.0,
        # A police helicopter is police property; same exemption and the same
        # reason as `steal_cop_car`.
        wants_heat=True,
        level=3,
    ),
    Goal(
        id="go_flying",
        category="stunt",
        description="get to an airfield, take something that flies, fly it",
        why="the ground is boring",
        needs=_needs_go_flying,
        plan=_plan_go_flying,
        done_when=_done_go_flying,
        # A cross-map drive, finding something on the apron, a take-off and a
        # minute in the air. Long, and worth it: this is the goal that ends "he
        # has been driving for too long". Was 600 s when the bar was "seated";
        # the flight itself needs the extra.
        timeout_s=900.0,
        cooldown_s=45 * 60.0,
        chaos_cost=0.5,
        level=1,
    ),
    Goal(
        id="shoot_and_run",
        category="trouble",
        description="pick someone, put rounds near them, and leg it",
        why="somebody was asking for it",
        needs=_needs_shoot_and_run,
        plan=_plan_shoot_and_run,
        done_when=_done_shoot_and_run,
        timeout_s=150.0,
        cooldown_s=8 * 60.0,
        chaos_cost=2.0,
        wants_heat=True,
        level=2,
    ),
    Goal(
        id="shoot_a_cop",
        category="trouble",
        description="start on the nearest cop, then live with it",
        why="that uniform has opinions",
        needs=_needs_shoot_a_cop,
        plan=_plan_shoot_a_cop,
        done_when=_done_shoot_a_cop,
        # COP_FIGHT_S plus the approach the engine's combat task makes on its
        # own; anything longer is a standoff the stream does not need.
        timeout_s=120.0,
        cooldown_s=45 * 60.0,
        chaos_cost=2.0,
        # The stars are the point. Without the exemption the wanted override
        # ends the goal at the first star, before the shot is graded.
        wants_heat=True,
        # L3 with the other things that can genuinely end him. See the block
        # comment above `_needs_shoot_a_cop` for every gate.
        level=3,
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


def _t_gang_corner(state: GameState, view: RoamView) -> bool:
    """Enough of them, close enough, that "whose corner is this" is a real question."""
    return len(gang_nearby(state)) >= GANG_MIN_PEDS


def _t_onramp(state: GameState, view: RoamView) -> bool:
    """In something quick, near a curated freeway approach point.

    THE POINT LIST IS CURATED AND UNVERIFIED. `/state` cannot answer "is this a
    freeway": `location.street` is a name string with no road-type flag (see
    :data:`UNCOMPUTABLE_TRIGGERS`), so the only options were authored data or no
    trigger at all. The points are REUSED from
    :data:`activities.STUNT_APPROACHES`, which are already-curated approach
    coordinates near freeway ramps — nothing new was invented here, and none of
    them was surveyed against the running game. A wrong point costs a promotion
    (the goal is still on the menu, just not at the top); it can never cause a
    false completion, because `freeway_run` is graded on displacement and never
    on where he was standing when it started.
    """
    if not _needs_freeway_run(state, view):
        return False
    here = player_pos(state)
    return any(planar_distance(here, a["pos"]) <= ONRAMP_NEAR_M for a in FREEWAY_ONRAMPS)


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
    # T7 additions. Both sit BELOW the acquisition triggers on purpose: a
    # supercar under his nose is a stronger reason to change what he is doing
    # than a corner he could pick a fight on, and the first matching trigger
    # takes the top slot.
    Trigger("gang_trouble", "wrong corner, wrong colours", _t_gang_corner),
    # Below the gang corner and every acquisition: a cop on the pavement is a
    # reason to put the bit at the top of the menu ONLY when every gate in
    # `_needs_shoot_a_cop` already passed (a trigger can promote, never admit).
    Trigger("shoot_a_cop", "that uniform has opinions", _needs_shoot_a_cop),
    Trigger("freeway_run", "there's the on-ramp", _t_onramp),
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
        cadence: RoamCadence | None = None,
        level: int = CHAOS_START_LEVEL,
    ) -> None:
        self._rng = rng or random.Random()
        #: Operator switch (Settings.missions_enabled). Off: `start_nearest_mission`
        #: is never offered and never forced, so free roam is the whole show.
        self._missions_enabled = missions_enabled
        #: T7: when the story gets offered and when it gets forced, as data.
        #: Defaulted from the environment so the operator can retune the show
        #: without a code change (WASTED_ROAM_GOALS_BEFORE_MISSION /
        #: WASTED_ROAM_MISSION_FORCE_S) and a caller can inject one in a test.
        self.cadence = cadence or RoamCadence.from_env()
        self._clock = clock
        #: T7: the chaos tier, and the deaths that move it.
        # `WASTED_CHAOS_LEVEL=1|2|3` pins the starting tier from the box's .env
        # (docs/go-live.md §6); the ladder still moves itself from there.
        self.ladder = ChaosLadder(clock, int(_env_float("WASTED_CHAOS_LEVEL", float(level))))
        #: Death EDGE detection lives here, not in the ladder: `player.dead` is
        #: true for every tick of the wasted screen, so counting ticks would
        #: spend the whole hour's death budget on one death.
        self._was_dead = False
        self.current: LockedGoal | None = None

        self._last_run: dict[str, float] = {}
        self._chaos_spent: list[tuple[float, float]] = []
        self._last_category: str | None = None
        #: The last few goal ids he actually ran, newest last. Two rules hang off
        #: it: the SAME id is never offered twice running (hard), and anything in
        #: this window is sorted to the back of the menu (soft), so a viewer does
        #: not watch "steal a car, drive to the sign, steal a car, drive to the
        #: sign" — the operator's "make sure no same pattern is followed".
        self._recent_ids: collections.deque[str] = collections.deque(maxlen=NOVELTY_WINDOW)
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

    @property
    def level(self) -> int:
        """The chaos tier goals are filtered on. Read-only: only the ladder moves it."""
        return self.ladder.level

    def observe(self, state: GameState, *, mood: str = "bored", mood_style: str = "normal") -> None:
        """Fold this snapshot into the cross-tick derivations. Once per tick."""
        now = self._clock()
        self._observe_death(state, now)
        self.ladder.tick()
        if self.ladder.transition is not None:
            # The ladder gets its own line rather than sharing the goal
            # transition slot: a tier change is news, and losing it because a
            # goal happened to end on the same tick would make the step-down
            # invisible on air and in the log.
            self._transition = self.ladder.transition
            self.ladder.transition = None
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

    def _observe_death(self, state: GameState, now: float) -> None:
        """Count a death ONCE, on the false->true edge of `player.dead`.

        Only deaths that happen while free roam owns him count towards the
        ladder: dying inside a mission is the mission's problem, and stepping
        the nuisance tier down for it would punish the wrong half of the show.
        `self.current is not None` is the test, and it is deliberately the LOCK
        rather than "the engine exists" — between goals nobody is steering.
        """
        dead = state.player.dead
        if dead and not self._was_dead and self.current is not None:
            self.ladder.note_death(now)
            log.info(
                "roam death",
                extra={
                    "kv": {
                        "goal": self.current.goal.id,
                        "level": self.ladder.level,
                        "deaths_in_window": self.ladder.deaths_in_window(),
                    }
                },
            )
        self._was_dead = dead

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
        if getattr(state.phone, "in_call", False):
            # A connected call is not stillness he chose: the game runs no
            # movement task on a ped on the phone (live 2026-09-03), so the
            # stuck watchdog's clock is held rather than spent.
            self._anchor, self._anchor_at = here, now
            return
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
        budget = CHAOS_BUDGET_BY_LEVEL.get(self.ladder.level, CHAOS_BUDGET_PER_HOUR)
        return budget - sum(c for _, c in self._chaos_spent)

    # -- the menu ---------------------------------------------------------------

    def mission_forced(self) -> bool:
        """Has the story waited long enough that a job is the ONLY thing on offer?

        The wall clock and nothing else (:class:`RoamCadence`). The completed-goal
        counter used to force too, which is how a stream that is supposed to be
        free roam collapsed its whole menu to one goal after three cars; that
        counter is now :meth:`mission_offered`, an offer he can refuse.
        """
        if not self._missions_enabled:
            return False
        return self._clock() - self._roam_started_at >= self.cadence.force_after_s

    def mission_offered(self) -> bool:
        """Has he done enough roam goals that the job belongs at the TOP of the menu?

        An offer, not an order: every other legal goal stays on the list under
        it, so the draw (or the model) may still take a car. The menu only ever
        collapses on :meth:`mission_forced`.
        """
        if not self._missions_enabled:
            return False
        return self._completed_since_mission >= self.cadence.goals_before_offer

    def available(self, state: GameState) -> list[Offer]:
        """The ordered menu. The ONLY goals that may be picked this tick.

        Order of the rules is the specification:
        1. `wanted > 0` puts `lose_the_cops` at the top — but the goals whose
           whole point is heat (`wants_heat`) stay on the menu under it.
        2. Forty minutes without a job forces the job (:class:`RoamCadence`).
        3. Otherwise: tier + needs + cooldown + chaos + health + category
           alternation.
        4. A matching trigger sorts its goal to the top with a quotable `why`.
        5. The menu is never empty: the fallback is always legal.
        """
        view = self.view
        now = self._clock()

        # 1. The cops. `lose_the_cops` leads, and everything that is ABOUT heat
        # rides along: the old rule replaced the whole menu, which meant
        # `three_star_survival` — a goal whose `needs` is "he already has two
        # stars" — could never be offered at all, and `earn_two_stars` could
        # never be re-picked after the first star landed. Everything else still
        # yields: a scenic drive with a police helicopter overhead is not a
        # scenic drive.
        cops = GOALS_BY_ID["lose_the_cops"]
        if cops.needs(state, view):
            offers = [Offer(cops, "they are on me", triggered=True)]
            offers += self._filtered(state, view, now, heat_only=True)
            self._offers = offers
            self._offered = tuple(o.id for o in offers)
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
        offers = self._filtered(state, view, now, heat_only=False)

        # 3b. Enough goals done that the job belongs at the top — as an OFFER.
        # It goes in front of the triggers deliberately and then the triggers
        # run: a live opportunity (a supercar under his nose, an unattended cop
        # car) still outranks "you have done six things", because the offer is
        # about pacing and the trigger is about something that is happening.
        if self.mission_offered() and job.needs(state, view):
            offers = [Offer(job, "six down, time to get paid", triggered=True)] + [
                o for o in offers if o.id != job.id
            ]

        # 4. Triggers: at most one goal is promoted, and it keeps its `why`.
        offers = self._promote_triggered(state, offers)

        # 5. The fallback is ALWAYS legal and always last. Not "when the menu
        # came out empty" — always, so that no combination of cooldowns,
        # category alternation and a quiet street can ever produce a tick on
        # which the honest answer is "nothing to do". It is exempt from every
        # filter above for the same reason.
        fallback = next(g for g in CATALOG if g.fallback)
        offers = [o for o in offers if o.id != fallback.id]
        # Soft novelty: a triggered offer keeps the front (the world just handed
        # him a reason); everything else that ran recently goes to the back.
        recent = set(self._recent_ids)
        head = [o for o in offers if o.triggered]
        rest = [o for o in offers if not o.triggered]
        rest.sort(key=lambda o: o.id in recent)
        offers = head + rest
        offers.append(Offer(fallback, fallback.why))

        self._offers = offers
        self._offered = tuple(o.id for o in offers)
        return list(offers)

    def _filtered(
        self, state: GameState, view: RoamView, now: float, *, heat_only: bool
    ) -> list[Offer]:
        """Every ordinary goal that passes every filter, in catalog order.

        Factored out of :meth:`available` so the wanted branch and the ordinary
        branch cannot drift apart: the ONLY difference between them is
        `heat_only`, which keeps the `wants_heat` goals and drops the rest.
        """
        chaos = self.chaos_available()
        hurt = health_fraction(state) < CALM_HEALTH_FRACTION
        offers: list[Offer] = []
        for goal in CATALOG:
            if goal.override_only or goal.fallback:
                continue
            if heat_only and not goal.wants_heat:
                continue
            # T7: the chaos tier. Checked FIRST because it is the cheapest test
            # and because a goal above the tier must not even run its `needs` —
            # `needs` is a live-state read and running it costs nothing, but a
            # tier the ladder just took away should look exactly like a goal
            # that is not in the catalog.
            if goal.level > self.ladder.level:
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
            if self._recent_ids and goal.id == self._recent_ids[-1]:
                continue  # never the exact same goal twice running
            if not goal.needs(state, view):
                continue
            offers.append(Offer(goal, goal.why))
        return offers

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

    def heat_is_the_goal(self) -> bool:
        """Is the locked goal one that WANTS the police (`Goal.wants_heat`)?

        Read by the threat reflex: its stars-only rung must not flee from the
        heat a goal just went and earned. The other rungs are not consulted —
        being shot is being shot, whatever the goal.
        """
        return self.current is not None and self.current.goal.wants_heat

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
        self._recent_ids.append(goal.id)
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
        self._fold_goal_progress(state, locked)
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
        if self.still_for_s() >= GOAL_STUCK_S and not _movement_task_running(state):
            # Only second-guess stillness the BRIDGE is not already accounting
            # for. While a movement task (walk/drive/enter/flee) is `running`,
            # the bridge's own no_progress watchdog owns "is he actually
            # moving"; escalating here on the roam anchor as well is what turned
            # a slow-but-progressing walk-to-a-far-car into a "stuck" verdict
            # that re-posted the task and froze him (2026-09-03). If the task is
            # genuinely stalled the bridge fails it, `last_task.status` leaves
            # `running`, and this fires on the next tick as it should.
            locked.strikes += 1
            self.reset_movement_anchor()
            if locked.strikes >= GOAL_STUCK_STRIKES:
                return "stuck"
            return "escalate"
        return None

    #: The keys :meth:`_fold_goal_progress` maintains inside a locked goal's
    #: snapshot. Underscore-prefixed so they can never collide with a plan's own
    #: pick-time memory, and listed here so a `done_when` author can see the
    #: whole vocabulary in one place.
    PROGRESS_KEYS: tuple[str, ...] = (
        "_elapsed",
        "_from_start_m",
        "_held3_s",
        "_ammo_spent",
        "_climb_m",
        "_drop_from_peak_m",
        "_airborne_s",
    )

    def _fold_goal_progress(self, state: GameState, locked: LockedGoal) -> None:
        """Update the cross-tick measurements a `done_when` may read.

        The predicates stay PURE functions of (state, snapshot) — the same
        property the module's `needs`/`plan` trio has — and the derivation that
        needs consecutive snapshots happens here, in the engine, which is the
        only place that sees every tick. Three of the four measurements are
        impossible to express any other way: "he has held three stars for
        ninety seconds" and "he has fired a round since he started" are not
        facts about one snapshot.

        The snapshot dict is already a LIVE record (`replan` mutates it with
        `setdefault`), so writing into it is not a new liberty.
        """
        now = self._clock()
        snap = locked.snapshot
        snap["_elapsed"] = locked.elapsed(now)

        start = snap.get("start")
        if start is not None:
            snap["_from_start_m"] = planar_distance(player_pos(state), start)

        # Continuous seconds at three stars or more. Broken by dropping below.
        if state.player.wanted >= 3:
            since = snap.get("_held3_since")
            if since is None:
                snap["_held3_since"] = now
                since = now
            snap["_held3_s"] = now - since
        else:
            snap["_held3_since"] = None
            snap["_held3_s"] = 0.0

        # Continuous seconds airborne IN AN AIRCRAFT, for `go_flying`. Same
        # shape as the three-star hold: broken the moment `vehicle.in_air`
        # reads false or he is no longer in something that flies, so a kerb
        # bounce, a taxi over a bump or a crash-landing all read as zero. A
        # pre-1.7.0 bridge never sends `in_air`, the model default is False,
        # and the goal that reads this is not offered on such a bridge.
        if _in_aircraft(state) and in_air(state):
            since = snap.get("_airborne_since")
            if since is None:
                snap["_airborne_since"] = now
                since = now
            snap["_airborne_s"] = now - since
        else:
            snap["_airborne_since"] = None
            snap["_airborne_s"] = 0.0

        # Rounds gone since the goal was picked, totalled over the tracked
        # loadout. A TOTAL rather than per-weapon on purpose: which weapon the
        # bridge selects is decided bridge-side from the range at task start,
        # and duplicating that rule here would be two owners of one decision.
        # Only DECREASES count, so picking ammo up mid-goal cannot go negative
        # and cannot mask a shot already fired.
        # Altitude, measured against HIS OWN trajectory rather than against a
        # coordinate somebody typed in. `LANDMARKS` is marked UNVERIFIED in
        # `activities.py` and the knowledge base carries an unresolved
        # disagreement about Mount Chiliad's height (797.1 m per the wiki
        # against our 766.5), so "did he reach the summit" is not a question
        # this code can honestly ask. "Did he climb 300 m, and has he since
        # given 200 m of it back" is answerable from two readings of `pos.z`
        # and is true whatever the mountain actually measures.
        start_z = snap.get("start_z")
        if start_z is not None:
            here_z = state.player.pos.z
            peak = max(float(snap.get("_peak_z", start_z)), here_z)
            snap["_peak_z"] = peak
            snap["_climb_m"] = peak - float(start_z)
            snap["_drop_from_peak_m"] = peak - here_z

        ammo_start = snap.get("ammo_start")
        if isinstance(ammo_start, dict):
            spent = 0
            for name, started in ammo_start.items():
                now_ammo = weapon_ammo(state, name)
                if now_ammo is None:
                    continue  # weapon lost (arrest, death) — not evidence of a shot
                spent += max(0, int(started) - int(now_ammo))
            snap["_ammo_spent"] = max(int(snap.get("_ammo_spent", 0)), spent)

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
        the difference between committing to a goal and going mute. CONTRACTS
        v1.13's `answer_call`/`reject_call` are not blocked either, for the same
        reason: they are `POST /task`, but they move nobody, and a locked goal
        has no business deciding whether the agent may hang up on a ringing phone.
        """
        from ..brain.schemas import MOVEMENT_TASKS

        locked = self.current
        if locked is None or action_type not in MOVEMENT_TASKS:
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
            # The tier is stated because it EXPLAINS the menu: without it, a
            # model that has seen `armed_rampage_block` on an earlier menu reads
            # its absence as an oversight and writes prose about it. One clause,
            # no instruction — the filtering is done in code either way.
            lines.append(f"ROAM TIER: L{self.ladder.level} of 3.")
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
