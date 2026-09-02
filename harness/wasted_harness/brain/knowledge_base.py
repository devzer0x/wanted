"""Retrieval-bounded GTA V knowledge for the brain — the general-mechanics counterpart to
`mission_knowledge.py`'s per-mission card.

Why this exists: a research workflow produces a large knowledge base as JSON files under
`brain/knowledge/<domain>.json` (hud_icons, map_markers, police_system, driving, combat,
npc_entities, random_events, activities_freeroam, controls_interactions, vehicles,
aircraft_water, failure_recovery, world_common_sense) plus `mission_states.json`. Injecting all
of it into every model call would multiply the cost of the cached prompt prefix (already ~14.9k
tokens; a warm tactical call is ~$0.0026 at ~240 calls/hour — CONTRACTS §3). So this module never
puts knowledge in the static prefix: it RETRIEVES a small, situational slice per tick and hands
the caller a bounded block of prose for the DYNAMIC (uncached) context, the same place
`mission_knowledge.mission_card()` already rides.

Design constraints, matching `mission_knowledge.py`:
* No fabrication. A missing or malformed data file returns empty, never raises — the research
  workflow may not have produced these files yet, exactly like `mission_knowledge.load_missions()`
  before `missions.json` existed. One WARNING is logged per bad file, never a crash.
* `select()` and `mission_state_hint()` are honest about uncertainty: when the current situation
  cannot be pinned down (which mission state the agent is in, whether a hostile is actually nearby),
  they return less, or a documented fallback, never an invented specific.
* This module is not wired into `main.py` here — the orchestrator does that in a separate pass.
"""

from __future__ import annotations

import json
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from ..logsetup import get_logger

log = get_logger("wasted.knowledge_base")

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
MISSION_STATES_FILE = KNOWLEDGE_DIR / "mission_states.json"

#: The domain files the research workflow is expected to produce (brief, 2026-09-02). Not every
#: name needs to exist for this module to work — `load_domain` treats an absent file as `[]`,
#: same contract as `mission_knowledge.load_missions()` before `missions.json` landed.
DOMAINS: tuple[str, ...] = (
    "hud_icons",
    "map_markers",
    "police_system",
    "driving",
    "combat",
    "npc_entities",
    "random_events",
    "activities_freeroam",
    "controls_interactions",
    "vehicles",
    "aircraft_water",
    "failure_recovery",
    "world_common_sense",
)

# ---- budgets / thresholds (every one has a one-line rationale, mission_knowledge.py style) ----

#: Shared default for both `select()` and `render()`. ~1800 chars is roughly 450 tokens of
#: DYNAMIC context — small next to the ~14.9k cached prefix, but enough for a handful of items.
#: Deliberately smaller than `mission_knowledge.MAX_CARD_CHARS` (2400): retrieved knowledge is
#: ADDITIVE to the mission card in the same dynamic context, not a replacement for it, so the two
#: budgets must not both max out on the same tick.
DEFAULT_BUDGET_CHARS = 1800

#: Rough length of one rendered line (`- <cue> -> <meaning> | do: ... | avoid: ...`). Used only to
#: turn a char budget into an item-count cap in `select()` *before* paying for `render()`'s own
#: exact accounting — an estimate, not the truncation rule itself (`render()` truncates for real).
AVG_ITEM_RENDER_CHARS = 100

#: Dead/arrested: nothing else is relevant except how to get back up. Kept tight — a screen is
#: likely already blocking (busted/wasted), so this rides the next reaction call, not a wall of text.
FAILURE_ONLY_MAX_ITEMS = 6

#: Cutscene / locked control: he cannot act at all. A short reminder not to narrate the frozen
#: world, and nothing else — "nothing else" per the brief, enforced by returning early.
CUTSCENE_MAX_ITEMS = 3

#: Keyword match (category/cue/context, case-insensitive substring) for the cutscene/locked-control
#: set. Keyword- rather than domain-based on purpose: which domain file a data author puts these
#: facts in (controls_interactions is the obvious home, but not a contract) should not matter.
CUTSCENE_KEYWORDS: tuple[str, ...] = (
    "cutscene",
    "locked control",
    "control_locked",
    "control locked",
    "no control",
    "control disabled",
)

#: Wanted level: always at least this many police_system items once wanted > 0 ...
POLICE_ITEMS_BASE = 2
#: ... plus one more per star, so a 5-star chase visibly surfaces more than a 1-star one.
POLICE_ITEMS_PER_STAR = 1
#: ... capped so even 5 stars cannot crowd out every other domain's contribution.
POLICE_ITEMS_MAX = 8

#: Combat + npc_entities together, capped so a chaotic firefight cannot fill the whole budget by
#: itself once combined with everything else already in the candidate pool.
COMBAT_ITEMS_MAX = 8

#: Map-marker + failure-recovery items while a mission is active, capped the same way.
MISSION_ACTIVE_ITEMS_MAX = 8

#: Keyword match for "no marker" / follow-phase map_markers items — the exact class of fact that
#: fixes the gap in `behavior/missions.py` (`MissionFollower`): a follow mission issues NO marker,
#: only a friendly blue dot, and the game does not say so on screen.
FOLLOW_PHASE_KEYWORDS: tuple[str, ...] = (
    "follow",
    "no marker",
    "no_marker",
    "blue dot",
    "no objective blip",
    "objective_blip",
)

#: Vehicle-type routing: display_name/class keyword match (CONTRACTS §1 VehicleState is the only
#: vehicle-type signal /state exposes; there is no fixed enum to hard-code here — CLAUDE.md rule 6).
AIRCRAFT_WATER_KEYWORDS: tuple[str, ...] = (
    "helicopter",
    "heli",
    "plane",
    "jet",
    "boat",
    "seaplane",
    "dinghy",
)
BIKE_KEYWORDS: tuple[str, ...] = ("motorcycle", "motorbike", "bike", "cycle", "bmx", "scooter", "moped")

#: Always show at least this many world_common_sense items, whichever branch below fired (except
#: the two exclusive ones, which explicitly want "nothing else"). Dedup absorbs any overlap.
WORLD_COMMON_SENSE_FLOOR = 2

#: Free roam: rotate the activities/common-sense pool every this many ticks, so idle commentary
#: does not recite the same two facts for a whole session, while staying stable within a short
#: window (repeated calls the same tick/second must not flap).
FREE_ROAM_ROTATION_WINDOW_TICKS = 40
#: ... pulling this many items from the pool each rotation.
FREE_ROAM_ITEMS_PER_TICK = 4

#: Ranking (brief §3): urgency first, then confidence, then a stable id tiebreak so output does
#: not flap tick to tick when nothing about the situation actually changed.
URGENCY_RANK: dict[str, int] = {"critical": 0, "high": 1, "normal": 2, "low": 3}
CONFIDENCE_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

#: An item whose own `context` mentions a mission is mission-gated...
MISSION_WORD = "mission"

#: ...unless it also says it applies outside one. Researchers write scope in prose, not flags, so
#: this pair is how the prose is read: "Missions that hand you a getaway car" is mission-only,
#: while "In free roam or on a mission, sirens mean police nearby" is not.
ALSO_OUTSIDE_MISSION_PHRASES: tuple[str, ...] = (
    "free roam",
    "free-roam",
    "anywhere",
    "at any time",
    "always",
    "any time",
    "everywhere",
    "persistent",
)

#: Per-FIELD ceilings in a rendered knowledge block, applied before the parts are joined.
#: Two things forced this. Item prose from the research agents varies by an order of magnitude, so
#: without a clamp the single wordiest item eats a third of the budget and the items behind it are
#: never shown — retrieval quietly degrades into "show the longest fact". And clamping the joined
#: LINE instead is worse than useless: the line reads `cue -> meaning | do: ... | avoid: ...`, so a
#: tail trim keeps the description and throws away the ACTION, which is the only part that changes
#: what he does. Clamping each field keeps every part present and trims the wordy ones.
ITEM_FIELD_MAX_CHARS: dict[str, int] = {
    "cue": 100,
    "meaning": 140,
    "suggested_action": 130,
    "avoid": 90,
}

#: Hard ceiling on one rendered mission-state hint. Unlike `render()`, this is a single string
#: assembled from data-file prose, and a `blocked_reason` written by a research agent can run to
#: several hundred characters. It lands in the DYNAMIC context, so it is paid for at full input
#: price on every decision — 700 chars (~175 tokens) is enough for the objective, the incapacity
#: and the recovery, and stops one verbose row from quietly doubling the per-call cost.
MISSION_HINT_MAX_CHARS = 700

KNOWLEDGE_HEADER = "KNOWLEDGE (retrieved for this moment):"


# ---- loading -------------------------------------------------------------------------------


def _warn_malformed(label: str, path: Path, reason: str) -> list[dict[str, Any]]:
    # A bad data file must not take the show down (CLAUDE.md rule 1's spirit applied to
    # research-workflow output): log once, carry on as if the file were absent.
    log.warning(
        "knowledge file malformed; ignoring it for this process",
        extra={"kv": {"file": label, "path": str(path), "error": reason}},
    )
    return []


def _read_items(path: Path, label: str) -> list[dict[str, Any]]:
    """Shared load path for a domain file / mission_states.json: `[]` on anything short of a
    well-formed `{"items"/"missions": [...]}` (or bare list) document. Never raises."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return _warn_malformed(label, path, str(exc))
    if isinstance(data, list):
        items: Any = data
    elif isinstance(data, dict):
        items = data.get("items", data.get("missions"))
    else:
        items = None
    if not isinstance(items, list):
        return _warn_malformed(label, path, f"expected a list of items, got {type(items).__name__}")
    return [it for it in items if isinstance(it, dict)]


@cache
def load_domain(name: str) -> list[dict[str, Any]]:
    """One knowledge-base domain's items (`brain/knowledge/<name>.json`). `[]` when the file is
    absent or malformed — the research workflow may not have produced it yet."""
    return _read_items(KNOWLEDGE_DIR / f"{name}.json", name)


@lru_cache(maxsize=1)
def load_all() -> dict[str, list[dict[str, Any]]]:
    """Every known domain, keyed by name. Missing files simply contribute `[]`."""
    return {name: load_domain(name) for name in DOMAINS}


@lru_cache(maxsize=1)
def _load_mission_states() -> dict[str, list[dict[str, Any]]]:
    """`mission_states.json`, keyed by normalized mission name. `{}` when absent/malformed."""
    missions = _read_items(MISSION_STATES_FILE, "mission_states.json")
    out: dict[str, list[dict[str, Any]]] = {}
    for m in missions:
        name = m.get("name")
        states = m.get("states")
        if name and isinstance(states, list):
            out[_norm(name)] = [s for s in states if isinstance(s, dict)]
    return out


def _norm(s: str) -> str:
    return " ".join(str(s).lower().split())


# ---- duck-typed state access -----------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Field access that works whether `obj` is a real bridge_client model, a dict, a
    SimpleNamespace test stand-in, or None — so tests can pass simple stand-ins (brief)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _has_hostile(nearby: Any) -> bool:
    peds = _get(nearby, "peds", []) or []
    return any(str(_get(p, "relationship", "")).lower() == "hostile" for p in peds)


def _vehicle_domains(vehicle: Any) -> list[str]:
    """Which domain(s) apply to the vehicle the agent is currently in. A bike/boat/helicopter/plane
    handles nothing like a car (traction, no drift-braking, buoyancy, flight controls), so
    generic car-driving advice would be actively wrong for it."""
    if vehicle is None:
        return ["driving"]
    blob = " ".join(
        str(x)
        for x in (
            _get(vehicle, "display_name", ""),
            _get(vehicle, "vehicle_class", "") or _get(vehicle, "class", ""),
            _get(vehicle, "model", ""),
        )
        if x
    ).lower()
    if any(kw in blob for kw in AIRCRAFT_WATER_KEYWORDS):
        return ["aircraft_water"]
    if any(kw in blob for kw in BIKE_KEYWORDS):
        return ["vehicles"]
    return ["driving"]


def _is_mission_only(item: dict[str, Any]) -> bool:
    """True when an item's own `context` says it applies during a mission.

    `context` is the schema's "when this applies / what must also be true" field, so it is the
    item's own statement about its scope rather than a guess from its prose. Deliberately narrow:
    it reads ONLY `context`, so an item that merely mentions missions in passing while giving
    general advice is kept. Being wrong here is cheap in one direction (a slightly off-topic line)
    and expensive in the other (dropping real advice), so it errs toward keeping.
    """
    ctx = _norm(str(item.get("context", "")))
    if MISSION_WORD not in ctx:
        return False
    return not any(kw in ctx for kw in ALSO_OUTSIDE_MISSION_PHRASES)


def _text_blob(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(k, "")) for k in ("category", "cue", "context", "meaning")
    ).lower()


def _matches_any(item: dict[str, Any], keywords: tuple[str, ...]) -> bool:
    blob = _text_blob(item)
    return any(kw in blob for kw in keywords)


# ---- ranking / dedup --------------------------------------------------------------------------


def _rank_key(item: dict[str, Any]) -> tuple[int, int, str]:
    return (
        URGENCY_RANK.get(str(item.get("urgency", "")).lower(), len(URGENCY_RANK)),
        CONFIDENCE_RANK.get(str(item.get("confidence", "")).lower(), len(CONFIDENCE_RANK)),
        str(item.get("id", "")),
    )


def _rank(
    items: list[dict[str, Any]], priority_ids: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Sort: urgency, then preference, then confidence, then id — stable tick to tick.

    `priority_ids` is how `select()` implements "prefer follow-phase/no-marker items" (rule 6).
    The preference is deliberately applied **inside** the urgency tier, not above it. Ranking it
    above urgency was the other available reading, and it is wrong for one concrete situation:
    four stars and a helicopter overhead while a follow mission is running. Both are true at once,
    the budget only fits a handful of lines, and "you are being hunted" has to reach him before
    "there is no marker on a follow phase". Urgency is the field that exists to say which of two
    true things matters more; overriding it would make the field decorative in exactly the case it
    was written for. Within one tier the preference is decisive, which is all rule 6 needs.
    """
    urgency, confidence, ident = 0, 1, 2

    def key(it: dict[str, Any]) -> tuple[int, int, int, str]:
        ranked = _rank_key(it)
        preferred = 0 if it.get("id") in priority_ids else 1
        return (ranked[urgency], preferred, ranked[confidence], str(ranked[ident]))

    return sorted(items, key=key)


def _dedup(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First occurrence wins per id. Items without an id (malformed data) are never deduped
    against each other — dropping them silently on a missing key would be its own fabrication."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        key = it.get("id")
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(it)
    return out


def _rotate(pool: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    """A slice of `pool`, ranked, that changes every `FREE_ROAM_ROTATION_WINDOW_TICKS` ticks."""
    ranked = _rank(_dedup(pool))
    n = len(ranked)
    if n == 0:
        return []
    bucket = (max(seed, 0) // FREE_ROAM_ROTATION_WINDOW_TICKS) % n
    return [ranked[(bucket + i) % n] for i in range(min(FREE_ROAM_ITEMS_PER_TICK, n))]


# ---- selection ---------------------------------------------------------------------------------


def select(
    state: Any,
    *,
    mission: dict[str, Any] | None = None,
    damage_taken: bool = False,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
) -> list[dict[str, Any]]:
    """Pick the knowledge items that matter for THIS tick, ranked and deduplicated.

    `state` is duck-typed (a real `bridge_client.GameState`, or a test stand-in exposing the same
    attribute names) so tests do not need to construct a full pydantic model.

    `mission`: the currently identified mission record (`mission_knowledge.identify_mission`'s
    return value), accepted so the caller (which already tracks `self.current_mission`) has a
    natural place to pass it, and reserved for future per-mission ranking bias. `state.mission`
    (the CONTRACTS §1 fields) is what drives every rule below; behavior does not depend on whether
    a caller supplies `mission`.
    """
    player = _get(state, "player")
    game_mission = _get(state, "mission")

    # 1. Terminal states outrank everything: dead/arrested, nothing to retrieve knowledge FOR
    # except how to get back up.
    if bool(_get(player, "dead", False)) or bool(_get(player, "arrested", False)):
        return _rank(_dedup(load_domain("failure_recovery")))[:FAILURE_ONLY_MAX_ITEMS]

    # 2. No agency at all: a cutscene, or the engine has taken control away. Every action is
    # `wait`; driving/combat/police tips he cannot act on would just be prompt noise. "Nothing
    # else" per the brief, enforced by returning immediately.
    control_enabled = _get(player, "control_enabled", True)
    if bool(_get(game_mission, "cutscene_active", False)) or control_enabled is False:
        pool = [it for items in load_all().values() for it in items if _matches_any(it, CUTSCENE_KEYWORDS)]
        return _rank(_dedup(pool))[:CUTSCENE_MAX_ITEMS]

    candidates: list[dict[str, Any]] = []
    # Ids that must sort ahead of everything else regardless of their own urgency/confidence —
    # populated below by rule 6 (follow-phase/no-marker map_markers items) and applied at the
    # final rank() call, since this module only ever advises the model in prose; it never drives
    # an action (that safety-critical wheel is elsewhere — see MissionFollower's own docstring
    # point 6), so a follow-phase reminder briefly outranking a police fact on the same tick costs
    # nothing but is exactly what the brief's "prefer" instruction asks for.
    priority_ids: set[str] = set()

    # 3. Wanted level: police_system facts, more of them the hotter it runs. "Prefer urgency
    # critical/high" is already what the shared rank key does; scaling the slice size with wanted
    # just means more of that same priority-ordered list gets through.
    wanted = int(_get(player, "wanted", 0) or 0)
    if wanted > 0:
        n = min(POLICE_ITEMS_MAX, POLICE_ITEMS_BASE + wanted * POLICE_ITEMS_PER_STAR)
        candidates += _rank(load_domain("police_system"))[:n]

    # 4. A live threat: a hostile ped in range, or health dropping right now even with nothing
    # hostile in `nearby` (explosion, fall, an off-screen shooter) — `damage_taken` covers that case.
    if _has_hostile(_get(state, "nearby")) or damage_taken:
        candidates += _rank(load_domain("combat") + load_domain("npc_entities"))[:COMBAT_ITEMS_MAX]

    # 5. On wheels: car-driving knowledge by default; a bike/boat/helicopter/plane gets its own
    # domain instead of wrong car advice.
    if bool(_get(player, "in_vehicle", False)):
        for domain in _vehicle_domains(_get(state, "vehicle")):
            candidates += load_domain(domain)

    # 6. A mission is running: failure/recovery plus map-marker reading. When `objective_blip` is
    # None the game has issued no marker at all (a follow mission — the exact gap that lost Lamar
    # in behavior/missions.py's MissionFollower docstring), so those map_markers items are
    # PREFERRED: their ids go into `priority_ids` and outrank every other item at the final sort,
    # not just their non-preferred map_markers siblings.
    if bool(_get(game_mission, "active", False)):
        map_markers = load_domain("map_markers")
        no_marker = _get(game_mission, "objective_blip", None) is None
        follow_ids = frozenset(
            it["id"]
            for it in map_markers
            if it.get("id") and no_marker and _matches_any(it, FOLLOW_PHASE_KEYWORDS)
        )
        priority_ids |= follow_ids
        mission_pool = load_domain("failure_recovery") + map_markers
        candidates += _rank(mission_pool, priority_ids=follow_ids)[:MISSION_ACTIVE_ITEMS_MAX]

    # 7. Nothing above fired: free roam. Rotated by `state.tick` (always present, CONTRACTS §1) so
    # idle commentary does not recite the same couple of facts all session.
    if not candidates:
        pool = load_domain("activities_freeroam") + load_domain("world_common_sense")
        candidates += _rotate(pool, seed=int(_get(state, "tick", 0) or 0))

    # 8. Floor: a couple of world_common_sense items regardless of which branch fired above.
    # Dedup below absorbs the overlap when free roam already included them.
    candidates += load_domain("world_common_sense")[:WORLD_COMMON_SENSE_FLOOR]

    # 9. With no mission running, drop the items that only apply inside one. Several of them are
    # tagged `critical` ("do not abandon the mission vehicle", "an ally dying fails the mission"),
    # and urgency is the first sort key — so without this they win the top slots on EVERY free-roam
    # tick and the same two irrelevant lines are all he ever sees while driving around. They are
    # genuinely critical, just not now; the mission branches above put them back the moment one
    # starts.
    if not _get(game_mission, "active", False):
        candidates = [it for it in candidates if not _is_mission_only(it)]

    ranked = _rank(_dedup(candidates), priority_ids=frozenset(priority_ids))
    max_items = max(1, budget_chars // AVG_ITEM_RENDER_CHARS)
    return ranked[:max_items]


# ---- render --------------------------------------------------------------------------------


def _clamp(item: dict[str, Any], field: str) -> str:
    """One item field, trimmed to its ceiling on a word boundary (never mid-word: a fragment
    ending "do not fire at the hos" reads as corruption and invites the model to fill the rest in
    itself)."""
    text = str(item.get(field, "")).strip()
    cap = ITEM_FIELD_MAX_CHARS.get(field)
    if cap is None or len(text) <= cap:
        return text
    return text[:cap].rsplit(" ", 1)[0] + " ..."


def _render_line(item: dict[str, Any]) -> str:
    cue = _clamp(item, "cue")
    meaning = _clamp(item, "meaning")
    if not cue and not meaning:
        return ""
    line = f"- {cue} -> {meaning}"
    action = _clamp(item, "suggested_action")
    if action:
        line += f" | do: {action}"
    avoid = _clamp(item, "avoid")
    if avoid:
        line += f" | avoid: {avoid}"
    return line


def render(items: list[dict[str, Any]], budget_chars: int = DEFAULT_BUDGET_CHARS) -> str:
    """A compact prose block for the model — never raw JSON. Hard-truncates to `budget_chars` on
    an item boundary (never mid-word): each candidate line is added only if the WHOLE line still
    fits. `""` when there is nothing (the caller's `_dynamic_context`-style joiner filters empties,
    same convention as `mission_knowledge.mission_card`)."""
    if not items:
        return ""
    lines = [KNOWLEDGE_HEADER]
    for it in items:
        line = _render_line(it)
        if not line:
            continue
        trial = "\n".join([*lines, line])
        if len(trial) > budget_chars:
            break
        lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""


# ---- mission-state hint ----------------------------------------------------------------------


def _render_state_hint(s: dict[str, Any]) -> str:
    objective = str(s.get("objective_text", "")).strip()
    parts = [f"MISSION STATE: {objective}" if objective else "MISSION STATE:"]
    if s.get("executable") is False:
        # A state the critics marked as needing something outside the frozen action vocabulary -
        # an aimed shot at one named thing, most often. Saying "do: shoot the alarm box" to an
        # agent with no aim and no fire action is how he ends up narrating a shot he never took,
        # so the incapacity is stated FIRST and the expected_action is framed as best effort.
        reason = str(s.get("blocked_reason", "")).strip() or "this needs an action you do not have"
        parts.append(f"YOU CANNOT DO THIS STEP DIRECTLY ({reason}) - get into position and let the "
                     f"game's own scripting resolve it; do not claim you did it")
    action = str(s.get("expected_action", "")).strip()
    if action:
        parts.append(f"do: {action}")
    wait_s = s.get("max_wait_s")
    if isinstance(wait_s, (int, float)) and not isinstance(wait_s, bool):
        # Waiting is often correct here; waiting FOREVER never is. Seven of these states used to
        # suppress stall detection with no ceiling at all.
        parts.append(f"waiting is fine for up to ~{int(wait_s)}s, then escalate")
    recovery = str(s.get("recovery", "")).strip()
    if recovery:
        parts.append(f"if stuck: {recovery}")
    hint = " | ".join(parts)
    if len(hint) <= MISSION_HINT_MAX_CHARS:
        return hint
    # Trim on a word boundary rather than mid-word: a hint that ends "do not fire at the hos"
    # reads as corruption and invites the model to guess at the rest.
    return hint[:MISSION_HINT_MAX_CHARS].rsplit(" ", 1)[0] + " ..."


def mission_state_hint(mission_name: str | None, state: Any) -> str | None:
    """A short hint for the most plausible CURRENT state of the active mission, or `None` when
    this cannot honestly be pinned down.

    Priority: a state tagged `has_marker: false` when `mission.objective_blip is None` (the
    follow-phase case — see `select()` rule 6 and `behavior/missions.py`'s `MissionFollower`).
    Otherwise the mission's first state is returned as an honest "best guess, low confidence"
    default — never a fabricated specific state. `None` when the mission is unknown or carries
    no recorded states.
    """
    if not mission_name:
        return None
    states = _load_mission_states().get(_norm(mission_name))
    if not states:
        return None
    game_mission = _get(state, "mission")
    no_marker = _get(game_mission, "objective_blip", None) is None
    if no_marker:
        candidates = [s for s in states if _get(s, "has_marker", True) is False]
        if candidates:
            return _render_state_hint(candidates[0])
    # Cannot tell which state he is in beyond the marker signal above: the honest fallback is
    # the mission's first recorded state, not a guess at progress we have no signal for.
    return _render_state_hint(states[0])
