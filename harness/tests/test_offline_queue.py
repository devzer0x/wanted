"""Offline queue vs a REAL unreachable Supabase (http://127.0.0.1:1 refuses)."""

import json
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
    entries = [json.loads(l) for l in writer.queue_path.read_text().splitlines()]
    assert [e["row"]["type"] for e in entries] == ["death", "bridge_up"]


def test_queue_survives_repeated_failed_flushes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "http://127.0.0.1:1", "sb_secret_unused")
    writer = SupabaseWriter(settings, session_id="00000000-0000-0000-0000-000000000001")
    writer.record_event("unstick", {"distance_m": 2.4, "stuck_for_s": 25})
    writer.flush()
    writer.flush()  # retries the backlog against the refused port
    entries = [json.loads(l) for l in writer.queue_path.read_text().splitlines()]
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
