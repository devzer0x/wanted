"""A deterministic, zero-network stand-in for the model half of the brain.

**THE SEAM.** ``TacticalBrain`` and ``DirectorBrain`` (``wasted_harness/brain/
tactical.py``, ``wasted_harness/brain/director.py``) both hold exactly one
attribute that ever touches the network: ``self._call_api``, a
``brain.tactical.BilledCall`` built in their own ``__init__``. Every other
line of ``TacticalBrain.decide`` / ``DirectorBrain.decide`` — the two-attempt
retry loop, the word-limit-violation feedback appended to the retry context,
the ``mission_active`` model swap, the vision-trigger validation — is pure
Python with no I/O, and is exactly the code this project needs verified while
the API key is disabled. So the fake brain does not replace ``TacticalBrain``/
``DirectorBrain`` at all: it replaces the one attribute that calls out.

    tactical = TacticalBrain(anthropic_client, pricing, on_cost)
    tactical._call_api = FakeBilledCall()          # <-- the whole seam
    director = DirectorBrain(anthropic_client, pricing, on_cost)
    director._call_api = FakeBilledCall()

``anthropic_client`` above is still constructed for real (``anthropic.
Anthropic(api_key=...)``) because ``TacticalBrain.__init__`` and the real
``BilledCall.__init__`` it discards a moment later both store it without
calling it — building the object never makes a network call, only *invoking*
one of its methods would, and this file invokes none. ``install()`` below
builds a harmless placeholder client (no key needed) so callers do not have to
know that.

``FakeBilledCall.run(model_id, prefix, content, max_tokens)`` matches
``BilledCall.run``'s signature exactly (the caller — ``TacticalBrain._call`` /
``DirectorBrain._call`` — does not know or care which one it is holding) and
does what the real one does structurally: turn ``content`` (the dynamic
context string, or — for the director — a list of content blocks whose first
is ``{"type": "text", "text": <that string>}``) into a decision, by reading
the *same* ``STATE: {...json...}`` block ``main._dynamic_context`` always
appends, parsing it with the *real* ``GameState.model_validate`` (so a
malformed synthetic state fails exactly as a malformed real one would), and
then validating the produced decision with the *real*
``DecisionModel.model_validate_json`` — the same schema, the same word-limit
validators, on the exact JSON text a real API response's text block would
carry. This is why the misbehaviour modes below are worth having: they are
not hand-built ``DecisionModel`` instances that skip validation, they are
JSON text pushed through the real validator.

Nothing here calls the network, reads an API key, or imports ``anthropic``'s
HTTP machinery beyond what ``TacticalBrain``/``DirectorBrain`` already need to
construct.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from wasted_harness.brain.characters import PED_MODEL_NAMES, present_names
from wasted_harness.brain.schemas import DecisionModel
from wasted_harness.brain.tactical import DecisionResult
from wasted_harness.bridge_client import GameState

# --------------------------------------------------------------------------- #
#  pulling the salient facts back out of a dynamic context                    #
# --------------------------------------------------------------------------- #

_STATE_MARKER = "STATE: "
_RETRY_MARKER = "RETRY —"


class FakeBrainContextError(ValueError):
    """The content handed to `.run()` does not look like `_dynamic_context`'s output.

    Raised rather than guessed past: fabricating a decision from something
    that is not really game state would be exactly the invented data CLAUDE.md
    rule 1 forbids, only one layer further removed.
    """


def _dynamic_text(content: list[dict[str, Any]] | str) -> str:
    if isinstance(content, str):
        return content
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block["text"])
    raise FakeBrainContextError(
        "director content carried no {'type': 'text'} block; the fake brain has "
        "nothing to read a STATE block out of"
    )


def parse_state_from_context(content: list[dict[str, Any]] | str) -> tuple[str, GameState]:
    """Recover `(dynamic_context_text, GameState)` from a `BilledCall.run` `content` arg.

    The ``STATE: {...}`` block is appended by ``main._dynamic_context`` on
    every call (tactical and director alike) — see main.py's own
    ``_dynamic_context``. ``rfind`` rather than ``find``: it is the LAST such
    marker in the text (memory/recent-lines blocks that follow it are free
    text and could coincidentally contain the substring "STATE: " in prose,
    though none of the shipped prompts do).
    """
    text = _dynamic_text(content)
    idx = text.rfind(_STATE_MARKER)
    if idx == -1:
        raise FakeBrainContextError(
            "no 'STATE: ' block in the dynamic context — this does not look like "
            "output from main._dynamic_context, so there is no real game state to "
            "decide from (the fake brain refuses to invent one)"
        )
    start = idx + len(_STATE_MARKER)
    try:
        obj, _end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise FakeBrainContextError(f"STATE block is not valid JSON: {exc}") from exc
    return text, GameState.model_validate(obj)


def _roam_hint(text: str) -> tuple[str | None, list[str]]:
    """`(locked_goal_id, offered_ids)` read out of `RoamEngine.note()`'s own text.

    `RoamEngine` renders "ROAM CURRENT: <id> — ..." when a goal is locked, or
    "... Set your \"goal\" field to EXACTLY ONE of: id1 | id2 | ... —" when one
    is on offer (behavior/roam.py `RoamEngine.note`, read verbatim before
    writing this). Neither shape is part of `/state`, so this is the only
    place to learn it — and it is real text the harness itself produced this
    tick, not a guess.
    """
    marker = "ROAM CURRENT: "
    idx = text.find(marker)
    if idx != -1:
        rest = text[idx + len(marker) :]
        goal_id = rest.split(" —", 1)[0].strip()
        if goal_id:
            return goal_id, []
    marker = 'Set your "goal" field to EXACTLY ONE of: '
    idx = text.find(marker)
    if idx != -1:
        rest = text[idx + len(marker) :]
        ids_part = rest.split(" —", 1)[0]
        ids = [x.strip() for x in ids_part.split("|") if x.strip()]
        if ids:
            return None, ids
    return None, []


def _is_retry(text: str) -> bool:
    return _RETRY_MARKER in text


def _stable_pick(options: list[str], salt: str) -> str:
    """A DETERMINISTIC choice from `options`, keyed by `salt` — never `random`.

    The brief asks for "a hash of salient fields -> a fixed policy": the same
    salt always yields the same pick, so two replays of the same recorded
    stream produce byte-identical logs.
    """
    h = int(hashlib.sha256(salt.encode("utf-8")).hexdigest(), 16)
    return options[h % len(options)]


def _limit_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[:n])


# --------------------------------------------------------------------------- #
#  the deterministic "plays it straight" policy                               #
# --------------------------------------------------------------------------- #

_ROAM_LINES = (
    "Rolling with it.",
    "Let's see where this goes.",
    "Keeping it moving.",
    "Nothing urgent, just looking around.",
    "Cruising for now.",
    "Might as well see what's out here.",
)
_MISSION_LINES = (
    "Pushing the objective.",
    "Heading for the marker.",
    "On the job.",
    "Closing the distance.",
)
_CUTSCENE_LINES = ("Watching this play out.", "Not my scene to run.", "")
_DOWN_LINES = ("Waiting this one out.", "")


def _policy_decision(text: str, state: GameState) -> dict[str, Any]:
    """`available[0]`'s id in roam; drive/walk toward the objective in a mission;
    `wait` in a cutscene — the brief's own policy, verbatim."""
    tick_salt = f"{state.tick}:{state.player.pos.x:.1f}:{state.player.pos.y:.1f}"

    if state.mission.cutscene_active or state.player.switch_in_progress or state.mission.retry_in_flight:
        return {
            "thought": "The game owns this beat; nothing to decide.",
            "say": _stable_pick(list(_CUTSCENE_LINES), "cutscene:" + tick_salt),
            "mood": "chill",
            "action": {"type": "wait", "params": {"seconds": 2}},
            "goal": "wait out the cutscene",
            "confidence": 0.9,
        }
    if state.player.dead or state.player.arrested:
        return {
            "thought": "Down. Nothing physical to do until the game respawns me.",
            "say": _stable_pick(list(_DOWN_LINES), "down:" + tick_salt),
            "mood": "bored",
            "action": {"type": "wait", "params": {"seconds": 2}},
            "goal": "wait for the respawn",
            "confidence": 0.9,
        }
    if state.mission.active:
        blip = state.mission.objective_blip
        if blip is not None:
            if state.player.in_vehicle:
                action = {
                    "type": "drive_to",
                    "params": {
                        "x": blip.pos.x, "y": blip.pos.y, "z": blip.pos.z,
                        "speed_mps": 20.0, "style": "normal", "arrive_radius_m": 8.0,
                    },
                }
            else:
                action = {
                    "type": "walk_to",
                    "params": {"x": blip.pos.x, "y": blip.pos.y, "z": blip.pos.z, "run": True},
                }
            return {
                "thought": "Objective is marked; close the distance to it.",
                "say": _stable_pick(list(_MISSION_LINES), "mission:" + tick_salt),
                "mood": "chill",
                "action": action,
                "goal": "push the mission objective",
                "confidence": 0.7,
            }
        return {
            "thought": "In a mission with no marker yet; hold position.",
            "say": "",
            "mood": "chill",
            "action": {"type": "wait", "params": {"seconds": 2}},
            "goal": "wait for the objective",
            "confidence": 0.4,
        }

    locked, offered = _roam_hint(text)
    if locked is not None:
        goal = locked
    elif offered:
        goal = offered[0]  # "available[0]'s id", per the brief
    else:
        goal = "see what the day wants"
    return {
        "thought": "Free roam; taking the offered goal and looking around while it runs.",
        "say": _stable_pick(list(_ROAM_LINES), "roam:" + tick_salt),
        "mood": "chill",
        "action": {"type": "look_around", "params": {}},
        "goal": _limit_words(goal, 12),
        "confidence": 0.5,
    }


# --------------------------------------------------------------------------- #
#  the scripted "misbehaviour" mode                                           #
# --------------------------------------------------------------------------- #

#: Deterministically > the DecisionModel `thought` word cap (40); the real
#: `_thought_words` validator soft-truncates it to 40 words + "…" rather than
#: rejecting, so this exercises that code path for real on every call.
_OVERLONG_THOUGHT = (
    "overthinking this decision like it is a merge onto a packed freeway, "
) * 6

#: Prose, not a bare id — simultaneously "prose in goal" AND "an id not on the
#: menu" (see the module docstring / brief): whatever `RoamEngine.note()`
#: actually offered this tick, this text will not literally contain any of
#: those snake_case ids, so `RoamEngine.model_choice` matches nothing and
#: `_begin_roam_goal` falls back to the top of the menu, logging
#: `goal_fallback`.
_PROSE_GOAL = "wandering around looking for something worth doing right now"


def _absent_name(state: GameState) -> str:
    """A story character's name that is NOT `present_names(state)` right now.

    Deterministic order (sorted), so the same state always yields the same
    name. `present_names` is the REAL grounding function `main._apply_decision`
    checks `say` against — reusing it here (rather than a hand-rolled guess at
    who is absent) is what guarantees this always trips the real check.
    """
    present = present_names(state)
    for name in sorted(set(PED_MODEL_NAMES.values())):
        if name not in present:
            return name
    return "Trevor"  # pragma: no cover - every table name would have to be present


def _misbehave_decision(text: str, state: GameState) -> dict[str, Any]:
    """Always pydantic-valid, but wrong in every way `main._apply_decision` /
    `RoamEngine` police at the harness level rather than the schema level:
    prose in `goal` that names no offered id, a `thought` long enough to
    soft-truncate, and a `say` naming someone who is not in `nearby.peds` /
    `mission.entity_blips[]` / `player.protagonist` this tick."""
    name = _absent_name(state)
    return {
        "thought": _OVERLONG_THOUGHT,
        "say": f"{name} is watching my back on this one, no worries.",
        "mood": "smug",
        "action": {"type": "wait", "params": {"seconds": 3}},
        "goal": _PROSE_GOAL,
        "confidence": 0.4,
    }


#: Deterministically > the `say` word cap (20) — `DecisionModel._say_words`
#: RAISES on this one (not a soft truncate), so a decision built from it fails
#: real pydantic validation exactly as a real API response would, and
#: `TacticalBrain.decide` / `DirectorBrain.decide`'s real retry loop runs.
_OVERLONG_SAY = "word " * 25


def _reject_once_decision(text: str, state: GameState) -> dict[str, Any]:
    """First attempt: a `say` that pydantic REJECTS (>20 words) — a real
    `ValidationError` out of `DecisionModel.model_validate_json`, exactly like
    a live API response that broke the word-limit contract. Second attempt
    (the caller's real retry, detected the same way the model would notice —
    the appended "RETRY —" coaching in the dynamic context): a compliant
    decision, so the retry actually succeeds and `DecisionFailedError` is
    NOT what ends this call, matching the common case CONTRACTS §2 describes
    ("a response failing validation is retried once")."""
    if _is_retry(text):
        return _policy_decision(text, state)
    return {
        "thought": "This one is going to get rejected on purpose.",
        "say": _OVERLONG_SAY.strip(),
        "mood": "chill",
        "action": {"type": "wait", "params": {"seconds": 1}},
        "goal": "trip the word limit",
        "confidence": 0.5,
    }


_MODES = {
    "policy": _policy_decision,
    "misbehave": _misbehave_decision,
    "reject_once": _reject_once_decision,
}


@dataclass
class FakeBilledCall:
    """Drop-in for `brain.tactical.BilledCall` — see the module docstring.

    `mode` selects which of the three functions above builds the decision
    dict; `calls` is filled in as a side effect purely for tests that want to
    assert "the fake brain was actually reached N times" without scraping the
    replay log for it.
    """

    mode: str = "policy"
    on_cost: Any = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in _MODES:
            raise ValueError(f"unknown fake-brain mode {self.mode!r}; known: {sorted(_MODES)}")

    def run(
        self,
        model_id: str,
        prefix: str,
        content: list[dict[str, Any]] | str,
        max_tokens: int = 500,
    ) -> DecisionResult:
        text, state = parse_state_from_context(content)
        builder = _MODES[self.mode]
        payload = builder(text, state)
        raw = json.dumps(payload)
        # The REAL validation path — `DecisionModel.model_validate_json` is
        # exactly what `brain.tactical.BilledCall.run` calls on a real
        # response's text block. A ValidationError here propagates exactly as
        # it would from a live call, so `TacticalBrain.decide` /
        # `DirectorBrain.decide`'s retry loop is exercised for real, not
        # re-described.
        decision = DecisionModel.model_validate_json(raw)
        # Deterministic, tiny, and clearly synthetic — never charged for real
        # (the fake brain makes no network call), but shaped like real usage
        # so a caller that logs `input_tokens`/`output_tokens` sees plausible
        # numbers rather than a suspicious constant zero.
        input_tokens = max(1, len(text) // 4)
        output_tokens = max(1, len(raw) // 4)
        cost = 0.0
        if self.on_cost is not None:
            self.on_cost(cost)
        self.calls.append({"model_id": model_id, "mode": self.mode, "goal": decision.goal})
        return DecisionResult(
            decision=decision,
            model=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=cost,
        )


def install(
    tactical: Any, director: Any, mode: str = "policy", on_cost: Any = None
) -> tuple[FakeBilledCall, FakeBilledCall]:
    """Swap both brains' network seam for a fake one. Zero network either way.

    `tactical`/`director` are real `TacticalBrain`/`DirectorBrain` instances
    (real retry loop, real word-limit feedback, real vision-trigger checks —
    see the module docstring for why replacing only `_call_api` is the right
    seam rather than standing in for the whole class). `on_cost`, when given,
    is called with the (always 0.0) cost of every fake call, same shape as the
    real `BilledCall`'s callback — wire it to a real `BudgetGovernor.record` to
    exercise that path too. Returns the two fakes so a caller can inspect
    `.calls` afterwards.
    """
    tactical_fake = FakeBilledCall(mode=mode, on_cost=on_cost)
    director_fake = FakeBilledCall(mode=mode, on_cost=on_cost)
    tactical._call_api = tactical_fake
    director._call_api = director_fake
    return tactical_fake, director_fake
