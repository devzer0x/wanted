"""Recent-line memory: the prompt promises the harness tracks his last lines.

commentary_style.md tells the agent "the harness shows you your recent lines; treat
that list as forbidden ground". These tests are why that sentence is true.
"""

import random
from pathlib import Path

from wasted_harness.commentary import (
    BUSTED_LINES,
    DEATH_LINES,
    NO_REPEAT_WINDOW,
    RECENT_LINES_SHOWN,
    Commentary,
    RecentLines,
)


def test_pools_have_no_duplicate_lines() -> None:
    assert len(set(DEATH_LINES)) == len(DEATH_LINES)
    assert len(set(BUSTED_LINES)) == len(BUSTED_LINES)
    # Comfortably above the no-repeat window, so a pick always exists.
    assert len(DEATH_LINES) >= NO_REPEAT_WINDOW * 2
    assert len(BUSTED_LINES) >= NO_REPEAT_WINDOW * 2


def test_banner_lines_stay_short_enough_to_read_on_a_banner() -> None:
    for line in DEATH_LINES + BUSTED_LINES:
        assert len(line.split()) <= 14, f"too long for a banner: {line!r}"
        assert line.strip() == line
        assert not line.startswith(("As an AI", "as an AI"))


def test_recent_lines_persist_and_report_repeats(tmp_path: Path) -> None:
    path = tmp_path / "rot.json"
    recent = RecentLines(path)
    assert recent.context_block() == ""
    assert recent.add("Put it on the tab.") is False
    assert recent.add("Physics: undefeated.") is False
    # Same line, different punctuation/case is still a repeat.
    assert recent.add("put it on the tab") is True

    reloaded = RecentLines(path)  # a restart must not forget
    assert "Put it on the tab." in reloaded.recent()
    block = reloaded.context_block()
    assert "do not repeat these" in block
    assert '"Physics: undefeated."' in block


def test_recent_lines_window_is_bounded(tmp_path: Path) -> None:
    recent = RecentLines(tmp_path / "rot.json")
    for i in range(100):
        recent.add(f"line number {i}")
    assert len(recent.recent()) == RECENT_LINES_SHOWN
    assert recent.recent()[-1] == "line number 99"


def test_commentary_records_banner_and_model_lines_in_one_history(tmp_path: Path) -> None:
    c = Commentary(tmp_path, random.Random(3))
    death = c.death_line()
    assert death in c.recent.recent()
    assert c.record_say(death) is True  # the model must not echo the banner line
    assert c.record_say("Something else entirely.") is False


def test_rotation_and_recent_lines_share_the_file_without_clobbering(tmp_path: Path) -> None:
    c = Commentary(tmp_path, random.Random(11))
    c.death_line()
    c.busted_line()
    c.record_say("A model line.")
    reloaded = Commentary(tmp_path, random.Random(12))
    assert reloaded.death._recent, "death rotation history lost"
    assert reloaded.busted._recent, "busted rotation history lost"
    assert "A model line." in reloaded.recent.recent()
