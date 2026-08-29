"""Humanizer bounds: reaction jitter, break windows, idle cooldowns, mood styles."""

import random

from wasted_harness.behavior.humanizer import (
    _EVENT_MOOD_INTENT,
    BREAK_EVERY_S,
    BREAK_LENGTH_S,
    EVENT_MOOD,
    IDLE_BEHAVIORS,
    REACTION_JITTER_S,
    BreakScheduler,
    IdlePicker,
    MoodModel,
    reaction_delay,
)
from wasted_harness.events import UNPRODUCED_EVENT_TYPES


def test_reaction_jitter_bounds() -> None:
    rng = random.Random(42)
    delays = [reaction_delay(rng) for _ in range(2000)]
    assert all(REACTION_JITTER_S[0] <= d <= REACTION_JITTER_S[1] for d in delays)
    assert min(delays) < 0.4 and max(delays) > 0.8  # actually spans the range


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_break_scheduling_window() -> None:
    for seed in range(30):
        clock = FakeClock()
        sched = BreakScheduler(rng=random.Random(seed), clock=clock)
        assert not sched.due()
        clock.t = BREAK_EVERY_S[0] - 1
        assert not sched.due()  # never before 40 min
        clock.t = BREAK_EVERY_S[1] + 1
        assert sched.due()  # always by 70 min
        planned = sched.start()
        assert BREAK_LENGTH_S[0] <= planned <= BREAK_LENGTH_S[1]
        assert sched.on_break
        clock.t += planned + 1
        assert sched.finish_if_over() == planned
        assert not sched.on_break


def test_idle_picker_respects_cooldowns_and_moods() -> None:
    clock = FakeClock()
    picker = IdlePicker(random.Random(5), clock=clock)
    b = picker.pick("bored")
    assert b is not None
    # Immediately after, the same behavior is on cooldown.
    b2 = picker.pick("bored")
    if b2 is not None:
        assert b2.name != b.name
    # Scared the agent never honks or flips to a music station for fun.
    clock.t += 10_000.0
    for _ in range(50):
        pick = picker.pick("scared")
        clock.t += 1.0
        if pick is not None:
            assert pick.name not in ("horn_tap", "radio_flip")


def test_idle_actions_are_valid_schema_actions() -> None:
    from wasted_harness.brain.schemas import ACTION_TYPES

    for b in IDLE_BEHAVIORS:
        assert b.action["type"] in ACTION_TYPES


def test_mood_transitions_and_styles() -> None:
    clock = FakeClock()
    m = MoodModel(clock=clock)
    assert m.mood == "chill"
    assert m.driving_style() == "normal"
    m.observe("death")
    assert m.mood == "scared"
    assert m.driving_style() == "avoid_traffic"  # scared avoids traffic (contract note)
    clock.t += 200.0
    m.observe("quiet")
    assert m.mood == "chill"  # scared decays after ~3 min of quiet
    clock.t += 500.0
    m.observe("quiet")
    assert m.mood == "bored"  # long quiet drifts to bored
    m.observe("wanted_high")
    assert m.mood == "scared"
    assert m.driving_style() == "avoid_traffic"
    m.observe("wanted_clear")
    assert m.mood == "smug"
    assert m.driving_style() == "normal"


def test_mood_rules_only_arm_events_the_harness_actually_emits() -> None:
    """No mood rule may key off an event nothing produces.

    `stunt` used to sit in the forced-mood table looking wired up while nothing
    could ever emit it (no airtime/on-ground field in /state v1.2). The intent
    for every §4 event is still declared in `_EVENT_MOOD_INTENT`, but only the
    emitted ones are armed, so wiring the event up in events.py re-arms its mood
    rule and nothing has to remember this file.
    """
    assert UNPRODUCED_EVENT_TYPES, "nothing to check — keep this test honest"
    assert "stunt" in _EVENT_MOOD_INTENT, "keep the intent; it returns in Phase 3"
    assert not (set(EVENT_MOOD) & UNPRODUCED_EVENT_TYPES)
    assert set(EVENT_MOOD) == set(_EVENT_MOOD_INTENT) - UNPRODUCED_EVENT_TYPES

    m = MoodModel(clock=FakeClock())
    for event in UNPRODUCED_EVENT_TYPES:
        # The forced-mood table bypasses the min-hold, so a mapped event would
        # change the mood here on the spot.
        before = m.mood
        assert m.observe(event) == before, f"{event} moves the mood but is never emitted"
    # And the events that DO get emitted still move it.
    assert m.observe("death") == "scared"
