#!/usr/bin/env python3
"""F1-F6: is the show fun to watch? A script over a replay log, not a game.

Reads the two `.jsonl` files `tests/support/replayer.py::ReplayLog.write_jsonl`
/ `write_states_jsonl` produce (or that a caller assembles in the same shape):
a flat, tick-ordered `records` stream (`kind` discriminates: `posted_task`,
`event`, `say`, `wheel`, `decision`, ...) and a `states` stream (one
`/state`-shaped snapshot per tick, with `clock_t`). Nothing here touches the
bridge or the network; it is pure analysis of what already happened.

Reuses the REAL production rules wherever one exists, rather than
re-describing them: `wasted_harness.brain.characters.present_names` /
`absent_names_mentioned` for F3's names check, and
`wasted_harness.brain.schemas.jaccard_similarity` for F3's dedupe rule — the
exact same functions `main._apply_decision` / T4's `validate_decision_content`
use live, so a PASS here means the same thing a PASS in production would.
Banned phrases come from `wasted_harness.brain.prompts.banned_phrases()`
(parsed from `commentary_style.md`) when it returns a non-empty list; a
small, clearly-labelled fallback list is used only if it does not (see
`_FALLBACK_BANNED_PHRASES` below).

Exit code is non-zero iff any check FAILs (`--exit-zero` overrides, for CI
dashboards that want the table without failing the job).

Usage::

    python harness/tools/funcheck.py --log run.replay.jsonl --states run.states.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wasted_harness.brain.characters import absent_names_mentioned, present_names
from wasted_harness.brain.schemas import (
    MOVEMENT_TASKS,
    jaccard_similarity,
)
from wasted_harness.bridge_client import GameState

try:
    from wasted_harness.brain.prompts import banned_phrases as _banned_phrases_fn
except ImportError:  # pragma: no cover - the package always has this module
    _banned_phrases_fn = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- #
#  thresholds (named so a future tune is one line, not a hunt through code)   #
# --------------------------------------------------------------------------- #

F1_IDLE_RATIO_MAX = 0.05
F1_WINDOW_S = 600.0
#: A window shorter than this is not yet a measurement, it is start-up
#: noise: the first tick or two of ANY run is idle before the first goal
#: is even picked (RoamEngine's own 2-12s jittered gap), and a single-tick
#: window trivially reads 0% or 100%. Only a window that has actually
#: accumulated a reasonable slice of real time counts as "a window".
F1_MIN_WINDOW_SPAN_S = 60.0
F2_GAP_MAX_S = 60.0
F3_DEDUPE_OVERLAP_MAX = 0.60
F3_LINE_EVENT_WINDOW_S = 20.0
F4_COMPLETION_MIN = 0.60
F5_DEATHS_PER_HOUR_MAX = 6.0
F6_RESUME_WINDOW_S = 3.0

#: Only used when `wasted_harness.brain.prompts.banned_phrases()` (parsed live
#: from `commentary_style.md`) is unavailable or returns nothing — e.g. an
#: older log recorded before that section existed. Kept intentionally short:
#: this is a backstop, not a second copy of the real list.
_FALLBACK_BANNED_PHRASES: tuple[str, ...] = (
    "as an ai",
    "as a language model",
    "i'm an ai",
    "grand theft auto",
    "rockstar",
)

#: "Fight" for F2's "something new" timeline: a task that means combat, or the
#: reflex ladder taking the wheel for survival.
_FIGHT_TASK_TYPES = frozenset({"fight_ped", "combat_hated_targets_around"})
_FIGHT_WHEEL_OWNERS = frozenset({"threat", "flip"})


# --------------------------------------------------------------------------- #
#  loading                                                                    #
# --------------------------------------------------------------------------- #


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


@dataclass
class ReplayData:
    records: list[dict[str, Any]]
    states: list[dict[str, Any]]

    @classmethod
    def load(cls, log_path: Path, states_path: Path) -> ReplayData:
        records = sorted(load_jsonl(log_path), key=lambda r: (r["clock_t"], r["tick"]))
        states = sorted(load_jsonl(states_path), key=lambda s: (s["clock_t"], s["tick"]))
        return cls(records=records, states=states)

    @property
    def duration_s(self) -> float:
        if not self.states:
            return 0.0
        return self.states[-1]["clock_t"] - self.states[0]["clock_t"]

    @property
    def duration_hours(self) -> float:
        return self.duration_s / 3600.0

    def by_kind(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["kind"] == kind]

    def events(self, *types: str) -> list[dict[str, Any]]:
        evs = self.by_kind("event")
        if not types:
            return evs
        wanted = set(types)
        return [e for e in evs if e.get("type") in wanted]

    def state_at_or_before(self, clock_t: float) -> dict[str, Any] | None:
        """The last recorded state with `clock_t <= clock_t`, or the first
        state if none precede it (a line spoken before the first snapshot is
        still checked against the earliest truth available)."""
        best = None
        for s in self.states:
            if s["clock_t"] <= clock_t:
                best = s
            else:
                break
        return best if best is not None else (self.states[0] if self.states else None)


@dataclass
class CheckResult:
    key: str
    label: str
    passed: bool
    detail: str
    numbers: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  F1 — idle ratio                                                            #
# --------------------------------------------------------------------------- #


def _f1_eligible_and_idle(state: dict[str, Any]) -> tuple[bool, bool]:
    player = state["player"]
    mission = state["mission"]
    eligible = (
        bool(player.get("control_enabled", True))
        and not mission.get("cutscene_active", False)
        and not player.get("dead", False)
        and not player.get("arrested", False)
        and not player.get("switch_in_progress", False)
        and not mission.get("retry_in_flight", False)
    )
    if not eligible:
        return False, False
    last_task = state["last_task"]
    active_movement = (
        last_task.get("status") == "running" and last_task.get("type") in MOVEMENT_TASKS
    )
    return True, not active_movement


def check_f1_idle_ratio(data: ReplayData) -> CheckResult:
    ticks = [(s["clock_t"], *_f1_eligible_and_idle(s["state"])) for s in data.states]
    if not any(eligible for _, eligible, _ in ticks):
        return CheckResult(
            "F1", "idle ratio < 5% / 10min window", True,
            "no eligible ticks (always dead/cutscene/no-control) — nothing to measure",
            {"worst_ratio": None},
        )
    worst_ratio = 0.0
    worst_window: tuple[float, float] | None = None
    lo = 0
    n_elig = n_idle = 0
    for hi in range(len(ticks)):
        t_hi, elig_hi, idle_hi = ticks[hi]
        if elig_hi:
            n_elig += 1
            if idle_hi:
                n_idle += 1
        while ticks[lo][0] < t_hi - F1_WINDOW_S:
            _, elig_lo, idle_lo = ticks[lo]
            if elig_lo:
                n_elig -= 1
                if idle_lo:
                    n_idle -= 1
            lo += 1
        span = t_hi - ticks[lo][0]
        if n_elig > 0 and span >= F1_MIN_WINDOW_SPAN_S:
            ratio = n_idle / n_elig
            if ratio > worst_ratio:
                worst_ratio = ratio
                worst_window = (ticks[lo][0], t_hi)
    if worst_window is None:
        return CheckResult(
            "F1", "idle ratio < 5% / 10min window", True,
            f"log shorter than the {F1_MIN_WINDOW_SPAN_S:.0f}s minimum measurable span — nothing to measure",
            {"worst_ratio": None},
        )
    passed = worst_ratio < F1_IDLE_RATIO_MAX
    detail = (
        f"worst window idle_ratio={worst_ratio:.1%} over "
        f"t={worst_window[0]:.0f}s-{worst_window[1]:.0f}s"
    )
    return CheckResult(
        "F1", "idle ratio < 5% / 10min window", passed, detail, {"worst_ratio": round(worst_ratio, 4)}
    )


# --------------------------------------------------------------------------- #
#  F2 — something new every 60s                                               #
# --------------------------------------------------------------------------- #


def _f2_moments(data: ReplayData) -> list[tuple[float, str]]:
    moments: list[tuple[float, str]] = []
    for e in data.events(
        "activity_start", "activity_end", "death", "busted", "wanted_change",
        "mission_start", "mission_end", "mission_fail", "unstick",
    ):
        moments.append((e["clock_t"], f"event:{e['type']}"))
    for r in data.by_kind("posted_task"):
        if r.get("type") in _FIGHT_TASK_TYPES:
            moments.append((r["clock_t"], f"fight:{r['type']}"))
    for r in data.by_kind("wheel"):
        if r.get("owner") in _FIGHT_WHEEL_OWNERS and r.get("owner") != r.get("previous_owner"):
            moments.append((r["clock_t"], f"wheel:{r['owner']}"))
    prev_vehicle: Any = "unset"
    for s in data.states:
        veh = s["state"].get("vehicle")
        handle = veh.get("handle") if veh else None
        if prev_vehicle != "unset" and handle != prev_vehicle:
            moments.append((s["clock_t"], "vehicle_change"))
        prev_vehicle = handle
    moments.sort(key=lambda m: m[0])
    return moments


def check_f2_something_new(data: ReplayData) -> CheckResult:
    if data.duration_s <= 0:
        return CheckResult("F2", "something new every 60s", True, "log too short to measure", {})
    moments = _f2_moments(data)
    start, end = data.states[0]["clock_t"], data.states[-1]["clock_t"]
    marks = [start, *[m[0] for m in moments], end]
    worst_gap = 0.0
    worst_at = start
    for a, b in pairwise(marks):
        gap = b - a
        if gap > worst_gap:
            worst_gap, worst_at = gap, a
    passed = worst_gap <= F2_GAP_MAX_S
    return CheckResult(
        "F2", "something new every 60s", passed,
        f"longest gap {worst_gap:.0f}s (starting t={worst_at:.0f}s); {len(moments)} moments total",
        {"longest_gap_s": round(worst_gap, 1), "moments": len(moments)},
    )


# --------------------------------------------------------------------------- #
#  F3 — commentary hygiene                                                    #
# --------------------------------------------------------------------------- #


def _banned_phrases() -> tuple[str, ...]:
    if _banned_phrases_fn is not None:
        phrases = _banned_phrases_fn()
        if phrases:
            return phrases
    return _FALLBACK_BANNED_PHRASES


def _f3_has_nearby_activity(data: ReplayData, clock_t: float, window_s: float) -> bool:
    lo, hi = clock_t - window_s, clock_t + window_s
    for r in data.records:
        if r["kind"] in ("event", "posted_task", "wheel") and lo <= r["clock_t"] <= hi:
            return True
    return False


def check_f3_commentary(data: ReplayData) -> CheckResult:
    says = data.by_kind("say")
    says = [s for s in says if s.get("text")]  # empty lines (deliberate silence) are not "a line"
    phrases = _banned_phrases()
    offenders: list[dict[str, Any]] = []
    recent: list[str] = []
    for s in says:
        text = s["text"]
        reasons: list[str] = []
        lowered = text.lower()
        for phrase in phrases:
            if phrase and phrase in lowered:
                reasons.append(f"banned phrase {phrase!r}")
        for prior in recent[-5:]:
            overlap = jaccard_similarity(text, prior)
            if overlap >= F3_DEDUPE_OVERLAP_MAX:
                reasons.append(f"overlaps a recent line ({overlap:.2f}): {prior!r}")
        st = data.state_at_or_before(s["clock_t"])
        if st is not None:
            state = GameState.model_validate(st["state"])
            absent = absent_names_mentioned(text, present_names(state))
            if absent:
                reasons.append(f"names not in STATE: {', '.join(absent)}")
        if not _f3_has_nearby_activity(data, s["clock_t"], F3_LINE_EVENT_WINDOW_S):
            reasons.append(f"no event/task/wheel activity within {F3_LINE_EVENT_WINDOW_S:.0f}s")
        if reasons:
            offenders.append({"t": s["clock_t"], "text": text, "reasons": reasons})
        recent.append(text)
    passed = not offenders
    detail = f"{len(says)} lines checked, {len(offenders)} offenders"
    if offenders:
        worst = offenders[0]
        detail += f"; first at t={worst['t']:.0f}s: {'; '.join(worst['reasons'])} — {worst['text']!r}"
    return CheckResult(
        "F3", "commentary hygiene", passed, detail,
        {"lines": len(says), "offenders": len(offenders)},
    )


# --------------------------------------------------------------------------- #
#  F4 — roam goal completion                                                  #
# --------------------------------------------------------------------------- #


def check_f4_goal_completion(data: ReplayData) -> CheckResult:
    resolutions = [
        e for e in data.events("activity_end")
        if e["payload"].get("verified") is not None and e["payload"].get("outcome") != "preempted"
    ]
    if not resolutions:
        return CheckResult(
            "F4", "roam goal completion >= 60%", True, "no roam goal resolved in this window", {}
        )
    completed = sum(1 for e in resolutions if e["payload"]["verified"])
    rate = completed / len(resolutions)
    consecutive_timeouts = 0
    two_in_a_row = False
    for e in resolutions:
        if e["payload"].get("outcome") == "timeout":
            consecutive_timeouts += 1
            if consecutive_timeouts >= 2:
                two_in_a_row = True
        else:
            consecutive_timeouts = 0
    passed = rate >= F4_COMPLETION_MIN and not two_in_a_row
    detail = f"{completed}/{len(resolutions)} completed ({rate:.0%})"
    if two_in_a_row:
        detail += "; a goal timed out twice in a row"
    return CheckResult(
        "F4", "roam goal completion >= 60%", passed, detail,
        {"completed": completed, "resolved": len(resolutions), "rate": round(rate, 3),
         "timed_out_twice_in_a_row": two_in_a_row},
    )


# --------------------------------------------------------------------------- #
#  F5 — roam deaths per hour                                                  #
# --------------------------------------------------------------------------- #


def check_f5_roam_deaths(data: ReplayData) -> CheckResult:
    deaths = data.events("death")
    roam_deaths = 0
    for d in deaths:
        st = data.state_at_or_before(d["clock_t"])
        in_mission = bool(st["state"]["mission"].get("active")) if st else False
        if not in_mission:
            roam_deaths += 1
    hours = max(data.duration_hours, 1e-9)
    rate = roam_deaths / hours
    passed = rate < F5_DEATHS_PER_HOUR_MAX
    detail = f"{roam_deaths} roam deaths over {data.duration_hours:.2f}h -> {rate:.1f}/h"
    if data.duration_hours < 0.05:
        detail += " (short window; rate is extrapolated)"
    return CheckResult(
        "F5", "roam deaths < 6/hour", passed, detail,
        {"roam_deaths": roam_deaths, "hours": round(data.duration_hours, 3), "rate_per_hour": round(rate, 2)},
    )


# --------------------------------------------------------------------------- #
#  F6 — a movement task within 3s of every control-regained edge              #
# --------------------------------------------------------------------------- #


def _f6_edges(data: ReplayData) -> list[tuple[float, str]]:
    edges: list[tuple[float, str]] = []
    prev: dict[str, Any] | None = None
    for s in data.states:
        st = s["state"]
        if prev is not None:
            was_down = prev["player"].get("dead") or prev["player"].get("arrested")
            is_down = st["player"].get("dead") or st["player"].get("arrested")
            if was_down and not is_down:
                edges.append((s["clock_t"], "respawn"))
            if (prev["player"].get("interior") is not None) and st["player"].get("interior") is None:
                edges.append((s["clock_t"], "interior_exit"))
            if prev["mission"].get("cutscene_active") and not st["mission"].get("cutscene_active"):
                edges.append((s["clock_t"], "cutscene_end"))
            if prev["mission"].get("active") and not st["mission"].get("active"):
                edges.append((s["clock_t"], "mission_end"))
        prev = st
    return edges


def check_f6_resume_after_control(data: ReplayData) -> CheckResult:
    edges = _f6_edges(data)
    if not edges:
        return CheckResult(
            "F6", "movement task within 3s of control regained", True,
            "no respawn/mission-end/cutscene-end/interior-exit edges in this window", {},
        )
    movement_posts = sorted(
        r["clock_t"] for r in data.by_kind("posted_task") if r.get("movement")
    )
    missed: list[tuple[float, str]] = []
    for t, kind in edges:
        if not any(t <= p <= t + F6_RESUME_WINDOW_S for p in movement_posts):
            missed.append((t, kind))
    passed = not missed
    detail = f"{len(edges) - len(missed)}/{len(edges)} edges answered within {F6_RESUME_WINDOW_S:.0f}s"
    if missed:
        detail += "; missed: " + ", ".join(f"{kind}@t={t:.0f}s" for t, kind in missed)
    return CheckResult(
        "F6", "movement task within 3s of control regained", passed, detail,
        {"edges": len(edges), "missed": len(missed)},
    )


# --------------------------------------------------------------------------- #
#  the table                                                                  #
# --------------------------------------------------------------------------- #

ALL_CHECKS = (
    check_f1_idle_ratio,
    check_f2_something_new,
    check_f3_commentary,
    check_f4_goal_completion,
    check_f5_roam_deaths,
    check_f6_resume_after_control,
)


def run_all(data: ReplayData) -> list[CheckResult]:
    return [fn(data) for fn in ALL_CHECKS]


def format_table(results: list[CheckResult]) -> str:
    rows = [("CHECK", "RESULT", "DETAIL")]
    for r in results:
        rows.append((r.key, "PASS" if r.passed else "FAIL", r.detail))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = []
    for i, row in enumerate(rows):
        line = "  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row))
        lines.append(line)
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, required=True, help="replay records .jsonl (ReplayLog.write_jsonl)")
    parser.add_argument("--states", type=Path, required=True, help="replay states .jsonl (ReplayLog.write_states_jsonl)")
    parser.add_argument("--exit-zero", action="store_true", help="always exit 0 (for dashboards)")
    parser.add_argument("--json", type=Path, default=None, help="also write the numbers as JSON")
    args = parser.parse_args(argv)

    data = ReplayData.load(args.log, args.states)
    print(f"funcheck: {len(data.states)} states, {len(data.records)} records, "
          f"{data.duration_s:.0f}s ({data.duration_hours:.2f}h) covered\n")
    results = run_all(data)
    print(format_table(results))

    counts = Counter("PASS" if r.passed else "FAIL" for r in results)
    print(f"\n{counts['PASS']} passed, {counts['FAIL']} failed")

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {r.key: {"passed": r.passed, "detail": r.detail, **r.numbers} for r in results},
                indent=2,
            ),
            encoding="utf-8",
        )

    if args.exit_zero:
        return 0
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
