"""Offline-queue durability: locking, atomic rewrite, size cap, no lost rows.

The Windows failure this guards against: the watchdog's post_event CLI holds
queue.jsonl open while the harness rewrites it, `os.replace` hits a sharing
violation, and an unguarded flush() both raises into the main loop and drops
every pending row. Here the same race is provoked with a real second handle.
"""

import json
from pathlib import Path

from test_offline_queue import make_settings

from wasted_harness.events import MAX_QUEUE_BYTES, SupabaseWriter, _queue_file_lock


def _entries(writer: SupabaseWriter) -> list[dict]:
    return [json.loads(line) for line in writer.queue_path.read_text(encoding="utf-8").splitlines()]


def test_rewrite_survives_another_open_handle(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "http://127.0.0.1:1", "sb_secret_unused")
    writer = SupabaseWriter(settings, session_id="00000000-0000-0000-0000-000000000001")
    writer.record_event("unstick", {"distance_m": 2.4, "stuck_for_s": 25})
    writer.flush()
    assert writer.queue_depth() == 1

    # A second process (the watchdog CLI) reading the queue while the harness
    # flushes again. On Windows this is what turns os.replace into a
    # PermissionError; the writer must retry, not raise, and must not lose rows.
    with writer.queue_path.open("r", encoding="utf-8") as _held:
        writer.record_event("bridge_down", {"consecutive_failures": 3})
        assert writer.flush() is False
    assert [e["row"]["type"] for e in _entries(writer)] == ["unstick", "bridge_down"]


def test_flush_never_raises_when_the_queue_directory_is_gone(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "http://127.0.0.1:1", "sb_secret_unused")
    writer = SupabaseWriter(settings, session_id="00000000-0000-0000-0000-000000000001")
    writer.record_event("death", {"cause": "?", "street": "x", "deaths_total": 1})
    writer.flush()
    # Simulate the state directory disappearing under the harness.
    for child in writer.queue_path.parent.iterdir():
        child.unlink()
    writer.queue_path.parent.rmdir()
    writer.record_event("bridge_up", {"downtime_s": 3.0})
    assert writer.flush() is False  # reported honestly, no exception


def test_queue_is_capped_so_an_outage_cannot_fill_the_disk(
    tmp_path: Path, monkeypatch
) -> None:
    # The real cap is 40 MB; shrink it so the test exercises the same code path
    # without writing 40 MB to a machine that is short on disk.
    cap = 20_000
    monkeypatch.setattr("wasted_harness.events.MAX_QUEUE_BYTES", cap)
    assert MAX_QUEUE_BYTES == 40 * 1024 * 1024, "the shipped cap should stay 40 MB"

    settings = make_settings(tmp_path, None, None)
    writer = SupabaseWriter(settings, session_id="00000000-0000-0000-0000-000000000001")
    blob = "x" * 1_000
    rows = 60
    for i in range(rows):
        writer.record_event("wanted_change", {"from": 0, "to": 1, "pad": blob, "i": i})
        writer.flush()
    size = writer.queue_path.stat().st_size
    assert size <= cap, f"queue grew to {size} bytes past the {cap}-byte cap"
    kept = _entries(writer)
    # Oldest dropped, newest kept: the last hours of the show are what matter.
    assert kept[-1]["row"]["payload"]["i"] == rows - 1
    assert kept[0]["row"]["payload"]["i"] > 0
    assert len(kept) < rows


def test_lock_is_reentrant_across_sequential_acquisitions(tmp_path: Path) -> None:
    lock = tmp_path / "queue.lock"
    for _ in range(3):
        with _queue_file_lock(lock):
            pass
    assert lock.exists()
