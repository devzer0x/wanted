"""Decision schema — EXACTLY CONTRACTS.md §2.

One model call produces the decision AND the commentary; this object is the
whole public feed. Word limits are contract text, enforced as validators: a
response failing validation is retried once by the caller, then the reflex
layer keeps control (CONTRACTS §2).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..logsetup import get_logger
from .characters import absent_names_mentioned

log = get_logger("wasted.brain.schemas")

Mood = Literal["chill", "bored", "hyped", "scared", "smug"]
MOODS: tuple[str, ...] = ("chill", "bored", "hyped", "scared", "smug")

# 14 bridge task types (CONTRACTS §1) + v1 harness-side manual-control
# primitives (CONTRACTS §2). Adding a primitive bumps the contract version.
BRIDGE_TASKS: tuple[str, ...] = (
    "drive_to",
    "walk_to",
    "enter_nearest_vehicle",
    "exit_vehicle",
    "wander_drive",
    "flee_police",
    "combat_hated_targets_around",
    "seek_cover",
    "follow_entity",
    "fight_ped",
    "flee_ped",
    "set_waypoint",
    "stop",
    # CONTRACTS v1.13, the phone. Bridge tasks like the rest — they go out as
    # POST /task and they preempt the running task — but see PHONE_TASKS below
    # for the one way they are handled differently.
    "answer_call",
    "reject_call",
    # --- fix-opus-b (T6): bridge 1.7.0, CONTRACTS proposal v1.14 ------------
    # Three new verbs, each verified against the PINNED SHVDN
    # (bridge/lib/ScriptHookVDotNet3.dll, 3.7.0.189) before being written:
    #   shoot_at{handle, duration_s}   TASK_SHOOT_AT_ENTITY  0x08DA95E8298AE772
    #   drive_by{handle, duration_s}   TASK_DRIVE_BY         0x2F8AF0E82773A171
    #   enter_vehicle_seat{handle,seat} TASK_ENTER_VEHICLE   0xC20E50AA46D09CA8
    # `attack_ped` is deliberately NOT here: the ticket's
    # `attack_ped{handle}` = TASK_COMBAT_PED(player, target, 0, 16) is exactly
    # what `fight_ped`'s ranged arm already issues, so the only real difference
    # (which weapon he holds) ships as `fight_ped`'s new `weapon` param rather
    # than as a second task type with the same native, the same param and no
    # way for the model to tell them apart.
    "shoot_at",
    "drive_by",
    "enter_vehicle_seat",
    # --- bridge 1.8.0 (CONTRACTS §1 proposal): the flight step of `go_flying`.
    # `fly_to{x, y, z, speed_mps, arrive_radius_m}` — TASK_PLANE_MISSION or
    # TASK_HELI_MISSION (chosen bridge-side from the aircraft's model) with
    # VehicleMissionType.GoTo, through the pinned SHVDN wrappers
    # `TaskInvoker.StartPlaneMission` / `StartHeliMission` (Vector3 overloads).
    # It reuses drive_to's five frozen keys and adds NO new param, so the
    # 15-union-param ceiling below is untouched. `z` is the CRUISE ALTITUDE
    # above sea level (the engine's `flightHeight`), not the ground at x,y.
    # It is a MOVEMENT task like every drive: it competes for the wheel, so a
    # mission block or a survival rung takes the wheel off it exactly as they
    # take it off a `drive_to`.
    "fly_to",
)

#: CONTRACTS v1.13. The two bridge tasks that MOVE NOBODY: they inject one
#: phone control per frame and issue no engine ped-task at all.
#:
#: They are split out because `main._execute_action`'s movement gate refuses
#: every :data:`BRIDGE_TASKS` entry that does not carry the movement wheel's
#: current token, and a ringing phone must be answerable or refusable
#: whatever owns the wheel. Making the phone ask `roam` or `mission` for
#: permission to hang up on Simeon would mean the missions-off switch quietly
#: stops working exactly when a goal is locked — which is most of the time.
#: So these two are on the primitive-like side of that gate: no token, no
#: wheel, no preempt hook. They still go through every OTHER refusal in that
#: choke point (cutscene, dead/arrested, blocking screen), because a task
#: posted then reaches nobody regardless of what it does.
PHONE_TASKS: tuple[str, ...] = ("answer_call", "reject_call")

#: The bridge tasks that DO compete for the movement wheel — i.e. everything
#: that actually moves him. This, not :data:`BRIDGE_TASKS`, is what the
#: movement gate, `_reflex_act`, `_apply_decision` and
#: `RoamEngine.blocks_foreign_action` classify on.
MOVEMENT_TASKS: tuple[str, ...] = tuple(t for t in BRIDGE_TASKS if t not in PHONE_TASKS)
PRIMITIVES: tuple[str, ...] = (
    "look_around",
    "brake_tap",
    "swerve",
    "reverse_out",
    "press_prompt_key",
    "wait",
    "radio",
    "horn",
)
ACTION_TYPES: tuple[str, ...] = BRIDGE_TASKS + PRIMITIVES

ActionType = Literal[
    "drive_to",
    "walk_to",
    "enter_nearest_vehicle",
    "exit_vehicle",
    "wander_drive",
    "flee_police",
    "combat_hated_targets_around",
    "seek_cover",
    "follow_entity",
    "fight_ped",
    "flee_ped",
    "set_waypoint",
    "stop",
    "answer_call",
    "reject_call",
    # fix-opus-b (T6), bridge 1.7.0 — see BRIDGE_TASKS above.
    "shoot_at",
    "drive_by",
    "enter_vehicle_seat",
    "fly_to",
    "look_around",
    "brake_tap",
    "swerve",
    "reverse_out",
    "press_prompt_key",
    "wait",
    "radio",
    "horn",
]


def _word_count(text: str) -> int:
    return len(text.split())


#: Params the bridge hard-requires per action type (CONTRACTS §1). The action catalog tells the
#: model these are required, but nothing used to enforce it — so a `drive_to` with no coordinates
#: passed validation, reached the bridge, and came back
#:   400 invalid_params "drive_to requires numeric x, y, z".
#: That failure happens AFTER the decision is accepted, so the model never learned it was wrong
#: and simply did it again. Observed live: every single drive command the agent issued was rejected
#: this way while he sat motionless. Validating here turns a silent dead-end into a normal schema
#: error, which the retry path feeds back to the model so it can correct itself.
REQUIRED_NUMERIC_PARAMS: dict[str, tuple[str, ...]] = {
    "drive_to": ("x", "y", "z"),
    "walk_to": ("x", "y", "z"),
    "set_waypoint": ("x", "y"),
    # bridge 1.8.0: the bridge 400s a fly_to without a target and altitude,
    # exactly as it does a drive_to without coordinates.
    "fly_to": ("x", "y", "z"),
}
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "follow_entity": ("handle",),
    "fight_ped": ("handle",),
    "flee_ped": ("handle",),
    # fix-opus-b (T6): all three 1.7.0 verbs are target-explicit. A handle-less
    # `shoot_at` is not "shoot at nothing", it is a 400 from the bridge after
    # the decision was already accepted — the exact silent dead-end this table
    # exists to turn into a schema error the retry path can feed back.
    "shoot_at": ("handle",),
    "drive_by": ("handle",),
    "enter_vehicle_seat": ("handle",),
}


#: What the model is told this object is. Deliberately one line: see
#: :func:`_shape_wire_schema` for why the class docstring must not go on the wire.
_PARAMS_WIRE_DESCRIPTION = (
    "Parameters for the chosen action. Fill in the keys that action takes "
    "(the catalog lists them) and leave every other key null."
)


def _shape_wire_schema(schema: dict[str, Any]) -> None:
    """Two things the generated JSON schema must do that pydantic will not do alone.

    **1. Every property `required`.** This is the second half of the grammar-cost fix
    documented on :class:`ActionParamsModel`: *optional* properties are what make the
    API's grammar compilation blow up, because the decoder has to allow every subset
    of them in every order. Marking them all required makes the object one fixed
    sequence, and the model writes `null` for the ones that do not apply. Python stays
    lenient on purpose — the fields keep their defaults, so `ActionParamsModel()` and
    `{"type": "stop", "params": {}}` still validate. The wire schema being stricter
    than the Python model is the safe direction: the decoder cannot omit a key, and
    nothing else can be broken by one.

    **2. A one-line description instead of the class docstring.** pydantic uses
    `__doc__` as a model's JSON-schema description, so without this the 3.8 kB of
    grammar-compilation rationale below would be shipped to Haiku on every decision,
    sitting between the action catalog and the state the agent is supposed to be reading.
    Measured on the real endpoint (claude-haiku-4-5, 2026-09-02, prompt "x", no cache
    block): the schema cost 3727 input tokens with the docstring, 2435 with one line.
    Those tokens turn out to be *cacheable* — the same day, with the production shape
    (static prefix + `cache_control` + `output_format`), two identical calls reported
    `input=16, cache_write=14957` then `input=16, cache_read=14957`, i.e. 12597 prefix
    + ~2360 schema, all of it inside the cached block. So the docstring was costing
    cache-read cents, not dollars; it is off the wire because 3.8 kB of engineering
    notes in the middle of a decision prompt is noise, and the rationale belongs in
    the source where humans read it.

    pydantic applies a callable `json_schema_extra` wherever the model appears,
    including inside `$defs` and through `TypeAdapter`, which is the path
    `messages.parse` actually takes (anthropic 1.0.0 messages.py:1055).
    """
    schema["required"] = list(schema.get("properties", {}))
    schema["description"] = _PARAMS_WIRE_DESCRIPTION


class ActionParamsModel(BaseModel):
    """Every param name the action catalog documents, flat and all-optional.

    WHY THIS EXISTS AS A MODEL AND NOT ``dict[str, Any]``. Both tiers send this
    schema as a *constrained-decoding grammar* (``messages.parse(...,
    output_format=DecisionModel)`` → ``output_config.format``). The SDK's
    transform (anthropic 1.0.0, ``anthropic/lib/_parse/_transform.py``) rewrites
    any object node into ``{"type": "object", "properties": {...},
    "additionalProperties": false}`` — and an untyped dict has no properties, so
    it became ``{"properties": {}, "additionalProperties": false}``: a grammar
    whose ONLY legal value is ``{}``. The model could not emit a coordinate even
    when it wanted to. Measured on the first live session: 174 decisions, every
    one with ``params: {}``, so every ``drive_to``/``walk_to``/``follow_entity``/
    ``set_waypoint`` was dead on arrival and the two parameterless actions
    (``enter_nearest_vehicle``, ``exit_vehicle``) took 110 of the 174 — the
    observed "gets in a car, gets out, gets in another car" loop. No amount of
    prompt text can argue with a decoding grammar; the keys have to be in the
    schema.

    Flat rather than a per-action union: a 19-variant union is a much larger
    grammar on every call for the same expressive power, and the required-param
    rules already live in :data:`REQUIRED_NUMERIC_PARAMS` / :data:`REQUIRED_PARAMS`
    where the error text can be fed back to the model. Unset keys never reach the
    bridge — :meth:`ActionModel.wire_params` drops them.

    WHY `style`, `run` and `direction` ARE NOT `| None` LIKE THE REST. The API caps
    how many nullable (union-typed) params one schema may have, and the cap is
    lower than the seventeen names the catalog uses. Measured against the real
    endpoint (claude-haiku-4-5, 2026-09-02, one synthetic schema per size, no game
    data involved):

      * 17 union params → `400 invalid_request_error: Schemas contains too many
        parameters with union types (17 parameters with type arrays or anyOf).
        This causes exponential grammar compilation.`
      * 16 union params → accepted, then the request hangs and dies at 60 s.
        Twice, reproducibly, where 15 answers in ~1 s.
      * ≤15 union params → accepted, ~1.0-1.3 s.

    So the working ceiling is 15, not the documented-nowhere 16, and two of the
    seventeen keys had to stop being nullable. These three are the only ones where
    "the model said nothing" and "the model said the default" are the same thing:
    `style="normal"` is the contract's baseline driving style, `run=false` is the
    catalog's own `walk_to` default, and `direction="left"` is already what
    primitives.py falls back to. Nothing in :data:`REQUIRED_NUMERIC_PARAMS` or
    :data:`REQUIRED_PARAMS` may be converted this way: a defaulted `x` would turn a
    coordinate the model never chose into a drive to the origin, which is the class
    of bug this whole model exists to kill. Because they always carry a value,
    :data:`ACTION_PARAM_KEYS` is what keeps them out of the eighteen actions they
    mean nothing to.

    WHY EVERY FIELD IS `required` IN THE WIRE SCHEMA — see
    :func:`_every_field_required`. Same endpoint, same day, same method: grammar
    compilation is exponential in the number of OPTIONAL properties, and the wall
    clock says so plainly (first-time compile of one object of n optional params):

        n=8 → 2.5 s · n=10 → 4.5 s · n=12 → 12.0 s · n=13 → 14.9 s
        n=14 → 57.4 s · n=15, 16, 17 → the server drops the connection at 60 s

    All seventeen marked `required` compiles in **3.1 s cold, 1.3 s warm** — the
    compiled grammar is cached server-side, but a schema that never finishes
    compiling never gets cached, and an 8-25 s decision cadence cannot absorb a
    60 s stall on the first call after any eviction. The price is the `null`s the
    model now writes for the keys the action does not use, measured on the same
    prompt through both schemas: **57 → 142 output tokens**, i.e. +85 tokens, which
    at Haiku 4.5 output pricing is +$0.00043/decision (+$0.19/h at the 450 calls/h
    tactical ceiling). That is the whole cost of the agent being able to name a place he
    wants to drive to.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra=_shape_wire_schema)

    x: float | None = Field(
        default=None, description="drive_to/walk_to/set_waypoint: world X, m."
    )
    y: float | None = Field(
        default=None, description="drive_to/walk_to/set_waypoint: world Y, m."
    )
    z: float | None = Field(
        default=None, description="drive_to/walk_to: world Z, m. set_waypoint has no z."
    )
    speed_mps: float | None = Field(
        default=None,
        description="drive_to: target speed, m/s.",
    )
    style: Literal["normal", "rushed", "ignore_lights", "avoid_traffic"] = Field(
        default="normal", description="drive_to/wander_drive: driving style."
    )
    arrive_radius_m: float | None = Field(
        default=None,
        description="drive_to: arrival radius, m (8 default).",
    )
    run: bool = Field(default=False, description="walk_to: run instead of walking.")
    prefer: Literal["nicer", "any"] | None = Field(
        default=None,
        description="enter_nearest_vehicle: 'nicer' upgrades class, 'any' is an emergency.",
    )
    search_radius_m: float | None = Field(
        default=None,
        description="enter_nearest_vehicle: search radius, m (30 normal).",
    )
    radius_m: float | None = Field(
        default=None, description="combat_hated_targets_around: engagement radius, m."
    )
    duration_s: float | None = Field(
        default=None, description="seek_cover: seconds to hold cover."
    )
    handle: int | None = Field(
        default=None,
        description="follow_entity/fight_ped: handle from nearby.*/threat.* in the CURRENT snapshot.",
    )
    in_vehicle: bool | None = Field(
        default=None, description="follow_entity: true to tail in a vehicle, false on foot."
    )
    seconds: float | None = Field(
        default=None, description="wait: seconds to sit still."
    )
    station: str | None = Field(
        default=None, description="radio: the game's own station name, or 'off'."
    )
    ms: int | None = Field(
        default=None,
        description="reverse_out and horn: duration in milliseconds.",
    )
    #: The catalog states the only two legal values, so the grammar states them too:
    #: primitives.py maps anything that is not "left" to a right-hand swerve, which
    #: would turn a typo into a silent wrong-direction flinch.
    direction: Literal["left", "right"] = Field(
        default="left", description="swerve: which way to flinch."
    )
    # --- fix-opus-b (T6), bridge 1.7.0 ---------------------------------------
    #
    # BOTH ARE NON-NULLABLE ON PURPOSE, and it is not a style choice: the API's
    # working ceiling is 15 union-typed params (see this class's docstring —
    # 16 hangs and dies at 60 s, 17 is a hard 400) and the schema already
    # carries 14. Spending the last slot here would leave none for the movement
    # tickets landing in the same round, so these two take the `style`/`run`/
    # `direction` treatment instead. They qualify on that rule's own test —
    # "the model said nothing" and "the model said the default" are the same
    # thing:
    #
    #   `weapon="auto"`  IS the pre-1.7.0 `fight_ped` behaviour, byte for byte
    #                    (melee vs ranged chosen from the TARGET's weapon
    #                    class), so an omitted value changes nothing.
    #   `seat=2`         `enter_vehicle_seat` exists ONLY to ride as a
    #                    passenger; the driver's seat is `enter_nearest_vehicle`
    #                    and is not reachable from this enum at all. Rear-right
    #                    is where the game's own cab AI puts the player.
    #
    # `ACTION_PARAM_KEYS` keeps both out of the actions they mean nothing to,
    # exactly as it does for the other three.
    weapon: Literal["auto", "unarmed", "armed"] = Field(
        default="auto",
        description=(
            "fight_ped: 'auto' picks melee or gun from the target's weapon; "
            "'unarmed' forces fists; 'armed' forces the loadout gun."
        ),
    )
    seat: Literal[0, 1, 2] = Field(
        default=2,
        description=(
            "enter_vehicle_seat: passenger seat — 0 front, 1 rear-left, "
            "2 rear-right. The driver's seat is enter_nearest_vehicle."
        ),
    )


class ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ActionType = Field(description="One bridge task type or manual-control primitive.")
    params: ActionParamsModel = Field(
        default_factory=ActionParamsModel,
        description="Parameters for the action, per the action catalog.",
    )

    @model_validator(mode="after")
    def _required_params(self) -> ActionModel:
        for name in REQUIRED_NUMERIC_PARAMS.get(self.type, ()):
            v = getattr(self.params, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                # ValueError on purpose (ruff TRY004 wants TypeError): pydantic turns ValueError
                # into a ValidationError the retry path feeds back to the model; a TypeError
                # would escape the validator and crash the decision loop instead.
                raise ValueError(  # noqa: TRY004
                    f"action {self.type!r} requires numeric {name!r} in params; "
                    f"got {v!r}. The bridge rejects this action without it."
                )
        for name in REQUIRED_PARAMS.get(self.type, ()):
            if getattr(self.params, name) is None:
                raise ValueError(
                    f"action {self.type!r} requires {name!r} in params; it was missing."
                )
        return self

    def wire_params(self) -> dict[str, Any]:
        """The params as they actually go out: the keys this action takes, that were set.

        Every unset key is None, and neither the bridge (CONTRACTS §1) nor the
        `decisions.action` jsonb (CONTRACTS §2 shape `{"type":..,"params":{..}}`)
        wants sixteen nulls next to the one key that mattered. The allowlist on
        top of that is what stops `style`/`run`/`direction` — which cannot be
        nullable, see :class:`ActionParamsModel` — from riding along on the
        eighteen actions they mean nothing to, and stops a stray `speed_mps` on a
        `walk_to` from earning a 400 invalid_params. This is what callers pass to
        the bridge and store; never `params` itself.
        """
        allowed = ACTION_PARAM_KEYS[self.type]
        return {
            k: v for k, v in self.params.model_dump(exclude_none=True).items() if k in allowed
        }


#: The param keys each action actually takes — CONTRACTS §1's task table plus §2's
#: primitive list, nothing invented. :meth:`ActionModel.wire_params` filters through
#: this, so the bridge and the `decisions.action` jsonb only ever see keys that belong
#: to the action. Their union is exactly the field set of :class:`ActionParamsModel`
#: and their keys are exactly :data:`ACTION_TYPES`; both are asserted in the tests.
ACTION_PARAM_KEYS: dict[str, tuple[str, ...]] = {
    "drive_to": ("x", "y", "z", "speed_mps", "style", "arrive_radius_m"),
    "walk_to": ("x", "y", "z", "run"),
    "enter_nearest_vehicle": ("prefer", "search_radius_m"),
    "exit_vehicle": (),
    "wander_drive": ("style",),
    "flee_police": (),
    "combat_hated_targets_around": ("radius_m",),
    "seek_cover": ("duration_s",),
    # CONTRACTS v1.9 gives follow_entity `speed_mps` — but deliberately NOT `style`, even though
    # the bridge accepts it. `style` is one of the three non-nullable keys (v1.4: the wire forces a
    # value for every key), so listing it here would make EVERY follow carry `style: "normal"` —
    # stop at red lights, 54 km/h — which is precisely the bridge-side bug v1.9 exists to kill.
    # Omitted, the bridge applies its own `ignore_lights` default and the tail can keep up. Speed
    # stays settable because it is nullable, so it is absent unless someone means it.
    "follow_entity": ("handle", "in_vehicle", "speed_mps"),
    # v1.11: fight ONE named ped, no relationship setup needed (unlike
    # combat_hated_targets_around). Only `handle` — see CONTRACTS §1.
    # v1.11 gave it `handle`; bridge 1.7.0 adds `weapon`, which is what makes
    # `pick_a_fight` a FIST fight on a the agent who is carrying a pistol.
    "fight_ped": ("handle", "weapon"),
    "flee_ped": ("handle",),
    "set_waypoint": ("x", "y"),
    "stop": (),
    # CONTRACTS v1.13: no params at all. Which call is ringing is not something
    # the caller gets to name — no native exposes the caller's identity — so
    # there is nothing to pass and no key to invent.
    "answer_call": (),
    "reject_call": (),
    # fix-opus-b (T6), bridge 1.7.0. `duration_s` is REUSED from seek_cover
    # rather than a new key: it is already nullable, it means the same thing
    # (how long the engine task runs), and a new nullable key would have spent
    # the last of the 15-union-param budget.
    "shoot_at": ("handle", "duration_s"),
    "drive_by": ("handle", "duration_s"),
    "enter_vehicle_seat": ("handle", "seat"),
    # bridge 1.8.0: drive_to's keys minus `style` (an aircraft has no traffic
    # lights to ignore). No new key — see BRIDGE_TASKS.
    "fly_to": ("x", "y", "z", "speed_mps", "arrive_radius_m"),
    "look_around": (),
    "brake_tap": (),
    "swerve": ("direction",),
    "reverse_out": ("ms",),
    "press_prompt_key": (),
    "wait": ("seconds",),
    "radio": ("station",),
    "horn": ("ms",),
}


class DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thought: str = Field(
        description="<=40 words. The agent's private-ish reasoning, shown on site as "
        "'what he was thinking'. Over the limit is soft-truncated to 40 words plus "
        "'…', never rejected — see `_thought_words`."
    )
    say: str = Field(
        description="<=20 words. The agent's out-loud line in his voice. This is the commentary."
    )
    mood: Mood
    action: ActionModel
    goal: str = Field(
        description="Current goal in <=12 words, unchanged unless the director changed it."
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("thought")
    @classmethod
    def _thought_words(cls, v: str) -> str:
        """Soft-truncate rather than reject.

        Observed live: a valid, useful decision (real coordinates, a correct
        action) was thrown away in its entirety because the narration
        attached to it ran long — "thought is 59 words; contract max is 40"
        discarded the whole decision, including the ACTION, on a tick where a
        mission follower was mid-recovery and needed exactly that action to
        post. Narration length is cosmetic; the action is not. Clip to the
        first 40 words and mark the cut with "…" instead of raising, so a
        decision is never discarded for how long its thought was.
        """
        words = v.split()
        if len(words) > 40:
            log.warning(
                "thought exceeded the 40-word contract limit; soft-truncating "
                "rather than discarding the decision",
                extra={"kv": {"word_count": len(words)}},
            )
            return " ".join(words[:40]) + "…"
        return v

    @field_validator("say")
    @classmethod
    def _say_words(cls, v: str) -> str:
        if _word_count(v) > 20:
            raise ValueError(f"say is {_word_count(v)} words; contract max is 20")
        return v

    @field_validator("goal")
    @classmethod
    def _goal_words(cls, v: str) -> str:
        if _word_count(v) > 12:
            raise ValueError(f"goal is {_word_count(v)} words; contract max is 12")
        return v


# --- T4: the output validator ---------------------------------------------
#
# findings.md R4: nothing checked whether the model's OWN output — a name it
# used, a mission title, a banned phrase, a repeated line, a roam goal id —
# was actually true of the world it was just shown. Pydantic's field
# validators above can only see the decision object; these need a snapshot of
# the world/roam/commentary state alongside it, so they are plain functions
# the caller (brain.tactical / brain.director) runs AFTER a decision parses,
# not `@field_validator`s. `absent_names_mentioned` is reused, not
# reimplemented — this module adds the mission-name, banned-phrase, dedupe and
# roam-goal-id rules that sit beside it.


def normalize_line(text: str) -> str:
    """Case/punctuation-insensitive form, so "Fine." and "fine" count as one.

    The single implementation `commentary.py`'s dedupe gate and this module's
    dedupe rule both use — moved here so schemas.py (imported BY commentary.py
    already) never has to import back the other way.
    """
    kept = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text.lower())
    return " ".join(kept.split())


def jaccard_similarity(a: str, b: str) -> float:
    """Normalized-token-overlap similarity: |shared words| / |all words|.

    Cheap, no model call. Two empty (post-normalization) strings are not
    similar to each other; there is nothing shared to measure.
    """
    wa = set(normalize_line(a).split())
    wb = set(normalize_line(b).split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


#: Jaccard overlap at/above which a `say` line counts as "basically the last
#: line again" for the VALIDATOR's dedupe rule (T4). Deliberately tighter than
#: `commentary.Commentary.gate_say`'s own 0.8 backstop: this one gets a
#: regenerate before the line is ever shown, so it can afford to be stricter.
DEDUPE_OVERLAP_THRESHOLD = 0.6
#: How many of the caller's most-recently-shown lines the dedupe rule checks.
DEDUPE_RECENT_WINDOW = 5


@dataclass(frozen=True)
class DecisionValidationContext:
    """Everything :func:`validate_decision_content` needs, gathered by the
    caller (main._think) from world/roam/commentary state. Deliberately plain
    data — no `GameState` import here, so this module stays state-shape
    agnostic like the rest of it.
    """

    #: Story character names legitimately on screen right now (brain.characters.present_names).
    present_names: frozenset[str] = frozenset()
    #: brain.characters.has_unidentified_friendly(state) — an unnamed friendly
    #: ped nearby means the names check cannot prove anybody is absent, so it
    #: stays silent rather than risk dropping a true line.
    names_check_suspended: bool = False
    #: The identified active mission's own name, or None (mission not active
    #: / not identified) — the mission-name rule is a no-op without it.
    mission_name: str | None = None
    #: The full catalogued mission-name vocabulary MINUS the current mission's
    #: own name and minus anything that collides with a character name (so
    #: "Chop" the dog is never read as "Chop" the mission).
    known_mission_names: frozenset[str] = frozenset()
    #: The last few lines actually shown (oldest first), for the dedupe rule.
    recent_lines: tuple[str, ...] = ()
    #: Hard-banned phrases, loaded from commentary_style.md (brain.prompts.banned_phrases()).
    banned_phrases: tuple[str, ...] = ()
    #: The roam ids actually on offer THIS tick, or empty when no menu is
    #: showing (a goal is locked, or free roam is not in play at all).
    roam_offered_ids: tuple[str, ...] = ()
    #: `RoamEngine.model_choice`, bound — reused, not reimplemented, per T4.
    #: None when `roam_offered_ids` is empty (nothing to validate against).
    roam_model_choice: Callable[[str | None], str | None] | None = None


@dataclass(frozen=True)
class DecisionViolation:
    """What :func:`validate_decision_content` found wrong, if anything.

    The two halves are independent on purpose: a bad `say` and a bad `goal`
    are different problems with different fixes (drop the line vs. let the
    roam engine's own `available[0]` fallback take over), so the caller needs
    to know which one(s) fired, not just that something did.
    """

    say_reason: str | None = None
    goal_reason: str | None = None

    def __bool__(self) -> bool:
        return self.say_reason is not None or self.goal_reason is not None

    def describe(self) -> str:
        parts = [r for r in (self.say_reason, self.goal_reason) if r]
        return "; ".join(parts)


def _mismatched_mission_names(
    text: str, mission_name: str, known_mission_names: Iterable[str]
) -> list[str]:
    """Other catalogued mission names spoken while `mission_name` is the one
    actually identified. Word-boundary, case-insensitive; the caller has
    already excluded `mission_name` itself (however it's cased) from
    `known_mission_names`, so nothing here has to special-case it."""
    lowered_current = mission_name.strip().lower()
    offenders: list[str] = []
    for name in known_mission_names:
        n = name.strip()
        if not n or n.lower() == lowered_current:
            continue
        if re.search(rf"\b{re.escape(n)}\b", text, flags=re.IGNORECASE):
            offenders.append(n)
    return sorted(set(offenders))


def _say_violation(decision: DecisionModel, ctx: DecisionValidationContext) -> str | None:
    haystack = f"{decision.say}\n{decision.thought}".lower()
    for phrase in ctx.banned_phrases:
        if phrase and phrase in haystack:
            return f"banned phrase {phrase!r}"
    if not ctx.names_check_suspended:
        # `present_names` empty is not a reason to skip this: nobody being on
        # screen yet still means no CHECKED_NAMES character may be spoken.
        absent = absent_names_mentioned(
            decision.say, ctx.present_names
        ) or absent_names_mentioned(decision.thought, ctx.present_names)
        if absent:
            return f"names not present right now: {', '.join(absent)}"
    if ctx.mission_name:
        mismatched = _mismatched_mission_names(
            decision.say, ctx.mission_name, ctx.known_mission_names
        ) or _mismatched_mission_names(decision.thought, ctx.mission_name, ctx.known_mission_names)
        if mismatched:
            return f"named a different mission ({', '.join(mismatched)}); actual: {ctx.mission_name}"
    for prior in ctx.recent_lines[-DEDUPE_RECENT_WINDOW:]:
        overlap = jaccard_similarity(decision.say, prior)
        if overlap >= DEDUPE_OVERLAP_THRESHOLD:
            return f"say overlaps a recent line ({overlap:.2f} >= {DEDUPE_OVERLAP_THRESHOLD}): {prior!r}"
    return None


def _goal_violation(decision: DecisionModel, ctx: DecisionValidationContext) -> str | None:
    if not ctx.roam_offered_ids or ctx.roam_model_choice is None:
        return None
    if ctx.roam_model_choice(decision.goal) is not None:
        return None
    return (
        f"goal {decision.goal!r} names no offered roam id "
        f"({', '.join(ctx.roam_offered_ids)})"
    )


def validate_decision_content(
    decision: DecisionModel, ctx: DecisionValidationContext
) -> DecisionViolation:
    """T4: names ⊂ STATE, mission name == identified mission, banned phrases,
    dedupe >= 0.6, and (when a roam menu is on offer) `goal` ∈ available.

    Pure function — no I/O, no mutation — so the caller (brain.tactical /
    brain.director) owns the one-regenerate-then-drop policy.
    """
    return DecisionViolation(
        say_reason=_say_violation(decision, ctx),
        goal_reason=_goal_violation(decision, ctx),
    )


def sanitize_decision(decision: DecisionModel, violation: DecisionViolation) -> DecisionModel:
    """Applied only when the single regenerate ALSO still violates: drop the
    offending line, never the action.

    `goal_reason` needs no sanitizing here — an unmatched roam id is the roam
    engine's own `available[0]` fallback to make (main._begin_roam_goal,
    logged as `goal_fallback`), and mutating `goal` here would just be a
    second, competing fallback for the same problem.
    """
    if violation.say_reason is None:
        return decision
    return decision.model_copy(update={"say": ""})
