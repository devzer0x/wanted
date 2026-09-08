"""PredictionWriter: same buffered writer, same real on-disk offline queue.

Mirrors `tests/test_offline_queue.py`'s own idiom (a real `SupabaseWriter`
against either an unconfigured client or a REAL TCP connection refusal on
`127.0.0.1:1` — never a mock) because this package must not stand up a second
Supabase client; every row goes through that exact object.

No live Supabase project is reachable from this environment (no
`SUPABASE_URL`/`SUPABASE_SECRET_KEY` provided to this task), so the deepest
real check available here is the same one the rest of the harness test suite
already relies on for this code path: a genuine network failure / genuine
"not configured" branch, both exercised against real code, ending in the real
on-disk queue file being read back and parsed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support.states import make_state

from wasted_harness.events import SupabaseWriter
from wasted_harness.predictions.generator import PredictionGenerator
from wasted_harness.predictions.writer import PredictionWriter
from wasted_harness.settings import Settings

SESSION_ID = "00000000-0000-0000-0000-0000000000bb"


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


def test_write_buffers_and_flushes_to_the_real_offline_queue(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, None, None)
    supabase_writer = SupabaseWriter(settings, session_id=SESSION_ID)
    writer = PredictionWriter(supabase_writer)

    row = {
        "session_id": SESSION_ID,
        "question": "WILL WANTED DIE IN THE NEXT 3 MINUTES?",
        "prediction_type": "death_in_window",
        "state_context": {"health": 200, "wanted": 0},
        "created_from_event": None,
        "opened_at": "2026-09-08T00:00:00+00:00",
        "locks_at": "2026-09-08T00:00:15+00:00",
        "resolves_at": "2026-09-08T00:03:00+00:00",
        "outcomes": [{"key": "yes", "label": "YES"}, {"key": "no", "label": "NO"}],
        "telemetry_rule": {"kind": "event_occurs", "event_type": "death"},
        "status": "open",
    }
    writer.write(row)
    assert writer.flush() is False  # unconfigured -> queued, exactly like events.py's own writer

    lines = supabase_writer.queue_path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["table"] == "predictions"
    assert entry["op"] == "insert"
    assert entry["row"]["session_id"] == SESSION_ID
    assert entry["row"]["prediction_type"] == "death_in_window"
    assert entry["row"]["telemetry_rule"] == {"kind": "event_occurs", "event_type": "death"}


def test_write_backfills_session_id_from_the_writer_when_the_row_omits_it(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, None, None)
    supabase_writer = SupabaseWriter(settings, session_id=SESSION_ID)
    writer = PredictionWriter(supabase_writer)

    row = {
        "question": "WILL WANTED GET IN A VEHICLE?",
        "prediction_type": "enters_vehicle",
        "state_context": {},
        "created_from_event": None,
        "opened_at": "x",
        "locks_at": "y",
        "resolves_at": "z",
        "outcomes": [],
        "telemetry_rule": {"kind": "vehicle_entered"},
        "status": "open",
    }
    writer.write(row)
    writer.flush()
    entry = json.loads(supabase_writer.queue_path.read_text().splitlines()[0])
    assert entry["row"]["session_id"] == SESSION_ID


def test_write_refuses_a_row_with_no_session_anywhere(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, None, None)
    supabase_writer = SupabaseWriter(settings, session_id=None)  # no live session
    writer = PredictionWriter(supabase_writer)
    with pytest.raises(ValueError, match="session_id"):
        writer.write({"question": "x", "prediction_type": "death_in_window"})


def test_write_against_a_real_unreachable_supabase_still_queues(tmp_path: Path) -> None:
    # Port 1 on localhost: a real connection refusal, not a mock — same trick
    # as test_offline_queue.py::test_configured_but_unreachable_queues.
    settings = make_settings(tmp_path, "http://127.0.0.1:1", "sb_secret_not_a_real_key")
    supabase_writer = SupabaseWriter(settings, session_id=SESSION_ID)
    writer = PredictionWriter(supabase_writer)
    writer.write(
        {
            "session_id": SESSION_ID,
            "question": "WILL WANTED LOSE THE COPS?",
            "prediction_type": "loses_the_cops",
            "state_context": {},
            "created_from_event": None,
            "opened_at": "x",
            "locks_at": "y",
            "resolves_at": "z",
            "outcomes": [],
            "telemetry_rule": {"kind": "wanted_clears"},
            "status": "open",
        }
    )
    assert writer.flush() is False
    entries = [json.loads(line) for line in supabase_writer.queue_path.read_text().splitlines()]
    assert [e["table"] for e in entries] == ["predictions"]


def test_end_to_end_generator_row_flows_through_the_real_writer(tmp_path: Path) -> None:
    """Generator -> writer -> real offline queue, no mock anywhere in the chain."""
    settings = make_settings(tmp_path, None, None)
    supabase_writer = SupabaseWriter(settings, session_id=SESSION_ID)
    prediction_writer = PredictionWriter(supabase_writer)

    from wasted_harness.predictions.catalog import CATALOG

    generator = PredictionGenerator(catalog=CATALOG, clock=lambda: 0.0, wall_clock=lambda: 1_893_456_000.0)
    row = generator.generate(make_state(), session_id=SESSION_ID)
    assert row is not None

    prediction_writer.write(row)
    prediction_writer.flush()

    entry = json.loads(supabase_writer.queue_path.read_text().splitlines()[0])
    assert entry["table"] == "predictions"
    assert entry["row"]["prediction_type"] == row["prediction_type"]
    assert entry["row"]["telemetry_rule"] == row["telemetry_rule"]
