"""Who is actually on screen — the story characters, keyed by the game's own ped models.

WHY THIS FILE EXISTS — observed live 2026-09-02, with screenshots. The agent spent a
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
damaging thing the agent can do - it reads as the show being fake.

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
    "player_zero": "Michael",
    "player_one": "Franklin",
    "player_two": "Trevor",
    "ig_lamardavis": "Lamar",
    "ig_davenorton": "Dave",
    "ig_simeon": "Simeon",
    "ig_lestercrest": "Lester",
    "ig_jimmydisanto": "Jimmy",
    "ig_tracydisanto": "Tracey",
    "ig_amandatownley": "Amanda",
    "ig_ron": "Ron",
    "ig_wade": "Wade",
    "ig_stretch": "Stretch",
    "ig_tanisha": "Tanisha",
    "ig_denise": "Denise",
    "ig_franklin": "Franklin",
    "ig_michael": "Michael",
    "ig_trevor": "Trevor",
    "a_c_chop": "Chop",
}

#: Every name the table can produce. A capitalised word in a line is only ever
#: challenged when it is one of THESE — so street names, zone names, car models
#: and ordinary sentence-initial words are never touched by the check.
CHECKED_NAMES: frozenset[str] = frozenset(PED_MODEL_NAMES.values())


def name_for_model(model: str | None) -> str | None:
    """The display name for a ped model, or None when we do not know it."""
    if not model:
        return None
    return PED_MODEL_NAMES.get(model.strip().lower())


def present_names(state: Any) -> set[str]:
    """Every story character the agent can legitimately talk about right now:
    whoever is in `nearby.peds` this tick, plus whoever he currently IS.

    `player.protagonist` is included because he is always entitled to refer to
    himself by name — he is the one person guaranteed to be on screen.
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
    return names


def absent_names_mentioned(text: str, allowed: Iterable[str]) -> list[str]:
    """Story names used in `text` that belong to nobody currently around.

    Word-boundary matching on the CHECKED_NAMES set only. A name that is not in
    the table is not challenged (we have no opinion about it), and a name that
    IS in the table but present in `allowed` is fine.
    """
    import re

    permitted = {n.lower() for n in allowed}
    offenders: list[str] = []
    for name in CHECKED_NAMES:
        if name.lower() in permitted:
            continue
        if re.search(rf"\b{re.escape(name)}\b", text, flags=re.IGNORECASE):
            offenders.append(name)
    return sorted(offenders)


def unknown_story_models(state: Any) -> list[str]:
    """`ig_*` models seen in `nearby.peds` that this table has no name for.

    Logged (once per model, by the caller's own throttling) so the table can be
    extended from real sightings instead of from memory.
    """
    peds = getattr(getattr(state, "nearby", None), "peds", None) or []
    unknown: set[str] = set()
    for ped in peds:
        model = (getattr(ped, "model", None) or "").strip().lower()
        if model.startswith("ig_") and model not in PED_MODEL_NAMES:
            unknown.add(model)
    return sorted(unknown)
