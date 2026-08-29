"""ActivityRunner: rhythm, preemption, timeouts, and the variety rules."""

import random

from wasted_harness.behavior.activities import (
    ACTIVITY_GAP_S,
    CATALOG,
    CATEGORIES,
    STEP_TIMEOUT_S,
    ActivityPicker,
    ActivityRunner,
)
from wasted_harness.brain.schemas import ACTION_TYPES
from wasted_harness.bridge_client import BRIDGE_TASK_TYPES


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _runner(seed: int = 1) -> tuple[ActivityRunner, ActivityPicker, FakeClock]:
    clock = FakeClock()
    picker = ActivityPicker(random.Random(seed), clock=clock)
    return ActivityRunner(picker, random.Random(seed), clock=clock), picker, clock


def test_every_activity_declares_a_known_category_and_a_brief() -> None:
    for a in CATALOG:
        assert a.category in CATEGORIES, f"{a.name} has category {a.category!r}"
        assert a.brief, f"{a.name} has no brief for the brain"


def test_peek_is_pure_and_commit_books_the_cooldown() -> None:
    clock = FakeClock()
    picker = ActivityPicker(random.Random(4), clock=clock)
    before = {a.name for a, _ in picker.eligible("bored")}
    for _ in range(5):
        picker.peek("bored")
    assert {a.name for a, _ in picker.eligible("bored")} == before, "peek mutated state"
    chosen = picker.peek("bored")
    assert chosen is not None
    picker.commit(chosen)
    assert chosen.name not in {a.name for a, _ in picker.eligible("bored")}


def test_same_activity_and_category_are_damped_after_running() -> None:
    clock = FakeClock()
    picker = ActivityPicker(random.Random(5), clock=clock)
    scenic = next(a for a in CATALOG if a.category == "scenic")
    baseline = {a.name: w for a, w in picker.eligible("chill")}
    picker.commit(scenic)
    clock.t += scenic.cooldown_s + 1  # cooldown expired; only variety applies now
    after = {a.name: w for a, w in picker.eligible("chill")}
    assert after[scenic.name] < baseline[scenic.name], "repeat not damped"
    other_scenic = [
        a.name for a in CATALOG if a.category == "scenic" and a.name != scenic.name
    ]
    assert any(after[n] < baseline[n] for n in other_scenic if n in after), (
        "the whole recent category should be damped, not just the exact activity"
    )


def test_runner_waits_a_jittered_gap_before_the_first_activity() -> None:
    runner, _, clock = _runner()
    assert not runner.due()
    clock.t = ACTIVITY_GAP_S[0] - 1
    assert not runner.due()
    clock.t = ACTIVITY_GAP_S[1] + 1
    assert runner.due()


def _bind(runner: ActivityRunner, step: dict, task_id: str) -> str | None:
    """Stand in for main._issue_activity_step: bind only when a task was posted."""
    posted = task_id if step["type"] in BRIDGE_TASK_TYPES else None
    runner.bind_step_task(posted)
    return posted


def test_runner_walks_a_plan_step_by_step_then_completes() -> None:
    runner, picker, clock = _runner(seed=7)
    picker.last_death_pos = (1.0, 2.0, 3.0)
    clock.t = ACTIVITY_GAP_S[1] + 1
    started = runner.start("chill", "normal")
    assert started is not None
    activity, first = started
    assert first["type"] in ACTION_TYPES
    assert runner.note().startswith("ACTIVITY: " + activity.name)

    steps = 1
    bound = _bind(runner, first, f"t-{steps}")
    while True:
        clock.t += 5.0
        # The snapshot reports whatever id POST /task handed back for this step
        # (or the previous one, for a primitive step that posted nothing).
        step = runner.next_step("done", bound or f"t-{steps}")
        if step is None:
            break
        assert step["type"] in ACTION_TYPES
        steps += 1
        bound = _bind(runner, step, f"t-{steps}")
    assert runner.current is not None and runner.current.finished
    ended = runner.finish("completed")
    assert ended is not None
    name, payload = ended
    assert name == activity.name
    assert payload["outcome"] == "completed"
    assert payload["duration_s"] >= 0
    assert runner.current is None
    assert runner.note().startswith("ACTIVITY: none")


def test_runner_does_not_advance_while_the_task_is_still_running() -> None:
    runner, _, clock = _runner(seed=3)
    clock.t = ACTIVITY_GAP_S[1] + 1
    started = runner.start("chill", "normal")
    assert started is not None
    _bind(runner, started[1], "t-1")
    clock.t += 5.0
    assert runner.next_step("running", "t-1") is None
    assert runner.current is not None and runner.current.step == 0


def test_a_new_task_id_means_the_brain_took_over() -> None:
    runner, _, clock = _runner(seed=9)
    clock.t = ACTIVITY_GAP_S[1] + 1
    started = runner.start("bored", "normal")
    assert started is not None
    assert _bind(runner, started[1], "t-1") == "t-1"  # this step posted t-1
    clock.t += 3.0
    assert runner.next_step("running", "t-2") is None  # someone else is driving
    assert runner.current is None, "a preempted activity must not keep reporting progress"


def test_a_stale_previous_task_id_does_not_advance_the_step() -> None:
    """Regression: the runner used to latch whatever id the next snapshot had.

    POST /task returns the authoritative id, but the harness polls at 2-4 Hz
    against a 60 Hz game thread, so the very next /state can still describe the
    PREVIOUS task — often `done`, since posting preempts it. The old runner
    bound to that stale id and then read its terminal status as "my step
    finished", skipping a step (and, for a two-step plan, ending the activity
    before the car had moved).
    """
    runner, _, clock = _runner(seed=9)
    clock.t = ACTIVITY_GAP_S[1] + 1
    started = runner.start("bored", "normal")
    assert started is not None
    activity, first = started
    assert first["type"] in BRIDGE_TASK_TYPES, "this test needs a bridge-task first step"
    _bind(runner, first, "t-77")  # POST /task said: your task is t-77

    # Snapshot still carrying the preempted previous task, already terminal.
    clock.t += 0.33
    assert runner.next_step("done", "t-76") is None
    assert runner.current is not None and runner.current.step == 0, (
        "the PREVIOUS task's `done` must not advance this step"
    )
    # …and one carrying a preempted-to-failed previous task.
    clock.t += 0.33
    assert runner.next_step("failed", "t-76") is None
    assert runner.current.step == 0

    # Our task finally appears, running: still not done.
    clock.t += 0.33
    assert runner.next_step("running", "t-77") is None
    assert runner.current.step == 0

    # Our task completes: now, and only now, the step advances.
    clock.t += 5.0
    nxt = runner.next_step("done", "t-77")
    assert runner.current.step == 1
    if len(runner.current.plan) > 1:
        assert nxt is not None and nxt["type"] in ACTION_TYPES
    assert activity.name  # the activity is still the one we started


def test_a_step_whose_post_never_landed_cannot_be_completed_by_a_foreign_id() -> None:
    """bind_step_task(None) = the bridge never gave us a task id for this step."""
    runner, _, clock = _runner(seed=9)
    clock.t = ACTIVITY_GAP_S[1] + 1
    started = runner.start("bored", "normal")
    assert started is not None
    assert started[1]["type"] in BRIDGE_TASK_TYPES
    runner.bind_step_task(None)  # POST /task failed
    clock.t += 5.0
    assert runner.next_step("done", "t-5") is None
    assert runner.current is not None and runner.current.step == 0
    clock.t += STEP_TIMEOUT_S + 1
    runner.next_step("done", "t-5")
    assert runner.current.step == 1, "only the timeout may rescue a lost post"


def test_a_step_that_never_completes_times_out() -> None:
    """An unverified coordinate must not wedge the show forever.

    The plan coordinates are curated, not measured (Phase 3 tunes them). A step
    that the engine can never satisfy has to expire, or one bad number parks the
    stream for the rest of the day.
    """
    runner, _, clock = _runner(seed=11)
    clock.t = ACTIVITY_GAP_S[1] + 1
    started = runner.start("chill", "normal")
    assert started is not None
    plan_len = len(runner.current.plan)
    _bind(runner, started[1], "t-1")  # a task that never ends
    assert runner.next_step("running", "t-1") is None
    assert runner.current.step == 0
    clock.t += STEP_TIMEOUT_S + 1
    step = runner.next_step("running", "t-1")
    assert runner.current.step == 1, "a stuck step must expire"
    if plan_len > 1:
        assert step is not None and step["type"] in ACTION_TYPES
    else:
        assert step is None and runner.current.finished


def test_finish_schedules_the_next_gap() -> None:
    runner, _, clock = _runner(seed=13)
    clock.t = ACTIVITY_GAP_S[1] + 1
    runner.start("chill", "normal")
    runner.finish("completed")
    assert not runner.due()
    clock.t += ACTIVITY_GAP_S[1] + 1
    assert runner.due()
