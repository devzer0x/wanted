"""Commentary rotation: never repeats within the last 10, and persists to disk."""

import random
from pathlib import Path

import pytest

from wasted_harness.commentary import (
    BUSTED_LINES,
    DEATH_LINES,
    NO_REPEAT_WINDOW,
    Commentary,
    LineRotation,
)


def test_pools_are_large_enough() -> None:
    assert len(DEATH_LINES) > NO_REPEAT_WINDOW
    assert len(BUSTED_LINES) > NO_REPEAT_WINDOW


def test_no_repeat_within_window(tmp_path: Path) -> None:
    rot = LineRotation("death", DEATH_LINES, tmp_path / "rot.json", random.Random(7))
    picks = [rot.next() for _ in range(60)]
    for i in range(len(picks)):
        window = picks[max(0, i - NO_REPEAT_WINDOW) : i]
        assert picks[i] not in window, f"repeat at pick {i}: {picks[i]!r}"


def test_rotation_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "rot.json"
    first = LineRotation("busted", BUSTED_LINES, path, random.Random(1))
    picks = [first.next() for _ in range(NO_REPEAT_WINDOW)]
    # A fresh instance (simulating a harness restart) must still honor the
    # sliding window over the COMBINED history.
    second = LineRotation("busted", BUSTED_LINES, path, random.Random(2))
    for _ in range(20):
        picks.append(second.next())
    for i in range(len(picks)):
        window = picks[max(0, i - NO_REPEAT_WINDOW) : i]
        assert picks[i] not in window, f"repeat at combined pick {i}"


def test_small_pool_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs >"):
        LineRotation("tiny", ("a", "b", "c"), tmp_path / "rot.json")


def test_commentary_facade_shares_state_file(tmp_path: Path) -> None:
    c = Commentary(tmp_path, random.Random(3))
    line_d = c.death_line()
    line_b = c.busted_line()
    assert line_d in DEATH_LINES
    assert line_b in BUSTED_LINES
    assert (tmp_path / "commentary_rotation.json").exists()


def test_feed_line_is_pure_derivation() -> None:
    from wasted_harness.brain.schemas import ActionModel, DecisionModel

    d = DecisionModel(
        thought="Two stars, freeway is right there. Lose them by the port.",
        say="Relax. I've done this twice.",
        mood="hyped",
        action=ActionModel(type="flee_police", params={}),
        goal="lose the cops by the port",
        confidence=0.7,
    )
    feed = Commentary.feed_line(d, "tactical")
    assert feed == {
        "layer": "tactical",
        "say": d.say,
        "thought": d.thought,
        "mood": "hyped",
        "goal": d.goal,
        "action_type": "flee_police",
    }


# --- WP-C: the decision-derived similarity gate -------------------------------


def test_a_near_identical_line_is_suppressed(tmp_path: Path) -> None:
    c = Commentary(tmp_path, random.Random(4))
    assert c.gate_say("Still on his six, closing the gap.") is True
    # Same words, reordered/slightly reworded — Jaccard over word sets treats
    # this as the same line.
    assert c.gate_say("Closing the gap, still on his six.") is False


def test_a_genuinely_new_line_is_not_suppressed(tmp_path: Path) -> None:
    c = Commentary(tmp_path, random.Random(4))
    assert c.gate_say("Still on his six, closing the gap.") is True
    assert c.gate_say("Four stars now. This is getting expensive.") is True


def test_the_gate_only_compares_against_the_similarity_window(tmp_path: Path) -> None:
    """Once SIMILARITY_WINDOW genuinely different lines have been shown, an
    old near-duplicate is allowed back on screen — this is a short-memory
    mechanical backstop, not the long-term no-repeat memory."""
    from wasted_harness.commentary import SIMILARITY_WINDOW

    c = Commentary(tmp_path, random.Random(4))
    assert c.gate_say("Still on his six, closing the gap.") is True
    for i in range(SIMILARITY_WINDOW):
        assert c.gate_say(f"Genuinely unrelated line number {i}.") is True
    assert c.gate_say("Closing the gap, still on his six.") is True


def test_gate_say_does_not_touch_record_say_or_recent_lines(tmp_path: Path) -> None:
    """The brain's own no-repeat memory must always see the real line said,
    never the gate's opinion of it — record_say is a separate call main.py
    always makes, gated or not."""
    c = Commentary(tmp_path, random.Random(4))
    c.gate_say("Still on his six, closing the gap.")
    suppressed = c.gate_say("Closing the gap, still on his six.")
    assert suppressed is False
    c.record_say("Closing the gap, still on his six.")
    assert "Closing the gap, still on his six." in c.recent.recent()


def test_suppressing_a_line_logs_a_debug_line(tmp_path: Path, caplog) -> None:
    import logging

    c = Commentary(tmp_path, random.Random(4))
    c.gate_say("Still on his six, closing the gap.")
    with caplog.at_level(logging.DEBUG, logger="wasted.commentary"):
        c.gate_say("Closing the gap, still on his six.")
    assert any("suppressed" in r.message for r in caplog.records)
