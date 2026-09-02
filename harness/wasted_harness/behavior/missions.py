"""Mission state machine — phase tracking plus structural objective-following.

What is REAL now: phase tracking driven entirely by /state mission flags
(`mission.active`, `mission.cutscene_active`, `random_event_active`) and the
objective hash from perception, the §4 mission events that can be emitted from
those flags alone, and (:class:`MissionFollower`) turning `mission.objective_blip`
into the existing bridge navigation tasks (`drive_to` / `walk_to` /
`enter_nearest_vehicle`) — the gap that used to leave the agent running plain
free-roam logic (widening a vehicle search, say) while a mission was active or
even mid-cutscene.

Also real, and the reason this file changed again: a FOLLOW mission issues no
objective marker at all. The friendly blue dot is the objective, and
`MissionFollower` now tails it with `follow_entity`, escalates when the gap
widens, and calls target-lost when the companion is gone — all inside the
frozen §1 task vocabulary, no new task type.

Also here now: the one failure signal this package can see honestly — a
mission that was active and ended while the player was dead or arrested is a
failure, not a pass or a skip (§4 `mission_fail`), observed live as the exact
gap that used to leave a death silently ending the mission with nothing
recorded and nothing retried.

What is deliberately NOT here yet (Phase 4 per PLAN.md): mission
identification by name, per-mission objective tactics beyond "go toward the
blip", and the `missions` table bookkeeping of attempts/outcomes. Until Phase
4 lands, mission names are reported as "unknown" — that is the truth the
bridge exposes; naming missions before we can identify them would be
invention.

Also here now: a mission ending *without* a concurrent death/arrest used to be
logged and nothing else — the exact live bug ("it cant understand if mission
is failed it keeps on sayin random things"). The most common way a mission
fails is precisely this: nobody died, nobody got arrested, the game just
prints MISSION FAILED with its own reason ("Franklin lost Lamar" on a follow
mission whose target drove away), and this package had no honest way to know.
`MissionTracker.feed` still cannot guess that from flags alone — CLAUDE.md
rule 1 forbids it — so on this ending it now arms `pending_outcome_read`
instead of just logging, the same way `mission_started` already arms the
director's vision trigger. The caller (main.py) reads the actual PASSED/
FAILED/UNKNOWN screen and reports the result through `resolve_outcome`, which
is the only place `mission_end`'s `outcome: "passed"` or a screen-detected
`mission_fail` gets emitted. `unknown` still emits nothing: an unread screen
is not a pass, and guessing one would put a false number on the public site's
`missions_passed` counter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..bridge_client import GameState
from ..logsetup import get_logger
from ..perception import Delta
from .navigation import (
    DRIVE_ARRIVE_RADIUS_M,
    RUSHED_SPEED_MPS,
    RUSHED_STYLE_DISTANCE_M,
    VEHICLE_SEARCH_RADIUS_M,
    WALK_ARRIVE_RADIUS_M,
    WALK_SWITCH_RADIUS_M,
    navigate_to,
    planar_distance,
)

log = get_logger("wasted.missions")

#: The navigation ladder (drive / walk / go-and-get-a-car) and its thresholds
#: now live in behavior/navigation.py, because behavior/planner.py needs the
#: identical ladder to walk into a mission-START marker. They are re-exported
#: here under the names they have always had: this module is where the rule was
#: born and where its callers and tests still import it from.
__all__ = [
    "DRIVE_ARRIVE_RADIUS_M",
    "FOLLOW_GAP_WIDEN_M",
    "FOLLOW_GAP_WINDOW_S",
    "FOLLOW_RECOVERY_EPISODE_RESET_S",
    "FOLLOW_RECOVERY_EXHAUSTED_S",
    "FOLLOW_RECOVERY_RUNG_GAP_S",
    "FOLLOW_RECOVERY_VEHICLE_RADIUS_M",
    "PROGRESS_MARGIN_M",
    "RUSHED_STYLE_DISTANCE_M",
    "STUCK_WINDOW_S",
    "TARGET_MOVED_THRESHOLD_M",
    "VEHICLE_SEARCH_RADIUS_M",
    "WALK_ARRIVE_RADIUS_M",
    "WALK_SWITCH_RADIUS_M",
    "MissionEvent",
    "MissionFollower",
    "MissionPhase",
    "MissionTracker",
]


class MissionPhase(Enum):
    NO_MISSION = "no_mission"
    CUTSCENE = "cutscene"
    OBJECTIVE = "objective"
    RANDOM_EVENT = "random_event"


@dataclass
class MissionEvent:
    """A §4 event derived from mission-flag transitions."""

    type: str  # mission_start | mission_end (outcome unknown until Phase 4)
    payload: dict[str, Any]


@dataclass
class MissionTracker:
    phase: MissionPhase = MissionPhase.NO_MISSION
    mission_started_at: float | None = None
    objective_changes: int = 0
    clock: Any = time.monotonic
    #: Best-effort retry counter. The bridge exposes no mission identity
    #: ("You are never told a mission's name" — CONTRACTS/prompts), so this
    #: cannot promise "attempt N of THIS mission"; it is honestly "N
    #: consecutive fail -> retry cycles since the last mission that ended
    #: clean", which is what the game's own checkpoint-retry loop looks like
    #: from here. situations.md already expects this to exist ("Three fails
    #: on the same beat: the director will change the plan").
    attempt: int = 1
    #: True for exactly one `_handle_mission_events` call: `feed()` armed it
    #: because the mission just ended with the player neither dead nor
    #: arrested, and a screen read (`resolve_outcome`) is the only honest way
    #: left to learn pass vs fail. main.py checks this right after `feed()`,
    #: in the same tick, the same way it already checks `_pending_screenshot_
    #: trigger` for `mission_start` — there is no cross-tick queue here.
    pending_outcome_read: bool = False
    #: The duration of the mission that just ended, captured by `feed()`
    #: before it clears `mission_started_at`; consumed (and zeroed) by
    #: `resolve_outcome` for the `mission_end` payload's `duration_s`.
    _pending_duration_s: float = 0.0
    #: Deaths observed across THIS mission's whole retry sequence (every
    #: `attempt`, not just the last one) — a real count from the same
    #: `player_dead` flag `feed()` is already given, reset only when a truly
    #: fresh mission starts (`attempt` resets to 1). Reported as `mission_end`'s
    #: `deaths` field on an eventual pass; never incremented for an arrest,
    #: which is a failure but not a death.
    deaths_this_mission: int = 0
    #: One line about the mission that JUST ended (pass, or fail + the real
    #: reason), surfaced through `brain_note()` while `phase` is NO_MISSION.
    #: Exists because "keeps talking even after mission has failed" was the
    #: reported live bug: the brain otherwise has nothing telling it the job
    #: is over, so it keeps narrating one that no longer exists. Cleared the
    #: moment a new mission starts — it describes the LAST one, not this one.
    _last_outcome_note: str | None = None
    _last_ended_in_failure: bool = False
    _events: list[MissionEvent] = field(default_factory=list)

    def feed(
        self,
        mission_active: bool,
        cutscene_active: bool,
        random_event_active: bool,
        delta: Delta,
        player_dead: bool = False,
        player_arrested: bool = False,
    ) -> list[MissionEvent]:
        """Advance the machine from the flags; returns §4 events to emit."""
        self._events = []
        if delta.mission_started:
            self.mission_started_at = self.clock()
            self.objective_changes = 0
            self.attempt = self.attempt + 1 if self._last_ended_in_failure else 1
            self._last_ended_in_failure = False
            self._last_outcome_note = None
            if self.attempt == 1:
                # A genuinely fresh job, not a retry of the last one: the death
                # count from whatever came before does not belong to this one.
                self.deaths_this_mission = 0
            # Mission names need Phase 4 identification (script-hash/blip work);
            # "unknown" is what we actually know at this phase.
            self._emit("mission_start", {"name": "unknown"})
        if delta.mission_ended:
            duration = (
                self.clock() - self.mission_started_at if self.mission_started_at else 0.0
            )
            failed = player_dead or player_arrested
            self._last_ended_in_failure = failed
            if failed:
                # The one failure signal this package can see honestly without
                # a screen read: the mission ended while the agent was dead or in
                # cuffs. A genuine pass or a skip does neither, so this is a
                # real, not a guessed, failure — reason_text is exactly what
                # triggered it, nothing more specific is knowable (same
                # honesty rule as the `death` event's own `cause: "?"`).
                if player_dead:
                    self.deaths_this_mission += 1
                reason = "arrested" if player_arrested else "died"
                self._last_outcome_note = f"Just FAILED ({reason}). Stop treating it as still running."
                self._emit(
                    "mission_fail",
                    {
                        "name": "unknown",
                        "reason_text": reason,
                        "attempt": self.attempt,
                    },
                )
            else:
                # Neither dead nor arrested: the fast flags-only path cannot
                # honestly call this a pass, a fail, or a skip (CLAUDE.md rule
                # 1). This is the exact gap that used to leave a follow-mission
                # failure ("Franklin lost Lamar") unreported entirely — arm a
                # screen read instead of just logging. main.py sees this flag
                # right after `feed()` returns (same tick, same idiom as
                # `mission_start` arming `_pending_screenshot_trigger`),
                # captures the mission-end screenshot, reads PASSED/FAILED/
                # UNKNOWN off it, and reports the result through
                # `resolve_outcome`.
                self.pending_outcome_read = True
                self._pending_duration_s = duration
                log.info(
                    "mission ended alive; screen read requested to learn pass/fail",
                    extra={"kv": {"duration_s": round(duration, 1), "objective_changes": self.objective_changes}},
                )
            self.mission_started_at = None
        if delta.objective_changed and mission_active:
            self.objective_changes += 1

        if cutscene_active:
            self.phase = MissionPhase.CUTSCENE
        elif mission_active:
            self.phase = MissionPhase.OBJECTIVE
        elif random_event_active:
            self.phase = MissionPhase.RANDOM_EVENT
        else:
            self.phase = MissionPhase.NO_MISSION
        return self._events

    def _emit(self, type_: str, payload: dict[str, Any]) -> None:
        self._events.append(MissionEvent(type_, payload))
        log.info("mission event", extra={"kv": {"type": type_, **payload}})

    def resolve_outcome(self, outcome: str, reason_text: str | None) -> MissionEvent | None:
        """Turn a mission-end screen read into the one §4 event honesty allows.

        `outcome` is `brain.vision.read_mission_outcome`'s own closed
        vocabulary — `"passed" | "failed" | "unknown"` — never a raw guess.
        A no-op (returns `None`, touches nothing) when `pending_outcome_read`
        is not set, so a caller may call this once per tick without needing
        to track whether a read was actually pending; it also makes a single
        ending impossible to double-count.

        `unknown` emits nothing and resolves to `None`: an unread screen is
        not a pass, and reporting one anyway would put a false number on the
        public site's `missions_passed` counter (CLAUDE.md rule 1). `failed`
        sets `_last_ended_in_failure` here — the fast dead/arrested path sets
        it directly in `feed()`, but a screen-detected failure only reaches
        this method, so without this the next `mission_start` would reset
        `attempt` to 1 and retries would stop being counted.
        """
        if not self.pending_outcome_read:
            return None
        self.pending_outcome_read = False
        duration = self._pending_duration_s
        self._pending_duration_s = 0.0
        if outcome == "failed":
            self._last_ended_in_failure = True
            # The screen's OWN words are the point of this whole read — never
            # "?" when the game actually told us. The fallback only fires if
            # FAILED printed with no reason line at all.
            reason = reason_text or "no reason shown on screen"
            self._last_outcome_note = f"Just FAILED: {reason}. Stop treating it as still running."
            self._emit(
                "mission_fail",
                {"name": "unknown", "reason_text": reason, "attempt": self.attempt},
            )
            return self._events[-1]
        if outcome == "passed":
            self._last_ended_in_failure = False
            self._last_outcome_note = "Just PASSED. Nothing to finish — it is over."
            self._emit(
                "mission_end",
                {
                    "name": "unknown",
                    "outcome": "passed",
                    "duration_s": round(duration, 1),
                    "deaths": self.deaths_this_mission,
                    "attempts": self.attempt,
                },
            )
            return self._events[-1]
        # unknown, or anything the vision layer did not resolve to a closed
        # value: a mission genuinely ended, but this package still cannot
        # honestly say how. Logged, not reported — the same rule that used to
        # cover every alive ending before this method existed.
        log.info(
            "mission outcome screen unreadable; nothing recorded",
            extra={"kv": {"duration_s": round(duration, 1)}},
        )
        return None

    @property
    def in_mission(self) -> bool:
        return self.phase in (MissionPhase.CUTSCENE, MissionPhase.OBJECTIVE)

    def brain_note(self) -> str:
        """One line for the tactical prompt about mission state."""
        if self.phase is MissionPhase.CUTSCENE:
            return "MISSION: cutscene playing — action must be wait."
        if self.phase is MissionPhase.OBJECTIVE:
            mins = (
                (self.clock() - self.mission_started_at) / 60.0 if self.mission_started_at else 0.0
            )
            attempt_note = f" This is attempt {self.attempt}." if self.attempt > 1 else ""
            return (
                f"MISSION: active for {mins:.0f} min, {self.objective_changes} objective "
                f"changes so far.{attempt_note} Objective outranks everything but survival."
            )
        if self.phase is MissionPhase.RANDOM_EVENT:
            return "MISSION: a random street event is active nearby."
        if self._last_outcome_note:
            return f"MISSION: none active. {self._last_outcome_note}"
        return "MISSION: none active."


# --- objective following -------------------------------------------------------


#: Progress must beat the previous best by at least this much to count as
#: "getting closer" — a couple of metres of GPS/pathing jitter must not keep
#: resetting the stuck clock forever.
PROGRESS_MARGIN_M = 2.0

#: The blip counts as a NEW objective (not the same one drifting) past this
#: much movement — the same threshold perception.py's `Delta.objective_changed`
#: already uses for this exact field, so the two stay in agreement.
TARGET_MOVED_THRESHOLD_M = 5.0

#: How long the objective may go without getting closer before the follower
#: backs off and leaves the tick to the tactical/director brain (or plain
#: idling). Long enough to survive one bad route around a block; short enough
#: that a genuinely bad coordinate does not run for the whole mission.
STUCK_WINDOW_S = 60.0


# --- following a friendly when the game issues no marker ----------------------
#
# The game rule, from the operator: "for an active mission when you have to
# follow, you don't get a marker, you only have to follow the blue dot, it's a
# car so it's fast." Measured live: `mission.active = true`,
# `mission.objective_blip = null`, a `friendly` ped 18 m ahead driving away —
# and this class did nothing at all, because it returned None the moment the
# blip was null. The agent sat there narrating "no marker yet, waiting for the job
# to tell me where to go" until the game printed
#     MISSION FAILED
#     Franklin lost Lamar.
# CONTRACTS v1.5's `relationship: "friendly"` (the engine's own
# Companion/Like/Respect group) is what makes this addressable at all, and it
# has been verified live: the companion came through as `friendly` at 4.7 m
# while the only `hostile` in the list was a cat.

#: A followed friendly that has drifted this much further away than the
#: closest this tail has managed is pulling away, not taking a corner wide.
#: 25 m is between one and two seconds of a car at city speed, and city
#: traffic, junctions and lane changes routinely produce everything below it.
#: TIGHTENED 25.0 -> 12.0 (2026-09-02): 25 m of lost ground is most of the way
#: to losing the target on a road, and the escalation that follows is the only
#: lever we have. Half that is still well clear of the metre-or-two of jitter a
#: tail shows at a junction.
FOLLOW_GAP_WIDEN_M = 12.0

#: ...and it has to stay that way for this long before anything escalates. One
#: snapshot at 2-4 Hz catches every overtake; six seconds of continuously
#: losing ground is the mission failing in real time.
#: TIGHTENED 6.0 -> 2.5 (2026-09-02): six seconds of falling behind at
#: mission-NPC speed is ~180 m gone before he asks for more speed.
FOLLOW_GAP_WINDOW_S = 2.5

#: THE TARGET-LOST RECOVERY LADDER, replacing the old single "wait 10s, say
#: `stop` once" behaviour (root-caused from the 2026-09-02 live follow-mission
#: failure: the followed friendly got into a vehicle, `_friendly_missing`
#: posted one `stop` after 10s and went quiet, and the car coasted to a halt
#: mid-mission). `nearby` is the top 8 BY DISTANCE (CONTRACTS §1), so a
#: companion can drop off the list for a moment simply by being the
#: ninth-closest body in a crowd, or while the engine streams him back in —
#: that is not "gone". A bounded, non-repeating ladder of real hypotheses,
#: cheapest and most likely first, now runs instead of a bare wait:
#:   a. vehicle hand-off — the last `in_vehicle_handle` seen on him, fires
#:      immediately (no wait at all: it is the single most likely explanation
#:      for a friendly simply disappearing).
#:   b. the game's own routed entity (`_routed_entity`, CONTRACTS v1.8) —
#:      already outranks everything above `_friendly_missing` in
#:      `_follow_friendly`, so nothing further is needed here.
#:   c. the nearest unclaimed vehicle to his last-seen position.
#:   d. driving/walking to his last-seen position (re-acquire).
#:   e. exhausted: the original single `stop`, now at the end of the ladder
#:      rather than the whole of it.

#: Distance from the friendly's last-seen position within which a nearby,
#: unclaimed vehicle is worth guessing as "the car they just got into" (rung
#: c). Wide enough to survive a few seconds of GPS/pathing jitter between
#: samples, narrow enough that a car parked two blocks over is not mistaken
#: for it.
FOLLOW_RECOVERY_VEHICLE_RADIUS_M = 15.0

#: Minimum gap between two rungs of the ladder actually firing. Same
#: rationale as `vehicle.RECOVERY_RUNG_GAP_S`: a task posted this tick only
#: shows up as evidence in the NEXT snapshot, so grading a rung sooner than
#: this would judge it before the game had answered. Rung (a) is exempt from
#: this on the FIRST missing tick of a fresh episode — nothing has fired yet
#: to wait on.
FOLLOW_RECOVERY_RUNG_GAP_S = 4.0

#: How long, from the moment the friendly went missing, before the ladder
#: gives up entirely and falls back to `stop` (rung e) — the same behaviour
#: this file always had, just later, because rungs a/c/d now get a real shot
#: first. Roughly the old 10s wait plus room for two or three rungs at
#: `FOLLOW_RECOVERY_RUNG_GAP_S` apart plus their own evidence lag.
FOLLOW_RECOVERY_EXHAUSTED_S = 30.0

#: How long the friendly must be CONTINUOUSLY back in view before a loss
#: episode is considered over and the ladder resets for next time. Short of
#: this, a brief regain-then-lose-again (a corner, a lag spike) continues the
#: SAME episode from wherever it left off rather than re-trying hypotheses
#: that already failed — the brief's own rule: "do not restart the ladder
#: from rung (a) blindly".
FOLLOW_RECOVERY_EPISODE_RESET_S = 20.0

#: Speed to ask for once the target is provably pulling away, in m/s. CONTRACTS
#: v1.9 gives `follow_entity` a `speed_mps`; the bridge's own default is 30.0
#: (108 km/h), which matches an NPC driving a mission route. 40.0 (144 km/h) is
#: the "he is getting away, close the gap" setting - fast enough to catch up on
#: a straight, and still a speed the traffic-aware follow task can drive rather
#: than a number that just puts him into a wall. It is only ever requested after
#: FOLLOW_GAP_WINDOW_S of continuously losing ground, never as the opening move.
FOLLOW_CHASE_SPEED_MPS = 40.0

# THE MUTUAL-WAIT DEADLOCK, observed live on stream 2026-09-02. The agent and Lamar stood in the
# street facing each other, neither moving, for minutes. His own commentary is the diagnosis:
#     "Lead the way, I'm not the one with the plan here."
#     "Holding position. His call."
#     "Still on Lamar's hip. He'll move when he moves."
# He was politely waiting for the crewmate. The crewmate was a mission NPC waiting for the PLAYER
# to do something — in GTA V that is nearly always "get in the car" or "step into the trigger".
# `follow_entity` on a target that never moves never completes, so nothing ever broke the tie.
#
# The rule this encodes: **a stationary crewmate is not an instruction to stand still.** When
# neither of you has moved for a while, the game is waiting for YOU.

#: Neither he nor the followed friendly has moved more than this, so nothing is happening.
#: 3 m is wider than idle-animation drift and pathing jitter, narrower than a real repositioning.
MUTUAL_STALL_MOVE_M = 3.0

#: ...for this long. Scripted mission dialogue genuinely runs 10 s+ and interrupting it is its own
#: failure, so this has to clear a real conversation. Twelve seconds of two men standing in a
#: street is already too long on a live stream.
MUTUAL_STALL_WINDOW_S = 12.0

#: Minimum gap between two deadlock-breaking attempts. Without it the breaker re-fires every tick
#: while the world catches up with the task it just posted, which is its own kind of standing still.
MUTUAL_STALL_COOLDOWN_S = 20.0


@dataclass(frozen=True)
class _RoutedTarget:
    """A `route_blips[]` entity dressed up as something the tail logic can hold.

    It carries only what that logic reads - `handle` (to pass to
    `follow_entity`) and `distance` (to spot a widening gap) - so a routed
    vehicle and a friendly ped go down exactly the same path.
    """

    handle: int
    distance: float
    #: The game's own label for the blip, when it had one ("Lamar"). v1.11; carried
    #: so `note()` can tell the brain WHO it is tailing rather than a bare handle.
    name: str | None = None


@dataclass
class MissionFollower:
    """Structural pursuit of the mission objective — the marker, or the blue dot.

    Pure reflex, no model call, two modes and a strict priority between them:

    * **marker mode** (`mission.objective_blip` present) — drive or walk
      toward its *position* using the existing bridge task vocabulary. Wins
      outright whenever a marker exists.
    * **blue-dot mode** (`mission.active` but `objective_blip is None`) — the
      nearest `friendly` ped IS the objective, pursued with `follow_entity`.
      Follow missions never issue a marker, so this used to be a dead tick:
      the class returned `None` and the agent stood still until MISSION FAILED.
      See the section header above for the measurement.

    A brain call cannot do a tail: 1-2 s per decision against a companion in a
    car, and the model is easily distracted into narrating instead of driving.
    So the tail, the widening-gap escalation and target-lost all live here.

    `objective_blip.kind` ("coord" vs "entity") is read but not branched on:
    either way the field the bridge gives us is `pos`. Using `follow_entity`
    would need an *entity* handle, and the blip only carries a *blip* handle
    (CONTRACTS §1) — treating the two as interchangeable would be guessing an
    API this package has not verified (CLAUDE.md rule 6). So both kinds are
    chased the same honest way: drive/walk to wherever the blip currently is,
    re-aiming whenever it moves.

    Four structural guarantees, matching the brief's four requirements:

    1. **Cutscene policy** — `plan()` returns `None` outright while
       `mission.cutscene_active`; no task is even considered, let alone posted.
    2. **Never abandons a mission** — this class only ever *proposes*
       navigation; it is `main._drive_activities` that already refuses to run
       free-roam behaviour while `MissionTracker.in_mission`, so mission intent
       is never displaced by joyriding.
    3. **Failure / stuck handling** — if planar distance to the target has not
       improved by `PROGRESS_MARGIN_M` in `STUCK_WINDOW_S`, `plan()` returns
       `None` (backing off) until the objective moves or the distance improves
       again — even if something else (the brain, a human on the couch)
       is what closed the gap. No infinite retry loop.
    4. **Never fights whoever already has the wheel** — a foreign task in
       `running` status (the brain's own decision, an activity, an idle pick)
       is left alone; the follower only posts once the bridge reports the slot
       idle/done/failed. Same idiom as `ActivityRunner.bind_step_task`/
       `next_step` (CONTRACTS: only `POST /task`'s own returned id is
       authoritative, never whatever `last_task.id` the next snapshot shows —
       a 2-4 Hz poll can still be reporting the previous task).
    5. **The tail is latched, like every other repeated post** — the same
       preemption rule that made combat stutter applies here: re-posting
       `follow_entity` for the same handle at the poll rate would restart the
       engine's follow task three times a second. It is re-posted only when
       the task is no longer running, the handle changed, or the situation
       class changed (`_follow_signature`), and a class change is the one path
       allowed to preempt deliberately.
    6. **Survival still outranks both.** Not enforced here — `main._reflex`
       runs the threat ladder first and `main._drive_mission_objective` stands
       down for the tick when it claimed the wheel.
    """

    clock: Any = time.monotonic
    stuck_window_s: float = STUCK_WINDOW_S
    _target: tuple[float, float, float] | None = None
    _best_distance: float | None = None
    _best_distance_at: float | None = None
    _last_distance: float | None = None
    _stuck_logged: bool = False
    _bound_task_id: str | None = None
    #: "blip" | "follow" | "idle", plus a one-shot flag for the tick the mode
    #: changes on. The latch below must not treat the task the OTHER mode
    #: posted as "our navigation is already under way": an objective marker
    #: appearing mid-tail has to preempt the tail on that very tick, and a
    #: marker vanishing mid-drive has to hand over to the tail just as fast.
    _mode: str = "idle"
    _mode_changed: bool = False
    #: The `nearby.peds[].handle` currently being tailed. Ephemeral by contract
    #: (§1: handles are valid only while the entity exists), which is why it is
    #: re-read from `/state` every tick and never cached across a target change.
    _follow_handle: int | None = None
    #: The game's own label for whoever is being tailed, when a v1.11 entity blip
    #: supplied one. Purely for `note()` — the brain reads "tailing Lamar", not a handle.
    _follow_name: str | None = None
    _follow_distance: float | None = None
    _follow_best_distance: float | None = None
    _follow_gap_since: float | None = None
    _follow_missing_since: float | None = None
    _follow_signature: tuple[Any, ...] | None = None
    _follow_no_faster_logged: bool = False
    #: Last-seen ground truth about the friendly currently being tailed,
    #: updated every tick he is actually visible as a `NearbyPed` (never from
    #: a `_RoutedTarget`, which carries neither field). This is exactly what
    #: the target-lost recovery ladder below reasons from once he vanishes.
    _follow_last_pos: tuple[float, float, float] | None = None
    _follow_last_in_vehicle_handle: int | None = None
    #: The recovery ladder's own state: which rungs have already been tried
    #: THIS loss episode (each fires at most once — brief requirement), the
    #: clock of the last rung that actually fired (pacing), and — while he is
    #: back in view after a loss — when that regain started, so a hold of
    #: `FOLLOW_RECOVERY_EPISODE_RESET_S` can retire the episode instead of a
    #: momentary regain silently wiping ladder progress.
    _recovery_tried: set[str] = field(default_factory=set)
    _recovery_last_rung_at: float = 0.0
    _recovery_regained_at: float | None = None
    #: One line for `note()` while a loss episode is running, set by whichever
    #: rung last fired — see the class docstring's "never a bare 'no friendly
    #: in range' during an active loss episode" requirement.
    _recovery_note: str | None = None
    #: Deadlock detection: where both of us were when the stall window opened.
    _stall_anchor_self: tuple[float, float, float] | None = None
    _stall_anchor_target: tuple[float, float, float] | None = None
    _stall_since: float | None = None
    _stall_last_break: float = 0.0

    def reset(self) -> None:
        self._forget_blip()
        self._end_follow()
        self._mode = "idle"
        self._mode_changed = False
        self._bound_task_id = None

    def _forget_blip(self) -> None:
        self._target = None
        self._best_distance = None
        self._best_distance_at = None
        self._last_distance = None
        self._stuck_logged = False

    def _end_follow(self) -> None:
        self._follow_handle = None
        self._follow_name = None
        self._follow_distance = None
        self._follow_best_distance = None
        self._follow_gap_since = None
        self._follow_missing_since = None
        self._follow_signature = None
        self._follow_no_faster_logged = False
        self._stall_anchor_self = None
        self._stall_anchor_target = None
        self._stall_since = None
        self._follow_last_pos = None
        self._follow_last_in_vehicle_handle = None
        self._reset_recovery_ladder()

    def _reset_recovery_ladder(self) -> None:
        """A fresh episode: nothing tried yet, no pacing floor, no regain in
        progress. Called both by `_end_follow` (a brand new tail) and once a
        loss episode is judged OVER (the friendly held continuously for
        `FOLLOW_RECOVERY_EPISODE_RESET_S`) — see `_follow_friendly`."""
        self._recovery_tried = set()
        self._recovery_last_rung_at = 0.0
        self._recovery_regained_at = None
        self._recovery_note = None

    def _set_mode(self, mode: str) -> None:
        if self._mode != mode:
            self._mode = mode
            self._mode_changed = True

    def bind_task(self, task_id: str | None) -> None:
        """Record the id `POST /task` returned for the navigation step just
        issued. This is the only thing `plan()` matches against — see class
        docstring point 4."""
        self._bound_task_id = task_id

    def plan(self, state: GameState) -> dict[str, Any] | None:
        """The next navigation task toward the objective, or `None` to leave
        the wheel alone this tick."""
        mission = state.mission
        if not mission.active or mission.cutscene_active:
            self.reset()
            return None
        if mission.objective_blip is None:
            # No marker. On a follow mission the game never issues one — the
            # friendly blue dot IS the objective (see the section header).
            self._forget_blip()
            self._set_mode("follow")
            return self._follow_friendly(state)
        # A marker exists: it wins outright (the brief's priority rule).
        self._end_follow()
        self._set_mode("blip")
        return self._chase_blip(state)

    # -- the objective marker ----------------------------------------------------

    def _chase_blip(self, state: GameState) -> dict[str, Any] | None:
        blip = state.mission.objective_blip
        assert blip is not None  # `plan` only routes here with one
        target = (blip.pos.x, blip.pos.y, blip.pos.z)
        player_pos = (state.player.pos.x, state.player.pos.y, state.player.pos.z)
        distance = planar_distance(player_pos, target)
        self._last_distance = distance

        if self._target is None or planar_distance(self._target, target) > TARGET_MOVED_THRESHOLD_M:
            # First sight of this objective, or the blip moved enough to count
            # as a new sub-objective: chase it fresh.
            self._target = target
            self._best_distance = distance
            self._best_distance_at = self.clock()
            self._stuck_logged = False
        elif self._best_distance is None or distance < self._best_distance - PROGRESS_MARGIN_M:
            self._best_distance = distance
            self._best_distance_at = self.clock()
            self._stuck_logged = False

        if self.clock() - (self._best_distance_at or self.clock()) > self.stuck_window_s:
            if not self._stuck_logged:
                log.info(
                    "mission objective not getting closer; backing off",
                    extra={"kv": {"distance_m": round(distance, 1), "window_s": self.stuck_window_s}},
                )
                self._stuck_logged = True
            return None

        if self._someone_else_has_the_wheel(state):
            return None

        # The drive / walk / go-and-get-a-car ladder itself is
        # navigation.navigate_to — shared with the day planner so both halves of
        # the show agree on what "too far to walk" means. No final-approach
        # dismount here: a mission OBJECTIVE is frequently something you are
        # meant to arrive at in the car.
        return navigate_to(player_pos, state.player.in_vehicle, target)

    # -- the blue dot ------------------------------------------------------------

    @staticmethod
    def _nearest_friendly(state: GameState) -> Any:
        """The nearest `friendly` ped, or None. One friendly is the companion;
        several (a crew) means the nearest is the one he is meant to be on."""
        friends = [p for p in state.nearby.peds if p.relationship == "friendly"]
        return min(friends, key=lambda p: p.distance) if friends else None

    @staticmethod
    def _routed_entity(state: GameState) -> Any:
        """The nearest thing the GAME has plotted a live GPS route to, or None.

        CONTRACTS v1.8 `mission.route_blips[]` with `kind == "entity"` is the
        strongest evidence there is about what a follow mission wants, and it
        outranks `_nearest_friendly` for two reasons. It is the game's own
        answer rather than our inference, so it is right when the nearest
        friendly is the wrong friendly - a crew of three in two cars, and only
        one of them is the one being tailed. And it can name a VEHICLE, which
        is what a follow route usually points at; `nearby.peds[]` cannot, so
        following the driver-as-ped was always a proxy for the real target.

        Returns a lightweight stand-in with the same `.handle`/`.distance`
        surface the friendly path uses, so the gap, latch and escalation logic
        below is shared rather than duplicated. `distance` is planar, from
        `route_blips[].pos` - the same measure `_chase_blip` uses.
        """
        routed = [b for b in getattr(state.mission, "route_blips", []) if b.kind == "entity"]
        if not routed:
            return None
        here = (state.player.pos.x, state.player.pos.y, state.player.pos.z)
        best = min(routed, key=lambda b: planar_distance(here, (b.pos.x, b.pos.y, b.pos.z)))
        return _RoutedTarget(
            handle=best.handle,
            distance=planar_distance(here, (best.pos.x, best.pos.y, best.pos.z)),
        )

    def _map_dot_target(self, state: GameState) -> Any:
        """The map dot dressed up as something the tail logic can hold.

        Same stand-in shape `_routed_entity` returns (`.handle`/`.distance`, plus a
        `.name` when the game labelled it), so the gap, latch and escalation logic
        below is shared rather than duplicated.
        """
        dot = self._best_map_dot(state)
        if dot is None:
            return None
        return _RoutedTarget(
            handle=dot.handle,
            distance=getattr(dot, "distance", 0.0) or 0.0,
            name=getattr(dot, "name", None),
        )

    def _follow_friendly(self, state: GameState) -> dict[str, Any] | None:
        now = self.clock()
        # The game's own route wins; the nearest friendly is the fallback for
        # when it has not plotted one (on-foot follows, and any pre-v1.8 bridge);
        # and an entity blip (v1.11) is the fallback after THAT, because it is the
        # only source that survives the ~50 m ped-scan radius — the map dot is
        # still there when the man himself is out of sensor range.
        friendly = (
            self._routed_entity(state)
            or self._nearest_friendly(state)
            or self._map_dot_target(state)
        )
        if friendly is None:
            return self._friendly_missing(state, now)

        distance = friendly.distance
        if friendly.handle != self._follow_handle:
            # A new companion (or the first one): fresh tail, fresh baseline —
            # `_end_follow` also resets the recovery ladder for the new episode.
            self._end_follow()
            self._follow_handle = friendly.handle
            self._follow_name = getattr(friendly, "name", None)
            self._follow_best_distance = distance
            log.info(
                "no objective marker; tailing the friendly blue dot instead",
                extra={"kv": {"handle": friendly.handle, "distance_m": round(distance, 1)}},
            )
        else:
            if distance < (self._follow_best_distance or distance):
                self._follow_best_distance = distance
            if self._follow_missing_since is not None:
                # Regained the SAME target after a loss. Do not blindly wipe
                # the ladder: only a continuous hold of
                # FOLLOW_RECOVERY_EPISODE_RESET_S counts as the episode being
                # over — a brief regain-then-lose-again (a corner, a lag
                # spike) continues from wherever the ladder left off.
                self._follow_missing_since = None
                if self._recovery_regained_at is None:
                    self._recovery_regained_at = now
        if (
            self._recovery_regained_at is not None
            and now - self._recovery_regained_at >= FOLLOW_RECOVERY_EPISODE_RESET_S
        ):
            self._reset_recovery_ladder()
        self._follow_distance = distance

        # Last-seen ground truth for the recovery ladder below, updated only
        # from a real `NearbyPed` — `_RoutedTarget` (the routed-entity path)
        # carries neither field and must not clobber what the ped path
        # already learned about the SAME companion.
        if hasattr(friendly, "pos") and friendly.pos is not None:
            self._follow_last_pos = (friendly.pos.x, friendly.pos.y, friendly.pos.z)
        if hasattr(friendly, "in_vehicle_handle"):
            self._follow_last_in_vehicle_handle = friendly.in_vehicle_handle

        # A widening gap is the mission failing in real time, not a detail.
        baseline = self._follow_best_distance if self._follow_best_distance is not None else distance
        if distance > baseline + FOLLOW_GAP_WIDEN_M:
            if self._follow_gap_since is None:
                self._follow_gap_since = now
        else:
            self._follow_gap_since = None
        widening = (
            self._follow_gap_since is not None
            and now - self._follow_gap_since >= FOLLOW_GAP_WINDOW_S
        )

        # Deadlock check BEFORE the tail is re-asserted. `follow_entity` on a target that never
        # moves never completes, so without this the latch happily holds a task that is achieving
        # nothing while two men stand in the street.
        if (
            now - self._stall_last_break >= MUTUAL_STALL_COOLDOWN_S
            and self._mutual_stall(state, friendly, now)
        ):
            step = self._break_deadlock(state, friendly, now)
            if step is not None:
                # Deliberate preempt, same as a signature change: clear the latch so the next
                # tick re-evaluates from scratch rather than resuming the dead tail.
                self._follow_signature = None
                self._bound_task_id = None
                return step

        want = self._follow_step(state, friendly, widening)
        signature = (
            want["type"],
            want["params"].get("handle"),
            want["params"].get("in_vehicle"),
            # Part of the signature so the escalation to chase speed actually
            # reaches the bridge: without it the latch sees "same follow, same
            # handle, same in_vehicle" and holds the slower task in place.
            want["params"].get("speed_mps"),
        )
        if signature != self._follow_signature:
            # The situation class changed (first tail, new handle, or an
            # escalation): post now, even over our own running task. This is
            # the ONLY path that preempts deliberately, so it also consumes
            # the mode-change hand-over below - the preempt has happened.
            self._follow_signature = signature
            self._mode_changed = False
            self._bound_task_id = None
            return want
        if self._someone_else_has_the_wheel(state):
            return None
        return want

    def _mutual_stall(self, state: GameState, friendly: Any, now: float) -> bool:
        """True when neither he nor the followed friendly has moved for a while.

        Both anchors are required. A target that is moving means the tail is working even if he
        is briefly stopped at a light; a target standing still while HE moves is him circling,
        which the gap logic already covers. Only the both-still case is the deadlock.
        """
        here = (state.player.pos.x, state.player.pos.y, state.player.pos.z)
        there = getattr(friendly, "pos", None)
        if there is None:
            # Pre-v1.6 bridge, or a routed blip without a position: no honest way to tell whether
            # the target is moving, so do not guess a deadlock into existence.
            self._stall_since = None
            return False
        theirs = (there.x, there.y, there.z)

        if self._stall_anchor_self is None or self._stall_anchor_target is None:
            self._stall_anchor_self, self._stall_anchor_target = here, theirs
            self._stall_since = now
            return False

        moved_self = planar_distance(here, self._stall_anchor_self)
        moved_them = planar_distance(theirs, self._stall_anchor_target)
        if moved_self > MUTUAL_STALL_MOVE_M or moved_them > MUTUAL_STALL_MOVE_M:
            # Something is happening. Re-anchor and start the clock again.
            self._stall_anchor_self, self._stall_anchor_target = here, theirs
            self._stall_since = now
            return False
        return self._stall_since is not None and now - self._stall_since >= MUTUAL_STALL_WINDOW_S

    def _break_deadlock(self, state: GameState, friendly: Any, now: float) -> dict[str, Any] | None:
        """Do something. Anything defensible, in the order most likely to be what the game wants.

        Ordered by what actually unblocks a stalled GTA V mission, most common first:
        1. **Get in a car.** Overwhelmingly the trigger — "follow Lamar" beats usually mean
           "drive after Lamar", and the crew waits at the vehicle until the player is seated.
        2. **Close the distance on foot.** Many triggers are proximity coronas a couple of metres
           across; sitting at 8 m looks identical to standing next to him but is not.
        3. **Look around.** Cheap, breaks the animation, and gives the brain a fresh frame to
           reason about rather than another identical one.
        """
        self._stall_last_break = now
        self._stall_since = now  # restart the clock; do not re-fire on the next tick

        if not state.player.in_vehicle and state.nearby.vehicles:
            log.info(
                "mutual stall: both of us stationary; getting in a car",
                extra={"kv": {"handle": friendly.handle, "distance_m": round(friendly.distance, 1),
                              "window_s": MUTUAL_STALL_WINDOW_S}},
            )
            return {
                "type": "enter_nearest_vehicle",
                "params": {"prefer": "any", "search_radius_m": VEHICLE_SEARCH_RADIUS_M},
            }

        pos = getattr(friendly, "pos", None)
        if pos is not None and friendly.distance > MUTUAL_STALL_MOVE_M:
            # Close the distance under his own power rather than waiting to be led. Which verb
            # depends on what he is sitting in: telling a man in a car to walk somewhere makes
            # him get out first, which is slower and looks broken on stream.
            if state.player.in_vehicle:
                log.info(
                    "mutual stall: driving to the crewmate instead of waiting on him",
                    extra={"kv": {"handle": friendly.handle,
                                  "distance_m": round(friendly.distance, 1)}},
                )
                return {
                    "type": "drive_to",
                    # speed_mps is REQUIRED by the bridge (CONTRACTS §1); without it
                    # every one of these recoveries came back 400 invalid_params and
                    # the car never moved. Observed live 2026-09-02.
                    "params": {"x": pos.x, "y": pos.y, "z": pos.z,
                               "speed_mps": RUSHED_SPEED_MPS,
                               "style": "rushed", "arrive_radius_m": DRIVE_ARRIVE_RADIUS_M},
                }
            log.info(
                "mutual stall: closing the last few metres to the crewmate",
                extra={"kv": {"handle": friendly.handle, "distance_m": round(friendly.distance, 1)}},
            )
            return {"type": "walk_to", "params": {"x": pos.x, "y": pos.y, "z": pos.z, "run": True}}

        log.info(
            "mutual stall: nothing obvious to try; looking around",
            extra={"kv": {"handle": friendly.handle}},
        )
        return {"type": "look_around", "params": {}}

    def _follow_step(
        self, state: GameState, friendly: Any, widening: bool
    ) -> dict[str, Any]:
        """One tail step: `follow_entity`, or the escalation when losing ground."""
        if widening and not state.player.in_vehicle:
            # Losing ground on foot. Whether the companion is in a car is not
            # observable — `nearby.peds[]` has no such field (CONTRACTS §1) —
            # but it does not need to be: nothing on foot keeps up with
            # something that is steadily pulling away, and a car is the only
            # answer in the frozen vocabulary. `VEHICLE_SEARCH_RADIUS_M` is the
            # same radius the rest of this package uses for "a car he could
            # plausibly reach".
            return {
                "type": "enter_nearest_vehicle",
                "params": {"prefer": "any", "search_radius_m": VEHICLE_SEARCH_RADIUS_M},
            }
        params: dict[str, Any] = {
            "handle": friendly.handle,
            # The only half of "are you both in vehicles" that /state
            # exposes. If he is driving, the tail has to be a driving tail.
            "in_vehicle": state.player.in_vehicle,
        }
        if widening and state.player.in_vehicle:
            # Already driving after them and still losing ground. Under
            # CONTRACTS v1.9 this IS expressible: `follow_entity` takes
            # `speed_mps`, so ask for the chase speed instead of the bridge's
            # 30 m/s cruise default. (Before v1.9 the bridge tailed at a
            # hard-coded 15 m/s in a style that stopped at red lights, which is
            # why this branch used to have nothing to offer but a log line.)
            # `style` is deliberately NOT sent - see ACTION_PARAM_KEYS - the
            # bridge's `ignore_lights` default is already the right one and the
            # wire default for `style` is the wrong one.
            params["speed_mps"] = FOLLOW_CHASE_SPEED_MPS
            if not self._follow_no_faster_logged:
                self._follow_no_faster_logged = True
                log.info(
                    "target pulling away; asking for chase speed",
                    extra={
                        "kv": {
                            "handle": friendly.handle,
                            "distance_m": round(friendly.distance, 1),
                            "closest_m": round(self._follow_best_distance or 0.0, 1),
                            "speed_mps": FOLLOW_CHASE_SPEED_MPS,
                        }
                    },
                )
        return {"type": "follow_entity", "params": params}

    def _friendly_missing(self, state: GameState, now: float) -> dict[str, Any] | None:
        """No friendly in `nearby.peds` (and no routed entity) this tick.

        This is the ONE place target-lost is decided (the bridge's own
        `follow_entity` -> `failed`/`target_lost` covers the entity handle
        going invalid; it did NOT cover the companion simply leaving, which is
        what was measured). Runs the bounded, non-repeating recovery ladder
        (module docstring above `FOLLOW_RECOVERY_VEHICLE_RADIUS_M`) instead of
        the old bare wait-then-stop.
        """
        if self._follow_handle is None:
            return None  # nothing was being followed; nothing to lose
        if self._follow_missing_since is None:
            self._follow_missing_since = now
            self._recovery_regained_at = None
        return self._try_recovery_rungs(state, now)

    #: The ladder, in priority order. Rung (b) — the game's own routed entity
    #: — already outranks this whole method from `_follow_friendly`, so it is
    #: not repeated here.
    _RECOVERY_RUNGS: tuple[str, ...] = (
        "vehicle_handoff", "nearest_vehicle", "map_dot", "reacquire", "exhausted",
    )

    def _try_recovery_rungs(self, state: GameState, now: float) -> dict[str, Any] | None:
        """Grade / advance the ladder. Each rung fires at most once per loss
        episode; an INAPPLICABLE rung (missing data) is skipped on the same
        tick rather than consuming the pacing gap — only an actual POST does
        that."""
        if now - self._recovery_last_rung_at < FOLLOW_RECOVERY_RUNG_GAP_S:
            return None
        for rung in self._RECOVERY_RUNGS:
            if rung in self._recovery_tried:
                continue
            result = self._recovery_rung_action(rung, state, now)
            if result is None:
                continue  # not applicable (yet, or at all) — try the next one now
            task, note = result
            self._recovery_tried.add(rung)
            self._recovery_last_rung_at = now
            self._recovery_note = note
            log.warning(
                "mission follow: target-lost recovery ladder",
                extra={
                    "kv": {
                        "rung": rung,
                        "handle": self._follow_handle,
                        "task": task["type"],
                        "missing_for_s": round(now - (self._follow_missing_since or now), 1),
                        "last_distance_m": round(self._follow_distance or 0.0, 1),
                    }
                },
            )
            if rung == "exhausted":
                self._end_follow()
                self._bound_task_id = None
            return task
        return None

    @staticmethod
    def _best_map_dot(state: GameState) -> Any:
        """The entity blip most likely to BE the target we just lost.

        Preference, strongest evidence first: the one the game has plotted a route to
        (`is_route`), then a named one (an unnamed dot is more likely scenery than crew),
        then the nearest. Returns None when the bridge is pre-v1.11 or nothing is marked.
        """
        blips = [b for b in getattr(state.mission, "entity_blips", []) or [] if getattr(b, "handle", None)]
        if not blips:
            return None
        routed = [b for b in blips if getattr(b, "is_route", False)]
        named = [b for b in blips if getattr(b, "name", None)]
        pool = routed or named or blips
        return min(pool, key=lambda b: getattr(b, "distance", 0.0) or 0.0)

    def _recovery_rung_action(
        self, rung: str, state: GameState, now: float
    ) -> tuple[dict[str, Any], str] | None:
        """One rung's `(task, note)`, or `None` when it does not apply right now."""
        last_dist = (
            f"{self._follow_distance:.0f}m" if self._follow_distance is not None else "an unknown range"
        )
        if rung == "vehicle_handoff":
            handle = self._follow_last_in_vehicle_handle
            if handle is None:
                return None
            task = {
                "type": "follow_entity",
                "params": {"handle": handle, "in_vehicle": state.player.in_vehicle},
            }
            note = (
                f"MISSION NAV: the friendly I was tailing vanished at {last_dist} — almost "
                f"certainly got into a vehicle; following their car (handle {handle})."
            )
            return task, note

        if rung == "map_dot":
            # CONTRACTS v1.11: `mission.entity_blips[]` are blips pinned to a ped or a
            # vehicle, present whether or not the game drew a route. They outlive
            # `nearby.peds`' ~50 m radius, which is exactly the window this ladder is
            # recovering from — and they carry the game's own label ("Lamar").
            #
            # OBSERVED LIVE 2026-09-02: he lost the crewmate at close range and then drove
            # around at random ("No sign of him anywhere. Widening the search") while the
            # dot sat on the minimap the whole time. A LIVE dot beats the `reacquire`
            # rung below, which only knows where the man WAS when he vanished.
            dot = self._best_map_dot(state)
            if dot is None:
                return None
            who = f" ({dot.name})" if getattr(dot, "name", None) else ""
            distance = getattr(dot, "distance", None)
            out = f"{distance:.0f}m" if isinstance(distance, (int, float)) else "an unknown range"
            # Follow the entity when we can: the engine tracks it as it moves, where a
            # coordinate goes stale the moment he drives on.
            task = {
                "type": "follow_entity",
                "params": {"handle": dot.handle, "in_vehicle": state.player.in_vehicle},
            }
            note = (
                f"MISSION NAV: lost him from close range, but his dot is still on the map "
                f"{out} out{who} — going to it."
            )
            return task, note

        if rung == "nearest_vehicle":
            if self._follow_last_pos is None:
                return None
            candidates = [
                v
                for v in state.nearby.vehicles
                if v.pos is not None
                and v.driver != "player"
                and planar_distance((v.pos.x, v.pos.y, v.pos.z), self._follow_last_pos)
                <= FOLLOW_RECOVERY_VEHICLE_RADIUS_M
            ]
            if not candidates:
                return None
            best = min(
                candidates,
                key=lambda v: planar_distance((v.pos.x, v.pos.y, v.pos.z), self._follow_last_pos),
            )
            task = {
                "type": "follow_entity",
                "params": {"handle": best.handle, "in_vehicle": state.player.in_vehicle},
            }
            note = (
                f"MISSION NAV: the friendly vanished at {last_dist} with no known vehicle of "
                f"their own — a car was near where they were last seen (handle {best.handle}); "
                f"trying that instead."
            )
            return task, note

        if rung == "reacquire":
            if self._follow_last_pos is None:
                return None
            x, y, z = self._follow_last_pos
            if state.player.in_vehicle:
                task = {
                    "type": "drive_to",
                    "params": {
                        "x": x, "y": y, "z": z,
                        # Required by the bridge — see the deadlock breaker above.
                        "speed_mps": RUSHED_SPEED_MPS,
                        "style": "rushed", "arrive_radius_m": DRIVE_ARRIVE_RADIUS_M,
                    },
                }
                note = (
                    f"MISSION NAV: the friendly vanished at {last_dist} with nothing to hand off "
                    f"to — driving to where they last were."
                )
            else:
                task = {"type": "walk_to", "params": {"x": x, "y": y, "z": z, "run": True}}
                note = (
                    f"MISSION NAV: the friendly vanished at {last_dist} with nothing to hand off "
                    f"to — heading to where they last were."
                )
            return task, note

        if rung == "exhausted":
            if (
                self._follow_missing_since is None
                or now - self._follow_missing_since < FOLLOW_RECOVERY_EXHAUSTED_S
            ):
                return None
            task = {"type": "stop", "params": {}}
            note = (
                f"MISSION NAV: lost the friendly at {last_dist} for good — the whole recovery "
                f"ladder ran with nothing to show for it. Stopping rather than tailing nobody."
            )
            return task, note

        raise AssertionError(f"unknown recovery rung {rung!r}")  # pragma: no cover

    # -- shared ------------------------------------------------------------------

    def _someone_else_has_the_wheel(self, state: GameState) -> bool:
        """Class docstring point 4, shared by both modes."""
        lt = state.last_task
        if self._mode_changed:
            self._mode_changed = False
            if self._bound_task_id is not None and lt.id == self._bound_task_id:
                # Our OWN task, posted by the mode we just left. Handing over
                # between the two modes is not "fighting whoever has the
                # wheel"; it is the same owner changing its mind.
                self._bound_task_id = None
                return False
        if self._bound_task_id is not None:
            if lt.id == self._bound_task_id:
                if lt.status == "running":
                    return True  # our own navigation is already under way
                self._bound_task_id = None  # done/failed: reconsider
                return False
            return lt.status == "running"  # brain / activity / idle pick
        return lt.status == "running"  # a task we never bound; do not fight it

    def note(self) -> str:
        """One line for the brain's dynamic context, mirroring
        `ActivityRunner.note()`."""
        if self._mode == "follow":
            if self._follow_handle is None:
                return (
                    "MISSION NAV: mission active with no objective marker and no friendly "
                    "in range to follow."
                )
            if self._follow_missing_since is not None:
                # An active loss episode: never fall back to a bare "no friendly in
                # range" here — the ladder always has SOMETHING to say about what it
                # is trying (or why it is still waiting to try the next thing).
                return self._recovery_note or (
                    "MISSION NAV: the friendly I was tailing just vanished — working "
                    "out where they went."
                )
            dist = f"{self._follow_distance:.0f}m" if self._follow_distance is not None else "?"
            if self._follow_no_faster_logged:
                return (
                    f"MISSION NAV: no marker — tailing the friendly (blue dot), {dist} out "
                    f"and PULLING AWAY. follow_entity has no speed setting; if you want him "
                    f"caught, drive there yourself."
                )
            who = f" — {self._follow_name}" if self._follow_name else ""
            return f"MISSION NAV: no marker — tailing the friendly (blue dot){who}, {dist} out."
        if self._target is None:
            return "MISSION NAV: not pursuing (no objective blip yet, or a cutscene is playing)."
        if self._best_distance_at is not None and self.clock() - self._best_distance_at > self.stuck_window_s:
            return (
                "MISSION NAV: gave up steering toward the objective — it stopped getting "
                "closer. Your call."
            )
        dist = f"{self._last_distance:.0f}m" if self._last_distance is not None else "?"
        return f"MISSION NAV: steering toward the objective, {dist} out."
