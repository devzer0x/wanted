#!/usr/bin/env python3
"""The HARNESS half of `verify-rounds.mjs`: the real generator, driven twice.

Nothing here is a reimplementation. Every object that decides what gets asked is
imported from the shipped package:

    wasted_harness.predictions.catalog.CATALOG       the real templates
    wasted_harness.predictions.baserate.RollingBaseRate   the real §3 estimator
    wasted_harness.predictions.generator.PredictionGenerator  the real picker
    wasted_harness.predictions.writer.PredictionWriter        the real adapter
    wasted_harness.events.SupabaseWriter                      the real writer

The only thing injected is a CLOCK, at the constructor boundary the package
already exposes for it (`PredictionGenerator(clock=..., wall_clock=...)`,
`RollingBaseRate(clock=...)`) — the same injection
`harness/tests/test_predictions_replay.py` uses. No event, outcome, rate or row
is invented.

Two modes, sharing one `_rounds()` so they cannot drift:

  plan   Replays the recording in memory and prints, as JSON, every round the
         generator would ask and whether each round's window contains a real
         matching event (decided by the product's own `baserate.rule_matcher`,
         which is Postgres's `payload @> payload_match`). `verify-rounds.mjs`
         uses this to choose the single constant time offset that puts one
         chosen round's entry window over "now".

  run    The same replay, on the SHIFTED timeline, with the rolling base rate
         WARM STARTED by reading real `events` rows back out of the database
         through the product's own Supabase client (what `main.py` does on
         every restart), and every row the generator produces written through
         PredictionWriter -> SupabaseWriter.flush().

The recording is `harness/tests/fixtures/real_session_2026-09-20.json`, a
provenance-stamped production pull. `run` shifts its clock by a constant; it
never alters an event.

GameState: built by `harness/tests/support/states.py`'s `make_state`, the same
builder the replay test uses, so the states this generator sees are the ones the
shipped acceptance test already reports on. The recording is events-only (no
`/state`), so state is derived from the events exactly as that test derives it,
and its own docstring lists the consequences.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
HARNESS = REPO / "harness"
for path in (HARNESS, HARNESS / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from support.states import make_state  # noqa: E402  (path set above)

from wasted_harness.events import SupabaseWriter  # noqa: E402
from wasted_harness.predictions.baserate import (  # noqa: E402
    DEFAULT_HISTORY_S,
    RollingBaseRate,
    rule_matcher,
)
from wasted_harness.predictions.catalog import (  # noqa: E402
    CATALOG,
    DEFAULT_ROUND_INTERVAL_S,
)
from wasted_harness.predictions.generator import (  # noqa: E402
    GeneratorConfig,
    PredictionGenerator,
)
from wasted_harness.predictions.writer import PredictionWriter  # noqa: E402
from wasted_harness.settings import Settings  # noqa: E402

#: The replay step, as in `tests/test_predictions_replay.py`: the harness's own
#: lifecycle cadence. Every gate in play is measured in tens of seconds.
STEP_S = 5.0

#: How much of the recording is handed to the estimator as HISTORY before the
#: replay starts asking. This is the real restart case: §3 needs 12 sampled
#: windows at 300 s, so a cold estimator is silent for ~59 minutes, and
#: `main._warm_start_base_rate` exists precisely to skip that. One hour gives
#: (3600 - 240) / 300 + 1 = 12 closed windows at the first tick.
WARM_START_S = 3600.0


def _load(fixture: pathlib.Path) -> list[dict[str, Any]]:
    with fixture.open() as f:
        events = json.load(f)["events"]
    for e in events:
        e["at"] = datetime.fromisoformat(e["ts"]).timestamp()
        e["payload"] = e.get("payload") or {}
    events.sort(key=lambda e: e["at"])
    return events


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _rounds(
    events: Sequence[dict[str, Any]],
    *,
    t_start: float,
    t_end: float,
    estimator: RollingBaseRate,
    clock: _Clock,
    session_id: str,
    sink: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    """Step the clock from `t_start` to `t_end` and let the generator decide.

    `estimator` is already warm (in memory for `plan`, from the database for
    `run`) and its clock is `clock`. Every event whose timestamp the clock
    passes is observed exactly once, which is what `main._note_recent_event`
    does live.
    """
    generator = PredictionGenerator(
        catalog=CATALOG,
        clock=clock,
        wall_clock=clock,
        config=GeneratorConfig(),
        base_rate=estimator.measure,
    )
    states = {w: make_state(wanted=w) for w in range(6)}
    wanted = 0
    recent: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(events) and events[i]["at"] <= t_start:
        i += 1
    now = t_start
    while now <= t_end:
        clock.t = now
        while i < len(events) and events[i]["at"] <= now:
            e = events[i]
            i += 1
            estimator.observe(e["at"], e["type"], e["payload"])
            recent.append(e)
            if e["type"] == "wanted_change":
                with contextlib.suppress(TypeError, ValueError):
                    wanted = int(e["payload"].get("to", wanted))
        cutoff = now - 60.0
        recent = [e for e in recent if e["at"] >= cutoff]
        row = generator.generate(states[min(wanted, 5)], session_id, recent)
        if row is not None:
            rows.append(row)
            sink(row)
        now += STEP_S
    return rows


def _window(row: dict[str, Any]) -> tuple[float, float]:
    return (
        datetime.fromisoformat(row["locks_at"]).timestamp(),
        datetime.fromisoformat(row["resolves_at"]).timestamp(),
    )


def _matching_event(row: dict[str, Any], events: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The first real event in [locks_at, resolves_at] that settles this row YES.

    Uses the product's own `baserate.rule_matcher` — the same containment test
    `settle_due_predictions()` runs as `payload @> payload_match` — so this is a
    prediction of what settlement will find, not a second opinion about it.
    """
    matcher = rule_matcher(row["telemetry_rule"])
    if matcher is None:
        return None
    opens, closes = _window(row)
    for e in events:
        if e["at"] < opens:
            continue
        if e["at"] > closes:
            break
        if matcher(e["type"], e["payload"]):
            return e
    return None


def _describe(row: dict[str, Any], events: Sequence[dict[str, Any]], t0: float) -> dict[str, Any]:
    opens, closes = _window(row)
    hit = _matching_event(row, events)
    return {
        "prediction_type": row["prediction_type"],
        "question": row["question"],
        "telemetry_rule": row["telemetry_rule"],
        "calibration": row["state_context"].get("calibration"),
        "opened_at": row["opened_at"],
        "locks_at": row["locks_at"],
        "resolves_at": row["resolves_at"],
        "opened_epoch": datetime.fromisoformat(row["opened_at"]).timestamp(),
        "locks_epoch": opens,
        "resolves_epoch": closes,
        "offset_from_t0_s": datetime.fromisoformat(row["opened_at"]).timestamp() - t0,
        "matching_event": None
        if hit is None
        else {"ts": hit["ts"], "type": hit["type"], "payload": hit["payload"]},
        "reward_pool": row["reward_pool"],
        "reward_asset": row["reward_asset"],
    }


def cmd_plan(args: argparse.Namespace) -> int:
    events = _load(pathlib.Path(args.fixture))
    t0, t1 = events[0]["at"], events[-1]["at"]
    t_start = t0 + WARM_START_S
    clock = _Clock(t_start)
    estimator = RollingBaseRate(
        clock=clock, history_s=DEFAULT_HISTORY_S, round_interval_s=DEFAULT_ROUND_INTERVAL_S
    )
    # The in-memory equivalent of `run`'s database warm start: the real rows
    # that precede the replay's first tick, oldest first, exactly as
    # `main._warm_start_base_rate` feeds what it read back.
    warm = [e for e in events if e["at"] <= t_start]
    for e in warm:
        estimator.observe(e["at"], e["type"], e["payload"])
    rows = _rounds(
        events,
        t_start=t_start,
        t_end=t1,
        estimator=estimator,
        clock=clock,
        session_id=args.session,
        sink=lambda _row: None,
    )
    json.dump(
        {
            "fixture": str(args.fixture),
            "t0": t0,
            "t1": t1,
            "replay_start": t_start,
            "warm_start_rows": len(warm),
            "step_s": STEP_S,
            "rounds": [_describe(r, events, t0) for r in rows],
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    offset = float(args.offset)
    events = _load(pathlib.Path(args.fixture))
    for e in events:
        e["at"] += offset
        e["ts"] = datetime.fromtimestamp(e["at"], UTC).isoformat()
    t0, t1 = events[0]["at"], events[-1]["at"]
    t_start = t0 + WARM_START_S

    # No `.env` is read: `Settings.load()` would otherwise pull the PRODUCTION
    # Supabase credentials out of `harness/.env` into this process. The local
    # stack's URL and service JWT come from the environment only.
    settings = Settings.load(env_file=pathlib.Path(os.devnull))
    if not settings.supabase_configured:
        print("SUPABASE_URL / SUPABASE_SECRET_KEY are not set", file=sys.stderr)
        return 2
    clock = _Clock(t_start)
    # The writer judges "is this row's entry window still open?" at the moment it
    # inserts (events.MIN_ENTRY_REMAINING_S, CONTRACTS-PREDICTIONS v2.5 §3), so it
    # must read the SAME clock the generator stamped the row with. On the real
    # time.time() every replayed round would look minutes or hours stale — the
    # replay runs 2.32 h of play in seconds — and be dropped, correctly.
    writer = SupabaseWriter(settings, session_id=args.session, wall_clock=clock)
    prediction_writer = PredictionWriter(writer)
    estimator = RollingBaseRate(
        clock=clock, history_s=DEFAULT_HISTORY_S, round_interval_s=DEFAULT_ROUND_INTERVAL_S
    )

    # --- the warm start, the production path (main._warm_start_base_rate) -----
    # Same client, same table, same ordering, same cap. One deliberate addition:
    # an upper bound at the replay's first tick. Production has no rows from the
    # future; a replay standing at `t_start` does, and feeding them to the
    # estimator would hand it telemetry the live harness could not have had.
    since = datetime.fromtimestamp(t_start, UTC) - timedelta(seconds=DEFAULT_HISTORY_S)
    resp = (
        writer._get_client()
        .table("events")
        .select("ts,type,payload")
        .gte("ts", since.isoformat())
        .lte("ts", datetime.fromtimestamp(t_start, UTC).isoformat())
        .order("ts", desc=True)
        .limit(2000)
        .execute()
    )
    warm_rows = list(reversed(resp.data or []))
    for row in warm_rows:
        estimator.observe(
            datetime.fromisoformat(str(row["ts"])).timestamp(),
            str(row.get("type")),
            row.get("payload") or {},
        )

    written: list[dict[str, Any]] = []
    flushes: list[bool] = []

    def sink(row: dict[str, Any]) -> None:
        # Flushed at the tick it was generated, as production does within one
        # FLUSH_INTERVAL_S (2 s): one flush after the whole replay would judge
        # every row against the replay's LAST tick.
        prediction_writer.write(row)
        flushes.append(prediction_writer.flush())
        written.append(row)

    rows = _rounds(
        events,
        t_start=t_start,
        t_end=t1,
        estimator=estimator,
        clock=clock,
        session_id=args.session,
        sink=sink,
    )
    flushed = all(flushes) and prediction_writer.flush()
    json.dump(
        {
            "offset_s": offset,
            "session_id": args.session,
            "t0": t0,
            "t1": t1,
            "replay_start": t_start,
            "warm_start_rows": len(warm_rows),
            "warm_start_span_s": round(estimator.observed_span_s, 1),
            "generated": len(rows),
            "written": len(written),
            "flushed": flushed,
            "write_failures": writer.predictions_write_failures,
            "last_error": writer.last_predictions_error,
            "queue_path": str(writer.queue_path),
            "rounds": [_describe(r, events, t0) for r in rows],
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0 if flushed and not writer.predictions_write_failures else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    default_fixture = HARNESS / "tests" / "fixtures" / "real_session_2026-09-20.json"

    p = sub.add_parser("plan", help="replay in memory; print the rounds as JSON")
    p.add_argument("--fixture", default=str(default_fixture))
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_plan)

    r = sub.add_parser("run", help="replay onto the shifted clock; write through the real writer")
    r.add_argument("--fixture", default=str(default_fixture))
    r.add_argument("--session", required=True)
    r.add_argument("--offset", required=True, help="seconds added to every event timestamp")
    r.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
