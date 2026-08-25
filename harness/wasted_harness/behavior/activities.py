"""Core activity catalog (WP-H / master brief): what the agent does between missions.

Each activity is a small plan of bridge tasks/primitives with a weight, a
cooldown, per-mood affinity, and a chaos cost drawn from an hourly chaos budget
(so "deliberate police chase" happens, but not three times an hour).

Coordinates are curated from community mapping of the game world; they are
The agent's mental map, validated/tuned during the Phase 3 live checks on the
server (the shapes here don't change, the numbers may).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from ..logsetup import get_logger

log = get_logger("wasted.activities")

# Landmarks (world-space meters).
LANDMARKS: dict[str, tuple[float, float, float]] = {
    "mount_chiliad": (425.4, 5614.3, 766.5),
    "del_perro_pier": (-1850.1, -1231.8, 13.0),
    "vespucci_beach": (-1223.5, -1491.1, 4.3),
    "galileo_observatory": (-438.8, 1076.0, 352.4),
    "vinewood_sign": (726.0, 1198.0, 326.0),
    "legion_square": (215.8, -810.1, 30.7),
    "lsia_overlook": (-1034.6, -2733.6, 13.8),
    "sandy_shores": (1961.2, 3740.5, 32.3),
    "paleto_bay": (-275.5, 6635.8, 7.4),
}

# Curated stunt-jump approach points: (approach coord, heading hint). A stunt
# activity drives to the approach at speed; the jump itself is the road ahead.
STUNT_APPROACHES: tuple[dict[str, Any], ...] = (
    {"name": "lsia_terminal_ramp", "pos": (-1053.0, -2547.0, 13.8), "speed_mps": 32.0},
    {"name": "del_perro_fwy_ramp", "pos": (-2033.0, -302.0, 25.6), "speed_mps": 30.0},
    {"name": "vespucci_canal_hop", "pos": (-1161.0, -1427.0, 4.5), "speed_mps": 28.0},
    {"name": "elysian_dock_ramp", "pos": (155.0, -3067.0, 5.9), "speed_mps": 33.0},
)

CHAOS_BUDGET_PER_HOUR = 2.0


@dataclass(frozen=True)
class Activity:
    name: str
    weight: float
    cooldown_s: float
    chaos_cost: float
    mood_affinity: dict[str, float]  # multiplier per mood; missing mood => 1.0
    plan: tuple[dict[str, Any], ...]  # ordered decision-schema actions


def _drive_to(pos: tuple[float, float, float], speed: float, style: str, radius: float = 8.0) -> dict:
    x, y, z = pos
    return {
        "type": "drive_to",
        "params": {"x": x, "y": y, "z": z, "speed_mps": speed, "style": style, "arrive_radius_m": radius},
    }


def _waypoint(pos: tuple[float, float, float]) -> dict:
    return {"type": "set_waypoint", "params": {"x": pos[0], "y": pos[1]}}


CATALOG: tuple[Activity, ...] = (
    Activity(
        name="cruise_to_landmark",
        weight=5.0,
        cooldown_s=15 * 60,
        chaos_cost=0.0,
        mood_affinity={"chill": 1.5, "bored": 1.2},
        # Target landmark is chosen at plan time (see build_plan).
        plan=(),
    ),
    Activity(
        name="steal_nicer_car",
        weight=4.0,
        cooldown_s=10 * 60,
        chaos_cost=0.5,
        mood_affinity={"bored": 2.0, "smug": 1.3, "scared": 0.2},
        plan=(
            {"type": "enter_nearest_vehicle", "params": {"prefer": "nicer", "search_radius_m": 50}},
        ),
    ),
    Activity(
        name="stunt_jump",
        weight=2.0,
        cooldown_s=30 * 60,
        chaos_cost=0.5,
        mood_affinity={"hyped": 2.5, "bored": 1.5, "scared": 0.1},
        plan=(),  # approach chosen at plan time
    ),
    Activity(
        name="mount_chiliad_run",
        weight=1.5,
        cooldown_s=3 * 3600,
        chaos_cost=0.0,
        mood_affinity={"bored": 1.6, "chill": 1.2},
        plan=(
            _waypoint(LANDMARKS["mount_chiliad"]),
            _drive_to(LANDMARKS["mount_chiliad"], 22.0, "normal", 15.0),
        ),
    ),
    Activity(
        name="beach_pier",
        weight=3.0,
        cooldown_s=45 * 60,
        chaos_cost=0.0,
        mood_affinity={"chill": 1.8, "smug": 1.2},
        plan=(
            _waypoint(LANDMARKS["del_perro_pier"]),
            _drive_to(LANDMARKS["del_perro_pier"], 15.0, "normal"),
            {"type": "exit_vehicle", "params": {}},
            {"type": "walk_to", "params": {"x": -1850.1, "y": -1231.8, "z": 13.0, "run": False}},
            {"type": "wait", "params": {"seconds": 20}},
        ),
    ),
    Activity(
        name="deliberate_chase",
        weight=1.0,
        cooldown_s=60 * 60,
        chaos_cost=2.0,
        mood_affinity={"bored": 2.0, "hyped": 1.5, "scared": 0.0, "chill": 0.5},
        # The chase itself starts from what the tactical tier does at the scene
        # (speeding past cruisers, a horn verdict); the plan just gets him there
        # and commits. No combat, no targeting anyone — driving crime only.
        plan=(
            _drive_to(LANDMARKS["legion_square"], 25.0, "rushed"),
            {"type": "wander_drive", "params": {"style": "ignore_lights"}},
        ),
    ),
    Activity(
        name="park_and_watch",
        weight=3.0,
        cooldown_s=25 * 60,
        chaos_cost=0.0,
        mood_affinity={"chill": 1.6, "scared": 1.4, "bored": 0.7},
        plan=(
            _drive_to(LANDMARKS["galileo_observatory"], 14.0, "normal", 12.0),
            {"type": "stop", "params": {}},
            {"type": "look_around", "params": {}},
            {"type": "wait", "params": {"seconds": 25}},
        ),
    ),
    Activity(
        name="go_home",
        weight=1.5,
        cooldown_s=2 * 3600,
        chaos_cost=0.0,
        mood_affinity={"scared": 1.8, "chill": 1.1, "hyped": 0.3},
        # The live safehouse coordinate is supplied at plan time by the caller
        # (depends on the active protagonist); fallback is Michael's block.
        plan=(),
    ),
    Activity(
        name="visit_death_spot",
        weight=1.0,
        cooldown_s=90 * 60,
        chaos_cost=0.0,
        mood_affinity={"bored": 1.4, "smug": 1.3},
        # Only offered when a death spot exists (see ActivityPicker.pick).
        plan=(),
    ),
)

DEFAULT_SAFEHOUSE = (-852.4, 160.0, 65.6)


class ActivityPicker:
    """Weighted selection with cooldowns, mood affinity and a chaos budget."""

    def __init__(self, rng: random.Random | None = None, clock=time.monotonic) -> None:
        self._rng = rng or random.Random()
        self._clock = clock
        self._last_run: dict[str, float] = {}
        self._chaos_spent: list[tuple[float, float]] = []  # (ts, cost)
        self.last_death_pos: tuple[float, float, float] | None = None
        self.safehouse: tuple[float, float, float] = DEFAULT_SAFEHOUSE

    # -- chaos budget ----------------------------------------------------------

    def chaos_available(self) -> float:
        now = self._clock()
        self._chaos_spent = [(t, c) for (t, c) in self._chaos_spent if now - t < 3600.0]
        return CHAOS_BUDGET_PER_HOUR - sum(c for _, c in self._chaos_spent)

    # -- selection -------------------------------------------------------------

    def eligible(self, mood: str) -> list[tuple[Activity, float]]:
        now = self._clock()
        chaos = self.chaos_available()
        out: list[tuple[Activity, float]] = []
        for a in CATALOG:
            if now - self._last_run.get(a.name, -1e12) < a.cooldown_s:
                continue
            if a.chaos_cost > chaos:
                continue
            if a.name == "visit_death_spot" and self.last_death_pos is None:
                continue
            w = a.weight * a.mood_affinity.get(mood, 1.0)
            if w <= 0:
                continue
            out.append((a, w))
        return out

    def pick(self, mood: str) -> Activity | None:
        candidates = self.eligible(mood)
        if not candidates:
            return None
        total = sum(w for _, w in candidates)
        r = self._rng.uniform(0, total)
        acc = 0.0
        chosen = candidates[-1][0]
        for a, w in candidates:
            acc += w
            if r <= acc:
                chosen = a
                break
        self._last_run[chosen.name] = self._clock()
        if chosen.chaos_cost > 0:
            self._chaos_spent.append((self._clock(), chosen.chaos_cost))
        return chosen

    # -- plan materialization --------------------------------------------------

    def build_plan(self, activity: Activity, mood_style: str) -> list[dict[str, Any]]:
        if activity.name == "cruise_to_landmark":
            name = self._rng.choice(list(LANDMARKS))
            pos = LANDMARKS[name]
            return [_waypoint(pos), _drive_to(pos, 16.0, mood_style, 12.0)]
        if activity.name == "stunt_jump":
            jump = self._rng.choice(STUNT_APPROACHES)
            return [
                _waypoint(jump["pos"]),
                _drive_to(jump["pos"], 20.0, "normal", 10.0),
                _drive_to(jump["pos"], jump["speed_mps"], "rushed", 5.0),
            ]
        if activity.name == "go_home":
            return [_waypoint(self.safehouse), _drive_to(self.safehouse, 16.0, mood_style, 10.0)]
        if activity.name == "visit_death_spot":
            assert self.last_death_pos is not None
            return [
                _waypoint(self.last_death_pos),
                _drive_to(self.last_death_pos, 14.0, "normal", 10.0),
                {"type": "wait", "params": {"seconds": 10}},
            ]
        return list(activity.plan)
