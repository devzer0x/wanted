#!/usr/bin/env python3
"""Poll ``GET /state`` at 5 Hz and append one JSON line per tick to a `.jsonl` file.

This runs on the game server, alongside the harness (CLAUDE.md "Where things
run": nothing game-adjacent can be *verified* anywhere else). It is
deliberately independent of the harness process — no import of
``wasted_harness.main``, no shared state — so it can run before, during, or
instead of the harness (e.g. to capture a session while someone plays
manually, for a fixture nobody has generated yet).

Each output line is a JSON object:

    {"ts": "<recorder wall-clock ISO-8601 UTC>", "state": {<raw /state body>}}

``ts`` is the RECORDER's own receipt time (used by
``tests/support/replayer.py::replay_from_jsonl`` to pace the replay); the
bridge's own ``ts`` field travels untouched inside ``state``. The body is
written EXACTLY as the bridge sent it — parsed once, with
``wasted_harness.bridge_client.GameState``, only to catch and log a
contract-shape mismatch loudly; the raw JSON (not the re-serialized model) is
what is written, so a field the harness does not yet know about survives in
the recording rather than being silently dropped.

**Robust to the bridge being unreachable.** A connection failure, a timeout, or
a v1.2 transient 503 (`not_ready` / `game_thread_stalled` / `queue_full`) is
logged and the poll loop continues — the recorder's whole point is to be
running before anyone remembers to check the game is actually up, and a
launch/loading screen is a completely normal thing to be recording through.
An `online_session_active` 503 is different: STORY MODE ONLY (CLAUDE.md rule
5) is the same rule here as everywhere else, so the recorder refuses to record
a network session's state and waits for it to clear rather than quietly
recording something this project must not touch.

Usage::

    python harness/tools/record_state.py --out session.states.jsonl
    python harness/tools/record_state.py --out session.states.jsonl --hz 5 \\
        --bridge-url http://127.0.0.1:7777 --max-seconds 1200

``--from-log`` does NOT reconstruct a `/state` stream — see
:func:`synthesize_from_harness_log`'s docstring for exactly why, and what it
produces instead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Runs standalone (no `wasted_harness` install required beyond what the
# harness venv already provides) — but the harness package IS what parses and
# validates a `/state` body, and re-deriving that here would be exactly the
# kind of drifting second copy CLAUDE.md rule 6 warns about. So it imports the
# harness package rather than talking raw HTTP + json.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wasted_harness.bridge_client import (
    BridgeApiError,
    BridgeClient,
    BridgeDownError,
    BridgeTransientError,
    GameState,
    OnlineSessionActiveError,
)

DEFAULT_HZ = 5.0
DEFAULT_BRIDGE_URL = "http://127.0.0.1:7777"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record(
    out_path: Path,
    *,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    hz: float = DEFAULT_HZ,
    max_seconds: float | None = None,
    stop_after: int | None = None,
) -> int:
    """Poll and append. Returns the number of lines written. Never raises on a
    down/not-ready bridge — only on being unable to WRITE the output file."""
    interval = 1.0 / max(0.1, hz)
    bridge = BridgeClient(bridge_url, timeout_s=2.0, connect_retries=0)
    written = 0
    consecutive_failures = 0
    started = time.monotonic()
    print(f"[record_state] polling {bridge_url} at {hz} Hz -> {out_path}", file=sys.stderr)
    try:
        with out_path.open("a", encoding="utf-8") as fh:
            while True:
                loop_started = time.monotonic()
                try:
                    state: GameState = bridge.get_state()
                except OnlineSessionActiveError:
                    # CLAUDE.md rule 5: story mode only. Not logged as an error
                    # (it is the safety rail working), but loudly enough that
                    # nobody wonders why the file stopped growing.
                    print(
                        "[record_state] online session active; refusing to record "
                        "until it clears (Story Mode only)",
                        file=sys.stderr,
                    )
                    consecutive_failures += 1
                except BridgeTransientError as exc:
                    print(f"[record_state] bridge not ready ({exc.error}); retrying", file=sys.stderr)
                    consecutive_failures += 1
                except BridgeDownError as exc:
                    print(f"[record_state] bridge unreachable: {exc}; retrying", file=sys.stderr)
                    consecutive_failures += 1
                except BridgeApiError as exc:
                    print(
                        f"[record_state] bridge rejected /state ({exc.status} {exc.error}); "
                        f"retrying",
                        file=sys.stderr,
                    )
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                    line = {"ts": _now_iso(), "state": json.loads(state.model_dump_json(by_alias=True))}
                    fh.write(json.dumps(line, sort_keys=True) + "\n")
                    fh.flush()
                    written += 1
                    if written % (int(hz) * 30 or 1) == 0:
                        print(f"[record_state] {written} lines written", file=sys.stderr)

                if stop_after is not None and written >= stop_after:
                    break
                if max_seconds is not None and (time.monotonic() - started) >= max_seconds:
                    break
                elapsed = time.monotonic() - loop_started
                time.sleep(max(0.0, interval - elapsed))
    finally:
        bridge.close()
    print(f"[record_state] done: {written} lines -> {out_path}", file=sys.stderr)
    return written


# --------------------------------------------------------------------------- #
#  --from-log                                                                 #
# --------------------------------------------------------------------------- #

#: `logsetup.LogfmtFormatter` line shape: `ts=... level=... logger=... msg="..." k=v ...`.
_LOGFMT_LINE = re.compile(r'^ts=(?P<ts>\S+)\s+level=(?P<level>\S+)\s+logger=(?P<logger>\S+)\s+msg="(?P<msg>[^"]*)"\s*(?P<kv>.*)$')
_KV_PAIR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def _parse_kv(rest: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _KV_PAIR.findall(rest)}


def synthesize_from_harness_log(log_path: Path, out_path: Path) -> int:
    """Extract whatever a harness run log actually carries. NOT a `/state` stream.

    Read `wasted_harness/logsetup.py` before trusting this function with
    anything: every line the harness prints is
    ``ts=<iso> level=<LVL> logger=<name> msg="<text>" key=value ...``
    (`LogfmtFormatter`) — key/value pairs from whatever a caller passed as
    ``extra={"kv": {...}}``. Nothing in `main.py` ever logs a raw `/state`
    body (grepped for it before writing this docstring: it is not there), so
    there is **no way to recover player position, vehicle, mission state, or
    nearby entities from a harness log** — those fields simply were never
    printed, on purpose (a `/state` snapshot is dozens of fields at 3 Hz; the
    log is for the 3am operator, not for reconstructing gameplay).

    What IS recoverable, and what this writes instead — one JSON object per
    parseable log line, honestly labelled as log-derived rather than as
    state: ``{"ts": ..., "logger": ..., "level": ..., "msg": ..., "kv": {...}}``
    for every line the log format above can be parsed out of. This is useful
    to `tools/funcheck.py` for cross-checking commentary cadence (F2) and
    event timing against what the harness itself reported, but it is NOT a
    `.states.jsonl` file and `replay_from_jsonl` cannot consume it — replaying
    requires the actual game states, which only `record_state.py`'s own
    `--out` path (against a live bridge) or a real recording captures.
    """
    print(
        "[record_state] --from-log does NOT reconstruct a /state stream: harness "
        "run logs never carry a raw /state body (verified against logsetup.py and "
        "every log.* call site in main.py before this tool was written). Writing "
        "the log-derived events that ARE recoverable instead; do not pass the "
        "result to replay_from_jsonl.",
        file=sys.stderr,
    )
    written = 0
    with log_path.open("r", encoding="utf-8", errors="replace") as src, out_path.open(
        "w", encoding="utf-8"
    ) as dst:
        for line in src:
            m = _LOGFMT_LINE.match(line.rstrip("\n"))
            if not m:
                continue
            row: dict[str, Any] = {
                "ts": m.group("ts"),
                "level": m.group("level"),
                "logger": m.group("logger"),
                "msg": m.group("msg"),
                "kv": _parse_kv(m.group("kv")),
            }
            dst.write(json.dumps(row, sort_keys=True) + "\n")
            written += 1
    print(f"[record_state] {written} log-derived lines -> {out_path}", file=sys.stderr)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help="output .jsonl path (appended to)")
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    parser.add_argument("--hz", type=float, default=DEFAULT_HZ)
    parser.add_argument("--max-seconds", type=float, default=None, help="stop after this many wall seconds")
    parser.add_argument("--stop-after", type=int, default=None, help="stop after this many lines (mostly for tests)")
    parser.add_argument(
        "--from-log", type=Path, default=None,
        help="synthesize log-derived events (NOT a /state stream — see synthesize_from_harness_log) from an existing harness run log",
    )
    args = parser.parse_args(argv)

    if args.from_log is not None:
        if args.out is None:
            parser.error("--from-log requires --out")
        synthesize_from_harness_log(args.from_log, args.out)
        return 0

    if args.out is None:
        parser.error("--out is required")
    record(
        args.out,
        bridge_url=args.bridge_url,
        hz=args.hz,
        max_seconds=args.max_seconds,
        stop_after=args.stop_after,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
