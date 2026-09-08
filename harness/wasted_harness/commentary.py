"""Commentary: feed lines from decisions, recent-line memory, and banner rotation.

The decision's `say` IS the commentary (one model call produces decision and
commentary — CONTRACTS §2); this module never generates a second model call.
What it adds:

- feed lines for the overlay/site derived from decisions,
- **recent-line memory**: the last lines WANTED said, fed back into the prompt so
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
from .brain.schemas import jaccard_similarity as _jaccard_similarity
from .brain.schemas import normalize_line as _normalize
from .logsetup import get_logger

log = get_logger("wasted.commentary")

NO_REPEAT_WINDOW = 10
#: How many recent `say` lines are shown back to the brain.
RECENT_LINES_SHOWN = 12
#: How many are kept on disk (a little history beyond what is shown).
RECENT_LINES_KEPT = 40

#: How many of the most recently SHOWN (displayed on the overlay/feed) lines a
#: new decision-derived line is checked against before it is allowed on
#: screen. Deliberately smaller than RECENT_LINES_SHOWN (fed to the model as
#: prose it is *asked* to honour) — this is the mechanical backstop for when
#: the model's own effort at variety still lands a near-duplicate.
SIMILARITY_WINDOW = 5
#: Jaccard similarity (over lowercased, normalized word sets) above which a
#: line counts as "basically the same line again" and is dropped rather than
#: shown. Cheap, no model call: exact repeats are already caught elsewhere
#: (RecentLines.add's no-repeat check); this catches "Still on his six" vs
#: "Right on his six" — different strings, same line, back to back.
SIMILARITY_THRESHOLD = 0.8

#: How many of the recently shown lines may name the SAME subject before a
#: further line about it is dropped. The similarity gate above compares
#: WORDING, so "Buffalo's still where I left it" and "back in the Buffalo" score
#: far below its threshold and both go through — different sentences, same
#: subject, over and over. Watching that back it reads like a stuck bot rather
#: than someone talking, which is the one thing the feed cannot afford (operator,
#: 2026-09-04: "he talks about random buffalo buffalo loop... it sounds fake like
#: fake AI generated"). Two mentions inside the window is a callback; a third is
#: a loop.
SUBJECT_REPEAT_MAX = 2

#: Words that are Capitalised mid-sentence but are not really subjects: the
#: brand and character names the show says constantly by design, plus the
#: sentence-initial "I" that survives the mid-sentence test in quoted speech.
#: Kept deliberately tiny — anything else repeated three times in five lines is
#: a loop whether it is a car, a street or a person.
SUBJECT_STOPWORDS: frozenset[str] = frozenset({"i", "im", "ive", "ill", "id", "wanted", "los", "santos"})

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
    """The last things WANTED said, so the prompt's no-repeat rule is enforceable."""

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


class Commentary:
    def __init__(self, state_dir: Path, rng: random.Random | None = None) -> None:
        path = state_dir / "commentary_rotation.json"
        self.death = LineRotation("death", DEATH_LINES, path, rng)
        self.busted = LineRotation("busted", BUSTED_LINES, path, rng)
        self.recent = RecentLines(path)
        #: In-memory only, deliberately not persisted: this is a same-session
        #: "did I just say something like this" filter, not a long-term
        #: no-repeat memory (RecentLines already owns that, on disk).
        self._shown: deque[str] = deque(maxlen=SIMILARITY_WINDOW)

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
    def _subjects(line: str) -> set[str]:
        """The proper nouns a line is ABOUT: tokens capitalised mid-sentence.

        Sentence-initial words are skipped because every line starts with a
        capital and that says nothing about its subject. This is a deliberately
        dumb reader — no model call, no name list — because the thing it has to
        catch (`Buffalo`, `Vespucci`, `Trevor` three lines running) is exactly
        what capitalisation already marks in ordinary English prose.
        """
        subjects: set[str] = set()
        starts_sentence = True
        for raw in line.split():
            word = raw.strip("\"'“”‘’(),.:;!?—-")
            if not word:
                continue
            if not starts_sentence and word[:1].isupper() and word.isalpha():
                token = word.lower()
                if token not in SUBJECT_STOPWORDS:
                    subjects.add(token)
            starts_sentence = raw.endswith((".", "!", "?", "…"))
        return subjects

    def gate_say(self, line: str) -> bool:
        """Should `line` actually be shown (overlay/feed) right now?

        Compares against the last SIMILARITY_WINDOW lines this method has
        already approved, using cheap normalized-token-overlap (no model
        call). Too similar to a recent one → drop it and return False; the
        caller simply does not publish a new "say" event, so whatever line is
        already on screen stays there rather than being replaced by a
        near-duplicate. Banner-pool rotation (`death_line`/`busted_line`) is
        untouched by this — it is a separate, deliberately-repeating pool with
        its own no-repeat-in-10 rule, not decision-derived commentary.

        This does NOT touch `record_say`/`RecentLines`: the brain's own
        no-repeat memory must always see the real line the model actually
        said, never this gate's opinion of it, or the "don't repeat yourself"
        instruction in the prompt would be lying to the model about what it
        said last.
        """
        for prior in self._shown:
            similarity = _jaccard_similarity(line, prior)
            if similarity > SIMILARITY_THRESHOLD:
                log.debug(
                    "commentary line suppressed: too similar to a recently shown one",
                    extra={
                        "kv": {
                            "say": line[:120],
                            "prior": prior[:120],
                            "similarity": round(similarity, 3),
                        }
                    },
                )
                return False
        subjects = self._subjects(line)
        if subjects:
            for subject in sorted(subjects):
                seen = sum(1 for prior in self._shown if subject in self._subjects(prior))
                if seen >= SUBJECT_REPEAT_MAX:
                    log.debug(
                        "commentary line suppressed: same subject too many times running",
                        extra={"kv": {"say": line[:120], "subject": subject, "seen": seen}},
                    )
                    return False
        self._shown.append(line)
        return True

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
