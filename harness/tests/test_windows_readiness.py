"""Windows-server readiness: console encoding, path anchoring, mood clock.

None of these need Windows to test — they test the code that behaves
differently there. What genuinely cannot be tested here (SendInput actually
reaching the game window, dxcam against the virtual display, OBS on the real
box) is marked as such in the report, never asserted.
"""

import io
import logging
import os
import random
from pathlib import Path

import pytest

from wasted_harness.behavior.humanizer import MOOD_TIMER_RANGE_S, MoodModel
from wasted_harness.brain.schemas import MOODS
from wasted_harness.brain.tactical import (
    L1_TIMER_SCALE,
    MAX_TACTICAL_CALLS_PER_HOUR,
    MEASURED_WARM_TACTICAL_CALL_USD,
    MIN_TACTICAL_GAP_S,
    TACTICAL_TIMER_RANGE_S,
    TacticalCadence,
    calls_per_hour_by_mood,
)
from wasted_harness.logsetup import LogfmtFormatter, force_utf8_console
from wasted_harness.perception import Delta
from wasted_harness.settings import ConfigError, Settings


class FakeClock:
    def __init__(self, t: float = 5_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# --- console encoding ---------------------------------------------------------

# Runtime strings in this package contain these; a legacy Windows console code
# page (cp437/cp850) cannot encode them, and an unhandled UnicodeEncodeError in
# a logging handler loses the run log.
NON_ASCII_SAMPLE = "CONTRACTS §4 — bridge unreachable … re-init"


def test_legacy_codepage_stream_would_fail_without_the_fix() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp437", errors="strict", newline="")
    with pytest.raises(UnicodeEncodeError):
        stream.write(NON_ASCII_SAMPLE)
        stream.flush()


def test_reconfigured_stream_survives_non_ascii_log_records() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp437", errors="strict", newline="")
    stream.reconfigure(encoding="utf-8", errors="replace")  # what force_utf8_console does
    handler = logging.StreamHandler(stream)
    handler.setFormatter(LogfmtFormatter())
    record = logging.LogRecord(
        "wasted.test", logging.WARNING, __file__, 1, NON_ASCII_SAMPLE, None, None
    )
    record.kv = {"path": "C:\\wasted\\state\\queue.jsonl", "note": "— dash"}
    handler.emit(record)
    handler.flush()
    out = raw.getvalue().decode("utf-8")
    assert "§4" in out
    assert "level=WARNING" in out
    assert "C:\\wasted\\state\\queue.jsonl" in out


def test_force_utf8_console_never_raises_on_odd_streams() -> None:
    force_utf8_console()  # real stdout/stderr under pytest capture
    force_utf8_console()  # idempotent


# --- settings / paths ---------------------------------------------------------


def test_state_and_pricing_paths_are_absolute_and_cwd_independent(
    tmp_path: Path, monkeypatch
) -> None:
    """The server starts the harness from a scheduled task with a foreign CWD."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WASTED_STATE_DIR", raising=False)
    monkeypatch.delenv("WASTED_PRICING_FILE", raising=False)
    settings = Settings.load()
    assert settings.state_dir.is_absolute()
    assert settings.pricing_file.is_absolute()
    assert settings.pricing_file.exists()
    assert tmp_path not in settings.state_dir.parents


def test_state_dir_env_override_expands_a_home_relative_path(monkeypatch) -> None:
    monkeypatch.setenv("WASTED_STATE_DIR", os.path.join("~", "wasted-state"))
    settings = Settings.load()
    assert "~" not in str(settings.state_dir)
    assert settings.state_dir.is_absolute()


def test_bad_numeric_env_fails_with_a_config_error_not_a_traceback(monkeypatch) -> None:
    monkeypatch.setenv("WASTED_POLL_HZ", "fast")
    with pytest.raises(ConfigError, match="WASTED_POLL_HZ"):
        Settings.load()
    monkeypatch.setenv("WASTED_POLL_HZ", "3.0")
    monkeypatch.setenv("WASTED_HOURLY_CAP_USD", "lots")
    with pytest.raises(ConfigError, match="WASTED_HOURLY_CAP_USD"):
        Settings.load()


# --- mood clock ---------------------------------------------------------------


def test_mood_does_not_boot_straight_into_bored() -> None:
    """Regression: `_changed_at = 0.0` against a monotonic clock made the very
    first quiet tick look like an 8-minute silence, so the agent started bored."""
    clock = FakeClock()
    mood = MoodModel(rng=random.Random(1), clock=clock)
    assert mood.mood == "chill"
    for _ in range(50):
        clock.t += 0.33
        mood.observe("quiet")
    assert mood.mood == "chill", "mood decayed before any time actually passed"
    assert mood.held_for_s() < 30


def test_mood_still_decays_to_bored_after_a_real_quiet_stretch() -> None:
    clock = FakeClock()
    mood = MoodModel(rng=random.Random(1), clock=clock)
    clock.t += 481.0
    mood.observe("quiet")
    assert mood.mood == "bored"


def test_every_mood_has_a_cadence_window_inside_the_global_band() -> None:
    lo, hi = TACTICAL_TIMER_RANGE_S
    assert set(MOOD_TIMER_RANGE_S) == set(MOODS)
    for mood, (mlo, mhi) in MOOD_TIMER_RANGE_S.items():
        assert lo <= mlo < mhi <= hi, f"{mood} window {mlo}-{mhi} escapes {lo}-{hi}"


def test_mood_timer_draws_stay_inside_the_contract_band() -> None:
    rng = random.Random(5)
    cadence = TacticalCadence(rng)
    draws = {
        mood: [cadence._draw_timer(0, mood) for _ in range(400)] for mood in MOODS
    }
    lo, hi = TACTICAL_TIMER_RANGE_S
    for mood, values in draws.items():
        assert min(values) >= lo, f"{mood} fires faster than the contract floor"
        assert max(values) <= hi, f"{mood} fires slower than the contract band"
    # Behaviour has to be visibly different, not just nominally configurable.
    assert sum(draws["hyped"]) / 400 < sum(draws["bored"]) / 400


def test_mood_does_change_calls_per_hour_and_the_docs_say_so() -> None:
    """The claim this replaces was false.

    The old docstring and test said mood "cannot raise the per-hour call ceiling
    because both ends stay inside the 8-25 s band". Staying inside the band does
    not fix the rate: the expected interval is the window's midpoint, so
    narrowing it to 8-15 s (hyped) raises the expected rate by ~74% over bored's
    15-25 s. Assert the real numbers instead of a comfortable one.
    """
    rates = calls_per_hour_by_mood()
    assert set(rates) == set(MOODS)
    # 3600 / midpoint, to one decimal place.
    assert round(rates["hyped"], 1) == 313.0  # 8-15 s
    assert round(rates["scared"], 1) == 288.0  # 8-17 s
    assert round(rates["smug"], 1) == 240.0  # 10-20 s
    assert round(rates["chill"], 1) == 194.6  # 12-25 s
    assert round(rates["bored"], 1) == 180.0  # 15-25 s
    assert rates["hyped"] > rates["bored"] * 1.7, "mood really does move the bill"
    # …and every one of them is under the enforced ceiling.
    for mood, rate in rates.items():
        assert rate < MAX_TACTICAL_CALLS_PER_HOUR, mood


def test_the_call_ceiling_is_enforced_between_every_trigger_not_just_the_timer() -> None:
    """Regression: `danger` is true on every poll while wanted > 0.

    Without a floor between calls that fires a tactical decision per poll — at
    the 3 Hz default, 10,800 calls/h (~$18/h at the measured warm price), which
    no timer band constrains. The floor is what makes the ceiling real.
    """
    cadence = TacticalCadence(random.Random(5))
    danger = Delta(wanted_from=2, wanted_to=2, danger=True)
    now = 10_000.0
    fires = 0
    poll_interval = 1.0 / 3.0  # WASTED_POLL_HZ default
    for _ in range(int(3600 / poll_interval)):  # one simulated hour of polling
        if cadence.should_fire(now, danger, 0) is not None:
            fires += 1
            cadence.fired(now, 0, "hyped")
        now += poll_interval
    assert fires <= MAX_TACTICAL_CALLS_PER_HOUR, (
        f"{fires} tactical calls in a simulated hour of continuous danger; "
        f"ceiling is {MAX_TACTICAL_CALLS_PER_HOUR}"
    )
    assert fires >= MAX_TACTICAL_CALLS_PER_HOUR * 0.95, (
        "continuous triggers should reach the ceiling — otherwise this test "
        "would pass even if the tier stopped firing"
    )


def test_governor_l1_halves_the_ceiling() -> None:
    """§7 L1 is 'slower tactical timers'; the floor scales with it."""
    assert TacticalCadence.min_gap_s(0) == MIN_TACTICAL_GAP_S
    assert TacticalCadence.min_gap_s(1) == MIN_TACTICAL_GAP_S * L1_TIMER_SCALE
    cadence = TacticalCadence(random.Random(1))
    danger = Delta(wanted_from=1, wanted_to=1, danger=True)
    assert cadence.should_fire(10_000.0, danger, 1) == "danger"
    cadence.fired(10_000.0, 1, "hyped")
    # Inside the doubled floor: silent.
    assert cadence.should_fire(10_000.0 + MIN_TACTICAL_GAP_S + 0.1, danger, 1) is None
    # Past it: fires again.
    assert cadence.should_fire(
        10_000.0 + MIN_TACTICAL_GAP_S * L1_TIMER_SCALE + 0.1, danger, 1
    ) == "danger"


def test_the_stated_dollars_per_hour_matches_the_stated_calls_per_hour() -> None:
    """The numbers quoted in the docstrings are arithmetic, not vibes.

    MEASURED_WARM_TACTICAL_CALL_USD is the real measured warm cost of one
    tactical call (docs/STATUS.md 2026-08-25, live API: $0.011331 cold /
    $0.001714 warm). Everything below is that number times a rate this code
    enforces, so if either drifts the test says so.
    """
    ceiling_usd = MAX_TACTICAL_CALLS_PER_HOUR * MEASURED_WARM_TACTICAL_CALL_USD
    assert round(ceiling_usd, 2) == 0.77, ceiling_usd  # 450 calls/h
    l1_usd = (3600.0 / TacticalCadence.min_gap_s(1)) * MEASURED_WARM_TACTICAL_CALL_USD
    assert round(l1_usd, 2) == 0.39, l1_usd  # 225 calls/h
    rates = calls_per_hour_by_mood()
    assert round(rates["hyped"] * MEASURED_WARM_TACTICAL_CALL_USD, 2) == 0.54
    assert round(rates["bored"] * MEASURED_WARM_TACTICAL_CALL_USD, 2) == 0.31
    # Every one of those is inside the §7 target of < ~$1.50 per streamed hour,
    # with room left for the director tier (60-120 s => 30-60 Sonnet calls/h).
    assert ceiling_usd < 1.50
