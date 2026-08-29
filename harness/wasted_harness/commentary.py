"""Commentary: feed lines from decisions, recent-line memory, and banner rotation.

The decision's `say` IS the commentary (one model call produces decision and
commentary — CONTRACTS §2); this module never generates a second model call.
What it adds:

- feed lines for the overlay/site derived from decisions,
- **recent-line memory**: the last lines the agent said, fed back into the prompt so
  he can actually honour "never repeat a line you've said recently". The prompt
  claims the harness tracks this; this is where that claim becomes true.
- prepared banner lines for death/busted moments (the model isn't consulted at
  the instant of death — the banner needs a line NOW), rotated so no line
  repeats within the last 10 uses, persisted across restarts in
  state/commentary_rotation.json.

Writes are atomic (temp file + replace, retried) because on Windows another
process holding the file open turns a plain rename into a sharing violation.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import tempfile
import time
from collections import deque
from pathlib import Path

from .brain.schemas import DecisionModel
from .logsetup import get_logger

log = get_logger("wasted.commentary")

NO_REPEAT_WINDOW = 10
#: How many recent `say` lines are shown back to the brain.
RECENT_LINES_SHOWN = 12
#: How many are kept on disk (a little history beyond what is shown).
RECENT_LINES_KEPT = 40

_WRITE_RETRIES = 8
_WRITE_DELAY_S = 0.12

# Pools are > window+3 so a non-repeating pick always exists with slack.
DEATH_LINES: tuple[str, ...] = (
    "Scheduled rapid disassembly. Right on time.",
    "The ground and I had a disagreement. It won on points.",
    "Physics: undefeated.",
    "Put it on the tab.",
    "Cause of death: confidence.",
    "I'd watch the replay, but I was there.",
    "That was a load-bearing decision.",
    "The hospital knows me by engine sound now.",
    "Died doing what I love. Unclear what that was.",
    "New rule: bridges are suggestions with consequences.",
    "Somewhere, an actuary just smiled.",
    "Don't clip that. Don't you dare clip that.",
    "Respawning is just the city apologizing.",
    "I have died more times than I've parallel parked.",
    "Everything went fine right up until all of it.",
    "In my defense, the road moved.",
    "I'd like to thank gravity for its continued support.",
    "The seatbelt was decorative.",
    "Well. The car's fine.",
    "Ten out of ten. Nobody asked, but ten.",
    "That's going to buff out. I'm not going to buff out.",
    "Third-act problems.",
    "I regret nothing. I remember nothing. Same thing.",
    "The plan was perfect up to the part with the wall.",
)

BUSTED_LINES: tuple[str, ...] = (
    "Entrapment. The road entrapped me.",
    "I was cooperating. Loudly. At speed.",
    "This is why I never stop at lights.",
    "Booked under my legal name: some guy.",
    "The cuffs are a fashion choice I didn't make.",
    "I want my phone call. I'm calling the radio station.",
    "Alright. That one was fair.",
    "They got me on a technicality. The technicality was everything.",
    "Processing, mugshot, out by lunch. Routine.",
    "My lawyer is a horn sound and I stand by him.",
    "You can't arrest a concept. Apparently you can arrest me.",
    "Noted: the pier is a dead end.",
    "The system works. Rude of it.",
    "Caught slowing down to admire my own driving.",
    "Four cruisers. I'm flattered and detained.",
    "I'd run, but I'm parked.",
    "A misunderstanding I fully intended.",
    "He said step out of the vehicle like it was mine.",
    "Tell the car I'll be back. Lie to the car.",
    "The alley had a gate. The gate had opinions.",
    "Arrested for driving while ambitious.",
    "Deaths are physics. This is paperwork.",
    "Zero stars. Spent them all at once.",
    "Turns out they do talk to each other.",
)


def _atomic_write_json(path: Path, data: object) -> None:
    """Write JSON atomically, retrying the replace on Windows sharing violations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        for attempt in range(_WRITE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _WRITE_RETRIES - 1:
                    raise
                time.sleep(_WRITE_DELAY_S)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(
            "commentary state unreadable, starting fresh",
            extra={"kv": {"path": str(path), "error": str(exc)[:120]}},
        )
        return {}


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
        recent = _read_state(self.state_path).get(self.name, [])
        if not isinstance(recent, list):
            return []
        return [i for i in recent if isinstance(i, int) and 0 <= i < len(self.pool)]

    def _save(self) -> None:
        data = _read_state(self.state_path)
        data[self.name] = self._recent
        try:
            _atomic_write_json(self.state_path, data)
        except OSError as exc:
            # Losing the rotation history costs variety, never the show.
            log.warning(
                "could not persist commentary rotation",
                extra={"kv": {"path": str(self.state_path), "error": str(exc)[:120]}},
            )

    def next(self) -> str:
        blocked = set(self._recent[-NO_REPEAT_WINDOW:])
        candidates = [i for i in range(len(self.pool)) if i not in blocked]
        idx = self._rng.choice(candidates)
        self._recent.append(idx)
        self._recent = self._recent[-(NO_REPEAT_WINDOW * 2):]
        self._save()
        return self.pool[idx]


class RecentLines:
    """The last things the agent said, so the prompt's no-repeat rule is enforceable."""

    def __init__(self, state_path: Path, key: str = "recent_say") -> None:
        self.state_path = state_path
        self.key = key
        stored = _read_state(state_path).get(key, [])
        lines = [str(s) for s in stored] if isinstance(stored, list) else []
        self._lines: deque[str] = deque(lines[-RECENT_LINES_KEPT:], maxlen=RECENT_LINES_KEPT)

    def add(self, line: str) -> bool:
        """Record a line; returns True when it duplicates a recent one."""
        norm = _normalize(line)
        repeated = any(_normalize(old) == norm for old in list(self._lines)[-RECENT_LINES_SHOWN:])
        if repeated:
            log.warning("repeated commentary line", extra={"kv": {"say": line[:120]}})
        self._lines.append(line)
        data = _read_state(self.state_path)
        data[self.key] = list(self._lines)
        try:
            _atomic_write_json(self.state_path, data)
        except OSError as exc:
            log.warning(
                "could not persist recent lines",
                extra={"kv": {"error": str(exc)[:120]}},
            )
        return repeated

    def recent(self, n: int = RECENT_LINES_SHOWN) -> list[str]:
        return list(self._lines)[-n:]

    def context_block(self) -> str:
        """The prompt block. Empty string when there is nothing to avoid yet."""
        lines = self.recent()
        if not lines:
            return ""
        body = "\n".join(f'- "{line}"' for line in lines)
        return (
            "LINES YOU ALREADY USED (do not repeat these, and do not rephrase "
            f"them — find a new angle):\n{body}"
        )


def _normalize(line: str) -> str:
    """Case/punctuation-insensitive form, so "Fine." and "fine" count as one."""
    kept = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in line.lower())
    return " ".join(kept.split())


class Commentary:
    def __init__(self, state_dir: Path, rng: random.Random | None = None) -> None:
        path = state_dir / "commentary_rotation.json"
        self.death = LineRotation("death", DEATH_LINES, path, rng)
        self.busted = LineRotation("busted", BUSTED_LINES, path, rng)
        self.recent = RecentLines(path)

    def death_line(self) -> str:
        line = self.death.next()
        self.recent.add(line)
        return line

    def busted_line(self) -> str:
        line = self.busted.next()
        self.recent.add(line)
        return line

    def record_say(self, line: str) -> bool:
        """Remember a model-authored line; True when it repeated a recent one."""
        return self.recent.add(line)

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
