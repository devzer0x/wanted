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

import difflib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
MISSIONS_FILE = KNOWLEDGE_DIR / "missions.json"
HUD_LEGEND_FILE = KNOWLEDGE_DIR / "hud_legend.md"

#: Hard cap on an injected card. ~600 tokens; it rides in the dynamic (uncached) context.
MAX_CARD_CHARS = 2400
#: Fuzzy-match threshold for a screen-read title against a known mission name.
TITLE_MATCH_RATIO = 0.8


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


def identify_mission(title_text: str | None, zone: str | None) -> dict[str, Any] | None:
    """Best-effort identification: a screen-read title first, then a zone that maps to exactly
    one mission (e.g. North Yankton -> Prologue). Returns None rather than guessing."""
    missions = load_missions()
    if not missions:
        return None
    if title_text:
        t = _norm(title_text)
        names = [_norm(m["name"]) for m in missions]
        if t in names:
            return missions[names.index(t)]
        best = difflib.get_close_matches(t, names, n=1, cutoff=TITLE_MATCH_RATIO)
        if best:
            return missions[names.index(best[0])]
    if zone:
        z = _norm(zone)
        hits = [m for m in missions if z and z in _norm(str(m.get("start_zone", "")))]
        if len(hits) == 1:
            return hits[0]
    return None


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
