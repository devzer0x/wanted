"""Offline queue vs a REAL unreachable Supabase (http://127.0.0.1:1 refuses)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from wasted_harness.events import SupabaseWriter
from wasted_harness.settings import Settings


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


def test_unconfigured_writer_queues_offline(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, None, None)
    writer = SupabaseWriter(settings, session_id=None)
    assert not writer.configured
    writer.record_event("bridge_down", {"consecutive_failures": 3})
    assert writer.flush() is False
    lines = writer.queue_path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["table"] == "events"
    assert entry["row"]["type"] == "bridge_down"
    assert entry["row"]["payload"] == {"consecutive_failures": 3}
    assert entry["row"]["session_id"] is None  # back-filled by the harness at flush


def test_configured_but_unreachable_queues(tmp_path: Path) -> None:
    # Port 1 on localhost: a real connection refusal, not a mock.
    settings = make_settings(
        tmp_path, "http://127.0.0.1:1", "sb_secret_not_a_real_key_but_never_sent"
    )
    writer = SupabaseWriter(settings, session_id="00000000-0000-0000-0000-000000000001")
    assert writer.configured
    writer.record_event("death", {"cause": "?", "street": "x", "deaths_total": 1})
    writer.record_event("bridge_up", {"downtime_s": 12.5})
    assert writer.flush() is False
    entries = [json.loads(line) for line in writer.queue_path.read_text().splitlines()]
    assert [e["row"]["type"] for e in entries] == ["death", "bridge_up"]


def test_queue_survives_repeated_failed_flushes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "http://127.0.0.1:1", "sb_secret_unused")
    writer = SupabaseWriter(settings, session_id="00000000-0000-0000-0000-000000000001")
    writer.record_event("unstick", {"distance_m": 2.4, "stuck_for_s": 25})
    writer.flush()
    writer.flush()  # retries the backlog against the refused port
    entries = [json.loads(line) for line in writer.queue_path.read_text().splitlines()]
    assert len(entries) == 1  # no duplication, no loss
    assert writer.queue_depth() == 1


def test_event_type_enum_is_closed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, None, None)
    writer = SupabaseWriter(settings)
    with pytest.raises(ValueError, match="not a CONTRACTS"):
        writer.record_event("agent_sneezed", {})


def test_clip_insert_offline_returns_none(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, None, None)
    writer = SupabaseWriter(settings, session_id="00000000-0000-0000-0000-000000000001")
    clip_id = writer.insert_clip(
        {"event_id": None, "storage_path": "x/y.mp4", "duration_s": 30.0, "caption": "c"}
    )
    assert clip_id is None  # honest: no id exists until the row is really inserted
    assert writer.queue_depth() == 1


# -- the heartbeat may never outrun the events it implies -----------------------------
#
# `settle_due_predictions()` opens its telemetry-completeness gate when
# `max(events.ts) >= resolves_at` OR `stats.heartbeat_at >= resolves_at`
# (20260908120000_predictions.sql:383-400), treating the heartbeat as proof "the
# writer had caught up past the window" (CONTRACTS-PREDICTIONS §3). The writer is
# not one ordered channel: every (table, op) run is a separate HTTP request and a
# failed run does not abort the rest of the flush. So an `events` insert carrying an
# in-window death could fail while the `stats` upsert right behind it succeeded —
# publishing the proof without the evidence, and letting a window settle on
# telemetry that had not landed. The exposed rules fail toward PAYING OUT: a queued
# death makes `survives_window` resolve SURVIVED.
#
# These drive the real `SupabaseWriter` against a real HTTP server that accepts
# `stats` and refuses `events` — the exact asymmetry the exploit needs.


class _EventsRefusingHandler(BaseHTTPRequestHandler):
    """Real PostgREST-shaped endpoint: 500 on /rest/v1/events, 200 elsewhere."""

    def do_POST(self) -> None:  # BaseHTTPRequestHandler's own casing
        self._respond()

    def do_PATCH(self) -> None:
        self._respond()

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.seen.append(self.path)  # type: ignore[attr-defined]
        failing = "/events" in self.path
        self.send_response(500 if failing else 200)
        self.send_header("Content-Type", "application/json")
        body = b'{"message":"injected failure"}' if failing else b"[]"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def events_refusing_server():
    server = HTTPServer(("127.0.0.1", 0), _EventsRefusingHandler)
    server.seen: list[str] = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _writer_against(server: HTTPServer, tmp_path: Path) -> SupabaseWriter:
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return SupabaseWriter(make_settings(tmp_path, url, "test-key"), session_id=SESSION)


SESSION = "00000000-0000-0000-0000-0000000000b4"


def test_a_heartbeat_is_not_published_when_the_events_beside_it_failed(
    tmp_path: Path, events_refusing_server: HTTPServer
) -> None:
    writer = _writer_against(events_refusing_server, tmp_path)
    writer.record_event("death", {"cause": "in the window we are about to settle"})
    writer.upsert_stats({"session_id": SESSION, "deaths": 1, "heartbeat_at": "2026-09-20T00:00:00Z"})

    assert writer.flush() is False

    paths = events_refusing_server.seen  # type: ignore[attr-defined]
    assert any("/events" in p for p in paths), "the events write was never attempted"
    assert not any("/stats" in p for p in paths), (
        f"the heartbeat was published while an event was still queued: {paths}. "
        "That is the settlement-gate race — it lets a window resolve on absent telemetry."
    )
    queued = [json.loads(line) for line in writer.queue_path.read_text().splitlines()]
    assert {e["table"] for e in queued} == {"events", "stats"}


def test_flush_records_that_it_left_rows_behind(
    tmp_path: Path, events_refusing_server: HTTPServer
) -> None:
    writer = _writer_against(events_refusing_server, tmp_path)
    assert writer.unflushed is False  # nothing has failed yet
    writer.record_event("death", {})
    assert writer.flush() is False
    assert writer.unflushed is True, (
        "Harness._heartbeat reads this to decide whether a heartbeat can honestly "
        "claim the writer has caught up"
    )


def test_a_clean_flush_clears_the_flag(tmp_path: Path) -> None:
    """A writer with nothing to say is caught up, and must not be stuck OFF AIR."""
    writer = SupabaseWriter(make_settings(tmp_path, None, None), session_id=SESSION)
    writer.unflushed = True
    writer._flush_once = lambda: True  # type: ignore[method-assign]
    assert writer.flush() is True
    assert writer.unflushed is False
