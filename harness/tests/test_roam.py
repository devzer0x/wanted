"""The free-roam goal engine: what is offered, what locks, and what counts as done.

The selection rules ARE the specification, so they are tested one at a time.
Game states here are constructed pydantic objects built by the explicit builders
below — pure-function inputs, not recorded sessions. `tests/fixtures/` stays
empty by design (CLAUDE.md rule 1: a fixture is only ever a recording of a real
session, never something invented by hand, so an invented one goes in a builder
where it is obviously an input and not evidence).
"""

from __future__ import annotations

import itertools
import random
import re
from pathlib import Path
from typing import Any

import pytest

from wasted_harness.behavior.roam import (
    BOREDOM_S,
    BREADCRUMB_M,
    CALM_HEALTH_FRACTION,
    CATALOG,
    CLEAN_RECOVERY_S,
    DEATHS_PER_HOUR_STEP_DOWN,
    FALLBACK_GOAL_ID,
    FREEWAY_ONRAMPS,
    GOAL_STUCK_S,
    GOAL_STUCK_STRIKES,
    GOALS_BEFORE_MISSION,
    GOALS_BY_ID,
    HOLD_THREE_S,
    INDOOR_STILL_S,
    MAX_ESCAPE_RUNGS,
    PICKABLE_GOAL_IDS,
    ROAM_BEFORE_MISSION_S,
    ROAM_CATEGORIES,
    UNBUILDABLE_GOALS,
    UNCOMPUTABLE_TRIGGERS,
    VEHICLE_RANK,
    HouseEscape,
    RoamCadence,
    RoamEngine,
    RoamView,
    air_reported,
    as_activity,
    in_air,
    is_bus_model,
    is_police_model,
    owns_weapon,
    riding_as_passenger,
    seat_reported,
    supports_v17,
    vehicle_rank,
    weapon_ammo,
    weapon_reported,
)
from wasted_harness.brain.schemas import ACTION_TYPES, BRIDGE_TASKS
from wasted_harness.bridge_client import GameState


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float) -> None:
        self.t += dt


# --- explicit builders ---------------------------------------------------------


def veh(
    handle: int = 1,
    model: str = "sultan",
    vclass: str = "Sedans",
    distance: float = 10.0,
    driver: str = "empty",
    pos: tuple[float, float, float] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "handle": handle,
        "model": model,
        "display_name": model.title(),
        "class": vclass,
        "distance": distance,
        "driver": driver,
    }
    if pos is not None:
        body["pos"] = {"x": pos[0], "y": pos[1], "z": pos[2]}
    return body


def ped(
    handle: int = 90,
    model: str = "a_m_y_skater_01",
    distance: float = 5.0,
    relationship: str = "neutral",
    pos: tuple[float, float, float] | None = (5.0, 0.0, 0.0),
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "handle": handle,
        "model": model,
        "distance": distance,
        "relationship": relationship,
    }
    if pos is not None:
        body["pos"] = {"x": pos[0], "y": pos[1], "z": pos[2]}
    return body


def make_state(**over: Any) -> GameState:
    """A minimal, contract-shaped /state body. Every field is explicit."""
    pos = over.pop("pos", (0.0, 0.0, 0.0))
    in_vehicle = over.pop("in_vehicle", False)
    vehicle = over.pop("vehicle", None)
    if in_vehicle and vehicle is None:
        vehicle = {"model": "sultan", "class": "Sedans"}
    body: dict[str, Any] = {
        "ts": "2026-09-02T00:00:00Z",
        "tick": over.pop("tick", 1000),
        "player": {
            "pos": {"x": pos[0], "y": pos[1], "z": pos[2]},
            "heading": 0.0,
            "health": over.pop("health", 200),
            "max_health": over.pop("max_health", 200),
            "armor": 0,
            "wanted": over.pop("wanted", 0),
            "cash": over.pop("cash", 500),
            "dead": over.pop("dead", False),
            "arrested": over.pop("arrested", False),
            "in_vehicle": in_vehicle,
            "control_enabled": True,
            "protagonist": over.pop("protagonist", "franklin"),
            # CONTRACTS v1.12. Absent by DEFAULT here on purpose: most of this
            # module tests behaviour that predates the field, and `HouseEscape`
            # is now explicitly the PRE-v1.12 fallback — it must keep working on
            # a snapshot that carries no `interior` key at all.
            **(
                {}
                if (interior := over.pop("interior", "absent")) == "absent"
                else {
                    "interior": (
                        None if interior is None else {"id": interior[0], "since_s": interior[1]}
                    )
                }
            ),
        },
        "vehicle": (
            None
            if vehicle is None
            else {
                "handle": vehicle.get("handle", 77),
                "model": vehicle.get("model", "sultan"),
                "display_name": vehicle.get("model", "sultan").title(),
                "class": vehicle.get("class", "Sedans"),
                "speed": vehicle.get("speed", 0.0),
                "health": 1000.0,
                "upside_down": False,
                "in_water": False,
                "stopped_for_s": vehicle.get("stopped_for_s", 0.0),
            }
        ),
        "location": {
            "street": over.pop("street", "Vinewood Blvd"),
            "zone": over.pop("zone", "Downtown Vinewood"),
        },
        "world": {"clock": "13:45", "weather": "CLEAR", "timescale": 1.0},
        "mission": {
            "active": over.pop("mission_active", False),
            "random_event_active": over.pop("random_event_active", False),
            "cutscene_active": False,
            "objective_blip": over.pop("objective_blip", None),
            "starts": over.pop("starts", []),
            "route_blips": over.pop("route_blips", []),
        },
        "nearby": {
            "vehicles": over.pop("nearby_vehicles", []),
            "peds": over.pop("nearby_peds", []),
        },
        "last_task": {
            "id": over.pop("task_id", "t-1"),
            "type": over.pop("task_type", "stop"),
            "status": over.pop("task_status", "idle"),
            "detail": over.pop("task_detail", ""),
        },
        "bridge": {"version": "1.1.0", "edition": "legacy"},
    }
    assert not over, f"unused overrides: {sorted(over)}"
    return GameState.model_validate(body)


def start(marker: tuple[float, float, float], protagonist: str = "franklin") -> dict[str, Any]:
    return {"pos": {"x": marker[0], "y": marker[1], "z": marker[2]}, "protagonist": protagonist}


def engine(seed: int = 1) -> tuple[RoamEngine, FakeClock]:
    clock = FakeClock()
    return RoamEngine(random.Random(seed), clock=clock), clock


def observed(e: RoamEngine, state: GameState, **kw: Any) -> GameState:
    """Feed a snapshot through `observe` the way the tick does, then return it."""
    e.observe(state, **kw)
    return state


# --- the catalog itself --------------------------------------------------------


def test_every_goal_is_well_formed() -> None:
    seen: set[str] = set()
    for goal in CATALOG:
        assert goal.id not in seen, f"{goal.id} is in the catalog twice"
        seen.add(goal.id)
        assert goal.category in ROAM_CATEGORIES, f"{goal.id}: {goal.category!r}"
        assert goal.description, f"{goal.id} has no description"
        # CONTRACTS §2 caps the decision's `goal` field at 12 words, and the
        # dashboard shows this string, so a longer one could never be echoed.
        assert len(goal.description.split()) <= 12, goal.description
        assert goal.why, f"{goal.id} has no quotable why"
        assert goal.timeout_s > 0
        assert goal.cooldown_s >= 0


def test_no_goal_can_ask_for_an_action_the_schema_rejects() -> None:
    """Every materialised plan, from a state rich enough to satisfy every `needs`."""
    e, _ = engine()
    state = observed(
        e,
        make_state(
            in_vehicle=True,
            vehicle={"class": "Sports", "model": "banshee", "speed": 32.0},
            random_event_active=True,
            starts=[start((300.0, 0.0, 0.0))],
            nearby_vehicles=[
                veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0)),
                veh(2, "police", "Emergency", 15.0, pos=(15.0, 0.0, 0.0)),
                veh(3, "bus", "Service", 20.0, pos=(20.0, 0.0, 0.0)),
                veh(4, "bati", "Motorcycles", 9.0, pos=(9.0, 0.0, 0.0)),
                veh(5, "taxi", "Sedans", 6.0, driver="npc", pos=(6.0, 0.0, 0.0)),
            ],
            nearby_peds=[ped()],
        ),
    )
    # A second identical snapshot so the bus reads as stationary.
    e._clock.tick(2.0)  # type: ignore[attr-defined]
    e.observe(state)
    on_foot = make_state(
        nearby_vehicles=[veh(5, "taxi", "Sedans", 6.0, "npc", pos=(6.0, 0.0, 0.0))]
    )
    # Two goals contradict the rich state by construction and get one of their
    # own: `lose_the_cops` requires stars (which `start_nearest_mission` refuses
    # to be offered with), and `take_my_car_back` requires him to be ON FOOT
    # next to the car somebody took off him.
    # The two fight goals need him ON FOOT with someone to start on, which the
    # rich in-vehicle state contradicts by construction — being in a car and out
    # of it are not simultaneously satisfiable, so they get their own snapshots.
    brawl = make_state(
        in_vehicle=False, health=200,
        nearby_peds=[
            {"handle": 40, "model": "a_m_y_hipster_01", "distance": 6.0,
             "relationship": "neutral", "pos": {"x": 6.0, "y": 0.0, "z": 0.0}},
        ],
    )
    gang = make_state(
        in_vehicle=False, health=200,
        nearby_peds=[
            {"handle": 60, "model": "g_m_y_ballasout_01", "distance": 15.0,
             "relationship": "neutral", "pos": {"x": 15.0, "y": 0.0, "z": 0.0}},
            {"handle": 61, "model": "g_m_y_famca_01", "distance": 18.0,
             "relationship": "neutral", "pos": {"x": 18.0, "y": 0.0, "z": 0.0}},
        ],
    )
    driven = make_state(
        in_vehicle=True, health=200,
        vehicle={"class": "Sports", "model": "banshee", "speed": 20.0},
        nearby_vehicles=[veh(9, "adder", "Super", 25.0, driver="npc", pos=(25.0, 0.0, 0.0))],
    )
    jackable = make_state(
        in_vehicle=False, health=200,
        nearby_vehicles=[veh(5, "taxi", "Sedans", 6.0, driver="npc", pos=(6.0, 0.0, 0.0))],
    )
    # `taxi_ride` needs the cab to read as STOPPED, which is a cross-tick
    # derivation, so it gets its own engine fed two identical snapshots.
    taxi_engine, taxi_clock = engine(seed=2)
    taxi_kerb = armed_state(
        in_vehicle=False,
        nearby_vehicles=[veh(77, "taxi", "Sedans", 9.0, driver="npc", pos=(9.0, 0.0, 0.0))],
    )
    taxi_engine.observe(taxi_kerb)
    taxi_clock.tick(2.0)
    taxi_engine.observe(taxi_kerb)
    # T7: six goals read a field that only a bridge >= 1.7.0 sends, so they need
    # a snapshot that carries it — `armed_state` is `make_state` plus exactly
    # those keys. Building them here rather than widening `make_state` keeps
    # every other test in this file exercising the pre-1.7.0 shape, which is
    # what the "an older bridge simply does not offer it" gate is for.
    full_kit = {"Pistol": 60, "MicroSMG": 90, "PumpShotgun": 24}
    gang_ped = {
        "handle": 62, "model": "g_m_y_ballasout_01", "distance": 12.0,
        "relationship": "neutral", "pos": {"x": 12.0, "y": 0.0, "z": 0.0},
    }
    special = {
        "lose_the_cops": make_state(wanted=2),
        "take_my_car_back": on_foot,
        "pick_a_fight": brawl,
        "gang_trouble": gang,
        "chase_that_car": driven,
        "honk_run": driven,
        "jack_a_driver": jackable,
        "big_jump": armed_state(
            in_vehicle=True,
            vehicle={"class": "Sports", "model": "banshee", "speed": 30.0},
            in_air=False,
        ),
        "drive_by_run": armed_state(
            in_vehicle=True,
            vehicle={"class": "Sedans", "model": "sultan", "speed": 14.0},
            health=200,
            weapon=_weapon(owned=dict(full_kit)),
            nearby_peds=[gang_ped],
        ),
        "three_star_survival": armed_state(
            wanted=3,
            in_vehicle=True,
            vehicle={"class": "Sedans", "model": "sultan", "speed": 20.0},
            health=200,
        ),
        "armed_rampage_block": armed_state(
            in_vehicle=False,
            health=200,
            weapon=_weapon(owned=dict(full_kit)),
            nearby_peds=[
                {"handle": 41, "model": "a_m_y_hipster_01", "distance": 7.0,
                 "relationship": "neutral", "pos": {"x": 7.0, "y": 0.0, "z": 0.0}},
            ],
        ),
        "helicopter_grab": armed_state(
            in_vehicle=False,
            nearby_vehicles=[veh(88, "polmav", "Helicopters", 25.0, pos=(25.0, 0.0, 0.0))],
        ),
        "taxi_ride": taxi_kerb,
    }
    jacked_view = RoamView(rng=random.Random(1), stolen_from=(5, (6.0, 0.0, 0.0)))
    for goal in CATALOG:
        goal_state = special.get(goal.id, state)
        view = jacked_view if goal.id == "take_my_car_back" else e.view
        if goal.id == "taxi_ride":
            view = taxi_engine.view
        assert goal.needs(goal_state, view), f"{goal.id} was not offerable"
        plan, _snap = goal.plan(goal_state, view)
        if goal.handoff:
            assert plan == [], "a handoff goal must post nothing of its own"
            continue
        assert plan, f"{goal.id} materialised an empty plan"
        for step in plan:
            assert step["type"] in ACTION_TYPES, f"{goal.id} uses unknown action {step['type']}"


def test_unbuildable_goals_are_declared_and_absent() -> None:
    """A goal whose done_when can never fire must not be shipped as one."""
    ids = {g.id for g in CATALOG}
    for goal_id, reason in UNBUILDABLE_GOALS.items():
        assert goal_id not in ids, f"{goal_id} is declared unbuildable but is in the catalog"
        assert len(reason) > 80, f"{goal_id} needs the missing capability named, not a label"
    for name, reason in UNCOMPUTABLE_TRIGGERS.items():
        assert len(reason) > 80, f"{name} needs a real reason"


def test_the_rank_ladder_is_the_bridge_s_own() -> None:
    """`prefer: "nicer"` is graded on the BRIDGE's ladder, so ours must be it.

    Read out of the bridge source rather than asserted from memory (CLAUDE.md
    rule 6). If the two ever disagree, `steal_nice_car` is offered on one
    definition of "nicer" and graded on another — which is the exact bug that
    let the old activity complete in a WORSE car.
    """
    src = Path(__file__).resolve().parents[2] / "bridge" / "src" / "TaskEngine.cs"
    if not src.exists():  # pragma: no cover - checkout without the bridge
        pytest.skip("bridge source not present in this checkout")
    body = src.read_text(encoding="utf-8").split("class VehicleRank", 1)
    assert len(body) == 2, "VehicleRank is no longer in TaskEngine.cs"
    cases = re.findall(r"case VehicleClass\.(\w+):\s*return (\d+);", body[1])
    assert cases, "no ranked classes found in VehicleRank"
    for name, rank in cases:
        assert vehicle_rank(name) == int(rank), f"{name}: bridge says {rank}"
    assert set(VEHICLE_RANK) == {n.lower() for n, _ in cases}
    # Everything else is the switch's `default: return 0` arm — which is why a
    # police car (class Emergency) can never be reached by `prefer: "nicer"`.
    assert vehicle_rank("Emergency") == 0
    assert vehicle_rank(None) == 0


def test_pinned_model_sets_are_recognised_case_and_prefix() -> None:
    assert is_police_model("police2") and is_police_model("SHERIFF")
    # The prefix arm: a member name the pinned set has never heard of.
    assert is_police_model("police7")
    assert not is_police_model("ambulance") and not is_police_model("firetruk")
    assert is_bus_model("coach") and not is_bus_model("bus2")


def test_as_activity_carries_the_goal_s_own_fields() -> None:
    goal = GOALS_BY_ID["steal_nice_car"]
    a = as_activity(goal)
    assert (a.name, a.category, a.brief) == (goal.id, goal.category, goal.description)
    assert a.cooldown_s == goal.cooldown_s and a.chaos_cost == goal.chaos_cost


# --- done_when: fires on a satisfying state, and not otherwise -----------------


def _lock(e: RoamEngine, state: GameState, goal_id: str) -> None:
    """Force one specific goal to be the locked one, off its own real menu."""
    e.observe(state)
    picked = e.pick(state, goal_id=goal_id)
    assert picked is not None and picked[0].goal.id == goal_id, f"{goal_id} was not offered"


def test_steal_nice_car_is_not_done_in_a_worse_car() -> None:
    """The headline bug: `enter_nearest_vehicle` falls back to the nearest usable
    car when nothing outranks, so `in_vehicle` alone completed the goal in a
    WORSE vehicle. It has to be graded on the rank going UP."""
    goal = GOALS_BY_ID["steal_nice_car"]
    snap = {"rank": vehicle_rank("Sedans"), "target_model": "adder"}
    worse = make_state(in_vehicle=True, vehicle={"class": "Compacts", "model": "blista"})
    same = make_state(in_vehicle=True, vehicle={"class": "Sedans", "model": "sultan"})
    better = make_state(in_vehicle=True, vehicle={"class": "Super", "model": "adder"})
    assert not goal.done_when(worse, snap)
    assert not goal.done_when(same, snap)
    assert goal.done_when(better, snap)
    assert not goal.done_when(make_state(), snap)  # on foot


def test_steal_cop_car_is_graded_on_the_model_not_the_class() -> None:
    goal = GOALS_BY_ID["steal_cop_car"]
    assert goal.done_when(
        make_state(in_vehicle=True, vehicle={"class": "Emergency", "model": "police3"}), {}
    )
    assert not goal.done_when(
        make_state(in_vehicle=True, vehicle={"class": "Emergency", "model": "ambulance"}), {}
    )


def test_hijack_bus_done_when() -> None:
    goal = GOALS_BY_ID["hijack_bus"]
    assert goal.done_when(make_state(in_vehicle=True, vehicle={"model": "bus"}), {})
    assert not goal.done_when(make_state(in_vehicle=True, vehicle={"model": "sultan"}), {})


def test_drive_to_landmark_done_when_is_planar_arrival() -> None:
    goal = GOALS_BY_ID["drive_to_landmark"]
    snap = {"target": (100.0, 100.0, 30.0)}
    # Inside the 12 m arrival radius in XY, 80 m below it in Z: the bridge's own
    # arrival rule is planar, so a freeway overpass overhead is still "there".
    assert goal.done_when(make_state(pos=(105.0, 100.0, -50.0)), snap)
    assert not goal.done_when(make_state(pos=(140.0, 100.0, 30.0)), snap)


def test_freeway_run_done_when_is_displacement_not_a_speed_reading() -> None:
    goal = GOALS_BY_ID["freeway_run"]
    snap = {"start": (0.0, 0.0, 0.0)}
    fast_but_here = make_state(
        pos=(10.0, 0.0, 0.0), in_vehicle=True, vehicle={"class": "Super", "speed": 60.0}
    )
    assert not goal.done_when(fast_but_here, snap), "one lucky speed sample is not a run"
    assert goal.done_when(make_state(pos=(1600.0, 0.0, 0.0)), snap)


def test_earn_two_stars_and_lose_the_cops_are_exact_opposites() -> None:
    assert GOALS_BY_ID["earn_two_stars"].done_when(make_state(wanted=2), {})
    assert not GOALS_BY_ID["earn_two_stars"].done_when(make_state(wanted=1), {})
    assert GOALS_BY_ID["lose_the_cops"].done_when(make_state(wanted=0), {})
    assert not GOALS_BY_ID["lose_the_cops"].done_when(make_state(wanted=1), {})


def test_bike_hills_needs_the_bike_and_the_hill() -> None:
    goal = GOALS_BY_ID["bike_hills"]
    snap = {"target": (0.0, 0.0, 0.0)}
    on_bike_there = make_state(in_vehicle=True, vehicle={"class": "Motorcycles"}, pos=(5.0, 0.0, 0.0))
    in_car_there = make_state(in_vehicle=True, vehicle={"class": "Sports"}, pos=(5.0, 0.0, 0.0))
    on_bike_elsewhere = make_state(
        in_vehicle=True, vehicle={"class": "Motorcycles"}, pos=(900.0, 0.0, 0.0)
    )
    assert goal.done_when(on_bike_there, snap)
    assert not goal.done_when(in_car_there, snap)
    assert not goal.done_when(on_bike_elsewhere, snap)


def test_random_event_and_start_mission_watch_the_mission_flags() -> None:
    assert GOALS_BY_ID["random_event"].done_when(make_state(random_event_active=False), {})
    assert not GOALS_BY_ID["random_event"].done_when(make_state(random_event_active=True), {})
    assert GOALS_BY_ID["start_nearest_mission"].done_when(make_state(mission_active=True), {})
    assert not GOALS_BY_ID["start_nearest_mission"].done_when(make_state(), {})


def test_roam_the_block_done_when_is_real_ground_covered() -> None:
    goal = GOALS_BY_ID["roam_the_block"]
    snap = {"start": (0.0, 0.0, 0.0)}
    assert not goal.done_when(make_state(pos=(50.0, 0.0, 0.0)), snap)
    assert goal.done_when(make_state(pos=(500.0, 0.0, 0.0)), snap)


# --- availability --------------------------------------------------------------


def test_a_goal_whose_needs_fail_is_never_offered() -> None:
    e, _ = engine()
    bare = observed(e, make_state())  # on foot, nothing nearby, no markers
    ids = {o.id for o in e.available(bare)}
    for goal_id in ("steal_nice_car", "steal_cop_car", "hijack_bus", "bike_hills", "freeway_run"):
        assert goal_id not in ids, f"{goal_id} was offered with nothing to do it with"


def test_the_menu_is_never_empty() -> None:
    """The operator's rule: he is never out of ideas, so there is always a legal one."""
    e, _ = engine()
    offers = e.available(observed(e, make_state()))
    assert offers, "available() returned nothing at all"
    # The fallback is on EVERY menu, exempt from cooldown, category alternation
    # and the health gate, so no combination of them can produce a tick whose
    # honest answer is "nothing to do".
    assert offers[-1].id == "roam_the_block"


def test_a_cooldown_holds_and_then_expires() -> None:
    e, clock = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    assert "steal_nice_car" in {o.id for o in e.available(state)}
    e.pick(state, goal_id="steal_nice_car")
    e.close("completed")
    e.observe(state)
    assert "steal_nice_car" not in {o.id for o in e.available(state)}
    # Something in another category runs next, so the alternation rule is not
    # what keeps it off the menu — the cooldown is the thing under test.
    e.pick(state, goal_id="drive_to_landmark")
    e.close("completed")
    e.observe(state)
    assert "steal_nice_car" not in {o.id for o in e.available(state)}, "cooldown not held"
    clock.tick(GOALS_BY_ID["steal_nice_car"].cooldown_s + 1.0)
    e.observe(state)
    assert "steal_nice_car" in {o.id for o in e.available(state)}


def test_the_same_category_is_never_offered_twice_running() -> None:
    e, clock = engine()
    state = observed(
        e,
        make_state(
            nearby_vehicles=[
                veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0)),
                veh(2, "police", "Emergency", 14.0, pos=(14.0, 0.0, 0.0)),
            ]
        ),
    )
    picked = e.pick(state, goal_id="steal_nice_car")
    assert picked is not None
    e.close("completed")
    e.observe(state)
    offers = e.available(state)
    assert "acquisition" not in {o.goal.category for o in offers}, (
        "the category that just ran is still on the menu"
    )
    # ...and it comes back once something else has run.
    e.pick(state, goal_id="drive_to_landmark")
    e.close("completed")
    clock.tick(GOALS_BY_ID["steal_cop_car"].cooldown_s + 1.0)
    e.observe(state)
    assert "acquisition" in {o.goal.category for o in e.available(state)}


def test_low_health_leaves_only_calm_goals_on_the_menu() -> None:
    e, _ = engine()
    hurt = observed(
        e,
        make_state(
            health=int(200 * CALM_HEALTH_FRACTION) - 10,
            nearby_vehicles=[
                veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0)),
                veh(2, "police", "Emergency", 14.0, pos=(14.0, 0.0, 0.0)),
            ],
        ),
    )
    offers = e.available(hurt)
    assert offers, "the menu must never be empty, hurt or not"
    assert all(o.goal.calm for o in offers), [o.id for o in offers]
    assert "need a minute" in {o.why for o in offers}


def test_a_wanted_level_overrides_everything_with_lose_the_cops() -> None:
    e, _ = engine()
    wanted = observed(
        e,
        make_state(
            wanted=3, nearby_vehicles=[veh(1, "adder", "Super", 5.0, pos=(5.0, 0.0, 0.0))]
        ),
    )
    offers = e.available(wanted)
    assert [o.id for o in offers] == ["lose_the_cops"]
    picked = e.pick(wanted, goal_id="steal_nice_car")
    assert picked is not None and picked[0].goal.id == "lose_the_cops", (
        "an unoffered id must not be reachable, even by an explicit request"
    )


def test_a_wanted_level_ends_any_other_locked_goal() -> None:
    e, _ = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    e.pick(state, goal_id="steal_nice_car")
    assert e.judge(observed(e, make_state(wanted=2))) == "wanted_override"


def test_lose_the_cops_is_not_ended_by_the_stars_it_exists_to_lose() -> None:
    e, _ = engine()
    e.pick(observed(e, make_state(wanted=2)), goal_id="lose_the_cops")
    assert e.judge(observed(e, make_state(wanted=2))) is None
    assert e.judge(observed(e, make_state(wanted=0))) == "done"


# --- the story has to move -----------------------------------------------------


def test_six_completed_goals_OFFER_the_job_at_the_top_without_forcing_it() -> None:
    """T7 retune: the goal counter offers; only the wall clock forces.

    It used to force at three, which is how a stream that is supposed to be free
    roam collapsed its whole menu to one goal after three cars.
    """
    e, clock = engine()
    state = observed(e, make_state(starts=[start((400.0, 0.0, 0.0))]))
    for _ in range(GOALS_BEFORE_MISSION):
        assert not e.mission_offered()
        assert not e.mission_forced()
        e.pick(state, goal_id="roam_the_block")
        e.close("completed")
        clock.tick(1.0)
        e.observe(state)
    assert e.mission_offered()
    assert not e.mission_forced(), "completed goals must never collapse the menu"
    offers = [o.id for o in e.available(state)]
    assert offers[0] == "start_nearest_mission", "the job is what he picks"
    assert FALLBACK_GOAL_ID in offers, "never a menu of one goal that cannot move him"
    assert len(offers) > 1, "an OFFER leaves the rest of the menu standing"


def test_a_failed_goal_does_not_count_toward_the_offer() -> None:
    e, clock = engine()
    state = observed(e, make_state(starts=[start((400.0, 0.0, 0.0))]))
    for _ in range(GOALS_BEFORE_MISSION + 2):
        e.pick(state, goal_id="roam_the_block")
        e.close("timeout")
        clock.tick(1.0)
        e.observe(state)
    assert not e.mission_offered(), "a timeout is not an achievement"
    assert not e.mission_forced()


def test_the_wall_clock_is_the_only_thing_that_forces_the_job() -> None:
    e, clock = engine()
    state = observed(e, make_state(starts=[start((400.0, 0.0, 0.0))]))
    clock.tick(ROAM_BEFORE_MISSION_S - 1.0)
    assert not e.mission_forced()
    clock.tick(2.0)
    assert e.mission_forced()
    offers = [o.id for o in e.available(state)]
    assert offers[0] == "start_nearest_mission", "the job is what he picks"
    assert FALLBACK_GOAL_ID in offers, (
        "the forced menu must still carry the fallback: start_nearest_mission posts NOTHING "
        "itself (it hands the trip to DayPlanner), so a menu of one goal that cannot move him "
        "is a man standing in the street until the 420 s timeout"
    )


def test_a_forced_job_falls_through_when_there_is_no_job_he_can_start() -> None:
    """Forcing is not lying: with no marker on the map the menu stays honest."""
    e, clock = engine()
    clock.tick(ROAM_BEFORE_MISSION_S + 1.0)
    state = observed(e, make_state(starts=[]))
    assert e.mission_forced()
    offers = e.available(state)
    assert offers and [o.id for o in offers] != ["start_nearest_mission"]


def test_a_started_mission_resets_both_halves_of_the_deadline() -> None:
    e, clock = engine()
    observed(e, make_state(starts=[start((400.0, 0.0, 0.0))]))
    clock.tick(ROAM_BEFORE_MISSION_S + 1.0)
    assert e.mission_forced()
    e.note_mission_started()
    assert not e.mission_forced()


def test_start_nearest_mission_is_not_offered_below_the_health_floor() -> None:
    """The day planner refuses to start a job below half health; promising one
    the planner will then refuse is a plan the show cannot keep."""
    e, clock = engine()
    clock.tick(ROAM_BEFORE_MISSION_S + 1.0)
    hurt = observed(e, make_state(health=60, starts=[start((400.0, 0.0, 0.0))]))
    assert "start_nearest_mission" not in {o.id for o in e.available(hurt)}


# --- the lock ------------------------------------------------------------------


def test_a_locked_goal_cannot_be_switched_mid_flight() -> None:
    e, _ = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    first = e.pick(state, goal_id="steal_nice_car")
    assert first is not None
    assert e.pick(state, goal_id="drive_to_landmark") is None
    assert e.current is not None and e.current.goal.id == "steal_nice_car"


def test_a_model_goal_naming_a_different_id_is_ignored() -> None:
    e, _ = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    offered = {o.id for o in e.available(state)}
    assert "steal_nice_car" in offered
    # On the menu -> honoured.
    assert e.model_choice("goal_id: steal_nice_car, that thing is quick") == "steal_nice_car"
    # Not on the menu -> None, and the engine picks for itself.
    assert e.model_choice("rob_store because I am bored") is None
    assert e.model_choice("just vibe for a while") is None
    # Two at once is not a choice.
    assert e.model_choice("steal_nice_car or maybe roam_the_block") is None
    assert e.model_choice(None) is None


def test_the_model_cannot_reach_a_goal_that_was_never_offered() -> None:
    e, _ = engine()
    bare = observed(e, make_state())
    e.available(bare)
    assert e.model_choice("steal_cop_car") is None
    picked = e.pick(bare, goal_id="steal_cop_car")
    assert picked is None or picked[0].goal.id != "steal_cop_car"


def test_a_foreign_bridge_task_is_dropped_while_a_goal_is_locked() -> None:
    e, _ = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    assert not e.blocks_foreign_action("drive_to", "anything"), "nothing locked, nothing blocked"
    e.pick(state, goal_id="steal_nice_car")
    assert e.blocks_foreign_action("drive_to", "go to the pier instead")
    assert e.blocks_foreign_action("wander_drive", "")
    # ...unless it IS the goal.
    assert not e.blocks_foreign_action("walk_to", "working on steal_nice_car")
    # Primitives are never blocked: they keep the commentary alive and move nothing.
    for primitive in ("radio", "horn", "look_around", "wait", "press_prompt_key"):
        assert primitive not in BRIDGE_TASKS
        assert not e.blocks_foreign_action(primitive, "something else entirely")


def test_the_dashboard_goal_comes_from_the_lock_not_the_model() -> None:
    e, _ = engine()
    assert e.dashboard_goal() is None
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    e.pick(state, goal_id="steal_nice_car")
    assert e.dashboard_goal() == GOALS_BY_ID["steal_nice_car"].description
    e.close("completed")
    assert e.dashboard_goal() is None


# --- timeouts, stalls and the boredom floor -----------------------------------


def test_a_timeout_fails_the_goal_and_a_new_one_can_be_picked() -> None:
    e, clock = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    e.pick(state, goal_id="steal_nice_car")
    goal = GOALS_BY_ID["steal_nice_car"]
    # Ticked at the poll cadence with him MOVING throughout, so the only thing
    # that can end this goal is its own clock.
    while clock.t < 1000.0 + goal.timeout_s - 1.0:
        clock.tick(1.0)
        moving = make_state(pos=(clock.t * 5.0, 0.0, 0.0))
        e.observe(moving)
        assert e.judge(moving) is None
    clock.tick(2.0)
    last = make_state(pos=(clock.t * 5.0, 0.0, 0.0))
    e.observe(last)
    assert e.judge(last) == "timeout"
    payload = e.close("timeout")
    assert payload["goal_id"] == "steal_nice_car" and payload["verified"] is False
    assert e.current is None
    clock.tick(60.0)
    assert e.pick(observed(e, state)) is not None, "he must be free to pick again"


def test_the_in_goal_stuck_watchdog_escalates_once_then_fails() -> None:
    e, clock = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    e.pick(state, goal_id="steal_nice_car")
    e.observe(state)
    for expected in ["escalate"] * (GOAL_STUCK_STRIKES - 1) + ["stuck"]:
        clock.tick(GOAL_STUCK_S + 0.1)
        e.observe(state)  # same position every time: he has not moved
        assert e.judge(state) == expected
    assert GOAL_STUCK_S < BOREDOM_S, "the in-goal watchdog must bite before boredom does"


def test_movement_resets_the_stuck_watchdog() -> None:
    e, clock = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    e.pick(state, goal_id="steal_nice_car")
    for i in range(1, 8):
        clock.tick(GOAL_STUCK_S * 0.5)
        moving = make_state(pos=(i * 20.0, 0.0, 0.0))
        e.observe(moving)
        assert e.judge(moving) is None, "a moving goal must never be called stuck"


def test_he_cannot_be_left_idle_past_the_boredom_threshold() -> None:
    """The operator's bar is "don't stand even for 30 seconds". `due()` normally
    honours a short jittered beat between goals; standing still cancels it."""
    e, clock = engine()
    assert BOREDOM_S < 30.0, "the threshold must sit under the operator's 30 s bar"
    state = make_state()
    e.observe(state)
    e.pick(state, goal_id="roam_the_block")
    e.close("completed")
    # A beat was booked. He is inside it and moving, so nothing is due.
    for i in range(1, 4):
        clock.tick(1.0)
        e.observe(make_state(pos=(i * 50.0, 0.0, 0.0)))
    assert not e.due()
    assert not e.bored()
    # Now he stops dead. Past the threshold, `due` ignores its own beat.
    parked = make_state(pos=(150.0, 0.0, 0.0))
    for _ in range(3):
        clock.tick((BOREDOM_S + 1.0) / 3.0)
        e.observe(parked)
    assert e.bored()
    state = parked
    assert e.due(), "standing still must never be waited out"
    assert e.pick(state) is not None, "and a goal must be pickable right then"


def test_the_beat_between_goals_is_short_enough_to_be_invisible() -> None:
    e, clock = engine()
    state = observed(e, make_state())
    e.pick(state, goal_id="roam_the_block")
    e.close("completed")
    for i in range(1, 5):  # moving, so boredom is never the reason it is due
        clock.tick(BOREDOM_S / 4.0)
        e.observe(make_state(pos=(i * 100.0, 0.0, 0.0)))
    assert not e.bored()
    assert e.due(), "the jittered gap must expire well inside the boredom window"


def test_a_plan_that_ran_out_can_be_rebuilt_once_and_then_no_more() -> None:
    e, _ = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    e.pick(state, goal_id="steal_nice_car")
    assert e.replan(state) is not None
    assert e.replan(state) is None, "one honest retry, not stubbornness"


def test_a_replan_does_not_move_the_goalposts() -> None:
    """`steal_nice_car` must stay graded against the rank he had when he decided
    to upgrade, or a replan after he is already in a better car would let the
    goal complete on the car it was supposed to be an upgrade FROM."""
    e, _ = engine()
    in_sedan = observed(
        e,
        make_state(
            in_vehicle=True,
            vehicle={"class": "Sedans"},
            nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))],
        ),
    )
    e.pick(in_sedan, goal_id="steal_nice_car")
    assert e.current is not None
    original = e.current.snapshot["rank"]
    in_super = observed(
        e,
        make_state(
            in_vehicle=True,
            vehicle={"class": "Super"},
            nearby_vehicles=[veh(2, "zentorno", "Super", 12.0, pos=(12.0, 0.0, 0.0))],
        ),
    )
    e.replan(in_super)
    assert e.current.snapshot["rank"] == original


# --- transitions and the brain's one line -------------------------------------


def test_the_brain_gets_exactly_one_line_per_transition() -> None:
    e, _ = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    e.pick(state, goal_id="steal_nice_car")
    first = e.note()
    assert "ROAM GOAL PICKED: steal_nice_car" in first
    assert "ROAM GOAL PICKED" not in e.note(), "a transition must be announced once"
    assert "ROAM CURRENT: steal_nice_car" in e.note()
    e.close("completed")
    assert "ROAM GOAL DONE: steal_nice_car" in e.note()
    e.observe(state)
    e.available(state)
    menu = e.note()
    assert "ROAM AVAILABLE" in menu and 'Set your "goal" field to EXACTLY ONE of:' in menu


def test_the_offer_list_carries_a_quotable_why_for_every_entry() -> None:
    e, _ = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    for offer in e.available(state):
        assert offer.why and len(offer.why.split()) <= 8, offer


# --- opportunistic triggers ----------------------------------------------------


def test_a_nice_car_nearby_goes_to_the_top_with_its_own_line() -> None:
    e, _ = engine()
    state = observed(
        e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 30.0, pos=(30.0, 0.0, 0.0))])
    )
    offers = e.available(state)
    assert offers[0].id == "steal_nice_car"
    assert offers[0].why == "that's a nice car" and offers[0].triggered


def test_an_unattended_cop_car_outranks_an_ordinary_upgrade() -> None:
    e, _ = engine()
    state = observed(
        e,
        make_state(
            nearby_vehicles=[
                veh(1, "adder", "Super", 30.0, pos=(30.0, 0.0, 0.0)),
                veh(2, "police2", "Emergency", 20.0, pos=(20.0, 0.0, 0.0)),
            ]
        ),
    )
    offers = e.available(state)
    assert offers[0].id == "steal_cop_car" and offers[0].why == "unattended"


def test_a_cop_car_with_a_cop_in_it_is_not_unattended() -> None:
    e, _ = engine()
    state = observed(
        e,
        make_state(
            nearby_vehicles=[veh(2, "police2", "Emergency", 20.0, "npc", pos=(20.0, 0.0, 0.0))]
        ),
    )
    assert "steal_cop_car" not in {o.id for o in e.available(state)}


def test_a_moving_bus_is_not_offered_and_a_stopped_one_is() -> None:
    """`nearby.vehicles[]` has no per-vehicle speed, so "stopped" is derived by
    diffing `pos` per handle across ticks. A bus in service is moving, and
    `walk_to` would chase a position that is already stale."""
    e, clock = engine()
    for x in (0.0, 40.0, 80.0):  # the bus is driving past
        clock.tick(2.0)
        moving = make_state(nearby_vehicles=[veh(3, "bus", "Service", 25.0, pos=(x, 0.0, 0.0))])
        e.observe(moving)
    assert "hijack_bus" not in {o.id for o in e.available(moving)}
    for _ in range(3):  # now it is at a stop
        clock.tick(2.0)
        parked = make_state(nearby_vehicles=[veh(3, "bus", "Service", 25.0, pos=(80.0, 0.0, 0.0))])
        e.observe(parked)
    offers = e.available(parked)
    assert offers[0].id == "hijack_bus" and offers[0].why == "public transport"


def test_being_pulled_out_of_his_own_car_is_told_apart_from_getting_out() -> None:
    e, clock = engine()
    seated = make_state(in_vehicle=True, vehicle={"handle": 42, "class": "Sports"})
    e.observe(seated)
    clock.tick(1.0)
    # He is on foot and handle 42 now has an NPC in it, and WE issued no exit.
    jacked = make_state(
        nearby_vehicles=[veh(42, "banshee", "Sports", 4.0, "npc", pos=(4.0, 0.0, 0.0))]
    )
    e.observe(jacked)
    offers = e.available(jacked)
    assert offers[0].id == "take_my_car_back" and offers[0].why == "that's my car"
    # ...and it is graded on getting THAT car back, not just any car.
    goal = GOALS_BY_ID["take_my_car_back"]
    assert goal.done_when(make_state(in_vehicle=True, vehicle={"handle": 42}), {"handle": 42})
    assert not goal.done_when(make_state(in_vehicle=True, vehicle={"handle": 7}), {"handle": 42})

    # Same shape, but the dismount was ours: no grievance.
    e2, clock2 = engine()
    e2.observe(seated)
    e2.note_self_exit()
    clock2.tick(1.0)
    e2.observe(jacked)
    assert e2.available(jacked)[0].why != "that's my car"


def test_the_open_road_trigger_needs_both_the_car_and_the_speed() -> None:
    e, _ = engine()
    slow = observed(
        e, make_state(in_vehicle=True, vehicle={"class": "Sports", "speed": 5.0})
    )
    # The goal is offerable (he has the car) but nothing PROVOKED it.
    slow_offers = {o.id: o for o in e.available(slow)}
    assert "freeway_run" in slow_offers
    assert not slow_offers["freeway_run"].triggered
    assert "open road" not in {o.why for o in slow_offers.values()}
    fast = observed(
        e, make_state(in_vehicle=True, vehicle={"class": "Sports", "speed": 35.0})
    )
    offers = e.available(fast)
    assert offers[0].id == "freeway_run" and offers[0].why == "open road"
    assert offers[0].triggered


def test_a_live_random_event_is_offered_the_moment_it_is_visible() -> None:
    e, _ = engine()
    state = observed(e, make_state(random_event_active=True, nearby_peds=[ped()]))
    offers = e.available(state)
    assert offers[0].id == "random_event" and offers[0].why == "something's happening"


# --- getting out of a house ----------------------------------------------------


def _pinned_indoors(**over: Any) -> GameState:
    """The composite indoor tell: vehicle entry failed with cars in sight."""
    over.setdefault(
        "nearby_vehicles", [veh(1, "sultan", "Sedans", 22.0, pos=(20.0, 12.0, 0.0))]
    )
    return make_state(
        task_type="enter_nearest_vehicle",
        task_status="failed",
        task_detail="timeout",
        **over,
    )


def test_the_indoor_tell_needs_both_halves() -> None:
    esc = HouseEscape(clock=FakeClock())
    state = _pinned_indoors()
    assert not esc.looks_indoors(state, still_for_s=1.0), "moving is not pinned"
    assert esc.looks_indoors(state, still_for_s=INDOOR_STILL_S + 1.0)
    # A task that simply finished is not evidence of anything.
    assert not esc.looks_indoors(
        make_state(nearby_vehicles=[veh(1, pos=(20.0, 0.0, 0.0))], task_status="done"),
        still_for_s=999.0,
    )
    # Nor is a failure with nothing in sight to have failed to reach.
    assert not esc.looks_indoors(_pinned_indoors(nearby_vehicles=[]), still_for_s=999.0)
    # And never while he is in a car.
    assert not esc.looks_indoors(_pinned_indoors(in_vehicle=True), still_for_s=999.0)


def test_the_escape_ladder_produces_real_actions_and_walks_in_breadcrumbs() -> None:
    clock = FakeClock()
    esc = HouseEscape(clock=clock)
    state = _pinned_indoors()
    actions: list[dict[str, Any]] = []
    for _ in range(MAX_ESCAPE_RUNGS + 2):
        act = esc.check(state, still_for_s=INDOOR_STILL_S + 1.0)
        if act is not None:
            actions.append(act)
        clock.tick(10.0)
    types = [a["type"] for a in actions]
    assert types, "the ladder produced nothing at all"
    assert all(t in ACTION_TYPES for t in types), types
    assert types[0] == "look_around", "the free rung goes first"
    assert "walk_to" in types, "walk_to is the only action that paths through a door"
    assert "press_prompt_key" in types
    assert "enter_nearest_vehicle" in types
    assert len(actions) <= MAX_ESCAPE_RUNGS


def test_the_walk_is_a_breadcrumb_toward_a_car_and_not_the_car_itself() -> None:
    clock = FakeClock()
    esc = HouseEscape(clock=clock)
    far = _pinned_indoors(nearby_vehicles=[veh(1, "sultan", "Sedans", 60.0, pos=(60.0, 0.0, 0.0))])
    walks = []
    for _ in range(MAX_ESCAPE_RUNGS):
        act = esc.check(far, still_for_s=INDOOR_STILL_S + 1.0)
        if act is not None and act["type"] == "walk_to":
            walks.append(act)
        clock.tick(10.0)
    assert walks, "no walk was ever issued"
    first = walks[0]["params"]
    assert first["run"] is True
    assert abs(first["x"] - BREADCRUMB_M) < 0.01, (
        "a single long nav-mesh request out of an interior can fail to generate a "
        "path at all; the walk must go out in breadcrumbs"
    )


def test_the_ladder_stops_and_says_so_rather_than_drifting() -> None:
    clock = FakeClock()
    esc = HouseEscape(clock=clock)
    state = _pinned_indoors()
    for _ in range(MAX_ESCAPE_RUNGS + 5):
        esc.check(state, still_for_s=INDOOR_STILL_S + 1.0)
        clock.tick(10.0)
    assert esc.check(state, still_for_s=999.0) is None
    assert esc.rung >= MAX_ESCAPE_RUNGS


def test_the_ladder_stands_down_the_moment_he_is_moving_again() -> None:
    clock = FakeClock()
    esc = HouseEscape(clock=clock)
    assert esc.check(_pinned_indoors(), still_for_s=INDOOR_STILL_S + 1.0) is not None
    clock.tick(10.0)
    walk = esc.check(_pinned_indoors(), still_for_s=INDOOR_STILL_S + 1.0)
    assert walk is not None and walk["type"] == "walk_to"
    clock.tick(10.0)
    # He is out on the pavement now.
    assert esc.check(_pinned_indoors(pos=(30.0, 30.0, 0.0)), still_for_s=1.0) is None
    assert esc.rung == 0, "the ladder resets once it worked"


def test_the_ladder_never_runs_while_he_is_in_a_car() -> None:
    esc = HouseEscape(clock=FakeClock())
    assert esc.check(_pinned_indoors(in_vehicle=True), still_for_s=999.0) is None


# --- the day plan's ideas are real goals --------------------------------------


def test_every_pickable_goal_id_is_a_real_catalog_entry() -> None:
    assert set(PICKABLE_GOAL_IDS) <= {g.id for g in CATALOG}
    for goal_id in PICKABLE_GOAL_IDS:
        assert not GOALS_BY_ID[goal_id].override_only
        assert not GOALS_BY_ID[goal_id].fallback


def test_earn_two_stars_survives_its_own_first_star() -> None:
    """The wanted override fires on `wanted > 0` — a state this goal deliberately
    creates. Without the exemption it is killed at ONE star and can never reach its
    own done_when of two: a goal that can never complete, shipped in the catalog."""
    e, clock = engine()
    state = observed(e, make_state(in_vehicle=True, health=200))
    e.pick(state, goal_id="earn_two_stars")
    clock.tick(2.0)
    one_star = observed(e, make_state(in_vehicle=True, health=200, wanted=1))
    assert e.judge(one_star) != "wanted_override", "one star is progress, not a reason to quit"
    two = observed(e, make_state(in_vehicle=True, health=200, wanted=2))
    assert e.judge(two) == "done"


def test_every_other_goal_still_yields_to_the_cops() -> None:
    e, clock = engine()
    state = observed(e, make_state(in_vehicle=True, health=200))
    e.pick(state, goal_id="roam_the_block")
    clock.tick(2.0)
    hot = observed(e, make_state(in_vehicle=True, health=200, wanted=1))
    assert e.judge(hot) == "wanted_override"


def test_the_fallback_walks_when_there_is_no_car_to_take() -> None:
    """The goal that guarantees "never stand still" used to post
    enter_nearest_vehicle then wander_drive. On foot with no car in reach the entry
    fails, wander_drive needs a vehicle, and the floor left him standing still."""
    e, _ = engine()
    state = observed(e, make_state(in_vehicle=False, nearby_vehicles=[]))
    picked = e.pick(state, goal_id="roam_the_block")
    assert picked is not None, "the floor must always produce an action"
    _locked, step = picked
    assert step["type"] == "walk_to", f"on foot with no car he walks; got {step}"


def test_steal_cop_car_survives_the_star_it_earns() -> None:
    """Sitting in a police car reliably earns a star, so the wanted-override would
    kill this goal at the moment it starts working — the same never-completes bug
    earn_two_stars had, in a goal where heat is a consequence rather than the aim."""
    e, clock = engine()
    cop = veh(1, "police", "Emergency", 6.0, "empty", pos=(6.0, 0.0, 0.0))
    state = observed(e, make_state(in_vehicle=False, nearby_vehicles=[cop]))
    e.pick(state, goal_id="steal_cop_car")
    clock.tick(2.0)
    hot = observed(e, make_state(in_vehicle=False, nearby_vehicles=[cop], wanted=1))
    assert e.judge(hot) != "wanted_override", "the star it just earned is not a reason to quit"


# --- pick_a_fight / gang_trouble ---------------------------------------------
# Both were in UNBUILDABLE_GOALS until `fight_ped{handle}` landed: nothing could
# start violence against a peaceful ped. TaskEngine.StartFightPed tasks combat
# against any NAMED ped regardless of relationship, so both now ship.


def _ped(handle: int, model: str, distance: float, rel: str = "neutral") -> dict[str, Any]:
    return {
        "handle": handle, "model": model, "distance": distance,
        "relationship": rel, "pos": {"x": distance, "y": 0.0, "z": 0.0},
    }


def test_he_starts_on_the_nearest_stranger() -> None:
    e, _ = engine()
    state = observed(e, make_state(
        in_vehicle=False, health=200,
        nearby_peds=[_ped(40, "a_m_y_hipster_01", 6.0), _ped(41, "a_f_y_tourist_01", 18.0)],
    ))
    picked = e.pick(state, goal_id="pick_a_fight")
    assert picked is not None
    _locked, step = picked
    assert step["type"] in ("walk_to", "fight_ped")


def test_story_characters_and_cops_are_never_marks() -> None:
    """Killing Lamar ends the story the channel is built on; starting on a cop
    turns a bit of fun into a wanted level the engine then spends a goal escaping."""
    e, _ = engine()
    for model in ("ig_lamardavis", "s_m_y_cop_01", "player_zero", "ig_simeon"):
        state = observed(e, make_state(
            in_vehicle=False, health=200, nearby_peds=[_ped(50, model, 4.0)]
        ))
        assert "pick_a_fight" not in [o.id for o in e.available(state)], model


def test_he_does_not_start_fights_on_low_health() -> None:
    e, _ = engine()
    state = observed(e, make_state(
        in_vehicle=False, health=50,
        nearby_peds=[_ped(40, "a_m_y_hipster_01", 5.0)],
    ))
    assert "pick_a_fight" not in [o.id for o in e.available(state)]


def test_an_already_hostile_ped_is_retaliation_not_a_picked_fight() -> None:
    """Fighting back is the threat reflex's job and it does it without a model
    call. This goal is only ever about the agent starting it."""
    e, _ = engine()
    state = observed(e, make_state(
        in_vehicle=False, health=200,
        nearby_peds=[_ped(40, "a_m_y_hipster_01", 5.0, rel="hostile")],
    ))
    assert "pick_a_fight" not in [o.id for o in e.available(state)]


def test_gang_trouble_needs_a_gang_not_one_man() -> None:
    e, _ = engine()
    one = observed(e, make_state(
        in_vehicle=False, health=200, nearby_peds=[_ped(60, "g_m_y_ballasout_01", 15.0)]
    ))
    assert "gang_trouble" not in [o.id for o in e.available(one)]
    crew = observed(e, make_state(
        in_vehicle=False, health=200,
        nearby_peds=[_ped(60, "g_m_y_ballasout_01", 15.0), _ped(61, "g_m_y_famca_01", 18.0)],
    ))
    assert "gang_trouble" in [o.id for o in e.available(crew)]


def test_gang_trouble_ends_when_he_is_losing() -> None:
    e, _ = engine()
    state = observed(e, make_state(
        in_vehicle=False, health=200,
        nearby_peds=[_ped(60, "g_m_y_ballasout_01", 15.0), _ped(61, "g_m_y_famca_01", 18.0)],
    ))
    e.pick(state, goal_id="gang_trouble")
    hurt = observed(e, make_state(
        in_vehicle=False, health=30,
        nearby_peds=[_ped(60, "g_m_y_ballasout_01", 8.0, rel="hostile")],
    ))
    assert e.judge(hurt) == "done", "he leaves before the survival ladder has to drag him out"


def test_a_live_roam_goal_is_not_stranded() -> None:
    """Watched on stream 2026-09-03. `stranded` is reflex-class, so it outranked
    roam and preempted every goal ~2 s after it was picked:

        roam goal picked   goal=roam_the_block
        wheel preempted    owner=roam by=stranded
        roam goal ended    outcome=preempted duration_s=0.3

    including `roam_the_block`, whose own plan IS `enter_nearest_vehicle` — it
    preempted a goal to do the thing that goal was already doing, forever. A man
    walking to a fight is not stranded; the goal owns getting him there.
    """
    import inspect

    from wasted_harness import main as main_mod

    src = inspect.getsource(main_mod.Harness._reflex)
    assert "self.roam.current is not None" in src, (
        "the stranded ladder must stand down while a roam goal is live"
    )
    guard = src.index("self.roam.current is not None")
    reset = src.index("self.stranded.reset()", guard)
    strand_check = src.index("self.stranded.check(state)")
    assert reset < strand_check, "the reset must come BEFORE the stranded check"


# --- missions switched off (Settings.missions_enabled) --------------------------
# Operator call 2026-09-03: "maybe not do a mission cos dont think he is ready
# yet". Off means the job is never offered, never forced, and never scheduled —
# free roam is the whole show until it is switched back on.


def test_with_missions_off_the_job_is_never_offered_even_when_a_marker_is_right_there() -> None:
    e = RoamEngine(random.Random(1), clock=FakeClock(), missions_enabled=False)
    state = observed(e, make_state(starts=[start((30.0, 0.0, 0.0))]))
    assert "start_nearest_mission" not in [o.id for o in e.available(state)]


def test_with_missions_off_three_goals_do_not_force_the_job() -> None:
    clock = FakeClock()
    e = RoamEngine(random.Random(1), clock=clock, missions_enabled=False)
    state = observed(e, make_state(starts=[start((300.0, 0.0, 0.0))]))
    for _ in range(GOALS_BEFORE_MISSION + 2):
        e.pick(state, goal_id="roam_the_block")
        e.close("done")
        clock.tick(1.0)
        e.observe(state)
    assert not e.mission_forced(), "nothing forces a job while missions are off"
    assert "start_nearest_mission" not in [o.id for o in e.available(state)]


def test_with_missions_off_fifteen_minutes_do_not_force_the_job_either() -> None:
    clock = FakeClock()
    e = RoamEngine(random.Random(1), clock=clock, missions_enabled=False)
    state = observed(e, make_state(starts=[start((300.0, 0.0, 0.0))]))
    clock.tick(ROAM_BEFORE_MISSION_S + 60.0)
    assert not e.mission_forced()
    offers = [o.id for o in e.available(state)]
    assert offers and "start_nearest_mission" not in offers, "the menu stays alive without the job"


def test_the_planner_never_schedules_or_requests_a_mission_when_off() -> None:
    from wasted_harness.behavior.planner import DayPlanner

    p = DayPlanner(random.Random(1), missions_enabled=False)
    assert not p._overdue_for_a_mission(10**9), "never overdue for something switched off"
    p.request_mission_block("let's get paid")
    assert not p.in_mission_block, "an explicit request is refused too"



def test_the_menu_tells_him_the_field_and_the_exact_value() -> None:
    """Watched on stream after deploy: every pick logged goal_fallback with prose in
    the goal field ("cruise around, find a bike, aim for a hill"). The softer
    "pick ONE by id" was not enough; the line has to name the FIELD and show the
    bare ids in the shape he must return."""
    e, _ = engine()
    state = observed(e, make_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))]))
    e.available(state)
    note = e.note()
    assert 'Set your "goal" field to EXACTLY ONE of:' in note
    for o in e._offers:
        assert o.id in note
    assert "bare id, no sentence" in note


def test_a_locked_goal_names_the_exact_value_to_return() -> None:
    e, _ = engine()
    state = observed(e, make_state())
    e.pick(state, goal_id="roam_the_block")
    e.note()  # drains the transition line
    note = e.note()
    assert 'Set your "goal" field to exactly: roam_the_block' in note


# --- novelty: "make sure no same pattern is followed" ---------------------------


def _car_state(**kw):
    kw.setdefault("in_vehicle", True)
    kw.setdefault("health", 200)
    kw.setdefault("vehicle", {"class": "Sports", "model": "banshee", "speed": 5.0})
    return make_state(**kw)


def test_the_same_goal_is_never_offered_twice_running() -> None:
    e, clock = engine()
    state = observed(e, _car_state())
    e.pick(state, goal_id="drive_to_landmark")
    e.close("done")
    clock.tick(15.0)
    state = observed(e, state)
    assert "drive_to_landmark" not in [o.id for o in e.available(state)], "just did that"


def test_recent_goals_sink_to_the_back_of_the_menu() -> None:
    e, clock = engine()
    state = observed(e, _car_state())
    first = [o.id for o in e.available(state) if not o.triggered]
    assert len(first) >= 3, first
    picked = first[1]
    e.pick(state, goal_id=picked)
    e.close("done")
    clock.tick(15.0)
    later = [o.id for o in e.available(observed(e, state))]
    if picked in later:
        assert later.index(picked) >= len(later) - 2, f"{picked} should be near the back: {later}"


def test_chase_that_car_wants_a_moving_nice_car_and_ends_when_it_is_gone() -> None:
    e, _ = engine()
    car = veh(9, "adder", "Super", 25.0, driver="npc", pos=(25.0, 0.0, 0.0))
    state = observed(e, _car_state(vehicle={"class": "Sports", "model": "banshee", "speed": 20.0},
                                   nearby_vehicles=[car]))
    picked = e.pick(state, goal_id="chase_that_car")
    assert picked is not None
    _, step = picked
    assert step["type"] == "follow_entity" and step["params"]["handle"] == 9
    gone = observed(e, _car_state(vehicle={"class": "Sports", "model": "banshee", "speed": 20.0},
                                  nearby_vehicles=[]))
    assert e.judge(gone) == "done"


def test_jack_a_driver_is_done_only_in_that_car() -> None:
    e, _ = engine()
    taxi = veh(5, "taxi", "Sedans", 6.0, driver="npc", pos=(6.0, 0.0, 0.0))
    state = observed(e, make_state(in_vehicle=False, health=200, nearby_vehicles=[taxi]))
    assert e.pick(state, goal_id="jack_a_driver") is not None
    wrong = observed(e, _car_state(vehicle={"handle": 77, "class": "Sedans", "model": "prairie", "speed": 3.0}))
    assert e.judge(wrong) != "done", "a different car is not the jack"
    right = observed(e, _car_state(vehicle={"handle": 5, "class": "Sedans", "model": "taxi", "speed": 3.0}))
    assert e.judge(right) == "done"


def test_honk_run_uses_the_horn_primitive_and_ends_on_distance() -> None:
    e, _ = engine()
    state = observed(e, _car_state(pos=(0.0, 0.0, 0.0), vehicle={"class": "Sedans", "model": "prairie", "speed": 10.0}))
    picked = e.pick(state, goal_id="honk_run")
    assert picked is not None
    locked, first = picked
    assert first["type"] == "wander_drive"
    assert any(s["type"] == "horn" for s in locked.plan)
    far = observed(e, _car_state(pos=(400.0, 0.0, 0.0), vehicle={"class": "Sedans", "model": "prairie", "speed": 10.0}))
    assert e.judge(far) == "done"


# --- T7: the chaos ladder, the tiers, the cadence -------------------------------
#
# fix-opus-b. Every test below drives the public surface (`observe`, `available`,
# `pick`, `judge`, `close`) with states from the builders at the top of this
# file — no private attribute is poked except the FakeClock, which is the
# injected clock and is meant to be driven.


def _weapon(
    name: str = "Pistol",
    weapon_class: str = "gun",
    ammo: int = 60,
    owned: dict[str, int] | None = None,
    loadout: str = "off",
) -> dict[str, Any]:
    """A bridge-1.7.0 `player.weapon` object, shaped exactly like the DTO."""
    return {
        "name": name,
        "class": weapon_class,
        "ammo": ammo,
        "owned": {"Pistol": 60} if owned is None else owned,
        "loadout": loadout,
    }


def armed_state(**over: Any) -> GameState:
    """`make_state` plus the 1.7.0 fields, applied after validation-shaped merge.

    `make_state` builds the body from the pre-1.7.0 contract, so the new keys are
    grafted on here rather than threaded through it — that keeps every existing
    test in this file testing exactly what it tested before, including the
    "a pre-1.7.0 bridge omits the key" path the new goals gate on.
    """
    weapon = over.pop("weapon", _weapon())
    in_air = over.pop("in_air", None)
    seat = over.pop("seat", None)
    state = make_state(**over)
    body = state.model_dump(by_alias=True)
    body["player"]["weapon"] = weapon
    if body.get("vehicle") is not None:
        if in_air is not None:
            body["vehicle"]["in_air"] = in_air
        if seat is not None:
            body["vehicle"]["seat"] = seat
    return GameState.model_validate(body)


def test_the_1_7_0_fields_are_only_read_when_the_bridge_actually_sent_them() -> None:
    """A pre-1.7.0 snapshot must read as "cannot tell", never as "unarmed"."""
    old = make_state(in_vehicle=True)
    assert not supports_v17(old)
    assert not weapon_reported(old)
    assert not air_reported(old)
    assert not seat_reported(old)
    new = armed_state(in_vehicle=True, in_air=True, seat="passenger")
    assert supports_v17(new)
    assert weapon_reported(new)
    assert air_reported(new) and in_air(new)
    assert seat_reported(new) and riding_as_passenger(new)
    assert owns_weapon(new, "Pistol") and not owns_weapon(new, "MicroSMG")
    assert weapon_ammo(new, "Pistol") == 60


def test_tier_filters_the_menu_and_nothing_above_it_is_offerable() -> None:
    """L1 sees no trouble; L3 sees everything its state allows."""
    for level in (1, 2, 3):
        clock = FakeClock()
        e = RoamEngine(random.Random(3), clock=clock, level=level)
        state = armed_state(
            in_vehicle=False,
            health=200,
            nearby_peds=[ped(handle=40, model="a_m_y_hipster_01", distance=6.0)],
            weapon=_weapon(owned={"Pistol": 60, "MicroSMG": 90, "PumpShotgun": 24}),
        )
        e.observe(state)
        offers = e.available(state)
        assert offers, "the menu is never empty"
        assert all(o.goal.level <= level for o in offers), (
            level, [(o.id, o.goal.level) for o in offers]
        )
    # And the tier really is the thing doing it: the same state at L3 offers a
    # goal that L1 refused.
    clock = FakeClock()
    e3 = RoamEngine(random.Random(3), clock=clock, level=3)
    state = armed_state(
        in_vehicle=False,
        health=200,
        nearby_peds=[ped(handle=40, model="a_m_y_hipster_01", distance=6.0)],
        weapon=_weapon(owned={"Pistol": 60}),
    )
    e3.observe(state)
    assert "armed_rampage_block" in {o.id for o in e3.available(state)}
    e1 = RoamEngine(random.Random(3), clock=FakeClock(), level=1)
    e1.observe(state)
    assert "armed_rampage_block" not in {o.id for o in e1.available(state)}


def test_seven_deaths_in_an_hour_steps_the_tier_down_and_a_clean_run_gives_it_back() -> None:
    """F5, as arithmetic: the synthetic seven-death hour the ticket asks for."""
    clock = FakeClock()
    e = RoamEngine(random.Random(5), clock=clock, level=3)
    alive = armed_state(in_vehicle=False, nearby_peds=[ped(handle=40, distance=6.0)])
    dead = armed_state(in_vehicle=False, dead=True, nearby_peds=[ped(handle=40, distance=6.0)])
    assert e.level == 3

    levels: list[int] = []
    last_death_at = clock.t
    for _i in range(7):
        e.observe(alive)
        # A death only counts while a goal is LOCKED — dying between goals is
        # nobody's fault and must not move the tier.
        if e.current is None:
            e.pick(alive)
        clock.tick(60.0)
        e.observe(dead)
        e.observe(dead)  # the wasted screen lasts many ticks; the edge fires once
        last_death_at = clock.t
        levels.append(e.level)
        if e.current is not None:
            e.close("player_down")
        clock.tick(30.0)
        e.observe(alive)

    assert e.ladder.deaths_in_window() <= DEATHS_PER_HOUR_STEP_DOWN
    assert e.level == 2, levels
    assert levels[:5] == [3, 3, 3, 3, 3], "no step before the sixth death"
    assert levels[5] == 2, "the sixth death is the one that costs a tier"

    # A clean half hour buys it back, and not a second earlier. Measured from
    # the LAST death, which is when the clean run started.
    clock.t = last_death_at + CLEAN_RECOVERY_S - 1.0
    e.observe(alive)
    assert e.level == 2
    clock.tick(2.0)
    e.observe(alive)
    assert e.level == 3


def test_a_death_between_goals_does_not_cost_a_tier() -> None:
    clock = FakeClock()
    e = RoamEngine(random.Random(5), clock=clock, level=2)
    alive = armed_state()
    dead = armed_state(dead=True)
    for _ in range(10):
        e.observe(alive)
        e.observe(dead)
        clock.tick(5.0)
    assert e.ladder.deaths_in_window() == 0
    assert e.level == 2


def test_heat_goals_stay_on_the_menu_with_stars_up_and_nothing_else_does() -> None:
    clock = FakeClock()
    e = RoamEngine(random.Random(7), clock=clock, level=3)
    hot = armed_state(
        wanted=2,
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 20.0},
        health=200,
        nearby_vehicles=[veh(2, "police", "Emergency", 15.0, pos=(15.0, 0.0, 0.0))],
    )
    e.observe(hot)
    offers = e.available(hot)
    ids = [o.id for o in offers]
    assert ids[0] == "lose_the_cops", ids
    assert "three_star_survival" in ids, ids
    for offer in offers[1:]:
        assert offer.goal.wants_heat, f"{offer.id} should have yielded to the cops"
    # The pre-existing exemptions are still exemptions.
    assert GOALS_BY_ID["steal_cop_car"].wants_heat
    assert GOALS_BY_ID["earn_two_stars"].wants_heat


def test_the_mission_is_offered_after_six_goals_and_forced_only_by_the_clock() -> None:
    clock = FakeClock()
    cadence = RoamCadence(goals_before_offer=6, force_after_s=40 * 60.0)
    e = RoamEngine(random.Random(11), clock=clock, cadence=cadence, level=1)
    # Counted on a state with NO mission markers, so the loop cannot pick
    # `start_nearest_mission` itself — completing THAT resets the counter (it
    # started a mission), which would make the test measure nothing.
    roaming = armed_state(
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 20.0},
    )
    state = armed_state(
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 20.0},
        starts=[start((300.0, 0.0, 0.0))],
    )
    e.observe(roaming)
    assert not e.mission_offered() and not e.mission_forced()

    for i in range(6):
        e.observe(roaming)
        picked = e.pick(roaming)
        assert picked is not None
        e.close("completed")
        clock.tick(1.0)
        # The offer arrives on the sixth completion, not before it.
        assert e.mission_offered() is (i == 5), i
    assert not e.mission_forced(), "six goals must NOT force; only the clock does"

    e.observe(state)
    ids = [o.id for o in e.available(state)]
    assert ids[0] == "start_nearest_mission", ids
    assert len(ids) > 1, "an OFFER leaves the rest of the menu standing"

    # Forty minutes is what collapses it.
    clock.tick(40 * 60.0)
    assert e.mission_forced()
    e.observe(state)
    forced = [o.id for o in e.available(state)]
    assert forced[0] == "start_nearest_mission"
    assert set(forced) <= {"start_nearest_mission", FALLBACK_GOAL_ID}, forced


def test_missions_off_beats_every_cadence_value() -> None:
    clock = FakeClock()
    e = RoamEngine(
        random.Random(11),
        clock=clock,
        missions_enabled=False,
        cadence=RoamCadence(goals_before_offer=1, force_after_s=1.0),
        level=1,
    )
    state = armed_state(in_vehicle=True, starts=[start((300.0, 0.0, 0.0))])
    clock.tick(10 * 60.0)
    e.observe(state)
    assert not e.mission_offered()
    assert not e.mission_forced()
    assert "start_nearest_mission" not in {o.id for o in e.available(state)}


def test_the_cadence_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WASTED_ROAM_GOALS_BEFORE_MISSION", "9")
    monkeypatch.setenv("WASTED_ROAM_MISSION_FORCE_S", "600")
    assert RoamCadence.from_env() == RoamCadence(goals_before_offer=9, force_after_s=600.0)
    monkeypatch.setenv("WASTED_ROAM_GOALS_BEFORE_MISSION", "not a number")
    assert RoamCadence.from_env().goals_before_offer == GOALS_BEFORE_MISSION


def test_the_new_triggers_promote_with_a_why() -> None:
    clock = FakeClock()
    e = RoamEngine(random.Random(13), clock=clock, level=2)
    corner = armed_state(
        in_vehicle=False,
        health=200,
        nearby_peds=[
            ped(handle=60, model="g_m_y_ballasout_01", distance=15.0, pos=(15.0, 0.0, 0.0)),
            ped(handle=61, model="g_m_y_famca_01", distance=18.0, pos=(18.0, 0.0, 0.0)),
        ],
    )
    e.observe(corner)
    top = e.available(corner)[0]
    assert top.id == "gang_trouble", [o.id for o in e.available(corner)]
    assert top.triggered and top.why == "wrong corner, wrong colours"

    ramp = FREEWAY_ONRAMPS[0]["pos"]
    e2 = RoamEngine(random.Random(13), clock=FakeClock(), level=1)
    on_ramp = armed_state(
        pos=(ramp[0] + 20.0, ramp[1], ramp[2]),
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 12.0},
    )
    e2.observe(on_ramp)
    offers = e2.available(on_ramp)
    freeway = next(o for o in offers if o.id == "freeway_run")
    assert freeway.triggered and freeway.why == "there's the on-ramp"
    assert offers[0].id == "freeway_run", [o.id for o in offers]


def test_novelty_memory_still_holds_across_tiers() -> None:
    """Never the same id twice running, and the last four sink."""
    clock = FakeClock()
    e = RoamEngine(random.Random(17), clock=clock, level=3)
    state = armed_state(
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 20.0},
        nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))],
    )
    seen: list[str] = []
    for _ in range(8):
        e.observe(state)
        picked = e.pick(state)
        if picked is None:
            clock.tick(5.0)
            continue
        locked, _first = picked
        seen.append(locked.goal.id)
        # Never offered the same id twice running.
        e.close("completed")
        clock.tick(20.0)
        e.observe(state)
        if not locked.goal.fallback:
            # The fallback is exempt by design — it is the one guaranteed
            # option, so `available()` is never empty and he is never out of
            # ideas. Every other id must be off the next menu.
            assert locked.goal.id not in {o.id for o in e.available(state)}, locked.goal.id
    assert len(seen) >= 4
    for a, b in itertools.pairwise(seen):
        assert a != b, seen


def test_goal_progress_is_folded_and_the_new_done_whens_read_it() -> None:
    """`_held3_s`, `_ammo_spent`, `_from_start_m` — the cross-tick measurements."""
    clock = FakeClock()
    e = RoamEngine(random.Random(19), clock=clock, level=3)
    hot = armed_state(
        wanted=3,
        in_vehicle=True,
        vehicle={"class": "Sedans", "model": "sultan", "speed": 20.0},
        health=200,
    )
    e.observe(hot)
    picked = e.pick(hot, goal_id="three_star_survival")
    assert picked is not None and picked[0].goal.id == "three_star_survival"
    assert e.judge(hot) is None
    clock.tick(HOLD_THREE_S - 1.0)
    e.reset_movement_anchor()
    assert e.judge(hot) is None, "eighty-nine seconds is not ninety"
    clock.tick(2.0)
    e.reset_movement_anchor()
    assert e.judge(hot) == "done"
    assert e.current is not None and e.current.snapshot["_held3_s"] >= HOLD_THREE_S

    # Dropping below three stars restarts the clock rather than banking it.
    e.close("completed")
    clock.tick(60.0)
    e.observe(hot)
    e.pick(hot, goal_id="three_star_survival")
    clock.tick(60.0)
    cooled = armed_state(wanted=1, in_vehicle=True, health=200)
    e.reset_movement_anchor()
    assert e.judge(cooled) is None
    assert e.current is not None and e.current.snapshot["_held3_s"] == 0.0


def test_a_drive_by_that_fired_nothing_never_completes() -> None:
    """The native is UNVERIFIED on a player ped; the goal is built to say so."""
    clock = FakeClock()
    e = RoamEngine(random.Random(23), clock=clock, level=2)
    full = {"Pistol": 60, "MicroSMG": 90}
    at_the_corner = armed_state(
        in_vehicle=True,
        vehicle={"class": "Sedans", "model": "sultan", "speed": 14.0},
        health=200,
        weapon=_weapon(owned=dict(full)),
        nearby_peds=[ped(handle=60, model="g_m_y_ballasout_01", distance=12.0, pos=(12.0, 0.0, 0.0))],
    )
    e.observe(at_the_corner)
    picked = e.pick(at_the_corner, goal_id="drive_by_run")
    assert picked is not None and picked[0].goal.id == "drive_by_run"
    assert picked[1]["type"] == "drive_by"

    # Drove away, fired nothing: NOT done, because nothing happened.
    far_no_shot = armed_state(
        pos=(400.0, 0.0, 0.0),
        in_vehicle=True,
        vehicle={"class": "Sedans", "model": "sultan", "speed": 20.0},
        health=200,
        weapon=_weapon(owned=dict(full)),
    )
    clock.tick(20.0)
    e.reset_movement_anchor()
    assert e.judge(far_no_shot) is None

    # Same distance, rounds gone: done.
    far_shot = armed_state(
        pos=(400.0, 0.0, 0.0),
        in_vehicle=True,
        vehicle={"class": "Sedans", "model": "sultan", "speed": 20.0},
        health=200,
        weapon=_weapon(owned={"Pistol": 60, "MicroSMG": 61}),
    )
    e.reset_movement_anchor()
    assert e.judge(far_shot) == "done"


def test_big_jump_needs_the_airtime_field_and_grades_on_it() -> None:
    clock = FakeClock()
    e = RoamEngine(random.Random(29), clock=clock, level=1)
    driving = armed_state(
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 30.0},
        in_air=False,
    )
    e.observe(driving)
    assert "big_jump" in {o.id for o in e.available(driving)}
    # A bridge that does not send the field must not offer the goal at all.
    old_bridge = make_state(
        in_vehicle=True, vehicle={"class": "Sports", "model": "banshee", "speed": 30.0}
    )
    e_old = RoamEngine(random.Random(29), clock=FakeClock(), level=1)
    e_old.observe(old_bridge)
    assert "big_jump" not in {o.id for o in e_old.available(old_bridge)}

    picked = e.pick(driving, goal_id="big_jump")
    assert picked is not None and picked[0].goal.id == "big_jump"
    # Airborne on the doorstep is a kerb, not a jump.
    kerb = armed_state(
        pos=(10.0, 0.0, 0.0),
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 30.0},
        in_air=True,
    )
    e.reset_movement_anchor()
    assert e.judge(kerb) is None
    airborne = armed_state(
        pos=(400.0, 0.0, 0.0),
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 30.0},
        in_air=True,
    )
    e.reset_movement_anchor()
    assert e.judge(airborne) == "done"


def test_taxi_ride_is_graded_on_the_seat_not_on_being_in_the_car() -> None:
    clock = FakeClock()
    e = RoamEngine(random.Random(31), clock=clock, level=1)
    cab = veh(77, "taxi", "Sedans", 9.0, driver="npc", pos=(9.0, 0.0, 0.0))
    kerb = armed_state(in_vehicle=False, nearby_vehicles=[cab])
    e.observe(kerb)
    clock.tick(2.0)
    e.observe(kerb)  # a second identical snapshot: the cab reads as stopped
    assert "taxi_ride" in {o.id for o in e.available(kerb)}
    picked = e.pick(kerb, goal_id="taxi_ride")
    assert picked is not None
    types = [s["type"] for s in picked[0].plan]
    assert types[0] == "set_waypoint", types
    assert types[-1] == "enter_vehicle_seat", types
    assert picked[0].plan[-1]["params"] == {"handle": 77, "seat": 2}

    # He jacked it instead: in the cab, driving it. NOT a taxi ride.
    jacked = armed_state(
        pos=(500.0, 0.0, 0.0),
        in_vehicle=True,
        vehicle={"handle": 77, "class": "Sedans", "model": "taxi", "speed": 18.0},
        seat="driver",
    )
    clock.tick(30.0)
    e.reset_movement_anchor()
    assert e.judge(jacked) is None
    rode = armed_state(
        pos=(500.0, 0.0, 0.0),
        in_vehicle=True,
        vehicle={"handle": 77, "class": "Sedans", "model": "taxi", "speed": 18.0},
        seat="passenger",
    )
    e.reset_movement_anchor()
    assert e.judge(rode) == "done"


def test_pick_a_fight_is_always_a_fist_fight() -> None:
    """CLAUDE.md-adjacent: a the agent who owns a pistol must not execute a pedestrian."""
    clock = FakeClock()
    e = RoamEngine(random.Random(37), clock=clock, level=2)
    state = armed_state(
        in_vehicle=False,
        health=200,
        weapon=_weapon(name="Pistol", weapon_class="gun", owned={"Pistol": 60}),
        nearby_peds=[ped(handle=40, model="a_m_y_hipster_01", distance=6.0)],
    )
    e.observe(state)
    picked = e.pick(state, goal_id="pick_a_fight")
    assert picked is not None
    fight = next(s for s in picked[0].plan if s["type"] == "fight_ped")
    assert fight["params"]["weapon"] == "unarmed", fight
    # ...and the armed goal asks for the other one.
    e.close("completed")
    clock.tick(30.0)
    e3 = RoamEngine(random.Random(37), clock=FakeClock(), level=3)
    e3.observe(state)
    rampage = e3.pick(state, goal_id="armed_rampage_block")
    assert rampage is not None
    assert rampage[0].plan[0]["params"]["weapon"] == "armed"


def test_every_new_goal_is_offerable_in_some_state_and_plans_legal_actions() -> None:
    """The T7 half of the catalog-wide guarantee, with 1.7.0-shaped snapshots."""
    full = {"Pistol": 60, "MicroSMG": 90, "PumpShotgun": 24}
    gang_ped = ped(handle=60, model="g_m_y_ballasout_01", distance=12.0, pos=(12.0, 0.0, 0.0))
    cases: dict[str, GameState] = {
        "big_jump": armed_state(
            in_vehicle=True,
            vehicle={"class": "Sports", "model": "banshee", "speed": 30.0},
            in_air=False,
        ),
        "drive_by_run": armed_state(
            in_vehicle=True,
            vehicle={"class": "Sedans", "model": "sultan", "speed": 14.0},
            health=200,
            weapon=_weapon(owned=dict(full)),
            nearby_peds=[gang_ped],
        ),
        "three_star_survival": armed_state(
            wanted=3,
            in_vehicle=True,
            vehicle={"class": "Sedans", "model": "sultan", "speed": 20.0},
            health=200,
        ),
        "armed_rampage_block": armed_state(
            in_vehicle=False,
            health=200,
            weapon=_weapon(owned=dict(full)),
            nearby_peds=[ped(handle=41, model="a_m_y_hipster_01", distance=7.0)],
        ),
        "helicopter_grab": armed_state(
            in_vehicle=False,
            nearby_vehicles=[veh(88, "polmav", "Helicopters", 25.0, pos=(25.0, 0.0, 0.0))],
        ),
    }
    for goal_id, state in cases.items():
        goal = GOALS_BY_ID[goal_id]
        e = RoamEngine(random.Random(41), clock=FakeClock(), level=3)
        e.observe(state)
        assert goal.needs(state, e.view), f"{goal_id} was not offerable"
        plan, _snap = goal.plan(state, e.view)
        assert plan, f"{goal_id} materialised an empty plan"
        for step in plan:
            assert step["type"] in ACTION_TYPES, f"{goal_id}: {step['type']}"
        assert goal_id in {o.id for o in e.available(state)}, goal_id

    # taxi_ride needs two snapshots for the cab to read as stopped.
    clock = FakeClock()
    e = RoamEngine(random.Random(41), clock=clock, level=1)
    kerb = armed_state(
        in_vehicle=False,
        nearby_vehicles=[veh(77, "taxi", "Sedans", 9.0, driver="npc", pos=(9.0, 0.0, 0.0))],
    )
    e.observe(kerb)
    clock.tick(2.0)
    e.observe(kerb)
    assert "taxi_ride" in {o.id for o in e.available(kerb)}
    plan, _ = GOALS_BY_ID["taxi_ride"].plan(kerb, e.view)
    for step in plan:
        assert step["type"] in ACTION_TYPES, step


# --- earn_two_stars provokes before it drives (soak finding, 2026-09-03) -------


def test_earn_two_stars_opens_with_a_drive_by_when_he_has_the_smg() -> None:
    """Measured: the get-in-a-car-and-run-lights plan sat at zero stars for its whole
    180 s timeout — the longest silent stretch in the 12-minute soak. Armed and seated,
    the plan now fires out of the window first (bridge 1.7.0 `drive_by`), then drives."""
    e, _ = engine()
    state = observed(
        e,
        armed_state(
            in_vehicle=True, health=200,
            weapon=_weapon(name="MicroSMG", owned={"Pistol": 60, "MicroSMG": 90}),
            nearby_peds=[ped(handle=62, distance=12.0)],
        ),
    )
    picked = e.pick(state, goal_id="earn_two_stars")
    assert picked is not None
    plan = picked[0].plan
    assert plan[0]["type"] == "drive_by" and plan[0]["params"]["handle"] == 62
    assert plan[-1]["type"] == "wander_drive"


def test_earn_two_stars_opens_with_a_burst_on_foot_when_armed() -> None:
    e, _ = engine()
    state = observed(
        e,
        armed_state(
            in_vehicle=False, health=200,
            weapon=_weapon(owned={"Pistol": 60}),
            nearby_peds=[ped(handle=71, distance=8.0)],
            nearby_vehicles=[veh(5, "taxi", "Sedans", 10.0, driver="npc", pos=(10.0, 0.0, 0.0))],
        ),
    )
    picked = e.pick(state, goal_id="earn_two_stars")
    assert picked is not None
    types = [s["type"] for s in picked[0].plan]
    assert types[0] == "shoot_at" and picked[0].plan[0]["params"]["handle"] == 71
    assert types[-1] == "wander_drive"


def test_earn_two_stars_unarmed_is_the_carjack_it_always_was() -> None:
    """No gun, no invented gun (CLAUDE.md rule 5): the plan is the old one."""
    e, _ = engine()
    state = observed(
        e,
        make_state(
            in_vehicle=False, health=200,
            nearby_peds=[ped(handle=71, distance=8.0)],
            nearby_vehicles=[veh(5, "taxi", "Sedans", 10.0, driver="npc", pos=(10.0, 0.0, 0.0))],
        ),
    )
    picked = e.pick(state, goal_id="earn_two_stars")
    assert picked is not None
    types = [s["type"] for s in picked[0].plan]
    assert "shoot_at" not in types and "drive_by" not in types
    assert types[-1] == "wander_drive"


def test_big_jump_picked_next_to_the_ramp_still_counts_the_jump() -> None:
    """The approach is the nearest one, so he can start 60 m from it: the jump then
    happens under the 100 m run-up bar. Eight seconds in, airborne is the jump."""
    clock = FakeClock()
    e = RoamEngine(random.Random(29), clock=clock, level=1)
    driving = armed_state(
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 30.0},
        in_air=False,
    )
    e.observe(driving)
    assert e.pick(driving, goal_id="big_jump") is not None
    clock.tick(10.0)
    near = armed_state(
        pos=(60.0, 0.0, 0.0),
        in_vehicle=True,
        vehicle={"class": "Sports", "model": "banshee", "speed": 30.0},
        in_air=True,
    )
    e.reset_movement_anchor()
    assert e.judge(near) == "done"


def test_the_chaos_level_can_be_pinned_from_the_environment(monkeypatch) -> None:
    """docs/go-live.md §6: `WASTED_CHAOS_LEVEL` pins the starting tier; the ladder
    still moves itself from there, and nonsense is ignored, not fatal."""
    monkeypatch.setenv("WASTED_CHAOS_LEVEL", "1")
    assert RoamEngine(random.Random(1), clock=FakeClock()).ladder.level == 1
    monkeypatch.setenv("WASTED_CHAOS_LEVEL", "9")
    assert RoamEngine(random.Random(1), clock=FakeClock()).ladder.level == 3
    monkeypatch.setenv("WASTED_CHAOS_LEVEL", "lots")
    assert RoamEngine(random.Random(1), clock=FakeClock()).ladder.level == 2
    monkeypatch.delenv("WASTED_CHAOS_LEVEL")
    assert RoamEngine(random.Random(1), clock=FakeClock()).ladder.level == 2
