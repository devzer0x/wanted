"""Recovery reflexes: game restart, API backoff causes, stranded escalation.

Game states here are constructed pydantic objects (pure-function inputs), not
recorded sessions — the fixtures directory stays empty by design.
"""

import random

import pytest

from wasted_harness.behavior.recovery import (
    BACKOFF_FLOOR_S,
    STRANDED_RADII_M,
    ApiBackoff,
    BridgeStallTracker,
    GameRestartDetector,
    StrandedEscalator,
    StuckDetector,
    classify_api_failure,
    flipped_action,
)
from wasted_harness.bridge_client import BridgeApiError, BridgeTransientError, GameState


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
            "pos": {"x": 0.0, "y": 0.0, "z": 0.0},
            "heading": 0.0,
            "health": 200,
            "max_health": 200,
            "armor": 0,
            "wanted": 0,
            "cash": 0,
            "dead": False,
            "arrested": False,
            "in_vehicle": over.pop("in_vehicle", False),
            "control_enabled": True,
        },
        "vehicle": over.pop("vehicle", None),
        "location": {"street": "Vinewood Blvd", "zone": "Downtown Vinewood"},
        "world": {"clock": "13:45", "weather": "CLEAR", "timescale": 1.0},
        "mission": {"active": False, "random_event_active": False, "cutscene_active": False},
        "nearby": {"vehicles": over.pop("nearby_vehicles", []), "peds": []},
        "last_task": {
            "id": over.pop("task_id", "t-1"),
            "type": over.pop("task_type", "stop"),
            "status": over.pop("task_status", "idle"),
            "detail": "",
        },
        "bridge": {"version": over.pop("bridge_version", "1.0.0"), "edition": "legacy"},
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
    assert classify_api_failure(ValueError("bad json")) == "other"
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
    assert classify_api_failure(a) == "other"


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
