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


def test_cost_per_hour_measured() -> None:
    clock = FakeClock()
    gov = BudgetGovernor(hourly_cap_usd=1.50, clock=clock)
    gov.record(0.25)
    clock.t += 1800.0  # half an hour
    assert gov.cost_per_hour_usd() == pytest.approx(0.50, rel=1e-3)
    assert gov.total_usd() == pytest.approx(0.25)
