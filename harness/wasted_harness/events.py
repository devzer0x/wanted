"""Supabase writer: batched service-role writes with an on-disk offline queue.

Write path per CONTRACTS §5: harness only, secret key, batched inserts,
offline queue at state/queue.jsonl, flush on reconnect. Resilience per the
supabase research brief: API-level failures raise postgrest ``APIError``,
network failures raise raw httpx exceptions — both are caught broadly, rows go
to the queue, nothing ever kills the main loop, and a queued row is always
reported as queued, never as written.

Rows queued by out-of-process writers (tools/post_event.py) may carry
``session_id: null``; the harness fills its live session id at flush time.
Rows that still have no session id when no session is known stay queued —
inserting them would violate the schema, and inventing a session row for them
would put untrue data on the site.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from postgrest.exceptions import APIError

from .logsetup import get_logger
from .settings import Settings

log = get_logger("wasted.events")

# CONTRACTS §4 — closed enum; adding a type bumps the contract version.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "death",
        "busted",
        "mission_start",
        "mission_end",
        "mission_fail",
        "wanted_change",
        "stunt",
        "clip",
        "break",
        "governor_level",
        "bridge_down",
        "bridge_up",
        "unstick",
        "activity_start",
        "activity_end",
        "session_start",
        "session_end",
    }
)

MAX_BATCH = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseWriter:
    """All harness → Supabase writes go through this. One instance per process."""

    def __init__(self, settings: Settings, session_id: str | None = None) -> None:
        self._settings = settings
        self.session_id = session_id
        self.queue_path = settings.ensure_state_dir() / "queue.jsonl"
        self._client: Any = None
        self._buffer: list[dict[str, Any]] = []

    # -- config / client -------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self._settings.supabase_configured

    def _get_client(self) -> Any:
        if self._client is None:
            from supabase import create_client  # deferred: import cost + optional path

            assert self._settings.supabase_url and self._settings.supabase_secret_key
            self._client = create_client(
                self._settings.supabase_url, self._settings.supabase_secret_key
            )
        return self._client

    # -- public write API ------------------------------------------------------

    def record_event(
        self,
        type_: str,
        payload: dict[str, Any],
        screenshot_url: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Buffer one §4 event for the next flush. Validates the closed enum."""
        if type_ not in EVENT_TYPES:
            raise ValueError(
                f"{type_!r} is not a CONTRACTS §4 event type (known: {sorted(EVENT_TYPES)})"
            )
        row: dict[str, Any] = {
            "session_id": session_id if session_id is not None else self.session_id,
            "ts": _now_iso(),
            "type": type_,
            "payload": payload,
        }
        if screenshot_url is not None:
            row["screenshot_url"] = screenshot_url
        self._buffer.append({"table": "events", "op": "insert", "row": row})
        if len(self._buffer) >= MAX_BATCH:
            self.flush()

    def record_decision(self, row: dict[str, Any]) -> None:
        row.setdefault("session_id", self.session_id)
        row.setdefault("ts", _now_iso())
        self._buffer.append({"table": "decisions", "op": "insert", "row": row})

    def upsert_stats(self, row: dict[str, Any]) -> None:
        row.setdefault("session_id", self.session_id)
        self._buffer.append({"table": "stats", "op": "upsert", "row": row})

    def insert_session(self, row: dict[str, Any]) -> None:
        self._buffer.append({"table": "sessions", "op": "insert", "row": row})

    def insert_clip(self, row: dict[str, Any]) -> int | None:
        """Insert a clips row immediately; returns its id, or None when offline
        (row queued; the dependent §4 `clip` event is the caller's call)."""
        row.setdefault("session_id", self.session_id)
        row.setdefault("ts", _now_iso())
        entry = {"table": "clips", "op": "insert", "row": row}
        if not self.configured:
            self._enqueue([entry], reason="supabase not configured")
            return None
        try:
            resp = self._get_client().table("clips").insert(row).execute()
            return int(resp.data[0]["id"])
        except Exception as exc:  # broad by design: writer must never kill the loop
            self._log_write_failure("clips insert", exc)
            self._enqueue([entry], reason=type(exc).__name__)
            return None

    # -- flushing --------------------------------------------------------------

    def flush(self) -> bool:
        """Attempt to write everything buffered (+ any queued backlog).

        Returns True when the buffer is fully drained to Supabase; False when
        rows went to (or stayed in) the offline queue.
        """
        buffered, self._buffer = self._buffer, []
        if not self.configured:
            if buffered:
                self._enqueue(buffered, reason="supabase not configured")
            return False
        # Reconnect path: backlog first so ordering roughly holds.
        backlog = self._read_queue()
        pending = backlog + buffered
        if not pending:
            return True
        ok, still_pending = self._write_entries(pending)
        self._rewrite_queue(still_pending)
        return ok and not still_pending

    def _write_entries(
        self, entries: list[dict[str, Any]]
    ) -> tuple[bool, list[dict[str, Any]]]:
        client = None
        try:
            client = self._get_client()
        except Exception as exc:  # bad URL/key etc. — queue, report loudly
            self._log_write_failure("client init", exc)
            return False, entries

        remaining: list[dict[str, Any]] = []
        ok = True
        for table, op in _grouping(entries):
            group = [e for e in entries if e["table"] == table and e["op"] == op]
            rows = []
            skipped = []
            for e in group:
                row = dict(e["row"])
                if table != "sessions" and row.get("session_id") is None:
                    if self.session_id is not None:
                        row["session_id"] = self.session_id
                    else:
                        skipped.append(e)  # cannot satisfy NOT NULL honestly yet
                        continue
                rows.append(row)
            if skipped:
                log.warning(
                    "rows kept queued: no session id known yet",
                    extra={"kv": {"table": table, "count": len(skipped)}},
                )
                remaining.extend(skipped)
            if not rows:
                continue
            try:
                if op == "upsert":
                    # stats: one row per session, PK conflict target (§5)
                    for row in rows:
                        client.table(table).upsert(row, returning="minimal").execute()
                else:
                    for i in range(0, len(rows), MAX_BATCH):
                        client.table(table).insert(rows[i : i + MAX_BATCH]).execute()
                log.info(
                    "flushed to supabase",
                    extra={"kv": {"table": table, "op": op, "rows": len(rows)}},
                )
            except (httpx.HTTPError, APIError, OSError) as exc:
                self._log_write_failure(f"{table} {op}", exc)
                remaining.extend(
                    e for e in group if e not in skipped
                )
                ok = False
            except Exception as exc:  # unexpected — still never kill the loop
                self._log_write_failure(f"{table} {op} (unexpected)", exc)
                remaining.extend(e for e in group if e not in skipped)
                ok = False
        return ok, remaining

    # -- queue -----------------------------------------------------------------

    def _enqueue(self, entries: list[dict[str, Any]], reason: str) -> None:
        with self.queue_path.open("a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        log.warning(
            "queued offline",
            extra={
                "kv": {
                    "rows": len(entries),
                    "reason": reason,
                    "queue": str(self.queue_path),
                }
            },
        )

    def _read_queue(self) -> list[dict[str, Any]]:
        if not self.queue_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.queue_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                log.error("dropping corrupt queue line", extra={"kv": {"line": line[:120]}})
        return entries

    def _rewrite_queue(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            self.queue_path.unlink(missing_ok=True)
            return
        fd, tmp = tempfile.mkstemp(dir=str(self.queue_path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, self.queue_path)

    def queue_depth(self) -> int:
        return len(self._read_queue())

    # -- storage ---------------------------------------------------------------

    def upload_screenshot(self, jpeg: bytes, name_hint: str) -> str | None:
        """Upload to the `shots` bucket at a new timestamped path; returns the
        public URL or None on failure (screenshots are best-effort)."""
        if not self.configured:
            return None
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = f"{self.session_id or 'nosession'}/{ts}-{name_hint}.jpg"
        try:
            storage = self._get_client().storage.from_("shots")
            storage.upload(
                path=path,
                file=jpeg,
                file_options={"content-type": "image/jpeg", "cache-control": "3600"},
            )
            return str(storage.get_public_url(path))
        except Exception as exc:
            self._log_write_failure("shots upload", exc)
            return None

    # -- misc ------------------------------------------------------------------

    @staticmethod
    def _log_write_failure(what: str, exc: Exception) -> None:
        log.warning(
            "supabase write failed",
            extra={"kv": {"what": what, "error": f"{type(exc).__name__}: {exc}"[:200]}},
        )


def _grouping(entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for e in entries:
        key = (e["table"], e["op"])
        if key not in seen:
            seen.append(key)
    return seen
