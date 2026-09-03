"""A `/state`-shaped `GameState` builder, owned by fix-sonnet-e.

This is a DELIBERATE, independent copy of the pattern already established in
``tests/test_mission_following.py::make_state`` (and borrowed from there by
``tests/test_interior_escape.py``) rather than an import of it: that file is
owned by a different, concurrently-active executor (it shows modified in git
status while this brief is being worked), and importing a function from a file
someone else may still be reshaping would silently couple this package's
correctness to their in-flight edits. Same contract-shaped-body idiom, same
"every field explicit" rule (CLAUDE.md rule 1 — this is a pure-function input,
never a fixture file), independently maintained.

Every field mirrors ``docs/CONTRACTS.md`` §1's ``GET /state`` body (frozen at
v1.13) and ``wasted_harness/bridge_client.py``'s parse of it, verified by
reading both before writing this file.
"""

from __future__ import annotations

from typing import Any

from wasted_harness.bridge_client import GameState

VEHICLE: dict[str, Any] = {
    "handle": 1,
    "model": "blista",
    "display_name": "Blista",
    "class": "Compacts",
    "speed": 0.0,
    "health": 1000.0,
    "upside_down": False,
    "in_water": False,
    "stopped_for_s": 0.0,
}


def make_state(**over: Any) -> GameState:
    """A minimal, CONTRACTS v1.13-shaped ``/state`` body. Every field explicit.

    Kept close to ``docs/CONTRACTS.md`` §1's example body; overrides use the
    same short, positional-tuple shorthand for nested lists that
    ``test_mission_following.make_state`` uses, because that shorthand is what
    the rest of this package's test authors already read fluently.
    """
    pos = over.pop("pos", (0.0, 0.0, 0.0))
    blip = over.pop("objective_blip", None)
    in_vehicle = over.pop("in_vehicle", False)
    vehicle_override = over.pop("vehicle", None)
    interior = over.pop("interior", None)
    last_outdoor = over.pop("last_outdoor", None)
    mission: dict[str, Any] = {
        "active": over.pop("mission_active", False),
        "random_event_active": over.pop("random_event_active", False),
        "cutscene_active": over.pop("cutscene_active", False),
        "retry_in_flight": over.pop("retry_in_flight", False),
        "starts": [
            {"pos": {"x": m[0][0], "y": m[0][1], "z": m[0][2]}, "protagonist": m[1]}
            for m in over.pop("starts", [])
        ],
        "route_blips": [
            {
                "pos": {"x": r[0][0], "y": r[0][1], "z": r[0][2]},
                "kind": r[1],
                "handle": r[2],
                "color": "Yellow",
            }
            for r in over.pop("route_blips", [])
        ],
        "entity_blips": [
            {
                "pos": {"x": e[0][0], "y": e[0][1], "z": e[0][2]},
                "handle": e[1],
                "color": "Blue",
                "is_route": False,
                "distance": ((e[0][0] - pos[0]) ** 2 + (e[0][1] - pos[1]) ** 2) ** 0.5,
                "name": e[2] if len(e) > 2 else None,
            }
            for e in over.pop("entity_blips", [])
        ],
        "script": over.pop("mission_script", None),
    }
    if blip is not None:
        bx, by, bz = blip.get("pos", (0.0, 0.0, 0.0))
        mission["objective_blip"] = {
            "pos": {"x": bx, "y": by, "z": bz},
            "kind": blip.get("kind", "coord"),
            "handle": blip.get("handle", 1),
        }
    body = {
        "ts": over.pop("ts", "2026-08-25T21:14:03.221Z"),
        "tick": over.pop("tick", 1000),
        "player": {
            "pos": {"x": pos[0], "y": pos[1], "z": pos[2]},
            "heading": over.pop("heading", 0.0),
            "health": over.pop("health", 200),
            "max_health": 200,
            "armor": over.pop("armor", 0),
            "wanted": over.pop("wanted", 0),
            "cash": over.pop("cash", 0),
            "dead": over.pop("dead", False),
            "arrested": over.pop("arrested", False),
            "in_vehicle": in_vehicle,
            "control_enabled": over.pop("control_enabled", True),
            "protagonist": over.pop("protagonist", "franklin"),
            "switch_in_progress": over.pop("switch_in_progress", False),
            "interior": (
                None if interior is None else {"id": interior[0], "since_s": interior[1]}
            ),
            "last_outdoor": (
                None
                if last_outdoor is None
                else {"x": last_outdoor[0], "y": last_outdoor[1], "z": last_outdoor[2]}
            ),
        },
        "vehicle": (vehicle_override or dict(VEHICLE)) if in_vehicle else None,
        "location": {
            "street": over.pop("street", "Vinewood Blvd"),
            "zone": over.pop("zone", "Downtown Vinewood"),
        },
        "world": {
            "clock": over.pop("clock", "13:45"),
            "weather": over.pop("weather", "CLEAR"),
            "timescale": over.pop("timescale", 1.0),
        },
        "mission": mission,
        "phone": over.pop("phone", {"ringing": False, "in_call": False}),
        "nearby": {
            "vehicles": over.pop("nearby_vehicles", []),
            "peds": over.pop("nearby_peds", []),
        },
        "last_task": {
            "id": over.pop("task_id", None),
            "type": over.pop("task_type", None),
            "status": over.pop("task_status", "idle"),
            "detail": over.pop("task_detail", ""),
        },
        "bridge": {"version": "1.6.0", "edition": "legacy"},
        "threat": {
            "attacker_handle": over.pop("attacker_handle", None),
            "being_jacked_by": over.pop("being_jacked_by", None),
        },
    }
    assert not over, f"unused overrides: {sorted(over)}"
    return GameState.model_validate(body)
