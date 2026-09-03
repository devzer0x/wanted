"""DayPlanner: what the agent does with his day — ROAM blocks and MISSION blocks.

The gap this closes. Until now nothing in the harness ever decided to go and
*start* a job. The director set a free-form goal every 60-120 s, the
:class:`~behavior.activities.ActivityRunner` picked free-roam set pieces, and
:class:`~behavior.missions.MissionFollower` chased an objective blip — but only
once a mission was ALREADY active. Missions therefore only happened if the
brain happened to drive through a marker by accident. CONTRACTS v1.7's
`mission.starts[]` is the missing fact (where the M/F/T letter blips are); this
module is the missing decision (when to go and stand in one).

**Structure, not a model call.** Every choice here is a comparison against a
named constant plus a dice roll for block length and which idea he fancies. It
costs nothing, it runs at tick rate, and it is deterministic enough to test with
a fake clock. The brain's job is what he *says and thinks* about the plan, not
whether he has one — so the planner never writes `current_goal` (that stays the
director's) and instead publishes its intent through :meth:`note`, the same
idiom as ``MissionFollower.note()`` and ``ActivityRunner.note()``.

**The day is two kinds of block, alternating.**

* **ROAM block** (6-12 min): a fun goal, held. The planner names the idea
  (:data:`ROAM_IDEAS`) and hands it to the ActivityRunner as a *preference*;
  the runner still owns cooldowns, the chaos budget and category variety, so an
  idea that is on cooldown quietly becomes whatever the picker fancies instead,
  and the planner then reports what actually started rather than what it asked
  for. The planner posts **no tasks at all** during a roam block — the runner
  and the brain already drive that half, and two things steering is how you get
  a task posted and preempted in the same tick.
* **MISSION block**: pick the nearest `mission.starts[]` marker whose
  protagonist matches `player.protagonist`, and go and stand in it. Entering
  the corona is what starts the job (CONTRACTS v1.7) — no keypress, no
  harness-side "start mission" call, nothing a human on the same couch could
  not do with the same two hands (CLAUDE.md rule 5). The moment
  `mission.active` flips true the planner steps back entirely and
  MissionFollower plus the mission card take over.

**Who has the wheel.** The planner is the lowest-priority thing that posts a
task. It defers to a foreign `running` task exactly the way MissionFollower
does (bind the id ``POST /task`` returned; never trust the id in the next
snapshot), it refuses to act while he is dead, arrested, wanted or mid-cutscene,
and `main` gates the ActivityRunner, the L2 wander reflex and the stranded
escalator off for the duration of a mission block so nothing fights it.

**Events.** Only §4 types, and only the pair that already exists for a bounded
undertaking: `activity_start` / `activity_end` around the trip to a marker
(`activity: "go_start_a_job"`, and the outcome says how it ended — the job
started, the marker never triggered, he gave up, or something went wrong).
Roam blocks emit nothing of their own: the activities running *inside* them
already emit that same pair, and a second overlapping pair for the container
would double-count every roam block in the site's feed.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..bridge_client import UNKNOWN_PROTAGONIST, GameState, MissionStart
from ..logsetup import get_logger
from .navigation import WALK_ARRIVE_RADIUS_M, navigate_to, planar_distance
from .recovery import HOSTILE_CLOSE_RADIUS_M
from .roam import ROAM_BEFORE_MISSION_S

log = get_logger("wasted.planner")


# --- tuning constants ---------------------------------------------------------
#
# Every threshold below is named and carries the reason it has that value.

#: How long a roam block is held before the planner reconsiders. Long enough for
#: one activity plus the ordinary driving either side of it (ActivityRunner's own
#: gap between activities is already 150-420 s); short enough that a streamed
#: hour is never one single idea.
ROAM_BLOCK_S = (6 * 60.0, 12 * 60.0)

#: A roam block is never cut shorter than this, whatever the mood says. Without
#: a floor, one bored tick ten seconds after a mission ends sends him straight
#: back to the marker he just walked out of and the whole alternation is
#: decoration.
ROAM_MIN_BLOCK_S = 120.0

#: Prefer a mission when the last one started longer ago than this.
#:
#: IMPORTED, not chosen: it is `behavior.roam.RoamCadence.force_after_s`'s default,
#: which is the hard deadline after which `start_nearest_mission` is the ONLY goal
#: the roam engine will offer. Two numbers for the same idea meant the planner
#: still thought there were minutes of roam left at the moment the roam engine had
#: already stopped offering anything to do with them — so there is now one number
#: and this name is an alias for it.
#:
#: Retuned in T7 from 15 min to 40 along with the roam side. The old pair (3
#: completed goals OR 15 minutes, both FORCING) turned a free-roam stream into a
#: mission queue; the goal counter is now an OFFER (`RoamEngine.mission_offered`)
#: and only this clock forces anything.
MISSION_OVERDUE_S = ROAM_BEFORE_MISSION_S

#: Roam blocks owed after any mission ends: one. Win or lose, a person does not
#: walk straight back into the next job — he drives off, and that beat is where
#: the commentary about what just happened lives.
ROAM_BLOCKS_AFTER_A_MISSION = 1

#: Consecutive mission failures before the planner stops going back for a full
#: roam block. The game's own Retry/Skip prompt is not ours to press (Story
#: Mode, no cheats), so the only honest way out of a job he cannot clear is to
#: leave and do something else for a while.
MAX_CONSECUTIVE_MISSION_FAILS = 3
FAIL_BACKOFF_BLOCKS = 1

#: Health floor for starting a job, as a fraction of `max_health`. Walking into
#: a mission at 20% health is a death and a fail; a human would find a burger
#: (or just drive around) until the regen caught up.
MIN_HEALTH_FRACTION_TO_START = 0.5

#: How long the trip to a marker may take before the planner gives up and roams
#: instead. Los Santos corner to corner is roughly five minutes at speed, so
#: past this the marker is effectively unreachable (blocked route, stale blip)
#: and retrying forever is exactly the loop this class exists to prevent.
MISSION_BLOCK_TIMEOUT_S = 6 * 60.0

#: Once standing in the marker, how long to wait for the game to start the job
#: before concluding it is not going to. A corona triggers within a frame or
#: two; 45 s is generous and still ends the block long before the 6 min
#: timeout leaves him standing on a pavement doing nothing.
MARKER_WAIT_S = 45.0

#: Within this range of the marker, park and finish on foot. CONTRACTS v1.7
#: says walking OR driving into a start marker begins the mission, so this is
#: not superstition about which one works — it is arithmetic: `drive_to` stops
#: at its 8 m arrival radius (§1), which is wider than the corona, while
#: `walk_to` finishes within 2 m, which is inside it.
MARKER_FINAL_APPROACH_M = 25.0

#: Re-pick the target marker only when the chosen one has moved (or vanished)
#: by more than this. Same idea as MissionFollower's TARGET_MOVED_THRESHOLD_M:
#: two markers at nearly equal distance must not make him oscillate between
#: them, re-posting a drive task every tick.
MARKER_MOVED_THRESHOLD_M = 5.0

#: The §4 `activity` payload name for the trip to a marker. Not a
#: behavior.activities catalog entry — it is the planner's own undertaking, and
#: the payload field is free text (the closed enum is on `events.type`).
GO_START_A_JOB = "go_start_a_job"


@dataclass(frozen=True)
class RoamIdea:
    """One fun thing to do with a roam block.

    `activity` is a real :data:`behavior.roam.CATALOG` goal id — the planner
    does not invent behaviour, it chooses among what the roam engine can already
    run AND VERIFY, so the idea it announces is the idea the engine will actually
    execute (when that goal's `needs`, cooldown and chaos budget allow it).
    `goal` is how the agent would put it, ≤12 words, matching the decision schema's
    goal field.

    Retargeted from the old `behavior.activities` catalog when free-roam picking
    moved to `behavior.roam`: an idea naming an activity the roam engine has
    never heard of is a plan the show cannot keep, which is exactly what the
    accompanying test refuses to allow.
    """

    activity: str
    goal: str


#: The curated day-plan menu. Every entry names a real roam goal id, so nothing
#: here is a promise the harness cannot keep — and every one of those goals has a
#: `done_when` the code can evaluate, so "the plan said he would do X" is now a
#: checkable claim rather than narration.
ROAM_IDEAS: tuple[RoamIdea, ...] = (
    RoamIdea("steal_nice_car", "upgrade the ride to something with a name"),
    RoamIdea("drive_to_landmark", "drive somewhere worth looking at"),
    RoamIdea("bike_hills", "find a bike and take it up a hill"),
    RoamIdea("freeway_run", "open it up and cover some real ground"),
    RoamIdea("steal_cop_car", "borrow a police car nobody is using"),
    RoamIdea("hijack_bus", "take the bus, and I mean the whole bus"),
    RoamIdea("earn_two_stars", "get the police genuinely interested"),
    RoamIdea("random_event", "see what this is before it stops happening"),
    RoamIdea("start_nearest_mission", "go and start the nearest job"),
)

#: What a roam block is when the runner had nothing eligible to offer. Honest:
#: he is driving around, and that is the whole plan.
GENERIC_ROAM_GOAL = "just drive and see what the city offers"

_IDEAS_BY_ACTIVITY: dict[str, RoamIdea] = {idea.activity: idea for idea in ROAM_IDEAS}

#: World space: +Y is north, +X is east (the game's map convention, the same one
#: the curated LANDMARKS table is written in). Unverified in-game like every
#: other coordinate claim in this package — but a wrong compass label only
#: mislabels a sentence: navigation always uses the raw `pos`, never the word.
_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def compass_bearing(dx: float, dy: float) -> str:
    """Eight-point compass label for a world-space delta (dx east, dy north)."""
    if dx == 0.0 and dy == 0.0:
        return "here"
    angle = math.degrees(math.atan2(dx, dy)) % 360.0
    return _COMPASS[int(((angle + 22.5) % 360.0) // 45.0)]


class Block(Enum):
    """The two kinds of block a day alternates between."""

    ROAM = "roam"
    MISSION = "mission"


class _Phase(Enum):
    """Internal detail: a MISSION block has a travelling half and a live half."""

    ROAM = "roam"
    TO_MISSION = "to_mission"
    IN_MISSION = "in_mission"


@dataclass
class PlannerOutcome:
    """What one :meth:`DayPlanner.update` produced.

    `task` is a decision-schema action the caller should post (and then hand the
    returned id back via :meth:`DayPlanner.bind_task`); `events` are §4
    ``(type, payload)`` pairs for the writer. The planner never touches the
    bridge or Supabase itself — main owns both, and keeping it that way is what
    makes this class testable without a game.
    """

    task: dict[str, Any] | None = None
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class DayPlanner:
    """Alternates roam blocks and mission blocks; decides when to go start a job."""

    def __init__(
        self,
        rng: random.Random | None = None,
        clock: Any = time.monotonic,
        *,
        missions_enabled: bool = True,
    ) -> None:
        self._rng = rng or random.Random()
        #: Operator switch. Off: no mission block is ever scheduled or requested.
        self._missions_enabled = missions_enabled
        self._clock = clock

        self._phase = _Phase.ROAM
        self._block_started_at = self._clock()
        self._block_length_s = self._rng.uniform(*ROAM_BLOCK_S)

        #: The idea this roam block asked for, and the one that actually
        #: started. `note()` reports the second when it exists, so the plan a
        #: viewer reads is never a claim about something the runner refused.
        self._roam_preference: RoamIdea = self._rng.choice(ROAM_IDEAS)
        self._roam_actual: RoamIdea | None = None
        self._activity_running = False

        #: When a mission last STARTED. None = not yet this session, which is
        #: deliberately treated as "overdue": the show should get to a job
        #: reasonably soon after it comes up.
        self._last_mission_at: float | None = None
        self._consecutive_fails = 0
        self._failed_this_mission = False
        self._roam_blocks_owed = 0
        self._owed_reason = ""

        #: Set by :meth:`request_mission_block` when the roam engine has run out
        #: of patience (three verified goals, or fifteen minutes). None the rest
        #: of the time. It ends the roam block early; it does NOT bypass
        #: `_mission_block_allowed`, so a request made with stars up or at low
        #: health still waits — a forced job he cannot start is not a plan.
        self._mission_requested: str | None = None

        # MISSION block state.
        self._marker: tuple[float, float, float] | None = None
        self._marker_protagonist = UNKNOWN_PROTAGONIST
        self._mission_deadline = 0.0
        self._trip_started_at = 0.0
        self._arrived_at: float | None = None
        self._bound_task_id: str | None = None

        #: Cached from the last `update()` so `note()` can render without a
        #: state argument (the same no-argument shape as the other notes). At
        #: 2-4 Hz this is at most a few hundred ms stale, against blocks
        #: measured in minutes.
        self._player_pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._next_marker: MissionStart | None = None
        self._blocked_reason = ""

    # -- public state ----------------------------------------------------------

    @property
    def block(self) -> Block:
        return Block.ROAM if self._phase is _Phase.ROAM else Block.MISSION

    @property
    def in_mission_block(self) -> bool:
        """True while the day plan is on a job — travelling to a marker or
        standing back because one is live. `main` gates the ActivityRunner, the
        L2 wander reflex and the stranded escalator on this."""
        return self._phase is not _Phase.ROAM

    @property
    def roam_preference(self) -> str | None:
        """The catalog activity this roam block would like, or None outside one."""
        return self._roam_preference.activity if self._phase is _Phase.ROAM else None

    # -- things `main` tells the planner ---------------------------------------

    def roam_activity_started(self, activity_name: str) -> None:
        """An ActivityRunner activity just started; adopt it as the block's idea.

        The runner may have overridden the preference (cooldown, chaos budget,
        variety damping). Reporting what actually started rather than what was
        asked for is the difference between a plan and a press release.
        """
        self._activity_running = True
        self._roam_actual = _IDEAS_BY_ACTIVITY.get(activity_name)

    def roam_activity_ended(self) -> None:
        self._activity_running = False
        self._roam_actual = None

    def request_mission_block(self, reason: str) -> None:
        """The roam engine is asking to go and start a job now.

        The forced-mission rule ("the ONLY option after 3 completed roam goals or
        15 minutes") lives in :mod:`behavior.roam`, which is where the goal
        counter is. Acting on it lives here, because the trip to a marker is
        already built here end to end — the drive/walk/get-a-car ladder and the
        25 m on-foot final approach the corona needs. Two implementations of that
        trip would be two engines posting into the same slot.
        """
        if not self._missions_enabled:
            return
        if self._phase is not _Phase.ROAM or self._mission_requested == reason:
            return
        self._mission_requested = reason
        log.info("day plan: mission block requested", extra={"kv": {"reason": reason}})

    def observe_mission_event(self, event_type: str) -> None:
        """Feed §4 mission events (`main._handle_mission_events` calls this)."""
        if event_type == "mission_start":
            self._last_mission_at = self._clock()
            self._failed_this_mission = False
        elif event_type == "mission_fail":
            self._failed_this_mission = True
            self._consecutive_fails += 1
            if self._consecutive_fails >= MAX_CONSECUTIVE_MISSION_FAILS:
                self._owe_roam_blocks(
                    FAIL_BACKOFF_BLOCKS,
                    f"{self._consecutive_fails} failed attempts in a row",
                )
                log.info(
                    "day plan: backing off missions after repeated failures",
                    extra={"kv": {"fails": self._consecutive_fails,
                                  "roam_blocks": FAIL_BACKOFF_BLOCKS}},
                )

    def bind_task(self, task_id: str | None) -> None:
        """Record the id ``POST /task`` returned for the navigation just issued.

        Only this id is matched against `last_task` — never whatever the next
        snapshot happens to carry, which at a 2-4 Hz poll against a 60 Hz game
        thread can still be the previous task (the same trap MissionFollower and
        ActivityRunner already document).
        """
        self._bound_task_id = task_id

    # A game restart is handled the way `main._on_game_restart` handles every
    # other stateful observer: a fresh instance, because a cached marker from a
    # dead world is worse than no plan at all. There is deliberately no
    # `reset()` — one way to do it, and it is the one main already uses.

    # -- the tick --------------------------------------------------------------

    def update(self, state: GameState, mood: str, in_mission: bool) -> PlannerOutcome:
        """Advance the day plan one tick.

        `in_mission` is ``MissionTracker.in_mission`` (cutscene or objective
        phase), i.e. the authority on whether a job is live — not
        `state.mission.active` alone, so a cutscene counts.
        """
        out = PlannerOutcome()
        now = self._clock()
        p = state.player
        self._player_pos = (p.pos.x, p.pos.y, p.pos.z)
        self._next_marker = self._nearest_eligible(state)
        # Evaluated every tick, not only at a block boundary, so `note()` can
        # tell the brain WHY the next thing is another roam block instead of
        # silently promising a job that this moment would not allow.
        self._mission_block_allowed(state)

        # 1. A live job outranks the plan completely.
        if in_mission:
            if self._phase is _Phase.TO_MISSION:
                out.events.append(self._end_trip("mission_started"))
            if self._phase is not _Phase.IN_MISSION:
                self._phase = _Phase.IN_MISSION
                self._block_started_at = now
                self._failed_this_mission = False
                if self._last_mission_at is None:
                    self._last_mission_at = now
                log.info("day plan: mission block — the job is live, standing back")
            return out

        # 2. The job just ended: one roam block owed, win or lose.
        if self._phase is _Phase.IN_MISSION:
            if not self._failed_this_mission:
                self._consecutive_fails = 0
            self._owe_roam_blocks(
                ROAM_BLOCKS_AFTER_A_MISSION,
                "the job just ended" if not self._failed_this_mission else "that one hurt",
            )
            self._failed_this_mission = False
            self._start_roam_block(now)
            return out

        # 3. Travelling to a marker.
        if self._phase is _Phase.TO_MISSION:
            self._advance_trip(state, now, out)
            return out

        # 4. Roam block: hold it, then decide what comes next.
        if not self._roam_block_over(now, mood):
            return out
        self._serve_one_owed_block()
        # Re-checked after serving the owed block: serving it is exactly what
        # can turn "not allowed" into "allowed".
        marker = self._next_marker if self._mission_block_allowed(state) else None
        if marker is None:
            self._start_roam_block(now)
            return out
        self._begin_trip(marker, now)
        out.events.append(
            (
                "activity_start",
                {
                    "activity": GO_START_A_JOB,
                    "params": {
                        "x": marker.pos.x,
                        "y": marker.pos.y,
                        "z": marker.pos.z,
                        "protagonist": marker.protagonist,
                    },
                },
            )
        )
        out.task = self._navigation(state)
        return out

    # -- mission block ---------------------------------------------------------

    def _threat_close(self, state: GameState) -> bool:
        """A hostile inside the threat reflex's own radius. Reused from
        behavior.recovery rather than re-guessed, so "close enough to matter"
        means one thing in this package."""
        return any(
            ped.relationship == "hostile" and ped.distance <= HOSTILE_CLOSE_RADIUS_M
            for ped in state.nearby.peds
        )

    def _advance_trip(self, state: GameState, now: float, out: PlannerOutcome) -> None:
        reason = self._trip_abort_reason(state)
        if reason is None and self._threat_close(state):
            # The survival ladder owns this tick. Posting navigation now would
            # preempt the combat/break-contact task the threat reflex issued a
            # few milliseconds ago from this same snapshot. The block is NOT
            # abandoned — a firefight is seconds, the trip is minutes, and the
            # block deadline keeps running so a long one still ends it. Checked
            # AFTER the abort reasons so a death next to a hostile still ends
            # the block properly instead of pausing forever.
            self._arrived_at = None
            return
        if reason is None and now >= self._mission_deadline:
            reason = "gave_up"
        if reason is None:
            self._retarget(state)
            if self._marker is None:
                reason = "marker_gone"
        if reason is None and self._arrived_at is not None and now - self._arrived_at > MARKER_WAIT_S:
            # Standing in it and nothing happened. The corona is not going to
            # trigger (wrong protagonist, blip we misread, mission unavailable);
            # say so honestly and go and do something else.
            reason = "marker_did_not_trigger"
        if reason is not None:
            out.events.append(self._end_trip(reason))
            self._start_roam_block(now)
            return

        assert self._marker is not None
        distance = planar_distance(self._player_pos, self._marker)
        if not state.player.in_vehicle and distance <= WALK_ARRIVE_RADIUS_M:
            if self._arrived_at is None:
                self._arrived_at = now
        else:
            self._arrived_at = None
        out.task = self._navigation(state)

    def _trip_abort_reason(self, state: GameState) -> str | None:
        """Why the trip must stop right now, or None to keep going."""
        p = state.player
        if p.dead or p.arrested:
            return "went_down"
        if p.wanted > 0:
            # Turning up to a job with a helicopter overhead is how you fail it
            # in the first thirty seconds. Lose them first, then come back.
            return "wanted"
        if state.mission.cutscene_active:
            return "cutscene"
        if state.player.switch_in_progress:
            return "switch_in_progress"
        if state.mission.retry_in_flight:
            return "retry_in_flight"
        return None

    def _begin_trip(self, marker: MissionStart, now: float) -> None:
        self._phase = _Phase.TO_MISSION
        self._mission_requested = None
        self._marker = (marker.pos.x, marker.pos.y, marker.pos.z)
        self._marker_protagonist = marker.protagonist
        self._block_started_at = now
        self._trip_started_at = now
        self._mission_deadline = now + MISSION_BLOCK_TIMEOUT_S
        self._arrived_at = None
        self._bound_task_id = None
        self._roam_actual = None
        log.info(
            "day plan: mission block — going to start a job",
            extra={
                "kv": {
                    "protagonist": marker.protagonist,
                    "distance_m": round(planar_distance(self._player_pos, self._marker), 1),
                    "bearing": self._marker_bearing(),
                }
            },
        )

    def _end_trip(self, outcome: str) -> tuple[str, dict[str, Any]]:
        payload = {
            "activity": GO_START_A_JOB,
            "outcome": outcome,
            "duration_s": round(self._clock() - self._trip_started_at, 1),
        }
        log.info("day plan: mission block ended", extra={"kv": dict(payload)})
        self._marker = None
        self._arrived_at = None
        self._bound_task_id = None
        return "activity_end", payload

    def _navigation(self, state: GameState) -> dict[str, Any] | None:
        """The next navigation task, or None to leave the wheel alone this tick."""
        if self._marker is None:
            return None
        lt = state.last_task
        if self._bound_task_id is not None:
            if lt.id == self._bound_task_id:
                if lt.status == "running":
                    return None  # our own navigation is already under way
                self._bound_task_id = None  # done/failed: reconsider below
            elif lt.status == "running":
                return None  # someone else has the wheel; do not fight them
        elif lt.status == "running":
            return None  # a task we never bound is running
        return navigate_to(
            self._player_pos,
            state.player.in_vehicle,
            self._marker,
            final_approach_on_foot_m=MARKER_FINAL_APPROACH_M,
        )

    # -- marker selection ------------------------------------------------------

    def _eligible(self, state: GameState) -> list[MissionStart]:
        """Markers he could actually start, nearest first.

        A protagonist's letter blips are startable by that protagonist. When
        `player.protagonist` is `unknown` (pre-v1.7 bridge, or the bridge could
        not tell) every marker is fair game — the alternative is never starting
        a mission at all. A marker whose own protagonist is `unknown` is always
        included for the same reason: there is no information to exclude it on.
        """
        who = state.player.protagonist
        starts = list(state.mission.starts)
        if who != UNKNOWN_PROTAGONIST:
            starts = [s for s in starts if s.protagonist in (who, UNKNOWN_PROTAGONIST)]
        return sorted(
            starts,
            key=lambda s: planar_distance(self._player_pos, (s.pos.x, s.pos.y, s.pos.z)),
        )

    def _nearest_eligible(self, state: GameState) -> MissionStart | None:
        eligible = self._eligible(state)
        return eligible[0] if eligible else None

    def _retarget(self, state: GameState) -> None:
        """Keep the chosen marker if it is still on the map; otherwise re-pick.

        Deliberately sticky: re-choosing "nearest" every tick makes two markers
        at similar distance swap places as he drives and re-posts a drive task
        each time.
        """
        eligible = self._eligible(state)
        if not eligible:
            self._marker = None
            return
        if self._marker is not None:
            for s in eligible:
                if planar_distance(self._marker, (s.pos.x, s.pos.y, s.pos.z)) <= MARKER_MOVED_THRESHOLD_M:
                    self._marker = (s.pos.x, s.pos.y, s.pos.z)
                    self._marker_protagonist = s.protagonist
                    return
        nearest = eligible[0]
        self._marker = (nearest.pos.x, nearest.pos.y, nearest.pos.z)
        self._marker_protagonist = nearest.protagonist

    # -- roam block ------------------------------------------------------------

    def _start_roam_block(self, now: float) -> None:
        self._phase = _Phase.ROAM
        self._mission_requested = None
        self._block_started_at = now
        self._block_length_s = self._rng.uniform(*ROAM_BLOCK_S)
        self._marker = None
        self._arrived_at = None
        self._bound_task_id = None
        self._roam_actual = None
        # A different idea from the one just held: repeating the same block back
        # to back is what the ActivityRunner's variety damping exists to stop,
        # and the plan should not undo it.
        previous = self._roam_preference
        choices = [i for i in ROAM_IDEAS if i is not previous] or list(ROAM_IDEAS)
        self._roam_preference = self._rng.choice(choices)
        log.info(
            "day plan: roam block",
            extra={
                "kv": {
                    "idea": self._roam_preference.activity,
                    "minutes": round(self._block_length_s / 60.0, 1),
                    "roam_blocks_owed": self._roam_blocks_owed,
                }
            },
        )

    def _roam_block_over(self, now: float, mood: str) -> bool:
        if self._mission_requested is not None:
            # Deliberately ahead of the ROAM_MIN_BLOCK_S floor and the
            # "do not cut an activity short" guard: by construction this is only
            # ever set after three VERIFIED goals or fifteen minutes, both of
            # which are far past the floor, and the roam engine has already
            # closed whatever it was running before asking.
            return True
        elapsed = now - self._block_started_at
        if elapsed < ROAM_MIN_BLOCK_S:
            return False
        if elapsed >= self._block_length_s:
            return True  # bounded time is bounded, even mid-activity
        if self._roam_blocks_owed > 0:
            # An owed block is a FULL block. Otherwise "roam for a full block
            # after three failures" degrades into two bored minutes and he is
            # back at the marker he cannot clear — the exact loop the back-off
            # exists to break.
            return False
        if self._activity_running:
            return False  # "or until it completes/fails" — do not cut it short
        if mood == "bored":
            return True
        return self._overdue_for_a_mission(now)

    def _overdue_for_a_mission(self, now: float) -> bool:
        if not self._missions_enabled:
            return False
        if self._last_mission_at is None:
            return True  # none yet this session
        return now - self._last_mission_at >= MISSION_OVERDUE_S

    def _owe_roam_blocks(self, count: int, reason: str) -> None:
        if count > self._roam_blocks_owed:
            self._roam_blocks_owed = count
            self._owed_reason = reason

    def _serve_one_owed_block(self) -> None:
        if self._roam_blocks_owed > 0:
            self._roam_blocks_owed -= 1
            if self._roam_blocks_owed == 0:
                self._owed_reason = ""
                # The back-off has been served; he is allowed to try again.
                if self._consecutive_fails >= MAX_CONSECUTIVE_MISSION_FAILS:
                    self._consecutive_fails = 0

    # -- the decision ----------------------------------------------------------

    def _mission_block_allowed(self, state: GameState) -> bool:
        """Is this a good moment to go and start a job? Sets `_blocked_reason`."""
        p = state.player
        if p.dead or p.arrested:
            self._blocked_reason = "he is down"
            return False
        if state.mission.cutscene_active:
            self._blocked_reason = "a cutscene is playing"
            return False
        if p.switch_in_progress:
            self._blocked_reason = "a protagonist switch is playing"
            return False
        if state.mission.retry_in_flight:
            self._blocked_reason = "a mission retry is in progress"
            return False
        if p.wanted > 0:
            self._blocked_reason = "the cops are still interested"
            return False
        if self._threat_close(state):
            self._blocked_reason = "there is a fight to finish first"
            return False
        if p.max_health > 0 and p.health < MIN_HEALTH_FRACTION_TO_START * p.max_health:
            self._blocked_reason = "too banged up to start a job"
            return False
        if self._roam_blocks_owed > 0:
            self._blocked_reason = self._owed_reason or "roaming first"
            return False
        if self._next_marker is None:
            self._blocked_reason = "no job marker he can use is on the map"
            return False
        self._blocked_reason = ""
        return True

    # -- reporting -------------------------------------------------------------

    def _marker_bearing(self) -> str:
        if self._marker is None:
            return "here"
        return compass_bearing(
            self._marker[0] - self._player_pos[0], self._marker[1] - self._player_pos[1]
        )

    def _job_phrase(self, protagonist: str, target: tuple[float, float, float]) -> str:
        who = protagonist if protagonist != UNKNOWN_PROTAGONIST else "next"
        distance = planar_distance(self._player_pos, target)
        bearing = compass_bearing(target[0] - self._player_pos[0], target[1] - self._player_pos[1])
        return f"go start the {who} job, {distance:.0f} m {bearing}"

    def _next_intent(self) -> str:
        if self._blocked_reason:
            return f"another roam block ({self._blocked_reason})"
        marker = self._next_marker
        if marker is None:
            return "another roam block — no job marker he can use is on the map"
        return self._job_phrase(
            marker.protagonist, (marker.pos.x, marker.pos.y, marker.pos.z)
        )

    def note(self) -> str:
        """One line for the brain's dynamic context, mirroring
        ``MissionFollower.note()`` and ``ActivityRunner.note()``.

        This is intent, not an instruction: the director owns the goal text and
        may override it. The point is that he can SEE the plan and therefore
        does not spend a decision fighting it.
        """
        now = self._clock()
        if self._phase is _Phase.IN_MISSION:
            return (
                "PLAN: mission block — the job is live, so the day plan stands back "
                "until it ends. The objective outranks everything but survival."
            )
        if self._phase is _Phase.TO_MISSION:
            if self._marker is None:
                return "PLAN: mission block — the marker just went off the map; roaming next."
            phrase = self._job_phrase(self._marker_protagonist, self._marker)
            left = max(0.0, self._mission_deadline - now) / 60.0
            arrived = (
                " He is standing in it; the game starts the job, not us."
                if self._arrived_at is not None
                else " Walking into the marker is what starts it."
            )
            return f"PLAN: mission block — {phrase}.{arrived} {left:.0f} min before he gives up."
        left = max(0.0, self._block_length_s - (now - self._block_started_at)) / 60.0
        if self._roam_actual is not None:
            doing = self._roam_actual.goal
        elif self._activity_running:
            doing = GENERIC_ROAM_GOAL
        else:
            doing = f"the idea is to {self._roam_preference.goal}"
        return f"PLAN: roam block ({doing}), {left:.0f} min left; next: {self._next_intent()}"
