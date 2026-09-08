"""Writes generator rows through the harness's EXISTING Supabase writer.

CLAUDE.md rule 1 / the task brief: no second Supabase client. Every row here
goes through the same `wasted_harness.events.SupabaseWriter` instance the rest
of the harness already uses — the same batched buffer, the same on-disk
offline queue (`state/queue.jsonl`), the same cross-process advisory lock, the
same `flush()` retry/backoff behaviour. Nothing in this module opens a socket,
imports `supabase`, or reads `SUPABASE_URL`/`SUPABASE_SECRET_KEY` itself.

**Why this reaches into `SupabaseWriter._buffer`/`._buffer_lock` instead of
calling a public method.** `SupabaseWriter` has no generic "insert any table"
method — every existing call (`record_event`, `insert_mission`, `insert_clip`,
...) is one hard-coded table. The buffer entry shape those all build is
`{"table": <name>, "op": "insert"|"upsert"|"update", "row": {...}}`, and
`events.py`'s own flush path (`_write_entries`/`_runs`) is already fully
table-agnostic over that shape — nothing there special-cases `"missions"` or
`"clips"` by name, so appending a `"predictions"` entry the same way
`insert_mission` appends a `"missions"` entry is a faithful use of the
existing machinery, not a workaround of it. This package's brief is scoped to
`harness/wasted_harness/predictions/` only and may not add a public
`insert_predictions_row`-style method to `events.py` (that file belongs to a
different directory owner) — if/when it does grow one, this function's body
becomes a one-line call and everything else here is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wasted_harness.events import SupabaseWriter
from wasted_harness.logsetup import get_logger

log = get_logger("wasted.predictions.writer")

PREDICTIONS_TABLE = "predictions"


def _buffer_insert(writer: SupabaseWriter, table: str, row: dict[str, Any]) -> None:
    row = dict(row)
    row.setdefault("session_id", writer.session_id)
    # Reaches SupabaseWriter's own buffer/lock directly — see the module
    # docstring for why (no public generic "insert any table" method exists).
    with writer._buffer_lock:
        writer._buffer.append({"table": table, "op": "insert", "row": row})


@dataclass
class PredictionWriter:
    """Thin adapter: `PredictionGenerator` rows in, buffered through `writer` out.

    One instance wraps one live `SupabaseWriter` (constructed and owned by the
    main harness process elsewhere — this package does not construct one
    itself, matching "reuse this writer, do not write a second client").
    """

    writer: SupabaseWriter

    def write(self, row: dict[str, Any]) -> None:
        """Buffer one prediction row for the next flush.

        Raises on a row with no `session_id` rather than silently writing bad
        data — CONTRACTS-PREDICTIONS §2: "a prediction always belongs to a
        real session". `PredictionGenerator.generate` already refuses to
        produce such a row (returns `None` instead when no session is live),
        so reaching this is a caller bug, not a live-game edge case.
        """
        session_id = row.get("session_id", self.writer.session_id)
        if session_id is None:
            raise ValueError(
                "refusing to write a prediction with no session_id "
                "(CONTRACTS-PREDICTIONS §2: a prediction always belongs to a real session)"
            )
        _buffer_insert(self.writer, PREDICTIONS_TABLE, row)
        log.info(
            "prediction buffered",
            extra={
                "kv": {
                    "prediction_type": row.get("prediction_type"),
                    "session_id": session_id,
                }
            },
        )

    def flush(self) -> bool:
        """Delegates to the underlying writer's own flush (batched insert +
        offline-queue fallback). Not a separate flush path."""
        return self.writer.flush()
