"""T5 (findings.md R3): the dashboard CURRENT GOAL is written by the PLUGIN.

`Harness.goal_text` (the site's CURRENT GOAL and `stats.current_goal`) reads
exactly three fixed sources, in order: a locked roam goal's own description,
the tracked mission's name (+ its first objective), or a fixed neutral
phrase. `self.current_goal` (the director's free-text `goal` field) is never
read here — that is proven both behaviourally and with a static check over
the source, because a future edit re-adding `or self.current_goal` would
compile and pass every OTHER test.

Mood: the published mood is the tracked `MoodModel`, computed from state
(health/wanted/recent events), never the model's own `mood` field — the T3
test suite (`test_say_gating.py::test_published_mood_is_the_tracked_mood_not_the_models`)
covers the behavioural half; this file adds the static check.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

from test_roam import engine, observed, veh
from test_roam import make_state as roam_state

from wasted_harness.behavior.roam import GOALS_BY_ID
from wasted_harness.main import INITIAL_GOAL, NEUTRAL_GOAL_TEXT, Harness

MAIN_PY = Path(__file__).resolve().parents[1] / "wasted_harness" / "main.py"


def _bare(state_dir=None) -> Harness:
    """A real `Harness` object with only what `goal_text` touches, in the
    same `object.__new__` idiom `test_main_loop_resilience.py::_harness` uses
    (its own `__init__` demands a live API key and a bridge)."""
    h = Harness.__new__(Harness)
    return h


# --- behavioural: the three sources, in order -----------------------------------


def test_neutral_phrase_when_nothing_is_locked_or_tracked() -> None:
    h = _bare()
    h.roam, _ = engine()
    h.current_mission = None
    h.current_goal = "a model-written goal that must never show up"
    assert h.goal_text == NEUTRAL_GOAL_TEXT


def test_a_locked_roam_goal_outranks_everything() -> None:
    h = _bare()
    e, _clock = engine()
    state = observed(
        e, roam_state(nearby_vehicles=[veh(1, "adder", "Super", 12.0, pos=(12.0, 0.0, 0.0))])
    )
    e.pick(state, goal_id="steal_nice_car")
    h.roam = e
    h.current_mission = {"name": "a mission that must not show through a locked roam goal"}
    h.current_goal = "a model-written goal that must never show up"
    assert h.goal_text == GOALS_BY_ID["steal_nice_car"].description


def test_the_tracked_mission_name_and_first_objective_when_no_roam_goal_is_locked() -> None:
    h = _bare()
    h.roam, _ = engine()
    h.current_mission = {
        "name": "Complications",
        "objectives": ["Meet Lamar", "Chase the target", "Lose the cops"],
    }
    h.current_goal = "a model-written goal that must never show up"
    assert h.goal_text == "Complications — Meet Lamar"


def test_the_tracked_mission_name_alone_when_it_has_no_objectives() -> None:
    h = _bare()
    h.roam, _ = engine()
    h.current_mission = {"name": "Some Unrecorded Job", "objectives": []}
    h.current_goal = "a model-written goal that must never show up"
    assert h.goal_text == "Some Unrecorded Job"


def test_the_model_written_current_goal_never_reaches_the_box() -> None:
    """The direct behavioural proof: whatever `self.current_goal` holds
    (director-writable, CONTRACTS §2) is absent from every branch's output."""
    poison = "SHOULD NEVER APPEAR ON THE DASHBOARD"
    h = _bare()
    h.roam, _ = engine()
    h.current_mission = None
    h.current_goal = poison
    assert poison not in h.goal_text
    h.current_mission = {"name": "A Job", "objectives": ["do the thing"]}
    assert poison not in h.goal_text


# --- static: the model's goal text has no path to the box -----------------------


def _goal_text_source() -> str:
    src = inspect.getsource(Harness.goal_text.fget)
    return src


def test_goal_text_property_never_references_current_goal() -> None:
    """A regression here means someone re-added `or self.current_goal` (or
    equivalent) to the property — every other test in this file would still
    pass if that fallback were only reached in a code path they do not probe,
    so this reads the actual source rather than trusting behavioural coverage
    alone."""
    src = _goal_text_source()
    tree = ast.parse(textwrap.dedent(src))
    names = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "current_goal"
    }
    assert not names, "goal_text must not read self.current_goal at all"


def test_goal_text_property_reads_exactly_the_three_documented_sources() -> None:
    src = _goal_text_source()
    assert "self.roam.dashboard_goal()" in src
    assert "self.current_mission" in src
    assert "NEUTRAL_GOAL_TEXT" in src


def test_stats_current_goal_is_written_from_the_goal_text_property() -> None:
    """`stats.current_goal` (CONTRACTS §5) — the site row — must come from
    the property under test, not from `self.current_goal`/`d.goal` directly."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert '"current_goal": self.goal_text,' in src
    assert '"current_goal": self.current_goal' not in src


def test_no_bus_say_event_ever_publishes_the_models_own_mood() -> None:
    """T5: the model's `mood` is advisory at most. Every `bus.publish("say",
    ...)` call site in main.py must carry the TRACKED mood
    (`self.mood.mood`), never `d.mood`/`decision.mood` straight from the
    model's own output."""
    src = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    say_publishes = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "publish"
        ):
            args = node.args
            if not args or not (isinstance(args[0], ast.Constant) and args[0].value == "say"):
                continue
            say_publishes += 1
            payload = args[1] if len(args) > 1 else None
            assert isinstance(payload, ast.Dict), "say publish must pass a literal payload dict"
            mood_value = None
            for key, value in zip(payload.keys, payload.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "mood":
                    mood_value = value
            assert mood_value is not None, "say payload must carry a mood key"
            rendered = ast.dump(mood_value)
            assert "current_goal" not in rendered  # sanity: real node, not a stub
            assert "attr='mood'" in rendered
            assert "self" in rendered or "mood" in rendered
            # The precise, positive assertion: it reads the TRACKER's mood
            # (an attribute access whose value chain is `self.mood.mood` /
            # `mood or self.mood.mood`), never a bare `d.mood`/`decision.mood`.
            names_in_expr = {
                n.id for n in ast.walk(mood_value) if isinstance(n, ast.Name)
            }
            assert "d" not in names_in_expr, (
                f"say publish reads the model's own mood field directly: {rendered}"
            )
    assert say_publishes >= 1, "expected at least one bus.publish('say', ...) call site"


def test_initial_goal_constant_is_still_a_thing_current_goal_may_hold() -> None:
    """Sanity: `current_goal` is still a real, live attribute elsewhere in the
    harness (recorded on `decisions`, fed back into the prompt) — T5 only
    removes it from the DASHBOARD path, it does not delete the field."""
    assert isinstance(INITIAL_GOAL, str) and INITIAL_GOAL
