"""The supervised loop must survive a bridge that is up but not ready.

`not_ready` and `game_thread_stalled` (CONTRACTS v1.2) are the *normal* answers
for the whole of a game launch or a loading screen. Before this, `/state`
returning one of them raised a `BridgeApiError` that nothing in `run()` caught:
the harness would exit on startup, every time, the moment the bridge came up
before the game did.

These tests run the real `Harness.run` — not a re-description of it — with the
collaborators it touches replaced by recorders, and a bridge that raises the
real exception the real client would raise for those bodies.
"""

from __future__ import annotations

import random
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from support import throwaway_totals

from wasted_harness.behavior.activities import ActivityPicker, ActivityRunner
from wasted_harness.behavior.humanizer import BreakScheduler, IdlePicker, MoodModel
from wasted_harness.behavior.missions import MissionFollower, MissionTracker
from wasted_harness.behavior.planner import DayPlanner
from wasted_harness.behavior.recovery import (
    BLOCKING_SCREEN_KEYS,
    ApiBackoff,
    BlockingScreenWatchdog,
    BridgeDownTracker,
    BridgeStallTracker,
    ClearedByGameBackoff,
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
)
from wasted_harness.behavior.roam import HouseEscape, InteriorEscape, RoamEngine
from wasted_harness.behavior.vehicle import (
    ControlRegained,
    MovementWheel,
    VehicleController,
)
from wasted_harness.brain.director import DirectorCadence
from wasted_harness.brain.tactical import DecisionFailedError, TacticalCadence
from wasted_harness.bridge_client import (
    BridgeApiError,
    BridgeDownError,
    BridgeTransientError,
    GameState,
)
from wasted_harness.main import FRAME_MAX_AGE_S, THINKING_TIMESCALE, Harness
from wasted_harness.perception import Delta, Perceptor, ScreenshotUnavailableError


class _Recorder:
    """Absorbs bookkeeping calls; records the ones the tests assert on."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.stats: list[dict[str, Any]] = []
        self.session_ends: list[tuple[str, str]] = []

    def record_event(self, type_: str, payload: dict[str, Any], **_kw: Any) -> None:
        self.events.append((type_, payload))

    def upsert_stats(self, row: dict[str, Any]) -> None:
        self.stats.append(row)

    def update_session_end(self, session_id: str, ended_at: str) -> None:
        self.session_ends.append((session_id, ended_at))

    def insert_mission(self, row: dict[str, Any]) -> None:
        pass

    def flush(self) -> None:
        pass


class _Governor:
    level = 0

    def total_usd(self) -> float:
        return 0.0

    def day_usd(self) -> float:
        return 0.0

    def cost_per_hour_usd(self) -> float:
        return 0.0

    def snapshot(self) -> dict[str, Any]:
        return {"cost_day": "2026-09-02", "cost_day_usd": 0.0, "recent_spend": []}


class _Settings:
    poll_hz = 20.0  # fast, so the test is quick; the loop honours the backoff anyway
    #: Shutdown clears state/current_session.json so post-mortem watchdog events
    #: stop being filed under a dead session; the real directory is created here
    #: rather than stubbed, because the unlink is the behaviour under test.
    state_dir = Path(tempfile.mkdtemp(prefix="wasted-state-"))


def _harness(bridge: Any) -> Harness:
    """A real Harness object with only the loop's collaborators supplied.

    `object.__new__` skips __init__ deliberately: __init__ demands a live
    Anthropic key, a pricing file and a bridge. The code under test is `run()`.
    """
    h = Harness.__new__(Harness)
    h.settings = _Settings()
    h.bridge = bridge
    h.writer = _Recorder()
    h.bus = _Recorder()
    h.bus.publish = lambda *a, **k: None
    h.governor = _Governor()
    h.bridge_down = BridgeDownTracker()
    h.bridge_stall = BridgeStallTracker()
    h.clips = None
    # Real lifetime totals in a throwaway dir — the counters and the played-time
    # accumulator are production code, not scaffolding.
    h.totals = throwaway_totals()
    h.counters = h.totals.counters()
    h.current_goal = "test"
    # T5 (findings.md R3): `goal_text`/`_validation_context` read this now.
    h.current_mission = None
    # T3: `_apply_decision` reads this; unset only means "no roam transition
    # was drained this tick", the correct default for a harness that never
    # called `_dynamic_context`.
    h._tick_roam_transition = False
    h.session_id = "test-session"
    h._stop = threading.Event()
    h._last_stats = 0.0
    h._last_flush = 0.0
    h._started = time.monotonic()
    h._last_live_state_at = 0.0
    h._mission_started_iso = None
    h._mission_tokens = 0
    # Read by `_heartbeat` on every path (a frozen snapshot is not publishable),
    # so the minimal harness needs it too, not just the full-tick one.
    h._screen_blocked = False
    # Startup side effects the loop does before polling; not under test here.
    h._start_overlay = lambda: None
    h._start_clips = lambda: None
    h._write_session_start = lambda: None
    h._end_activity_if_running = lambda outcome: None
    return h


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """`run()` installs handlers; give pytest its own back afterwards."""
    saved = {
        name: signal.getsignal(getattr(signal, name))
        for name in ("SIGTERM", "SIGINT", "SIGBREAK")
        if hasattr(signal, name)
    }
    yield
    for name, handler in saved.items():
        signal.signal(getattr(signal, name), handler)


class _StallingBridge:
    """Raises exactly what BridgeClient raises for the bridge's recorded 503s."""

    def __init__(self, code: str, times: int, stop: threading.Event | None = None) -> None:
        self.code = code
        self.times = times
        self.calls = 0
        self.stop = stop
        self.closed = False

    def get_state(self):
        self.calls += 1
        if self.calls >= self.times and self.stop is not None:
            self.stop.set()
        raise BridgeTransientError(503, self.code, "the game thread is not ticking")

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("code", ["not_ready", "game_thread_stalled", "queue_full"])
def test_a_transient_503_does_not_end_the_run(code: str) -> None:
    bridge = _StallingBridge(code, times=6)
    h = _harness(bridge)
    bridge.stop = h._stop
    assert h.run() == 0, f"{code} ended the run"
    assert bridge.calls >= 6, "the loop stopped polling"
    assert bridge.closed, "the bridge was not closed on the way out"
    # It is a stall, not an outage: no bridge_down claimed on the site.
    assert not [e for e, _ in h.writer.events if e == "bridge_down"]
    assert h.bridge_stall.consecutive >= 6
    # And the heartbeat is WITHHELD for the whole stall. `stats.heartbeat_at` is
    # the site's only ON AIR signal (CONTRACTS §5); the bridge answering while
    # the game has no snapshot is precisely the case where the harness cannot
    # vouch for the game running, and it used to publish a fresh timestamp next
    # to an empty `hud` — "ON THE AIR" over "No telemetry on record." The row
    # simply ages out and the site's own 60 s threshold does the rest.
    assert h.writer.stats == [], "a stall must not be published as being on the air"


def test_an_unknown_error_code_is_also_survived() -> None:
    """v1.2: consumers tolerate codes they do not know, they do not crash."""
    bridge = _StallingBridge("warp_core_breach", times=4)
    h = _harness(bridge)
    bridge.stop = h._stop
    assert h.run() == 0
    assert bridge.calls >= 4


def test_a_non_transient_bridge_error_is_loud_but_still_not_fatal() -> None:
    class _BadBridge(_StallingBridge):
        def get_state(self):
            self.calls += 1
            if self.calls >= 3:
                self.stop.set()
            raise BridgeApiError(400, "invalid_json", "not json")

    bridge = _BadBridge("invalid_json", times=3)
    h = _harness(bridge)
    bridge.stop = h._stop
    assert h.run() == 0
    assert bridge.calls >= 3


def test_connection_refused_still_reports_bridge_down() -> None:
    """The stall path must not have swallowed the real outage path."""

    class _DeadBridge(_StallingBridge):
        def get_state(self):
            self.calls += 1
            if self.calls >= 5:
                self.stop.set()
            raise BridgeDownError("bridge unreachable at http://127.0.0.1:7777/state")

    bridge = _DeadBridge("down", times=5)
    h = _harness(bridge)
    bridge.stop = h._stop
    assert h.run() == 0
    assert [e for e, _ in h.writer.events if e == "bridge_down"], (
        "a genuinely unreachable bridge must still raise bridge_down"
    )


# --- a full tick, with the collaborators the loop actually touches -------------
#
# Everything above exercises the paths that `continue` before the loop body. The
# fixes below (timescale guard, off-loop screen grab) live IN the body, so these
# need a harness that gets all the way through a tick.


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        self.published.append((kind, payload))


class _Absorber:
    """Absorbs the bookkeeping calls a tick makes (memory, commentary)."""

    def __getattr__(self, _name: str):
        return _Absorber()

    def __call__(self, *a: Any, **k: Any):
        return None


class _TickBridge:
    """A bridge that answers /state forever and records what was posted to it."""

    def __init__(self, state_body: dict[str, Any], stop_after: int) -> None:
        self.state_body = state_body
        self.stop_after = stop_after
        self.calls = 0
        self.timescales: list[float] = []
        self.tasks: list[tuple[str, dict[str, Any]]] = []
        self.stop: threading.Event | None = None
        self.closed = False

    def get_state(self):
        self.calls += 1
        if self.calls >= self.stop_after and self.stop is not None:
            self.stop.set()
        body = dict(self.state_body)
        body["tick"] = 1000 + self.calls
        return GameState.model_validate(body)

    def set_timescale(self, value: float) -> float:
        self.timescales.append(value)
        return value

    def post_task(self, type_: str, params: dict[str, Any]) -> str:
        self.tasks.append((type_, dict(params)))
        return f"t-{len(self.tasks)}"

    def set_radio(self, station: str) -> None:
        pass

    def horn(self, ms: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _state_body(**over: Any) -> dict[str, Any]:
    """Contract-shaped /state (CONTRACTS §1), same explicit-builder style as
    test_recovery.py — no invented recordings, every field written out."""
    return {
        "ts": "2026-09-01T17:14:10.000Z",
        "tick": 1000,
        "player": {
            "pos": {"x": 0.0, "y": 0.0, "z": 0.0},
            "heading": 0.0,
            "health": 200,
            "max_health": 200,
            "armor": 0,
            "wanted": 0,
            "cash": 0,
            "dead": over.pop("dead", False),
            "arrested": False,
            "in_vehicle": False,
            "control_enabled": True,
        },
        "vehicle": None,
        "location": {"street": "Vinewood Blvd", "zone": "Downtown Vinewood"},
        "world": {
            "clock": "13:45",
            "weather": "CLEAR",
            "timescale": over.pop("timescale", 1.0),
        },
        "mission": {
            "active": False,
            "random_event_active": False,
            "cutscene_active": over.pop("cutscene_active", False),
        },
        "nearby": {"vehicles": [], "peds": []},
        "last_task": {"id": None, "type": None, "status": "idle", "detail": ""},
        "bridge": {"version": "1.0.0", "edition": "legacy"},
    }


def _tick_harness(bridge: Any, grabber: Any = None) -> Harness:
    """A real Harness wired with the real behaviour objects a tick uses.

    Only the three things a dev machine cannot have are stood in for: the
    Anthropic client (the API backoff is held blocked, so `_think` returns
    before it would ever be used), Supabase, and SendInput.
    """
    rng = random.Random(7)
    h = _harness(bridge)
    h.rng = rng
    h.perceptor = Perceptor()
    h.mood = MoodModel(rng=rng)
    h.idle = IdlePicker(rng)
    h.breaks = BreakScheduler(rng=rng)
    h.activities = ActivityPicker(rng)
    h.activity_runner = ActivityRunner(h.activities, rng)
    h.missions = MissionTracker()
    h.mission_follower = MissionFollower()
    # `_reflex` and `_drive_day_plan` both consult the day plan now (it gates
    # the stranded escalator and the L2 wander reflex), so the real one is
    # wired here rather than stood in for — it makes no network calls.
    h.planner = DayPlanner(rng)
    # The real roam engine: free roam's single owner. `_handle_mission_events`
    # resets its "the story has to move" clock and `_reflex` reads its
    # standing-still measure, so stubbing it out would stop testing exactly the
    # machinery that keeps him from standing there.
    h.roam = RoamEngine(rng)
    h.house_escape = HouseEscape()
    # CONTRACTS v1.12: `_reflex` feeds the ground-truth interior escape on every
    # tick (that is the whole reachability fix), so it is a production
    # collaborator here too, not scaffolding.
    h.interior_escape = InteriorEscape()
    h._interior_token = None
    h.stuck = StuckDetector()
    h.task_stall = TaskStallDetector()
    h.cleared_backoff = ClearedByGameBackoff()
    h.idle_breaker = IdleBreaker()
    h.stranded = StrandedEscalator()
    # T9 (findings.md R1/R5): production collaborators of a tick too — `_reflex`
    # feeds all three every tick.
    h.water = WaterEscalator()
    h.road_dodge = RoadDodge()
    h.jack_handoff = JackHandoffGate()
    h.threat_latch = ThreatLatch()
    h.damage = DamageTracker()
    # The vehicle-entry machine and the movement arbiter are production
    # collaborators of a tick, not scaffolding: `_reflex` feeds both.
    h.vehicle = VehicleController()
    h.wheel = MovementWheel()
    # F6's stopwatch: `_reflex` feeds it every tick and `_execute_action` stops
    # it on the line that posts, so a loop tick cannot run without one.
    h.control_regained = ControlRegained()
    h.death_recovery = DeathArrestRecovery()
    h.blocking_screen_watchdog = BlockingScreenWatchdog()
    h.restart = GameRestartDetector()
    h.tactical_cadence = TacticalCadence(rng)
    h.director_cadence = DirectorCadence(rng)
    h.api_backoff = ApiBackoff(rng=rng)
    # Held blocked for the whole test: no API key here, and `_think` checks
    # `ready()` before anything else, so the brain is never reached.
    h.api_backoff.record_failure("rate_limit")
    h.memory = _Absorber()
    h.commentary = _Absorber()
    h.primitives = None
    h.grabber = grabber
    h.grab_pump = OffLoopGrab(h._grab_frame_and_hash, name="test-grab")
    h._frame = None
    h._frame_at = 0.0
    h._grab_reset_wanted = False
    h._pending_big_event = None
    h._pending_screenshot_trigger = None
    h._pending_park = False
    h._park_task_id = None
    h._park_deadline = 0.0
    h._quiet_until = 0.0
    h._cutscene_active = False
    h._switch_in_progress = False
    h._retry_in_flight = False
    h._player_down = False
    h._mission_active = False
    h._wanted_now = 0
    h._threat_has_the_wheel = False
    h._screen_blocked = False
    h._thinking_dip_active = False
    h._last_timescale_reassert = 0.0
    h.bus = _Bus()
    return h


# --- A: the world must never be left in slow motion ---------------------------


def test_a_stuck_timescale_is_re_asserted_and_rate_limited() -> None:
    """`_think` dipped the world to 0.15x, the restore POST came back 503, and
    the log said "watchdog will catch it" — there was no watchdog. Four such
    restores in one session; the game stayed at 15% speed for minutes."""
    bridge = _TickBridge(_state_body(timescale=0.15), stop_after=12)
    h = _tick_harness(bridge)
    bridge.stop = h._stop
    assert h.run() == 0
    assert bridge.calls >= 12, "the loop stopped ticking"
    assert bridge.timescales == [1.0], (
        f"exactly one re-assert across {bridge.calls} ticks, got {bridge.timescales}"
    )


def test_normal_time_is_left_alone() -> None:
    bridge = _TickBridge(_state_body(timescale=1.0), stop_after=6)
    h = _tick_harness(bridge)
    bridge.stop = h._stop
    assert h.run() == 0
    assert bridge.timescales == [], "nothing to restore; do not POST anyway"


def test_the_guard_does_not_fight_the_game_s_own_slow_motion() -> None:
    """The game slows time for its own death and cutscene sequences. Death sits
    around 0.075 — below the 0.1 the bridge clamps POST /timescale to, so it
    cannot be a value the harness set."""
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = _tick_harness(bridge)

    Harness._guard_timescale(h, GameState.model_validate(_state_body(timescale=0.075)))
    assert bridge.timescales == [], "0.075 is the game's own death effect"

    h._last_timescale_reassert = 0.0
    Harness._guard_timescale(h, GameState.model_validate(_state_body(timescale=0.3, dead=True)))
    assert bridge.timescales == [], "dead: the game is running its own sequence"

    h._last_timescale_reassert = 0.0
    Harness._guard_timescale(
        h, GameState.model_validate(_state_body(timescale=0.3, cutscene_active=True))
    )
    assert bridge.timescales == [], "cutscene: the game owns the clock"

    h._last_timescale_reassert = 0.0
    h._thinking_dip_active = True
    Harness._guard_timescale(h, GameState.model_validate(_state_body(timescale=0.15)))
    assert bridge.timescales == [], "never fight the harness's own deliberate dip"

    # ...and with none of those true, it does its job.
    h._thinking_dip_active = False
    h._last_timescale_reassert = 0.0
    Harness._guard_timescale(h, GameState.model_validate(_state_body(timescale=0.15)))
    assert bridge.timescales == [1.0]


def test_a_failed_re_assert_does_not_kill_the_loop() -> None:
    class _RefusingBridge(_TickBridge):
        def set_timescale(self, value: float) -> float:
            self.timescales.append(value)
            raise BridgeTransientError(503, "game_thread_stalled", "not ticking")

    bridge = _RefusingBridge(_state_body(timescale=0.15), stop_after=8)
    h = _tick_harness(bridge)
    bridge.stop = h._stop
    assert h.run() == 0
    assert bridge.calls >= 8
    assert bridge.timescales == [1.0]


# --- H: no think-time dip at all ----------------------------------------------


class _RaisingBrain:
    """A brain whose call fails exactly the way the real one signals failure:
    `DecisionFailedError` chained (`raise ... from`) off the last real cause."""

    def __init__(self, cause: BaseException) -> None:
        self._cause = cause
        self.calls = 0

    def _attempt(self) -> None:
        raise self._cause

    def decide(self, _context: str, mission_active: bool = False, validation_ctx: Any = None):
        self.calls += 1
        try:
            self._attempt()
        except BaseException as exc:
            raise DecisionFailedError(
                f"tactical decision failed twice; reflex layer keeps control "
                f"(last error: {type(exc).__name__}: {exc})"
            ) from exc


def _thinking_harness(bridge: Any) -> Harness:
    h = _tick_harness(bridge)
    h.api_backoff = ApiBackoff(rng=random.Random(1))  # ready
    h._dynamic_context = lambda *a, **k: "CONTEXT"
    return h


def test_thinking_never_touches_the_timescale() -> None:
    """THINKING_TIMESCALE is 1.0: no dip, no restore, no 503-on-restore path.
    The stream showed the old 0.15 dip as "goes slow for 3-4 seconds then
    normal speed" on every tactical call."""
    assert THINKING_TIMESCALE == 1.0
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = _thinking_harness(bridge)
    h.tactical = _RaisingBrain(ValueError("model returned no parseable decision object"))
    state = GameState.model_validate(_state_body())
    assert Harness._think(h, "tactical", state, Delta(wanted_from=0, wanted_to=0), "test") is None
    assert h.tactical.calls == 1
    assert bridge.timescales == [], f"the world was slowed to think, got {bridge.timescales}"


# --- C/H2: a local schema failure is not an API outage ------------------------


def test_a_schema_rejection_does_not_start_the_outage_backoff() -> None:
    import pydantic

    from wasted_harness.brain.schemas import DecisionModel

    with pytest.raises(pydantic.ValidationError) as caught:
        DecisionModel.model_validate({})  # a REAL rejection from the real model

    bridge = _TickBridge(_state_body(), stop_after=1)
    h = _thinking_harness(bridge)
    h.tactical = _RaisingBrain(caught.value)
    state = GameState.model_validate(_state_body())
    delta = Delta(wanted_from=0, wanted_to=0)

    for _ in range(4):
        assert Harness._think(h, "tactical", state, delta, "test") is None
    assert h.tactical.calls == 4, "the backoff blocked a call the API never refused"
    assert h.api_backoff.failures == 0
    assert h.api_backoff.ready() is True
    assert h.api_backoff.blocked_for_s() == 0.0


def test_a_real_api_failure_still_backs_off() -> None:
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        429,
        request=request,
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
    )
    rate_limited = anthropic.RateLimitError("slow", response=response, body=None)

    bridge = _TickBridge(_state_body(), stop_after=1)
    h = _thinking_harness(bridge)
    h.tactical = _RaisingBrain(rate_limited)
    state = GameState.model_validate(_state_body())
    assert Harness._think(h, "tactical", state, Delta(wanted_from=0, wanted_to=0), "test") is None
    assert h.api_backoff.failures == 1
    assert h.api_backoff.last_cause == "rate_limit"
    assert h.api_backoff.ready() is False


# --- B: a wedged screen grab may not own the loop thread ----------------------


class _SlowGrabber:
    """Stands in for a dxcam device stuck in its own output-recovery loop.

    Not a fake frame source: it produces a REAL PIL image, exactly as
    ScreenGrabber does, just slowly. What is under test is the loop's
    behaviour while the device takes far longer than a tick.
    """

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.grabs = 0
        self.released = threading.Event()

    def grab(self) -> tuple[Image.Image, float]:
        # Same contract as ScreenGrabber.grab(): the frame AND the monotonic
        # time it was captured, so a cached frame can never be passed off as new.
        self.grabs += 1
        self.released.wait(self.delay_s)
        return Image.new("RGB", (1920, 1080), (10, 20, 30)), time.monotonic()

    def reset(self) -> None:
        pass


def test_a_grab_slower_than_the_timeout_does_not_stall_a_tick() -> None:
    grabber = _SlowGrabber(delay_s=30.0)  # the real block was 172 s
    bridge = _TickBridge(_state_body(), stop_after=10)
    h = _tick_harness(bridge, grabber=grabber)
    bridge.stop = h._stop
    try:
        started = time.monotonic()
        assert h.run() == 0
        elapsed = time.monotonic() - started
    finally:
        grabber.released.set()
    assert bridge.calls >= 10, "the loop stopped polling while the grab hung"
    assert elapsed < 10.0, f"ten ticks took {elapsed:.1f}s behind a wedged grab"
    # And it never piled a second grab onto a device that was already busy.
    assert grabber.grabs == 1, f"{grabber.grabs} concurrent grabs of one device"
    # The bridge was answering with real snapshots throughout, so the heartbeat
    # kept beating: a slow screenshot is not a reason to go dark (§5, >60 s).
    assert h.writer.stats


def test_a_wedged_grab_does_not_take_event_screenshots_with_it() -> None:
    """`_capture_screenshot` used to call `grabber.grab()` itself — a second
    unbounded blocking call on the loop thread, and a second concurrent caller
    of a device that allows exactly one."""
    grabber = _SlowGrabber(delay_s=30.0)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = _tick_harness(bridge, grabber=grabber)
    try:
        assert h._pump_screen_grab() is None  # times out, still running
        started = time.monotonic()
        assert Harness._capture_screenshot(h, "death") == (None, None)
        assert time.monotonic() - started < 1.0
        assert grabber.grabs == 1
    finally:
        grabber.released.set()


def test_screen_capture_is_given_up_on_after_a_run_of_failures() -> None:
    class _DeadGrabber:
        def __init__(self) -> None:
            self.grabs = 0

        def grab(self):
            self.grabs += 1
            raise ScreenshotUnavailableError("Desktop Duplication is gone")

        def reset(self) -> None:
            pass

    grabber = _DeadGrabber()
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = _tick_harness(bridge, grabber=grabber)
    h.grab_pump = OffLoopGrab(h._grab_frame_and_hash, timeout_s=2.0, max_failures=3)
    for _ in range(3):
        assert h._pump_screen_grab() is None
    assert h.grabber is None, "capture must be dropped for the session"
    assert h._pump_screen_grab() is None
    assert grabber.grabs == 3, "no further work once capture is given up on"


def test_a_working_grab_feeds_both_the_hash_and_event_screenshots() -> None:
    class _FastGrabber(_SlowGrabber):
        def __init__(self) -> None:
            super().__init__(delay_s=0.0)
            self.released.set()

    grabber = _FastGrabber()
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = _tick_harness(bridge, grabber=grabber)
    h.writer.upload_screenshot = lambda jpeg, hint: f"https://shots/{hint}.jpg"

    objective_hash = None
    deadline = time.monotonic() + 5.0
    while objective_hash is None and time.monotonic() < deadline:
        objective_hash = h._pump_screen_grab()
    assert isinstance(objective_hash, int)

    jpeg, url = Harness._capture_screenshot(h, "death")
    assert jpeg is not None and jpeg[:2] == b"\xff\xd8"  # a real JPEG
    assert url == "https://shots/death.jpg"
    assert grabber.grabs >= 1


def test_a_stale_frame_is_not_filed_as_a_fresh_screenshot() -> None:
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = _tick_harness(bridge, grabber=_SlowGrabber(delay_s=0.0))
    h._frame = Image.new("RGB", (64, 64), (1, 2, 3))
    h._frame_at = time.monotonic() - (FRAME_MAX_AGE_S + 5.0)
    assert Harness._capture_screenshot(h, "death") == (None, None)


# --- a frozen script thread on a modal screen ---------------------------------
#
# Measured on the broadcast: the game was showing
#     MISSION FAILED
#     Franklin lost Lamar.
#     Skip [Tab]   Restart [Enter]
# while the live commentary read "Alpha's right there. Staying on his six." for
# over a minute. The bridge keeps answering 200 in that state, but the snapshot
# it returns never advances, so every world fact the harness has is stale.


class _FrozenTickBridge(_TickBridge):
    """Answers /state forever with a snapshot whose `tick` never moves."""

    def get_state(self):
        self.calls += 1
        if self.calls >= self.stop_after and self.stop is not None:
            self.stop.set()
        return GameState.model_validate(dict(self.state_body))


class _KeyRecorder:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def press_key(self, key: str) -> None:
        self.keys.append(key)

    def execute(self, action_type: str, params: dict[str, Any]) -> None:
        self.executed.append((action_type, dict(params)))


def test_a_frozen_snapshot_is_admitted_instead_of_narrated_over() -> None:
    bridge = _FrozenTickBridge(_state_body(), stop_after=6)
    h = _tick_harness(bridge)
    bridge.stop = h._stop
    # stall_timeout_s=0 so the real (wall-clock) loop reaches the blocked state
    # inside a handful of ticks instead of the production three seconds.
    h.blocking_screen_watchdog = BlockingScreenWatchdog(stall_timeout_s=0.0, retry_gap_s=0.0)
    h.primitives = _KeyRecorder()
    h.current_mission = {"name": "a mission that is already over"}
    h.mission_follower.bind_task("t-stale")

    assert h.run() == 0

    assert h._screen_blocked is True
    assert h.current_mission is None, "stale mission context must not survive the failure"
    assert h.mission_follower._bound_task_id is None
    assert h.primitives.keys, "the watchdog must actually press something"
    assert set(h.primitives.keys) <= set(BLOCKING_SCREEN_KEYS)
    assert h.primitives.keys[0] == "enter", "Enter is the non-destructive button"
    assert "tab" not in h.primitives.keys, "Tab is Skip/Restart — never sent"


def test_a_ticking_game_is_never_reported_as_a_blocking_screen() -> None:
    """Regression guard the other way: the ordinary loop (tick advancing every
    poll) must never claim the game is on a modal screen."""
    bridge = _TickBridge(_state_body(), stop_after=6)
    h = _tick_harness(bridge)
    bridge.stop = h._stop
    h.blocking_screen_watchdog = BlockingScreenWatchdog(stall_timeout_s=0.0, retry_gap_s=0.0)
    h.primitives = _KeyRecorder()
    assert h.run() == 0
    assert h._screen_blocked is False
    assert h.primitives.keys == []


def test_a_dxcam_import_that_raises_a_com_error_degrades_instead_of_killing_the_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dxcam builds its DXGI factory at MODULE IMPORT time, so `import dxcam`
    can raise `_ctypes.COMError` — not `ImportError`.

    Observed on the server 2026-09-02, twice in a row: the box has two display
    adapters (the virtual display driver plus the Intel iGPU) and one of them
    reported no currently-available output, so the import blew up with
    `COMError(-2005270494)`. `ScreenGrabber.__init__` only guarded `ImportError`,
    so the COMError escaped `Harness.__init__`'s `except ScreenshotUnavailableError`
    and killed the whole process on startup. The agent could not play at all because
    a SCREENSHOT was unavailable — while every decision he actually needs comes
    from the bridge. Capture is a nice-to-have; playing is not.
    """
    import builtins

    from wasted_harness import perception

    real_import = builtins.__import__

    def _explode(name: str, *args: object, **kwargs: object):
        if name == "dxcam":
            raise OSError("[WinError -2005270494] a resource is not available")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _explode)
    monkeypatch.setattr(perception.sys, "platform", "win32")

    with pytest.raises(perception.ScreenshotUnavailableError) as caught:
        perception.ScreenGrabber()
    assert "-2005270494" in str(caught.value), "the real COM reason must survive into the message"
