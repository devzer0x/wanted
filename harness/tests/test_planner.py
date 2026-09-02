"""DayPlanner: roam blocks, mission blocks, and the decision to go start a job.

The gap under test: nothing in the harness ever decided to go and START a
mission. `mission.starts[]` (CONTRACTS v1.7) is the fact that made the decision
possible; behavior/planner.py is the decision.

**On the inputs.** The `/state` bodies here are constructed pydantic objects
built field by field from CONTRACTS §1 — the same style as
test_mission_following.py, test_governor_l3.py and test_recovery.py, and for the
same reason: `harness/tests/fixtures/` is reserved for recordings of real
sessions (CLAUDE.md rule 1), and no recording of a v1.7 `/state` exists yet
because the bridge has only just gained the fields. These are structural
fixtures — they assert shapes, gating and arithmetic, never that a coordinate
means anything about the real map. What they CANNOT prove is that a corona at
`mission.starts[0].pos` actually starts a mission when the agent stands in it; that
is a live check on the server with the game running.
"""

from __future__ import annotations

import itertools
import random
from typing import Any

from support import throwaway_totals  # noqa: F401  (kept: shared real-store helper)
from test_roam import make_state as roam_state
from test_roam import veh

from wasted_harness.behavior.activities import ActivityPicker, ActivityRunner
from wasted_harness.behavior.humanizer import MoodModel
from wasted_harness.behavior.navigation import (
    VEHICLE_SEARCH_RADIUS_M,
    WALK_ARRIVE_RADIUS_M,
    WALK_SWITCH_RADIUS_M,
    navigate_to,
)
from wasted_harness.behavior.planner import (
    GO_START_A_JOB,
    MARKER_FINAL_APPROACH_M,
    MARKER_WAIT_S,
    MAX_CONSECUTIVE_MISSION_FAILS,
    MIN_HEALTH_FRACTION_TO_START,
    MISSION_BLOCK_TIMEOUT_S,
    MISSION_OVERDUE_S,
    ROAM_BLOCK_S,
    ROAM_IDEAS,
    ROAM_MIN_BLOCK_S,
    Block,
    DayPlanner,
    compass_bearing,
)
from wasted_harness.behavior.roam import (
    BOREDOM_S,
    PICKABLE_GOAL_IDS,
    RoamEngine,
    as_activity,
)
from wasted_harness.behavior.vehicle import MovementWheel
from wasted_harness.brain.schemas import ACTION_TYPES
from wasted_harness.bridge_client import UNKNOWN_PROTAGONIST, GameState
from wasted_harness.main import Harness

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
    """A contract-shaped /state body, including the v1.7 fields. Every field explicit."""
    pos = over.pop("pos", (0.0, 0.0, 0.0))
    in_vehicle = over.pop("in_vehicle", False)
    starts = over.pop("starts", [])
    mission: dict[str, Any] = {
        "active": over.pop("mission_active", False),
        "random_event_active": False,
        "cutscene_active": over.pop("cutscene_active", False),
        "objective_blip": None,
        "starts": [
            {"pos": {"x": s[0][0], "y": s[0][1], "z": s[0][2]}, "protagonist": s[1]}
            for s in starts
        ],
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
            "control_enabled": True,
            "protagonist": over.pop("protagonist", "franklin"),
        },
        "vehicle": dict(VEHICLE) if in_vehicle else None,
        "location": {"street": "Grove St", "zone": "Davis"},
        "world": {"clock": "13:45", "weather": "CLEAR", "timescale": 1.0},
        "mission": mission,
        "nearby": {"vehicles": [], "peds": over.pop("nearby_peds", [])},
        "last_task": {
            "id": over.pop("task_id", None),
            "type": over.pop("task_type", None),
            "status": over.pop("task_status", "idle"),
            "detail": "",
        },
        "bridge": {"version": "1.0.0", "edition": "legacy"},
    }
    assert not over, f"unused overrides: {sorted(over)}"
    return GameState.model_validate(body)


def planner(clock: FakeClock, seed: int = 1) -> DayPlanner:
    return DayPlanner(random.Random(seed), clock)


NEARBY_JOB = [((100.0, 0.0, 0.0), "franklin")]


# --- the v1.7 fields the whole thing rests on ---------------------------------


def test_state_without_the_v1_7_fields_still_parses() -> None:
    """The widening rule: a pre-v1.7 bridge must not crash the harness. `starts`
    defaults to [] and `protagonist` to "unknown" — an absent field and an empty
    list mean the same honest thing."""
    body = make_state().model_dump(by_alias=True)
    del body["player"]["protagonist"]
    del body["mission"]["starts"]
    state = GameState.model_validate(body)
    assert state.player.protagonist == UNKNOWN_PROTAGONIST
    assert state.mission.starts == []


def test_a_v1_7_state_carries_the_markers() -> None:
    state = make_state(starts=[((10.0, 20.0, 30.0), "trevor")], protagonist="trevor")
    assert state.player.protagonist == "trevor"
    assert len(state.mission.starts) == 1
    assert (state.mission.starts[0].pos.x, state.mission.starts[0].protagonist) == (10.0, "trevor")


# --- block alternation and timing ---------------------------------------------


def test_the_day_opens_on_a_roam_block() -> None:
    clock = FakeClock()
    p = planner(clock)
    assert p.block is Block.ROAM
    assert p.in_mission_block is False
    assert p.roam_preference in {idea.activity for idea in ROAM_IDEAS}


def test_a_roam_block_is_held_for_its_minimum_even_with_a_marker_right_there() -> None:
    clock = FakeClock()
    p = planner(clock)
    for _ in range(5):
        clock.tick(ROAM_MIN_BLOCK_S / 10.0)
        out = p.update(make_state(starts=NEARBY_JOB), "bored", False)
        assert out.task is None
        assert out.events == []
    assert p.block is Block.ROAM


def test_bored_ends_a_roam_block_once_the_floor_has_passed() -> None:
    """"or he is idle/bored per the mood tracker" — but never before the floor."""
    clock = FakeClock()
    p = planner(clock)
    clock.tick(ROAM_MIN_BLOCK_S + 1.0)
    out = p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.MISSION
    assert out.task is not None


def test_a_running_activity_is_not_cut_short_before_the_block_length() -> None:
    """"held ... until it completes/fails": a set piece mid-flight outranks a
    bored tick. The hard block length still wins (bounded time is bounded)."""
    clock = FakeClock()
    p = planner(clock)
    p.roam_activity_started("steal_nice_car")
    clock.tick(ROAM_MIN_BLOCK_S + 1.0)
    p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.ROAM, "an activity in flight must not be cut short"
    clock.tick(ROAM_BLOCK_S[1])
    p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.MISSION, "past the block length, the block ends regardless"


def test_the_block_length_is_inside_the_contracted_window() -> None:
    for seed in range(25):
        clock = FakeClock()
        p = planner(clock, seed=seed)
        # Never fires before ROAM_BLOCK_S[0] with a calm mood and a fresh mission clock.
        p.observe_mission_event("mission_start")
        clock.tick(ROAM_BLOCK_S[0] - 1.0)
        p.update(make_state(starts=NEARBY_JOB), "chill", False)
        assert p.block is Block.ROAM, f"seed {seed} ended a roam block early"
        clock.tick(ROAM_BLOCK_S[1] - ROAM_BLOCK_S[0] + 2.0)
        p.update(make_state(starts=NEARBY_JOB), "chill", False)
        assert p.block is Block.MISSION, f"seed {seed} never ended its roam block"


def test_a_fresh_mission_does_not_cut_a_roam_block_short_but_boredom_does() -> None:
    """A/B on the same clock reading: recency holds the block, boredom ends it."""
    for mood, expected in (("chill", Block.ROAM), ("bored", Block.MISSION)):
        clock = FakeClock()
        p = planner(clock)
        p.observe_mission_event("mission_start")  # a job started just now
        clock.tick(ROAM_MIN_BLOCK_S + 1.0)  # past the floor, under any block length
        p.update(make_state(starts=NEARBY_JOB), mood, False)
        assert p.block is expected, f"mood {mood} gave {p.block}"


def test_never_having_done_a_job_counts_as_overdue() -> None:
    """The contrast with the test above: same clock reading, same calm mood,
    but no mission this session — so he goes and finds one."""
    clock = FakeClock()
    p = planner(clock)
    clock.tick(ROAM_MIN_BLOCK_S + 1.0)
    p.update(make_state(starts=NEARBY_JOB), "chill", False)
    assert p.block is Block.MISSION


def test_the_overdue_window_is_measured_from_the_last_mission() -> None:
    """"prefer a mission when one has not been done in the last ~20 min"."""
    clock = FakeClock()
    start = clock.t
    p = planner(clock)
    p.observe_mission_event("mission_start")
    # Roll roam blocks forward with an empty map, so nothing can start.
    while clock.t - start < MISSION_OVERDUE_S:
        clock.tick(ROAM_BLOCK_S[1] + 1.0)
        p.update(make_state(starts=[]), "chill", False)
        assert p.block is Block.ROAM
    # A brand-new block, only just past its floor — but the last job is now
    # more than the overdue window ago, so the block is cut short for it.
    clock.tick(ROAM_MIN_BLOCK_S + 1.0)
    p.update(make_state(starts=NEARBY_JOB), "chill", False)
    assert p.block is Block.MISSION


# --- a mission block needs a marker he can actually use -----------------------


def _roam_out(p: DayPlanner, clock: FakeClock) -> None:
    """Run the roam block past its longest possible length."""
    clock.tick(ROAM_BLOCK_S[1] + 1.0)


def test_no_markers_means_no_mission_block_ever() -> None:
    clock = FakeClock()
    p = planner(clock)
    for _ in range(5):
        _roam_out(p, clock)
        out = p.update(make_state(starts=[]), "bored", False)
        assert p.block is Block.ROAM
        assert out.task is None
        assert out.events == []


def test_only_a_marker_for_his_own_protagonist_counts() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(make_state(protagonist="franklin", starts=[((100.0, 0.0, 0.0), "trevor")]), "bored", False)
    assert p.block is Block.ROAM, "Franklin cannot start Trevor's job by standing in it"


def test_an_unknown_player_protagonist_makes_every_marker_fair_game() -> None:
    """Pre-v1.7 bridge, or the bridge could not tell: the alternative to
    'try any of them' is 'never start a mission', which is the bug."""
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(
        make_state(protagonist=UNKNOWN_PROTAGONIST, starts=[((100.0, 0.0, 0.0), "trevor")]),
        "bored",
        False,
    )
    assert p.block is Block.MISSION


def test_a_marker_of_unknown_protagonist_is_not_excluded() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(
        make_state(protagonist="michael", starts=[((80.0, 0.0, 0.0), UNKNOWN_PROTAGONIST)]),
        "bored",
        False,
    )
    assert p.block is Block.MISSION


def test_the_nearest_matching_marker_is_the_one_he_goes_to() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    out = p.update(
        make_state(
            in_vehicle=True,
            protagonist="franklin",
            starts=[
                ((900.0, 0.0, 0.0), "franklin"),
                ((10.0, 0.0, 0.0), "trevor"),  # nearer, but not his
                ((300.0, 0.0, 0.0), "franklin"),
            ],
        ),
        "bored",
        False,
    )
    assert out.task is not None
    assert out.task["type"] == "drive_to"
    assert (out.task["params"]["x"], out.task["params"]["y"]) == (300.0, 0.0)


# --- bad moments --------------------------------------------------------------


def test_no_mission_block_while_wanted() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(make_state(starts=NEARBY_JOB, wanted=2), "bored", False)
    assert p.block is Block.ROAM


def test_no_mission_block_while_badly_hurt() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    hurt = int(MIN_HEALTH_FRACTION_TO_START * 200) - 1
    p.update(make_state(starts=NEARBY_JOB, health=hurt), "bored", False)
    assert p.block is Block.ROAM
    _roam_out(p, clock)
    p.update(make_state(starts=NEARBY_JOB, health=hurt + 2), "bored", False)
    assert p.block is Block.MISSION


def test_no_mission_block_while_dead_or_arrested() -> None:
    for over in ({"dead": True}, {"arrested": True}):
        clock = FakeClock()
        p = planner(clock)
        _roam_out(p, clock)
        p.update(make_state(starts=NEARBY_JOB, **over), "bored", False)
        assert p.block is Block.ROAM, f"{over} must not start a mission block"


def test_no_mission_block_mid_cutscene() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(make_state(starts=NEARBY_JOB, cutscene_active=True), "bored", False)
    assert p.block is Block.ROAM


def test_going_down_mid_trip_aborts_the_block_and_says_so() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(make_state(in_vehicle=True, starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.MISSION
    out = p.update(make_state(starts=NEARBY_JOB, dead=True), "bored", False)
    assert p.block is Block.ROAM
    assert [(t, pl["outcome"]) for t, pl in out.events] == [("activity_end", "went_down")]


def test_picking_up_stars_mid_trip_aborts_the_block() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(make_state(in_vehicle=True, starts=NEARBY_JOB), "bored", False)
    out = p.update(make_state(in_vehicle=True, starts=NEARBY_JOB, wanted=3), "bored", False)
    assert p.block is Block.ROAM
    assert out.events[0][1]["outcome"] == "wanted"


# --- back-off after repeated failures ------------------------------------------


def test_three_fails_in_a_row_buy_a_full_roam_block() -> None:
    """"do not loop on a mission he cannot clear" — and the game's own Skip
    prompt is not ours to press, so leaving is the only honest way out."""
    clock = FakeClock()
    p = planner(clock)
    for _ in range(MAX_CONSECUTIVE_MISSION_FAILS):
        p.observe_mission_event("mission_start")
        p.observe_mission_event("mission_fail")
    # A bored two minutes is NOT the full block the back-off asks for.
    clock.tick(ROAM_MIN_BLOCK_S + 1.0)
    p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.ROAM, "an owed block runs its full length"
    _roam_out(p, clock)
    p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.MISSION, "after one full roam block he is allowed to try again"


def test_two_fails_do_not_trigger_the_back_off() -> None:
    clock = FakeClock()
    p = planner(clock)
    for _ in range(MAX_CONSECUTIVE_MISSION_FAILS - 1):
        p.observe_mission_event("mission_start")
        p.observe_mission_event("mission_fail")
    _roam_out(p, clock)
    p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.MISSION


def test_a_clean_mission_resets_the_fail_streak() -> None:
    clock = FakeClock()
    p = planner(clock)
    for _ in range(MAX_CONSECUTIVE_MISSION_FAILS - 1):
        p.observe_mission_event("mission_start")
        p.observe_mission_event("mission_fail")
    p.observe_mission_event("mission_start")
    p.update(make_state(mission_active=True), "chill", True)
    p.update(make_state(starts=NEARBY_JOB), "chill", False)  # ended clean
    p.observe_mission_event("mission_start")
    p.observe_mission_event("mission_fail")
    _roam_out(p, clock)
    p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.MISSION, "one fail after a clean run is not a streak"


def test_one_roam_block_is_owed_after_every_mission() -> None:
    """The celebrate (or sulk) beat — he does not walk straight back in."""
    clock = FakeClock()
    p = planner(clock)
    p.observe_mission_event("mission_start")
    p.update(make_state(mission_active=True, starts=NEARBY_JOB), "chill", True)
    p.update(make_state(starts=NEARBY_JOB), "chill", False)
    assert p.block is Block.ROAM, "he does not walk straight back into the next job"
    # Bored, past the floor, a marker right there — and still roaming, because
    # the owed block is a full block.
    clock.tick(ROAM_MIN_BLOCK_S + 1.0)
    p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.ROAM
    _roam_out(p, clock)
    p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.MISSION, "one owed block, not two"


# --- handover to the mission itself --------------------------------------------


def test_mission_active_ends_the_trip_and_hands_over() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(make_state(in_vehicle=True, starts=NEARBY_JOB), "bored", False)
    clock.tick(30.0)
    out = p.update(make_state(mission_active=True, starts=NEARBY_JOB), "bored", True)
    assert p.block is Block.MISSION
    assert p.in_mission_block is True
    assert [(t, pl["outcome"]) for t, pl in out.events] == [("activity_end", "mission_started")]
    assert out.events[0][1]["activity"] == GO_START_A_JOB
    assert out.events[0][1]["duration_s"] == 30.0
    # Hands off from here: MissionFollower and the mission card own the tick.
    for _ in range(3):
        clock.tick(5.0)
        again = p.update(make_state(mission_active=True), "hyped", True)
        assert again.task is None
        assert again.events == []


def test_a_mission_that_starts_without_the_planner_is_still_respected() -> None:
    """The director drove through a marker on its own: the plan steps back all
    the same, and still owes a roam block afterwards."""
    clock = FakeClock()
    p = planner(clock)
    out = p.update(make_state(mission_active=True), "chill", True)
    assert p.in_mission_block is True
    assert out.events == [], "no trip was in flight, so nothing to close"
    p.update(make_state(starts=NEARBY_JOB), "chill", False)
    assert p.block is Block.ROAM
    clock.tick(ROAM_MIN_BLOCK_S + 1.0)
    p.update(make_state(starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.ROAM, "the post-mission block is owed in full"


# --- the trip: navigation, timeouts, arrival -----------------------------------


def _start_trip(clock: FakeClock, **over: Any) -> tuple[DayPlanner, Any]:
    p = planner(clock)
    _roam_out(p, clock)
    out = p.update(make_state(starts=NEARBY_JOB, **over), "bored", False)
    return p, out


def test_the_trip_emits_the_contracted_activity_start_pair() -> None:
    clock = FakeClock()
    _p, out = _start_trip(clock, in_vehicle=True)
    assert [t for t, _ in out.events] == ["activity_start"]
    payload = out.events[0][1]
    assert payload["activity"] == GO_START_A_JOB
    assert payload["params"] == {"x": 100.0, "y": 0.0, "z": 0.0, "protagonist": "franklin"}


def test_far_and_in_a_car_he_drives() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    out = p.update(
        make_state(in_vehicle=True, starts=[((900.0, 0.0, 0.0), "franklin")]), "bored", False
    )
    assert out.task is not None and out.task["type"] == "drive_to"
    assert out.task["params"]["style"] == "rushed", "a cross-map haul is not a pootle"


def test_close_and_in_a_car_he_parks_and_walks_in() -> None:
    """`drive_to` stops at 8 m, which is outside the corona; `walk_to` finishes
    within 2 m, which is inside it."""
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    out = p.update(
        make_state(in_vehicle=True, starts=[((MARKER_FINAL_APPROACH_M - 5.0, 0.0, 0.0), "franklin")]),
        "bored",
        False,
    )
    assert out.task is not None and out.task["type"] == "exit_vehicle"


def test_close_and_on_foot_he_walks() -> None:
    clock = FakeClock()
    _p, out = _start_trip(clock)  # 100 m, on foot, inside the walk radius
    assert WALK_SWITCH_RADIUS_M >= 100.0
    assert out.task is not None and out.task["type"] == "walk_to"
    assert out.task["params"]["run"] is True


def test_far_and_on_foot_he_finds_a_car_first() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    out = p.update(
        make_state(starts=[((WALK_SWITCH_RADIUS_M + 200.0, 0.0, 0.0), "franklin")]), "bored", False
    )
    assert out.task is not None and out.task["type"] == "enter_nearest_vehicle"
    assert out.task["params"]["search_radius_m"] == VEHICLE_SEARCH_RADIUS_M


def test_the_planner_does_not_fight_a_foreign_running_task() -> None:
    """Same rule as MissionFollower/ActivityRunner: the reflex only posts when
    the slot is free."""
    clock = FakeClock()
    p, _ = _start_trip(clock, in_vehicle=True)
    out = p.update(
        make_state(
            in_vehicle=True,
            starts=NEARBY_JOB,
            task_id="t-brain-9",
            task_type="combat_hated_targets_around",
            task_status="running",
        ),
        "bored",
        False,
    )
    assert out.task is None


def test_the_planner_does_not_reissue_its_own_running_task() -> None:
    clock = FakeClock()
    p, out = _start_trip(clock, in_vehicle=True)
    assert out.task is not None
    p.bind_task("t-plan-1")
    still = p.update(
        make_state(in_vehicle=True, starts=NEARBY_JOB, task_id="t-plan-1", task_status="running"),
        "bored",
        False,
    )
    assert still.task is None
    done = p.update(
        make_state(in_vehicle=True, starts=NEARBY_JOB, task_id="t-plan-1", task_status="done"),
        "bored",
        False,
    )
    assert done.task is not None, "a finished task means reconsider, not stop"


HOSTILE_PED = {
    "handle": 42,
    "model": "a_m_y_mexthug_01",
    "distance": 12.0,
    "relationship": "hostile",
    "pos": {"x": 12.0, "y": 0.0, "z": 0.0},
}


def test_a_close_hostile_pauses_the_trip_without_abandoning_it() -> None:
    """The survival ladder owns that tick — but a firefight is seconds and the
    trip is minutes, so the block survives it."""
    clock = FakeClock()
    p, _ = _start_trip(clock, in_vehicle=True)
    under_fire = make_state(in_vehicle=True, starts=NEARBY_JOB, nearby_peds=[HOSTILE_PED])
    out = p.update(under_fire, "scared", False)
    assert out.task is None
    assert out.events == []
    assert p.block is Block.MISSION, "the trip is paused, not abandoned"
    clear = p.update(make_state(in_vehicle=True, starts=NEARBY_JOB), "scared", False)
    assert clear.task is not None, "once the fight is over he carries on to the marker"


def test_going_down_next_to_a_hostile_still_ends_the_block() -> None:
    """The pause must sit behind the abort reasons, or a corpse beside a
    hostile pauses the trip forever instead of ending it."""
    clock = FakeClock()
    p, _ = _start_trip(clock, in_vehicle=True)
    out = p.update(
        make_state(starts=NEARBY_JOB, dead=True, nearby_peds=[HOSTILE_PED]), "scared", False
    )
    assert p.block is Block.ROAM
    assert out.events[0][1]["outcome"] == "went_down"


def test_no_mission_block_starts_while_a_hostile_is_close() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(make_state(starts=NEARBY_JOB, nearby_peds=[HOSTILE_PED]), "bored", False)
    assert p.block is Block.ROAM
    assert "fight to finish" in p.note()


def test_the_trip_times_out_instead_of_looping_forever() -> None:
    clock = FakeClock()
    p, _ = _start_trip(clock, in_vehicle=True)
    clock.tick(MISSION_BLOCK_TIMEOUT_S + 1.0)
    out = p.update(make_state(in_vehicle=True, starts=NEARBY_JOB), "bored", False)
    assert p.block is Block.ROAM
    assert out.events[0][1]["outcome"] == "gave_up"


def test_a_marker_that_disappears_ends_the_trip() -> None:
    clock = FakeClock()
    p, _ = _start_trip(clock, in_vehicle=True)
    out = p.update(make_state(in_vehicle=True, starts=[]), "bored", False)
    assert p.block is Block.ROAM
    assert out.events[0][1]["outcome"] == "marker_gone"


def test_standing_in_a_marker_that_never_triggers_is_given_up_on() -> None:
    """Honest failure: if the corona does not start the job, say so and go do
    something else rather than stand on the pavement for six minutes."""
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    on_top = make_state(starts=[((0.5, 0.0, 0.0), "franklin")])
    out = p.update(on_top, "bored", False)
    assert out.task is None, "already inside the arrival radius; stop pushing"
    clock.tick(MARKER_WAIT_S / 2.0)
    p.update(on_top, "bored", False)
    assert p.block is Block.MISSION
    clock.tick(MARKER_WAIT_S + 1.0)
    out = p.update(on_top, "bored", False)
    assert p.block is Block.ROAM
    assert out.events[0][1]["outcome"] == "marker_did_not_trigger"


def test_the_target_marker_is_sticky_while_two_sit_side_by_side() -> None:
    """Re-choosing "nearest" every tick makes near-equidistant markers swap and
    re-post a drive task each tick."""
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    both = [((300.0, 0.0, 0.0), "franklin"), ((301.0, 5.0, 0.0), "franklin")]
    first = p.update(make_state(in_vehicle=True, starts=both), "bored", False)
    assert first.task is not None
    chosen = (first.task["params"]["x"], first.task["params"]["y"])
    # Drive a little closer to the SECOND one; the target must not swap.
    again = p.update(make_state(in_vehicle=True, pos=(0.0, 5.0, 0.0), starts=both), "bored", False)
    assert again.task is not None
    assert (again.task["params"]["x"], again.task["params"]["y"]) == chosen


# --- events: only §4 types, and only the pair that already exists --------------


def test_every_event_the_planner_emits_is_a_contract_type() -> None:
    from wasted_harness.events import EVENT_TYPES

    clock = FakeClock()
    seen: set[str] = set()
    for finish in ("mission", "timeout", "down", "gone"):
        p = planner(clock)
        _roam_out(p, clock)
        out = p.update(make_state(in_vehicle=True, starts=NEARBY_JOB), "bored", False)
        seen |= {t for t, _ in out.events}
        if finish == "mission":
            out = p.update(make_state(mission_active=True, starts=NEARBY_JOB), "bored", True)
        elif finish == "timeout":
            clock.tick(MISSION_BLOCK_TIMEOUT_S + 1.0)
            out = p.update(make_state(in_vehicle=True, starts=NEARBY_JOB), "bored", False)
        elif finish == "down":
            out = p.update(make_state(starts=NEARBY_JOB, dead=True), "bored", False)
        else:
            out = p.update(make_state(in_vehicle=True, starts=[]), "bored", False)
        seen |= {t for t, _ in out.events}
    assert seen == {"activity_start", "activity_end"}
    assert seen <= EVENT_TYPES


def test_a_roam_block_emits_nothing_of_its_own() -> None:
    """The activities running inside a roam block already emit the pair; a
    second overlapping pair for the container would double-count it."""
    clock = FakeClock()
    p = planner(clock)
    for _ in range(6):
        clock.tick(60.0)
        out = p.update(make_state(starts=[]), "chill", False)
        assert out.events == []


# --- roam ideas are real behaviour, not narration ------------------------------


def test_every_roam_idea_maps_onto_a_real_roam_goal() -> None:
    """The plan may only announce ideas the roam engine can run AND VERIFY.

    Retargeted when free-roam picking moved from `behavior.activities` (a
    weighted draw that finished when its last bridge task returned `done`) to
    `behavior.roam` (a `needs` gate and a `done_when` predicate). An idea naming
    a goal the engine has never heard of is a plan the show cannot keep.
    """
    for idea in ROAM_IDEAS:
        assert idea.activity in PICKABLE_GOAL_IDS, (
            f"{idea.activity} is not a roam goal the engine can run"
        )


def test_every_roam_idea_goal_fits_the_decision_schema_goal_budget() -> None:
    """CONTRACTS §2: goal is ≤12 words."""
    for idea in ROAM_IDEAS:
        assert len(idea.goal.split()) <= 12, idea


def test_a_preferred_activity_is_honoured_when_eligible() -> None:
    picker = ActivityPicker(random.Random(3))
    chosen = picker.peek("chill", prefer="park_and_watch")
    assert chosen is not None and chosen.name == "park_and_watch"


def test_a_preference_the_picker_cannot_honour_falls_through_quietly() -> None:
    """Cooldowns, the chaos budget and the death-spot precondition still rule."""
    picker = ActivityPicker(random.Random(3))
    # visit_death_spot is ineligible until a real death has happened.
    chosen = picker.peek("chill", prefer="visit_death_spot")
    assert chosen is not None and chosen.name != "visit_death_spot"


def test_the_runner_reports_what_actually_started_not_what_was_asked_for() -> None:
    clock = FakeClock()
    p = planner(clock)
    p.roam_activity_started("bike_hills")
    assert "up a hill" in p.note()
    p.roam_activity_ended()
    assert "up a hill" not in p.note()


def test_the_planner_asks_for_nothing_while_it_is_on_a_job() -> None:
    clock = FakeClock()
    p, _ = _start_trip(clock, in_vehicle=True)
    assert p.roam_preference is None


# --- note() --------------------------------------------------------------------


def test_note_during_a_roam_block_names_the_idea_the_time_left_and_what_is_next() -> None:
    clock = FakeClock()
    p = planner(clock)
    p.update(make_state(starts=[((0.0, 900.0, 0.0), "franklin")]), "chill", False)
    note = p.note()
    assert note.startswith("PLAN: roam block")
    assert "min left" in note
    assert "next: go start the franklin job" in note
    assert "900 m N" in note, note


def test_note_says_when_the_next_thing_is_another_roam_block() -> None:
    clock = FakeClock()
    p = planner(clock)
    p.update(make_state(starts=[]), "chill", False)
    assert "no job marker he can use is on the map" in p.note()


def test_note_during_a_mission_block_says_where_and_how_long_he_will_try() -> None:
    clock = FakeClock()
    p = planner(clock)
    _roam_out(p, clock)
    p.update(make_state(in_vehicle=True, starts=[((0.0, -450.0, 0.0), "franklin")]), "bored", False)
    note = p.note()
    assert note.startswith("PLAN: mission block")
    assert "450 m S" in note, note
    assert "before he gives up" in note


def test_note_while_the_job_is_live_says_the_plan_stands_back() -> None:
    clock = FakeClock()
    p = planner(clock)
    p.update(make_state(mission_active=True), "chill", True)
    assert "the job is live" in p.note()


def test_note_explains_a_back_off_rather_than_going_quiet() -> None:
    clock = FakeClock()
    p = planner(clock)
    for _ in range(MAX_CONSECUTIVE_MISSION_FAILS):
        p.observe_mission_event("mission_start")
        p.observe_mission_event("mission_fail")
    p.update(make_state(starts=NEARBY_JOB), "chill", False)
    assert "failed attempts in a row" in p.note(), p.note()


def test_compass_bearings_use_the_maps_own_convention() -> None:
    """+Y north, +X east. A wrong label would only mislabel a sentence —
    navigation always uses the raw pos — but it should still be right."""
    assert compass_bearing(0.0, 100.0) == "N"
    assert compass_bearing(100.0, 0.0) == "E"
    assert compass_bearing(0.0, -100.0) == "S"
    assert compass_bearing(-100.0, 0.0) == "W"
    assert compass_bearing(100.0, 100.0) == "NE"
    assert compass_bearing(0.0, 0.0) == "here"


# --- the shared navigation helper ----------------------------------------------


def test_navigate_to_returns_none_once_he_is_there() -> None:
    assert navigate_to((0.0, 0.0, 0.0), False, (WALK_ARRIVE_RADIUS_M - 0.5, 0.0, 0.0)) is None
    assert navigate_to((0.0, 0.0, 0.0), True, (5.0, 0.0, 0.0)) is None


def test_navigate_to_only_dismounts_when_asked() -> None:
    """MissionFollower does not pass `final_approach_on_foot_m`; a mission
    OBJECTIVE is often something you arrive at in the car."""
    close = (MARKER_FINAL_APPROACH_M - 5.0, 0.0, 0.0)
    plain = navigate_to((0.0, 0.0, 0.0), True, close)
    assert plain is not None and plain["type"] == "drive_to"
    step = navigate_to(
        (0.0, 0.0, 0.0), True, close, final_approach_on_foot_m=MARKER_FINAL_APPROACH_M
    )
    assert step is not None and step["type"] == "exit_vehicle"


def test_navigate_to_only_ever_speaks_the_closed_task_vocabulary() -> None:
    from wasted_harness.bridge_client import BRIDGE_TASK_TYPES

    cases = [
        ((0.0, 0.0, 0.0), True, (900.0, 0.0, 0.0)),
        ((0.0, 0.0, 0.0), False, (50.0, 0.0, 0.0)),
        ((0.0, 0.0, 0.0), False, (900.0, 0.0, 0.0)),
        ((0.0, 0.0, 0.0), True, (10.0, 0.0, 0.0)),
    ]
    for pos, in_vehicle, target in cases:
        step = navigate_to(pos, in_vehicle, target, final_approach_on_foot_m=25.0)
        assert step is not None
        assert step["type"] in BRIDGE_TASK_TYPES, step


# --- wiring: the real Harness methods -------------------------------------------


class _Recorder:
    """Absorbs bookkeeping calls; records nothing asserted on."""

    def __getattr__(self, _name: str):
        return lambda *a, **k: None


class _PlanStub:
    """Just enough of the harness for the REAL `_drive_day_plan` — same idiom as
    test_mission_following.py's `_ReflexStub` and test_governor_l3.py's
    `StubHarness`: the method under test is the real, unmodified one."""

    def __init__(self, clock: FakeClock, seed: int = 1, level: int = 0) -> None:
        self.planner = DayPlanner(random.Random(seed), clock)
        self.governor = _Governor(level)
        self.mood = _Mood()
        self.missions = _Missions()
        self.writer = _EventWriter()
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self._quiet_until = 0.0
        #: `_drive_day_plan` stands down when the survival ladder claimed the
        #: tick (main.Harness._threat_has_the_wheel); no threat in these tests.
        self._threat_has_the_wheel = False
        #: Every movement post registers with the arbiter (main.wheel).
        self.wheel = MovementWheel()

    def _execute_action(self, action_type: str, params: dict[str, Any]) -> str | None:
        self.posted.append((action_type, dict(params)))
        return "t-plan-77"


class _Mood(MoodModel):
    """The real mood model (`_drive_activities` asks it for a driving style),
    pinned to `bored` so the activity picker's mood affinity is deterministic."""

    def __init__(self) -> None:
        super().__init__(rng=random.Random(1))
        self.mood = "bored"


class _Missions:
    in_mission = False


class _Governor:
    def __init__(self, level: int = 0) -> None:
        self.level = level


class _EventWriter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record_event(self, type_: str, payload: dict[str, Any], **_k: Any) -> None:
        self.events.append((type_, payload))


def test_drive_day_plan_posts_the_navigation_and_binds_the_returned_task_id() -> None:
    clock = FakeClock()
    stub = _PlanStub(clock)
    clock.tick(ROAM_BLOCK_S[1] + 1.0)
    Harness._drive_day_plan(stub, make_state(in_vehicle=True, starts=NEARBY_JOB))
    assert [t for t, _ in stub.posted] == ["drive_to"]
    assert [t for t, _ in stub.writer.events] == ["activity_start"]
    # Bound to POST /task's own id: the next tick must not re-post.
    Harness._drive_day_plan(
        stub,
        make_state(in_vehicle=True, starts=NEARBY_JOB, task_id="t-plan-77", task_status="running"),
    )
    assert len(stub.posted) == 1


def test_drive_day_plan_honours_a_pending_quiet_period() -> None:
    """A brain-issued `wait` must not be immediately overridden by the reflex —
    but the state machine still advances, so the block is not frozen."""
    clock = FakeClock()
    stub = _PlanStub(clock)
    stub._quiet_until = float("inf")
    clock.tick(ROAM_BLOCK_S[1] + 1.0)
    Harness._drive_day_plan(stub, make_state(in_vehicle=True, starts=NEARBY_JOB))
    assert stub.posted == []
    assert stub.planner.block is Block.MISSION
    assert [t for t, _ in stub.writer.events] == ["activity_start"]


def test_drive_day_plan_sleeps_at_governor_l3() -> None:
    """§7 L3 is "asleep in the car". A day plan that drove off to a marker
    while he is meant to be parked and silent is the same bug the L2 wander
    reflex had: it makes L3 mean nothing."""
    clock = FakeClock()
    stub = _PlanStub(clock, level=3)
    clock.tick(ROAM_BLOCK_S[1] + 1.0)
    Harness._drive_day_plan(stub, make_state(in_vehicle=True, starts=NEARBY_JOB))
    assert stub.posted == []
    assert stub.writer.events == []
    assert stub.planner.block is Block.ROAM


def test_drive_day_plan_writes_every_planner_event_to_the_writer() -> None:
    clock = FakeClock()
    stub = _PlanStub(clock)
    clock.tick(ROAM_BLOCK_S[1] + 1.0)
    Harness._drive_day_plan(stub, make_state(in_vehicle=True, starts=NEARBY_JOB))
    clock.tick(MISSION_BLOCK_TIMEOUT_S + 1.0)
    Harness._drive_day_plan(stub, make_state(in_vehicle=True, starts=NEARBY_JOB))
    assert [t for t, _ in stub.writer.events] == ["activity_start", "activity_end"]


# --- wiring: the mission block must not be fought by the free-roam layers ------


class _ActivityStub:
    """Enough of the harness for the REAL `_drive_activities`."""

    def __init__(self, clock: FakeClock, in_mission_block: bool) -> None:
        class _Breaks:
            on_break = False

        self.governor = _Governor(0)
        self.breaks = _Breaks()
        self.mood = _Mood()
        self.missions = _Missions()
        self.planner = DayPlanner(random.Random(1), clock)
        if in_mission_block:
            clock.tick(ROAM_BLOCK_S[1] + 1.0)
            self.planner.update(make_state(in_vehicle=True, starts=NEARBY_JOB), "bored", False)
            assert self.planner.in_mission_block
        self.activities = ActivityPicker(random.Random(1), clock)
        self.activity_runner = ActivityRunner(self.activities, random.Random(1), clock)
        #: Free roam's single owner, on the stub's fake clock so a test can roll
        #: cooldowns and the goal timeout deliberately. The REAL engine: which
        #: goal is offered and whether it locks is the behaviour under test.
        self.roam = RoamEngine(random.Random(1), clock)
        self.writer = _EventWriter()
        self.memory = _Recorder()
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self._quiet_until = 0.0
        self._threat_has_the_wheel = False
        self.wheel = MovementWheel()
        #: `_begin_roam_goal` reads the model's last goal line to see whether it
        #: named an offered id. Nothing has been said yet on this stub.
        self.current_goal = "see the city"

    def _execute_action(self, action_type: str, params: dict[str, Any]) -> str | None:
        self.posted.append((action_type, dict(params)))
        return "t-act-1"

    def _end_activity_if_running(self, outcome: str) -> None:
        Harness._end_activity_if_running(self, outcome)

    def _issue_activity_step(self, step: dict[str, Any]) -> None:
        Harness._issue_activity_step(self, step)

    # The three halves of the free-roam slot are the real ones: whether a goal
    # is picked, advanced or graded is exactly what these tests are about.
    def _judge_roam_goal(self, state: GameState) -> bool:
        return Harness._judge_roam_goal(self, state)

    def _advance_roam_goal(self, state: GameState) -> None:
        Harness._advance_roam_goal(self, state)

    def _begin_roam_goal(self, state: GameState) -> None:
        Harness._begin_roam_goal(self, state)


def test_drive_activities_starts_nothing_during_a_mission_block() -> None:
    clock = FakeClock()
    stub = _ActivityStub(clock, in_mission_block=True)
    clock.tick(500.0)  # past the runner's own jittered gap between activities
    Harness._drive_activities(stub, make_state(in_vehicle=True, starts=NEARBY_JOB))
    assert stub.posted == [], "a free-roam activity must not post over the trip to a marker"
    assert stub.activity_runner.current is None


def test_drive_activities_still_runs_during_a_roam_block() -> None:
    clock = FakeClock()
    stub = _ActivityStub(clock, in_mission_block=False)
    clock.tick(500.0)  # past the runner's own jittered gap
    Harness._drive_activities(stub, make_state(in_vehicle=True))
    assert stub.activity_runner.current is not None
    assert [t for t, _ in stub.writer.events] == ["activity_start"]


def test_a_mission_block_ends_an_activity_that_was_already_running() -> None:
    clock = FakeClock()
    stub = _ActivityStub(clock, in_mission_block=False)
    clock.tick(500.0)
    Harness._drive_activities(stub, make_state(in_vehicle=True))
    assert stub.activity_runner.current is not None
    # The block is ended by a REQUEST rather than by winding the clock past
    # ROAM_BLOCK_S: every roam goal now carries its own `timeout_s` (the longest
    # is 420 s), so a twelve-minute jump would fail the goal on the timeout and
    # this test would stop testing the mission-block gate at all.
    stub.planner.request_mission_block("three goals done")
    clock.tick(1.0)
    stub.planner.update(make_state(in_vehicle=True, starts=NEARBY_JOB), "bored", False)
    Harness._drive_activities(stub, make_state(in_vehicle=True, starts=NEARBY_JOB))
    assert stub.activity_runner.current is None
    ends = [p for t, p in stub.writer.events if t == "activity_end"]
    assert ends and ends[-1]["outcome"] == "day_plan_mission_block"


# --- the survival ladder outranks the plan for the tick it claims -------------
#
# `_reflex` runs before all three drive_* methods and sets
# `_threat_has_the_wheel` from THIS tick's snapshot. They cannot work it out
# for themselves: their own "someone else has the wheel" test reads
# `state.last_task`, and `state` predates the reflex's POST by milliseconds.
# Without the flag a mission trip posts navigation straight over the combat
# task, and ThreatLatch's hold-down then suppresses the re-post — the reflex is
# silently defeated. `DayPlanner._threat_close` has its own copy of this gate
# for the `hostile` case, which by construction cannot see the damage-driven
# path (relationship is exactly what that path exists to stop depending on).


def test_the_day_plan_stands_down_when_survival_has_the_tick() -> None:
    clock = FakeClock()
    stub = _PlanStub(clock)
    stub._threat_has_the_wheel = True
    clock.tick(ROAM_BLOCK_S[1] + 1.0)
    Harness._drive_day_plan(stub, make_state(in_vehicle=True, starts=NEARBY_JOB))
    assert stub.posted == []
    # ...but the state machine still advanced, exactly as for a brain `wait`.
    assert stub.planner.block is Block.MISSION
    assert [t for t, _ in stub.writer.events] == ["activity_start"]


def test_free_roam_never_leaves_him_standing_still() -> None:
    """The operator's one hard rule, through the REAL free-roam slot.

    He is on foot, parked in one spot, with nothing to do and no goal running.
    Every tick feeds the same position, so the boredom window closes and the
    slot has to pick a goal and post a task for it — the jittered beat between
    goals is not allowed to outlast that.
    """
    clock = FakeClock()
    stub = _ActivityStub(clock, in_mission_block=False)
    standing = make_state()
    posted_at: list[float] = [clock.t]  # the tick he started standing there
    for _ in range(240):  # four minutes at a 1 s tick
        before = len(stub.posted)
        Harness._drive_activities(stub, standing)
        if len(stub.posted) > before:
            posted_at.append(clock.t)
        clock.tick(1.0)
    assert len(posted_at) > 1, "he stood still for four minutes with nothing posted"
    # He never goes quiet for longer than the boredom window plus one tick. The
    # goals themselves keep FAILING here — a stub whose position never changes is
    # genuinely pinned, so the in-goal stuck watchdog fires every time — and that
    # is the point: failing a goal has to put him straight onto the next one.
    gaps = [b - a for a, b in itertools.pairwise(posted_at)]
    assert max(gaps) <= BOREDOM_S + 2.0, (
        f"a silent stretch of {max(gaps):.0f}s; the operator's bar is 30s"
    )
    assert len(posted_at) > 5, "one post in four minutes is not a show"
    starts = [p for t, p in stub.writer.events if t == "activity_start"]
    assert len(starts) > 1 and all(s["why"] for s in starts), "every goal needs its why"
    # And everything posted is a real action from the frozen vocabulary.
    for action_type, _params in stub.posted:
        assert action_type in ACTION_TYPES, action_type


def test_a_goal_the_world_completed_is_recorded_as_verified() -> None:
    """`done_when`, not "the last task returned done": the whole point of the
    goal engine. The feed has to be able to tell a real completion from a
    timeout, so the `activity_end` payload carries which one it was."""
    clock = FakeClock()
    stub = _ActivityStub(clock, in_mission_block=False)
    on_foot = roam_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    stub.roam.observe(on_foot)
    stub.roam.pick(on_foot, goal_id="steal_nice_car")
    activity = as_activity(stub.roam.current.goal)
    stub.activity_runner.start_plan(activity, stub.roam.current.plan)
    # The world now says he is in a Super, which outranks the nothing he had.
    in_super = roam_state(in_vehicle=True, vehicle={"class": "Super", "model": "adder"})
    Harness._drive_activities(stub, in_super)
    ends = [p for t, p in stub.writer.events if t == "activity_end"]
    assert ends and ends[-1]["goal_id"] == "steal_nice_car"
    assert ends[-1]["outcome"] == "completed" and ends[-1]["verified"] is True
    assert stub.roam.current is None


def test_activities_stand_down_when_survival_has_the_tick() -> None:
    clock = FakeClock()
    stub = _ActivityStub(clock, in_mission_block=False)
    stub._threat_has_the_wheel = True
    for _ in range(5):
        clock.tick(30.0)
        Harness._drive_activities(stub, make_state(in_vehicle=True))
    assert stub.posted == []
