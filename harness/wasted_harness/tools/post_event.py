"""Watchdog hook: post one §4 event from the command line.

    python -m wasted_harness.tools.post_event --type bridge_down --payload '{}'

Behavior, honestly:
- Supabase configured + reachable → the event row is inserted now (using the
  harness's current session id from state/current_session.json when present).
- Supabase absent/unreachable → the event is queued to state/queue.jsonl and
  the path is printed; the harness flushes it on reconnect. Queued rows without
  a known session id are back-filled by the harness at flush time.

Exit codes: 0 = written or queued; 2 = invalid arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..events import EVENT_TYPES, SupabaseWriter
from ..logsetup import get_logger, setup_logging
from ..settings import Settings

log = get_logger("wasted.post_event")


def read_current_session_id(state_dir: Path) -> str | None:
    path = state_dir / "current_session.json"
    if not path.exists():
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["session_id"])
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m wasted_harness.tools.post_event",
        description="Post one CONTRACTS §4 event (queues offline when Supabase is absent).",
    )
    parser.add_argument("--type", required=True, help=f"one of: {', '.join(sorted(EVENT_TYPES))}")
    parser.add_argument("--payload", default="{}", help="JSON object payload")
    parser.add_argument("--screenshot-url", default=None)
    args = parser.parse_args(argv)

    if args.type not in EVENT_TYPES:
        print(
            f"error: {args.type!r} is not a §4 event type. "
            f"Known: {', '.join(sorted(EVENT_TYPES))}",
            file=sys.stderr,
        )
        return 2
    try:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
    except ValueError as exc:
        print(f"error: --payload is not a JSON object: {exc}", file=sys.stderr)
        return 2

    settings = Settings.load()
    session_id = read_current_session_id(settings.ensure_state_dir())
    writer = SupabaseWriter(settings, session_id=session_id)
    writer.record_event(args.type, payload, screenshot_url=args.screenshot_url)
    flushed = writer.flush()

    if flushed:
        print(f"event {args.type!r} written to Supabase (session {session_id or 'unknown'})")
    else:
        reason = (
            "Supabase is not configured (SUPABASE_URL / SUPABASE_SECRET_KEY unset)"
            if not writer.configured
            else "Supabase write failed"
        )
        print(f"event {args.type!r} queued offline: {reason}")
        print(f"queue file: {writer.queue_path} (depth now {writer.queue_depth()})")
        print("the harness flushes this queue automatically on reconnect")
    return 0


if __name__ == "__main__":
    sys.exit(main())
