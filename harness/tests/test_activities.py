"""Activity selection: weights, cooldowns, mood affinity, chaos budget."""

import random

from wasted_harness.behavior.activities import (
    CATALOG,
    CHAOS_BUDGET_PER_HOUR,
    ActivityPicker,
)
from wasted_harness.brain.schemas import ACTION_TYPES


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_catalog_covers_the_briefed_set() -> None:
    names = {a.name for a in CATALOG}
    assert names == {
        "cruise_to_landmark",
        "steal_nicer_car",
        "stunt_jump",
        "mount_chiliad_run",
        "beach_pier",
        "deliberate_chase",
        "park_and_watch",
        "go_home",
        "visit_death_spot",
    }


def test_plans_use_only_contract_actions() -> None:
    clock = FakeClock()
    picker = ActivityPicker(random.Random(1), clock=clock)
    picker.last_death_pos = (100.0, 200.0, 30.0)
    for a in CATALOG:
        plan = picker.build_plan(a, "normal")
        assert plan, f"{a.name} produced an empty plan"
        for step in plan:
            assert step["type"] in ACTION_TYPES, f"{a.name} uses unknown action {step['type']}"


def test_cooldown_blocks_repeat() -> None:
    clock = FakeClock()
    picker = ActivityPicker(random.Random(2), clock=clock)
    chosen = picker.pick("chill")
    assert chosen is not None
    names_after = {a.name for a, _ in picker.eligible("chill")}
    assert chosen.name not in names_after
    clock.t += chosen.cooldown_s + 1
    assert chosen.name in {a.name for a, _ in picker.eligible("chill")}


def test_mood_affinity_shapes_selection() -> None:
    # scared: deliberate_chase affinity is 0.0 -> never eligible with weight > 0
    clock = FakeClock()
    picker = ActivityPicker(random.Random(3), clock=clock)
    scared = {a.name for a, _ in picker.eligible("scared")}
    assert "deliberate_chase" not in scared
    bored = dict((a.name, w) for a, w in picker.eligible("bored"))
    chill = dict((a.name, w) for a, w in picker.eligible("chill"))
    assert bored["steal_nicer_car"] > chill["steal_nicer_car"]  # boredom breeds crime


def test_chaos_budget_limits_chases() -> None:
    clock = FakeClock()
    picker = ActivityPicker(random.Random(4), clock=clock)
    assert picker.chaos_available() == CHAOS_BUDGET_PER_HOUR
    # Force-run the chase by spending its chaos through pick(): find it eligible,
    # then simulate having run it via the internal bookkeeping pick() performs.
    chase = next(a for a in CATALOG if a.name == "deliberate_chase")
    picker._last_run[chase.name] = -1e12
    picker._chaos_spent.append((clock.t, chase.chaos_cost))
    assert picker.chaos_available() == CHAOS_BUDGET_PER_HOUR - chase.chaos_cost
    # Budget spent: the chase (cost 2.0) is no longer affordable this hour.
    assert "deliberate_chase" not in {a.name for a, _ in picker.eligible("bored")}
    clock.t += 3601.0  # chaos refills on the rolling hour
    picker._last_run.pop(chase.name)
    assert "deliberate_chase" in {a.name for a, _ in picker.eligible("bored")}


def test_visit_death_spot_requires_a_death() -> None:
    clock = FakeClock()
    picker = ActivityPicker(random.Random(5), clock=clock)
    assert "visit_death_spot" not in {a.name for a, _ in picker.eligible("bored")}
    picker.last_death_pos = (10.0, 20.0, 30.0)
    assert "visit_death_spot" in {a.name for a, _ in picker.eligible("bored")}
