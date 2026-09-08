"""Who is actually on screen — the story characters, keyed by the game's own ped models.

WHY THIS FILE EXISTS — observed live 2026-09-02, with screenshots. WANTED spent a
whole mission talking about people who were not there:

    "survive the ambush, keep Dave alive"
    "Objective wants Trevor on a sniper rifle. I'm Franklin standing near Lamar."
    "Dave who? I've got Lamar and eight sports cars I'm ignoring."

He was playing `armenian1` (Franklin and Lamar). Dave Norton and a sniping
Trevor belong to a different mission entirely; they reached him because the
harness had guessed the mission from the ZONE and handed him the wrong
walkthrough card (fixed in `mission_knowledge.identify_mission_with_source`).

That root cause is fixed, but the failure mode it exposed is worth closing for
good: nothing checked whether a name the model used corresponded to anybody in
the world. On a live stream, confidently naming an absent character is the most
damaging thing WANTED can do - it reads as the show being fake.

WHAT THIS IS, AND WHAT IT IS DELIBERATELY NOT:

* A mapping from **ped model name to display name**. Model names are the game's
  own identifiers, visible in `nearby.peds[].model` on every snapshot
  (CONTRACTS §1), so this table is checkable against reality rather than
  remembered: if the mapping is wrong, the log line for an unknown `ig_*` model
  says so (see :func:`unknown_story_models`).
* NOT a claim about who is in any given mission. Nothing here says "Lamar is in
  armenian1" - that would be exactly the invented knowledge this file exists to
  prevent. It only answers "is this name attached to a ped that is in
  `nearby.peds` RIGHT NOW".
* NOT exhaustive. Missing entries cost nothing: an unrecognised model simply has
  no name, and a name nobody in the table owns is not checked. The table only
  ever has to be RIGHT, never complete.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..logsetup import get_logger

log = get_logger("wasted.characters")

#: Ped model -> the name a person would use out loud. The three protagonists are
#: the game's own `player_zero/one/two` models (already surfaced separately as
#: `player.protagonist`, CONTRACTS v1.7); the rest are the `ig_` ("ingame")
#: story models. Extend as missions unlock — an unknown model is logged, never
#: guessed.
PED_MODEL_NAMES: dict[str, str] = {
    # Keys are written bare (no `ig_` prefix) because that is what the bridge
    # emits; `name_for_model` resolves prefixed spellings onto these.
    "player_zero": "Michael",
    "player_one": "Franklin",
    "player_two": "Trevor",
    "lamardavis": "Lamar",
    "davenorton": "Dave",
    "simeon": "Simeon",
    "lestercrest": "Lester",
    "jimmydisanto": "Jimmy",
    "tracydisanto": "Tracey",
    "amandatownley": "Amanda",
    "ron": "Ron",
    "wade": "Wade",
    "stretch": "Stretch",
    "tanisha": "Tanisha",
    "denise": "Denise",
    "franklin": "Franklin",
    "michael": "Michael",
    "trevor": "Trevor",
    "chop": "Chop",
}

#: Every name the table can produce. A capitalised word in a line is only ever
#: challenged when it is one of THESE — so street names, zone names, car models
#: and ordinary sentence-initial words are never touched by the check.
CHECKED_NAMES: frozenset[str] = frozenset(PED_MODEL_NAMES.values())


#: Prefixes the game puts on ped models that carry no identity. The bridge
#: reports the model as SHVDN gives it, which drops `ig_` — observed live
#: 2026-09-02: Lamar came through as `lamardavis`, not `ig_lamardavis`, and the
#: grounding check dropped a perfectly true line about the man standing 1 m away.
#: Lookup therefore tries the name as-is, then with each prefix stripped, then
#: with `ig_` added.
_MODEL_PREFIXES = ("ig_", "a_c_", "csb_", "cs_", "u_m_y_", "u_m_m_")


def name_for_model(model: str | None) -> str | None:
    """The display name for a ped model, or None when we do not know it.

    Prefix-tolerant: `ig_lamardavis`, `lamardavis` and `IG_LamarDavis` all
    resolve to Lamar, because different layers spell the same ped differently.
    """
    if not model:
        return None
    raw = model.strip().lower()
    if raw in PED_MODEL_NAMES:
        return PED_MODEL_NAMES[raw]
    for prefix in _MODEL_PREFIXES:
        if raw.startswith(prefix) and raw[len(prefix):] in PED_MODEL_NAMES:
            return PED_MODEL_NAMES[raw[len(prefix):]]
        candidate = prefix + raw
        if candidate in PED_MODEL_NAMES:
            return PED_MODEL_NAMES[candidate]
    return None


def present_names(state: Any) -> set[str]:
    """Every story character WANTED can legitimately talk about right now:
    whoever is in `nearby.peds` this tick, whoever `mission.entity_blips[]`
    names (v1.11), plus whoever he currently IS.

    `player.protagonist` is included because he is always entitled to refer to
    himself by name — he is the one person guaranteed to be on screen.

    `mission.entity_blips[].name` (CONTRACTS v1.11) is the game's own
    map-legend text for a blip pinned to a ped/vehicle, and it outlives
    `nearby.peds`' ~50 m radius: when a followed crewmate drives off, his dot
    (and his name) are still here even though he vanished from `nearby`. Before
    this, a true line like "Lamar's a ghost now, his dot is way out there" was
    silenced as a hallucination, because the only source this function read
    was a list the man had already left.
    """
    names: set[str] = set()
    protagonist = getattr(getattr(state, "player", None), "protagonist", None)
    if isinstance(protagonist, str) and protagonist:
        names.add(protagonist.strip().capitalize())
    peds = getattr(getattr(state, "nearby", None), "peds", None) or []
    for ped in peds:
        found = name_for_model(getattr(ped, "model", None))
        if found:
            names.add(found)
    blips = getattr(getattr(state, "mission", None), "entity_blips", None) or []
    for blip in blips:
        name = getattr(blip, "name", None)
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def has_unidentified_friendly(state: Any) -> bool:
    """Is there a `friendly` ped nearby whose model we cannot name?

    If so, the grounding check must stay quiet: a friendly ped in a mission is
    almost always a named crew member, and one we have no entry for could be
    exactly the person the line names. Absence of evidence is not evidence of
    absence — and silencing a TRUE line is worse than letting one through.
    """
    peds = getattr(getattr(state, "nearby", None), "peds", None) or []
    return any(
        getattr(p, "relationship", None) == "friendly" and name_for_model(getattr(p, "model", None)) is None
        for p in peds
    )


#: Words that mean "this person is NOT here", which is a perfectly true thing to
#: say about somebody who is not here. Observed live 2026-09-02: the check
#: silenced "Lamar's a ghost now" and "empty Lamar-shaped hole" during a follow
#: mission where Lamar had genuinely vanished — the single most interesting
#: thing happening, and accurate. The harm this check exists to prevent is
#: asserting that an absent character is PRESENT and doing things; narrating
#: their absence is the opposite of that.
_ABSENCE_WORDS = (
    "gone", "ghost", "missing", "lost", "vanish", "disappear", "nowhere",
    "no sign", "without", "left me", "ditched", "shook me", "empty", "alone",
    "where is", "where's", "lose", "losing", "lost him", "shows", "turns up",
)


def absent_names_mentioned(text: str, allowed: Iterable[str]) -> list[str]:
    """Story names used in `text` that belong to nobody currently around.

    Word-boundary matching on the CHECKED_NAMES set only. A name that is not in
    the table is not challenged (we have no opinion about it), and a name that
    IS in the table but present in `allowed` is fine.

    A line that is plainly ABOUT somebody's absence is allowed to name them —
    see :data:`_ABSENCE_WORDS`.
    """
    import re

    lowered = text.lower()
    if any(word in lowered for word in _ABSENCE_WORDS):
        return []

    permitted = {n.lower() for n in allowed}
    offenders: list[str] = []
    for name in CHECKED_NAMES:
        if name.lower() in permitted:
            continue
        if re.search(rf"\b{re.escape(name)}\b", text, flags=re.IGNORECASE):
            offenders.append(name)
    return sorted(offenders)


def _looks_like_a_story_ped(model: str) -> bool:
    """Story peds are named models (`ig_*`, or a bare personal name); ambient
    crowd peds are coded (`genstreet01amy`, `stwhi02amy`, `beach01amo` — all
    seen live). The digits are the tell: no story character's model carries a
    two-digit crowd index."""
    import re

    return not re.search(r"\d", model)


def unknown_story_models(state: Any) -> list[str]:
    """`ig_*` models seen in `nearby.peds` that this table has no name for.

    Logged (once per model, by the caller's own throttling) so the table can be
    extended from real sightings instead of from memory.
    """
    peds = getattr(getattr(state, "nearby", None), "peds", None) or []
    unknown: set[str] = set()
    for ped in peds:
        model = (getattr(ped, "model", None) or "").strip().lower()
        if model and name_for_model(model) is None and _looks_like_a_story_ped(model):
            unknown.add(model)
    return sorted(unknown)
