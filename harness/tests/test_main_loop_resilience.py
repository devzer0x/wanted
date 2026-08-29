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

import signal
import threading
import time
from typing import Any

import pytest

from wasted_harness.behavior.recovery import BridgeDownTracker, BridgeStallTracker
from wasted_harness.bridge_client import (
    BridgeApiError,
    BridgeDownError,
    BridgeTransientError,
)
from wasted_harness.main import Harness


class _Recorder:
    """Absorbs bookkeeping calls; records the ones the tests assert on."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.stats: list[dict[str, Any]] = []

    def record_event(self, type_: str, payload: dict[str, Any], **_kw: Any) -> None:
        self.events.append((type_, payload))

    def upsert_stats(self, row: dict[str, Any]) -> None:
        self.stats.append(row)

    def flush(self) -> None:
        pass


class _Governor:
    level = 0

    def total_usd(self) -> float:
        return 0.0

    def cost_per_hour_usd(self) -> float:
        return 0.0


class _Settings:
    poll_hz = 20.0  # fast, so the test is quick; the loop honours the backoff anyway


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
    h.counters = {"deaths": 0, "busted": 0, "missions_passed": 0}
    h.current_goal = "test"
    h.session_id = "test-session"
    h._stop = threading.Event()
    h._last_stats = 0.0
    h._last_flush = 0.0
    h._started = time.monotonic()
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
    # The heartbeat keeps beating, so the site does not go stale while waiting.
    assert h.writer.stats, "no heartbeat during the stall"


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
