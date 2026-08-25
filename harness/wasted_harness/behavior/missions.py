"""Mission state machine — Phase 2 skeleton, honestly scoped.

What is REAL now: phase tracking driven entirely by /state mission flags
(`mission.active`, `mission.cutscene_active`, `random_event_active`) and the
objective hash from perception, plus the §4 mission events that can be emitted
from those flags alone.

What is deliberately NOT here yet (Phase 4 per PLAN.md): mission
identification by name, per-mission objective tactics, retry/fail-handling
policy, and the `missions` table bookkeeping of attempts/outcomes. Until
Phase 4 lands, mission names are reported as "unknown" — that is the truth the
bridge exposes; naming missions before we can identify them would be invention.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..logsetup import get_logger
from ..perception import Delta

log = get_logger("wasted.missions")


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
    _events: list[MissionEvent] = field(default_factory=list)

    def feed(self, mission_active: bool, cutscene_active: bool, random_event_active: bool, delta: Delta) -> list[MissionEvent]:
        """Advance the machine from the flags; returns §4 events to emit."""
        self._events = []
        if delta.mission_started:
            self.mission_started_at = self.clock()
            self.objective_changes = 0
            # Mission names need Phase 4 identification (script-hash/blip work);
            # "unknown" is what we actually know at this phase.
            self._emit("mission_start", {"name": "unknown"})
        if delta.mission_ended:
            duration = (
                self.clock() - self.mission_started_at if self.mission_started_at else 0.0
            )
            # Outcome detection (passed vs failed screen) is Phase 4; emitting
            # mission_end with outcome "passed" without knowing it would be a
            # fabrication, so the skeleton logs the transition and leaves the
            # §4 mission_end/mission_fail emission to Phase 4.
            log.info(
                "mission ended (outcome detection lands in Phase 4)",
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
            return (
                f"MISSION: active for {mins:.0f} min, {self.objective_changes} objective "
                f"changes so far. Objective outranks everything but survival."
            )
        if self.phase is MissionPhase.RANDOM_EVENT:
            return "MISSION: a random street event is active nearby."
        return "MISSION: none active."
