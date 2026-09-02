"""The public counters must not reset when the harness does.

WASTED / BUSTED / MISSIONS / HOURS are rendered with no session qualifier
(web/src/components/live/Counters.tsx) and repeated on the social share card,
and /agent tells the public "If he dies, he dies. That's the counter on the
front page." They were per-process: the watchdog restarts the harness after
every RDP disconnect, and the site — which takes the newest heartbeat across all
sessions — followed the new row straight down to 0 / 0 / 0 / 0.0 with nothing
saying it had reset.

These tests pin the three things that has to mean:
* a restart continues the count;
* the one-time seed from previously published rows can never double-count;
* `hours_alive` counts time the game was observed up, not process uptime.
"""

from __future__ import annotations

from pathlib import Path

from wasted_harness.totals import (
    PLAY_GAP_MAX_S,
    LifetimeTotals,
    is_lifetime_version,
    summarise_lifetime_seed,
)


def test_counters_survive_a_restart(tmp_path: Path) -> None:
    totals = LifetimeTotals.load(tmp_path)
    assert totals.counters() == {"deaths": 0, "busted": 0, "missions_passed": 0}
    assert totals.bump("deaths") == 1
    assert totals.bump("deaths") == 2
    assert totals.bump("busted") == 1

    reloaded = LifetimeTotals.load(tmp_path)  # the watchdog restarts the process
    assert reloaded.counters() == {"deaths": 2, "busted": 1, "missions_passed": 0}
    assert reloaded.bump("deaths") == 3, "the count must continue, not restart"


def test_a_missing_file_is_zero_and_not_seeded(tmp_path: Path) -> None:
    """A fresh rig starts at zero AND asks to be seeded; it never invents a
    plausible-looking history."""
    totals = LifetimeTotals.load(tmp_path)
    assert totals.counters() == {"deaths": 0, "busted": 0, "missions_passed": 0}
    assert totals.seeded is False
    assert totals.existed is False


def test_a_corrupt_file_does_not_stop_the_show(tmp_path: Path) -> None:
    (tmp_path / "lifetime.json").write_text("{not json", encoding="utf-8")
    totals = LifetimeTotals.load(tmp_path)
    assert totals.counters() == {"deaths": 0, "busted": 0, "missions_passed": 0}
    assert totals.seeded is False, "an unreadable file must be re-seeded, not accepted"


# --- the one-time seed --------------------------------------------------------
#
# The published history is a mix: rows written before 0.2.0 hold ONE session's
# tally, rows written at or after it hold the running total. Summing the second
# kind, or maxing the first, both put a wrong number on the front page.


def _legacy_rows() -> list[dict[str, object]]:
    """Shaped exactly like the real published rows (deaths 0,0,1,2 / busted 0)."""
    return [
        {"session_id": "a", "deaths": 0, "busted": 0, "missions_passed": 0, "hours_alive": 0.05},
        {"session_id": "b", "deaths": 1, "busted": 0, "missions_passed": 0, "hours_alive": 0.4},
        {"session_id": "c", "deaths": 2, "busted": 0, "missions_passed": 0, "hours_alive": 8.5},
        {"session_id": "d", "deaths": 1, "busted": 0, "missions_passed": 0, "hours_alive": 0.1},
    ]


def test_pre_lifetime_rows_are_summed() -> None:
    versions = {"a": "0.1.0", "b": "0.1.0", "c": "0.1.0", "d": "api-verify"}
    seed = summarise_lifetime_seed(_legacy_rows(), versions)
    assert seed["deaths"] == 4, "each old row is one session's own tally"
    assert seed["busted"] == 0
    assert seed["missions_passed"] == 0


def test_pre_lifetime_hours_are_not_carried_forward() -> None:
    """`hours_alive` used to be process uptime — it accrued through breaks,
    through L3 sleep and across a whole game crash. That is a different
    quantity from played time and must not be published as it."""
    versions = dict.fromkeys("abcd", "0.1.0")
    assert summarise_lifetime_seed(_legacy_rows(), versions)["played_seconds"] == 0.0


def test_lifetime_rows_are_maxed_never_added() -> None:
    """The double-count trap: a lifetime row already contains the old sum."""
    rows = [
        *_legacy_rows(),
        {"session_id": "e", "deaths": 4, "busted": 0, "missions_passed": 1, "hours_alive": 1.0},
        {"session_id": "f", "deaths": 6, "busted": 2, "missions_passed": 3, "hours_alive": 2.5},
    ]
    versions = {"a": "0.1.0", "b": "0.1.0", "c": "0.1.0", "d": "0.1.0", "e": "0.2.0", "f": "0.2.1"}
    seed = summarise_lifetime_seed(rows, versions)
    assert seed["deaths"] == 6, "4 legacy + 6 lifetime is 10 deaths that never happened"
    assert seed["busted"] == 2
    assert seed["missions_passed"] == 3
    assert seed["played_seconds"] == 2.5 * 3600.0


def test_a_re_seed_after_a_wiped_state_dir_under_reports_rather_than_over(tmp_path: Path) -> None:
    """If the lifetime row is somehow LOWER than the legacy sum (the seed read
    failed once and a session published from zero), the larger of the two wins
    — under-reporting is recoverable; inventing deaths is not."""
    rows = [
        *_legacy_rows(),
        {"session_id": "e", "deaths": 1, "busted": 0, "missions_passed": 0, "hours_alive": 0.2},
    ]
    versions = {"a": "0.1.0", "b": "0.1.0", "c": "0.1.0", "d": "0.1.0", "e": "0.2.0"}
    assert summarise_lifetime_seed(rows, versions)["deaths"] == 4


def test_seeding_only_ever_raises_a_counter(tmp_path: Path) -> None:
    totals = LifetimeTotals.load(tmp_path)
    totals.deaths = 7  # this process already saw more than the seed knows about
    totals.apply_seed({"deaths": 4, "busted": 1, "missions_passed": 0, "played_seconds": 60.0})
    assert totals.deaths == 7
    assert totals.busted == 1
    assert totals.seeded is True
    assert LifetimeTotals.load(tmp_path).seeded is True, "seeding is a one-time event"


def test_version_boundary() -> None:
    assert is_lifetime_version("0.2.0") is True
    assert is_lifetime_version("0.2.1") is True
    assert is_lifetime_version("1.0.0") is True
    assert is_lifetime_version("0.1.0") is False
    assert is_lifetime_version("api-verify") is False
    assert is_lifetime_version(None) is False


# --- played time --------------------------------------------------------------


def test_play_time_never_counts_a_gap_it_did_not_watch(tmp_path: Path) -> None:
    """A game crash used to reappear as a jump in `hours_alive` the moment the
    bridge answered again, because the number was `now - process start`."""
    totals = LifetimeTotals.load(tmp_path)
    totals.add_play_seconds(0.33)  # a normal 3 Hz tick
    totals.add_play_seconds(4.0)  # a break tick: the game is up, he is alive
    totals.add_play_seconds(1800.0)  # a half-hour crash: capped, not counted
    assert totals.played_seconds == 0.33 + 4.0 + PLAY_GAP_MAX_S
    totals.add_play_seconds(-5.0)  # a clock going backwards adds nothing
    assert totals.played_seconds == 0.33 + 4.0 + PLAY_GAP_MAX_S


def test_played_hours_round_trips_through_disk(tmp_path: Path) -> None:
    totals = LifetimeTotals.load(tmp_path)
    totals.add_play_seconds(3.0)
    totals.save()
    assert LifetimeTotals.load(tmp_path).played_hours == 3.0 / 3600.0


# --- the budget ledger --------------------------------------------------------


def test_the_day_ledger_is_dropped_when_the_day_has_changed(tmp_path: Path) -> None:
    """`cost_today_usd` must mean today even across a restart at 00:05."""
    totals = LifetimeTotals.load(tmp_path)
    totals.cost_day = "2026-09-01"
    totals.cost_day_usd = 1.25
    totals.recent_spend = [[1_000.0, 0.5]]
    seed = totals.budget_seed(now_epoch=1_760_000_000.0)  # a much later day
    assert seed["cost_day_usd"] == 0.0
    assert seed["recent_spend"] == [], "an hour-old window does not survive a day"


def test_the_day_ledger_is_kept_within_the_same_day(tmp_path: Path) -> None:
    import time as _time

    now = _time.time()
    totals = LifetimeTotals.load(tmp_path)
    totals.adopt_budget(
        {
            "cost_day": __import__("datetime").datetime.fromtimestamp(
                now, __import__("datetime").UTC
            ).strftime("%Y-%m-%d"),
            "cost_day_usd": 0.9,
            "recent_spend": [(now - 60.0, 0.4), (now - 7200.0, 0.5)],
        }
    )
    seed = totals.budget_seed(now_epoch=now)
    assert seed["cost_day_usd"] == 0.9
    assert [usd for _t, usd in seed["recent_spend"]] == [0.4], "only the last hour"
