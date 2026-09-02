"""Decision schema — EXACTLY CONTRACTS.md §2.

One model call produces the decision AND the commentary; this object is the
whole public feed. Word limits are contract text, enforced as validators: a
response failing validation is retried once by the caller, then the reflex
layer keeps control (CONTRACTS §2).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..logsetup import get_logger

log = get_logger("wasted.brain.schemas")

Mood = Literal["chill", "bored", "hyped", "scared", "smug"]
MOODS: tuple[str, ...] = ("chill", "bored", "hyped", "scared", "smug")

# 11 bridge task types (CONTRACTS §1) + v1 harness-side manual-control
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
    "set_waypoint",
    "stop",
)
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
    "set_waypoint",
    "stop",
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
}
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "follow_entity": ("handle",),
    "fight_ped": ("handle",),
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
    "fight_ped": ("handle",),
    "set_waypoint": ("x", "y"),
    "stop": (),
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
