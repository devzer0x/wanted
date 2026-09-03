"""Recovery reflexes: game restart, API backoff causes, stranded escalation.

Game states here are constructed pydantic objects (pure-function inputs), not
recorded sessions — the fixtures directory stays empty by design.
"""

import random
import threading
import time
from typing import Any

import pytest

from wasted_harness.behavior.recovery import (
    API_FAILURE_CAUSES,
    ARRESTED_STUCK_TIMEOUT_S,
    ATTACKER_CLOSE_RADIUS_M,
    BACKOFF_FLOOR_S,
    BLOCKING_SCREEN_KEYS,
    DAMAGE_ATTACK_HP,
    DAMAGE_WINDOW_S,
    DEAD_STUCK_TIMEOUT_S,
    HOSTILE_CLOSE_RADIUS_M,
    JACK_HANDOFF_GRACE_S,
    MAX_STALL_RECOVERY_ATTEMPTS,
    SCRIPT_STALL_TIMEOUT_S,
    STALL_INTERVENTION_GAP_S,
    STALL_MOVE_M,
    STALL_TYPE_BLOCK_S,
    STALL_WINDOW_S,
    STATIONARY_TASK_TYPES,
    STRANDED_RADII_M,
    THREAT_HOLD_S,
    WATER_TIMEOUT_S,
    ApiBackoff,
    BlockingScreenWatchdog,
    BridgeStallTracker,
    DamageTracker,
    DeathArrestRecovery,
    GameRestartDetector,
    IdleBreaker,
    JackHandoffGate,
    OffLoopGrab,
    RoadDodge,
    StrandedEscalator,
    StuckDetector,
    TaskStallDetector,
    ThreatLatch,
    WaterEscalator,
    classify_api_failure,
    flipped_action,
    threat_action,
)
from wasted_harness.bridge_client import BridgeApiError, BridgeTransientError, GameState, Vec3
from wasted_harness.perception import Delta


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_state(**over) -> GameState:
    """A minimal, contract-shaped /state body. Every field is explicit."""
    body = {
        "ts": "2026-08-29T00:00:00Z",
        "tick": over.pop("tick", 1000),
        "player": {
            "pos": {
                "x": over.get("pos", (0.0, 0.0, 0.0))[0],
                "y": over.get("pos", (0.0, 0.0, 0.0))[1],
                "z": over.pop("pos", (0.0, 0.0, 0.0))[2],
            },
            "heading": 0.0,
            "health": over.pop("health", 200),
            "max_health": over.pop("max_health", 200),
            "armor": over.pop("armor", 0),
            "wanted": over.pop("wanted", 0),
            "cash": 0,
            "dead": over.pop("dead", False),
            "arrested": over.pop("arrested", False),
            "in_vehicle": over.pop("in_vehicle", False),
            "control_enabled": True,
            "switch_in_progress": over.pop("switch_in_progress", False),
        },
        "vehicle": over.pop("vehicle", None),
        "location": {"street": "Vinewood Blvd", "zone": "Downtown Vinewood"},
        "world": {"clock": "13:45", "weather": "CLEAR", "timescale": 1.0},
        "mission": {
            "active": over.pop("mission_active", False),
            "random_event_active": False,
            "cutscene_active": over.pop("cutscene_active", False),
            "retry_in_flight": over.pop("retry_in_flight", False),
        },
        "nearby": {
            "vehicles": over.pop("nearby_vehicles", []),
            "peds": over.pop("nearby_peds", []),
        },
        "last_task": {
            "id": over.pop("task_id", "t-1"),
            "type": over.pop("task_type", "stop"),
            "status": over.pop("task_status", "idle"),
            "detail": "",
        },
        "bridge": {"version": over.pop("bridge_version", "1.0.0"), "edition": "legacy"},
        # v1.11: {attacker_handle, being_jacked_by}. Absent by default, exactly
        # the pre-v1.11-bridge state every existing test in this file predates.
        "threat": over.pop("threat", {"attacker_handle": None, "being_jacked_by": None}),
    }
    assert not over, f"unused overrides: {sorted(over)}"
    return GameState.model_validate(body)


# --- game restart -------------------------------------------------------------


def test_restart_detected_when_tick_goes_backwards() -> None:
    det = GameRestartDetector()
    assert det.check(make_state(tick=50_000)) is False  # first observation
    assert det.check(make_state(tick=50_100)) is False
    assert det.check(make_state(tick=12)) is True  # game relaunched
    assert det.check(make_state(tick=40)) is False  # steady again
    assert det.restarts == 1


def test_restart_detected_when_bridge_version_changes() -> None:
    det = GameRestartDetector()
    det.check(make_state(tick=10, bridge_version="1.0.0"))
    assert det.check(make_state(tick=11, bridge_version="1.0.1")) is True


# --- API backoff --------------------------------------------------------------


class FakeStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"status {status}")
        self.status_code = status


class RateLimitError(Exception):
    """Named to match the SDK's class name, which is what classification uses."""


def test_classify_api_failure() -> None:
    assert classify_api_failure(RateLimitError()) == "rate_limit"
    assert classify_api_failure(FakeStatusError(429)) == "rate_limit"
    assert classify_api_failure(FakeStatusError(529)) == "overloaded"
    # A bare ValueError is what `brain.tactical`/`brain.director` raise for an
    # unparseable response: a local rejection, not an outage (H2).
    assert classify_api_failure(ValueError("bad json")) == "invalid_output"
    assert classify_api_failure(None) == "other"
    # Wrapped: DecisionFailedError chains the real cause.
    wrapped = RuntimeError("decision failed twice")
    wrapped.__cause__ = RateLimitError()
    assert classify_api_failure(wrapped) == "rate_limit"


def test_classify_against_the_real_sdk_exception_classes() -> None:
    """Classification must hold against anthropic's actual error types."""
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def err(cls, status: int, message: str = "boom"):
        response = httpx.Response(
            status,
            request=request,
            json={"type": "error", "error": {"type": "x", "message": message}},
        )
        return cls(message, response=response, body=None)

    assert classify_api_failure(err(anthropic.RateLimitError, 429)) == "rate_limit"
    assert classify_api_failure(err(anthropic.InternalServerError, 529)) == "overloaded"
    assert classify_api_failure(err(anthropic.InternalServerError, 500)) == "overloaded"
    assert classify_api_failure(err(anthropic.BadRequestError, 400)) == "other"
    # The live shape of an org spend cap: a 400 that stays true for days.
    quota = err(
        anthropic.BadRequestError,
        400,
        "You have reached your specified API usage limits. "
        "You will regain access on 2026-09-01 at 00:00 UTC.",
    )
    assert classify_api_failure(quota) == "rate_limit"

    from wasted_harness.brain.tactical import DecisionFailedError

    try:
        try:
            raise err(anthropic.RateLimitError, 429)
        except anthropic.APIError as exc:
            raise DecisionFailedError("failed twice") from exc
    except DecisionFailedError as wrapped:
        assert classify_api_failure(wrapped.__cause__ or wrapped) == "rate_limit"


def test_classify_survives_a_cause_cycle() -> None:
    a, b = ValueError("a"), ValueError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert classify_api_failure(a) == "invalid_output"  # terminates, does not hang
    c, d = RuntimeError("c"), RuntimeError("d")
    c.__cause__ = d
    d.__cause__ = c
    assert classify_api_failure(c) == "other"


# --- H2: a decision OUR schema rejected is not an API outage -------------------
#
# The session logs show the backoff escalating 1.8 -> 3.7 -> 9.0 -> 16.4 s purely
# from pydantic rejections while the API answered every call. That backoff is why
# The agent "did nothing" between attempts: the reflex layer held the wheel for the
# whole window for a bug that had nothing to do with the network.


def test_a_schema_violation_is_not_an_api_outage() -> None:
    import anthropic
    import httpx
    import pydantic

    from wasted_harness.brain.schemas import DecisionModel
    from wasted_harness.brain.tactical import DecisionFailedError

    # A REAL pydantic failure from the real decision model - whatever the model
    # currently rejects. An empty object misses every required field.
    with pytest.raises(pydantic.ValidationError) as caught:
        DecisionModel.model_validate({})
    validation_error = caught.value

    try:
        try:
            raise validation_error
        except pydantic.ValidationError as exc:
            raise DecisionFailedError("tactical decision failed twice") from exc
    except DecisionFailedError as wrapped:
        assert classify_api_failure(wrapped.__cause__ or wrapped) == "invalid_output"
        assert classify_api_failure(wrapped) == "invalid_output"

    # An unparseable response (`ValueError` from the brain's own parse step)
    # lands in the same bucket.
    assert classify_api_failure(ValueError("model returned no parseable decision object")) == (
        "invalid_output"
    )

    # ...but a genuine API failure still is one, whichever way it is wrapped.
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        429,
        request=request,
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
    )
    rate_limited = anthropic.RateLimitError("slow down", response=response, body=None)
    assert classify_api_failure(rate_limited) == "rate_limit"

    # A transport error is neither: unknown, retried fast, escalating.
    assert classify_api_failure(httpx.ConnectError("connection refused")) == "other"

    # And an SDK error is never re-read as a local schema problem just because
    # something ValueError-ish is chained underneath it.
    mixed = anthropic.APIConnectionError(request=request)
    mixed.__cause__ = ValueError("underlying parse blew up")
    assert classify_api_failure(mixed) == "other"


def test_invalid_output_has_no_backoff_floor() -> None:
    """If any caller ever does record it, it must not inherit an outage wait."""
    assert BACKOFF_FLOOR_S["invalid_output"] == 0.0
    assert "invalid_output" in API_FAILURE_CAUSES


def test_rate_limit_backoff_has_a_floor() -> None:
    clock = FakeClock()
    backoff = ApiBackoff(rng=random.Random(1), clock=clock)
    delay = backoff.record_failure("rate_limit")
    # Floor 30 s, then +/-20% jitter: never the 2 s a first "other" failure gets.
    assert delay >= BACKOFF_FLOOR_S["rate_limit"] * 0.8
    assert not backoff.ready()
    clock.t += delay + 0.01
    assert backoff.ready()


def test_other_failures_retry_fast_then_escalate() -> None:
    backoff = ApiBackoff(rng=random.Random(2), clock=FakeClock())
    first = backoff.record_failure("other")
    assert first < BACKOFF_FLOOR_S["rate_limit"]
    delays = [backoff.record_failure("other") for _ in range(6)]
    assert delays[-1] > first
    assert delays[-1] <= backoff.cap_s
    backoff.record_success()
    assert backoff.failures == 0
    assert backoff.ready()


def test_unknown_cause_falls_back_to_other() -> None:
    backoff = ApiBackoff(rng=random.Random(3), clock=FakeClock())
    backoff.record_failure("something_new")
    assert backoff.last_cause == "other"


# --- stranded / flipped -------------------------------------------------------


def test_stranded_widens_the_search_then_hands_back_to_the_brain() -> None:
    clock = FakeClock()
    esc = StrandedEscalator(clock=clock)
    radii = []
    for _ in range(len(STRANDED_RADII_M)):
        action = esc.check(make_state(in_vehicle=False, task_status="idle"))
        assert action is not None
        assert action["type"] == "enter_nearest_vehicle"
        radii.append(action["params"]["search_radius_m"])
        clock.t += esc.retry_gap_s + 1
    assert radii == list(STRANDED_RADII_M)
    assert radii == sorted(radii), "search radius must widen, not shrink"
    # Exhausted: the next move is a creative one, so it belongs to the model.
    assert esc.check(make_state(in_vehicle=False, task_status="idle")) is None


def test_stranded_is_silent_while_a_task_runs_or_in_a_vehicle() -> None:
    esc = StrandedEscalator(clock=FakeClock())
    assert esc.check(make_state(in_vehicle=True)) is None
    assert esc.check(make_state(in_vehicle=False, task_status="running")) is None


def test_stranded_resets_once_back_in_a_car() -> None:
    clock = FakeClock()
    esc = StrandedEscalator(clock=clock)
    esc.check(make_state(in_vehicle=False, task_status="idle"))
    assert esc.attempts == 1
    esc.check(make_state(in_vehicle=True))
    assert esc.attempts == 0


@pytest.mark.parametrize(
    ("upside_down", "speed", "expected"),
    [(True, 0.0, "exit_vehicle"), (True, 5.0, None), (False, 0.0, None)],
)
def test_flipped_action(upside_down: bool, speed: float, expected: str | None) -> None:
    vehicle = {
        "handle": 1,
        "model": "adder",
        "display_name": "Adder",
        "class": "Super",
        "speed": speed,
        "health": 900.0,
        "upside_down": upside_down,
        "in_water": False,
        "stopped_for_s": 30.0,
    }
    action = flipped_action(make_state(in_vehicle=True, vehicle=vehicle))
    assert (action or {}).get("type") == expected


# --- T9 (findings.md R1/R5): WaterEscalator, RoadDodge, JackHandoffGate ------------------------


def _water_vehicle(**over: Any) -> dict[str, Any]:
    base = {
        "handle": 1, "model": "squalo", "display_name": "Squalo", "class": "Boats",
        "speed": 0.0, "health": 900.0, "upside_down": False, "in_water": True,
        "stopped_for_s": 5.0,
    }
    base.update(over)
    return base


def test_water_escalator_exits_only_after_the_timeout() -> None:
    clock = FakeClock()
    esc = WaterEscalator(clock=clock)
    state = make_state(in_vehicle=True, vehicle=_water_vehicle())
    assert esc.check(state) is None, "under the timeout: nothing yet"
    clock.t += WATER_TIMEOUT_S - 0.1
    assert esc.check(state) is None
    clock.t += 0.2
    action = esc.check(state)
    assert action == {"type": "exit_vehicle", "params": {}}


def test_water_escalator_walks_toward_last_outdoor_after_exiting() -> None:
    clock = FakeClock()
    esc = WaterEscalator(clock=clock)
    in_water = make_state(in_vehicle=True, vehicle=_water_vehicle())
    assert esc.check(in_water) is None, "arms the timer; nothing yet"
    clock.t += WATER_TIMEOUT_S + 1.0
    assert esc.check(in_water) == {"type": "exit_vehicle", "params": {}}

    on_foot = make_state(in_vehicle=False)
    on_foot.player.last_outdoor = Vec3(x=10.0, y=20.0, z=30.0)
    clock.t += 1.0
    action = esc.check(on_foot)
    assert action == {
        "type": "walk_to",
        "params": {"x": 10.0, "y": 20.0, "z": 30.0, "run": True},
    }
    # One walk order per bout, not one every tick.
    clock.t += 1.0
    assert esc.check(on_foot) is None


def test_water_escalator_says_so_honestly_with_no_last_outdoor() -> None:
    clock = FakeClock()
    esc = WaterEscalator(clock=clock)
    in_water = make_state(in_vehicle=True, vehicle=_water_vehicle())
    esc.check(in_water)
    clock.t += WATER_TIMEOUT_S + 1.0
    esc.check(in_water)
    on_foot = make_state(in_vehicle=False)  # last_outdoor defaults to None
    clock.t += 1.0
    assert esc.check(on_foot) is None, "no shore data and no last_outdoor: nothing to guess"


def test_water_escalator_does_nothing_dry() -> None:
    esc = WaterEscalator()
    dry = _water_vehicle(in_water=False)
    assert esc.check(make_state(in_vehicle=True, vehicle=dry)) is None


def _npc_vehicle(handle: int, x: float, y: float, *, driver: str = "npc", distance: float = 5.0) -> dict[str, Any]:
    return {
        "handle": handle, "model": "blista", "display_name": "Blista", "class": "Compact",
        "distance": distance, "driver": driver, "pos": {"x": x, "y": y, "z": 0.0},
    }


def test_road_dodge_fires_when_a_close_vehicle_closes_fast() -> None:
    clock = FakeClock()
    dodge = RoadDodge(clock=clock)
    far = make_state(in_vehicle=False, nearby_vehicles=[_npc_vehicle(1, 0.0, 20.0, distance=20.0)])
    assert dodge.check(far) is None, "first tick: nothing to derive a closing speed from yet"
    clock.t += 1.0
    # A second closer than ROAD_DODGE_RADIUS_M, having covered well over
    # ROAD_DODGE_CLOSING_MPS metres in that one second.
    close = make_state(in_vehicle=False, nearby_vehicles=[_npc_vehicle(1, 0.0, 5.0, distance=5.0)])
    action = dodge.check(close)
    assert action is not None
    assert action["type"] == "walk_to"
    assert action["params"]["run"] is True
    # Stepped AWAY from the vehicle (negative y: the vehicle is at y=5 and he is at y=0,
    # so away-from-it is further toward negative y).
    assert action["params"]["y"] < 0.0


def test_road_dodge_ignores_a_parked_or_distant_vehicle() -> None:
    clock = FakeClock()
    dodge = RoadDodge(clock=clock)
    dodge.check(make_state(in_vehicle=False, nearby_vehicles=[_npc_vehicle(1, 0.0, 20.0, distance=20.0)]))
    clock.t += 1.0
    # Barely moved: under ROAD_DODGE_CLOSING_MPS.
    barely = make_state(in_vehicle=False, nearby_vehicles=[_npc_vehicle(1, 0.0, 19.5, distance=19.5)])
    assert dodge.check(barely) is None


def test_road_dodge_ignores_the_players_own_vehicle_and_empty_cars() -> None:
    clock = FakeClock()
    dodge = RoadDodge(clock=clock)
    dodge.check(make_state(in_vehicle=False, nearby_vehicles=[_npc_vehicle(1, 0.0, 20.0, driver="player", distance=20.0)]))
    clock.t += 1.0
    close = make_state(in_vehicle=False, nearby_vehicles=[_npc_vehicle(1, 0.0, 5.0, driver="player", distance=5.0)])
    assert dodge.check(close) is None, "his own car closing on him is not a road hazard"


def test_road_dodge_never_fires_while_seated() -> None:
    dodge = RoadDodge()
    seated = make_state(
        in_vehicle=True,
        vehicle={
            "handle": 9, "model": "adder", "display_name": "Adder", "class": "Super",
            "speed": 10.0, "health": 900.0, "upside_down": False, "in_water": False,
            "stopped_for_s": 0.0,
        },
        nearby_vehicles=[_npc_vehicle(1, 0.0, 2.0, distance=2.0)],
    )
    assert dodge.check(seated) is None


def test_jack_handoff_gate_holds_for_the_grace_window_after_being_jacked() -> None:
    clock = FakeClock()
    gate = JackHandoffGate(clock=clock)
    jacked = make_state(threat={"attacker_handle": None, "being_jacked_by": 555})
    assert gate.feed(jacked) is True
    clear = make_state(threat={"attacker_handle": None, "being_jacked_by": None})
    clock.t += JACK_HANDOFF_GRACE_S - 0.5
    assert gate.feed(clear) is True, "still inside the grace window"
    clock.t += 1.0
    assert gate.feed(clear) is False, "grace window over: stranded may act again"


def test_jack_handoff_gate_is_false_when_nothing_was_ever_jacked() -> None:
    gate = JackHandoffGate()
    assert gate.feed(make_state()) is False


# --- bridge up but not ready (CONTRACTS v1.2 transient 503s) ------------------


def _transient(code: str, status: int = 503) -> BridgeTransientError:
    return BridgeTransientError(status, code, "the first game tick has not completed yet")


def test_a_not_ready_bridge_is_never_reported_as_bridge_down() -> None:
    """§4 `bridge_down` means unreachable. A bridge answering 503 not_ready is
    reachable and working — claiming an outage would put a false line on the
    site — so the stall tracker emits no event at all, only backoff."""
    clock = FakeClock()
    stall = BridgeStallTracker(clock=clock)
    for _ in range(200):
        wait = stall.record_failure(_transient("not_ready"))
        assert 0.0 < wait <= 5.0
        clock.t += wait
    assert stall.consecutive == 200
    # It ends by reporting how long it lasted, and nothing else.
    stalled_for = stall.record_success()
    assert stalled_for is not None and stalled_for > 0
    assert stall.consecutive == 0
    assert stall.record_success() is None  # not a stall; nothing to report


def test_stall_backoff_climbs_then_caps() -> None:
    stall = BridgeStallTracker(clock=FakeClock())
    waits = [stall.record_failure(_transient("game_thread_stalled")) for _ in range(8)]
    assert waits[0] == 0.5
    assert waits == sorted(waits), "backoff must not shrink while the stall lasts"
    assert max(waits) <= 5.0


def test_unstick_treats_every_bridge_refusal_as_no_nudge_not_a_crash() -> None:
    """A three-metre nudge is never worth taking the show down for."""

    class _Bridge:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        def unstick(self):
            raise self._exc

    detector = StuckDetector()
    assert detector.try_unstick(_Bridge(_transient("game_thread_stalled"))) is None
    assert detector.try_unstick(_Bridge(_transient("queue_full"))) is None
    assert detector.try_unstick(_Bridge(_transient("warp_core_breach"))) is None  # unknown code
    assert detector.try_unstick(
        _Bridge(BridgeApiError(409, "unstick_conditions_not_met", "not stuck"))
    ) is None
    assert detector.try_unstick(
        _Bridge(BridgeApiError(400, "invalid_params", "nonsense"))
    ) is None


# --- combat / threat reflex ----------------------------------------------------
#
# Observed live: a firefight left the agent standing still, because the only
# thing driving combat was a 1-2 s model call. `threat_action` is the reflex
# fix — no model call, evaluated fresh every tick.


def _hostile(distance: float) -> dict:
    return {"handle": 9, "model": "s_m_y", "distance": distance, "relationship": "hostile"}


NO_DANGER_DELTA = Delta(wanted_from=0, wanted_to=0)


def test_threat_action_is_silent_with_nothing_wrong() -> None:
    assert threat_action(make_state(), NO_DANGER_DELTA) is None


def test_threat_action_health_overrides_everything_including_wanted() -> None:
    """Hurt outranks the whole rest of the ladder, wanted stars included."""
    state = make_state(wanted=1, health=5, in_vehicle=False, nearby_peds=[_hostile(2.0)])
    assert threat_action(state, NO_DANGER_DELTA) == {
        "type": "seek_cover",
        "params": {"duration_s": 10},
    }


def test_threat_action_fights_a_close_hostile_even_while_wanted() -> None:
    """Revised per live feedback: the first cut fled from any `wanted > 0`
    unconditionally, so he never fought back even standing right next to
    whoever was already shooting at him ("does not just flee everything").
    Healthy + a close hostile (cop included — CONTRACTS exposes no
    "is a cop" field, only `relationship`) now fights, wanted stars or not."""
    state = make_state(wanted=3, in_vehicle=False, nearby_peds=[_hostile(2.0)])
    assert threat_action(state, NO_DANGER_DELTA) == {
        "type": "combat_hated_targets_around",
        "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M},
    }


def test_threat_action_flees_police_when_wanted_but_nothing_close_yet() -> None:
    """Stars accumulating from range, nobody actually in your face: the
    ordinary evade-by-driving response still applies."""
    state = make_state(wanted=2, in_vehicle=True)
    assert threat_action(state, NO_DANGER_DELTA) == {"type": "flee_police", "params": {}}


def test_threat_action_does_not_flee_a_mission_firefight() -> None:
    """Plenty of story missions ARE a scripted gunfight with police, and
    prompts/situations.md's rule for that case — shipping in the same deploy —
    is "fight, don't flee". A reflex that drove him away the moment a star
    appeared abandoned the mission and failed it. Healthy, nobody close enough
    to engage, stars up, mission active: this reflex says nothing and the
    mission follower / brain keep the wheel."""
    state = make_state(wanted=2, mission_active=True, in_vehicle=True)
    assert threat_action(state, NO_DANGER_DELTA) is None


def test_threat_action_still_fights_during_a_mission_when_a_hostile_is_close() -> None:
    """The mission gate is only on the flee branch: mission or not, something
    shooting at him from 2 m is still fought."""
    state = make_state(
        wanted=2, mission_active=True, in_vehicle=False, nearby_peds=[_hostile(2.0)]
    )
    assert threat_action(state, NO_DANGER_DELTA) == {
        "type": "combat_hated_targets_around",
        "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M},
    }


def test_threat_action_still_breaks_contact_during_a_mission_when_hurt() -> None:
    """Nor on the survival branch: "fight, don't flee" is not "die where you
    stand"."""
    state = make_state(wanted=2, mission_active=True, health=20, in_vehicle=False)
    assert threat_action(state, NO_DANGER_DELTA) == {
        "type": "seek_cover",
        "params": {"duration_s": 10},
    }


def test_threat_action_breaks_contact_on_foot_when_health_is_low() -> None:
    state = make_state(health=50, in_vehicle=False)  # 25% of 200
    assert threat_action(state, NO_DANGER_DELTA) == {
        "type": "seek_cover",
        "params": {"duration_s": 10},
    }


def test_threat_action_breaks_contact_via_a_big_health_drop_even_above_the_floor() -> None:
    """A big single-tick drop counts as hurt even if the absolute health is
    still above the static floor — a health bar can still read "high" a tick
    after a burst that will keep dropping it."""
    state = make_state(health=150, in_vehicle=False)
    dropping = Delta(wanted_from=0, wanted_to=0, big_health_drop=True)
    assert threat_action(state, dropping) is not None


def test_threat_action_drives_away_when_hurt_in_a_vehicle() -> None:
    vehicle = {
        "handle": 1, "model": "adder", "display_name": "Adder", "class": "Super",
        "speed": 15.0, "health": 900.0, "upside_down": False, "in_water": False,
        "stopped_for_s": 0.0,
    }
    state = make_state(health=50, in_vehicle=True, vehicle=vehicle)
    assert threat_action(state, NO_DANGER_DELTA) == {
        "type": "wander_drive",
        "params": {"style": "avoid_traffic"},
    }


def test_threat_action_fights_a_close_hostile_on_foot_when_healthy() -> None:
    state = make_state(in_vehicle=False, nearby_peds=[_hostile(HOSTILE_CLOSE_RADIUS_M - 1)])
    assert threat_action(state, NO_DANGER_DELTA) == {
        "type": "combat_hated_targets_around",
        "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M},
    }


def test_threat_action_ignores_a_hostile_outside_the_close_radius() -> None:
    state = make_state(in_vehicle=False, nearby_peds=[_hostile(HOSTILE_CLOSE_RADIUS_M + 1)])
    assert threat_action(state, NO_DANGER_DELTA) is None


def test_threat_action_ignores_a_neutral_ped_no_matter_how_close() -> None:
    state = make_state(
        in_vehicle=False,
        nearby_peds=[{"handle": 9, "model": "s_m_y", "distance": 1.0, "relationship": "neutral"}],
    )
    assert threat_action(state, NO_DANGER_DELTA) is None


def test_a_hostile_seen_from_a_moving_car_does_not_stop_the_car() -> None:
    """The operator's reversal, upper half. The old rule fought a hostile
    "REGARDLESS of in_vehicle"; the live bug it closed was "sits in car and
    dies", and the answer to that is still "stop sitting" — it is just no
    longer "get out and fight". A car already MOVING is already doing the best
    available thing, so the ladder falls through rather than preempting a
    working escape with a combat task."""
    vehicle = {
        "handle": 1, "model": "adder", "display_name": "Adder", "class": "Super",
        "speed": 15.0, "health": 900.0, "upside_down": False, "in_water": False,
        "stopped_for_s": 0.0,
    }
    state = make_state(in_vehicle=True, vehicle=vehicle, nearby_peds=[_hostile(2.0)])
    assert threat_action(state, NO_DANGER_DELTA) is None


def test_a_hostile_seen_from_a_parked_car_makes_him_leave_not_fight() -> None:
    """Same rung, lower half: parked and healthy with a hostile in range is the
    "sits in car and dies" state, and the answer is to drive off."""
    vehicle = {
        "handle": 1, "model": "adder", "display_name": "Adder", "class": "Super",
        "speed": 0.0, "health": 900.0, "upside_down": False, "in_water": False,
        "stopped_for_s": 4.0,
    }
    state = make_state(in_vehicle=True, vehicle=vehicle, nearby_peds=[_hostile(2.0)])
    assert threat_action(state, NO_DANGER_DELTA) == {
        "type": "wander_drive",
        "params": {"style": "avoid_traffic"},
    }


def test_a_car_that_cannot_leave_still_fights() -> None:
    """"In a vehicle that cannot move while taking damage, the existing ladder
    is right." Both routes to immobile are covered: the contract fields
    (upside down / in the water / a shell) and the state machine's measured
    `vehicle_blocked`, which is the only evidence there is that a car with
    healthy bodywork will not actually move."""
    fight = {
        "type": "combat_hated_targets_around",
        "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M},
    }
    base = {
        "handle": 1, "model": "adder", "display_name": "Adder", "class": "Super",
        "speed": 0.0, "health": 900.0, "upside_down": False, "in_water": False,
        "stopped_for_s": 9.0,
    }
    flipped = make_state(
        in_vehicle=True, vehicle={**base, "upside_down": True}, nearby_peds=[_hostile(2.0)]
    )
    assert threat_action(flipped, NO_DANGER_DELTA) == fight

    drowning = make_state(
        in_vehicle=True, vehicle={**base, "in_water": True}, nearby_peds=[_hostile(2.0)]
    )
    assert threat_action(drowning, NO_DANGER_DELTA) == fight

    shell = make_state(
        in_vehicle=True, vehicle={**base, "health": 20.0}, nearby_peds=[_hostile(2.0)]
    )
    assert threat_action(shell, NO_DANGER_DELTA) == fight

    parked = make_state(in_vehicle=True, vehicle=base, nearby_peds=[_hostile(2.0)])
    assert threat_action(parked, NO_DANGER_DELTA, vehicle_blocked=True) == fight


def test_a_mission_firefight_still_fights_from_the_car() -> None:
    """prompts/situations.md: "A mission firefight is not a car chase — fight,
    don't flee"; "Fleeing a scripted firefight fails the mission." A reflex
    preempts a prompt every time, so the mission/free-roam split has to be in
    the reflex or the two layers contradict each other on stream. Same
    precedent as the `flee_police` rung, which is already mission-gated."""
    vehicle = {
        "handle": 1, "model": "adder", "display_name": "Adder", "class": "Super",
        "speed": 0.0, "health": 900.0, "upside_down": False, "in_water": False,
        "stopped_for_s": 4.0,
    }
    state = make_state(
        in_vehicle=True, vehicle=vehicle, mission_active=True, nearby_peds=[_hostile(2.0)]
    )
    assert threat_action(state, NO_DANGER_DELTA) == {
        "type": "combat_hated_targets_around",
        "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M},
    }


def test_threat_action_ignores_a_hostile_far_beyond_engagement_range() -> None:
    state = make_state(in_vehicle=False, nearby_peds=[_hostile(HOSTILE_CLOSE_RADIUS_M + 20.0)])
    assert threat_action(state, NO_DANGER_DELTA) is None


def test_threat_action_defers_to_death_arrest_recovery() -> None:
    """Belt-and-braces: `main._reflex` never calls this while dead/arrested,
    but the function is honest about it either way."""
    assert threat_action(make_state(dead=True, wanted=5), NO_DANGER_DELTA) is None
    assert threat_action(make_state(arrested=True, wanted=5), NO_DANGER_DELTA) is None


# --- death / arrest recovery -----------------------------------------------


def test_death_arrest_recovery_is_quiet_while_alive_and_free() -> None:
    tracker = DeathArrestRecovery(clock=FakeClock())
    result = tracker.feed(make_state(dead=False, arrested=False))
    assert result == {"respawned": False, "respawn_cause": None}


def test_death_arrest_recovery_detects_the_respawn() -> None:
    clock = FakeClock()
    tracker = DeathArrestRecovery(clock=clock)
    tracker.feed(make_state(dead=True))
    clock.t += 3.0  # an ordinary few-second death fade
    result = tracker.feed(make_state(dead=False))
    assert result["respawned"] is True
    assert result["respawn_cause"] == "dead"


def test_death_arrest_recovery_detects_release_from_arrest() -> None:
    clock = FakeClock()
    tracker = DeathArrestRecovery(clock=clock)
    tracker.feed(make_state(arrested=True))
    clock.t += 10.0
    result = tracker.feed(make_state(arrested=False))
    assert result["respawned"] is True
    assert result["respawn_cause"] == "arrested"


def test_death_arrest_recovery_does_not_refire_while_still_down() -> None:
    clock = FakeClock()
    tracker = DeathArrestRecovery(clock=clock)
    tracker.feed(make_state(dead=True))
    for _ in range(5):
        clock.t += 1.0
        result = tracker.feed(make_state(dead=True))
        assert result["respawned"] is False


def test_death_arrest_recovery_logs_loudly_past_the_dead_timeout_but_does_not_spin(
    caplog,
) -> None:
    clock = FakeClock()
    tracker = DeathArrestRecovery(clock=clock)
    tracker.feed(make_state(dead=True))
    with caplog.at_level("ERROR", logger="wasted.recovery"):
        clock.t += DEAD_STUCK_TIMEOUT_S + 1.0
        tracker.feed(make_state(dead=True))  # first stuck log
        clock.t += 1.0
        tracker.feed(make_state(dead=True))  # too soon to re-log
    stuck_logs = [r for r in caplog.records if "past a sane timeout" in r.message]
    assert len(stuck_logs) == 1, "must not spam a log line every tick"


def test_death_arrest_recovery_uses_the_longer_arrested_timeout() -> None:
    """Being cuffed runs a longer scripted sequence than a death fade — the
    stuck ceiling must not be the same number for both."""
    clock = FakeClock()
    tracker = DeathArrestRecovery(clock=clock)
    tracker.feed(make_state(arrested=True))
    clock.t += DEAD_STUCK_TIMEOUT_S + 1.0  # past the DEAD ceiling only, not ARRESTED's
    tracker.feed(make_state(arrested=True))
    assert tracker._last_stuck_log == 0.0, "arrested must not use the dead timeout"
    clock.t += ARRESTED_STUCK_TIMEOUT_S
    tracker.feed(make_state(arrested=True))
    assert tracker._last_stuck_log != 0.0


# --- blocking-screen watchdog ---------------------------------------------
#
# Confirmed live: a mission failure put GTA V on a "MISSION FAILED" / retry
# screen and froze the SHVDN script thread entirely (`/health.tick_hz`
# pinned at 0.0, `/state` frozen) — and `SendKeys` does nothing at all,
# confirmed live too (GTA V discards synthetic window messages). This
# watchdog detects the freeze from the outside (state.tick not advancing)
# and drives a real SendInput keypress to try to clear it.


def test_watchdog_is_quiet_while_ticking_normally() -> None:
    wd = BlockingScreenWatchdog(clock=FakeClock())
    for tick in range(1000, 1010):
        assert wd.feed(make_state(tick=tick)) is None


def test_watchdog_is_quiet_within_the_stall_grace_period() -> None:
    clock = FakeClock()
    wd = BlockingScreenWatchdog(clock=clock)
    wd.feed(make_state(tick=42))
    clock.t += SCRIPT_STALL_TIMEOUT_S - 0.1
    assert wd.feed(make_state(tick=42)) is None  # same tick, but not long enough yet


def test_watchdog_presses_the_first_key_once_the_stall_timeout_passes() -> None:
    clock = FakeClock()
    wd = BlockingScreenWatchdog(clock=clock)
    wd.feed(make_state(tick=42))
    clock.t += SCRIPT_STALL_TIMEOUT_S + 0.1
    assert wd.feed(make_state(tick=42)) == BLOCKING_SCREEN_KEYS[0]


def test_watchdog_never_intervenes_on_a_frozen_cutscene() -> None:
    """The exact distinction the brief demands: a legitimately paused
    cutscene must be waited out, never treated as a dead script thread."""
    clock = FakeClock()
    wd = BlockingScreenWatchdog(clock=clock)
    wd.feed(make_state(tick=42, cutscene_active=True))
    clock.t += SCRIPT_STALL_TIMEOUT_S + 100.0
    assert wd.feed(make_state(tick=42, cutscene_active=True)) is None


def test_watchdog_stands_down_the_instant_the_tick_moves_again() -> None:
    clock = FakeClock()
    wd = BlockingScreenWatchdog(clock=clock)
    wd.feed(make_state(tick=42))
    clock.t += SCRIPT_STALL_TIMEOUT_S + 0.1
    assert wd.feed(make_state(tick=42)) is not None  # a key got pressed
    clock.t += 0.1
    assert wd.feed(make_state(tick=43)) is None  # ticking again: stand down
    # And it starts fresh from here — no leftover stall state.
    clock.t += SCRIPT_STALL_TIMEOUT_S + 0.1
    assert wd.feed(make_state(tick=43)) is not None  # a genuinely new stall


def test_watchdog_escalates_through_the_key_sequence_then_gives_up(caplog) -> None:
    clock = FakeClock()
    wd = BlockingScreenWatchdog(clock=clock)
    wd.feed(make_state(tick=42))
    clock.t += SCRIPT_STALL_TIMEOUT_S + 0.1
    pressed: list[str] = []
    with caplog.at_level("ERROR", logger="wasted.recovery"):
        for _ in range(MAX_STALL_RECOVERY_ATTEMPTS + 3):
            key = wd.feed(make_state(tick=42))
            if key is not None:
                pressed.append(key)
            clock.t += wd.retry_gap_s + 0.1
    assert len(pressed) == MAX_STALL_RECOVERY_ATTEMPTS, "must be bounded, never spam keys"
    assert pressed[0] == BLOCKING_SCREEN_KEYS[0]
    gave_up_logs = [r for r in caplog.records if "gave up" in r.message]
    assert len(gave_up_logs) == 1, "must give up loudly exactly once, not repeat it"


def test_watchdog_respects_the_gap_between_attempts() -> None:
    clock = FakeClock()
    wd = BlockingScreenWatchdog(clock=clock)
    wd.feed(make_state(tick=42))
    clock.t += SCRIPT_STALL_TIMEOUT_S + 0.1
    assert wd.feed(make_state(tick=42)) is not None  # attempt 1
    clock.t += 0.1  # well inside the retry gap
    assert wd.feed(make_state(tick=42)) is None


# --- unstick rate limit --------------------------------------------------------
#
# Observed live (server log, 2026-09-01): 7 /unstick nudges in 34 s, five of them
# inside 1.35 s, ~17.6 m of cumulative displacement. The detector returned
# "unstick" on every tick while the car stayed stopped. CLAUDE.md rule 5 allows
# one thing only: "an unstick nudge of a few meters when wedged".


def _stuck_vehicle(**over) -> dict:
    v = {
        "handle": 1,
        "model": "adder",
        "display_name": "Adder",
        "class": "Super",
        "speed": 0.0,
        "health": 900.0,
        "upside_down": False,
        "in_water": False,
        "stopped_for_s": 30.0,
    }
    v.update(over)
    return v


class _NudgingBridge:
    def __init__(self, refuse: bool = False) -> None:
        self.calls = 0
        self.refuse = refuse

    def unstick(self):
        self.calls += 1
        if self.refuse:
            raise BridgeApiError(409, "unstick_conditions_not_met", "not stuck")

        class _R:
            moved = True
            distance_m = 3.0

        return _R()


def _stuck_state():
    return make_state(
        in_vehicle=True, vehicle=_stuck_vehicle(), task_type="drive_to", task_status="running"
    )


def test_unstick_is_rate_limited_and_capped_per_episode() -> None:
    t = [1000.0]
    det = StuckDetector(clock=lambda: t[0])
    bridge = _NudgingBridge()

    # Ladder starts with the harmless primitive, never the nudge.
    assert det.check(_stuck_state()) == "reverse_out"

    # Still parked 5 s later: ONE nudge is allowed.
    t[0] += 5
    assert det.check(_stuck_state()) == "unstick"
    assert det.try_unstick(bridge) == 3.0

    # Every tick right after (the live failure): NO further nudge inside the cooldown.
    for _ in range(12):
        t[0] += 0.3
        assert det.check(_stuck_state()) is None
    assert bridge.calls == 1

    # Cooldown elapsed, reverse_out still "recent" (<45 s): the second and LAST nudge.
    t[0] += 30
    assert det.check(_stuck_state()) == "unstick"
    assert det.try_unstick(bridge) == 3.0
    assert bridge.calls == 2

    # Much later: reverse_out is retried (it is a primitive, not a cheat)...
    t[0] += 60
    assert det.check(_stuck_state()) == "reverse_out"
    # ...but the per-episode nudge cap holds even though the cooldown has passed.
    t[0] += 1
    assert det.check(_stuck_state()) is None
    assert bridge.calls == 2

    # The car moves: the episode ends and the ladder is reset from the top.
    moving = make_state(
        in_vehicle=True,
        vehicle=_stuck_vehicle(stopped_for_s=0.0, speed=12.0),
        task_type="drive_to",
        task_status="running",
    )
    assert det.check(moving) is None
    t[0] += 1
    assert det.check(_stuck_state()) == "reverse_out"


def test_a_refused_nudge_still_counts_toward_the_cooldown() -> None:
    """A 409 loop must not hammer the bridge every tick either."""
    t = [2000.0]
    det = StuckDetector(clock=lambda: t[0])
    bridge = _NudgingBridge(refuse=True)
    assert det.check(_stuck_state()) == "reverse_out"
    t[0] += 5
    assert det.check(_stuck_state()) == "unstick"
    assert det.try_unstick(bridge) is None  # refused, no nudge happened
    t[0] += 1
    assert det.check(_stuck_state()) is None  # but the attempt started the cooldown
    assert bridge.calls == 1


# --- ThreatLatch: one post per threat, not one per tick ------------------------
#
# Observed on stream: "walks like someone is pressing W constantly, stuttering"
# and "fires but not at the cops". CONTRACTS §1 - every POST /task preempts the
# running one - plus a 3-4 Hz reflex re-issuing the same combat order means the
# engine's combat task is torn down and restarted several times a second, so its
# aim/approach cycle never completes.

COMBAT = {"type": "combat_hated_targets_around", "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M}}
COVER = {"type": "seek_cover", "params": {"duration_s": 10}}


def test_latch_issues_the_first_threat_action() -> None:
    latch = ThreatLatch(clock=FakeClock())
    assert latch.should_issue(COMBAT, make_state(task_status="idle")) is True


def test_latch_stays_quiet_while_the_engine_runs_that_exact_task() -> None:
    clock = FakeClock()
    latch = ThreatLatch(clock=clock)
    running = make_state(task_status="running", task_type="combat_hated_targets_around")
    assert latch.should_issue(COMBAT, make_state(task_status="idle")) is True
    latch.issued(COMBAT)
    for _ in range(10):
        clock.t += 1.0  # well past the hold-down by the end
        assert latch.should_issue(COMBAT, running) is False


def test_latch_ignores_a_radius_difference_it_is_the_same_order() -> None:
    latch = ThreatLatch(clock=FakeClock())
    running = make_state(task_status="running", task_type="combat_hated_targets_around")
    wider = {"type": "combat_hated_targets_around", "params": {"radius_m": 25.0}}
    assert latch.should_issue(wider, running) is False


def test_latch_holds_briefly_when_the_task_reports_finished() -> None:
    clock = FakeClock()
    latch = ThreatLatch(clock=clock)
    done = make_state(task_status="done", task_type="combat_hated_targets_around")
    latch.issued(COMBAT)
    clock.t += THREAT_HOLD_S / 2
    assert latch.should_issue(COMBAT, done) is False
    clock.t += THREAT_HOLD_S
    assert latch.should_issue(COMBAT, done) is True


def test_latch_never_delays_a_change_of_situation_class() -> None:
    """Fight -> break contact must go out immediately: the hold is per action
    type, and the action type IS the intent."""
    clock = FakeClock()
    latch = ThreatLatch(clock=clock)
    latch.issued(COMBAT)
    running = make_state(task_status="running", task_type="combat_hated_targets_around")
    assert latch.should_issue(COVER, running) is True


def test_latch_reset_re_arms_it() -> None:
    clock = FakeClock()
    latch = ThreatLatch(clock=clock)
    latch.issued(COMBAT)
    assert latch.should_issue(COMBAT, make_state(task_status="idle")) is False
    latch.reset()
    assert latch.should_issue(COMBAT, make_state(task_status="idle")) is True


# --- OffLoopGrab: a wedged capture device may not own the loop thread ---------
#
# The real block: dxcam logged "Output change/access loss detected" at 16:53:53
# and its 90-attempt recovery ran INSIDE grab(), which was being called straight
# from the main loop. No perception, no reflex, no decision and no heartbeat for
# 172 s (and 76 s earlier the same session).


def test_a_slow_grab_does_not_stall_the_caller() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(30.0)  # far longer than the deadline; released in teardown
        return "late frame"

    pump = OffLoopGrab(slow, timeout_s=0.05, clock=FakeClock())
    try:
        t0 = time.monotonic()
        assert pump.poll() is None
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"the grab owned the caller for {elapsed:.2f}s"
        assert started.is_set(), "the work never actually ran"
        assert pump.failures == 1

        # A second tick must not wait on it again, and must not pile a second
        # grab onto a device that is already busy.
        t0 = time.monotonic()
        assert pump.poll() is None
        assert time.monotonic() - t0 < 0.05
        assert pump.failures == 1, "one wedged attempt is one failure, not one per tick"
    finally:
        release.set()


def test_a_late_result_is_used_once_it_arrives() -> None:
    """A capture that is merely slow must not blind the harness forever."""
    release = threading.Event()

    def slow():
        release.wait(5.0)
        return "frame"

    pump = OffLoopGrab(slow, timeout_s=0.05, clock=FakeClock())
    assert pump.poll() is None
    release.set()
    deadline = time.monotonic() + 5.0
    got = None
    while got is None and time.monotonic() < deadline:
        got = pump.poll()
        time.sleep(0.01)
    assert got == "frame"
    assert pump.failures == 0, "a success clears the failure run"


def test_a_fast_grab_comes_back_on_the_same_tick() -> None:
    pump = OffLoopGrab(lambda: 42, timeout_s=1.0, clock=FakeClock())
    assert pump.poll() == 42
    assert pump.poll() == 42
    assert pump.failures == 0
    assert pump.dead is False


def test_a_raising_grab_is_counted_and_never_escapes() -> None:
    pump = OffLoopGrab(
        lambda: (_ for _ in ()).throw(RuntimeError("duplication gone")),
        timeout_s=1.0,
        max_failures=3,
        clock=FakeClock(),
    )
    assert pump.poll() is None
    assert pump.failures == 1
    assert "duplication gone" in pump.last_reason
    assert pump.poll() is None
    assert pump.dead is False
    assert pump.poll() is None
    assert pump.dead is True, "a run of failures must give up on the device"
    # Dead means dead: no more worker threads, no more work.
    calls = []
    pump._work = lambda: calls.append(1)
    assert pump.poll() is None
    assert calls == []


def test_the_failure_run_is_a_sliding_window_not_a_lifetime_total() -> None:
    """Ten bad grabs across an afternoon is a flaky display; ten in a minute is
    a display that is gone. Only the second should disable capture."""
    clock = FakeClock()
    pump = OffLoopGrab(
        lambda: (_ for _ in ()).throw(RuntimeError("nope")),
        timeout_s=1.0,
        window_s=60.0,
        max_failures=3,
        clock=clock,
    )
    for _ in range(5):
        pump.poll()
        clock.t += 120.0  # each failure lands in its own window
        assert pump.dead is False
    for _ in range(3):
        pump.poll()
    assert pump.dead is True


def test_the_grab_worker_is_a_daemon_so_shutdown_is_never_held_up() -> None:
    release = threading.Event()
    pump = OffLoopGrab(lambda: release.wait(30.0), timeout_s=0.05, clock=FakeClock())
    try:
        pump.poll()
        assert pump._thread is not None
        assert pump._thread.daemon is True
    finally:
        release.set()


# --- damage-driven threat detection -------------------------------------------
#
# Observed live, and the reason `DamageTracker` exists: a pedestrian walked up
# on the freeway and beat the agent to death while he stood there. His recorded
# thought at the moment: "Something hostile nearby—cat, weird—but no objective
# blip yet. Hold position." The reflex keyed only off
# `nearby.peds[].relationship == "hostile"`, and a ped that simply starts
# swinging is normally still `neutral` in the snapshot — that field is the
# engine's relationship GROUP, not "is currently hitting me".
#
# States below are constructed from CONTRACTS §1's documented /state shape
# (`make_state` above writes every field out explicitly); no invented
# recordings, per CLAUDE.md rule 1.


def _neutral(distance: float, handle: int = 11) -> dict:
    return {
        "handle": handle,
        "model": "a_m_y_skater_01",
        "distance": distance,
        "relationship": "neutral",
    }


def _friendly(distance: float, handle: int = 12) -> dict:
    return {
        "handle": handle,
        "model": "ig_lamardavis",
        "distance": distance,
        "relationship": "friendly",
    }


def test_damage_tracker_sees_a_beating_the_per_tick_signal_misses() -> None:
    """The exact hole: `Delta.big_health_drop` needs >= 25 HP between two
    snapshots. Fists arrive a few HP at a time, so it never fired once.

    Policy change 2026-09-02, from live feedback ("why can't he fight back the
    moment he is punched"): the bar is now ONE clean punch, not three. The
    first reading is always False - it only establishes the baseline peak, and
    no damage has been observed yet - but the very next punch must register.
    """
    clock = FakeClock()
    tracker = DamageTracker(clock=clock)
    health = 200
    fired = []
    for _ in range(4):
        state = make_state(health=health)
        fired.append(tracker.feed(state))
        clock.t += 0.3
        health -= 5  # a punch, well under Delta's 25 HP per-tick bar

    assert fired[0] is False, "the first sample is the baseline; nothing lost yet"
    assert fired[1] is True, "one punch must be enough to call it an attack"
    assert fired[-1] is True
    assert tracker.lost_hp >= DAMAGE_ATTACK_HP


def test_damage_below_the_bar_is_still_ignored() -> None:
    """The bar came down to one punch, not to zero. Scrapes, a kerb, a shove
    that costs a couple of HP must not start a fistfight in the street."""
    clock = FakeClock()
    tracker = DamageTracker(clock=clock)
    assert tracker.feed(make_state(health=200)) is False
    clock.t += 0.5
    assert tracker.feed(make_state(health=198)) is False, "2 HP is noise, not an attack"
    assert tracker.lost_hp < DAMAGE_ATTACK_HP


def test_damage_tracker_counts_armor_as_effective_hp() -> None:
    """Armor absorbs damage first in GTA V, so `health` alone is flat while an
    armoured the agent is being shot."""
    clock = FakeClock()
    tracker = DamageTracker(clock=clock)
    assert tracker.feed(make_state(health=200, armor=100)) is False
    clock.t += 1.0
    assert tracker.feed(make_state(health=200, armor=80)) is True
    assert tracker.lost_hp == 20.0


def test_damage_tracker_ignores_health_coming_back() -> None:
    """Regeneration only raises the number; peak-to-now must not read that as
    damage, and armor pickups must not either."""
    clock = FakeClock()
    tracker = DamageTracker(clock=clock)
    tracker.feed(make_state(health=150))
    clock.t += 1.0
    assert tracker.feed(make_state(health=170)) is False
    clock.t += 1.0
    assert tracker.feed(make_state(health=200, armor=50)) is False
    assert tracker.lost_hp == 0.0


def test_damage_tracker_forgets_damage_older_than_the_window() -> None:
    clock = FakeClock()
    tracker = DamageTracker(clock=clock)
    tracker.feed(make_state(health=200))
    clock.t += 0.5
    assert tracker.feed(make_state(health=170)) is True
    # Nothing further happens for longer than the window: the 30 HP ages out.
    clock.t += DAMAGE_WINDOW_S + 1.0
    assert tracker.feed(make_state(health=170)) is False


def test_damage_tracker_reset_forgets_a_death() -> None:
    """A death is a 200 HP drop and the respawn a 200 HP jump; carried across,
    either would have him come back swinging at nobody."""
    clock = FakeClock()
    tracker = DamageTracker(clock=clock)
    tracker.feed(make_state(health=200))
    clock.t += 0.5
    assert tracker.feed(make_state(health=0, dead=True)) is True
    tracker.reset()
    clock.t += 0.5
    assert tracker.feed(make_state(health=200)) is False


def test_threat_action_fights_a_neutral_attacker_when_health_is_falling() -> None:
    """The live bug, closed: health dropping with ONLY `neutral` peds nearby
    must still produce a combat action."""
    state = make_state(health=185, in_vehicle=False, nearby_peds=[_neutral(2.0)])
    assert threat_action(state, NO_DANGER_DELTA, True) == {
        "type": "combat_hated_targets_around",
        "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M},
    }
    # ...and without the damage evidence the same snapshot is peaceful.
    assert threat_action(state, NO_DANGER_DELTA, False) is None


def test_threat_action_prefers_the_v1_11_attacker_handle_over_the_damage_heuristic() -> None:
    """The headline retaliation fix. `threat.attacker_handle` is target-explicit
    and needs no `nearby.peds`/relationship evidence at all — the carjack
    victim who punched him to death on stream could plausibly still be
    `neutral`/`friendly`, exactly the case `combat_hated_targets_around`
    silently no-ops on."""
    state = make_state(health=185, in_vehicle=False, threat={"attacker_handle": 9012, "being_jacked_by": None})
    assert threat_action(state, NO_DANGER_DELTA, False) == {
        "type": "fight_ped",
        "params": {"handle": 9012},
    }


def test_threat_action_fights_the_jacker_when_no_attacker_is_named() -> None:
    state = make_state(health=185, in_vehicle=False, threat={"attacker_handle": None, "being_jacked_by": 555})
    assert threat_action(state, NO_DANGER_DELTA, False) == {
        "type": "fight_ped",
        "params": {"handle": 555},
    }


def test_threat_action_never_posts_fight_ped_from_a_working_car() -> None:
    """The v1.11 hard rule: leaving beats fighting from a car that can drive
    away, exactly as it already did for the pre-v1.11 heuristic."""
    vehicle = {
        "handle": 1, "model": "adder", "display_name": "Adder", "class": "Super",
        "speed": 0.0, "health": 900.0, "upside_down": False, "in_water": False,
        "stopped_for_s": 3.0,
    }
    state = make_state(
        health=185, in_vehicle=True, vehicle=vehicle,
        threat={"attacker_handle": 9012, "being_jacked_by": None},
    )
    result = threat_action(state, NO_DANGER_DELTA, False)
    assert result == {"type": "wander_drive", "params": {"style": "avoid_traffic"}}


def test_threat_action_falls_back_to_the_damage_heuristic_on_a_pre_v1_11_bridge() -> None:
    """`threat.attacker_handle`/`being_jacked_by` both `None` (a pre-v1.11
    bridge, or simply nobody attacking by handle) must not change a single
    existing rung: DamageTracker's `under_attack` still drives
    `combat_hated_targets_around`."""
    state = make_state(health=185, in_vehicle=False, nearby_peds=[_neutral(2.0)])
    assert threat_action(state, NO_DANGER_DELTA, True) == {
        "type": "combat_hated_targets_around",
        "params": {"radius_m": HOSTILE_CLOSE_RADIUS_M},
    }


def test_threat_action_breaks_contact_when_nothing_is_in_reach_to_hit() -> None:
    """Taking hits from something he cannot reach (a rifle, a fire, a fall) is
    a cover problem, not a combat one."""
    state = make_state(
        health=185, in_vehicle=False, nearby_peds=[_neutral(ATTACKER_CLOSE_RADIUS_M + 5.0)]
    )
    assert threat_action(state, NO_DANGER_DELTA, True) == {
        "type": "seek_cover",
        "params": {"duration_s": 10},
    }


def test_threat_action_does_not_treat_a_crewmate_as_the_attacker() -> None:
    """`friendly` (CONTRACTS v1.5) is the engine's own Companion/Like/Respect
    group — the one ped that is definitely not the one hitting him."""
    state = make_state(health=185, in_vehicle=False, nearby_peds=[_friendly(1.5)])
    assert threat_action(state, NO_DANGER_DELTA, True) == {
        "type": "seek_cover",
        "params": {"duration_s": 10},
    }


def test_threat_action_breaks_contact_instead_of_trading_hits_when_badly_hurt() -> None:
    """Survival still wins the ladder: low health outranks the new rung."""
    state = make_state(health=40, in_vehicle=False, nearby_peds=[_neutral(2.0)])
    assert threat_action(state, NO_DANGER_DELTA, True) == {
        "type": "seek_cover",
        "params": {"duration_s": 10},
    }


def test_threat_action_does_not_answer_crash_damage_with_combat() -> None:
    """HP lost while driving is overwhelmingly a kerb or a lamppost. Answering
    that by preempting the drive with a combat task is the thrash this module
    exists to prevent, so the melee rung is on-foot only."""
    vehicle = {
        "handle": 1,
        "model": "adder",
        "display_name": "Adder",
        "class": "Super",
        "speed": 22.0,
        "health": 700.0,
        "upside_down": False,
        "in_water": False,
        "stopped_for_s": 0.0,
    }
    state = make_state(
        health=185, in_vehicle=True, vehicle=vehicle, nearby_peds=[_neutral(2.0)]
    )
    assert threat_action(state, NO_DANGER_DELTA, True) is None


def test_threat_action_is_silent_during_a_cutscene() -> None:
    """A scripted beat is the game's wheel, not his (and `_execute_action`
    would refuse the task anyway)."""
    state = make_state(
        cutscene_active=True,
        mission_active=True,
        health=185,
        nearby_peds=[_hostile(2.0), _neutral(1.0)],
        wanted=3,
    )
    assert threat_action(state, Delta(wanted_from=0, wanted_to=3, big_health_drop=True), True) is None


def test_threat_action_is_silent_during_a_protagonist_switch() -> None:
    """v1.11 `player.switch_in_progress`: treated exactly like a cutscene —
    the game owns the camera and the body."""
    state = make_state(
        switch_in_progress=True,
        health=185,
        nearby_peds=[_hostile(2.0)],
        wanted=3,
    )
    assert threat_action(state, Delta(wanted_from=0, wanted_to=3, big_health_drop=True), True) is None


def test_threat_action_is_silent_during_a_mission_retry() -> None:
    """v1.11 `mission.retry_in_flight`: a checkpoint reload is in progress;
    nothing survival-related should post over it."""
    state = make_state(
        retry_in_flight=True,
        health=185,
        nearby_peds=[_hostile(2.0)],
        wanted=3,
    )
    assert threat_action(state, Delta(wanted_from=0, wanted_to=3, big_health_drop=True), True) is None


def test_threat_action_still_does_not_flee_a_mission_firefight_under_attack() -> None:
    """situations.md's rule for a scripted police fight is "fight, don't flee";
    the new damage path must not smuggle a `flee_police` into a mission."""
    state = make_state(wanted=3, mission_active=True, health=185, in_vehicle=False)
    assert threat_action(state, NO_DANGER_DELTA, True) == {
        "type": "seek_cover",
        "params": {"duration_s": 10},
    }


def test_the_same_damage_threat_is_issued_once_over_ten_ticks() -> None:
    """The latch, extended to the damage path: every POST /task preempts the
    running one (CONTRACTS §1), so re-issuing at the poll rate restarts the
    engine's combat task three times a second."""
    clock = FakeClock()
    tracker = DamageTracker(clock=clock)
    latch = ThreatLatch(clock=clock)
    posts: list[dict] = []
    health = 200
    task_status, task_type = "idle", "stop"
    for _ in range(10):
        clock.t += 0.3
        health -= 5
        state = make_state(
            health=health,
            in_vehicle=False,
            nearby_peds=[_neutral(2.0)],
            task_status=task_status,
            task_type=task_type,
        )
        action = threat_action(state, NO_DANGER_DELTA, tracker.feed(state))
        if action is not None and latch.should_issue(action, state):
            posts.append(action)
            latch.issued(action)
            task_status, task_type = "running", action["type"]
    assert len(posts) == 1, f"one threat, one post - got {posts}"
    assert posts[0]["type"] == "combat_hated_targets_around"


# --- TaskStallDetector: a task that runs forever pins him ----------------------
#
# Measured live off /state at 4 s intervals: `combat_hated_targets_around`
# RUNNING for the whole window, `in_vehicle: false`, `player.pos` moved 0.2 m
# in 20 s, health 200 (nothing damaging him), a story mission waiting. Cause:
# a stray CAT is a `nearby.peds[]` entry with `relationship: "hostile"`, so the
# task's own done-check ("no hated targets remain in radius", CONTRACTS §1)
# could never come true.


def _pinned(**over) -> GameState:
    """A snapshot of him standing still with a task RUNNING."""
    over.setdefault("pos", (120.0, -45.0, 30.0))
    over.setdefault("task_status", "running")
    over.setdefault("task_type", "combat_hated_targets_around")
    over.setdefault("task_id", "t-77")
    return make_state(**over)


def test_a_running_task_that_never_moves_him_is_abandoned() -> None:
    clock = FakeClock()
    det = TaskStallDetector(clock=clock)
    assert det.feed(_pinned()) is None  # first sight: the window starts here
    clock.t += STALL_WINDOW_S - 1.0
    assert det.feed(_pinned()) is None  # not yet
    clock.t += 2.0
    assert det.feed(_pinned()) == "combat_hated_targets_around"


def test_the_stall_intervention_is_rate_limited_across_consecutive_ticks() -> None:
    clock = FakeClock()
    det = TaskStallDetector(clock=clock)
    det.feed(_pinned())
    clock.t += STALL_WINDOW_S + 0.1
    assert det.feed(_pinned()) == "combat_hated_targets_around"
    for _ in range(20):
        clock.t += 1.0
        assert det.feed(_pinned()) is None, "one `stop` per episode, not one per tick"
    # Past both the gap and a fresh window, it may act again.
    clock.t += STALL_INTERVENTION_GAP_S + STALL_WINDOW_S
    assert det.feed(_pinned()) == "combat_hated_targets_around"


def test_moving_re_anchors_the_stall_window() -> None:
    clock = FakeClock()
    det = TaskStallDetector(clock=clock)
    det.feed(_pinned(pos=(0.0, 0.0, 0.0)))
    clock.t += STALL_WINDOW_S - 1.0
    # He covered more than STALL_MOVE_M: the window restarts from here.
    assert det.feed(_pinned(pos=(0.0, STALL_MOVE_M + 5.0, 0.0))) is None
    clock.t += 2.0
    assert det.feed(_pinned(pos=(0.0, STALL_MOVE_M + 5.0, 0.0))) is None


def test_jitter_under_the_move_threshold_is_still_a_stall() -> None:
    clock = FakeClock()
    det = TaskStallDetector(clock=clock)
    det.feed(_pinned(pos=(0.0, 0.0, 0.0)))
    fired = []
    for i in range(1, 12):
        clock.t += 2.0
        # 0.2 m of shuffle, the measured number, in alternating directions.
        drift = 0.2 if i % 2 else -0.2
        fired.append(det.feed(_pinned(pos=(drift, 0.0, 0.0))))
    assert fired.count("combat_hated_targets_around") == 1
    assert fired.index("combat_hated_targets_around") == 9, (
        "20 s of 0.2 m shuffle is the measured deadlock, not movement"
    )


@pytest.mark.parametrize("task_type", sorted(STATIONARY_TASK_TYPES))
def test_tasks_that_are_meant_to_hold_still_are_never_called_stalled(task_type: str) -> None:
    """Standing still IS the task for these; a stationary window proves nothing."""
    clock = FakeClock()
    det = TaskStallDetector(clock=clock)
    det.feed(_pinned(task_type=task_type))
    clock.t += STALL_WINDOW_S * 3
    assert det.feed(_pinned(task_type=task_type)) is None


def test_no_stall_intervention_while_down_or_mid_cutscene_or_suspended() -> None:
    for kwargs, extra in (
        ({"dead": True}, {}),
        ({"arrested": True}, {}),
        ({"cutscene_active": True}, {}),
        ({}, {"suspended": True}),
        ({}, {"under_attack": True}),
    ):
        clock = FakeClock()
        det = TaskStallDetector(clock=clock)
        det.feed(_pinned(**kwargs), **extra)
        clock.t += STALL_WINDOW_S * 3
        assert det.feed(_pinned(**kwargs), **extra) is None, f"{kwargs} {extra}"


def test_an_idle_slot_is_not_a_stall() -> None:
    """Standing still with nothing running is just standing still; the day
    plan, the activity runner and the brain all own that case already."""
    clock = FakeClock()
    det = TaskStallDetector(clock=clock)
    det.feed(_pinned(task_status="idle"))
    clock.t += STALL_WINDOW_S * 3
    assert det.feed(_pinned(task_status="idle")) is None


def test_a_new_task_gets_its_own_window() -> None:
    clock = FakeClock()
    det = TaskStallDetector(clock=clock)
    det.feed(_pinned(task_id="t-1"))
    clock.t += STALL_WINDOW_S - 1.0
    # A different task started; it has not had its own 20 s yet.
    assert det.feed(_pinned(task_id="t-2")) is None
    clock.t += 2.0
    assert det.feed(_pinned(task_id="t-2")) is None
    clock.t += STALL_WINDOW_S
    assert det.feed(_pinned(task_id="t-2")) == "combat_hated_targets_around"


def test_the_type_that_deadlocked_him_is_refused_for_a_while() -> None:
    clock = FakeClock()
    det = TaskStallDetector(clock=clock)
    assert det.blocked("combat_hated_targets_around") is False
    det.feed(_pinned())
    clock.t += STALL_WINDOW_S + 0.1
    det.feed(_pinned())
    assert det.blocked("combat_hated_targets_around") is True
    assert det.blocked("drive_to") is False, "only the type that stalled"
    assert det.blocked("stop") is False, "`stop` is how this detector gets out"
    clock.t += STALL_TYPE_BLOCK_S + 0.1
    assert det.blocked("combat_hated_targets_around") is False


def test_the_stall_detector_leaves_the_vehicle_stuck_ladder_alone() -> None:
    """They cover different failures and must both still fire: StuckDetector
    needs `state.vehicle` and a driving task (so an on-foot pin is invisible to
    it), and answers by moving the car, not by abandoning the task."""
    vehicle = {
        "handle": 1,
        "model": "adder",
        "display_name": "Adder",
        "class": "Super",
        "speed": 0.0,
        "health": 900.0,
        "upside_down": False,
        "in_water": False,
        "stopped_for_s": 30.0,
    }
    clock = FakeClock()
    stuck = StuckDetector(clock=clock)
    stall = TaskStallDetector(clock=clock)
    wedged = {
        "in_vehicle": True,
        "vehicle": vehicle,
        "task_type": "drive_to",
        "task_status": "running",
    }
    assert stuck.check(_pinned(**wedged)) == "reverse_out"
    stall.feed(_pinned(**wedged))
    clock.t += STALL_WINDOW_S + 0.1
    assert stall.feed(_pinned(**wedged)) == "drive_to"
    # ...and the vehicle ladder is exactly where it was: still escalating on
    # its own cooldowns, unchanged by anything above.
    assert stuck.check(_pinned(**wedged)) == "unstick"


# --- BlockingScreenWatchdog: the standing "we are on a modal screen" answer ----


def test_the_watchdog_reports_blocked_only_once_the_stall_passes_the_timeout() -> None:
    clock = FakeClock()
    wd = BlockingScreenWatchdog(clock=clock)
    wd.feed(make_state(tick=500))
    assert wd.blocked is False
    clock.t += SCRIPT_STALL_TIMEOUT_S - 1.0
    wd.feed(make_state(tick=500))
    assert wd.blocked is False, "a poll landing inside one game frame is normal"
    clock.t += 2.0
    wd.feed(make_state(tick=500))
    assert wd.blocked is True
    # The script thread comes back: no longer blocked.
    clock.t += 1.0
    wd.feed(make_state(tick=501))
    assert wd.blocked is False


def test_a_cutscene_is_never_reported_as_a_blocking_screen() -> None:
    clock = FakeClock()
    wd = BlockingScreenWatchdog(clock=clock)
    wd.feed(make_state(tick=500, cutscene_active=True))
    clock.t += SCRIPT_STALL_TIMEOUT_S * 3
    assert wd.feed(make_state(tick=500, cutscene_active=True)) is None
    assert wd.blocked is False


def test_the_watchdog_stays_blocked_after_it_gives_up_pressing_keys() -> None:
    """Giving up on the keypresses does not un-block the screen, and the rest
    of the harness still has to know the snapshot is frozen."""
    clock = FakeClock()
    wd = BlockingScreenWatchdog(clock=clock)
    wd.feed(make_state(tick=500))
    pressed = []
    for _ in range(40):
        clock.t += 2.0
        key = wd.feed(make_state(tick=500))
        if key is not None:
            pressed.append(key)
    assert len(pressed) == MAX_STALL_RECOVERY_ATTEMPTS
    assert wd.blocked is True


def test_the_watchdog_never_presses_tab() -> None:
    """Both wordings of the failure screen put a destructive action on Tab
    (`Skip [Tab]` in one, `Restart [Tab]` in the other) and the non-destructive
    one on Enter. Tab must never be sent."""
    assert "tab" not in {k.lower() for k in BLOCKING_SCREEN_KEYS}
    assert BLOCKING_SCREEN_KEYS[0] == "enter"


# -- ClearedByGameBackoff --------------------------------------------------------
# Measured live 2026-09-03: `enter_nearest_vehicle` started, `failed: cleared_by_game`
# ~1 s later, and was re-posted within 300 ms by whichever owner got the wheel next,
# with an empty car 2.8 m away, for minutes. The game clears tasks for reasons the
# harness cannot see (a ringing phone, a scripted moment). Backing off is the only
# honest response.


def _task_state(task_id: str, ttype: str, status: str, detail: str = "") -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(last_task=SimpleNamespace(id=task_id, type=ttype, status=status, detail=detail))


def test_a_game_cleared_task_is_refused_for_a_while() -> None:
    from wasted_harness.behavior.recovery import CLEARED_BACKOFF_FIRST_S, ClearedByGameBackoff

    clock = FakeClock()
    b = ClearedByGameBackoff(clock=clock)
    assert b.refuses("enter_nearest_vehicle") == 0.0
    assert b.feed(_task_state("t1", "enter_nearest_vehicle", "failed", "cleared_by_game")) == "enter_nearest_vehicle"
    assert b.refuses("enter_nearest_vehicle") > 0.0, "the type the game just cleared must wait"
    assert b.refuses("walk_to") == 0.0, "a DIFFERENT type is not blocked — that is the variety he needs"
    clock.t += CLEARED_BACKOFF_FIRST_S + 0.1
    assert b.refuses("enter_nearest_vehicle") == 0.0


def test_repeated_clears_back_off_longer_but_are_capped() -> None:
    from wasted_harness.behavior.recovery import CLEARED_BACKOFF_MAX_S, ClearedByGameBackoff

    clock = FakeClock()
    b = ClearedByGameBackoff(clock=clock)
    waits = []
    for i in range(6):
        b.feed(_task_state(f"t{i}", "enter_nearest_vehicle", "failed", "cleared_by_game"))
        waits.append(b.refuses("enter_nearest_vehicle"))
        clock.t += 0.5
    assert waits[1] > waits[0], "a repeat backs off longer"
    assert max(waits) <= CLEARED_BACKOFF_MAX_S + 0.01, "but never past the cap"


def test_the_same_failure_is_not_counted_twice_across_ticks() -> None:
    from wasted_harness.behavior.recovery import ClearedByGameBackoff

    clock = FakeClock()
    b = ClearedByGameBackoff(clock=clock)
    same = _task_state("t1", "enter_nearest_vehicle", "failed", "cleared_by_game")
    assert b.feed(same) == "enter_nearest_vehicle"
    assert b.feed(same) is None, "one task id, one strike — /state repeats the same row every tick"


def test_other_failures_do_not_trigger_the_backoff() -> None:
    from wasted_harness.behavior.recovery import ClearedByGameBackoff

    b = ClearedByGameBackoff(clock=FakeClock())
    assert b.feed(_task_state("t1", "drive_to", "failed", "timeout")) is None
    assert b.refuses("drive_to") == 0.0, "a timeout is the stall detector's business, not this one's"


def test_the_stars_only_rung_stands_down_when_the_goal_wants_the_heat() -> None:
    """Soak finding (2026-09-03): `earn_two_stars` fired its drive-by, got its star,
    and this rung fled on the next poll — preempting the goal that wanted it. Only
    the wanted-alone rung yields; being hit still gets the fight/leave rungs."""
    calm = Delta(wanted_from=0, wanted_to=1, big_health_drop=False)
    one_star = make_state(in_vehicle=True, wanted=1, health=200)
    assert threat_action(one_star, calm, False) == {"type": "flee_police", "params": {}}
    assert threat_action(one_star, calm, False, heat_wanted=True) is None
    hurt = Delta(wanted_from=1, wanted_to=1, big_health_drop=True)
    assert threat_action(one_star, hurt, True, heat_wanted=True) is not None


# --- going nowhere: moving, but arriving nowhere ----------------------------------


def test_pacing_reads_as_moving_to_still_for_s_but_not_to_going_nowhere_s() -> None:
    """The 2026-09-04 feed bug, in a test.

    Eight metres is more than IDLE_MOVE_M, so every leg of this pace re-anchors
    `still_for_s` to zero and the old commentary gate never fired — he narrated
    a line per poll while covering no ground at all.
    """
    clock = FakeClock()
    breaker = IdleBreaker(clock=clock)
    for step in range(40):  # 40 s of pacing between x=0 and x=8
        breaker.observe(make_state(pos=(0.0 if step % 2 else 8.0, 0.0, 0.0)))
        clock.t += 1.0

    assert breaker.still_for_s() < 5.0, "every leg re-anchors it; that is the bug"
    assert breaker.going_nowhere_s() >= 30.0, "but he has been in one circle throughout"


def test_going_nowhere_s_resets_once_he_actually_travels() -> None:
    clock = FakeClock()
    breaker = IdleBreaker(clock=clock)
    for _ in range(40):
        breaker.observe(make_state(pos=(0.0, 0.0, 0.0)))
        clock.t += 1.0
    assert breaker.going_nowhere_s() >= 30.0

    clock.t += 1.0
    breaker.observe(make_state(pos=(400.0, 0.0, 0.0)))  # drove off
    assert breaker.going_nowhere_s() == 0.0


def test_going_nowhere_s_is_zero_before_anything_is_observed() -> None:
    assert IdleBreaker(clock=FakeClock()).going_nowhere_s() == 0.0
