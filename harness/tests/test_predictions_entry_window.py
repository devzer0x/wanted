"""A `predictions` row must never be INSERTED once its entry window is gone or
about to be — CONTRACTS-PREDICTIONS §3's promise that a viewer who sees a card
has a real chance to answer it.

`generator._build_row` fixes `opened_at`/`locks_at`/`resolves_at` from the
wall clock AT GENERATION. `predictions/writer.py` then appends the row to the
shared `SupabaseWriter` buffer (`wasted_harness.events`), and during a
Supabase outage that buffer holds it — in memory and in the on-disk offline
queue — until Supabase comes back, however long that takes. Nothing between
generation and insertion re-checks whether `locks_at` is still in the future,
so a row generated with (say) a 30s entry window can sit in the offline queue
for an hour and then be inserted VERBATIM with `status: 'open'` and `locks_at`
already long past: an unenterable card that `entry_count = 0` voids
`no_entries` at settlement, silently breaking §3's entry-window promise every
time it happens.

This file reproduces that with the SAME real writer, the same real on-disk
offline queue, and the same real `127.0.0.1:1` connection-refusal idiom
`test_offline_queue.py`/`test_predictions_writer.py` already use (no mocks —
CLAUDE.md rule 1), and asserts the fix: `SupabaseWriter` drops a `predictions`
insert — never attempts it, never requeues it — once the time left to enter
(`locks_at` minus the writer's OWN injectable wall clock, the same
clock-injection idiom `predictions/generator.py` and `budget.py` already use)
falls below `MIN_ENTRY_REMAINING_S`. A dropped row is handled, not a write
failure: it must not move `predictions_write_failures` (the circuit breaker in
`main.PREDICTION_FAILURE_LIMIT` reads that counter) and must not be requeued.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from wasted_harness.events import MIN_ENTRY_REMAINING_S, SupabaseWriter
from wasted_harness.predictions.writer import PredictionWriter
from wasted_harness.settings import Settings

SESSION_ID = "00000000-0000-0000-0000-0000000000ff"

#: A real closed port: nothing listens on 1, so the supabase client raises a
#: genuine `httpx.ConnectError` — same idiom as test_offline_queue.py and
#: test_predictions_writer.py, never a simulated failure.
REFUSED_URL = "http://127.0.0.1:1"
KEY = "sb_secret_not_a_real_key"

#: A fixed instant, used as the writer's injected wall clock so every test
#: reasons about `locks_at` relative to a KNOWN "now" rather than the real
#: clock (which would make a boundary test flaky).
NOW_EPOCH = 1_893_456_000.0  # 2030-01-01T00:00:00Z, arbitrary and fixed


def _iso(epoch_s: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch_s, UTC).isoformat()


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key=None,
        bridge_url="http://127.0.0.1:7777",
        supabase_url=REFUSED_URL,
        supabase_secret_key=KEY,
        obs_ws_host="127.0.0.1",
        obs_ws_port=4455,
        obs_ws_password=None,
        overlay_host="127.0.0.1",
        overlay_port=7788,
        hourly_cap_usd=1.50,
        state_dir=tmp_path / "state",
        pricing_file=Path(__file__).resolve().parent.parent / "config" / "pricing.yaml",
    )


def make_row(*, locks_at_epoch: float, prediction_type: str = "loses_the_cops") -> dict:
    return {
        "session_id": SESSION_ID,
        "question": "WILL WANTED LOSE THEM?",
        "prediction_type": prediction_type,
        "state_context": {},
        "created_from_event": None,
        "opened_at": _iso(locks_at_epoch - 30.0),
        "locks_at": _iso(locks_at_epoch),
        "resolves_at": _iso(locks_at_epoch + 80.0),
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


def queued_predictions(writer: SupabaseWriter) -> list[dict]:
    if not writer.queue_path.exists():
        return []
    return [
        json.loads(line)["row"]
        for line in writer.queue_path.read_text().splitlines()
        if line.strip() and json.loads(line).get("table") == "predictions"
    ]


# -- the constant itself ---------------------------------------------------------


def test_min_entry_remaining_s_is_ten_seconds() -> None:
    """The page polls every 8s (CONTRACTS-PREDICTIONS §3); less than that is a
    card nobody watching could realistically answer."""
    assert MIN_ENTRY_REMAINING_S == 10.0


# -- a row already past its entry window is dropped, not requeued ----------------


def test_a_row_already_past_locks_at_is_dropped_not_inserted_not_requeued(
    tmp_path: Path, caplog
) -> None:
    settings = make_settings(tmp_path)
    writer = SupabaseWriter(settings, session_id=SESSION_ID, wall_clock=lambda: NOW_EPOCH)
    prediction_writer = PredictionWriter(writer)

    # locks_at 5s AGO relative to the writer's own clock.
    row = make_row(locks_at_epoch=NOW_EPOCH - 5.0)
    prediction_writer.write(row)

    assert writer.predictions_write_failures == 0
    with caplog.at_level(logging.WARNING, logger="wasted.events"):
        drained = writer.flush()

    # Nothing failed: the row was never attempted, so there is nothing left
    # pending and nothing to retry.
    assert drained is True
    assert queued_predictions(writer) == [], "a stale row was requeued instead of dropped"
    assert writer.predictions_write_failures == 0, (
        "a dropped row must not trip the circuit breaker (main.PREDICTION_FAILURE_LIMIT)"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    dropped = [r for r in warnings if r.kv.get("prediction_type") == "loses_the_cops"]
    assert dropped, [r.getMessage() for r in warnings]


def test_a_row_with_less_than_the_floor_remaining_is_also_dropped(
    tmp_path: Path, caplog
) -> None:
    """Not yet past `locks_at`, but with less time left to enter than the
    floor: still a card nobody watching could realistically answer."""
    settings = make_settings(tmp_path)
    writer = SupabaseWriter(settings, session_id=SESSION_ID, wall_clock=lambda: NOW_EPOCH)
    prediction_writer = PredictionWriter(writer)

    row = make_row(locks_at_epoch=NOW_EPOCH + (MIN_ENTRY_REMAINING_S - 1.0))
    prediction_writer.write(row)

    with caplog.at_level(logging.WARNING, logger="wasted.events"):
        drained = writer.flush()

    assert drained is True
    assert queued_predictions(writer) == []
    assert writer.predictions_write_failures == 0


def test_a_row_at_or_above_the_floor_is_not_dropped(tmp_path: Path) -> None:
    """Boundary control: at or above the floor, the row is genuinely attempted
    (and, against the real closed port, genuinely fails and lands on disk —
    unchanged behaviour, proving the fix does not overreach)."""
    settings = make_settings(tmp_path)
    writer = SupabaseWriter(settings, session_id=SESSION_ID, wall_clock=lambda: NOW_EPOCH)
    prediction_writer = PredictionWriter(writer)

    row = make_row(locks_at_epoch=NOW_EPOCH + MIN_ENTRY_REMAINING_S)
    prediction_writer.write(row)

    drained = writer.flush()

    assert drained is False, "a genuinely-attempted insert against a closed port must fail"
    rows = queued_predictions(writer)
    assert len(rows) == 1
    assert rows[0]["prediction_type"] == "loses_the_cops"
    assert writer.predictions_write_failures == 1


# -- the same drop applies to a row REPLAYED from the on-disk offline queue -------


def test_a_stale_row_replayed_from_the_offline_queue_is_also_dropped(
    tmp_path: Path, caplog
) -> None:
    """The exact scenario in the defect report: a row queued during an outage,
    carrying its ORIGINAL `locks_at`, replayed on a later flush once the
    writer's clock has moved past it."""
    settings = make_settings(tmp_path)
    # Written first with an unconfigured writer sharing the same queue path,
    # exactly like a real outage leaves a backlog on disk for the next process.
    unconfigured_settings = Settings(
        anthropic_api_key=None,
        bridge_url="http://127.0.0.1:7777",
        supabase_url=None,
        supabase_secret_key=None,
        obs_ws_host="127.0.0.1",
        obs_ws_port=4455,
        obs_ws_password=None,
        overlay_host="127.0.0.1",
        overlay_port=7788,
        hourly_cap_usd=1.50,
        state_dir=tmp_path / "state",
        pricing_file=Path(__file__).resolve().parent.parent / "config" / "pricing.yaml",
    )
    offline_writer = SupabaseWriter(unconfigured_settings, session_id=SESSION_ID)
    PredictionWriter(offline_writer).write(make_row(locks_at_epoch=NOW_EPOCH - 3600.0))
    assert offline_writer.flush() is False  # queued to disk, unconfigured
    assert len(queued_predictions(offline_writer)) == 1, "setup: row must be on disk first"

    # A later process (or the same one, after Supabase configuration returns)
    # picks the SAME queue path back up, with the clock now an hour later.
    reconnected = SupabaseWriter(settings, session_id=SESSION_ID, wall_clock=lambda: NOW_EPOCH)

    with caplog.at_level(logging.WARNING, logger="wasted.events"):
        drained = reconnected.flush()

    assert drained is True
    assert queued_predictions(reconnected) == [], (
        "a row replayed from the offline queue with a long-past locks_at was re-inserted "
        "(or re-queued) instead of dropped"
    )
    assert reconnected.predictions_write_failures == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(r.kv.get("prediction_type") == "loses_the_cops" for r in warnings)


# -- a locks_at with no timezone is judged as UTC, as Postgres will store it ------------


def test_a_naive_locks_at_is_judged_as_utc_like_postgres(tmp_path: Path) -> None:
    """The generator only writes tz-aware UTC. A naive value would still be
    accepted by the `timestamptz` column, in the session time zone (UTC on
    Supabase), so the drop check reads it the same way — not as the box's local
    time, which is what `datetime.timestamp()` does to a naive value (and on
    Windows can raise OSError for). A naive stale value is dropped; a naive
    value with a full window left is kept."""
    from datetime import UTC, datetime

    def naive(epoch_s: float) -> str:
        return datetime.fromtimestamp(epoch_s, UTC).replace(tzinfo=None).isoformat()

    settings = make_settings(tmp_path)
    writer = SupabaseWriter(settings, session_id=SESSION_ID, wall_clock=lambda: NOW_EPOCH)
    prediction_writer = PredictionWriter(writer)

    stale = make_row(locks_at_epoch=NOW_EPOCH - 5.0, prediction_type="stale_naive")
    stale["locks_at"] = naive(NOW_EPOCH - 5.0)
    fresh = make_row(locks_at_epoch=NOW_EPOCH + 60.0, prediction_type="fresh_naive")
    fresh["locks_at"] = naive(NOW_EPOCH + 60.0)
    prediction_writer.write(stale)
    prediction_writer.write(fresh)

    drained = writer.flush()

    # Only the fresh row is attempted (and fails against the closed port).
    assert drained is False
    assert [r["prediction_type"] for r in queued_predictions(writer)] == ["fresh_naive"]
    assert writer.predictions_write_failures == 1
