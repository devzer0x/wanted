"""Core activity catalog (WP-H / master brief): what the agent does between missions.

Each activity is a small plan of bridge tasks/primitives with a weight, a
cooldown, a category, per-mood affinity, and a chaos cost drawn from an hourly
chaos budget (so "deliberate police chase" happens, but not three times an
hour).

Rhythm is the point. Three mechanisms shape it, and they are deliberately
different from each other:

* **cooldown** — how long before THIS activity may repeat (per activity).
* **chaos budget** — a rolling-hour allowance so loud activities stay rare no
  matter how the dice land. Sirens are seasoning.
* **category variety** — the last one or two categories are damped, so the show
  does not do three scenic drives in a row and call it a personality.

And a fourth, in :class:`ActivityRunner`: a jittered gap between activities, so
there is ordinary unscripted driving in between rather than a conveyor belt of
set pieces.

**Coordinate provenance (honest):** the landmark and stunt coordinates below are
curated from community mapping of the game world. They are the agent's mental map,
NOT verified against the running game — no one has driven to them yet. They are
scheduled for live tuning in Phase 3 (PLAN.md); until that has happened, treat
every number here as unverified. The shapes never change, the numbers may. A
wrong number degrades to a `drive_to` that ends somewhere odd or a task that
times out; both are handled (StuckDetector, task-failed deltas), neither is
silent.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

from ..bridge_client import BRIDGE_TASK_TYPES
from ..logsetup import get_logger

log = get_logger("wasted.activities")

# Landmarks (world-space meters). UNVERIFIED until Phase 3 — see module docstring.
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

# Curated stunt-jump approach points. A stunt activity drives to the approach at
# speed; the jump itself is the road ahead. UNVERIFIED until Phase 3.
STUNT_APPROACHES: tuple[dict[str, Any], ...] = (
    {"name": "lsia_terminal_ramp", "pos": (-1053.0, -2547.0, 13.8), "speed_mps": 32.0},
    {"name": "del_perro_fwy_ramp", "pos": (-2033.0, -302.0, 25.6), "speed_mps": 30.0},
    {"name": "vespucci_canal_hop", "pos": (-1161.0, -1427.0, 4.5), "speed_mps": 28.0},
    {"name": "elysian_dock_ramp", "pos": (155.0, -3067.0, 5.9), "speed_mps": 33.0},
)

CHAOS_BUDGET_PER_HOUR = 2.0

#: Categories exist so the picker can enforce variety. Two scenic drives back to
#: back is a screensaver; scenic → acquisition → scenic is a show.
CATEGORIES: tuple[str, ...] = ("scenic", "acquisition", "stunt", "errand", "trouble")

#: Weight multiplier applied to an activity whose category was the most recent
#: one, and the one before that.
CATEGORY_REPEAT_PENALTY = 0.35
CATEGORY_RECENT_PENALTY = 0.7
#: Weight multiplier for the exact activity that ran last (belt and braces on
#: top of its cooldown, for the long-cooldown-expired case).
SAME_ACTIVITY_PENALTY = 0.25
_CATEGORY_MEMORY = 2


@dataclass(frozen=True)
class Activity:
    name: str
    weight: float
    cooldown_s: float
    chaos_cost: float
    category: str
    mood_affinity: dict[str, float]  # multiplier per mood; missing mood => 1.0
    plan: tuple[dict[str, Any], ...]  # ordered decision-schema actions
    #: One line the brain sees while this runs, so commentary knows the bit.
    brief: str = ""


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
        category="scenic",
        mood_affinity={"chill": 1.5, "bored": 1.2},
        # Target landmark is chosen at plan time (see build_plan).
        plan=(),
        brief="driving somewhere worth looking at, no deadline",
    ),
    Activity(
        name="steal_nicer_car",
        weight=4.0,
        cooldown_s=10 * 60,
        chaos_cost=0.5,
        category="acquisition",
        mood_affinity={"bored": 2.0, "smug": 1.3, "scared": 0.2},
        plan=(
            {"type": "enter_nearest_vehicle", "params": {"prefer": "nicer", "search_radius_m": 50}},
        ),
        brief="upgrading the ride — the signature move",
    ),
    Activity(
        name="stunt_jump",
        weight=2.0,
        cooldown_s=30 * 60,
        chaos_cost=0.5,
        category="stunt",
        mood_affinity={"hyped": 2.5, "bored": 1.5, "scared": 0.1},
        plan=(),  # approach chosen at plan time
        brief="lining up a jump that physics will have opinions about",
    ),
    Activity(
        name="mount_chiliad_run",
        weight=1.5,
        cooldown_s=3 * 3600,
        chaos_cost=0.0,
        category="scenic",
        mood_affinity={"bored": 1.6, "chill": 1.2},
        plan=(
            _waypoint(LANDMARKS["mount_chiliad"]),
            _drive_to(LANDMARKS["mount_chiliad"], 22.0, "normal", 15.0),
        ),
        brief="the pilgrimage up Chiliad; the dirt road is the whole bit",
    ),
    Activity(
        name="beach_pier",
        weight=3.0,
        cooldown_s=45 * 60,
        chaos_cost=0.0,
        category="scenic",
        mood_affinity={"chill": 1.8, "smug": 1.2},
        plan=(
            _waypoint(LANDMARKS["del_perro_pier"]),
            _drive_to(LANDMARKS["del_perro_pier"], 15.0, "normal"),
            {"type": "exit_vehicle", "params": {}},
            {"type": "walk_to", "params": {"x": -1850.1, "y": -1231.8, "z": 13.0, "run": False}},
            {"type": "wait", "params": {"seconds": 20}},
        ),
        brief="parking at the pier to look at the ocean and pretend not to",
    ),
    Activity(
        name="deliberate_chase",
        weight=1.0,
        cooldown_s=60 * 60,
        chaos_cost=2.0,
        category="trouble",
        mood_affinity={"bored": 2.0, "hyped": 1.5, "scared": 0.0, "chill": 0.5},
        # The chase itself starts from what the tactical tier does at the scene
        # (speeding past cruisers, a horn verdict); the plan just gets him there
        # and commits. No combat, no targeting anyone — driving crime only.
        plan=(
            _drive_to(LANDMARKS["legion_square"], 25.0, "rushed"),
            {"type": "wander_drive", "params": {"style": "ignore_lights"}},
        ),
        brief="going downtown to drive badly in front of witnesses",
    ),
    Activity(
        name="park_and_watch",
        weight=3.0,
        cooldown_s=25 * 60,
        chaos_cost=0.0,
        category="scenic",
        mood_affinity={"chill": 1.6, "scared": 1.4, "bored": 0.7},
        plan=(
            _drive_to(LANDMARKS["galileo_observatory"], 14.0, "normal", 12.0),
            {"type": "stop", "params": {}},
            {"type": "look_around", "params": {}},
            {"type": "wait", "params": {"seconds": 25}},
        ),
        brief="the observatory balcony; the thinking spot, if he thought",
    ),
    Activity(
        name="go_home",
        weight=1.5,
        cooldown_s=2 * 3600,
        chaos_cost=0.0,
        category="errand",
        mood_affinity={"scared": 1.8, "chill": 1.1, "hyped": 0.3},
        # The live safehouse coordinate is supplied at plan time by the caller
        # (depends on the active protagonist); fallback is Michael's block.
        plan=(),
        brief="going home to reset the day",
    ),
    Activity(
        name="visit_death_spot",
        weight=1.0,
        cooldown_s=90 * 60,
        chaos_cost=0.0,
        category="errand",
        mood_affinity={"bored": 1.4, "smug": 1.3},
        # Only offered when a death spot exists (see ActivityPicker.pick).
        plan=(),
        brief="returning to the scene of the last embarrassment",
    ),
)

DEFAULT_SAFEHOUSE = (-852.4, 160.0, 65.6)

# --- governor L3: "asleep in the car" ----------------------------------------

#: Landmarks the agent is willing to fall asleep at. CONTRACTS §7 L3 says he "parks
#: somewhere scenic", so `stop` wherever he happens to be — which can be lane
#: three of the Los Santos Freeway — does not satisfy it. These are the scenic
#: landmarks (the ones the scenic-category activities already use), so L3 parks
#: the same places `park_and_watch` does, just at the nearest one.
SCENIC_PARK_SPOTS: tuple[str, ...] = (
    "galileo_observatory",
    "del_perro_pier",
    "vespucci_beach",
    "vinewood_sign",
    "lsia_overlook",
    "mount_chiliad",
    "paleto_bay",
    "sandy_shores",
)

#: How long the L3 drive-to-scenic gets before he gives up and stops where he
#: is. The budget window is an hour; spending more than three minutes of it
#: driving to a view defeats the point, and an unreachable curated coordinate
#: (Phase 3 risk) must not leave him circling.
L3_PARK_TIMEOUT_S = 180.0

#: Speed for the L3 drive. Slow: he is out of money, not in a hurry.
L3_PARK_SPEED_MPS = 14.0


def nearest_scenic_spot(
    pos: tuple[float, float, float],
) -> tuple[str, tuple[float, float, float], float]:
    """Nearest scenic parking landmark to `pos`: (name, coords, planar metres).

    Planar (XY) distance, matching the bridge's arrival rule (STATUS.md:
    "drive/walk arrival = planar (XY) distance").
    """
    x, y, _z = pos
    best_name = SCENIC_PARK_SPOTS[0]
    best_dist = float("inf")
    for name in SCENIC_PARK_SPOTS:
        lx, ly, _lz = LANDMARKS[name]
        dist = ((lx - x) ** 2 + (ly - y) ** 2) ** 0.5
        if dist < best_dist:
            best_name, best_dist = name, dist
    return best_name, LANDMARKS[best_name], best_dist


def scenic_park_plan(
    pos: tuple[float, float, float], style: str = "normal"
) -> tuple[str, list[dict[str, Any]]]:
    """The L3 park: drive to the nearest scenic spot, then stop.

    Returns (spot name, [drive_to, stop]). Both are ordinary bridge tasks — the
    engine does the driving, so parking scenically costs no model calls, which
    is the whole point of L3.
    """
    name, coords, _dist = nearest_scenic_spot(pos)
    return name, [
        _drive_to(coords, L3_PARK_SPEED_MPS, style, 15.0),
        {"type": "stop", "params": {}},
    ]


class ActivityPicker:
    """Weighted selection with cooldowns, mood affinity, chaos budget and variety.

    :meth:`peek` is pure (safe to call every tick, e.g. to show the brain what
    is available); :meth:`commit` books the cooldown and the chaos spend;
    :meth:`pick` is peek+commit for callers that act immediately.
    """

    def __init__(self, rng: random.Random | None = None, clock=time.monotonic) -> None:
        self._rng = rng or random.Random()
        self._clock = clock
        self._last_run: dict[str, float] = {}
        self._chaos_spent: list[tuple[float, float]] = []  # (ts, cost)
        self._recent_categories: list[str] = []
        self._last_name: str | None = None
        self.last_death_pos: tuple[float, float, float] | None = None
        self.safehouse: tuple[float, float, float] = DEFAULT_SAFEHOUSE

    # -- chaos budget ----------------------------------------------------------

    def chaos_available(self) -> float:
        now = self._clock()
        self._chaos_spent = [(t, c) for (t, c) in self._chaos_spent if now - t < 3600.0]
        return CHAOS_BUDGET_PER_HOUR - sum(c for _, c in self._chaos_spent)

    # -- selection -------------------------------------------------------------

    def _variety_multiplier(self, activity: Activity) -> float:
        m = 1.0
        recent = self._recent_categories[-_CATEGORY_MEMORY:]
        if recent and recent[-1] == activity.category:
            m *= CATEGORY_REPEAT_PENALTY
        elif activity.category in recent:
            m *= CATEGORY_RECENT_PENALTY
        if self._last_name == activity.name:
            m *= SAME_ACTIVITY_PENALTY
        return m

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
            w = a.weight * a.mood_affinity.get(mood, 1.0) * self._variety_multiplier(a)
            if w <= 0:
                continue
            out.append((a, w))
        return out

    def peek(self, mood: str, prefer: str | None = None) -> Activity | None:
        """Choose without booking anything. Pure apart from the RNG draw.

        `prefer` is the day planner's idea for this roam block (behavior/planner.py).
        It is honoured only when that activity is genuinely ELIGIBLE — cooldown,
        chaos budget and the death-spot precondition still decide. A preference
        that cannot be honoured falls through to the normal weighted draw
        silently, and the planner reports what actually started rather than what
        it asked for, so the plan a viewer reads is never a claim the runner
        refused.
        """
        candidates = self.eligible(mood)
        if not candidates:
            return None
        if prefer is not None:
            for activity, _weight in candidates:
                if activity.name == prefer:
                    return activity
        total = sum(w for _, w in candidates)
        r = self._rng.uniform(0, total)
        acc = 0.0
        chosen = candidates[-1][0]
        for a, w in candidates:
            acc += w
            if r <= acc:
                chosen = a
                break
        return chosen

    def commit(self, activity: Activity) -> None:
        """Book the cooldown, the chaos spend and the variety memory."""
        self._last_run[activity.name] = self._clock()
        self._last_name = activity.name
        self._recent_categories.append(activity.category)
        self._recent_categories = self._recent_categories[-_CATEGORY_MEMORY:]
        if activity.chaos_cost > 0:
            self._chaos_spent.append((self._clock(), activity.chaos_cost))
        log.info(
            "activity selected",
            extra={
                "kv": {
                    "activity": activity.name,
                    "category": activity.category,
                    "chaos_cost": activity.chaos_cost,
                    "chaos_left": round(self.chaos_available(), 2),
                }
            },
        )

    def pick(self, mood: str, prefer: str | None = None) -> Activity | None:
        chosen = self.peek(mood, prefer)
        if chosen is not None:
            self.commit(chosen)
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
            if self.last_death_pos is None:
                # Only reachable if a caller built a plan for an activity that
                # eligible() would have filtered out; say so instead of crashing.
                raise ValueError(
                    "visit_death_spot has no death position yet; it is only "
                    "eligible after a real death"
                )
            return [
                _waypoint(self.last_death_pos),
                _drive_to(self.last_death_pos, 14.0, "normal", 10.0),
                {"type": "wait", "params": {"seconds": 10}},
            ]
        return list(activity.plan)


# --- runner -------------------------------------------------------------------

#: Jittered quiet stretch between activities. Without it the show is a conveyor
#: belt of set pieces; with it there is ordinary driving to talk over.
ACTIVITY_GAP_S = (150.0, 420.0)

#: A plan step that has not completed in this long is abandoned. Guards against
#: an unverified coordinate that the engine can never path to (Phase 3 risk).
STEP_TIMEOUT_S = 210.0

#: Terminal states of the bridge's single-task lifecycle (CONTRACTS §1).
_TASK_DONE = ("idle", "done", "failed")


@dataclass
class RunningActivity:
    activity: Activity
    plan: list[dict[str, Any]]
    step: int = 0
    started_at: float = 0.0
    step_started_at: float = 0.0

    @property
    def finished(self) -> bool:
        return self.step >= len(self.plan)


class ActivityRunner:
    """Drives one activity plan at a time, one step per completed bridge task.

    The brain always outranks the runner: any decision that posts its own
    bridge task preempts the running step, which the runner notices (the task
    id changed) and treats as the activity being cut short. That is the
    intended relationship — the runner supplies rhythm when nobody is steering,
    the brain supplies intent whenever it has one.
    """

    def __init__(
        self,
        picker: ActivityPicker,
        rng: random.Random | None = None,
        clock=time.monotonic,
    ) -> None:
        self._picker = picker
        self._rng = rng or random.Random()
        self._clock = clock
        self.current: RunningActivity | None = None
        self._next_allowed_at = clock() + self._rng.uniform(*ACTIVITY_GAP_S)
        #: The task id POST /task returned for the step now running, or None
        #: when the step is a harness primitive that posts no task.
        self._step_task_id: str | None = None
        #: Whether the step now running posted a bridge task at all.
        self._step_expects_task = False

    # -- lifecycle -------------------------------------------------------------

    def due(self) -> bool:
        return self.current is None and self._clock() >= self._next_allowed_at

    def start(
        self, mood: str, mood_style: str, prefer: str | None = None
    ) -> tuple[Activity, dict[str, Any]] | None:
        """Pick an activity and return it with its first action, or None.

        `prefer` comes from the day planner's current roam block; see
        :meth:`ActivityPicker.peek` for why it is a preference and not an order.
        """
        activity = self._picker.peek(mood, prefer)
        if activity is None:
            # Nothing eligible: try again after a short gap rather than
            # hammering the picker every tick.
            self._next_allowed_at = self._clock() + ACTIVITY_GAP_S[0]
            return None
        plan = self._picker.build_plan(activity, mood_style)
        if not plan:
            self._next_allowed_at = self._clock() + ACTIVITY_GAP_S[0]
            return None
        self._picker.commit(activity)
        now = self._clock()
        self.current = RunningActivity(
            activity=activity, plan=plan, started_at=now, step_started_at=now
        )
        self._step_task_id = None
        self._step_expects_task = False
        return activity, plan[0]

    def bind_step_task(self, task_id: str | None) -> None:
        """Record the id ``POST /task`` returned for the step just issued.

        This is the authoritative identity of the step's task and the only thing
        :meth:`next_step` matches on. The runner used to latch whatever
        ``last_task.id`` turned up in the next snapshot instead — at a 2-4 Hz
        poll against a 60 Hz game thread that snapshot can still carry the
        PREVIOUS task, so the runner would bind to the wrong id and then treat
        the old task's `done` as this step completing, skipping a step per
        activity.

        `task_id` is None for a step that posts no bridge task (a primitive such
        as `look_around` or `wait`); those advance on the next tick that is not
        mid-task.
        """
        running = self.current
        if running is None or running.finished:
            return
        step_type = running.plan[running.step]["type"]
        self._step_expects_task = step_type in BRIDGE_TASK_TYPES
        self._step_task_id = task_id

    def next_step(self, task_status: str, task_id: str | None) -> dict[str, Any] | None:
        """Advance when the current step's task finished. None means "keep going".

        `task_status` / `task_id` come straight from ``/state.last_task``;
        `task_id` is None until a task has ever been posted (CONTRACTS v1.2).
        """
        running = self.current
        if running is None:
            return None
        now = self._clock()
        timed_out = now - running.step_started_at > STEP_TIMEOUT_S
        if not timed_out:
            if self._step_expects_task:
                if self._step_task_id is None:
                    # The step's POST never returned an id (bridge down at the
                    # moment it was issued). Nothing will ever complete it; let
                    # the timeout below be the only way out.
                    return None
                if task_id != self._step_task_id:
                    # Either our task has not reached a snapshot yet (poll lag),
                    # or somebody else's task is on the wheel. Only a *running*
                    # foreign task is preemption; a foreign terminal state is
                    # just the previous task still being reported.
                    if task_status == "running":
                        self.abandon("preempted")
                    return None
                if task_status not in _TASK_DONE:
                    return None
            elif task_status == "running":
                # A primitive step with someone else's task still running: wait
                # rather than racing ahead of it.
                return None
        else:
            log.warning(
                "activity step timed out",
                extra={
                    "kv": {
                        "activity": running.activity.name,
                        "step": running.step,
                        "timeout_s": STEP_TIMEOUT_S,
                        "task_id": self._step_task_id,
                    }
                },
            )
        running.step += 1
        running.step_started_at = now
        self._step_task_id = None
        self._step_expects_task = False
        if running.finished:
            return None
        return running.plan[running.step]

    def finish(self, outcome: str) -> tuple[str, dict[str, Any]] | None:
        """End the current activity; returns the §4 `activity_end` payload."""
        running, self.current = self.current, None
        self._step_task_id = None
        self._step_expects_task = False
        self._next_allowed_at = self._clock() + self._rng.uniform(*ACTIVITY_GAP_S)
        if running is None:
            return None
        payload = {
            "activity": running.activity.name,
            "outcome": outcome,
            "duration_s": round(self._clock() - running.started_at, 1),
        }
        log.info("activity ended", extra={"kv": dict(payload)})
        return running.activity.name, payload

    def abandon(self, reason: str) -> tuple[str, dict[str, Any]] | None:
        return self.finish(reason)

    # -- reporting -------------------------------------------------------------

    def note(self) -> str:
        """One line for the brain's dynamic context."""
        running = self.current
        if running is None:
            return "ACTIVITY: none running (free time — the goal is yours)."
        return (
            f"ACTIVITY: {running.activity.name} step {running.step + 1}/"
            f"{len(running.plan)} — {running.activity.brief}. Narrate it as your "
            f"own idea; post your own task whenever you want it to stop."
        )
