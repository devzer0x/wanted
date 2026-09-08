"""PredictionTicker: drives lock_due_predictions()/settle_due_predictions()
through the SAME SupabaseWriter/client, never against a mock.

No live Supabase project is reachable from this environment. As in
`test_predictions_writer.py`, the deepest real check available is a genuine
network failure (a real TCP connection refusal on `127.0.0.1:1`) and the
genuine "not configured" branch — both exercised against the real
`wasted_harness.events.SupabaseWriter`, never a stand-in client.
"""

from __future__ import annotations

import logging
from pathlib import Path

from wasted_harness.events import SupabaseWriter
from wasted_harness.predictions.ticker import LOCK_FN, SETTLE_FN, PredictionTicker
from wasted_harness.settings import Settings

SESSION_ID = "00000000-0000-0000-0000-0000000000cc"


def make_settings(tmp_path: Path, url: str | None, key: str | None) -> Settings:
    return Settings(
        anthropic_api_key=None,
        bridge_url="http://127.0.0.1:7777",
        supabase_url=url,
        supabase_secret_key=key,
        obs_ws_host="127.0.0.1",
        obs_ws_port=4455,
        obs_ws_password=None,
        overlay_host="127.0.0.1",
        overlay_port=7788,
        hourly_cap_usd=1.50,
        state_dir=tmp_path / "state",
        pricing_file=Path(__file__).resolve().parent.parent / "config" / "pricing.yaml",
    )


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# -- gating: no session, not configured, cadence -----------------------------------


def test_no_session_never_ticks(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, None, None)
    writer = SupabaseWriter(settings, session_id=None)
    ticker = PredictionTicker(writer, clock=FakeClock())
    assert ticker.maybe_tick(session_id=None) is None


def test_unconfigured_writer_never_ticks(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, None, None)
    writer = SupabaseWriter(settings, session_id=SESSION_ID)
    assert not writer.configured
    ticker = PredictionTicker(writer, clock=FakeClock())
    assert ticker.maybe_tick(session_id=SESSION_ID) is None


def test_cadence_blocks_a_second_tick_before_the_interval_elapses(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "http://127.0.0.1:1", "sb_secret_not_a_real_key")
    writer = SupabaseWriter(settings, session_id=SESSION_ID)
    clock = FakeClock()
    ticker = PredictionTicker(writer, interval_s=5.0, clock=clock)

    first = ticker.maybe_tick(session_id=SESSION_ID)
    assert first is not None  # a real (failing) attempt was made

    clock.advance(1.0)
    second = ticker.maybe_tick(session_id=SESSION_ID)
    assert second is None  # cadence not yet elapsed

    clock.advance(5.0)
    third = ticker.maybe_tick(session_id=SESSION_ID)
    assert third is not None


# -- real network failure: never raises, reports None per failed call, both RPCs tried --


def test_a_real_connection_refusal_never_raises_and_reports_none(tmp_path: Path, caplog) -> None:
    settings = make_settings(tmp_path, "http://127.0.0.1:1", "sb_secret_not_a_real_key")
    writer = SupabaseWriter(settings, session_id=SESSION_ID)
    ticker = PredictionTicker(writer, clock=FakeClock())

    with caplog.at_level(logging.WARNING, logger="wasted.predictions.ticker"):
        result = ticker.maybe_tick(session_id=SESSION_ID)

    assert result == (None, None)  # both calls genuinely failed, not "0 due"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    fns_warned = {r.kv["fn"] for r in warnings if hasattr(r, "kv")}
    assert fns_warned == {LOCK_FN, SETTLE_FN}  # both RPCs were actually attempted


def test_reuses_the_same_client_no_second_one_stood_up(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "http://127.0.0.1:1", "sb_secret_not_a_real_key")
    writer = SupabaseWriter(settings, session_id=SESSION_ID)
    ticker = PredictionTicker(writer, clock=FakeClock())

    assert writer._client is None  # nothing built yet
    ticker.maybe_tick(session_id=SESSION_ID)
    client_after_first = writer._client
    assert client_after_first is not None  # the ticker built (and cached) the real client

    ticker.interval_s = 0.0  # allow an immediate second tick for this assertion only
    ticker.maybe_tick(session_id=SESSION_ID)
    assert writer._client is client_after_first  # same object — no second client
