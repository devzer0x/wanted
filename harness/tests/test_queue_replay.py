"""Replay must be exactly-once and in order — the queue's two silent data bugs.

Both of these put wrong data on a public page rather than losing it:

1. A group was chunked into MAX_BATCH inserts inside ONE try. If chunk 2 failed,
   the whole group was requeued and chunk 1 was inserted a second time. `events`
   and `decisions` are `generated always as identity` with no natural key and no
   ON CONFLICT, so that is a genuinely duplicated row: the same WASTED moment
   twice in the feed, the same line said twice.
2. Rows were grouped globally by table, so a replay re-ordered the whole flush —
   every queued `events` row first, then every `decisions` row — and a
   `sessions` insert could land AFTER the rows referencing it (FK failure for
   the whole run, which then triggers bug 1 on the retry).

The double below is a stand-in for `postgrest`'s query builder — only the four
calls `SupabaseWriter._write_entries` makes — so the failure can be placed on an
exact chunk. Everything under test is the real writer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from wasted_harness.events import MAX_BATCH, SupabaseWriter
from wasted_harness.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key=None,
        bridge_url="http://127.0.0.1:7777",
        supabase_url="https://example.supabase.co",
        # Suppression is deliberate: S106 flags any literal passed to a *_key argument.
        # This is a test fixture that never leaves the process — the string itself says so.
        supabase_secret_key="sb_secret_never_sent_anywhere",  # noqa: S106
        obs_ws_host="127.0.0.1",
        obs_ws_port=4455,
        obs_ws_password=None,
        overlay_host="127.0.0.1",
        overlay_port=7788,
        hourly_cap_usd=1.50,
        state_dir=tmp_path / "state",
        pricing_file=Path(__file__).resolve().parent.parent / "config" / "pricing.yaml",
    )


class _Table:
    def __init__(self, client: _FakeClient, name: str) -> None:
        self._client = client
        self._name = name
        self._pending: tuple[str, Any] | None = None
        self._filters: list[tuple[str, Any]] = []

    def insert(self, rows: list[dict[str, Any]]) -> _Table:
        self._pending = ("insert", rows)
        return self

    def upsert(self, row: dict[str, Any], returning: str = "representation") -> _Table:
        self._pending = ("upsert", row)
        return self

    def update(self, row: dict[str, Any], returning: str = "representation") -> _Table:
        self._pending = ("update", row)
        return self

    def eq(self, column: str, value: Any) -> _Table:
        self._filters.append((column, value))
        return self

    def execute(self) -> Any:
        assert self._pending is not None
        op, payload = self._pending
        self._client.calls.append((self._name, op, payload, list(self._filters)))
        if self._client.fail_after is not None and len(
            [c for c in self._client.calls if c[1] == "insert"]
        ) > self._client.fail_after:
            raise httpx.ConnectError("connection reset mid-replay")
        if op == "insert":
            self._client.written.setdefault(self._name, []).extend(payload)
        else:
            self._client.written.setdefault(self._name, []).append(payload)
        return type("Resp", (), {"data": [{"id": 1}]})()


class _FakeClient:
    """The postgrest surface `_write_entries` uses, and nothing else."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.calls: list[tuple[str, str, Any, list[tuple[str, Any]]]] = []
        self.written: dict[str, list[dict[str, Any]]] = {}
        self.fail_after = fail_after

    def table(self, name: str) -> _Table:
        return _Table(self, name)


def _writer(tmp_path: Path, client: _FakeClient) -> SupabaseWriter:
    writer = SupabaseWriter(make_settings(tmp_path), session_id="s-1")
    writer._client = client  # the client is built lazily; hand it the double
    return writer


def _queued(writer: SupabaseWriter) -> list[dict[str, Any]]:
    if not writer.queue_path.exists():
        return []
    return [json.loads(line) for line in writer.queue_path.read_text().splitlines() if line.strip()]


def test_a_mid_group_failure_never_replays_a_chunk_that_already_landed() -> None:
    """The duplicated-death bug, end to end."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    client = _FakeClient(fail_after=1)  # chunk 1 lands, chunk 2 blows up
    writer = _writer(tmp, client)
    # Decisions, because they are the rows that really do arrive faster than
    # MAX_BATCH between flushes (one per tactical call) and `record_decision`
    # does not force a flush of its own.
    for i in range(MAX_BATCH + 5):
        writer.record_decision({"layer": "tactical", "say": f"line {i}", "n": i})
    writer.flush()

    landed = [row["n"] for row in client.written.get("decisions", [])]
    requeued = [e["row"]["n"] for e in _queued(writer)]
    assert len(landed) == len(set(landed)), f"a row was written twice: {landed}"
    assert not (set(landed) & set(requeued)), (
        "a row that already landed is queued for replay and will be inserted "
        f"a second time: {sorted(set(landed) & set(requeued))}"
    )
    assert sorted(landed + requeued) == list(range(MAX_BATCH + 5)), "a row was lost"


def test_replay_keeps_queue_order_across_tables(tmp_path: Path) -> None:
    client = _FakeClient()
    writer = _writer(tmp_path, client)
    writer.record_event("bridge_down", {"consecutive_failures": 3})
    writer.record_decision({"layer": "tactical", "say": "one"})
    writer.record_event("bridge_up", {"downtime_s": 4.0})
    writer.record_decision({"layer": "tactical", "say": "two"})
    assert writer.flush() is True

    order = [(table, op) for table, op, _payload, _f in client.calls]
    assert order == [
        ("events", "insert"),
        ("decisions", "insert"),
        ("events", "insert"),
        ("decisions", "insert"),
    ], f"the flush was re-ordered by table: {order}"


def test_a_session_row_is_written_before_anything_that_references_it(tmp_path: Path) -> None:
    """A governor level carried over from the last process is buffered in
    __init__, i.e. BEFORE the session row exists. Sent first it FK-fails."""
    client = _FakeClient()
    writer = _writer(tmp_path, client)
    writer.record_event("governor_level", {"from": 0, "to": 2, "reason": "carried_over"})
    writer.insert_session({"id": "s-1", "game_edition": "legacy", "harness_version": "0.2.0"})
    writer.record_event("session_start", {"harness_version": "0.2.0"})
    assert writer.flush() is True
    assert client.calls[0][0] == "sessions", (
        f"the session row must go first; order was {[c[0] for c in client.calls]}"
    )


def test_only_the_newest_heartbeat_per_session_is_kept(tmp_path: Path) -> None:
    """12 stats upserts a minute, each carrying absolute values: keeping them
    all cost one HTTP round trip per dead row on reconnect and ate the queue
    cap that protects the event history."""
    client = _FakeClient()
    writer = _writer(tmp_path, client)
    for i in range(50):
        writer.upsert_stats({"deaths": i, "heartbeat_at": f"2026-09-02T00:00:{i:02d}Z"})
    writer.record_event("unstick", {"distance_m": 1.0, "stuck_for_s": 25})
    for i in range(50, 60):
        writer.upsert_stats({"deaths": i, "heartbeat_at": f"2026-09-02T00:01:{i:02d}Z"})
    assert writer.flush() is True

    stats_writes = [c for c in client.calls if c[0] == "stats"]
    assert len(stats_writes) == 1, f"{len(stats_writes)} heartbeats written, expected 1"
    assert stats_writes[0][2]["deaths"] == 59, "the newest heartbeat must be the survivor"
    assert any(c[0] == "events" for c in client.calls), "the event must not be collapsed"


def test_stats_are_collapsed_in_the_offline_queue_too(tmp_path: Path) -> None:
    """Same rule when the queue is the destination: an outage must not
    accumulate ~720 dead heartbeats an hour."""
    settings = make_settings(tmp_path)
    object.__setattr__(settings, "supabase_url", None)  # unconfigured: straight to the queue
    writer = SupabaseWriter(settings, session_id="s-1")
    for i in range(30):
        writer.upsert_stats({"deaths": i, "heartbeat_at": f"2026-09-02T00:00:{i:02d}Z"})
    writer.flush()
    entries = _queued(writer)
    assert len(entries) == 1
    assert entries[0]["row"]["deaths"] == 29


def test_session_end_is_written_as_an_update_with_its_own_timestamp(tmp_path: Path) -> None:
    """`sessions.ended_at` was never written by anything, so a crash and a clean
    stop were indistinguishable in the table designed to tell them apart."""
    client = _FakeClient()
    writer = _writer(tmp_path, client)
    writer.update_session_end("s-1", "2026-09-02T08:00:00+00:00")
    assert writer.flush() is True
    table, op, payload, filters = client.calls[0]
    assert (table, op) == ("sessions", "update")
    assert payload == {"ended_at": "2026-09-02T08:00:00+00:00"}
    assert filters == [("id", "s-1")]


def test_a_queued_session_end_survives_an_outage(tmp_path: Path) -> None:
    """A clean stop taken while Supabase is unreachable must still close the
    session out when a later run replays the queue."""
    client_down = _FakeClient(fail_after=-1)
    writer = _writer(tmp_path, client_down)
    writer.update_session_end("s-1", "2026-09-02T08:00:00+00:00")
    writer.flush()
    entries = _queued(writer)
    assert entries and entries[0]["op"] == "update"
    assert entries[0]["match"] == {"id": "s-1"}

    client_up = _FakeClient()
    replayer = _writer(tmp_path, client_up)
    assert replayer.flush() is True
    assert client_up.calls[0][2] == {"ended_at": "2026-09-02T08:00:00+00:00"}
