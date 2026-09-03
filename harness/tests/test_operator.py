"""The operator's button (`wasted_harness.operator`): a tiny, auditable surface.

What matters here: the vocabulary is closed, bad input is refused with a reason
the operator can act on, game-state gating refuses what the game would ignore,
and `drive` expands to the one recovery that always moves him.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_recovery import make_state  # the suite's contract-shaped state builder

from wasted_harness.operator import (
    COMMANDS,
    NUDGE_TYPE_BAN_S,
    OPERATOR_TASK_TYPES,
    Directive,
    OperatorQueue,
    OperatorRefused,
    attach,
    blocking_reason,
    status_from,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# --- vocabulary and enqueue-time validation -------------------------------------


def test_the_vocabulary_is_exactly_five_shapes() -> None:
    assert {"nudge", "goal", "task", "stop"} == COMMANDS  # + status, which is a GET
    assert NUDGE_TYPE_BAN_S == 90.0


def test_unknown_command_is_refused_with_the_menu() -> None:
    q = OperatorQueue(clock=FakeClock())
    with pytest.raises(OperatorRefused) as caught:
        q.submit("teleport")
    assert "unknown command" in caught.value.reason
    assert "nudge" in caught.value.reason and "status" in caught.value.reason


@pytest.mark.parametrize("cmd", ["nudge", "stop"])
def test_argless_commands_reject_a_stray_argument(cmd: str) -> None:
    q = OperatorQueue(clock=FakeClock())
    with pytest.raises(OperatorRefused, match="takes no argument"):
        q.submit(cmd, "freeway_run")


def test_goal_needs_a_plausible_catalog_id() -> None:
    q = OperatorQueue(clock=FakeClock())
    with pytest.raises(OperatorRefused, match="needs a catalog id"):
        q.submit("goal", "")
    with pytest.raises(OperatorRefused, match="not a catalog id"):
        q.submit("goal", "rm -rf /")
    d = q.submit("goal", "freeway_run")
    assert d == Directive("goal", "freeway_run", None, 1000.0)


def test_task_is_limited_to_the_operator_postable_types() -> None:
    q = OperatorQueue(clock=FakeClock())
    with pytest.raises(OperatorRefused, match="not operator-postable"):
        q.submit("task", "set_waypoint")
    assert "drive" in OPERATOR_TASK_TYPES and "fight_ped" in OPERATOR_TASK_TYPES
    for t in sorted(OPERATOR_TASK_TYPES - {"fight_ped"}):
        assert q.submit("task", t).arg == t


def test_fight_ped_needs_an_integer_handle() -> None:
    q = OperatorQueue(clock=FakeClock())
    with pytest.raises(OperatorRefused, match="needs a ped handle"):
        q.submit("task", "fight_ped")
    with pytest.raises(OperatorRefused, match="not an integer"):
        q.submit("task", "fight_ped", "lamar")
    assert q.submit("task", "fight_ped", "4321").handle == 4321
    assert q.submit("task", "fight_ped", 4321).handle == 4321


def test_a_full_queue_is_refused_not_dropped() -> None:
    q = OperatorQueue(clock=FakeClock(), maxsize=2)
    q.submit("nudge")
    q.submit("nudge")
    with pytest.raises(OperatorRefused, match="queue is full"):
        q.submit("nudge")


def test_drain_is_fifo_and_non_blocking() -> None:
    q = OperatorQueue(clock=FakeClock())
    assert q.drain() == []
    q.submit("nudge")
    q.submit("task", "drive")
    got = q.drain()
    assert [d.cmd for d in got] == ["nudge", "task"]
    assert q.drain() == []
    assert q.pending == 0


# --- what a `task` directive actually posts --------------------------------------


def test_drive_expands_to_car_then_wander() -> None:
    steps = Directive("task", "drive").steps()
    assert [s["type"] for s in steps] == ["enter_nearest_vehicle", "wander_drive"]
    assert steps[1]["params"]["style"] == "rushed"


def test_fight_ped_carries_its_handle_and_non_task_shapes_post_nothing() -> None:
    assert Directive("task", "fight_ped", 77).steps() == [{"type": "fight_ped", "params": {"handle": 77}}]
    assert Directive("task", "shoot_at").steps() == [{"type": "shoot_at", "params": {}}]
    assert Directive("nudge").steps() == []
    assert Directive("goal", "freeway_run").steps() == []
    assert Directive("stop").steps() == []


# --- apply-time gating (needs game state) -----------------------------------------


def test_free_roam_with_control_blocks_nothing() -> None:
    for cmd in ("nudge", "goal", "task", "stop"):
        assert blocking_reason(make_state(), Directive(cmd, "x" if cmd in ("goal", "task") else "")) is None


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"dead": True}, "dead"),
        ({"arrested": True}, "arrested"),
        ({"cutscene_active": True}, "cutscene"),
        ({"switch_in_progress": True}, "switch"),
        ({"retry_in_flight": True}, "retry"),
    ],
)
def test_states_the_game_would_ignore_are_refused_with_a_reason(over: dict, needle: str) -> None:
    reason = blocking_reason(make_state(**over), Directive("task", "drive"))
    assert reason is not None and needle in reason


def test_missions_belong_to_the_follower_but_drive_and_stop_still_work() -> None:
    mission = make_state(mission_active=True)
    assert "mission" in (blocking_reason(mission, Directive("nudge")) or "")
    assert "mission" in (blocking_reason(mission, Directive("goal", "freeway_run")) or "")
    assert blocking_reason(mission, Directive("task", "drive")) is None
    assert blocking_reason(mission, Directive("stop")) is None


# --- status document -------------------------------------------------------------


def test_status_names_what_is_holding_him_and_who_is_near() -> None:
    state = make_state(
        nearby_peds=[
            {
                "handle": 501,
                "model": "lamardavis",
                "distance": 3.2,
                "relationship": "friendly",
                "pos": {"x": 1.0, "y": 0.0, "z": 0.0},
                "attacking_me": False,
            }
        ],
        threat={"attacker_handle": 501, "being_jacked_by": None},
    )
    doc = status_from(
        state,
        still_for_s=312.4,
        goal_id="roam_the_block",
        available_goals=["freeway_run", "steal_nice_car"],
        held_by="governor_l3",
        governor_level=3,
        last_applied="nudge",
        last_refusal=None,
    )
    assert doc["still_for_s"] == 312.4
    assert doc["goal"] == "roam_the_block"
    assert doc["available_goals"] == ["freeway_run", "steal_nice_car"]
    assert doc["held_by"] == "governor_l3"
    assert doc["attacker_handle"] == 501
    assert doc["nearby_peds"][0]["handle"] == 501
    assert doc["last_task"]["status"] == "idle"
    assert doc["mission_active"] is False


# --- the HTTP routes, end to end through a real FastAPI app -----------------------


def _client() -> tuple[TestClient, OperatorQueue]:
    app = FastAPI()
    q = OperatorQueue(clock=FakeClock())
    attach(app, q)
    return TestClient(app), q


def test_post_operator_accepts_and_queues() -> None:
    client, q = _client()
    r = client.post("/operator", json={"cmd": "goal", "arg": "freeway_run"})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True and body["cmd"] == "goal" and body["arg"] == "freeway_run"
    assert body["queued"] == 1
    assert [d.arg for d in q.drain()] == ["freeway_run"]


def test_post_operator_refuses_with_400_and_a_reason() -> None:
    client, _ = _client()
    r = client.post("/operator", json={"cmd": "task", "arg": "fight_ped"})
    assert r.status_code == 400
    assert "needs a ped handle" in r.json()["error"]
    r = client.post("/operator", json={"cmd": "godmode"})
    assert r.status_code == 400 and "unknown command" in r.json()["error"]
    r = client.post("/operator", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_status_route_is_503_before_the_first_tick_then_serves_the_snapshot() -> None:
    client, q = _client()
    assert client.get("/operator/status").status_code == 503
    q.status.publish({"still_for_s": 42.0, "goal": None})
    r = client.get("/operator/status")
    assert r.status_code == 200 and r.json()["still_for_s"] == 42.0


def test_apply_bookkeeping_is_visible_to_status() -> None:
    q = OperatorQueue(clock=FakeClock())
    d = q.submit("nudge")
    q.refused(d, "a cutscene is playing")
    assert q.last_refusal == "nudge: a cutscene is playing"
    q.applied(d, "goal closed, force_pick armed")
    assert q.last_refusal is None
    assert q.last_applied == "nudge (goal closed, force_pick armed)"
