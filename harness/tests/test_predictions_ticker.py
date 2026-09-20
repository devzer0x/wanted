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
import re
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


# --- the RPC names this ticker calls must actually be callable by service_role ---
#
# The harness holds the service-role key and nothing else. A migration that tightens a
# grant is invisible to Python: `_call_one`'s `except` is broad "by design: never kill
# the main loop", so a revoked function fails with 42501 on every tick and surfaces only
# as `settled: None` in a log line that looks like an idle tick.
#
# That is exactly what happened. `20260914000001_settlement_guards.sql` put settlement
# behind an advisory-lock wrapper and revoked the bare `settle_due_predictions()` from
# `service_role`; SETTLE_FN still named the bare one, so the contract's PRIMARY driver
# locked predictions and then never settled a single one. These tests read the real
# migrations and assert the names this module ships are granted and not revoked, so a
# future grant change breaks a test instead of silently un-driving the lifecycle.

MIGRATIONS = Path(__file__).resolve().parents[2] / "infra" / "supabase" / "migrations"

GRANT_RE = re.compile(
    r"^\s*grant\s+execute\s+on\s+function\s+public\.(\w+)\s*\([^)]*\)\s+to\s+([^;]+);",
    re.IGNORECASE | re.MULTILINE,
)
REVOKE_RE = re.compile(
    r"^\s*revoke\s+execute\s+on\s+function\s+public\.(\w+)\s*\([^)]*\)\s+from\s+([^;]+);",
    re.IGNORECASE | re.MULTILINE,
)


def service_role_grants() -> set[str]:
    """Replay every migration in filename order; return the functions `service_role`
    can still EXECUTE at the end. Order matters — settlement is granted, then revoked."""
    granted: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text()
        events: list[tuple[int, str, str, bool]] = []
        for m in GRANT_RE.finditer(sql):
            events.append((m.start(), m.group(1), m.group(2), True))
        for m in REVOKE_RE.finditer(sql):
            events.append((m.start(), m.group(1), m.group(2), False))
        for _pos, fn, roles, is_grant in sorted(events):
            if "service_role" not in roles:
                continue
            granted.add(fn) if is_grant else granted.discard(fn)
    return granted


def test_the_migrations_are_where_we_think_they_are() -> None:
    """Guards the two tests below from passing vacuously on an empty glob."""
    assert MIGRATIONS.is_dir(), MIGRATIONS
    assert len(list(MIGRATIONS.glob("*.sql"))) >= 8
    assert "lock_due_predictions" in service_role_grants()


def test_every_rpc_this_ticker_calls_is_granted_to_service_role() -> None:
    granted = service_role_grants()
    for fn in (LOCK_FN, SETTLE_FN):
        assert fn in granted, (
            f"{fn} is not EXECUTE-granted to service_role by the end of the migrations. "
            f"The harness holds only that key, so every tick would fail 42501 and be "
            f"swallowed by _call_one. Granted: {sorted(granted)}"
        )


def test_settlement_goes_through_the_advisory_lock_wrapper() -> None:
    """FM-10: two unserialized drivers could each spend the full daily cap. The bare
    function is revoked precisely so nothing can bypass the lock — including us."""
    assert SETTLE_FN == "settle_due_predictions_serialized"
    assert "settle_due_predictions" not in service_role_grants()
