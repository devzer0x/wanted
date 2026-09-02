"""Mission knowledge base: identify the active story mission and hand the brain a compact card.

Why this exists: /state carries no mission name and no objective text. The only signals are
`mission.active`, a nullable `objective_blip`, the zone, and - since CONTRACTS v1.3 - a
screenshot at `mission_start`. A walkthrough for the CURRENT mission (objectives in order, fail
conditions, tips, who the crew is) is what a human player has in their head; without it the agent
stood in the prologue with no idea what the job wanted.

Design constraints:
* The knowledge is data on disk (`brain/knowledge/missions.json`), NOT prompt-prefix text: 69
  missions of objectives would multiply the cached-prefix cost of every call. One mission card
  (bounded to MAX_CARD_CHARS) is injected into the DYNAMIC context while that mission is active.
* No fabrication: if the data file is absent or the mission cannot be identified, functions
  return None/[] and the harness carries on exactly as before.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..logsetup import get_logger

log = get_logger("wasted.brain.mission_knowledge")

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
MISSIONS_FILE = KNOWLEDGE_DIR / "missions.json"
HUD_LEGEND_FILE = KNOWLEDGE_DIR / "hud_legend.md"

#: Hard cap on an injected card. ~600 tokens; it rides in the dynamic (uncached) context.
MAX_CARD_CHARS = 2400
#: Fuzzy-match threshold for a screen-read title against a known mission name.
TITLE_MATCH_RATIO = 0.8

_WRITE_RETRIES = 8
_WRITE_DELAY_S = 0.12


def _atomic_write_json(path: Path, data: object) -> None:
    """Same atomic-write pattern as commentary.py's rotation state: temp file
    + replace, retried, because another process holding the file open on
    Windows turns a plain rename into a sharing violation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
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


class LearnedScripts:
    """script_name -> mission_name pairs LEARNED from real sessions.

    CONTRACTS v1.10 adds `mission.script` (the raw story-mission script name,
    e.g. "armenian1") to /state, but docs/research/brief-mission-scripts.json
    found no verifiable script -> English-title table — shipping guessed
    pairs would be exactly the fabrication CLAUDE.md rule 1 forbids. So the
    mapping starts empty and is learned one mission at a time, only from the
    OCR title path (see `identify_mission_with_source`'s `"title"` source),
    and only ever the FIRST pairing seen for a given script: a conflicting
    later sighting is evidence something is wrong (an ambiguous script name,
    a bad OCR read) and is logged rather than silently overwriting a mapping
    that was already good enough to be learned.

    Persisted the same way commentary.py's rotation state is: atomic write,
    tolerant read. Losing the file costs learning (a slower identification
    next time), never the show.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._map: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(
                "learned mission-script mapping unreadable, starting fresh",
                extra={"kv": {"path": str(self.path), "error": str(exc)[:120]}},
            )
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    @property
    def mapping(self) -> dict[str, str]:
        """A read-only copy — callers change the mapping only via `learn()`."""
        return dict(self._map)

    def get(self, script: str) -> str | None:
        return self._map.get(script)

    def learn(self, script: str, mission_name: str) -> None:
        """Record `script -> mission_name`, once. A conflicting later call
        logs a warning and does NOT overwrite the first pairing learned."""
        existing = self._map.get(script)
        if existing is not None:
            if existing != mission_name:
                log.warning(
                    "mission-script mapping conflict; keeping the first learned pair",
                    extra={"kv": {"script": script, "learned": existing, "new": mission_name}},
                )
            return
        self._map[script] = mission_name
        log.info(
            "learned a mission-script mapping",
            extra={"kv": {"script": script, "mission": mission_name}},
        )
        try:
            _atomic_write_json(self.path, self._map)
        except OSError as exc:
            log.warning(
                "could not persist learned mission-script mapping",
                extra={"kv": {"path": str(self.path), "error": str(exc)[:120]}},
            )


@lru_cache(maxsize=1)
def load_missions() -> list[dict[str, Any]]:
    """All known story missions, ordered. Empty when the data has not been produced yet."""
    if not MISSIONS_FILE.exists():
        return []
    data = json.loads(MISSIONS_FILE.read_text(encoding="utf-8"))
    missions = data["missions"] if isinstance(data, dict) else data
    return sorted((m for m in missions if m.get("name")), key=lambda m: int(m.get("order", 0)))


def hud_legend() -> str | None:
    return HUD_LEGEND_FILE.read_text(encoding="utf-8").strip() if HUD_LEGEND_FILE.exists() else None


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("’", "'").split())


def identify_mission_with_source(
    title_text: str | None,
    zone: str | None,
    *,
    script: str | None = None,
    learned: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The identification ladder, plus WHICH signal produced the answer:
    `"script"` (a learned `mission.script` pairing — CONTRACTS v1.10, checked
    first, exact key only), `"title"` (the screen-read title, exact or
    fuzzy), `"zone"` (fallback), or `None` when nothing matched.

    The source matters to the caller (`main.py`): only a `"title"` match is
    trustworthy enough to LEARN a new script pairing from — a zone can name
    more than one mission in general (that path only fires when exactly one
    hit exists, but it is still a weaker signal than a read title), and a
    `"script"` hit is already-learned data, not new evidence.
    """
    missions = load_missions()
    if not missions:
        return None, None
    names = [_norm(m["name"]) for m in missions]
    if script and learned:
        # The bridge emits the game's own thread name (`Armenian1`); the research
        # allowlist and the docs write it lowercase. Match on a casefolded key so a
        # pairing learned under either casing resolves under both.
        key = script.casefold()
        learned_name = next(
            (v for k, v in learned.items() if k.casefold() == key),
            None,
        )
        if learned_name:
            norm_name = _norm(learned_name)
            if norm_name in names:
                return missions[names.index(norm_name)], "script"
            log.warning(
                "learned mission-script mapping points at an unknown mission name; ignoring",
                extra={"kv": {"script": script, "learned_name": learned_name}},
            )
    if title_text:
        t = _norm(title_text)
        if t in names:
            return missions[names.index(t)], "title"
        best = difflib.get_close_matches(t, names, n=1, cutoff=TITLE_MATCH_RATIO)
        if best:
            return missions[names.index(best[0])], "title"
    if zone and not script:
        # Zone is the weakest signal and it fires ONLY when the engine has not
        # named the running script. Observed live 2026-09-02: the bridge reported
        # `script=Armenian1` while this fallback matched the zone "Pacific Bluffs"
        # and handed the agent the card for "The Wrap Up" — a different mission's
        # objectives, crew and tips, presented as fact. When the engine has named
        # the script and we have not learned that name yet, "unknown" is the only
        # honest answer: the harness carries on with no card (exactly as it does
        # for an unidentified mission) until a read TITLE teaches us the pairing.
        z = _norm(zone)
        hits = [m for m in missions if z and z in _norm(str(m.get("start_zone", "")))]
        if len(hits) == 1:
            return hits[0], "zone"
    return None, None


def identify_mission(
    title_text: str | None,
    zone: str | None,
    *,
    script: str | None = None,
    learned: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Best-effort identification: a learned script mapping first (CONTRACTS
    v1.10, exact), then a screen-read title, then a zone that maps to exactly
    one mission (e.g. North Yankton -> Prologue). Returns None rather than
    guessing. See `identify_mission_with_source` for which signal answered."""
    mission, _source = identify_mission_with_source(
        title_text, zone, script=script, learned=learned
    )
    return mission


def mission_card(m: dict[str, Any], max_chars: int = MAX_CARD_CHARS) -> str:
    """A compact, bounded walkthrough card for the dynamic context."""
    lines = [f"MISSION KNOWLEDGE — {m['name']} (played as {m.get('protagonist', '?')})"]
    if m.get("summary"):
        lines.append(str(m["summary"]).strip())
    objs = [str(o).strip() for o in m.get("objectives", []) if str(o).strip()]
    if objs:
        lines.append("Objectives, in order:")
        lines += [f"  {i + 1}. {o}" for i, o in enumerate(objs)]
    crew = [str(c) for c in m.get("crew_or_companions", []) if c]
    if crew:
        lines.append("Crew with you: " + ", ".join(crew) + " — stay with them; they show as friendly/blue.")
    fails = [str(f) for f in m.get("fail_conditions", []) if f]
    if fails:
        lines.append("Fails if: " + "; ".join(fails[:4]))
    tips = [str(t) for t in m.get("tips", []) if t]
    if tips:
        lines.append("Tips: " + " ".join(f"({i + 1}) {t}" for i, t in enumerate(tips[:5])))
    card = "\n".join(lines)
    if len(card) > max_chars:
        card = card[: max_chars - 1].rstrip() + "…"
    return card
