"""F1-F6, each checker exercised on a tiny hand-built log: one PASS shape, one
FAIL shape. These logs are NOT recordings (CLAUDE.md rule 1 is about
`tests/fixtures/*.json`; these are pure-function inputs built in code, same
status as `support.states.make_state`) and are never written to
`tests/fixtures/`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from support.states import make_state

# `tools/` is a sibling of `tests/`, not a package under `wasted_harness` —
# add the harness root so `import tools.funcheck` works the same way pytest
# already makes `support` importable for `tests/`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import funcheck as fc


def _state_row(tick: int, clock_t: float, **over) -> dict:
    state = make_state(tick=tick, **over)
    return {
        "tick": tick,
        "clock_t": clock_t,
        "wall_ts": "",
        "state": json.loads(state.model_dump_json(by_alias=True)),
    }


def _rec(kind: str, tick: int, clock_t: float, **fields) -> dict:
    return {"kind": kind, "tick": tick, "clock_t": clock_t, **fields}


def _data(states: list[dict], records: list[dict]) -> fc.ReplayData:
    return fc.ReplayData(records=records, states=states)


# --- F1: idle ratio -------------------------------------------------------


def test_f1_pass_when_a_movement_task_is_running_throughout() -> None:
    states = [
        _state_row(i, float(i), task_id="t-1", task_type="drive_to", task_status="running")
        for i in range(20)
    ]
    result = fc.check_f1_idle_ratio(_data(states, []))
    assert result.passed, result.detail


def test_f1_fails_when_idle_more_than_5_percent_of_a_window() -> None:
    # 620s window (> the 600s check window), first 60s idle (task_status idle),
    # the rest running — idle ratio inside the first 10-min window is high.
    states = []
    t = 0.0
    while t <= 620.0:
        idle_phase = t <= 60.0
        states.append(
            _state_row(
                int(t), t,
                task_id="t-1",
                task_type="drive_to",
                task_status="idle" if idle_phase else "running",
            )
        )
        t += 5.0
    result = fc.check_f1_idle_ratio(_data(states, []))
    assert not result.passed, result.detail


# --- F2: something new every 60s -------------------------------------------


def test_f2_pass_when_events_are_frequent() -> None:
    records = [
        _rec("event", i, float(i) * 30.0, type="activity_end", payload={})
        for i in range(10)
    ]
    states = [_state_row(0, 0.0), _state_row(1, 270.0)]
    result = fc.check_f2_something_new(_data(states, records))
    assert result.passed, result.detail


def test_f2_fails_on_a_long_silent_gap() -> None:
    records = [
        _rec("event", 0, 0.0, type="activity_start", payload={}),
        _rec("event", 1, 500.0, type="activity_end", payload={}),
    ]
    states = [_state_row(0, 0.0), _state_row(1, 500.0)]
    result = fc.check_f2_something_new(_data(states, records))
    assert not result.passed, result.detail


# --- F3: commentary hygiene -------------------------------------------------


def test_f3_pass_for_a_grounded_varied_line_near_an_event() -> None:
    states = [_state_row(0, 10.0)]
    records = [
        _rec("event", 0, 9.0, type="activity_start", payload={}),
        _rec("say", 0, 10.0, text="Third green light in a row. Something's wrong.", mood="chill"),
    ]
    result = fc.check_f3_commentary(_data(states, records))
    assert result.passed, result.detail


def test_f3_fails_on_a_banned_phrase() -> None:
    states = [_state_row(0, 10.0)]
    records = [
        _rec("event", 0, 9.0, type="activity_start", payload={}),
        _rec("say", 0, 10.0, text="As an AI, I cannot enjoy this crash.", mood="chill"),
    ]
    result = fc.check_f3_commentary(_data(states, records))
    assert not result.passed, result.detail
    assert "banned phrase" in result.detail


def test_f3_fails_on_a_name_not_present_in_state() -> None:
    states = [_state_row(0, 10.0)]  # no nearby peds, no entity blips
    records = [
        _rec("event", 0, 9.0, type="activity_start", payload={}),
        _rec("say", 0, 10.0, text="Trevor's got the wheel, we're clear.", mood="chill"),
    ]
    result = fc.check_f3_commentary(_data(states, records))
    assert not result.passed
    assert "names not in STATE" in result.detail


def test_f3_fails_on_a_near_duplicate_line() -> None:
    states = [_state_row(0, 10.0), _state_row(1, 20.0)]
    records = [
        _rec("event", 0, 9.0, type="activity_start", payload={}),
        _rec("say", 0, 10.0, text="Third green light in a row.", mood="chill"),
        _rec("event", 1, 19.0, type="activity_end", payload={}),
        _rec("say", 1, 20.0, text="Third green light in a row.", mood="chill"),
    ]
    result = fc.check_f3_commentary(_data(states, records))
    assert not result.passed
    assert "overlaps a recent line" in result.detail


def test_f3_fails_on_a_line_with_nothing_happening_nearby() -> None:
    states = [_state_row(0, 300.0)]
    records = [_rec("say", 0, 300.0, text="Quiet out here today.", mood="chill")]
    result = fc.check_f3_commentary(_data(states, records))
    assert not result.passed
    assert "no event/task/wheel activity" in result.detail


# --- F4: roam goal completion -----------------------------------------------


def test_f4_pass_when_most_goals_complete() -> None:
    records = [
        _rec("event", i, float(i) * 10, type="activity_end",
             payload={"outcome": "completed", "verified": True})
        for i in range(3)
    ] + [
        _rec("event", 3, 40.0, type="activity_end", payload={"outcome": "timeout", "verified": False}),
    ]
    result = fc.check_f4_goal_completion(_data([], records))
    assert result.passed, result.detail


def test_f4_fails_when_two_goals_time_out_in_a_row() -> None:
    records = [
        _rec("event", 0, 0.0, type="activity_end", payload={"outcome": "completed", "verified": True}),
        _rec("event", 1, 10.0, type="activity_end", payload={"outcome": "timeout", "verified": False}),
        _rec("event", 2, 20.0, type="activity_end", payload={"outcome": "timeout", "verified": False}),
    ]
    result = fc.check_f4_goal_completion(_data([], records))
    assert not result.passed, result.detail
    assert "twice in a row" in result.detail


# --- F5: roam deaths per hour ------------------------------------------------


def test_f5_pass_with_a_long_quiet_run() -> None:
    states = [_state_row(0, 0.0, mission_active=False), _state_row(1, 3600.0, mission_active=False)]
    records = [_rec("event", 0, 300.0, type="death", payload={"deaths_total": 1})]
    result = fc.check_f5_roam_deaths(_data(states, records))
    assert result.passed, result.detail


def test_f5_fails_with_many_deaths_in_a_short_window() -> None:
    states = [_state_row(0, 0.0, mission_active=False), _state_row(1, 600.0, mission_active=False)]
    records = [
        _rec("event", i, float(i) * 60, type="death", payload={"deaths_total": i + 1})
        for i in range(10)
    ]
    result = fc.check_f5_roam_deaths(_data(states, records))
    assert not result.passed, result.detail


# --- F6: movement task within 3s of control regained -------------------------


def test_f6_pass_when_a_movement_task_follows_a_respawn_quickly() -> None:
    states = [
        _state_row(0, 0.0, dead=True, health=0),
        _state_row(1, 5.0, dead=False, health=200),
    ]
    records = [
        _rec("posted_task", 1, 6.0, movement=True, type="enter_nearest_vehicle", params={}, owner="resume"),
    ]
    result = fc.check_f6_resume_after_control(_data(states, records))
    assert result.passed, result.detail


def test_f6_fails_when_nothing_moves_him_after_a_respawn() -> None:
    states = [
        _state_row(0, 0.0, dead=True, health=0),
        _state_row(1, 5.0, dead=False, health=200),
        _state_row(2, 30.0, dead=False, health=200),
    ]
    result = fc.check_f6_resume_after_control(_data(states, []))
    assert not result.passed, result.detail
    assert "respawn" in result.detail


# --- the table + CLI shape ---------------------------------------------------


def test_run_all_returns_one_result_per_check_in_order() -> None:
    results = fc.run_all(_data([_state_row(0, 0.0)], []))
    assert [r.key for r in results] == ["F1", "F2", "F3", "F4", "F5", "F6"]


def test_format_table_is_stable_and_readable() -> None:
    results = fc.run_all(_data([_state_row(0, 0.0)], []))
    table = fc.format_table(results)
    assert "CHECK" in table
    assert "F1" in table


def test_main_exits_nonzero_on_any_fail(tmp_path: Path) -> None:
    states = [_state_row(0, 0.0, dead=True, health=0), _state_row(1, 30.0, dead=False, health=200)]
    (tmp_path / "s.jsonl").write_text("\n".join(json.dumps(s) for s in states), encoding="utf-8")
    (tmp_path / "r.jsonl").write_text("", encoding="utf-8")
    rc = fc.main(["--log", str(tmp_path / "r.jsonl"), "--states", str(tmp_path / "s.jsonl")])
    assert rc != 0  # F6: nothing moved him after the respawn


def test_main_exit_zero_flag_always_returns_zero(tmp_path: Path) -> None:
    states = [_state_row(0, 0.0, dead=True, health=0), _state_row(1, 30.0, dead=False, health=200)]
    (tmp_path / "s.jsonl").write_text("\n".join(json.dumps(s) for s in states), encoding="utf-8")
    (tmp_path / "r.jsonl").write_text("", encoding="utf-8")
    rc = fc.main(["--log", str(tmp_path / "r.jsonl"), "--states", str(tmp_path / "s.jsonl"), "--exit-zero"])
    assert rc == 0


def test_main_writes_json_numbers_when_asked(tmp_path: Path) -> None:
    states = [_state_row(0, 0.0)]
    (tmp_path / "s.jsonl").write_text(json.dumps(states[0]), encoding="utf-8")
    (tmp_path / "r.jsonl").write_text("", encoding="utf-8")
    out = tmp_path / "numbers.json"
    fc.main(["--log", str(tmp_path / "r.jsonl"), "--states", str(tmp_path / "s.jsonl"),
              "--exit-zero", "--json", str(out)])
    payload = json.loads(out.read_text())
    assert set(payload) == {"F1", "F2", "F3", "F4", "F5", "F6"}
