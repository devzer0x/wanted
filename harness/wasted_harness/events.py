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

Concurrency: the queue file has two writers — the harness loop and the
watchdog's ``tools/post_event`` CLI. On Windows a file opened by another
process has no FILE_SHARE_DELETE, so ``os.replace`` onto it fails with a
sharing violation; an unguarded rewrite would raise inside ``flush()`` and take
the main loop down while dropping every pending row. Every queue operation
therefore runs under a cross-process advisory lock (msvcrt.locking on Windows,
fcntl.flock elsewhere) plus an in-process lock, and the rewrite retries.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
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

#: §4 types nothing in this package emits yet, each with the reason. Declaring
#: them here is what stops a trigger set, a mood rule or a vision rule from
#: silently waiting for an event that never arrives; every such set is built by
#: subtracting these, so wiring one up is a one-line change in this file.
UNPRODUCED_EVENT_REASONS: dict[str, str] = {
    "stunt": (
        "needs airtime_s, and /state v1.2 exposes no on-ground flag and no "
        "vertical velocity — at a 2-4 Hz poll a large z delta is equally a "
        "jump, a hill, a car-park ramp or a lift, so a detector built on the "
        "documented fields would be inventing the number it reports. Needs a "
        "bridge-side field (a contract change): Phase 3, harness/README.md."
    ),
    "mission_end": (
        "outcome detection (the passed/failed screen) is Phase 4; "
        "behavior/missions.py is the flag-driven skeleton and emits only "
        "mission_start, because emitting an outcome it cannot see would be a "
        "fabrication."
    ),
    "mission_fail": "same as mission_end — Phase 4 outcome detection.",
}
UNPRODUCED_EVENT_TYPES: frozenset[str] = frozenset(UNPRODUCED_EVENT_REASONS)

#: The §4 types the harness really does emit. Every trigger set is built from
#: this, never from EVENT_TYPES.
EMITTED_EVENT_TYPES: frozenset[str] = EVENT_TYPES - UNPRODUCED_EVENT_TYPES

MAX_BATCH = 20

# A Supabase outage must not fill the server's disk. At ~1 KB/row this caps the
# backlog around 40 MB; past it the OLDEST rows are dropped (loudly) so the most
# recent hours of the show survive.
MAX_QUEUED_ROWS = 40_000
#: Byte tripwire so the common (small) queue is never fully parsed on append.
MAX_QUEUE_BYTES = 40 * 1024 * 1024

_REPLACE_RETRIES = 10
_REPLACE_DELAY_S = 0.15


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@contextlib.contextmanager
def _queue_file_lock(lock_path: Path) -> Iterator[None]:
    """Advisory cross-process lock around queue mutations. Best effort.

    Failing to acquire is logged and the operation proceeds — losing the lock is
    strictly better than losing the event, and the rewrite path retries anyway.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    locked = False
    try:
        handle = lock_path.open("a+b")
        if os.name == "nt":
            import msvcrt

            for _ in range(_REPLACE_RETRIES):
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(_REPLACE_DELAY_S)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        if not locked:
            log.warning(
                "queue lock not acquired; proceeding unlocked",
                extra={"kv": {"lock": str(lock_path)}},
            )
        yield
    finally:
        if handle is not None:
            try:
                if locked and os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                elif locked:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


class SupabaseWriter:
    """All harness → Supabase writes go through this. One instance per process."""

    def __init__(self, settings: Settings, session_id: str | None = None) -> None:
        self._settings = settings
        self.session_id = session_id
        self.queue_path = settings.ensure_state_dir() / "queue.jsonl"
        self.lock_path = self.queue_path.with_suffix(".lock")
        self._client: Any = None
        self._buffer: list[dict[str, Any]] = []
        # The clip pipeline writes from a worker thread; the loop writes from
        # the main thread. Guards the buffer swap, not the network call.
        self._buffer_lock = threading.Lock()

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
        with self._buffer_lock:
            self._buffer.append({"table": "events", "op": "insert", "row": row})
            full = len(self._buffer) >= MAX_BATCH
        if full:
            self.flush()

    def record_decision(self, row: dict[str, Any]) -> None:
        row.setdefault("session_id", self.session_id)
        row.setdefault("ts", _now_iso())
        with self._buffer_lock:
            self._buffer.append({"table": "decisions", "op": "insert", "row": row})

    def upsert_stats(self, row: dict[str, Any]) -> None:
        row.setdefault("session_id", self.session_id)
        with self._buffer_lock:
            self._buffer.append({"table": "stats", "op": "upsert", "row": row})

    def insert_session(self, row: dict[str, Any]) -> None:
        with self._buffer_lock:
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
        with self._buffer_lock:
            buffered, self._buffer = self._buffer, []
        if not self.configured:
            if buffered:
                self._enqueue(buffered, reason="supabase not configured")
            return False
        still_pending: list[dict[str, Any]] = list(buffered)
        try:
            # Reconnect path: backlog first so ordering roughly holds. The whole
            # read → write → rewrite cycle is one critical section so a
            # concurrent post_event CLI cannot lose rows between the read and
            # the rewrite.
            with _queue_file_lock(self.lock_path):
                backlog = self._read_queue()
                pending = backlog + buffered
                if not pending:
                    return True
                ok, still_pending = self._write_entries(pending)
                self._rewrite_queue(still_pending)
            return ok and not still_pending
        except OSError as exc:
            # Disk full / permission / sharing violation: hold everything still
            # unwritten in the in-process buffer rather than dropping it, and
            # never raise into the main loop.
            self._log_write_failure("queue maintenance", exc)
            with self._buffer_lock:
                self._buffer = still_pending + self._buffer
            return False

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
        try:
            with _queue_file_lock(self.lock_path):
                with self.queue_path.open("a", encoding="utf-8") as f:
                    for e in entries:
                        f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
                self._trim_queue_if_oversized()
        except OSError as exc:
            self._log_write_failure("offline queue append", exc)
            return
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
        for raw in self.queue_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                log.error("dropping corrupt queue line", extra={"kv": {"line": line[:120]}})
        return entries

    def _trim_queue_if_oversized(self) -> None:
        """Drop the OLDEST rows once the backlog passes either cap.

        A multi-day Supabase outage must not fill the server's disk; the most
        recent hours of the show are the ones worth keeping. The cheap byte
        check runs first so a small queue is never fully parsed on append.
        """
        try:
            size = self.queue_path.stat().st_size
        except FileNotFoundError:
            return
        if size < MAX_QUEUE_BYTES:
            return
        entries = self._read_queue()
        keep: list[dict[str, Any]] = []
        budget = MAX_QUEUE_BYTES
        for entry in reversed(entries):  # newest first
            cost = len(json.dumps(entry, ensure_ascii=False, default=str)) + 1
            if cost > budget or len(keep) >= MAX_QUEUED_ROWS:
                break
            budget -= cost
            keep.append(entry)
        keep.reverse()
        dropped = len(entries) - len(keep)
        if dropped <= 0:
            return
        log.error(
            "offline queue over cap; dropping oldest rows",
            extra={
                "kv": {
                    "dropped": dropped,
                    "kept": len(keep),
                    "row_cap": MAX_QUEUED_ROWS,
                    "byte_cap": MAX_QUEUE_BYTES,
                    "queue": str(self.queue_path),
                }
            },
        )
        self._rewrite_queue(keep)

    def _rewrite_queue(self, entries: list[dict[str, Any]]) -> None:
        """Atomically replace the queue file. Retries on Windows sharing violations."""
        if not entries:
            for attempt in range(_REPLACE_RETRIES):
                try:
                    self.queue_path.unlink(missing_ok=True)
                    return
                except PermissionError:
                    if attempt == _REPLACE_RETRIES - 1:
                        raise
                    time.sleep(_REPLACE_DELAY_S)
            return
        fd, tmp = tempfile.mkstemp(dir=str(self.queue_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
            for attempt in range(_REPLACE_RETRIES):
                try:
                    os.replace(tmp, self.queue_path)
                    return
                except PermissionError:
                    # Another process (the watchdog CLI) has the queue open;
                    # Windows refuses MoveFileEx without FILE_SHARE_DELETE.
                    if attempt == _REPLACE_RETRIES - 1:
                        raise
                    time.sleep(_REPLACE_DELAY_S)
        finally:
            if os.path.exists(tmp):
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def queue_depth(self) -> int:
        try:
            return len(self._read_queue())
        except OSError as exc:
            self._log_write_failure("queue depth", exc)
            return -1

    # -- storage ---------------------------------------------------------------

    def storage_path_for(self, name_hint: str, suffix: str) -> str:
        """A NEW timestamped object path (§5: uploads never overwrite)."""
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{self.session_id or 'nosession'}/{ts}-{name_hint}{suffix}"

    def upload_bytes(
        self, bucket: str, path: str, data: bytes, content_type: str
    ) -> str | None:
        """Upload one object with an explicit content type; returns its public URL.

        Raises nothing: storage is best-effort everywhere it is used, and the
        caller decides what a missing URL means.
        """
        if not self.configured:
            return None
        try:
            storage = self._get_client().storage.from_(bucket)
            storage.upload(
                path=path,
                file=data,
                file_options={"content-type": content_type, "cache-control": "3600"},
            )
            return str(storage.get_public_url(path))
        except Exception as exc:  # broad by design: storage never kills the loop
            self._log_write_failure(f"{bucket} upload", exc)
            return None

    def upload_screenshot(self, jpeg: bytes, name_hint: str) -> str | None:
        """Upload to the `shots` bucket at a new timestamped path; returns the
        public URL or None on failure (screenshots are best-effort)."""
        return self.upload_bytes(
            "shots", self.storage_path_for(name_hint, ".jpg"), jpeg, "image/jpeg"
        )

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
