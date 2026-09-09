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


# --- the buffalo loop -------------------------------------------------------------


def _gate():
    """A Commentary whose only job here is `gate_say`, with a real state dir."""
    import tempfile
    from pathlib import Path

    from wasted_harness.commentary import Commentary

    return Commentary(Path(tempfile.mkdtemp()))


def test_the_same_subject_three_lines_running_is_a_loop_and_is_dropped() -> None:
    """Operator, 2026-09-04: "he talks about random buffalo buffalo loop... it
    sounds fake like fake AI generated".

    These three sentences share almost no wording, so the Jaccard gate passes
    every one of them — but they are all about the Buffalo, and back to back
    they read like a stuck bot rather than a person.
    """
    c = _gate()
    assert c.gate_say("The Buffalo took that corner better than I did.") is True
    assert c.gate_say("Still riding in this Buffalo, and it still smells.") is True
    assert c.gate_say("Somebody keyed the Buffalo while I was inside.") is False


def test_two_mentions_are_a_callback_not_a_loop() -> None:
    c = _gate()
    assert c.gate_say("Parked the Buffalo outside the store.") is True
    assert c.gate_say("Back to the Buffalo, then.") is True


def test_the_subject_gate_forgets_once_other_lines_push_it_out_of_the_window() -> None:
    c = _gate()
    assert c.gate_say("The Buffalo is filthy.") is True
    assert c.gate_say("This Buffalo pulls left.") is True
    for filler in (
        "Red light. Nobody else is stopping either.",
        "That siren is not for me yet.",
        "Rain on the windscreen, and no wipers worth the name.",
        "Someone is selling oranges in the middle of the road.",
        "The radio just played this one.",
    ):
        assert c.gate_say(filler) is True
    # The window has rolled over; the subject is fair game again.
    assert c.gate_say("Found the Buffalo where I left it.") is True


def test_a_line_with_no_proper_noun_is_never_subject_gated() -> None:
    c = _gate()
    assert c.gate_say("That was close.") is True
    assert c.gate_say("Still nothing behind me.") is True
    assert c.gate_say("Nothing much happening out here.") is True


def test_sentence_initial_capitals_are_not_treated_as_subjects() -> None:
    """Every line starts with a capital; that says nothing about its subject."""
    c = _gate()
    assert c.gate_say("Traffic is heavy today.") is True
    assert c.gate_say("Traffic has not moved in a while.") is True
    assert c.gate_say("Traffic finally broke up.") is True


def test_his_own_name_is_never_read_as_a_repeating_subject() -> None:
    """The subject gate drops a third line about the same proper noun. The show
    says his name and the city's constantly, by design, so those are stopwords —
    otherwise the gate would silence him for being himself."""
    c = _gate()
    assert c.gate_say("Two stars and the WANTED level is still climbing.") is True
    assert c.gate_say("Cruiser behind me, so the WANTED level holds.") is True
    assert c.gate_say("Lost him at the underpass; the WANTED level drops.") is True

def test_a_banned_brand_can_never_reach_the_public_feed() -> None:
    """The knowledge base and the research briefs name the game's publisher in
    hundreds of places, and that text is model INPUT. This is the gate on model
    OUTPUT, and it is what makes those references safe to leave alone.

    It is not hypothetical: a decision row is rendered verbatim on the public
    feed, so the brand rule has to hold on model output, not just on our prose.
    """
    from wasted_harness.brain.prompts import banned_phrases
    from wasted_harness.brain.schemas import (
        DecisionModel,
        DecisionValidationContext,
        validate_decision_content,
    )

    ctx = DecisionValidationContext(banned_phrases=tuple(banned_phrases()))

    def verdict(say: str) -> str | None:
        decision = DecisionModel(
            thought="t",
            say=say,
            mood="bored",
            goal="roam_the_block",
            action={"type": "wander_drive", "params": {}},
            confidence=0.5,
        )
        return validate_decision_content(decision, ctx).say_reason

    # Matched case-insensitively as a substring, so every casing is caught.
    for line in ("Nice one, Rockstar.", "nice one, rockstar.", "ROCKSTAR built this.", "peak rockstars"):
        assert verdict(line) is not None, f"the banned brand slipped through: {line!r}"

    # And it catches it in `thought` too, not only in the spoken line.
    thought_only = DecisionModel(
        thought="Rockstar should have paved this alley",
        say="Alley.",
        mood="bored",
        goal="roam_the_block",
        action={"type": "wander_drive", "params": {}},
        confidence=0.5,
    )
    assert validate_decision_content(thought_only, ctx).say_reason is not None

    # A clean line is untouched — the gate must not be a blanket refusal.
    assert verdict("That car's mine.") is None
