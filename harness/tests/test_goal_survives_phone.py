"""A locked roam goal must survive a phone task, not die as "stuck".

`ActivityRunner.next_step` abandons the step machine when it sees a foreign
RUNNING task, but only the wheel's preempt hook closes the roam goal. A phone
task (`answer_call` / `reject_call`, CONTRACTS v1.13) is not a movement task,
never touches the wheel, and still replaces the step's bridge task — so the
goal sat locked with nothing running until the stuck watchdog failed it ~20 s
later. Found in the 2026-09-03 soak trace; with the always-answer policy that
would have been every call.
"""

from __future__ import annotations

from support.replayer import ReplayHarness
from support.states import make_state


def _run(h: ReplayHarness, state, ticks: int, step_s: float = 0.33) -> None:
    for _ in range(ticks):
        h.clock.advance(step_s)
        h.feed(state)


def _roam_posts(h: ReplayHarness) -> list[dict]:
    return [r for r in h.log.records if r["kind"] == "posted_task" and r.get("owner") == "roam"]


def test_a_goal_survives_answer_call_and_restarts_its_plan() -> None:
    h = ReplayHarness(seed=1)
    _run(h, make_state(in_vehicle=True), 3)
    assert h.roam.current is not None and h.activity_runner.current is not None
    goal_id = h.roam.current.goal.id
    before = len(_roam_posts(h))

    # The phone reflex's task is on the wire and still running: the goal's own
    # bridge task is gone, and the step machine will abandon itself.
    ringing = make_state(
        in_vehicle=True, task_type="answer_call", task_status="running", task_id="phone-1",
        phone={"ringing": False, "in_call": True},
    )
    _run(h, ringing, 1)
    assert h.roam.current is not None and h.roam.current.goal.id == goal_id
    # While the phone task runs nothing is posted over it (it would be preempted).
    assert len(_roam_posts(h)) == before

    # The phone task is done: the goal restarts its plan on the same lock.
    done = make_state(
        in_vehicle=True, task_type="answer_call", task_status="done", task_id="phone-1",
        phone={"ringing": False, "in_call": True},
    )
    _run(h, done, 2)
    assert h.roam.current is not None and h.roam.current.goal.id == goal_id
    assert h.activity_runner.current is not None, "the step machine was not restarted"
    assert len(_roam_posts(h)) > before, "the goal posted nothing after the call"
    ends = [r for r in h.log.records if r["kind"] == "event" and r.get("type") == "activity_end"]
    assert not ends, f"the goal was ended: {ends}"
