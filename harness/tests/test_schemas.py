"""DecisionModel enforces CONTRACTS §2 exactly (pure-function tests, constructed inputs)."""

import pytest
from pydantic import ValidationError

from wasted_harness.brain.schemas import (
    ACTION_PARAM_KEYS,
    ACTION_TYPES,
    BRIDGE_TASKS,
    MOODS,
    PRIMITIVES,
    ActionModel,
    ActionParamsModel,
    DecisionModel,
)


def _valid(**overrides) -> dict:
    base = {
        "thought": "Two stars, tank's fine, freeway's right there.",
        "say": "Relax. I've done this twice.",
        "mood": "hyped",
        "action": {"type": "flee_police", "params": {}},
        "goal": "lose the cops by the port",
        "confidence": 0.7,
    }
    base.update(overrides)
    return base


def test_mood_enum_exact() -> None:
    assert MOODS == ("chill", "bored", "hyped", "scared", "smug")
    for mood in MOODS:
        DecisionModel.model_validate(_valid(mood=mood))
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(_valid(mood="angry"))


def test_action_catalog_exact() -> None:
    # v1.11 adds fight_ped; v1.13 adds answer_call + reject_call; bridge 1.7.0
    # (fix-opus-b, T6) adds shoot_at + drive_by + enter_vehicle_seat; bridge
    # 1.7.0 (fix-opus-a, T1) adds flee_ped; bridge 1.8.0 adds fly_to (the
    # flight step of `go_flying`).
    assert len(BRIDGE_TASKS) == 19
    assert PRIMITIVES == (
        "look_around",
        "brake_tap",
        "swerve",
        "reverse_out",
        "press_prompt_key",
        "wait",
        "radio",
        "horn",
    )
    # Every catalog action must be constructible. Some carry hard-required params: the bridge
    # answers 400 invalid_params without them, and ActionModel enforces that up front so the
    # model gets a correctable schema error instead of a silently discarded action. (Observed
    # live: every drive_to the agent issued had empty params, was rejected by the bridge, and he
    # spent minutes getting in and out of cars instead of driving anywhere.)
    minimal_params: dict[str, dict[str, object]] = {
        "drive_to": {"x": 1.0, "y": 2.0, "z": 3.0},
        "walk_to": {"x": 1.0, "y": 2.0, "z": 3.0},
        "set_waypoint": {"x": 1.0, "y": 2.0},
        "follow_entity": {"handle": 1},
        "fight_ped": {"handle": 1},
        "flee_ped": {"handle": 1},
        # bridge 1.7.0 (fix-opus-b, T6): all three are target-explicit.
        "shoot_at": {"handle": 1},
        "drive_by": {"handle": 1},
        "enter_vehicle_seat": {"handle": 1},
        # bridge 1.8.0: a target and a cruise altitude, like drive_to.
        "fly_to": {"x": 1.0, "y": 2.0, "z": 3.0},
    }
    for t in ACTION_TYPES:
        ActionModel.model_validate({"type": t, "params": minimal_params.get(t, {})})

    # ...and the required params really are required.
    for t, params in minimal_params.items():
        for missing in params:
            bad = {k: v for k, v in params.items() if k != missing}
            with pytest.raises(ValidationError):
                ActionModel.model_validate({"type": t, "params": bad})
    with pytest.raises(ValidationError):
        ActionModel.model_validate({"type": "teleport", "params": {}})
    with pytest.raises(ValidationError):
        ActionModel.model_validate({"type": "god_mode", "params": {}})


def test_fight_ped_round_trips_with_a_handle_and_a_weapon() -> None:
    """CONTRACTS v1.11 §1 `fight_ped {handle}` + bridge 1.7.0's `weapon`.

    `weapon` is non-nullable with a default (the 15-union-param ceiling — see
    ActionParamsModel), so it ALWAYS rides along on fight_ped and never on
    anything else. `"auto"` is byte-for-byte the pre-1.7.0 behaviour, so a
    caller that never heard of the field gets exactly what it used to.
    """
    action = ActionModel.model_validate({"type": "fight_ped", "params": {"handle": 9012}})
    assert action.wire_params() == {"handle": 9012, "weapon": "auto"}
    fists = ActionModel.model_validate(
        {"type": "fight_ped", "params": {"handle": 9012, "weapon": "unarmed"}}
    )
    assert fists.wire_params() == {"handle": 9012, "weapon": "unarmed"}
    with pytest.raises(ValidationError):
        ActionModel.model_validate({"type": "fight_ped", "params": {}})
    with pytest.raises(ValidationError):
        ActionModel.model_validate(
            {"type": "fight_ped", "params": {"handle": 9012, "weapon": "rocket"}}
        )
    # A param fight_ped does not take never rides along.
    stray = ActionModel.model_validate(
        {"type": "fight_ped", "params": {"handle": 9012, "radius_m": 25.0}}
    )
    assert stray.wire_params() == {"handle": 9012, "weapon": "auto"}


def test_the_new_1_7_0_verbs_carry_only_their_own_keys() -> None:
    """T6: three new bridge tasks, each target-explicit, no key bleed."""
    shoot = ActionModel.model_validate(
        {"type": "shoot_at", "params": {"handle": 11, "duration_s": 6.0, "radius_m": 30.0}}
    )
    assert shoot.wire_params() == {"handle": 11, "duration_s": 6.0}
    drive_by = ActionModel.model_validate({"type": "drive_by", "params": {"handle": 12}})
    assert drive_by.wire_params() == {"handle": 12}
    # `seat` is non-nullable with a passenger default, so it is always present
    # and the driver's seat is not expressible from this action at all.
    seat = ActionModel.model_validate({"type": "enter_vehicle_seat", "params": {"handle": 13}})
    assert seat.wire_params() == {"handle": 13, "seat": 2}
    with pytest.raises(ValidationError):
        ActionModel.model_validate({"type": "enter_vehicle_seat", "params": {"handle": 13, "seat": -1}})
    for verb in ("shoot_at", "drive_by", "enter_vehicle_seat"):
        with pytest.raises(ValidationError):
            ActionModel.model_validate({"type": verb, "params": {}})


def test_the_wire_schema_can_express_coordinates() -> None:
    """The regression for the bug that made every movement action impossible.

    `params` used to be `dict[str, Any]`. Both tiers ship the decision schema as a
    constrained-decoding grammar, and the SDK's transform rewrites an object with no
    declared properties into `{"properties": {}, "additionalProperties": false}` —
    a grammar whose only legal value is `{}`. So the model could not emit a
    coordinate even when it wanted to: 174 real decisions, 174 empty param objects,
    every drive_to/walk_to/follow_entity/set_waypoint undeliverable, and 110 of the
    174 spent on the two actions that need no params. This test asserts against the
    ACTUAL transform the SDK applies, not against our own model_json_schema, because
    the whole failure lived in the difference between the two.
    """
    from anthropic.lib._parse._transform import transform_schema

    s = transform_schema(DecisionModel.model_json_schema())
    params_node = s["$defs"]["ActionModel"]["properties"]["params"]
    # It must resolve to a real object with named properties, not an empty-object grammar.
    if "$ref" in params_node:
        params_node = s["$defs"][params_node["$ref"].rsplit("/", 1)[-1]]
    props = params_node["properties"]
    assert props, "params has no properties: the grammar can only ever produce {}"
    assert {"x", "y", "z", "style", "prefer", "handle", "seconds", "station", "ms"} <= set(props)
    assert set(props) == set(ActionParamsModel.model_fields)


def test_the_wire_schema_stays_inside_the_measured_api_limits() -> None:
    """Both limits are undocumented and were found by probing the real endpoint.

    - > 16 params with a union type (`anyOf` / type array) → 400 "Schemas contains
      too many parameters with union types".
    - Grammar compilation is exponential in the number of OPTIONAL properties: 12
      optional took 12 s to compile, 14 took 57 s, 15+ never returned (the server
      dropped the connection at 60 s). Marking every property `required` makes it
      linear — measured 3.1 s cold / 1.3 s warm for all seventeen.

    Adding one more nullable param, or letting one become optional again, silently
    reintroduces a minute-long stall (or a 400) on a live decision. Hence this test.
    """
    from anthropic.lib._parse._transform import transform_schema

    s = transform_schema(DecisionModel.model_json_schema())

    def walk(node: object) -> list[dict]:
        out: list[dict] = []
        if isinstance(node, dict):
            if "anyOf" in node or isinstance(node.get("type"), list):
                out.append(node)
            for key, value in node.items():
                if key != "anyOf":
                    out.extend(walk(value))
        elif isinstance(node, list):
            for value in node:
                out.extend(walk(value))
        return out

    assert len(walk(s)) <= 15, "more than 15 union-typed params; the API 400s at 17"
    params = s["$defs"]["ActionParamsModel"]
    assert set(params["required"]) == set(params["properties"]), (
        "an optional property in the params object: grammar compilation goes exponential"
    )


def test_every_contract_param_name_is_expressible() -> None:
    """Guard against `extra='forbid'` killing a decision over a forgotten key.

    The catalog is what the model is told to use. Any key named there that is not a
    field on ActionParamsModel cannot be emitted at all (the grammar has no slot for
    it) and would be rejected if it somehow were — a whole decision lost to a typo in
    a prompt file. The catalog's JSON examples are the authority here.
    """
    import json
    import re
    from importlib import resources

    catalog = (
        resources.files("wasted_harness.brain.prompts") / "action_catalog.md"
    ).read_text(encoding="utf-8")
    documented: set[str] = set()
    for block in re.findall(r"```json\n(.*?)```", catalog, re.DOTALL):
        documented.update(json.loads(block))
    assert documented, "no JSON examples found in the action catalog"
    assert documented <= set(ActionParamsModel.model_fields), (
        f"catalog names params the schema cannot express: "
        f"{sorted(documented - set(ActionParamsModel.model_fields))}"
    )
    # And nothing in the schema is undocumented, or the model is never told about it.
    assert set(ActionParamsModel.model_fields) == documented


def test_wire_params_sends_only_what_the_action_takes() -> None:
    """`wire_params` is what reaches POST /task and the decisions.action jsonb.

    Two jobs: drop the sixteen keys the action does not use (CONTRACTS §2 shape,
    and the bridge answers 400 invalid_params for junk), and keep style/run/direction
    — which cannot be nullable, see ActionParamsModel — out of the actions they mean
    nothing to.
    """
    assert set(ACTION_PARAM_KEYS) == set(ACTION_TYPES)
    union: set[str] = set()
    for keys in ACTION_PARAM_KEYS.values():
        union |= set(keys)
    assert union == set(ActionParamsModel.model_fields)

    drive = ActionModel.model_validate(
        {
            "type": "drive_to",
            "params": {"x": 1.0, "y": 2.0, "z": 3.0, "speed_mps": 16.0, "style": "rushed"},
        }
    )
    assert drive.wire_params() == {
        "x": 1.0,
        "y": 2.0,
        "z": 3.0,
        "speed_mps": 16.0,
        "style": "rushed",
    }
    # style/run/direction always carry a value; they must not ride along elsewhere.
    assert ActionModel.model_validate({"type": "exit_vehicle"}).wire_params() == {}
    assert ActionModel.model_validate({"type": "horn", "params": {"ms": 150}}).wire_params() == {
        "ms": 150
    }
    assert ActionModel.model_validate({"type": "swerve"}).wire_params() == {"direction": "left"}
    # A param the model set on the wrong action never reaches the bridge.
    stray = ActionModel.model_validate({"type": "wander_drive", "params": {"seconds": 9.0}})
    assert stray.wire_params() == {"style": "normal"}


def test_word_limits_enforced() -> None:
    # `say`/`goal` are still hard limits: a rejected decision means the agent does
    # nothing at all this tick, which the caller's retry logic is built around.
    with pytest.raises(ValidationError, match="20"):
        DecisionModel.model_validate(_valid(say=" ".join(["word"] * 21)))
    with pytest.raises(ValidationError, match="12"):
        DecisionModel.model_validate(_valid(goal=" ".join(["word"] * 13)))
    # At the limits is fine.
    DecisionModel.model_validate(
        _valid(
            thought=" ".join(["w"] * 40),
            say=" ".join(["w"] * 20),
            goal=" ".join(["w"] * 12),
        )
    )


def test_thought_over_the_limit_is_soft_truncated_not_rejected() -> None:
    """A valid decision (real coordinates, a correct action) must never be
    discarded in its entirety over narration length — see `_thought_words`."""
    fifty_nine_words = " ".join(f"w{i}" for i in range(59))
    decision = DecisionModel.model_validate(_valid(thought=fifty_nine_words))
    assert decision.thought.endswith("…")
    kept = decision.thought[:-1].split()
    assert len(kept) == 40
    assert kept == fifty_nine_words.split()[:40]

    # At/under the limit is untouched.
    at_limit = " ".join(["w"] * 40)
    assert DecisionModel.model_validate(_valid(thought=at_limit)).thought == at_limit


def test_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(_valid(confidence=1.2))
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(_valid(confidence=-0.1))
    DecisionModel.model_validate(_valid(confidence=0.0))
    DecisionModel.model_validate(_valid(confidence=1.0))


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        DecisionModel.model_validate(_valid(cheat_mode=True))


def test_retry_correction_coaches_the_error_it_actually_got() -> None:
    """The other half of a validation error: what the model is told on the one retry.

    Lives here rather than in a tactical test file because it is entirely about the
    ValidationErrors this module raises. The retry note used to end with the word-limit
    paragraph no matter what broke, so a `drive_to` rejected for a missing coordinate
    was answered with a lecture about thought being <= 40 words — the wrong correction,
    on the last attempt before the reflex layer takes the tick.
    """
    from wasted_harness.brain.tactical import WORD_LIMIT_MARKER, _retry_correction

    with pytest.raises(ValidationError) as missing_coord:
        DecisionModel.model_validate(_valid(action={"type": "drive_to", "params": {}}))
    note = _retry_correction(missing_coord.value)
    assert "ValidationError" in note
    assert "requires numeric" in note, "the model is not told which param it missed"
    assert WORD_LIMIT_MARKER not in note
    assert "40 words" not in note
    assert "the agent does nothing at all this tick" in note

    # `thought` no longer raises (soft-truncated instead — see
    # test_thought_over_the_limit_is_soft_truncated_not_rejected); `say` still
    # does, so it is what exercises the word-limit coaching branch here.
    with pytest.raises(ValidationError) as too_long:
        DecisionModel.model_validate(_valid(say=" ".join(["word"] * 21)))
    wordy = _retry_correction(too_long.value)
    assert WORD_LIMIT_MARKER in wordy
    assert "say <= 20 words" in wordy

    # An API error carries nothing the model can act on, so it still gets no note.
    import anthropic
    import httpx

    api_exc = anthropic.APIStatusError(
        "boom",
        response=httpx.Response(500, request=httpx.Request("POST", "https://api.anthropic.com")),
        body=None,
    )
    assert _retry_correction(api_exc) == ""


def test_prefixes_exceed_cache_minimums_by_char_heuristic() -> None:
    """Offline sanity floor only: ~4 chars/token is conservative for English.

    The REAL check is count_tokens at startup (verify_prefix_cacheable); this
    guards against someone gutting the prompt files without running --check.
    """
    from wasted_harness.brain.prompts import director_static_prefix, tactical_static_prefix

    assert len(tactical_static_prefix()) > 4096 * 4
    assert len(director_static_prefix()) > 1024 * 4


def test_the_director_gets_a_bigger_token_budget_than_tactical() -> None:
    """Regression: Sonnet 5 emits a THINKING block billed against the same `max_tokens`.

    Measured live 2026-09-02: at the shared 500-token cap the director returned
    `stop_reason=max_tokens`, `output_tokens=500`, and JSON truncated mid-`params` — every
    director call failed twice and the reflex layer kept control, so the agent played with no
    strategic layer. Haiku does not think, which is why the tactical tier never showed it.
    Same prompt at cap 1200 completes in 443 tokens with `end_turn`.
    """
    from wasted_harness.brain.tactical import DIRECTOR_MAX_DECISION_TOKENS, MAX_DECISION_TOKENS

    assert DIRECTOR_MAX_DECISION_TOKENS > MAX_DECISION_TOKENS
    assert DIRECTOR_MAX_DECISION_TOKENS >= 1000, "must clear the measured 443-token completion"


def test_the_director_actually_requests_that_budget() -> None:
    """The constant is worthless if the call site does not pass it — that was the whole bug."""
    import inspect

    from wasted_harness.brain import director

    src = inspect.getsource(director)
    assert "DIRECTOR_MAX_DECISION_TOKENS" in src, "director must pass its own token cap to run()"
    assert "max_tokens=DIRECTOR_MAX_DECISION_TOKENS" in src


def test_a_truncated_response_says_so_instead_of_no_parseable_object() -> None:
    """The original error ("model returned no parseable decision object") was misleading: the
    text was PRESENT, just cut off. That wording sent the diagnosis toward an empty response and
    cost a live debugging session. A max_tokens stop must name itself."""
    from types import SimpleNamespace

    import pytest

    from wasted_harness.brain.tactical import BilledCall

    class _Client:
        def __init__(self) -> None:
            self.messages = SimpleNamespace(create=self._create)

        @staticmethod
        def _create(**_: object) -> SimpleNamespace:
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"thought":"cut off here')],
                usage=SimpleNamespace(input_tokens=10, output_tokens=500,
                                      cache_read_input_tokens=0, cache_creation_input_tokens=0),
                stop_reason="max_tokens",
            )

    from wasted_harness.budget import Pricing
    from wasted_harness.settings import Settings

    pricing = Pricing.load(Settings.load().pricing_file)
    call = BilledCall(_Client(), pricing, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_tokens"):
        call.run("claude-sonnet-5", "prefix", "content")


# --- WP-C: TacticalBrain picks the mission-time model iff configured + active -


def _recording_client(captured: list[dict]):
    from types import SimpleNamespace

    class _Client:
        def __init__(self) -> None:
            self.messages = SimpleNamespace(create=self._create)

        @staticmethod
        def _create(**kwargs: object) -> SimpleNamespace:
            captured.append(kwargs)
            decision_json = (
                '{"thought": "steady", "say": "steady", "mood": "chill", '
                '"action": {"type": "stop", "params": {}}, "goal": "hold position", '
                '"confidence": 0.5}'
            )
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=decision_json)],
                usage=SimpleNamespace(input_tokens=10, output_tokens=20,
                                      cache_read_input_tokens=0, cache_creation_input_tokens=0),
                stop_reason="end_turn",
            )

    return _Client()


def _pricing_with_mission_tier(tmp_path):
    import yaml

    from wasted_harness.budget import Pricing

    model = {
        "id": "claude-sonnet-5", "input_per_mtok": 2.00, "output_per_mtok": 10.00,
        "cache_read_per_mtok": 0.20, "cache_write_5m_per_mtok": 2.50,
        "min_cacheable_prefix_tokens": 1024,
    }
    tactical = dict(model, id="claude-haiku-4-5-20251001", input_per_mtok=1.00,
                     output_per_mtok=5.00, cache_read_per_mtok=0.10,
                     cache_write_5m_per_mtok=1.25, min_cacheable_prefix_tokens=4096)
    doc = {
        "source_url": "https://platform.claude.com/docs/en/about-claude/pricing",
        "fetched": "2026-08-25",
        "models": {"tactical": tactical, "director": model, "tactical_mission": model},
    }
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return Pricing.load(path)


def test_mission_active_and_tier_configured_uses_the_mission_model(tmp_path) -> None:
    from wasted_harness.brain.tactical import DIRECTOR_MAX_DECISION_TOKENS, TacticalBrain

    pricing = _pricing_with_mission_tier(tmp_path)
    captured: list[dict] = []
    brain = TacticalBrain(_recording_client(captured), pricing, None)
    brain.decide("dynamic context", mission_active=True)
    assert len(captured) == 1
    assert captured[0]["model"] == pricing.tactical_mission.id
    assert captured[0]["max_tokens"] == DIRECTOR_MAX_DECISION_TOKENS


def test_mission_inactive_uses_the_normal_tactical_model_even_when_the_tier_is_configured(
    tmp_path,
) -> None:
    from wasted_harness.brain.tactical import MAX_DECISION_TOKENS, TacticalBrain

    pricing = _pricing_with_mission_tier(tmp_path)
    captured: list[dict] = []
    brain = TacticalBrain(_recording_client(captured), pricing, None)
    brain.decide("dynamic context", mission_active=False)
    assert captured[0]["model"] == pricing.tactical.id
    assert captured[0]["max_tokens"] == MAX_DECISION_TOKENS


def test_mission_active_but_no_mission_tier_configured_falls_back_to_tactical() -> None:
    """Absent config = today's behaviour exactly, even mid-mission."""
    from wasted_harness.brain.tactical import TacticalBrain
    from wasted_harness.budget import Pricing
    from wasted_harness.settings import Settings

    pricing = Pricing.load(Settings.load().pricing_file)
    assert pricing.tactical_mission is not None, (
        "the shipped pricing.yaml configures it; this test wants the ABSENT case"
    )
    from dataclasses import replace

    pricing_without_mission_tier = replace(pricing, tactical_mission=None)
    captured: list[dict] = []
    brain = TacticalBrain(_recording_client(captured), pricing_without_mission_tier, None)
    brain.decide("dynamic context", mission_active=True)
    assert captured[0]["model"] == pricing.tactical.id
