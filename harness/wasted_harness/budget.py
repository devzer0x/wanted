"""Budget accounting and the L0-L3 governor (CONTRACTS §7).

Prices come ONLY from config/pricing.yaml (CLAUDE.md rule 6). Cost per call uses
the per-model `usage` fields — input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens — never estimates
(Sonnet 5 tokenizes ~30% more than Haiku for the same text).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .logsetup import get_logger
from .settings import ConfigError

log = get_logger("wasted.budget")

MTOK = 1_000_000


@dataclass(frozen=True)
class ModelPricing:
    id: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_5m_per_mtok: float
    min_cacheable_prefix_tokens: int


@dataclass(frozen=True)
class Pricing:
    source_url: str
    fetched: str
    tactical: ModelPricing
    director: ModelPricing

    @classmethod
    def load(cls, path: Path) -> Pricing:
        if not path.exists():
            raise ConfigError(
                f"pricing file not found at {path}. It is the runtime source of "
                f"truth for model IDs and prices; the harness will not run without it."
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            models = raw["models"]
            tiers = {}
            for tier in ("tactical", "director"):
                m = models[tier]
                tiers[tier] = ModelPricing(
                    id=str(m["id"]),
                    input_per_mtok=float(m["input_per_mtok"]),
                    output_per_mtok=float(m["output_per_mtok"]),
                    cache_read_per_mtok=float(m["cache_read_per_mtok"]),
                    cache_write_5m_per_mtok=float(m["cache_write_5m_per_mtok"]),
                    min_cacheable_prefix_tokens=int(m["min_cacheable_prefix_tokens"]),
                )
            return cls(
                source_url=str(raw["source_url"]),
                fetched=str(raw["fetched"]),
                tactical=tiers["tactical"],
                director=tiers["director"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"pricing file {path} is malformed: {exc!r}") from exc

    def for_model(self, model_id: str) -> ModelPricing:
        for m in (self.tactical, self.director):
            if m.id == model_id:
                return m
        raise ConfigError(
            f"model {model_id!r} is not in pricing.yaml "
            f"(known: {self.tactical.id}, {self.director.id}); refusing to guess a price."
        )


def cost_usd(
    pricing: ModelPricing,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Cost of one call. input_tokens are the *uncached* tokens (as the API reports)."""
    return (
        input_tokens * pricing.input_per_mtok
        + output_tokens * pricing.output_per_mtok
        + cache_read_tokens * pricing.cache_read_per_mtok
        + cache_creation_tokens * pricing.cache_write_5m_per_mtok
    ) / MTOK


def cost_of_usage(pricing: Pricing, model_id: str, usage: object) -> float:
    """Cost from an SDK usage object (attributes may be None on some responses)."""
    mp = pricing.for_model(model_id)
    return cost_usd(
        mp,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


# --- governor -----------------------------------------------------------------

# Fractions of the hourly cap where each level engages. L3 == cap hit.
LEVEL_THRESHOLDS: tuple[float, float, float] = (0.70, 0.90, 1.00)

LEVEL_NOTES = {
    0: "normal cadence",
    1: "slower tactical timers, no flavor shots",
    2: "director-only; reflex layer drives",
    3: "asleep in the car until the hour resets",
}


@dataclass
class BudgetGovernor:
    """Tracks spend in a rolling 1-hour window and maps it to governor levels.

    `on_change(old, new, reason)` fires on every level transition so main can
    emit the governor_level event + feed line (CONTRACTS §7: all levels are
    announced).
    """

    hourly_cap_usd: float
    on_change: Callable[[int, int, str], None] | None = None
    clock: Callable[[], float] = time.monotonic
    _spend: list[tuple[float, float]] = field(default_factory=list)  # (ts, usd)
    _level: int = 0
    _total_usd: float = 0.0
    _started_at: float | None = None

    def __post_init__(self) -> None:
        if self.hourly_cap_usd <= 0:
            raise ConfigError(
                f"hourly cap must be > 0, got {self.hourly_cap_usd}. "
                f"Set WASTED_HOURLY_CAP_USD."
            )
        self._started_at = self.clock()

    def record(self, usd: float) -> int:
        now = self.clock()
        self._spend.append((now, usd))
        self._total_usd += usd
        return self._recompute("hourly_cap")

    def _prune(self, now: float) -> None:
        cutoff = now - 3600.0
        self._spend = [(t, c) for (t, c) in self._spend if t >= cutoff]

    def hourly_spend_usd(self) -> float:
        self._prune(self.clock())
        return sum(c for _, c in self._spend)

    @property
    def level(self) -> int:
        return self._recompute("reset")

    def _recompute(self, raise_reason: str) -> int:
        spend = self.hourly_spend_usd()
        frac = spend / self.hourly_cap_usd
        new = 0
        for i, threshold in enumerate(LEVEL_THRESHOLDS):
            if frac >= threshold:
                new = i + 1
        if new != self._level:
            old, self._level = self._level, new
            reason = raise_reason if new > old else "reset"
            log.info(
                "governor level change",
                extra={"kv": {"from": old, "to": new, "hourly_usd": round(spend, 4)}},
            )
            if self.on_change is not None:
                self.on_change(old, new, reason)
        return self._level

    def force_level(self, level: int, reason: str = "manual") -> None:
        if not 0 <= level <= 3:
            raise ValueError(f"governor level must be 0-3, got {level}")
        if level != self._level:
            old, self._level = self._level, level
            if self.on_change is not None:
                self.on_change(old, level, reason)

    # -- reporting -------------------------------------------------------------

    def total_usd(self) -> float:
        return self._total_usd

    def cost_per_hour_usd(self) -> float:
        assert self._started_at is not None
        elapsed_h = max((self.clock() - self._started_at) / 3600.0, 1e-9)
        return self._total_usd / elapsed_h
