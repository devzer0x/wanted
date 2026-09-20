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
from .totals import summarise_lifetime_seed

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
}
UNPRODUCED_EVENT_TYPES: frozenset[str] = frozenset(UNPRODUCED_EVENT_REASONS)

#: The §4 types the harness really does emit. Every trigger set is built from
#: this, never from EVENT_TYPES.
EMITTED_EVENT_TYPES: frozenset[str] = EVENT_TYPES - UNPRODUCED_EVENT_TYPES

MAX_BATCH = 20

#: The one table whose write failures the caller has to be able to SEE.
#:
#: Every other table here fails the same way and it is survivable: the row goes
#: to the offline queue, the queue drains when Supabase comes back, and nobody
#: was waiting on it. A `predictions` row is different in two ways. It is
#: REJECTED rather than merely undeliverable when the prediction migrations are
#: not applied to that project, or a CHECK fails, or RLS refuses the write —
#: and a rejection never heals, so the row is retried from the disk queue on
#: every flush, forever, on the game-loop thread. And the layer that produced
#: it has a circuit breaker (`main.PREDICTION_FAILURE_LIMIT`) that existed but
#: could never fire, because `flush()` catches the failure here and reports
#: `False` for reasons that have nothing to do with predictions.
#:
#: So this writer counts CONSECUTIVE flushes in which a `predictions` run
#: failed, says the table's name and the server's own message out loud (rate
#: limited), and lets the breaker read the count.
PREDICTIONS_TABLE = "predictions"
#: At most one ERROR per this many seconds. A rejected insert is retried on
#: every flush (2 s), so an unlimited log would be ~1,800 identical lines an
#: hour in the file the 3am operator has to read.
PREDICTIONS_ERROR_LOG_INTERVAL_S = 60.0

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
        #: True when the last flush left rows unwritten (buffer or offline queue).
        #: Read by `Harness._heartbeat`, which withholds the heartbeat while it is
        #: set: `settle_due_predictions()` treats `stats.heartbeat_at >= resolves_at`
        #: as proof the writer caught up past the window (CONTRACTS-PREDICTIONS §3),
        #: and that inference is only sound if a heartbeat cannot outrun the events
        #: it implies. Starts False: nothing has failed yet, and the first flush
        #: sets it honestly either way.
        self.unflushed = False
        #: Consecutive flushes in which a `predictions` run failed. Reset to 0
        #: by any flush that actually lands one. Read by
        #: `main.Harness._offer_prediction`'s circuit breaker — see
        #: PREDICTIONS_TABLE above for why that could not work without this.
        self.predictions_write_failures = 0
        #: The server's own words for the last predictions rejection, for the
        #: operator and for the breaker's log line. Never parsed.
        self.last_predictions_error: str | None = None
        self._last_predictions_error_log_at = float("-inf")
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
        """Buffer the heartbeat row. At most ONE per session is ever pending.

        Every stats row carries the full absolute values, so an older pending
        one has nothing in it the newer one does not. Appending them instead
        cost ~720 dead rows per hour of outage, all replayed one HTTP round
        trip at a time on the loop thread, and they ate the queue cap that
        exists to protect the event history.
        """
        row.setdefault("session_id", self.session_id)
        entry = {"table": "stats", "op": "upsert", "row": row}
        with self._buffer_lock:
            for i, pending in enumerate(self._buffer):
                if (
                    pending["table"] == "stats"
                    and pending["op"] == "upsert"
                    and pending["row"].get("session_id") == row.get("session_id")
                ):
                    self._buffer[i] = entry
                    return
            self._buffer.append(entry)

    def insert_session(self, row: dict[str, Any]) -> None:
        """Buffer the session row AT THE FRONT: every other table references
        sessions(id), so anything already buffered for this session (a governor
        level carried over from the last process, a startup event) would FK-fail
        if it went out first, requeue the whole run, and only land a flush later.
        """
        with self._buffer_lock:
            self._buffer.insert(0, {"table": "sessions", "op": "insert", "row": row})

    def update_session_end(self, session_id: str, ended_at: str) -> None:
        """Buffer `sessions.ended_at` for a clean shutdown (CONTRACTS §5).

        Nothing ever wrote this column, so every session that has ever run reads
        as still live — a crash and a clean stop were indistinguishable in the
        one table designed to tell them apart. The timestamp is computed here
        rather than left to the database's `now()` so a row replayed hours later
        still carries the moment the show actually ended.
        """
        with self._buffer_lock:
            self._buffer.append(
                {
                    "table": "sessions",
                    "op": "update",
                    "row": {"ended_at": ended_at},
                    "match": {"id": session_id},
                }
            )

    def insert_event_now(
        self,
        type_: str,
        payload: dict[str, Any],
        screenshot_url: str | None = None,
    ) -> int | None:
        """Insert ONE event immediately and return its id, or None when it could
        not be written (it is then queued exactly like any other event).

        Used only for the events a clip is cut from: `clips.event_id` was always
        NULL because batched inserts use `returning="minimal"`, so no clip could
        ever be linked back to the death or bust that produced it and the
        `clips_event_idx` index was dead. Deaths and busts are rare, so the extra
        round trip is bounded; the caller only takes this path when the clip
        pipeline is actually connected.
        """
        if type_ not in EVENT_TYPES:
            raise ValueError(
                f"{type_!r} is not a CONTRACTS §4 event type (known: {sorted(EVENT_TYPES)})"
            )
        row: dict[str, Any] = {
            "session_id": self.session_id,
            "ts": _now_iso(),
            "type": type_,
            "payload": payload,
        }
        if screenshot_url is not None:
            row["screenshot_url"] = screenshot_url
        entry = {"table": "events", "op": "insert", "row": row}
        if not self.configured or self.session_id is None:
            self._enqueue([entry], reason="supabase not configured")
            return None
        try:
            resp = self._get_client().table("events").insert(row).execute()
            return int(resp.data[0]["id"])
        except Exception as exc:  # broad by design: writer must never kill the loop
            self._log_write_failure("events insert", exc)
            self._enqueue([entry], reason=type(exc).__name__)
            return None

    def insert_mission(self, row: dict[str, Any]) -> None:
        row.setdefault("session_id", self.session_id)
        with self._buffer_lock:
            self._buffer.append({"table": "missions", "op": "insert", "row": row})

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
        rows went to (or stayed in) the offline queue. Either way it records the
        answer in `self.unflushed` — see that attribute for why the prediction
        settlement gate depends on it.
        """
        drained = self._flush_once()
        self.unflushed = not drained
        return drained

    def _flush_once(self) -> bool:
        with self._buffer_lock:
            buffered, self._buffer = self._buffer, []
        if not self.configured:
            if buffered:
                self._enqueue(_coalesce_stats(buffered), reason="supabase not configured")
            return False
        still_pending: list[dict[str, Any]] = list(buffered)
        try:
            # Reconnect path: backlog first so ordering roughly holds. The whole
            # read → write → rewrite cycle is one critical section so a
            # concurrent post_event CLI cannot lose rows between the read and
            # the rewrite.
            with _queue_file_lock(self.lock_path):
                backlog = self._read_queue()
                # An outage accumulates one stats row per heartbeat; all but the
                # newest per session are dead weight (each carries the full
                # absolute counters), and replaying them costs one HTTP round
                # trip each on this thread. Collapsing here also stops them
                # crowding the event history out of the queue cap.
                pending = _coalesce_stats(backlog + buffered)
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
        predictions_failed = False
        predictions_written = 0
        # `stats` carries `heartbeat_at`, which settlement reads as "the writer has
        # caught up past this moment". Each (table, op) run is a SEPARATE request, so
        # a failed `events` run followed by a successful `stats` run in the same flush
        # publishes that claim while the evidence behind it is still on disk — and a
        # prediction window can then settle on absent telemetry (resolving
        # `survives_window` as SURVIVED for a death still sitting in the queue).
        # So: heartbeats go last, and are skipped entirely once anything has failed.
        runs = list(_runs(entries))
        runs.sort(key=lambda run: run[0] == "stats")
        for table, op, group in runs:
            if table == "stats" and not ok:
                log.warning(
                    "heartbeat held back: earlier rows did not land",
                    extra={"kv": {"queued": len(remaining)}},
                )
                remaining.extend(group)
                continue
            pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
            skipped = []
            for e in group:
                row = dict(e["row"])
                if table != "sessions" and row.get("session_id") is None:
                    if self.session_id is not None:
                        row["session_id"] = self.session_id
                    else:
                        skipped.append(e)  # cannot satisfy NOT NULL honestly yet
                        continue
                pairs.append((e, row))
            if skipped:
                log.warning(
                    "rows kept queued: no session id known yet",
                    extra={"kv": {"table": table, "count": len(skipped)}},
                )
                remaining.extend(skipped)
            if not pairs:
                continue
            # How many of this run actually landed. Requeueing the whole run on a
            # late failure is what duplicated deaths and commentary lines on the
            # public feed: events/decisions are `generated always as identity`
            # with no natural key and no ON CONFLICT, so a re-inserted row is a
            # genuinely new row, not an idempotent write.
            written = 0
            try:
                if op == "upsert":
                    # stats: one row per session, PK conflict target (§5)
                    for _entry, row in pairs:
                        client.table(table).upsert(row, returning="minimal").execute()
                        written += 1
                elif op == "update":
                    for entry, row in pairs:
                        query = client.table(table).update(row, returning="minimal")
                        for column, value in (entry.get("match") or {}).items():
                            query = query.eq(column, value)
                        query.execute()
                        written += 1
                else:
                    for i in range(0, len(pairs), MAX_BATCH):
                        chunk = pairs[i : i + MAX_BATCH]
                        client.table(table).insert([row for _e, row in chunk]).execute()
                        written += len(chunk)
                log.info(
                    "flushed to supabase",
                    extra={"kv": {"table": table, "op": op, "rows": written}},
                )
            except (httpx.HTTPError, APIError, OSError) as exc:
                self._log_write_failure(f"{table} {op}", exc)
                remaining.extend(entry for entry, _row in pairs[written:])
                ok = False
                if table == PREDICTIONS_TABLE:
                    predictions_failed = True
                    self._note_predictions_failure(exc)
            except Exception as exc:  # unexpected — still never kill the loop
                self._log_write_failure(f"{table} {op} (unexpected)", exc)
                remaining.extend(entry for entry, _row in pairs[written:])
                ok = False
                if table == PREDICTIONS_TABLE:
                    predictions_failed = True
                    self._note_predictions_failure(exc)
            else:
                if table == PREDICTIONS_TABLE:
                    predictions_written += written
        # Consecutive, not cumulative: one flush that lands a prediction row
        # clears the count, because whatever was rejecting them has stopped.
        # A flush carrying no predictions at all changes nothing either way —
        # the agent being quiet is not evidence about the table.
        if predictions_failed:
            self.predictions_write_failures += 1
        elif predictions_written:
            self.predictions_write_failures = 0
            self.last_predictions_error = None
        return ok, remaining

    # -- reads -----------------------------------------------------------------

    def fetch_lifetime_seed(self) -> dict[str, float] | None:
        """Previously published totals, for the ONE-TIME seed of state/lifetime.json.

        Returns None (never zeros) when the read cannot be made, so the caller
        can leave the totals unseeded and try again next start rather than
        freezing an under-reported history in place.
        """
        if not self.configured:
            return None
        try:
            client = self._get_client()
            stats = (
                client.table("stats")
                .select("session_id,deaths,busted,missions_passed,hours_alive")
                .execute()
                .data
                or []
            )
            sessions = client.table("sessions").select("id,harness_version").execute().data or []
        except Exception as exc:  # broad by design: a read must never kill startup
            self._log_write_failure("lifetime seed read", exc)
            return None
        versions = {r.get("id"): r.get("harness_version") for r in sessions}
        return summarise_lifetime_seed(stats, versions)

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
        # Redundant heartbeats first: they are the bulk of a long outage and
        # dropping them costs nothing, whereas dropping events loses history.
        raw = self._read_queue()
        entries = _coalesce_stats(raw)
        superseded = len(raw) - len(entries)
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
        if dropped <= 0 and superseded <= 0:
            return
        log.error(
            "offline queue over cap; dropping oldest rows",
            extra={
                "kv": {
                    "dropped": dropped,
                    "superseded_stats": superseded,
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

    def _note_predictions_failure(self, exc: Exception) -> None:
        """Say, at ERROR, that the PREDICTIONS table refused a row, and why.

        `_log_write_failure` already logged it at WARNING alongside every other
        table's transient trouble, which is exactly the problem: a missing
        column, a failed CHECK or an RLS refusal is not transient, and it
        reads identically to a five-second network blip in the log. This line
        names the table and carries the server's own message, so the operator
        is told which of the two they have.
        """
        detail = getattr(exc, "message", None) or str(exc)
        self.last_predictions_error = f"{type(exc).__name__}: {detail}"[:300]
        now = time.monotonic()
        if now - self._last_predictions_error_log_at < PREDICTIONS_ERROR_LOG_INTERVAL_S:
            return
        self._last_predictions_error_log_at = now
        log.error(
            "supabase REFUSED a predictions row; it will be retried from the offline "
            "queue until this is fixed",
            extra={
                "kv": {
                    "table": PREDICTIONS_TABLE,
                    "server_message": self.last_predictions_error,
                    "consecutive_failed_flushes": self.predictions_write_failures + 1,
                    "queue": str(self.queue_path),
                }
            },
        )

    @staticmethod
    def _log_write_failure(what: str, exc: Exception) -> None:
        log.warning(
            "supabase write failed",
            extra={"kv": {"what": what, "error": f"{type(exc).__name__}: {exc}"[:200]}},
        )


def _runs(
    entries: list[dict[str, Any]],
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """Consecutive entries sharing (table, op), in queue order.

    Deliberately NOT a global group-by. Grouping globally re-ordered the whole
    flush by table — every queued `events` row went in as one block, then every
    `decisions` row — so identity ids stopped following chronology and a
    `sessions` insert could land after the rows that reference it (an FK failure
    for the whole run). Adjacent runs cost a few more round trips on an
    interleaved backlog and buy ids that agree with time.
    """
    runs: list[tuple[str, str, list[dict[str, Any]]]] = []
    for e in entries:
        table, op = e["table"], e["op"]
        if runs and runs[-1][0] == table and runs[-1][1] == op:
            runs[-1][2].append(e)
        else:
            runs.append((table, op, [e]))
    return runs


def _coalesce_stats(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop every stats upsert but the newest one per session, in place order.

    Safe because a stats row carries absolute values, not deltas: the newest row
    contains everything the ones before it said. Everything that is not a stats
    upsert is passed through untouched, in its original position.
    """
    newest: dict[Any, int] = {}
    for i, e in enumerate(entries):
        if e["table"] == "stats" and e["op"] == "upsert":
            newest[e["row"].get("session_id")] = i
    if not newest:
        return entries
    keep = set(newest.values())
    return [
        e
        for i, e in enumerate(entries)
        if not (e["table"] == "stats" and e["op"] == "upsert") or i in keep
    ]
