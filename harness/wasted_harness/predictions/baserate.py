"""The odds a viewer is shown are the odds that were measured.

CONTRACTS-PREDICTIONS.md §3 (v2.4), "Cadence and entry windows":

    An always-available question must earn its place on every ask. Its YES-rate
    is re-measured by the harness from the agent's own recent telemetry (the
    same `events` rows settlement will read), over the same window shape, and
    it is offered only while that rate is inside [0.20, 0.80] on at least 12
    sampled windows. [...] When nothing is in band, nothing is asked; an empty
    card is honest and a foregone conclusion is not.

:class:`RollingBaseRate` is that re-measurement. It is a pure, deterministic,
clock-injected estimator in the same idiom as the rest of this package
(`PredictionGenerator`, `PredictionTicker`, `main._RecentEvents`): nothing here
reads `time.time()` directly, nothing here does I/O, and two instances fed the
same events and asked at the same clock value return the same pair.

**Why it exists at all, in one number.** "Will WANTED pull off what he is
doing in the next three minutes" is 50% (14/28) in the 2026-09-20 recording
and 7% (75/1043) in the 2026-09-04 one — the same question, the same window,
two real sessions of the same agent. A fixed base rate would have been a
coin-flip on one day and a giveaway on the other, and nothing in the
catalogue could have known which day it was on.

**What it measures, exactly.** For a template whose window is `[t+lock,
t+lock+resolve]`, it counts sampled windows of that exact shape, stepped by
`round_interval_s` backwards from the newest one that has already closed, as
far back as `history_s` or as far as it has actually observed — whichever is
shorter. A window "contains a matching event" by the same test settlement
will apply: the event type matches and (for `event_matches`) the payload
contains every key/value of `payload_match`, which is Postgres's
`payload @> payload_match`.

**Stepping BACKWARDS from now, not forwards from the first event**, on
purpose. Stepping forwards would make the whole grid shift every time the
oldest event aged out of history, so the same underlying telemetry would give
a different answer depending on when the estimator happened to have started.
Backwards from `now - (lock + resolve)`, the newest window is always the
newest one that has closed and every older sample sits on a fixed 300 s grid
behind it.

**The honest limit, stated rather than hidden.** Absence of a matching event
in a window counts as a NO — which is exactly what settlement does — but that
inference is only sound while the harness was actually recording. A stretch
where nothing ran reads as a run of NOs and drags the rate DOWN, which makes
the gate refuse to ask rather than ask unfairly. That is the safe direction,
and it is why the warm start in `main.py` loads real `events` rows rather than
assuming the gap away.
"""

from __future__ import annotations

import bisect
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .catalog import DEFAULT_ROUND_INTERVAL_S, PredictionTemplate

#: How far back a rolling measurement looks. Three hours at the default 300 s
#: step is 36 sampled windows at most — three times `AMBIENT_MIN_SAMPLES`, so a
#: rate can move meaningfully inside it without the sample size collapsing.
#: It is also the warm-start read size in `main.py`, which is bounded by it.
DEFAULT_HISTORY_S = 3 * 3600.0

#: The kinds this estimator can measure. Both settle from the presence of an
#: `events` row inside the window, which is the only thing it can replay from
#: recorded events; every other §3 kind reads `stats`, a status transition or a
#: cross-row comparison, and asking for one returns `(0, 0)` — "not measured" —
#: rather than a number nobody counted.
MEASURABLE_RULE_KINDS: frozenset[str] = frozenset({"event_occurs", "event_matches"})


def payload_contains(payload: Mapping[str, Any] | None, match: Mapping[str, Any]) -> bool:
    """Postgres `payload @> match`, for the flat scalar case this layer writes.

    `_validate_rule` in `catalog.py` refuses a `payload_match` that is not a
    non-empty object of JSON scalars, so containment here is exactly "every
    key is present with an equal value". A key the payload does not carry is
    simply not contained — which is the right answer for `{"category":
    "trouble"}` against a `go_start_a_job` row that has no `category` at all
    (552 of the 2,668 real `activity_end` rows in the 2026-09-04 recording).
    """
    if not payload:
        return False
    for key, value in match.items():
        if key not in payload:
            return False
        if payload[key] != value:
            return False
    return True


def rule_matcher(rule: Mapping[str, Any]) -> Callable[[str, Mapping[str, Any] | None], bool] | None:
    """A predicate over `(type, payload)` for a §3 rule, or `None` if unmeasurable."""
    kind = rule.get("kind")
    if kind not in MEASURABLE_RULE_KINDS:
        return None
    params = rule.get("params") or {}
    event_type = params.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        return None
    if kind == "event_occurs":
        return lambda type_, _payload: type_ == event_type
    match = params.get("payload_match")
    if not isinstance(match, dict) or not match:
        return None
    frozen = dict(match)
    return lambda type_, payload: type_ == event_type and payload_contains(payload, frozen)


@dataclass(frozen=True)
class Calibration:
    """What was measured, in the shape that goes on the row.

    CONTRACTS-PREDICTIONS §3's point, restated: "the odds shown to a viewer are
    the odds that were measured". This ends up verbatim in
    `predictions.state_context["calibration"]`, so the card and the archive
    both carry the evidence for why the question was considered fair.
    """

    yes: int
    n: int
    window_s: float
    history_s: float

    @property
    def rate(self) -> float | None:
        return self.yes / self.n if self.n else None

    def as_json(self) -> dict[str, Any]:
        return {
            "yes": self.yes,
            "n": self.n,
            "window_s": self.window_s,
            "history_s": self.history_s,
        }


@dataclass
class RollingBaseRate:
    """Every event the harness records in, `(yes, n)` per template out.

    Fed from `main.Harness._note_recent_event` — the single choke point every
    §4 event this process emits already passes through (`_ObservedWriter`
    overrides both `record_event` and `insert_event_now` and calls it from
    each). Nothing else feeds it, so there is no second notion of "what the
    agent has been doing".
    """

    clock: Callable[[], float] = time.time
    history_s: float = DEFAULT_HISTORY_S
    round_interval_s: float = DEFAULT_ROUND_INTERVAL_S

    #: (ts, type, payload), kept sorted by ts. Small by construction: the
    #: busiest real hour in either recording is ~100 events, so three hours is
    #: a few hundred tuples.
    _events: list[tuple[float, str, Mapping[str, Any]]] = field(
        default_factory=list, init=False, repr=False
    )
    #: The earliest moment this estimator can speak for. Windows older than it
    #: are not sampled at all, because "no matching event" and "nobody was
    #: watching" are not the same claim.
    _origin: float | None = field(default=None, init=False, repr=False)
    #: Sorted match timestamps per rule, rebuilt only when `_events` changes.
    #: `measure` runs on the game-loop thread at up to 4 Hz for as long as the
    #: round gate stays open with nothing in band, so the O(events) filter is
    #: the one part of it worth not repeating. The cache key covers everything
    #: that can change the answer, so the result is identical either way.
    _hits: dict[tuple[Any, ...], list[float]] = field(
        default_factory=dict, init=False, repr=False
    )
    #: `observe` is called from whichever thread records the event — the clip
    #: worker records its own, which is why `main._RecentEvents` beside this is
    #: locked too — while `measure` runs on the loop thread. `observe` prunes,
    #: i.e. slices the front off the list `measure` is walking; unserialised
    #: that skips rows, and the understated count is then cached in `_hits` and
    #: written onto a real row as its calibration. Re-entrant because `measure`
    #: calls `_matching` with it held.
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False
    )

    # -- input ------------------------------------------------------------------

    def observe(self, ts: float, type_: str, payload: Mapping[str, Any] | None) -> None:
        """Record one §4 event. Never raises: it sits on the game-loop thread."""
        entry = (float(ts), str(type_), dict(payload or {}))
        with self._lock:
            self._observe_locked(entry)

    def _observe_locked(self, entry: tuple[float, str, Mapping[str, Any]]) -> None:
        if self._events and entry[0] < self._events[-1][0]:
            # Out of order (a queued row replayed, or a warm start interleaving
            # with live events). Rare enough to pay for a bisect insert rather
            # than re-sorting, and correctness here is cheap.
            bisect.insort(self._events, entry, key=lambda e: e[0])
        else:
            self._events.append(entry)
        self._origin = entry[0] if self._origin is None else min(self._origin, entry[0])
        self._hits.clear()
        self._prune()

    def observe_many(self, rows: Sequence[tuple[float, str, Mapping[str, Any] | None]]) -> int:
        """Bulk load (the warm start). Returns how many rows were taken."""
        taken = 0
        for ts, type_, payload in rows:
            self.observe(ts, type_, payload)
            taken += 1
        return taken

    # -- output -----------------------------------------------------------------

    def measure(self, template: PredictionTemplate) -> Calibration:
        """`(yes, n)` for this template's exact window shape. Pure: no mutation.

        `n` is the number of sampled windows that both closed before `now` and
        sit inside the observed history; `yes` is how many of them contain a
        matching event. An unmeasurable rule kind, or no history at all,
        returns `n = 0` — which every caller must read as "do not ask", never
        as "0%".
        """
        with self._lock:
            return self._measure_locked(template)

    def _measure_locked(self, template: PredictionTemplate) -> Calibration:
        window_s = template.window.resolve_delay_s
        rule = template.telemetry_rule
        if self._origin is None or rule_matcher(rule) is None:
            return Calibration(yes=0, n=0, window_s=window_s, history_s=self.history_s)

        now = self.clock()
        step = self.round_interval_s
        if step <= 0:
            return Calibration(yes=0, n=0, window_s=window_s, history_s=self.history_s)

        lock = template.window.lock_delay_s
        span = lock + window_s
        floor = max(self._origin, now - self.history_s)

        hits = self._matching(rule)
        yes = 0
        n = 0
        anchor = now - span
        while anchor >= floor:
            opens = anchor + lock
            closes = opens + window_s
            i = bisect.bisect_left(hits, opens)
            if i < len(hits) and hits[i] <= closes:
                yes += 1
            n += 1
            anchor -= step
        return Calibration(yes=yes, n=n, window_s=window_s, history_s=self.history_s)

    # -- introspection ----------------------------------------------------------

    @property
    def observed_count(self) -> int:
        return len(self._events)

    @property
    def observed_span_s(self) -> float:
        """How much history this estimator can actually speak for, in seconds."""
        if self._origin is None:
            return 0.0
        return max(0.0, self.clock() - max(self._origin, self.clock() - self.history_s))

    # -- bookkeeping ------------------------------------------------------------

    def _matching(self, rule: Mapping[str, Any]) -> list[float]:
        """Sorted timestamps of every observed event this rule matches."""
        params = rule.get("params") or {}
        match = params.get("payload_match") or {}
        key = (
            rule.get("kind"),
            params.get("event_type"),
            tuple(sorted(match.items())),
        )
        cached = self._hits.get(key)
        if cached is not None:
            return cached
        matcher = rule_matcher(rule)
        hits = (
            [] if matcher is None else [ts for ts, t, p in self._events if matcher(t, p)]
        )
        self._hits[key] = hits
        return hits

    def _prune(self) -> None:
        cutoff = self.clock() - self.history_s
        if not self._events or self._events[0][0] >= cutoff:
            return
        drop = bisect.bisect_left(self._events, cutoff, key=lambda e: e[0])
        del self._events[:drop]
        self._hits.clear()
        # The origin follows the surviving rows: once the oldest observation is
        # gone, this estimator can no longer speak for the windows before it.
        self._origin = self._events[0][0] if self._events else None
