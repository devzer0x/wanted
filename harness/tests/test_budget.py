"""Budget math from the real config/pricing.yaml + governor level transitions."""

from pathlib import Path

import pytest

from wasted_harness.budget import (
    LEVEL_THRESHOLDS,
    BudgetGovernor,
    Pricing,
    cost_usd,
)
from wasted_harness.settings import ConfigError

PRICING_PATH = Path(__file__).resolve().parent.parent / "config" / "pricing.yaml"


@pytest.fixture(scope="module")
def pricing() -> Pricing:
    return Pricing.load(PRICING_PATH)


def test_pricing_matches_contract(pricing: Pricing) -> None:
    # CONTRACTS §3 / D4 exact values.
    t, d = pricing.tactical, pricing.director
    assert t.id == "claude-haiku-4-5-20251001"
    assert (t.input_per_mtok, t.output_per_mtok) == (1.00, 5.00)
    assert (t.cache_read_per_mtok, t.cache_write_5m_per_mtok) == (0.10, 1.25)
    assert t.min_cacheable_prefix_tokens == 4096
    assert d.id == "claude-sonnet-5"
    assert (d.input_per_mtok, d.output_per_mtok) == (2.00, 10.00)
    assert (d.cache_read_per_mtok, d.cache_write_5m_per_mtok) == (0.20, 2.50)
    assert d.min_cacheable_prefix_tokens == 1024
    assert pricing.source_url.startswith("https://platform.claude.com/")
    assert pricing.fetched == "2026-08-25"


def test_cost_math_tactical(pricing: Pricing) -> None:
    # A representative cached tactical call: 200 uncached in, 150 out,
    # 6000 cache-read, 0 cache-write.
    cost = cost_usd(pricing.tactical, 200, 150, cache_read_tokens=6000)
    expected = (200 * 1.00 + 150 * 5.00 + 6000 * 0.10) / 1_000_000
    assert cost == pytest.approx(expected)
    # First call of a session writes the cache instead.
    cost_first = cost_usd(pricing.tactical, 200, 150, cache_creation_tokens=6000)
    assert cost_first == pytest.approx((200 * 1.00 + 150 * 5.00 + 6000 * 1.25) / 1_000_000)
    assert cost_first > cost  # cache write costs 12.5x a read


def test_cost_math_director_image(pricing: Pricing) -> None:
    # Director with a 768-px screenshot (~448 visual tokens folded into input).
    cost = cost_usd(pricing.director, 1000 + 448, 200, cache_read_tokens=2000)
    expected = (1448 * 2.00 + 200 * 10.00 + 2000 * 0.20) / 1_000_000
    assert cost == pytest.approx(expected)


def test_unknown_model_refused(pricing: Pricing) -> None:
    with pytest.raises(ConfigError, match="refusing to guess"):
        pricing.for_model("claude-3-haiku-20240307")


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_governor_levels_and_events() -> None:
    clock = FakeClock()
    changes: list[tuple[int, int, str]] = []
    gov = BudgetGovernor(
        hourly_cap_usd=1.50,
        on_change=lambda o, n, r: changes.append((o, n, r)),
        clock=clock,
    )
    assert gov.level == 0
    gov.record(1.50 * LEVEL_THRESHOLDS[0] + 0.001)  # just over the L1 threshold
    assert gov.level == 1
    gov.record(1.50 * (LEVEL_THRESHOLDS[1] - LEVEL_THRESHOLDS[0]) + 0.001)
    assert gov.level == 2
    gov.record(1.50 * (LEVEL_THRESHOLDS[2] - LEVEL_THRESHOLDS[1]) + 0.01)
    assert gov.level == 3
    # The window rolls: an hour later the spend expires and L0 returns.
    clock.t += 3601.0
    assert gov.level == 0
    assert [(o, n) for o, n, _ in changes] == [(0, 1), (1, 2), (2, 3), (3, 0)]
    assert changes[-1][2] == "reset"


def test_governor_requires_positive_cap() -> None:
    with pytest.raises(ConfigError, match="hourly cap"):
        BudgetGovernor(hourly_cap_usd=0.0)


def test_cost_per_hour_is_the_window_the_governor_throttles_on() -> None:
    """The published rate and the governor must be the same measurement.

    It used to be `total spend / process uptime`: a lifetime mean that averaged
    in breaks, L3 sleep and quiet hours, so a run sitting AT the cap could
    publish a figure far below it — and, with its 1e-9 denominator clamp, the
    first heartbeat after the first decision reported an absurd rate (the
    $2.24/h on a five-minute session in the published data is that artefact).
    """
    clock = FakeClock()
    gov = BudgetGovernor(hourly_cap_usd=1.50, clock=clock)
    gov.record(0.25)
    clock.t += 1.0
    # Thirty seconds in, the honest answer is "$0.25 spent in the last hour",
    # never 0.25 * 120.
    assert gov.cost_per_hour_usd() == pytest.approx(0.25)
    assert gov.cost_per_hour_usd() == pytest.approx(gov.hourly_spend_usd())
    clock.t += 1800.0  # half an hour: still inside the window, still $0.25
    assert gov.cost_per_hour_usd() == pytest.approx(0.25)
    clock.t += 1801.0  # the call ages out of the window
    assert gov.cost_per_hour_usd() == pytest.approx(0.0)
    assert gov.total_usd() == pytest.approx(0.25)  # lifetime total is unaffected


# --- "today" has to mean today, and the window has to survive a restart -------


class FakeWallClock:
    """Unix seconds, so a test can cross UTC midnight on purpose."""

    def __init__(self, epoch: float) -> None:
        self.t = epoch

    def __call__(self) -> float:
        return self.t


# 2026-09-02T23:50:00Z and ten past midnight the next day.
_LATE_NIGHT = 1788306600.0
_AFTER_MIDNIGHT = _LATE_NIGHT + 1200.0


def test_cost_today_rolls_over_at_utc_midnight() -> None:
    """There was no rollover anywhere: a 40-hour run reported 40 hours of spend
    under a field named `today`, and a restart at 23:00 reset it mid-day."""
    clock, wall = FakeClock(), FakeWallClock(_LATE_NIGHT)
    gov = BudgetGovernor(hourly_cap_usd=1.50, clock=clock, wall_clock=wall)
    gov.record(0.40)
    assert gov.day_usd() == pytest.approx(0.40)

    clock.t += 1200.0
    wall.t = _AFTER_MIDNIGHT
    assert gov.day_usd() == pytest.approx(0.0), "a new UTC day starts at zero"
    gov.record(0.10)
    assert gov.day_usd() == pytest.approx(0.10)
    # The lifetime total is a different number and does NOT roll over.
    assert gov.total_usd() == pytest.approx(0.50)


def test_a_restart_does_not_hand_back_the_hourly_cap() -> None:
    """The governor throttles on a rolling hour. Rebuilt from zero on every
    watchdog restart, a restart loop could spend the cap again every few
    minutes and still publish L0."""
    clock, wall = FakeClock(), FakeWallClock(_LATE_NIGHT)
    first = BudgetGovernor(hourly_cap_usd=1.00, clock=clock, wall_clock=wall)
    for _ in range(10):
        first.record(0.10)  # exactly the cap
    assert first.level == 3
    snapshot = first.snapshot()
    assert snapshot["cost_day_usd"] == pytest.approx(1.00)
    assert len(snapshot["recent_spend"]) == 10

    # A minute later a fresh process starts with the same on-disk ledger.
    clock.t += 60.0
    wall.t += 60.0
    second = BudgetGovernor(hourly_cap_usd=1.00, clock=FakeClock(), wall_clock=wall)
    second.seed(snapshot)
    assert second.hourly_spend_usd() == pytest.approx(1.00)
    assert second.level == 3, "the hour's spend really happened; it still counts"
    assert second.day_usd() == pytest.approx(1.00)


def test_seeding_does_not_announce_a_level_change() -> None:
    """Adopting an already-throttled state is not a transition, and announcing
    one would put a governor_level event on the feed for something that did not
    happen in this process."""
    changes: list[tuple[int, int, str]] = []
    wall = FakeWallClock(_LATE_NIGHT)
    gov = BudgetGovernor(hourly_cap_usd=1.00, clock=FakeClock(), wall_clock=wall)
    gov.seed({"cost_day": "", "cost_day_usd": 0.0, "recent_spend": [(wall.t - 30.0, 1.00)]})
    gov.on_change = lambda o, n, r: changes.append((o, n, r))
    assert gov.level == 3
    assert changes == []


def test_spend_older_than_the_window_is_not_seeded() -> None:
    wall = FakeWallClock(_LATE_NIGHT)
    gov = BudgetGovernor(hourly_cap_usd=1.00, clock=FakeClock(), wall_clock=wall)
    gov.seed(
        {
            "cost_day": "",
            "cost_day_usd": 0.0,
            "recent_spend": [(wall.t - 7200.0, 5.00), (wall.t - 10.0, 0.20)],
        }
    )
    assert gov.hourly_spend_usd() == pytest.approx(0.20)
    assert gov.level == 0
