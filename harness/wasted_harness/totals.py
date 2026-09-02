"""Everything the public counters need that must survive a harness restart.

The site renders WASTED / BUSTED / MISSIONS / HOURS with no session qualifier
(web/src/components/live/Counters.tsx) and the character page promises "If he
dies, he dies. That's the counter on the front page." Those are LIFETIME
claims. The harness, however, runs under a watchdog and — by current operating
procedure — is restarted after every RDP disconnect, so a per-process counter
publishes 0 / 0 / 0 several times a day next to an unqualified label. An
unlabelled counter that silently resets is a false number on a public page, so
the counters are lifetime and this file is where they live.

Machine-local on purpose. `state/` is the harness's own durable store (the
offline queue, the day log and the commentary rotation already live there), and
it is the one place that is writable with no network and no credentials — the
counters must keep counting through a Supabase outage. Supabase is used only
ONCE, to seed this file the first time it is created (see
`SupabaseWriter.fetch_lifetime_seed`), so upgrading an existing rig does not
throw away the history it already published.

Everything here is an accumulated real observation. Nothing is estimated,
extrapolated or back-filled: if the file is missing and the seed read fails,
the totals start at zero and say so loudly rather than inventing a plausible
history.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .logsetup import get_logger

log = get_logger("wasted.totals")

TOTALS_FILENAME = "lifetime.json"

#: The counters that are lifetime sums. Deliberately the three the site puts on
#: the front page and on the OG card; anything else stays per-session.
COUNTER_KEYS: tuple[str, ...] = ("deaths", "busted", "missions_passed")

#: First harness version whose published `stats` rows carry LIFETIME counters
#: rather than per-session ones. It is the discriminator the one-time seed needs:
#: rows written before it must be SUMMED (each holds only its own session's
#: count), rows written at or after it must be MAXed (each already holds the
#: running total). Getting this backwards would double-count a death onto a
#: public page. Bump it only if the meaning of those columns changes again.
LIFETIME_COUNTERS_SINCE_VERSION = "0.2.0"

#: Longest gap between two consecutive live snapshots that still counts as
#: continuous play. At 2-4 Hz the real gap is 0.25-0.5 s; the break path waits
#: 5 s between ticks and the game is genuinely running through it, so the cap
#: sits just above that. Anything longer is the loop being blocked, the bridge
#: being down or the game being gone — time we cannot vouch for, so it is not
#: counted. This is what stops a game crash from reappearing as a jump in
#: `hours_alive` the moment the bridge answers again.
PLAY_GAP_MAX_S = 6.0

_REPLACE_RETRIES = 10
_REPLACE_DELAY_S = 0.15


def _utc_date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d")


def _version_tuple(version: object) -> tuple[int, ...] | None:
    """(major, minor, patch) or None when the string is not a release version.

    `None` means "treat as pre-lifetime": the only non-version harness_version
    in the published history is the `api-verify` smoke row, which is exactly a
    pre-lifetime row.
    """
    if not isinstance(version, str):
        return None
    parts = version.strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_lifetime_version(version: object) -> bool:
    parsed = _version_tuple(version)
    if parsed is None:
        return False
    return parsed >= _version_tuple(LIFETIME_COUNTERS_SINCE_VERSION)  # type: ignore[operator]


def summarise_lifetime_seed(
    stats_rows: list[dict[str, Any]], versions_by_session: dict[Any, Any]
) -> dict[str, float]:
    """Collapse previously published `stats` rows into one lifetime seed.

    `max(sum of the pre-lifetime rows, max of the lifetime rows)`, per counter.
    Sum is right for the old rows (each is one session's own tally) and max is
    right for the new ones (each already carries the running total); taking the
    larger of the two is correct in both directions and, crucially, can never
    double-count — a re-seed after a wiped state dir under-reports at worst,
    and under-reporting is a smaller lie than inventing deaths.

    `hours_alive` is seeded ONLY from lifetime rows. In the older rows that
    column held harness process uptime — a different quantity, which accrued
    through breaks, through governor-L3 sleep and across whole game crashes —
    so carrying it forward as played time would be publishing a number the game
    never produced.
    """
    legacy_sum = dict.fromkeys(COUNTER_KEYS, 0)
    lifetime_max = dict.fromkeys(COUNTER_KEYS, 0)
    lifetime_hours = 0.0
    for row in stats_rows:
        lifetime = is_lifetime_version(versions_by_session.get(row.get("session_id")))
        for key in COUNTER_KEYS:
            try:
                value = int(row.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if lifetime:
                lifetime_max[key] = max(lifetime_max[key], value)
            else:
                legacy_sum[key] += value
        if lifetime:
            with contextlib.suppress(TypeError, ValueError):
                lifetime_hours = max(lifetime_hours, float(row.get("hours_alive") or 0.0))
    seed: dict[str, float] = {
        key: float(max(legacy_sum[key], lifetime_max[key])) for key in COUNTER_KEYS
    }
    seed["played_seconds"] = lifetime_hours * 3600.0
    return seed


@dataclass
class LifetimeTotals:
    """Counters + played time + the budget ledger, persisted across restarts."""

    path: Path
    deaths: int = 0
    busted: int = 0
    missions_passed: int = 0
    #: Seconds during which the bridge answered with a snapshot we believed —
    #: NOT harness process uptime. See PLAY_GAP_MAX_S.
    played_seconds: float = 0.0
    #: UTC date the day ledger belongs to, so `cost_today_usd` means today.
    cost_day: str = ""
    cost_day_usd: float = 0.0
    #: (unix_epoch_seconds, usd) for the last hour only. Persisted so a restart
    #: cannot reset the window the hourly cap is enforced on — otherwise a
    #: restart loop spends the cap again from zero every few minutes.
    recent_spend: list[list[float]] = field(default_factory=list)
    #: False until the one-time read of previously published totals has
    #: succeeded. Kept false when the read fails so a later start retries it
    #: instead of freezing an under-reported history in place.
    seeded: bool = False
    #: True when this object came off disk. A missing file is a fresh rig (or a
    #: wiped state dir), which is what triggers the seed.
    existed: bool = False

    # -- persistence -----------------------------------------------------------

    @classmethod
    def load(cls, state_dir: Path) -> LifetimeTotals:
        path = state_dir / TOTALS_FILENAME
        totals = cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return totals
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt totals file must not stop the show, and must not be
            # silently treated as "no history" either — that is how a counter
            # resets without anyone noticing.
            log.error(
                "lifetime totals unreadable; counters restart from zero for this "
                "process and will re-seed from previously published rows",
                extra={"kv": {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}},
            )
            return totals
        if not isinstance(raw, dict):
            return totals
        totals.existed = True
        for key in COUNTER_KEYS:
            totals.__dict__[key] = max(0, int(raw.get(key, 0) or 0))
        totals.played_seconds = max(0.0, float(raw.get("played_seconds", 0.0) or 0.0))
        totals.cost_day = str(raw.get("cost_day", "") or "")
        totals.cost_day_usd = max(0.0, float(raw.get("cost_day_usd", 0.0) or 0.0))
        totals.seeded = bool(raw.get("seeded", False))
        recent = raw.get("recent_spend") or []
        if isinstance(recent, list):
            totals.recent_spend = [
                [float(t), float(usd)]
                for t, usd in (e for e in recent if isinstance(e, (list, tuple)) and len(e) == 2)
            ]
        return totals

    def to_dict(self) -> dict[str, Any]:
        return {
            "deaths": self.deaths,
            "busted": self.busted,
            "missions_passed": self.missions_passed,
            "played_seconds": round(self.played_seconds, 3),
            "cost_day": self.cost_day,
            "cost_day_usd": round(self.cost_day_usd, 6),
            "recent_spend": [[round(t, 3), round(usd, 8)] for t, usd in self.recent_spend],
            "seeded": self.seeded,
        }

    def save(self) -> None:
        """Atomically replace the file. Never raises: losing a save costs the
        counters one update, and raising here would cost the show a tick."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.to_dict(), f)
                for attempt in range(_REPLACE_RETRIES):
                    try:
                        os.replace(tmp, self.path)
                        return
                    except PermissionError:
                        # Windows refuses MoveFileEx while another process has
                        # the target open (same reason the queue rewrite retries).
                        if attempt == _REPLACE_RETRIES - 1:
                            raise
                        time.sleep(_REPLACE_DELAY_S)
            finally:
                if os.path.exists(tmp):
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
        except OSError as exc:
            log.warning(
                "lifetime totals not saved",
                extra={"kv": {"path": str(self.path), "error": f"{type(exc).__name__}: {exc}"}},
            )

    # -- counters --------------------------------------------------------------

    def counters(self) -> dict[str, int]:
        return {key: int(getattr(self, key)) for key in COUNTER_KEYS}

    def bump(self, key: str) -> int:
        if key not in COUNTER_KEYS:
            raise ValueError(f"{key!r} is not a lifetime counter (known: {COUNTER_KEYS})")
        value = int(getattr(self, key)) + 1
        setattr(self, key, value)
        self.save()
        return value

    def apply_seed(self, seed: dict[str, float]) -> None:
        """Adopt previously published totals, once, at first creation.

        `max` and never `+`: the seed is itself a lifetime figure, so adding it
        to a counter that already carries it would double-count a death onto a
        public page. Seeding can only ever raise a counter.
        """
        for key in COUNTER_KEYS:
            setattr(self, key, max(int(getattr(self, key)), int(seed.get(key, 0) or 0)))
        self.played_seconds = max(
            self.played_seconds, float(seed.get("played_seconds", 0.0) or 0.0)
        )
        self.seeded = True
        self.save()

    @property
    def played_hours(self) -> float:
        return self.played_seconds / 3600.0

    def add_play_seconds(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.played_seconds += min(seconds, PLAY_GAP_MAX_S)

    # -- budget ledger ---------------------------------------------------------

    def adopt_budget(self, snapshot: dict[str, Any]) -> None:
        """Take the governor's persistable state verbatim (called on heartbeat)."""
        self.cost_day = str(snapshot.get("cost_day", ""))
        self.cost_day_usd = float(snapshot.get("cost_day_usd", 0.0))
        self.recent_spend = [[float(t), float(usd)] for t, usd in snapshot.get("recent_spend", [])]

    def budget_seed(self, now_epoch: float | None = None) -> dict[str, Any]:
        """The governor's seed: today's ledger only if it really is still today."""
        now = time.time() if now_epoch is None else now_epoch
        today = _utc_date(now)
        return {
            "cost_day": today,
            "cost_day_usd": self.cost_day_usd if self.cost_day == today else 0.0,
            "recent_spend": [(t, usd) for t, usd in self.recent_spend if now - t < 3600.0],
        }
