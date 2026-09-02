"""Ground navigation: one destination in, one bridge task out.

Extracted from :class:`behavior.missions.MissionFollower`, which grew the
drive / walk / go-and-get-a-car ladder first. The day planner (behavior/planner.py)
needs exactly the same ladder to get the agent into a mission-start marker, and two
copies of "how do I get there" is how the two halves of the show end up
disagreeing about what 120 m means.

Nothing here is new behaviour: the thresholds, the task shapes and the
planar-distance rule are the ones the follower has been using. The only
addition is `final_approach_on_foot_m`, which exists because a mission-start
corona is entered on foot (see the planner's rationale), and which the follower
does not pass.

The vocabulary is the closed CONTRACTS §1 task set — `drive_to`, `walk_to`,
`enter_nearest_vehicle`, `exit_vehicle`. No new task type is invented here and
none can be: the bridge rejects anything outside that list.
"""

from __future__ import annotations

from typing import Any


def planar_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """XY-only distance, matching the bridge's own arrival rule (STATUS.md:
    "drive/walk arrival = planar (XY) distance"). A 30 m drop off a freeway
    overpass is not 30 m of driving."""
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


#: Arrival radii: `drive_to`'s own contract default is 8 m; `walk_to` completes
#: within 2 m (CONTRACTS §1). Once inside, stop pushing — the engine (or the
#: mission script) owns the last few metres.
DRIVE_ARRIVE_RADIUS_M = 8.0
WALK_ARRIVE_RADIUS_M = 2.0

#: On foot, walk if the destination is within this range; farther than this,
#: look for a car first rather than jogging across the map.
WALK_SWITCH_RADIUS_M = 120.0

#: How wide `enter_nearest_vehicle` looks when the ladder decides he needs a car.
VEHICLE_SEARCH_RADIUS_M = 40.0

#: Past this planar distance, drive with the `rushed` style instead of `normal`
#: — a long haul across the map, not weaving through the block ahead.
#: RAISED after live feedback 2026-09-02 ("he can't drive quickly"). The old
#: numbers (150 m before rushing, 18/26 m/s) made every trip a sightseeing tour:
#: 18 m/s is 65 km/h, which loses a mission NPC immediately - `follow_entity`
#: alone defaults to 30 m/s (CONTRACTS v1.9) because that is what the game's own
#: mission drivers do. These are still speeds a human reaches in an ordinary car
#: on ordinary roads (24 m/s = 86 km/h, 34 m/s = 122 km/h), not a cheat: the
#: engine still drives, still steers, and still crashes if he asks too much.
RUSHED_STYLE_DISTANCE_M = 60.0
CRUISE_SPEED_MPS = 24.0
RUSHED_SPEED_MPS = 34.0

#: On foot, break into a run past this distance. Under it, walking reads as a
#: person arriving; over it, a jog is what a player would do.
RUN_DISTANCE_M = 15.0


def navigate_to(
    player_pos: tuple[float, float, float],
    in_vehicle: bool,
    target: tuple[float, float, float],
    *,
    prefer: str = "any",
    final_approach_on_foot_m: float | None = None,
) -> dict[str, Any] | None:
    """The next bridge task that gets the player from `player_pos` to `target`.

    Returns ``None`` when he is already there (inside the arrival radius for
    however he is travelling) — the caller should stop pushing rather than
    re-posting a task the engine has already satisfied.

    `final_approach_on_foot_m`: when set and he is driving, get out of the car
    at that range and finish on foot. Not because driving in would not work —
    CONTRACTS v1.7 says walking OR driving into a mission-start marker begins
    that mission — but because `drive_to` stops at its 8 m arrival radius
    (§1), and 8 m is wider than the corona. `walk_to` finishes within 2 m,
    which is inside it. The dismount is what turns "near the marker" into
    "in the marker".
    """
    distance = planar_distance(player_pos, target)
    x, y, z = target

    if in_vehicle:
        if final_approach_on_foot_m is not None and distance <= final_approach_on_foot_m:
            return {"type": "exit_vehicle", "params": {}}
        if distance <= DRIVE_ARRIVE_RADIUS_M:
            return None
        rushed = distance > RUSHED_STYLE_DISTANCE_M
        return {
            "type": "drive_to",
            "params": {
                "x": x,
                "y": y,
                "z": z,
                "speed_mps": RUSHED_SPEED_MPS if rushed else CRUISE_SPEED_MPS,
                "style": "rushed" if rushed else "normal",
                "arrive_radius_m": DRIVE_ARRIVE_RADIUS_M,
            },
        }

    if distance <= WALK_ARRIVE_RADIUS_M:
        return None
    if distance <= WALK_SWITCH_RADIUS_M:
        return {
            "type": "walk_to",
            "params": {"x": x, "y": y, "z": z, "run": distance > RUN_DISTANCE_M},
        }
    # On foot and too far to walk sensibly: get a car first. The drive branch
    # takes over once `in_vehicle` is true on a later tick.
    return {
        "type": "enter_nearest_vehicle",
        "params": {"prefer": prefer, "search_radius_m": VEHICLE_SEARCH_RADIUS_M},
    }
