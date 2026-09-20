"""The prediction layer, wired into the real loop.

`wasted_harness/predictions/` was already built and already tested on its own
(`test_predictions_catalog/generator/writer/ticker.py`). What was missing was
any caller: nothing in the harness ever constructed a generator, offered it a
`/state`, or drove `lock_due_predictions()` / `settle_due_predictions()`. These
tests are about that wiring, and they exercise it through the REAL
`Harness.run()` — the same "run the loop, do not re-describe it" idiom
`test_main_loop_resilience.py` established, whose harness builder they reuse
rather than fork.

Three things are deliberately real here, not stood in for:

* **The writer.** Every test uses a real `wasted_harness.main._ObservedWriter`
  (a real `SupabaseWriter`), with its real batched buffer and its real on-disk
  offline queue. Assertions about "was a prediction written" read that queue
  file back off the disk, exactly as `test_predictions_writer.py` does.
* **The Supabase failure.** No live Supabase project is reachable from this
  environment, so — as in `test_offline_queue.py`, `test_predictions_writer.py`
  and `test_predictions_ticker.py` — the failure under test is a genuine TCP
  connection refusal against a real closed port (`127.0.0.1:1`), not a
  simulated one. `configured` is true, the client is really built, the RPCs are
  really attempted, and the insert really fails.
* **The exception.** `_RefusingTicker`/`_RefusingWriter` raise
  `httpx.ConnectError`, which is what the supabase client actually raises
  against that closed port (measured, not assumed). They exist only because
  `PredictionTicker` and `PredictionWriter` already swallow their own I/O
  failures, so a real refusal can never reach `main.py`'s guard — and the guard
  is the thing under test. Same idiom as `test_main_loop_resilience.py`'s
  `_RefusingBridge`, which raises the real `BridgeApiError` a real bridge would.

`caplog` is deliberately not used across `run()`: `run()` starts with
`setup_logging()`, which strips every handler off the root logger, pytest's
capture handler included. Where a log line is the assertion, the method is
called directly instead.
"""

from __future__ import annotations

import json
import signal
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_main_loop_resilience import _state_body as _base_state_body
from test_main_loop_resilience import _tick_harness, _TickBridge

from wasted_harness.bridge_client import GameState
from wasted_harness.main import (
    PREDICTION_RECENT_MAX,
    PREDICTION_RECENT_WINDOW_S,
    Harness,
    _ObservedWriter,
    _RecentEvents,
)
from wasted_harness.predictions import CATALOG, PredictionTicker, PredictionWriter
from wasted_harness.settings import Settings


def _state_body(**over: Any) -> dict[str, Any]:
    """`test_main_loop_resilience._state_body`, plus a hostile ped.

    The shipped catalogue has no ambient question left: `enters_vehicle` and
    `exits_vehicle` were the only templates that fired on a quiet on-foot moment,
    and both were withdrawn because settlement voids their rule kind 100% of the
    time. So a bare /state now triggers nothing, and "nothing was written" would
    be true for reasons that have nothing to do with what these tests assert.
    A hostile ped in view is the cheapest real trigger (`survives_a_fight`).
    """
    body = _base_state_body(**over)
    body["nearby"]["peds"] = [
        {"handle": 7, "model": "g_m_y_lost_01", "distance": 3.0, "relationship": "hostile"}
    ]
    return body


SESSION_ID = "00000000-0000-0000-0000-0000000000dd"

#: A real closed port. Nothing listens on 1, so the supabase client raises a
#: real `httpx.ConnectError` — the same thing `test_predictions_ticker.py` and
#: `test_offline_queue.py` use to get an honest failure without a live project.
REFUSED_URL = "http://127.0.0.1:1"
KEY = "sb_secret_not_a_real_key"

CATALOG_TYPES = {tpl.prediction_type for tpl in CATALOG}


class FakeClock:
    """Same shape as `test_predictions_ticker.py`'s: a monotonic clock the test
    advances by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_settings(
    tmp_path: Path, *, predictions_enabled: bool = True, supabase: bool = True
) -> Settings:
    """A real `Settings`, built field by field like `test_predictions_writer.py`'s.

    `poll_hz=20.0` is passed straight to the constructor (only `Settings.load`
    clamps it to the 2-4 Hz the real box runs at) for the same reason
    `test_main_loop_resilience._Settings` does it: the loop honours its own
    backoffs either way, and the tests should not spend real seconds sleeping.
    """
    return Settings(
        anthropic_api_key=None,
        bridge_url="http://127.0.0.1:7777",
        supabase_url=REFUSED_URL if supabase else None,
        supabase_secret_key=KEY if supabase else None,
        obs_ws_host="127.0.0.1",
        obs_ws_port=4455,
        obs_ws_password=None,
        overlay_host="127.0.0.1",
        overlay_port=7788,
        hourly_cap_usd=1.50,
        state_dir=tmp_path / "state",
        pricing_file=Path(__file__).resolve().parent.parent / "config" / "pricing.yaml",
        poll_hz=20.0,
        predictions_enabled=predictions_enabled,
    )


def make_harness(bridge: Any, settings: Settings, session_id: str | None) -> Harness:
    """`test_main_loop_resilience`'s full-tick harness, with the real writer and
    the real prediction wiring `Harness.__init__` would have given it.

    `__init__` itself cannot run here (it demands an Anthropic key and makes
    live API calls to verify the model ids), which is exactly why the wiring it
    does was split into `_wire_predictions` — the branch under test is the
    production one, called with a production `Settings`.
    """
    h = _tick_harness(bridge)
    h.settings = settings
    h.session_id = session_id
    h.writer = _ObservedWriter(settings, session_id=session_id, on_event=h._note_recent_event)
    h._wire_predictions(settings)
    bridge.stop = h._stop
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


def queued(writer: _ObservedWriter) -> list[dict[str, Any]]:
    """Everything that reached the real on-disk offline queue."""
    if not writer.queue_path.exists():
        return []
    return [json.loads(line) for line in writer.queue_path.read_text().splitlines() if line.strip()]


def queued_predictions(writer: _ObservedWriter) -> list[dict[str, Any]]:
    return [e["row"] for e in queued(writer) if e.get("table") == "predictions"]


def buffered_predictions(writer: _ObservedWriter) -> list[dict[str, Any]]:
    return [e["row"] for e in writer._buffer if e.get("table") == "predictions"]


def written_predictions(writer: _ObservedWriter) -> list[dict[str, Any]]:
    """Predictions the loop produced, wherever they ended up — still buffered,
    or already pushed out to the queue by a failed flush. Both are "written" as
    far as this wiring is concerned; which one depends only on whether Supabase
    was reachable."""
    return buffered_predictions(writer) + queued_predictions(writer)


# --- the flag ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("0", False), ("no", False), ("off", False), ("FALSE", False),
     ("true", True), ("1", True), ("anything else", True)],
)
def test_the_env_var_is_what_switches_the_layer_off(
    tmp_path: Path, monkeypatch, value: str, expected: bool
) -> None:
    """`WASTED_PREDICTIONS_ENABLED`, parsed by the real `Settings.load`, in the
    same shape as `WASTED_MISSIONS_ENABLED` (on unless explicitly switched off)."""
    monkeypatch.setenv("WASTED_PREDICTIONS_ENABLED", value)
    settings = Settings.load(env_file=tmp_path / "absent.env")
    assert settings.predictions_enabled is expected


def test_the_layer_is_off_by_default(tmp_path: Path, monkeypatch) -> None:
    """Opt-in per deployment, deliberately.

    The layer needs schema a Supabase project may not have. A box that starts
    generating against a missing table has every insert rejected and requeued on
    the game-loop thread, and the backlog is re-POSTed on every flush until the
    2-4 Hz loop collapses and the agent stops playing. For a machine that runs
    unattended, the safe default is to do nothing until told.
    """
    monkeypatch.delenv("WASTED_PREDICTIONS_ENABLED", raising=False)
    assert Settings.load(env_file=tmp_path / "absent.env").predictions_enabled is False


def test_the_layer_turns_on_when_the_operator_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WASTED_PREDICTIONS_ENABLED", "true")
    assert Settings.load(env_file=tmp_path / "absent.env").predictions_enabled is True


# --- (a) flag off: nothing generated, nothing ticked ---------------------------


def test_with_the_flag_off_nothing_is_generated_and_nothing_is_ticked(tmp_path: Path) -> None:
    """Supabase is CONFIGURED here (at a closed port), so the writer is fully
    live and a tick would really reach for the network. Nothing does."""
    settings = make_settings(tmp_path, predictions_enabled=False)
    bridge = _TickBridge(_state_body(), stop_after=4)
    h = make_harness(bridge, settings, SESSION_ID)

    # `_wire_predictions` left every one of them alone: this is what "off" is.
    assert h.predictions is None
    assert h.prediction_writer is None
    assert h.prediction_ticker is None
    assert h._recent_events is None

    assert h.run() == 0
    assert bridge.calls >= 4, "the loop stopped ticking"

    assert written_predictions(h.writer) == [], "a switched-off layer wrote a prediction"
    assert h._recent_event_window() == [], "a switched-off layer kept an event window"
    # ...and the run really did happen and really did write: the assertion above
    # is the layer being off, not the loop being dead.
    assert [e for e in queued(h.writer) if e["table"] == "events"], (
        "the loop recorded no events at all; the negative above proves nothing"
    )


# --- (b) flag on, no live session: nothing written -----------------------------


def test_with_the_flag_on_and_no_live_session_nothing_is_written(tmp_path: Path) -> None:
    """CONTRACTS-PREDICTIONS §2: a prediction always belongs to a real session.

    Supabase is configured (at the closed port) and the layer is fully wired, so
    the ONLY thing standing between this run and a prediction is the absent
    session id.
    """
    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=4)
    h = make_harness(bridge, settings, session_id=None)

    assert h.predictions is not None and h.prediction_ticker is not None
    assert h.writer.session_id is None

    assert h.run() == 0
    assert bridge.calls >= 4

    assert written_predictions(h.writer) == []
    assert h.predictions.open_count == 0, "the generator counted a prediction it never made"
    # The ticker was offered every iteration and declined every one of them: the
    # lifecycle RPCs belong to a session's predictions, and there is no session.
    assert h.prediction_ticker._last_tick_at == float("-inf")


# --- the control: with a live session, all of that really does happen ----------


def test_a_live_session_opens_a_real_prediction_through_the_one_writer(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=4)
    h = make_harness(bridge, settings, SESSION_ID)

    assert h.run() == 0

    rows = written_predictions(h.writer)
    assert len(rows) == 1, f"expected exactly one prediction from a short run, got {len(rows)}"
    row = rows[0]
    assert row["session_id"] == SESSION_ID
    assert row["prediction_type"] in CATALOG_TYPES
    assert row["status"] == "open"
    # CONTRACTS-PREDICTIONS §2's CHECK, on the row that was actually written.
    assert row["opened_at"] < row["locks_at"] < row["resolves_at"]
    assert row["telemetry_rule"]["kind"], "a prediction with no telemetry rule voids at settlement"

    # The lifecycle really was driven, on the real client.
    assert h.prediction_ticker._last_tick_at > float("-inf")

    # ...and all of it went through the ONE writer this process has. No second
    # Supabase client was stood up for the prediction layer.
    assert h.prediction_writer.writer is h.writer
    assert h.prediction_ticker.writer is h.writer
    assert h.writer._client is not None, "the ticker never actually reached for a client"


# --- (c) a Supabase failure in the prediction path stays there -----------------


def test_a_real_supabase_failure_in_the_prediction_path_never_reaches_the_loop(
    tmp_path: Path,
) -> None:
    """A genuine connection refusal on every RPC and every insert, for a whole
    run. The show carries on and the prediction ends up in the real queue."""
    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=6)
    h = make_harness(bridge, settings, SESSION_ID)

    assert h.run() == 0, "a failing Supabase ended the run"
    assert bridge.calls >= 6, "the loop stopped ticking"
    assert bridge.closed, "the bridge was not closed on the way out"

    # The insert genuinely failed and the row genuinely fell back to disk —
    # `events.py`'s own offline queue, not a second mechanism for predictions.
    assert len(queued_predictions(h.writer)) == 1
    # The RPCs were genuinely attempted against the closed port.
    assert h.prediction_ticker._last_tick_at > float("-inf")
    assert h.writer._client is not None


class _RefusingTicker(PredictionTicker):
    """Raises what the supabase client really raises against `127.0.0.1:1`.

    The real ticker catches that itself, so a real refusal can never reach the
    loop's own guard — which is the thing this file needs to prove is there.
    """

    calls: int = 0

    def maybe_tick(self, session_id: str | None) -> tuple[int | None, int | None] | None:
        self.calls += 1
        raise httpx.ConnectError("[Errno 61] Connection refused")


class _RefusingWriter(PredictionWriter):
    """Same, for the write half of the path."""

    calls: int = 0

    def write(self, row: dict[str, Any]) -> None:
        self.calls += 1
        raise httpx.ConnectError("[Errno 61] Connection refused")


def test_a_raising_prediction_seam_never_takes_the_loop_down(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=6)
    h = make_harness(bridge, settings, SESSION_ID)
    h.prediction_ticker = _RefusingTicker(h.writer)
    h.prediction_writer = _RefusingWriter(h.writer)

    assert h.run() == 0, "a raising prediction layer ended the run"
    assert bridge.calls >= 6, "the loop stopped ticking"
    assert bridge.closed

    # Not vacuous: both halves really were called and really did raise.
    assert h.prediction_ticker.calls >= 6, "the lifecycle was never driven"
    assert h.prediction_writer.calls >= 1, "the generator never offered a row"
    # And the rest of the tick was untouched — real events still went out.
    assert [e for e in queued(h.writer) if e["table"] == "events"]


def test_the_raise_is_logged_rather_than_swallowed_in_silence(tmp_path: Path, caplog) -> None:
    """Called directly, not through `run()`: `run()` calls `setup_logging()`,
    which removes pytest's capture handler from the root logger."""
    import logging

    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = make_harness(bridge, settings, SESSION_ID)
    h.prediction_ticker = _RefusingTicker(h.writer)
    h.prediction_writer = _RefusingWriter(h.writer)
    state = GameState.model_validate(_state_body())

    with caplog.at_level(logging.WARNING, logger="wasted.main"):
        h._tick_prediction_lifecycle()
        h._offer_prediction(state)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("prediction lifecycle tick failed" in m for m in messages), messages
    assert any("prediction generation failed" in m for m in messages), messages
    errors = [r.kv["error"] for r in caplog.records if hasattr(r, "kv") and "error" in r.kv]
    assert all(e.startswith("ConnectError") for e in errors), errors


# --- the recency window the generator is offered --------------------------------


def test_every_event_the_writer_is_handed_lands_in_the_recency_window(tmp_path: Path) -> None:
    """The tap is on the writer, not on the ~17 call sites, so an event emitted
    by ANY of them is in the window with no further wiring."""
    settings = make_settings(tmp_path, predictions_enabled=True, supabase=False)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = make_harness(bridge, settings, SESSION_ID)

    h.writer.record_event("wanted_change", {"from": 0, "to": 2})
    # The unbatched path `_record_big_event` takes for death/busted when clips
    # are up. Unconfigured here, so it queues and returns None — the event still
    # happened, and is still noted.
    assert h.writer.insert_event_now("death", {"cause": "?"}) is None

    window = h._recent_event_window()
    assert [e["type"] for e in window] == ["wanted_change", "death"]
    assert window[0]["payload"] == {"from": 0, "to": 2}
    assert all("ts" in e for e in window), "RecentEvent carries the §4 row's own shape"


def test_an_event_type_outside_the_contract_is_refused_and_never_noted(tmp_path: Path) -> None:
    """`SupabaseWriter.record_event` validates the closed §4 enum before it
    buffers anything; an event that was refused never happened."""
    settings = make_settings(tmp_path, predictions_enabled=True, supabase=False)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = make_harness(bridge, settings, SESSION_ID)

    with pytest.raises(ValueError):
        h.writer.record_event("not_a_contract_event", {})
    assert h._recent_event_window() == []


def test_the_window_forgets_events_older_than_a_minute() -> None:
    """The real `_RecentEvents` on an injected clock — the same idiom
    `PredictionTicker`/`PredictionGenerator` use — rather than a real minute of
    `time.sleep`."""
    clock = FakeClock()
    recent = _RecentEvents(PREDICTION_RECENT_MAX, PREDICTION_RECENT_WINDOW_S, clock=clock)

    recent.note("death", {"cause": "?"})
    clock.advance(PREDICTION_RECENT_WINDOW_S - 1.0)
    recent.note("wanted_change", {"from": 0, "to": 1})
    assert [e["type"] for e in recent.window()] == ["death", "wanted_change"]

    clock.advance(2.0)  # the death is now just outside the window, the gain is not
    assert [e["type"] for e in recent.window()] == ["wanted_change"]

    clock.advance(PREDICTION_RECENT_WINDOW_S)
    assert recent.window() == []
    assert recent.maxlen == PREDICTION_RECENT_MAX, "the window has no hard ceiling"


def test_the_window_never_grows_past_its_ceiling() -> None:
    recent = _RecentEvents(PREDICTION_RECENT_MAX, PREDICTION_RECENT_WINDOW_S)
    for i in range(PREDICTION_RECENT_MAX * 2):
        recent.note("wanted_change", {"from": 0, "to": i})
    window = recent.window()
    assert len(window) == PREDICTION_RECENT_MAX
    # Oldest first, and it is the OLDEST that was dropped.
    assert window[0]["payload"]["to"] == PREDICTION_RECENT_MAX


def test_the_window_survives_being_written_from_another_thread() -> None:
    """The clip worker records a `clip` event from its own thread (`obs.py`)
    while the loop thread reads the window. Iterating a bare `deque` through
    that raises `RuntimeError: deque mutated during iteration`."""
    recent = _RecentEvents(PREDICTION_RECENT_MAX, PREDICTION_RECENT_WINDOW_S)
    stop = threading.Event()
    failures: list[BaseException] = []

    def writing() -> None:
        try:
            while not stop.is_set():
                recent.note("clip", {"clip_id": 1, "event_type": "death"})
        except BaseException as exc:  # re-raised on the main thread; a thread that
            # dies silently is exactly the failure this test exists to catch
            failures.append(exc)

    def reading() -> None:
        try:
            for _ in range(3000):
                recent.window()
        except BaseException as exc:  # re-raised on the main thread; a thread that
            # dies silently is exactly the failure this test exists to catch
            failures.append(exc)

    writer_thread = threading.Thread(target=writing, name="test-clip-writer")
    writer_thread.start()
    try:
        reading()
    finally:
        stop.set()
        writer_thread.join(timeout=5.0)
    assert not writer_thread.is_alive()
    assert failures == [], failures


def test_the_window_is_offered_to_the_generator_within_the_tick_that_made_it(
    tmp_path: Path,
) -> None:
    """`_offer_prediction` runs after `_reflex`, so a `wanted_change` recorded
    from THIS tick's snapshot is already in the window the generator sees. Three
    of the catalogue's templates are calibrated on exactly that."""
    settings = make_settings(tmp_path, predictions_enabled=True, supabase=False)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = make_harness(bridge, settings, SESSION_ID)

    seen: list[list[dict[str, Any]]] = []
    real_generate = h.predictions.generate

    def recording_generate(state, session_id, recent_events=()):
        seen.append(list(recent_events))
        return real_generate(state, session_id, recent_events)

    h.predictions.generate = recording_generate  # observe the real call, do not replace it
    h.writer.record_event("wanted_change", {"from": 0, "to": 2})
    h._offer_prediction(GameState.model_validate(_state_body(dead=False)))

    assert seen, "the generator was never offered anything"
    assert [e["type"] for e in seen[0]] == ["wanted_change"]

# --- (d) the two guards that stop a misconfigured layer taking the show down ---


def test_the_written_row_carries_the_columns_the_real_schema_demands(tmp_path: Path) -> None:
    """`predictions.reward_pool` is `numeric(38,18) NOT NULL CHECK (> 0)` and
    `reward_asset` is `NOT NULL`, both with no default and nothing backfilling
    them.

    Asserting this here is the point: the layer's own control test passed for a
    long time while the row it wrote could never actually land, because
    `written_predictions()` reads back out of the in-process buffer and the
    buffer does not know what the schema requires. A rejected insert is not
    dropped — it is requeued and re-POSTed on the game-loop thread forever.
    """
    from decimal import Decimal

    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=4)
    h = make_harness(bridge, settings, SESSION_ID)
    assert h.run() == 0

    rows = written_predictions(h.writer)
    assert rows, "no prediction to check"
    row = rows[0]
    assert row.get("reward_asset"), "reward_asset is NOT NULL in the schema"
    assert Decimal(row["reward_pool"]) > 0, "reward_pool has CHECK (reward_pool > 0)"


def test_no_prediction_is_opened_from_a_frozen_screen_snapshot(tmp_path: Path) -> None:
    """While the blocking-screen watchdog holds a frozen snapshot the harness
    refuses to publish the HUD, because "republishing them under a fresh
    timestamp is publishing a number the game is not producing".

    A prediction carries those same frozen HUD fields in `state_context` AND
    opens a real, publicly enterable window off them — and because a frozen game
    emits no events, §3's completeness gate never opens, so the question voids
    and the audience gets nothing back for having answered it.

    Driven through `_offer_prediction` directly rather than `run()`: the loop
    recomputes `_screen_blocked` from the watchdog on every tick, so a value set
    before `run()` would simply be overwritten and the test would pass without
    ever exercising the gate.
    """
    from support.states import make_state

    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=2)
    h = make_harness(bridge, settings, SESSION_ID)
    state = make_state()

    # Believable: a prediction IS offered, so the test can tell a working gate
    # from a layer that simply never generates anything.
    # A spy in place of the real generator. The real one has its own cooldowns
    # and trigger conditions, so "nothing was written" would be true for
    # reasons that have nothing to do with the gate — the test has to be able
    # to tell a working gate from a quiet generator.
    class _SpyGenerator:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, state, session_id, recent_events):
            self.calls += 1
            return None  # nothing to write; we are counting the ASK, not the row

    spy = _SpyGenerator()
    h.predictions = spy

    # Control: unblocked, the generator really is asked. Without this the test
    # would pass even if `_offer_prediction` had been deleted entirely.
    h._screen_blocked = False
    assert h._believe_state(state) is True
    h._offer_prediction(state)
    assert spy.calls == 1, "control: the generator was never asked even when unblocked"

    # Frozen: the same snapshot, now disbelieved. The generator must not be
    # asked at all, however long the freeze lasts.
    h._screen_blocked = True
    assert h._believe_state(state) is False
    for _ in range(10):
        h._offer_prediction(state)
    assert spy.calls == 1, (
        f"the generator was asked {spy.calls - 1} time(s) for a prediction built "
        "from a snapshot this process had already decided was stale"
    )


def test_generation_stops_after_repeated_write_failures(tmp_path: Path) -> None:
    """The circuit breaker. Without it, a missing table turns "predictions do
    not work" into "the agent stops playing": every rejected insert is requeued
    and the whole backlog is re-POSTed on the loop thread every flush.
    """
    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=4)
    h = make_harness(bridge, settings, SESSION_ID)

    from wasted_harness.main import PREDICTION_FAILURE_LIMIT

    h._prediction_write_failures = PREDICTION_FAILURE_LIMIT
    assert h.run() == 0
    assert written_predictions(h.writer) == [], (
        "generation continued after the breaker had tripped"
    )



# --- (e) a rejected predictions insert is VISIBLE, and trips the breaker --------
#
# The gap the test above documents from the other side. `test_generation_stops_
# after_repeated_write_failures` proves the breaker stops generation once the
# counter is at the limit — but on a real box nothing ever moved that counter.
# `_offer_prediction` only counted RAISES out of `generate()`/`write()`, and a
# `predictions` insert that Supabase refuses does not raise there: `events.py`
# catches it inside `flush()`, logs it like any other table's network blip,
# writes the row to the offline queue and re-POSTs the whole backlog on the
# game-loop thread on every flush, forever. The breaker could not see the one
# failure it exists for.
#
# The failure below is real, not simulated: a genuine TCP refusal against
# 127.0.0.1:1 through the real supabase client, the same idiom the rest of this
# file (and `test_offline_queue.py`) uses to get an honest failure without a
# live project. What it exercises is the counting and the breaker, and those do
# not care whether the server said "connection refused" or "column does not
# exist" — only that the `predictions` run failed and kept failing.


def test_a_refused_predictions_insert_is_counted_on_the_writer(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, predictions_enabled=True)
    writer = _ObservedWriter(settings, session_id=SESSION_ID, on_event=lambda *_: None)
    prediction_writer = PredictionWriter(writer)

    assert writer.predictions_write_failures == 0
    prediction_writer.write(
        {
            "session_id": SESSION_ID,
            "question": "WILL WANTED LOSE THEM?",
            "prediction_type": "loses_the_cops",
            "state_context": {},
            "created_from_event": None,
            "opened_at": "2026-09-21T00:00:00+00:00",
            "locks_at": "2026-09-21T00:00:30+00:00",
            "resolves_at": "2026-09-21T00:01:50+00:00",
            "outcomes": [{"key": "yes", "label": "YES"}, {"key": "no", "label": "NO"}],
            "telemetry_rule": {
                "kind": "wanted_clears",
                "outcome_if_true": "yes",
                "outcome_if_false": "no",
            },
            "status": "open",
            "is_event": False,
            "reward_pool": "0.005",
            "reward_asset": "TTWO",
        }
    )
    # Every flush retries the queued row and fails again — the "retried from
    # the disk queue forever" behaviour, now counted.
    for expected in range(1, 4):
        assert writer.flush() is False
        assert writer.predictions_write_failures == expected
    assert writer.last_predictions_error is not None
    assert "ConnectError" in writer.last_predictions_error
    # ...and the row is genuinely on disk, not quietly dropped.
    assert len(queued_predictions(writer)) == 1


def test_the_refusal_is_logged_at_error_naming_the_table_and_the_server(
    tmp_path: Path, caplog
) -> None:
    """A WARNING among every other table's WARNINGs is how this stayed
    invisible. It is an ERROR now, and it says which table and what the server
    said — the two things an operator needs to tell a five-second network blip
    from an unapplied migration."""
    import logging

    settings = make_settings(tmp_path, predictions_enabled=True)
    writer = _ObservedWriter(settings, session_id=SESSION_ID, on_event=lambda *_: None)
    PredictionWriter(writer).write(
        {
            "session_id": SESSION_ID,
            "question": "WILL WANTED LOSE THEM?",
            "prediction_type": "loses_the_cops",
            "telemetry_rule": {
                "kind": "wanted_clears",
                "outcome_if_true": "yes",
                "outcome_if_false": "no",
            },
            "status": "open",
            "reward_pool": "0.005",
            "reward_asset": "TTWO",
        }
    )
    with caplog.at_level(logging.ERROR, logger="wasted.events"):
        writer.flush()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, [r.getMessage() for r in caplog.records]
    assert "predictions" in errors[0].getMessage() or errors[0].kv["table"] == "predictions"
    assert "ConnectError" in errors[0].kv["server_message"]


def test_a_landed_predictions_flush_clears_the_count(tmp_path: Path) -> None:
    """The counter is CONSECUTIVE failures. Whatever was rejecting the rows
    stopping is the thing that clears it, and nothing else."""
    settings = make_settings(tmp_path, predictions_enabled=True)
    writer = _ObservedWriter(settings, session_id=SESSION_ID, on_event=lambda *_: None)
    writer.predictions_write_failures = 3
    writer.last_predictions_error = "APIError: relation does not exist"

    # A flush with no predictions in it changes nothing either way: the agent
    # being quiet is not evidence about the table.
    writer.record_event("break", {"phase": "start"})
    writer.flush()
    assert writer.predictions_write_failures == 3
    assert writer.last_predictions_error is not None


def test_a_real_rejected_insert_trips_the_generation_breaker(tmp_path: Path) -> None:
    """End to end: a `predictions` insert that keeps being refused stops
    generation, without a single exception ever reaching `_offer_prediction`."""
    from wasted_harness.main import PREDICTION_FAILURE_LIMIT

    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = make_harness(bridge, settings, SESSION_ID)
    state = GameState.model_validate(_state_body())

    h._offer_prediction(state)
    assert written_predictions(h.writer), "control: nothing was generated to fail on"
    # Nothing raised on the generate/write path — that is the whole point.
    assert h._prediction_write_failures == 0

    for _ in range(PREDICTION_FAILURE_LIMIT):
        h.writer.flush()
    assert h.writer.predictions_write_failures >= PREDICTION_FAILURE_LIMIT
    assert h._prediction_failures() >= PREDICTION_FAILURE_LIMIT

    # The breaker is now shut: the loop may keep running, but it stops adding
    # rows to a queue nothing will ever drain.
    before = len(queued_predictions(h.writer)) + len(buffered_predictions(h.writer))
    for _ in range(20):
        h._offer_prediction(state)
    after = len(queued_predictions(h.writer)) + len(buffered_predictions(h.writer))
    assert after == before, "generation continued after a real rejection tripped the breaker"


def test_the_breaker_says_so_once_and_names_the_server_message(
    tmp_path: Path, caplog
) -> None:
    import logging

    from wasted_harness.main import PREDICTION_FAILURE_LIMIT

    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = make_harness(bridge, settings, SESSION_ID)
    h.writer.predictions_write_failures = PREDICTION_FAILURE_LIMIT
    h.writer.last_predictions_error = "APIError: relation \"predictions\" does not exist"
    state = GameState.model_validate(_state_body())

    with caplog.at_level(logging.WARNING, logger="wasted.main"):
        for _ in range(10):
            h._offer_prediction(state)

    announcements = [
        r for r in caplog.records if "generation is now OFF for this run" in r.getMessage()
    ]
    assert len(announcements) == 1, "the breaker announced itself more than once"
    assert "does not exist" in announcements[0].kv["server_message"]


# --- (f) the operator knobs reach the generator ---------------------------------


def test_the_operator_knobs_reach_the_generator_config(tmp_path: Path, monkeypatch) -> None:
    """`settings.py` -> `GeneratorConfig`, through the real `_wire_predictions`."""
    for name, value in (
        ("WASTED_PREDICTIONS_ENABLED", "true"),
        ("WASTED_PREDICTION_ROUND_S", "120"),
        ("WASTED_PREDICTION_AMBIENT", "false"),
        ("WASTED_PREDICTION_MIN_GAP_S", "45"),
        ("WASTED_PREDICTION_MAX_OPEN", "4"),
        ("WASTED_PREDICTION_BASE_POOL", "0.01"),
        # NO SUPABASE, deliberately and explicitly. `_wire_predictions` warm
        # starts the base rate with one real read, and the developer `.env`
        # this repo is checked out with points at the live cloud project —
        # `load_dotenv` puts it in `os.environ` for the whole pytest process,
        # so "pass an absent env_file" is NOT enough on its own. Blanked here
        # so this test can never touch production data. Every other test in
        # this file uses a closed port for the same reason.
        ("SUPABASE_URL", ""),
        ("SUPABASE_SECRET_KEY", ""),
    ):
        monkeypatch.setenv(name, value)
    settings = Settings.load(env_file=tmp_path / "absent.env")
    assert settings.supabase_configured is False
    assert settings.state_dir  # real Settings, not the hand-built one above

    bridge = _TickBridge(_state_body(), stop_after=1)
    h = _tick_harness(bridge)
    h.settings = settings
    h.writer = _ObservedWriter(settings, session_id=SESSION_ID, on_event=h._note_recent_event)
    h._wire_predictions(settings)

    config = h.predictions.config
    assert config.round_interval_s == 120.0
    assert config.ambient_enabled is False
    assert config.min_gap_s == 45.0
    assert config.max_concurrent_open == 4
    assert config.base_reward_pool == "0.01"
    assert h.prediction_base_rate is not None
    assert h.predictions.base_rate == h.prediction_base_rate.measure


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WASTED_PREDICTION_ROUND_S", "soon"),
        ("WASTED_PREDICTION_ROUND_S", "0"),
        ("WASTED_PREDICTION_MIN_GAP_S", "-5"),
        ("WASTED_PREDICTION_MAX_OPEN", "two"),
        ("WASTED_PREDICTION_MAX_OPEN", "0"),
        ("WASTED_PREDICTION_BASE_POOL", "free"),
        ("WASTED_PREDICTION_BASE_POOL", "0"),
    ],
)
def test_an_unreadable_knob_is_a_startup_error_not_a_silent_default(
    tmp_path: Path, monkeypatch, name: str, value: str
) -> None:
    """An operator who set a cadence, a cap or a treasury pool and got the
    default would have no way to tell. §6's own rule for the claim rails, held
    here too: "a rail that is present but unparseable is an error"."""
    from wasted_harness.settings import ConfigError

    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        Settings.load(env_file=Path(tmp_path) / "absent.env")


# --- (g) every recorded event reaches the live base-rate estimator --------------


def test_every_event_the_writer_records_also_feeds_the_base_rate(tmp_path: Path) -> None:
    """One choke point, two consumers: the recency window the situational
    templates read, and the rolling measurement the ambient ones need."""
    settings = make_settings(tmp_path, predictions_enabled=True, supabase=False)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = make_harness(bridge, settings, SESSION_ID)
    assert h.prediction_base_rate is not None

    before = h.prediction_base_rate.observed_count
    h.writer.record_event("activity_end", {"activity": "x", "outcome": "completed"})
    h.writer.record_event("wanted_change", {"from": 0, "to": 1})
    assert h.prediction_base_rate.observed_count == before + 2
    # ...including the unbatched path `death`/`busted` take when clips are up.
    h.writer.insert_event_now("death", {"cause": "?"})
    assert h.prediction_base_rate.observed_count == before + 3


def test_with_the_layer_off_nothing_is_measured(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, predictions_enabled=False, supabase=False)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = make_harness(bridge, settings, SESSION_ID)
    assert h.prediction_base_rate is None
    h.writer.record_event("activity_end", {"activity": "x", "outcome": "completed"})  # no crash


def test_a_warm_start_against_an_unreachable_supabase_falls_back_to_cold(
    tmp_path: Path,
) -> None:
    """The read is best effort in every direction: a real connection refusal
    leaves a cold estimator and a running harness, never an exception at
    startup."""
    settings = make_settings(tmp_path, predictions_enabled=True)
    bridge = _TickBridge(_state_body(), stop_after=1)
    h = make_harness(bridge, settings, SESSION_ID)  # _wire_predictions ran the read
    assert h.prediction_base_rate is not None
    assert h.prediction_base_rate.observed_count == 0
    assert h.run() == 0
