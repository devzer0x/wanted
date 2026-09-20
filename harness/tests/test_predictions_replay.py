"""Replay both real recordings through the real generator, and report.

This is the acceptance check for CONTRACTS-PREDICTIONS §3 (v2.4) "Cadence and
entry windows": not "does each piece work" — the other four prediction test
files cover that — but "what would the shipped catalogue, the shipped
generator and the shipped base-rate estimator actually have DONE, minute by
minute, on two real sessions of this agent".

Nothing here is simulated except the clock. The events are the two
provenance-stamped production pulls in `tests/fixtures/`; the generator, the
catalogue, the estimator and the recency window are the shipped objects, wired
together the same way `main.Harness` wires them.

**What the recordings cannot supply, and how that is handled.** Both files are
`events`-only: no `/state`, no HUD, no threat. So the live `GameState` each
tick is DERIVED from the events, field by field, and each derivation is named
in `_replay` below. Two consequences are worth stating up front rather than
discovering in a number:

* `survives_a_fight` never triggers in either replay (its trigger is
  `/state.threat.attacker_handle` or a hostile ped, neither of which is an
  event), and `mission_outcome` never triggers (missions were operator-off for
  both sessions, confirmed by each file's own `_provenance`).
* `dead`/`arrested` are never simulated as true, because no event says when he
  stands back up. That makes the replay slightly MORE permissive than the live
  harness, which is the safe direction for a test whose assertions are
  ceilings.
"""

from __future__ import annotations

import contextlib
import json
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from support.states import make_state

from wasted_harness.main import PREDICTION_RECENT_WINDOW_S
from wasted_harness.predictions.baserate import RollingBaseRate
from wasted_harness.predictions.catalog import (
    AMBIENT_MAX_RATE,
    AMBIENT_MIN_RATE,
    AMBIENT_MIN_SAMPLES,
    CATALOG,
    DEFAULT_ROUND_INTERVAL_S,
    MIN_ENTRY_WINDOW_S,
    TELEMETRY_RULE_KINDS,
    UNSETTLEABLE_RULE_KINDS,
)
from wasted_harness.predictions.generator import GeneratorConfig, PredictionGenerator

FIXTURES = Path(__file__).parent / "fixtures"
OLD = FIXTURES / "real_session_2026-09-04.json"
NEW = FIXTURES / "real_session_2026-09-20.json"

SESSION_ID = "00000000-0000-0000-0000-0000000000ee"

#: The harness's own lifecycle cadence (`predictions.ticker.DEFAULT_INTERVAL_S`),
#: used here as the replay step. The live loop offers a prediction at 2-4 Hz,
#: but every gate in play is measured in tens of seconds, so stepping at 5 s
#: changes nothing about what is offered and makes an 87-hour replay finish.
STEP_S = 5.0

AMBIENT_TYPES = {t.prediction_type for t in CATALOG if t.ambient}


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@dataclass
class Replay:
    name: str
    t0: float
    t1: float
    rows: list[dict[str, Any]]
    #: Replay TICKS (not rounds) on which the round was open and at least one
    #: ambient template measured inside §3's band — i.e. how much of the
    #: recording a fair always-available question existed for at all, before
    #: any anti-spam rule had its say.
    fair_ambient_ticks: int

    @property
    def hours(self) -> float:
        return (self.t1 - self.t0) / 3600.0

    @property
    def per_hour(self) -> float:
        return len(self.rows) / self.hours

    @property
    def ambient(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["prediction_type"] in AMBIENT_TYPES]

    @property
    def situational(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["prediction_type"] not in AMBIENT_TYPES]

    @property
    def entry_windows(self) -> list[float]:
        return [
            (
                datetime.fromisoformat(r["locks_at"]) - datetime.fromisoformat(r["opened_at"])
            ).total_seconds()
            for r in self.rows
        ]


def _load(fixture: Path) -> list[dict[str, Any]]:
    with fixture.open() as f:
        events = json.load(f)["events"]
    for e in events:
        e["at"] = datetime.fromisoformat(e["ts"]).timestamp()
        e["payload"] = e.get("payload") or {}
    events.sort(key=lambda e: e["at"])
    return events


def _replay(fixture: Path) -> Replay:
    events = _load(fixture)
    t0, t1 = events[0]["at"], events[-1]["at"]
    clock = _Clock(t0)

    estimator = RollingBaseRate(clock=clock, round_interval_s=DEFAULT_ROUND_INTERVAL_S)
    generator = PredictionGenerator(
        catalog=CATALOG,
        clock=clock,
        wall_clock=clock,
        config=GeneratorConfig(),
        base_rate=estimator.measure,
    )

    # Derived live state, field by field, from events only:
    #   wanted          <- the last `wanted_change.to` seen (0 until the first)
    #   dead / arrested <- never true; no event marks the end of a respawn
    #   mission.active  <- never true; missions were operator-off for both
    #                      sessions (each file's own `_provenance`)
    #   health/vehicle  <- the builder's defaults; no HUD exists in an
    #                      events-only export, and neither one gates a trigger
    # Cached per `wanted` value: building a GameState 62,000 times is the only
    # slow thing in this replay, and nothing else about the state varies.
    states = {w: make_state(wanted=w) for w in range(6)}
    wanted = 0

    recent: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    ambient_rounds_possible = 0
    i = 0
    now = t0
    while now <= t1:
        clock.t = now
        while i < len(events) and events[i]["at"] <= now:
            e = events[i]
            i += 1
            estimator.observe(e["at"], e["type"], e["payload"])
            recent.append(e)
            if e["type"] == "wanted_change":
                # A real payload off the wire can carry a null `to`; the live
                # triggers tolerate that, so the replay has to as well.
                with contextlib.suppress(TypeError, ValueError):
                    wanted = int(e["payload"].get("to", wanted))
        cutoff = now - PREDICTION_RECENT_WINDOW_S
        recent = [e for e in recent if e["at"] >= cutoff]

        # How often the ROUND was open with something in band — the number the
        # calibration gate is really about, separate from whether an anti-spam
        # rule then blocked the ask.
        if generator._ambient_round_open(now) and any(
            generator._calibration_for(t) is not None for t in CATALOG if t.ambient
        ):
            ambient_rounds_possible += 1

        row = generator.generate(states[min(wanted, 5)], SESSION_ID, recent)
        if row is not None:
            rows.append(row)
        now += STEP_S

    return Replay(
        name=fixture.name,
        t0=t0,
        t1=t1,
        rows=rows,
        fair_ambient_ticks=ambient_rounds_possible,
    )


@pytest.fixture(scope="module")
def replays() -> dict[str, Replay]:
    out = {}
    for fixture in (NEW, OLD):
        if fixture.exists():
            out[fixture.name] = _replay(fixture)
    return out


def _report(r: Replay) -> str:
    windows = r.entry_windows
    kinds = Counter(row["telemetry_rule"]["kind"] for row in r.rows)
    return (
        f"\n--- {r.name} ({r.hours:.2f} h of play) ---\n"
        f"  predictions generated     : {len(r.rows)} ({r.per_hour:.2f}/h)\n"
        f"  ambient vs situational    : {len(r.ambient)} / {len(r.situational)}\n"
        f"  entry window min / median : "
        f"{min(windows) if windows else 0:.0f}s / "
        f"{statistics.median(windows) if windows else 0:.0f}s\n"
        f"  fair-ambient availability : {r.fair_ambient_ticks} of "
        f"{int((r.t1 - r.t0) / STEP_S)} ticks "
        f"({r.fair_ambient_ticks * STEP_S / max(1.0, r.t1 - r.t0):.0%} of the recording)\n"
        f"  by type                   : {dict(Counter(x['prediction_type'] for x in r.rows))}\n"
        f"  by rule kind              : {dict(kinds)}\n"
    )


@pytest.mark.skipif(not (OLD.exists() and NEW.exists()), reason="real fixtures not present")
def test_replay_report(replays: dict[str, Replay], capsys) -> None:
    """Prints the per-recording numbers. Run with `-s` to read them."""
    assert replays, "neither recording is present"
    with capsys.disabled():
        for r in replays.values():
            print(_report(r), end="")


# -- the properties every row must have, on both recordings --------------------


@pytest.mark.skipif(not (OLD.exists() and NEW.exists()), reason="real fixtures not present")
def test_no_two_consecutive_rows_share_a_prediction_type(replays: dict[str, Replay]) -> None:
    for r in replays.values():
        types = [row["prediction_type"] for row in r.rows]
        repeats = [a for a, b in pairwise(types) if a == b]
        assert not repeats, f"{r.name}: back-to-back {repeats[:3]}"


@pytest.mark.skipif(not (OLD.exists() and NEW.exists()), reason="real fixtures not present")
def test_every_row_gives_a_viewer_the_contract_floor_to_enter(
    replays: dict[str, Replay],
) -> None:
    for r in replays.values():
        assert r.rows, r.name
        assert min(r.entry_windows) >= MIN_ENTRY_WINDOW_S, r.name


@pytest.mark.skipif(not (OLD.exists() and NEW.exists()), reason="real fixtures not present")
def test_every_rule_kind_written_is_one_settlement_can_answer(
    replays: dict[str, Replay],
) -> None:
    """Recognised AND resolvable. Those are different sets, and shipping a
    template from the gap between them is what `enters_vehicle` did: real
    entries, `missing_telemetry` void, 100% of the time."""
    for r in replays.values():
        kinds = {row["telemetry_rule"]["kind"] for row in r.rows}
        assert kinds <= TELEMETRY_RULE_KINDS, (r.name, kinds - TELEMETRY_RULE_KINDS)
        assert not (kinds & UNSETTLEABLE_RULE_KINDS), (r.name, kinds & UNSETTLEABLE_RULE_KINDS)
        for row in r.rows:
            rule = row["telemetry_rule"]
            assert rule["outcome_if_true"] in {o["key"] for o in row["outcomes"]}
            assert rule["outcome_if_false"] in {o["key"] for o in row["outcomes"]}
            if rule["kind"] in ("event_occurs", "event_matches"):
                assert rule["params"]["event_type"]
            if rule["kind"] == "event_matches":
                assert rule["params"]["payload_match"]


@pytest.mark.skipif(not (OLD.exists() and NEW.exists()), reason="real fixtures not present")
def test_every_ambient_row_carries_the_measurement_that_justified_it(
    replays: dict[str, Replay],
) -> None:
    for r in replays.values():
        for row in r.ambient:
            c = row["state_context"]["calibration"]
            assert c["n"] >= AMBIENT_MIN_SAMPLES, (r.name, c)
            assert AMBIENT_MIN_RATE <= c["yes"] / c["n"] <= AMBIENT_MAX_RATE, (r.name, c)
            assert c["window_s"] == 180.0
        for row in r.situational:
            assert "calibration" not in row["state_context"]


# -- the point of the calibration gate ------------------------------------------


@pytest.mark.skipif(not NEW.exists(), reason="real fixture not present")
def test_ambient_rounds_run_on_the_live_free_roam_recording(
    replays: dict[str, Replay],
) -> None:
    """2026-09-20: 2 h 19 m of real play with no wanted star, no death and no
    bust. Every situational template is unofferable for the whole recording, so
    without the scheduled round the card is empty for the entire broadcast.

    The expected shape: nothing for the first hour (12 sampled windows at 300 s
    is the §3 minimum, and a cold estimator has to live through them), then
    about one question every five minutes.
    """
    r = replays[NEW.name]
    assert r.situational == [], "this recording has no wanted/death/bust telemetry at all"
    assert r.ambient, "nothing was asked on a recording where only ambient questions can be"

    # The first moment §3's `n >= 12` can be satisfied by a cold estimator: the
    # newest window has to have CLOSED (60 + 180 s after its anchor) and eleven
    # more have to fit behind it at one round apart. 240 + 11 x 300 = 3,540 s.
    span = 60.0 + 180.0
    first_askable = span + (AMBIENT_MIN_SAMPLES - 1) * DEFAULT_ROUND_INTERVAL_S
    assert first_askable == 3540.0
    askable_s = (r.t1 - r.t0) - first_askable
    expected = int(askable_s // DEFAULT_ROUND_INTERVAL_S) + 1
    assert len(r.ambient) == expected, (
        f"{len(r.ambient)} ambient rows; one per {DEFAULT_ROUND_INTERVAL_S:.0f}s over the "
        f"{askable_s / 60:.0f} askable minutes is {expected}"
    )
    # ...which is one question every five minutes, said as a rate.
    per_hour_once_warm = len(r.ambient) / (askable_s / 3600.0)
    assert per_hour_once_warm == pytest.approx(3600.0 / DEFAULT_ROUND_INTERVAL_S, abs=1.0)

    # And nothing at all before the estimator could honestly measure anything.
    first_row_at = datetime.fromisoformat(r.ambient[0]["opened_at"]).timestamp()
    assert first_row_at - r.t0 >= first_askable


@pytest.mark.skipif(not OLD.exists(), reason="real fixture not present")
def test_the_calibration_gate_bites_on_the_older_recording(
    replays: dict[str, Replay],
) -> None:
    """2026-09-04, 86.93 h: the same three ambient questions measure 7%, 3% and
    9% over the whole file — all far below §3's 0.20 floor.

    The gate is a ROLLING one, though, and the honest result is not "never":
    goal completions are heavily front-loaded in this recording (34 of the 194
    in one 3-hour stretch, 11 in another), so there are hours where
    `pulls_off_a_goal` really was a fair question, and the gate lets those
    through. What it must not do is ask across the long flat tail where the
    question is a giveaway — so this asserts the rate, not a zero:
    ambient questions are a small minority of a day here, and they stop
    entirely once the agent stops finishing things.
    """
    r = replays[OLD.name]
    rounds_available = (r.hours * 3600.0) / DEFAULT_ROUND_INTERVAL_S
    share = len(r.ambient) / rounds_available
    assert share < 0.30, (
        f"{len(r.ambient)} ambient rows against {rounds_available:.0f} possible rounds "
        f"({share:.0%}) — the calibration gate is not biting"
    )

    # MEASURED, and the sharpest single number in this file: every ambient
    # question the gate allowed on this recording falls inside its first
    # 21.5 hours. Across the remaining 65 hours — the long flat tail where the
    # agent had stopped finishing things and the question really is a giveaway
    # — the gate asks NOTHING, without anyone having written a rule about it.
    offsets_h = [
        (datetime.fromisoformat(row["opened_at"]).timestamp() - r.t0) / 3600.0
        for row in r.ambient
    ]
    assert max(offsets_h) < r.hours / 3.0, (
        f"an ambient question was asked {max(offsets_h):.1f} h into a "
        f"{r.hours:.1f} h recording whose whole-file rate is far below the band"
    )
    midpoint_h = r.hours / 2.0
    assert [h for h in offsets_h if h > midpoint_h] == []

    # ...and every one that WAS asked carries a measurement inside the band.
    for row in r.ambient:
        c = row["state_context"]["calibration"]
        assert AMBIENT_MIN_RATE <= c["yes"] / c["n"] <= AMBIENT_MAX_RATE, c


@pytest.mark.skipif(not (OLD.exists() and NEW.exists()), reason="real fixtures not present")
def test_the_two_recordings_disagree_about_the_same_question(
    replays: dict[str, Replay],
) -> None:
    """The whole argument for §3's live re-measurement, in one assertion: the
    newer recording is dominated by the always-available questions and the
    older one is not, from the same catalogue and the same code."""
    new, old = replays[NEW.name], replays[OLD.name]
    new_share = len(new.ambient) / max(1, len(new.rows))
    old_share = len(old.ambient) / max(1, len(old.rows))
    assert new_share == 1.0
    assert old_share < new_share
