"""Commentary: feed lines from decisions + banner-line rotation for deaths/busts.

The decision's `say` IS the commentary (one model call produces decision and
commentary — CONTRACTS §2); this module never generates a second model call.
What it adds:

- feed lines for the overlay/site derived from decisions,
- prepared banner lines for death/busted moments (the model isn't consulted at
  the instant of death — the banner needs a line NOW), rotated so no line
  repeats within the last 10 uses, persisted across restarts in
  state/commentary_rotation.json.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from .brain.schemas import DecisionModel
from .logsetup import get_logger

log = get_logger("wasted.commentary")

NO_REPEAT_WINDOW = 10

# Pools are > window+3 so a non-repeating pick always exists with slack.
DEATH_LINES: tuple[str, ...] = (
    "Scheduled rapid disassembly. Right on schedule.",
    "That's not a death, that's a checkpoint with attitude.",
    "The ground and I had a disagreement. It won on points.",
    "Physics: undefeated since launch day.",
    "I'd like to see the replay. Actually, no I wouldn't.",
    "Put it on the tab.",
    "The hospital knows me by my engine sound now.",
    "Died doing what I love. Whatever that was.",
    "That one's on the map designer. Mostly. Partly. Fine.",
    "New rule: bridges are load-bearing suggestions.",
    "I have died more times than I've parallel parked. Telling.",
    "Somewhere, an insurance adjuster just felt a chill.",
    "Don't clip that. Don't you dare clip that.",
    "Cause of death: confidence.",
    "The good news is I can't feel pain. The bad news is everything else.",
    "Respawning is just the city apologizing to me.",
)

BUSTED_LINES: tuple[str, ...] = (
    "Entrapment. The road entrapped me.",
    "I was cooperating. Loudly. At speed.",
    "This is why I don't stop at lights. They find you at lights.",
    "Booked under my legal name: some guy, no ID.",
    "The cuffs are a fashion statement I didn't ask for.",
    "I want my phone call. I'm calling the radio station.",
    "Alright, fair. That one was fair.",
    "They got me on a technicality. The technicality was all of it.",
    "Processing fee, mugshot, out by lunch. Routine.",
    "My lawyer is a horn sound and I stand by him.",
    "You can't arrest a concept.",
    "Noted for next time: the pier is a dead end.",
    "The system works. Annoying, but it works.",
    "Caught because I slowed down to admire my own driving.",
)


class LineRotation:
    """Persisted no-repeat-in-last-10 rotation over a fixed pool."""

    def __init__(
        self,
        name: str,
        pool: tuple[str, ...],
        state_path: Path,
        rng: random.Random | None = None,
    ) -> None:
        if len(pool) <= NO_REPEAT_WINDOW:
            raise ValueError(
                f"pool {name!r} has {len(pool)} lines; needs > {NO_REPEAT_WINDOW} "
                f"to guarantee a non-repeating pick"
            )
        self.name = name
        self.pool = pool
        self.state_path = state_path
        self._rng = rng or random.Random()
        self._recent: list[int] = self._load()

    def _load(self) -> list[int]:
        if not self.state_path.exists():
            return []
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            recent = data.get(self.name, [])
            return [i for i in recent if isinstance(i, int) and 0 <= i < len(self.pool)]
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(
                "rotation state unreadable, starting fresh",
                extra={"kv": {"path": str(self.state_path), "error": str(exc)}},
            )
            return []

    def _save(self) -> None:
        data: dict[str, list[int]] = {}
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        data[self.name] = self._recent
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data), encoding="utf-8")

    def next(self) -> str:
        blocked = set(self._recent[-NO_REPEAT_WINDOW:])
        candidates = [i for i in range(len(self.pool)) if i not in blocked]
        idx = self._rng.choice(candidates)
        self._recent.append(idx)
        self._recent = self._recent[-(NO_REPEAT_WINDOW * 2):]
        self._save()
        return self.pool[idx]


class Commentary:
    def __init__(self, state_dir: Path, rng: random.Random | None = None) -> None:
        path = state_dir / "commentary_rotation.json"
        self.death = LineRotation("death", DEATH_LINES, path, rng)
        self.busted = LineRotation("busted", BUSTED_LINES, path, rng)

    def death_line(self) -> str:
        return self.death.next()

    def busted_line(self) -> str:
        return self.busted.next()

    @staticmethod
    def feed_line(decision: DecisionModel, layer: str) -> dict:
        """The overlay/site feed entry for a decision. Pure derivation, no invention."""
        return {
            "layer": layer,
            "say": decision.say,
            "thought": decision.thought,
            "mood": decision.mood,
            "goal": decision.goal,
            "action_type": decision.action.type,
        }
