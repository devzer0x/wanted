"""The prediction template catalogue: real telemetry, turned into questions.

Every template here is a candidate WANTED prediction (docs/CONTRACTS-PREDICTIONS.md,
frozen v2.0). A template is only ever built from:

- fields the bridge actually emits in `GET /state` (docs/CONTRACTS.md §1, parsed
  by `wasted_harness.bridge_client.GameState`), and
- event types the harness actually emits (`wasted_harness.events.EMITTED_EVENT_TYPES`
  — the §4 enum minus `UNPRODUCED_EVENT_REASONS`), and
- a `telemetry_rule.kind` from the CLOSED settlement registry,
  docs/CONTRACTS-PREDICTIONS.md §3. `TELEMETRY_RULE_KINDS` below is that table,
  copied verbatim; :class:`PredictionTemplate` refuses to construct with any
  other kind, so a typo can never reach a row (CLAUDE.md rule 1: an unresolvable
  or unrecognised rule voids at settlement, wasting the audience's time — the
  fix here is to make it structurally impossible to ship one).

**On "the brief".** The task that produced this package quotes brief sections
(§9 windows, §10 example questions, §12 scoring, §13 `is_event`/WANTED EVENT)
that CONTRACTS-PREDICTIONS.md itself refers to as a companion document. That
document is not present anywhere in this repository (checked: `docs/`,
`docs/research/`, git history of CONTRACTS-PREDICTIONS.md — the file itself is
untracked/new). This module was built from the concrete list the task text
quoted directly plus CONTRACTS-PREDICTIONS.md §2/§3, which ARE both in the
repo and frozen.

## WANTED EVENTS (`is_event`)

`is_event` — the §13 "WANTED EVENT" highlight flag — IS implemented now. The
paragraph that used to stand here (saying it was not, because nothing defined
what qualifies) is superseded by the operator's own words, quoted verbatim in
the task that added it:

    "Allow occasional highlighted predictions: WANTED EVENT. Examples: CAN
     WANTED SURVIVE 5 STARS FOR 5 MINUTES? / CAN WANTED STEAL A POLICE CAR AND
     ESCAPE? / CAN WANTED LAND THE HELICOPTER? These may have boosted TTWO
     reward pools. Keep them special. Do not make every prediction feel like a
     major event."

What that turns into here: a template may set `is_event=True`, and an event
template may only be CONSTRUCTED with a :class:`MeasuredRate` — a base rate
recomputed from the real recording, over at least `EVENT_MIN_SAMPLE` real
anchors, landing inside [`EVENT_MIN_BASE_RATE`, `EVENT_MAX_BASE_RATE`].
`PredictionTemplate.__post_init__` refuses to build an event template that
misses any of those, so an unmeasured "major event" cannot reach a row. A
headline nobody can win, or nobody can lose, is worse than no headline.

How OFTEN an event may be offered is not decided here: that is
`generator.GeneratorConfig` (`event_cooldown_s`, `events_enabled`, plus a
structural never-back-to-back rule), so the rarity is named configuration
rather than a number buried in the picker.

All three of the operator's own examples were measured against the real
recording, and all three are in `REJECTED_TEMPLATES` with the number that
killed them: the five-star one is unwinnable (the agent has never exceeded two
stars in 86.93 h of recording), and the other two have no telemetry in the §3
registry that can settle them. What ships instead is the one event this
recording can both trigger and settle — `two_star_standoff`, the operator's
own "survive the heat for N minutes" question re-scoped from five stars to the
most heat the agent has ever actually pulled.

**Reward columns are deliberately absent from every row this package builds.**
`reward_pool`, `reward_asset`, `entry_count`, `correct_count` are, per
CONTRACTS-PREDICTIONS.md §2, "maintained by trigger/settlement, never by a
client" — this generator is a client. Inventing a number for `reward_pool`
would be exactly the fake-data CLAUDE.md rule 1 forbids; the DB/infra layer
owns that default.

**Where the WANTED EVENT reward boost belongs, and why it is not here.** The
operator's brief says events "may have boosted TTWO reward pools". This
package still writes no `reward_pool`, and deliberately: choosing a treasury
number is not a generation-time decision, and there is no point inside
settlement where a multiplier could be applied either — `settle_due_predictions`
only ever READS `p.reward_pool` (`infra/supabase/migrations/
20260908120000_predictions.sql`, the `v_pool_base := floor(p.reward_pool * 1e18)`
line) and then clamps it against `REWARDS_MAX_PER_PREDICTION`. So the boost
belongs wherever `reward_pool` is FUNDED, and `is_event` — which this package
does now write on every row — is the flag that layer keys off:
`case when is_event then base_pool * boost else base_pool end`.

Two facts for whoever owns that layer, read out of the migration rather than
assumed: `predictions.reward_pool` (`not null check (reward_pool > 0)`) and
`predictions.reward_asset` (`not null`) have NO column default and no trigger
that fills them, so an insert that omits both — which is every row this
package builds — is rejected by Postgres before any of this matters. A default
or a `before insert` trigger has to exist first, and that trigger is exactly
the right home for the event multiplier.

## Calibration against a real recording (2026-09-08)

`tests/fixtures/real_session_2026-09-04.json` is a real, provenance-stamped
export of `public.events` for one real session (5,649 rows, `session_id`
`2c135a5f-f040-4c2f-970f-f52bc034e9cb`, span 2026-09-04 00:59 UTC to
2026-09-07 15:55 UTC). It was pulled into the repo by another workstream mid-
task and is used here exactly as CLAUDE.md rule 1 requires: as a *recording*,
never hand-edited. The measured numbers below were recomputed independently
from that file (not copied from anyone else's table) — see the coordinator
message in this task's history for the number they measured; where the two
disagree it is called out explicitly rather than silently resolved either way.

What this fixture actually contains, and what it does NOT: it is
`events`-only (no `stats`/`decisions`/`/state` rows), and the session ran with
missions switched off (operator decision, 2026-09-03), so it can calibrate
`wanted_change`/`death`/`busted` driven templates but **cannot** calibrate
`mission_outcome` (no mission events at all), `vehicle_entered`/
`vehicle_exited` (no HUD/`stats.hud.vehicle` history in an events-only
export), or `survives_a_fight` (no attack/threat telemetry — `threat.
attacker_handle` is a `/state` field, not an event). Each such template below
says so at its own definition rather than pretending otherwise.

**Measured, and what it changed:**

1. Sampling every 30s across the ENTIRE recording (`t0` to `t1`, both real
   event timestamps — no gap in raw event density longer than 10 minutes was
   found anywhere in the file, so nothing was excluded as "idle"), an
   AMBIENT, untriggered "will he gain a star in the next 2 min" fires only
   ~1.1% of the time (112/10,428 anchors), "clears to 0 in 2 min" ~0.9%
   (93/10,428), "dies or is busted in 3 min" ~0.9% (95/10,426), and
   "survives 3 min" (ambient) is a giveaway at ~99.1% (10,331/10,426).
   These confirm the general shape of what the coordinator reported for the
   same ambient questions (single-digit-percent / high-90s), though the
   coordinator's own figures (3.7% / 2.7% / 2.1% / 97.9%) run 2-4x higher
   than what this recount finds — almost certainly because their stated
   denominator ("~6.5 h of true playing time") is far smaller than the
   ~87 h continuous span this file's own timestamps cover end-to-end, and
   this export carries no `stats.hours_alive`/heartbeat data to reconcile
   the two. **This discrepancy is reported, not resolved**, per this task's
   own instruction; either way the CONCLUSION is unchanged and is the load-
   bearing one: ambient versions of these questions are one-sided giveaways
   and must not ship as-is.
2. `wanted_reaches_3`: **zero** of the 63 real `wanted_change` events in the
   whole recording ever reached `to >= 3` (max `to` observed: 2). Confirmed,
   not merely repeated from the coordinator. **Dropped** — see
   `REJECTED_TEMPLATES`.
3. `wanted_gain`: re-tried CONDITIONED on the start of an already-aggressive
   roam activity (`pick_a_fight`, `shoot_a_cop`, `armed_rampage_block`,
   `steal_cop_car`, `chase_that_car`, `burn_the_city`, `earn_two_stars`,
   `hijack_bus`, `drive_by_run`, `shoot_and_run`, `jack_a_driver` — 199 real
   starts) rather than left ambient, to see if a real trigger could rescue
   it. It could not: 3.0-5.0% YES across 60-180s windows, still nowhere near
   a fair question. **Dropped** — see `REJECTED_TEMPLATES`.
4. The three surviving wanted-star templates below are now TRIGGERED off a
   real `wanted_change` gain event appearing in `recent_events` (not offered
   ambiently), and their windows are set to the exact points measured on the
   real 39 gain events in this recording:
   - `loses_the_cops`, 90s after the gain: **53.8% YES** (21/39) — the
     coordinator's own "single best question found", reproduced exactly here
     (independently recomputed, not copied).
   - `survives_a_chase`, 150s after the gain (no `death`/`busted` in that
     span): **48.7% YES** (19/39) — even closer to 50/50, a new number this
     recount found (not in the coordinator's table).
   - `death_in_window`, 240s after the gain (a real `death` event, this
     template's rule, not `busted`): **43.6% YES** (17/39) — also newly
     measured here; the coordinator's table only gave the combined
     death-or-busted figure.
5. **WANTED EVENT calibration (2026-09-09), added with `is_event`.** Same
   file, same rule: recomputed here, nothing copied. This pass measured with
   the REAL settlement semantics rather than the anchor-to-anchor shortcut
   points 1-4 used — settlement reads `[locks_at, resolves_at]`, a window that
   opens `lock_delay_s` AFTER the row is created, and the row is created some
   unknown-but-bounded time after the anchoring event (the caller owns the
   `recent_events` recency window). The band is not a guess: the caller is
   `main.py`, and `PREDICTION_RECENT_WINDOW_S = 60.0` there is exactly how long
   a `wanted_change` stays visible to a trigger — so the row can be created
   anywhere from 0 to 60 s after the anchoring event, and every candidate was
   swept across that whole band:
   - `two_star_standoff` (SHIPPED). Anchors: the **17** real `wanted_change`
     events reaching `to >= 2` — two stars being the most heat this agent has
     ever pulled (`MEASURED_PEAK_WANTED_LEVEL`). With `lock_delay_s=15` /
     `resolve_delay_s=240`, no `death` or `busted` row lands in the settled
     window for **5 of those 17** (29.4%; Wilson 95% CI 13.3-53.1%) at zero
     generation delay, 7/17 (41.2%) at 10-45 s, and 8/17 (47.1%) at 60 s.
     Fair at BOTH ends of the band, which is the property that actually
     matters when the caller owns the delay.
   - "reach two stars from one" (`wanted_reaches` level 2, anchored on the 22
     real gains to exactly one star) was measured and NOT shipped, even though
     its headline number is the prettiest in this file. One fixed window
     (`lock_delay_s=5`, `resolve_delay_s=55`), swept across the same band:
     45.5% at 0 s, **50.0%** at 2 s, 40.9% at 10 s, 36.4% at 30 s, **13.6%**
     at 60 s. Escalations happen fast — of the 15 anchors that reached two
     stars at all within 90 s, the median gap is 36 s — so a late tick simply
     misses the event that would have settled it YES. A question whose
     fairness depends on how promptly the loop happened to tick is not a fair
     question, however good its best number looks.
   - all three of the operator's own event examples are in
     `REJECTED_TEMPLATES`, each with the number measured here: **0/63**
     `wanted_change` events ever exceeded two stars, so "5 stars for 5
     minutes" can never even be asked; `steal_cop_car` started **2** times in
     86.93 h and completed **0**; `go_flying` ended **92** times and completed
     **0** (`up_only`: 1/98), and no §3 rule kind can observe a landing at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from wasted_harness.bridge_client import GameState

#: One buffered §4 event, the same shape `SupabaseWriter.record_event` builds
#: and the same shape a caller already holds after `writer.record_event(...)`:
#: `{"type": "<CONTRACTS §4 type>", "payload": {...}, "ts": "<iso8601>", ...}`.
#: **Contract with callers:** `recent_events` passed to a template's `trigger`
#: is expected to already be a bounded recency window (e.g. the last minute) —
#: templates check for PRESENCE of an event type, never a timestamp, so the
#: caller (the generator) owns deciding what "recent" means.
RecentEvent = dict[str, Any]

#: docs/CONTRACTS-PREDICTIONS.md §3 — the CLOSED settlement registry. Copied
#: verbatim (kind names only; the params column is documentation, not
#: enforced here — settlement owns validating its own params). A rule whose
#: kind is not in this set voids at settlement and "credits nobody" per §3's
#: "Voiding is mandatory" rule, so nothing in this package may ever construct
#: one.
TELEMETRY_RULE_KINDS: frozenset[str] = frozenset(
    {
        "event_occurs",
        "wanted_reaches",
        "wanted_clears",
        "wanted_gained",
        "survives_window",
        "vehicle_entered",
        "vehicle_exited",
        "mission_outcome",
        "activity_outcome",
    }
)

#: `predictions.outcomes` (CONTRACTS-PREDICTIONS §2): every template here is a
#: plain yes/no question, matching the shape the contract's own example gives.
YES_NO_OUTCOMES: tuple[dict[str, str], ...] = (
    {"key": "yes", "label": "YES"},
    {"key": "no", "label": "NO"},
)

#: §9's stated range. A window whose total (lock delay + resolve delay) falls
#: outside this is refused at construction — see :class:`Window`.
MIN_WINDOW_S = 30.0
MAX_WINDOW_S = 300.0

#: The highest `wanted_change.to` in 86.93 h of real recording
#: (`real_session_2026-09-04.json`: 63 `wanted_change` events, max `to` = 2).
#: Two stars is this agent's observed ceiling, which is why it — and not the
#: brief's five — is what the shipped WANTED EVENT is built on. Recomputed by
#: `tests/test_predictions_catalog.py`, so it fails loudly if a later
#: recording ever beats it.
MEASURED_PEAK_WANTED_LEVEL = 2

#: --- WANTED EVENT admission rules -------------------------------------------
#: An event template gets a highlight on the site and (per the operator brief)
#: may carry a boosted reward pool, so the bar to ship one is higher than for
#: an ordinary template, not lower. All three are enforced in
#: `PredictionTemplate.__post_init__`, not left to review.
#:
#: `EVENT_MIN_SAMPLE` is a floor on real anchors, not a claim of statistical
#: power: 17 anchors still leave a Wilson 95% interval ~40 points wide. Its job
#: is to make the un-measurable ideas fail closed — the rejected
#: `steal_a_police_car_and_escape` had n=2 and the five-star one n=0.
EVENT_MIN_SAMPLE = 15
#: A measured rate outside this band is a giveaway in one direction or the
#: other. Deliberately wider than an ordinary template's near-50/50 target: an
#: event is allowed to be a long shot (that is part of why its pool is
#: boosted), it is just not allowed to be a foregone conclusion.
EVENT_MIN_BASE_RATE = 0.10
EVENT_MAX_BASE_RATE = 0.90

#: Templates considered and DELIBERATELY NOT SHIPPED, each with the real,
#: measured reason (see the module docstring's "Calibration" section for the
#: numbers). Same house pattern as `wasted_harness.events.UNPRODUCED_EVENT_REASONS`
#: — say so here rather than silently leaving no trace of the attempt, so the
#: next person does not re-propose the same unwinnable/giveaway question.
REJECTED_TEMPLATES: dict[str, str] = {
    "wanted_reaches_3": (
        "measured 0/63 real wanted_change events ever reached to>=3 across the "
        "full real_session_2026-09-04.json recording (max observed: 2 stars). "
        "An unwinnable question that always settles NO is not a prediction, "
        "it is a giveaway in the other direction — refusing to ship it."
    ),
    "wanted_gain": (
        "measured 0.7-1.1% YES sampled ambiently every 30s across the whole "
        "recording, and 3.0-5.0% YES even when conditioned on the start of an "
        "already-aggressive roam activity (pick_a_fight, chase_that_car, "
        "burn_the_city, ...; 199 real starts) at 60-300s windows. No field "
        "this package can read (no 'police proximity' or 'aiming a gun' "
        "signal in /state) gets it into a fair range; shipping it ambient "
        "would be exactly the giveaway CLAUDE.md rule 1's spirit forbids."
    ),
    # --- the operator's own three WANTED EVENT examples, all measured -------
    "survive_five_stars": (
        "the brief's own example, 'CAN WANTED SURVIVE 5 STARS FOR 5 MINUTES?'. "
        "Measured: 0 of the 63 real wanted_change events in 86.93 h of "
        "real_session_2026-09-04.json ever exceeded to=2 (histogram: 24x to=0, "
        "22x to=1, 17x to=2). Five stars has never happened, so the question "
        "can never even be TRIGGERED, let alone won — and asking it anyway "
        "would settle NO forever. What ships instead is two_star_standoff: the "
        "same 'survive the heat' question, re-scoped to the heat that exists."
    ),
    "steal_a_police_car_and_escape": (
        "the brief's own example, 'CAN WANTED STEAL A POLICE CAR AND ESCAPE?'. "
        "Two separate blockers, both measured. (1) Sample: the steal_cop_car "
        "roam activity started 2 times in 86.93 h and completed 0 of them "
        "(outcomes: 1 preempted, 1 actions_done_goal_unmet) — n=2 is far below "
        "EVENT_MIN_SAMPLE, so there is no base rate to have. (2) Shape: 'steal "
        "AND escape' is two clauses, and every §3 rule kind is single-clause "
        "(activity_outcome sees one activity_end row; wanted_clears sees one "
        "wanted_change row). Settlement cannot AND them, so the honest version "
        "of this question is not the question the operator asked for."
    ),
    "land_the_helicopter": (
        "the brief's own example, 'CAN WANTED LAND THE HELICOPTER?'. No §3 "
        "rule kind can observe a landing at all: there is no touchdown, "
        "altitude or aircraft event in the CONTRACTS.md §4 enum, and altitude "
        "lives only in /state, which settlement never reads. The nearest "
        "measurable proxies are both unwinnable anyway — the go_flying "
        "activity ended 92 times in the recording and completed 0, up_only "
        "completed 1 of 98. Unresolvable AND unwinnable; not shipped."
    ),
}


@dataclass(frozen=True)
class MeasuredRate:
    """A base rate RECOUNTED from a real recording. Never an estimate.

    `yes`/`n` are counts of real anchors in `fixture`; `anchor` and
    `settled_window_s` state exactly how they were produced, so
    `tests/test_predictions_catalog.py` can re-derive the identical pair from
    the same file and fail if the code, the window or the comment drifts apart
    from the data (the same trick `test_calibration_numbers_match_the_real_
    fixture` already plays for the ordinary templates).

    `generation_delay_band_s` records that the number was checked across the
    whole plausible gap between the anchoring event and the row being created,
    not at one convenient point: settlement reads `[locks_at, resolves_at]`, so
    a template whose fairness collapses when the loop ticks a few seconds later
    is not fair at all (see the module docstring, calibration point 5).
    """

    yes: int
    n: int
    fixture: str
    anchor: str
    settled_window_s: float
    generation_delay_band_s: tuple[float, float]

    def __post_init__(self) -> None:
        if self.n <= 0:
            raise ValueError("a measured rate needs at least one real anchor")
        if not (0 <= self.yes <= self.n):
            raise ValueError(f"yes must be within 0..n; got yes={self.yes}, n={self.n}")
        lo, hi = self.generation_delay_band_s
        if lo < 0 or hi < lo:
            raise ValueError(f"generation_delay_band_s must be 0 <= lo <= hi; got {(lo, hi)}")

    @property
    def rate(self) -> float:
        return self.yes / self.n


@dataclass(frozen=True)
class Window:
    """A prediction's timing: how long to accept entries, how long to wait to settle.

    `opened_at < locks_at < resolves_at` (CONTRACTS-PREDICTIONS §2's CHECK) is
    guaranteed by construction: both delays must be positive, so `locks_at` is
    strictly after `opened_at` and `resolves_at` strictly after `locks_at`.
    """

    lock_delay_s: float
    resolve_delay_s: float

    def __post_init__(self) -> None:
        if self.lock_delay_s <= 0 or self.resolve_delay_s <= 0:
            raise ValueError(
                f"both delays must be > 0 (opened_at < locks_at < resolves_at); "
                f"got lock_delay_s={self.lock_delay_s}, resolve_delay_s={self.resolve_delay_s}"
            )
        if not (MIN_WINDOW_S <= self.total_s <= MAX_WINDOW_S):
            raise ValueError(
                f"window total (lock_delay_s + resolve_delay_s) must be "
                f"{MIN_WINDOW_S}-{MAX_WINDOW_S}s (brief §9); got {self.total_s}s"
            )

    @property
    def total_s(self) -> float:
        return self.lock_delay_s + self.resolve_delay_s


@dataclass(frozen=True)
class PredictionTemplate:
    """One prediction, ready to be scored and (if picked) turned into a row.

    `trigger` is the hard, cheap precondition — "is this even a sane question
    right now" (e.g. you cannot ask "will he lose the cops" with no cops on
    him, and — per the calibration above — three of these templates require a
    REAL, RECENT `wanted_change` gain event, not merely "wanted > 0", because
    an ambient version of the same question measures as a near-certain
    giveaway). `score` is the continuous "how good a moment is this" rank,
    folding in a real-field-derived proxy for uncertainty (near-50/50
    preferred over 99/1) and watchability, seeded from the measured rates
    above where they exist. Both take the live `GameState`; `trigger` also
    takes the caller's recency-windowed event list.
    """

    prediction_type: str
    question: str
    outcomes: tuple[dict[str, str], ...]
    #: JSON-serialisable, CONTRACTS-PREDICTIONS §3 shape: `{"kind": ..., ...params}`.
    telemetry_rule: dict[str, Any]
    window: Window
    trigger: Callable[[GameState, Sequence[RecentEvent]], bool]
    score: Callable[[GameState], float]
    #: Static, per-template estimate in [0, 1] of "settles cleanly rather than
    #: voiding" — see each template's comment for the reasoning. Used by the
    #: generator's ranking, never by settlement (settlement is real telemetry
    #: only; this is a generation-time preference, not a promise).
    reliability: float
    #: `predictions.is_event` — a highlighted WANTED EVENT. The site already
    #: renders the flag (`web/src/components/predict/LivePredictionCard.tsx`
    #: labels the card "Wanted event"), and it is the flag the reward-funding
    #: layer keys a boosted pool off; see the module docstring. Setting it
    #: REQUIRES `measured` below.
    is_event: bool = False
    #: The real, recounted base rate behind this template, where one exists.
    #: Optional for an ordinary template (several in this catalog have no
    #: calibrating telemetry at all and say so at their own definition);
    #: MANDATORY for an event.
    measured: MeasuredRate | None = None

    def __post_init__(self) -> None:
        kind = self.telemetry_rule.get("kind")
        if kind not in TELEMETRY_RULE_KINDS:
            raise ValueError(
                f"{self.prediction_type!r}: telemetry_rule.kind {kind!r} is not in the "
                f"CONTRACTS-PREDICTIONS.md §3 registry ({sorted(TELEMETRY_RULE_KINDS)}). "
                f"An unrecognised kind voids at settlement — refusing to build it."
            )
        if not (0.0 <= self.reliability <= 1.0):
            raise ValueError(f"{self.prediction_type!r}: reliability must be in [0,1]")
        if not self.is_event:
            return
        # A WANTED EVENT is highlighted and may carry a boosted pool. It ships
        # only on a rate somebody actually counted off a real recording.
        if self.measured is None:
            raise ValueError(
                f"{self.prediction_type!r}: an event template (is_event=True) must ship a "
                f"MeasuredRate recounted from a real recording. An unmeasured 'major event' "
                f"is a guess with a highlight on it — refusing to build it."
            )
        if self.measured.n < EVENT_MIN_SAMPLE:
            raise ValueError(
                f"{self.prediction_type!r}: measured on {self.measured.n} real anchors, below "
                f"EVENT_MIN_SAMPLE={EVENT_MIN_SAMPLE}. Not enough real moments to call this a "
                f"base rate — refusing to build it."
            )
        if not (EVENT_MIN_BASE_RATE <= self.measured.rate <= EVENT_MAX_BASE_RATE):
            raise ValueError(
                f"{self.prediction_type!r}: measured base rate {self.measured.rate:.3f} is "
                f"outside [{EVENT_MIN_BASE_RATE}, {EVENT_MAX_BASE_RATE}] — a foregone "
                f"conclusion, not an event. Refusing to build it."
            )


# --- real-state helpers (every one reads a field docs/CONTRACTS.md §1 names) --


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _uncertainty(p: float) -> float:
    """1.0 at p=0.5 (maximally uncertain), 0.0 at p=0 or p=1."""
    return 1.0 - abs(_clamp(p) - 0.5) * 2.0


def _health_fraction(state: GameState) -> float:
    if state.player.max_health <= 0:
        return 1.0
    return _clamp(state.player.health / state.player.max_health)


def _under_attack(state: GameState) -> bool:
    return state.threat.attacker_handle is not None


def _hostile_nearby(state: GameState) -> bool:
    return any(p.relationship == "hostile" for p in state.nearby.peds)


def _recent_type(recent_events: Sequence[RecentEvent], type_: str) -> bool:
    return any(e.get("type") == type_ for e in recent_events)


def _recent_wanted_gain(recent_events: Sequence[RecentEvent]) -> bool:
    """A real `wanted_change` event with `to > from` in the caller's recency
    window. This is the real trigger the calibration above found necessary:
    the ambient version of every wanted-star question measured as a
    near-certain giveaway; anchored on the moment the game itself just raised
    the heat, the same three questions measure close to 50/50."""
    for e in recent_events:
        if e.get("type") != "wanted_change":
            continue
        payload = e.get("payload") or {}
        try:
            if payload.get("to", 0) > payload.get("from", 0):
                return True
        except TypeError:
            continue
    return False


def _recent_wanted_gain_to(recent_events: Sequence[RecentEvent], level: int) -> bool:
    """A real `wanted_change` event in the caller's recency window that both
    RAISED the level and landed at `level` or above. Stricter than
    `_recent_wanted_gain`: the WANTED EVENT below needs the moment the heat hit
    its ceiling, not merely the moment it went up."""
    for e in recent_events:
        if e.get("type") != "wanted_change":
            continue
        payload = e.get("payload") or {}
        try:
            to = payload.get("to", 0)
            if to >= level and to > payload.get("from", 0):
                return True
        except TypeError:
            continue
    return False


# --- 1. death soon after a real wanted-star gain ---------------------------------
# kind: event_occurs{event_type:"death"} — an `events` row of type `death` in
# the window. `death` IS emitted (wasted_harness.events.EMITTED_EVENT_TYPES).
# CALIBRATED: 240s after a real gain, 17/39 real gains (43.6%) were followed
# by a real `death` event and none of the earlier windows tried (90-210s)
# cleared 40% — see module docstring §"Calibration", point 4.

_DEATH_WINDOW = Window(lock_delay_s=15.0, resolve_delay_s=225.0)  # 240s total


def _trigger_death_in_window(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    return (
        not state.player.dead
        and not state.player.arrested
        and state.player.wanted > 0
        and _recent_wanted_gain(recent_events)
        # No grace period right after a just-recorded death (avoids re-asking
        # the instant he respawns, which is also usually a fresh 0-star state
        # anyway).
        and not _recent_type(recent_events, "death")
    )


def _score_death_in_window(state: GameState) -> float:
    # Seeded from the measured 43.6% base rate (n=39), nudged by real-time
    # danger signals the calibration data could not see (it has no /state).
    p = _clamp(
        0.436
        + 0.20 * (1.0 - _health_fraction(state))
        + 0.10 * (1.0 if _under_attack(state) else 0.0)
        + 0.05 * (1.0 if _hostile_nearby(state) else 0.0)
    )
    # Weighted toward watchability rather than 50/50 with uncertainty: the
    # measured base rate (43.6%) already sits close to 0.5, so pushing p up
    # with real danger signals trades a little uncertainty for a lot of
    # "this is the moment to watch" — a near-death instant is more must-see
    # than it is more predictable, and the blend should say so.
    return 0.35 * _uncertainty(p) + 0.65 * p





TPL_DEATH_IN_WINDOW = PredictionTemplate(
    prediction_type="death_in_window",
    question="NOW THAT THE COPS ARE ON HIM: WILL WANTED DIE IN THE NEXT 4 MINUTES?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule={
        "kind": "event_occurs",
        "event_type": "death",
        "outcome_if_true": "yes",
        "outcome_if_false": "no",
    },
    window=_DEATH_WINDOW,
    trigger=_trigger_death_in_window,
    score=_score_death_in_window,
    reliability=0.85,
)


# --- 2. loses the cops, right after a real wanted-star gain -----------------------
# kind: wanted_clears — `wanted_change` payload `to = 0`.
# CALIBRATED: 90s after a real gain, 21/39 real gains (53.8%) cleared to 0 —
# the coordinator's own "single best question found", reproduced independently
# here. See module docstring §"Calibration", point 4.

_LOSES_COPS_WINDOW = Window(lock_delay_s=10.0, resolve_delay_s=80.0)  # 90s total


def _trigger_loses_the_cops(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    return (
        not state.player.dead
        and not state.player.arrested
        and state.player.wanted > 0
        and _recent_wanted_gain(recent_events)
    )


def _score_loses_the_cops(state: GameState) -> float:
    # Seeded from the measured 53.8% base rate (n=39) — already very close to
    # 50/50 on its own; nudged only slightly by real-time signals.
    p = _clamp(
        0.538
        + 0.10 * (1.0 if state.player.in_vehicle else -0.10)
        - 0.05 * (state.player.wanted / 5.0)
    )
    watchability = 0.5 + 0.5 * (state.player.wanted / 5.0)
    return 0.5 * _uncertainty(p) + 0.5 * _clamp(watchability)


TPL_LOSES_THE_COPS = PredictionTemplate(
    prediction_type="loses_the_cops",
    question="THE COPS JUST CAME ON: WILL WANTED LOSE THEM?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule={"kind": "wanted_clears"},
    window=_LOSES_COPS_WINDOW,
    trigger=_trigger_loses_the_cops,
    score=_score_loses_the_cops,
    reliability=0.85,
)


# --- 3. survives the chase, right after a real wanted-star gain -------------------
# kind: survives_window — absence of `death`/`busted` in the window. `busted`
# IS emitted (EMITTED_EVENT_TYPES), same as `death`.
# CALIBRATED: an AMBIENT "survives 3 min" is a 99.1% giveaway (recomputed
# here; the coordinator measured 97.9% on their own denominator — see the
# module docstring for why the two differ). Anchored on a real gain instead,
# 150s out, 19/39 (48.7%) survived with no death/busted in between — the
# single closest-to-50/50 number this recount found. See "Calibration" pt 4.

_SURVIVES_CHASE_WINDOW = Window(lock_delay_s=10.0, resolve_delay_s=140.0)  # 150s total


def _trigger_survives_a_chase(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    return (
        not state.player.dead
        and not state.player.arrested
        and state.player.wanted > 0
        and _recent_wanted_gain(recent_events)
    )


def _score_survives_a_chase(state: GameState) -> float:
    # Seeded from the measured 48.7% base rate (n=39) — the best-calibrated
    # number in this catalog; real-time health/heat only nudge it.
    p = _clamp(
        0.487
        + 0.15 * (_health_fraction(state) - 0.5)
        - 0.05 * (state.player.wanted / 5.0)
    )
    watchability = 0.5 + 0.5 * (state.player.wanted / 5.0)
    return 0.5 * _uncertainty(p) + 0.5 * _clamp(watchability)


TPL_SURVIVES_A_CHASE = PredictionTemplate(
    prediction_type="survives_a_chase",
    question="THE COPS JUST CAME ON: WILL WANTED SURVIVE THIS CHASE?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule={"kind": "survives_window"},
    window=_SURVIVES_CHASE_WINDOW,
    trigger=_trigger_survives_a_chase,
    score=_score_survives_a_chase,
    reliability=0.9,
)


# --- 4. enters a vehicle -------------------------------------------------------------
# kind: vehicle_entered — HUD `vehicle` transition null -> non-null (no params
# per the §3 registry). Source: `player.in_vehicle` / `vehicle` in `/state`,
# the same field `Perceptor.observe` already diffs for `entered_vehicle`.
# NOT CALIBRATED against real_session_2026-09-04.json: that fixture is
# `events`-only (no `stats.hud`/`/state` history), and no §4 event type
# records a vehicle enter, so no real base rate could be recomputed for this
# template. Shipped on trigger-shape grounds only (a candidate vehicle must be
# in view); flagged here rather than implied to be measured.

_ENTERS_VEHICLE_WINDOW = Window(lock_delay_s=10.0, resolve_delay_s=50.0)  # 1 min total


def _trigger_enters_vehicle(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    return not state.player.dead and not state.player.in_vehicle


def _score_enters_vehicle(state: GameState) -> float:
    # A candidate vehicle nearby raises the odds toward "obviously yes"; none
    # nearby raises them toward "obviously no". Best uncertainty is somewhere
    # with exactly a couple of options in view. Uncalibrated — see note above.
    n = len(state.nearby.vehicles)
    p = _clamp(0.15 + 0.20 * min(n, 4))
    watchability = 0.5  # mundane but fast to resolve; not scene-dependent
    return 0.6 * _uncertainty(p) + 0.4 * watchability


TPL_ENTERS_VEHICLE = PredictionTemplate(
    prediction_type="enters_vehicle",
    question="WILL WANTED GET IN A VEHICLE?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule={"kind": "vehicle_entered"},
    window=_ENTERS_VEHICLE_WINDOW,
    trigger=_trigger_enters_vehicle,
    score=_score_enters_vehicle,
    reliability=0.75,
)


# --- 5. exits a vehicle ---------------------------------------------------------------
# kind: vehicle_exited — HUD `vehicle` transition non-null -> null.
# NOT CALIBRATED against real_session_2026-09-04.json — same reason as
# `enters_vehicle` above (no vehicle-transition telemetry in an events-only export).

_EXITS_VEHICLE_WINDOW = Window(lock_delay_s=10.0, resolve_delay_s=50.0)  # 1 min total


def _trigger_exits_vehicle(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    return not state.player.dead and state.player.in_vehicle


def _score_exits_vehicle(state: GameState) -> float:
    # A stopped car is much likelier to be got out of soon than one at speed.
    # Uncalibrated — see note above.
    speed = state.vehicle.speed if state.vehicle is not None else 0.0
    p = _clamp(0.6 - 0.03 * speed)
    watchability = 0.5
    return 0.6 * _uncertainty(p) + 0.4 * watchability


TPL_EXITS_VEHICLE = PredictionTemplate(
    prediction_type="exits_vehicle",
    question="WILL WANTED GET OUT OF THE VEHICLE?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule={"kind": "vehicle_exited"},
    window=_EXITS_VEHICLE_WINDOW,
    trigger=_trigger_exits_vehicle,
    score=_score_exits_vehicle,
    reliability=0.75,
)


# --- 6. survives a fight -----------------------------------------------------------------
# kind: survives_window, same rule as the chase question — a different
# prediction_type/question/trigger, the identical (parameterless) rule.
# NOT CALIBRATED against real_session_2026-09-04.json: nothing in an
# `events`-only export marks "under attack" (that is `/state.threat.
# attacker_handle`, never a §4 event), so no fight-specific window could be
# measured. `enters`/`exits_vehicle`'s caveat applies here identically.

_SURVIVES_FIGHT_WINDOW = Window(lock_delay_s=10.0, resolve_delay_s=50.0)  # 1 min total


def _trigger_survives_a_fight(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    return (
        not state.player.dead
        and not state.player.arrested
        and (_under_attack(state) or _hostile_nearby(state))
    )


def _score_survives_a_fight(state: GameState) -> float:
    # Uncalibrated — see note above.
    p = _clamp(0.4 + 0.5 * _health_fraction(state) - (0.15 if state.player.wanted > 0 else 0.0))
    return 0.5 * _uncertainty(p) + 0.5 * 0.7  # a fight is inherently watchable


TPL_SURVIVES_A_FIGHT = PredictionTemplate(
    prediction_type="survives_a_fight",
    question="WILL WANTED SURVIVE THIS FIGHT?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule={"kind": "survives_window"},
    window=_SURVIVES_FIGHT_WINDOW,
    trigger=_trigger_survives_a_fight,
    score=_score_survives_a_fight,
    reliability=0.9,
)


# --- 7. mission complete / fail -----------------------------------------------------------
# kind: mission_outcome{expect:"passed"} — `mission_end`(passed) / `mission_fail`.
# Both event types are emitted (EMITTED_EVENT_TYPES), but NOT in this fixture:
# real_session_2026-09-04.json's `_provenance.absent_types_and_why` confirms
# missions were operator-disabled for that entire session (docs/STATUS.md
# 2026-09-03) — zero mission_start/end/fail rows exist to calibrate against,
# confirmed by direct inspection, not merely asserted. Separately: real
# missions routinely run well past 5 minutes, and every window here is capped
# at 5 min (brief §9 / MAX_WINDOW_S) — that cap is honoured, not stretched, so
# this template will often not resolve within its window and settle `void`
# per CONTRACTS-PREDICTIONS §3's mandatory-void rule. `reliability` below is
# set low on purpose to reflect that; the generator will rank it behind
# comparably-uncertain, comparably-watchable options that resolve cleanly.

_MISSION_OUTCOME_WINDOW = Window(lock_delay_s=20.0, resolve_delay_s=280.0)  # 5 min total (capped)


def _trigger_mission_outcome(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    return state.mission.active and not state.mission.cutscene_active


def _score_mission_outcome(state: GameState) -> float:
    # No real per-mission pass/fail odds signal exists in /state, and this
    # session's fixture has zero mission events to calibrate from (missions
    # were off); treat as maximally uncertain (0.5) whenever it is even
    # offerable, and let `reliability` (not this function) express the
    # low-confidence-of-clean-resolution caveat above.
    return 0.5 * 1.0 + 0.5 * 0.6  # uncertainty=1.0 at p=0.5; missions are watchable


TPL_MISSION_OUTCOME = PredictionTemplate(
    prediction_type="mission_outcome",
    question="WILL WANTED COMPLETE THIS MISSION?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule={"kind": "mission_outcome", "expect": "passed"},
    window=_MISSION_OUTCOME_WINDOW,
    trigger=_trigger_mission_outcome,
    score=_score_mission_outcome,
    reliability=0.5,
)


# --- 8. WANTED EVENT: two stars — his ceiling — held for four minutes ------------
# kind: survives_window — absence of `death`/`busted` in [locks_at, resolves_at].
# The same parameterless rule `survives_a_chase` uses; a different trigger, a
# different window, and the only `is_event=True` template in this catalog.
#
# This is the operator's "CAN WANTED SURVIVE 5 STARS FOR 5 MINUTES?" with the
# star count corrected to reality: five stars has never happened (0/63), two is
# the ceiling (MEASURED_PEAK_WANTED_LEVEL), so two stars is where the question
# is asked. See `REJECTED_TEMPLATES["survive_five_stars"]` for the original.
#
# CALIBRATED, with the real settlement window rather than an anchor-to-anchor
# approximation: of the 17 real `wanted_change` events reaching `to >= 2`, the
# 240s settled window that opens 15s after the row is created contains no
# `death`/`busted` row for 5 (29.4%) at zero generation delay and 8 (47.1%) at
# a 60s delay — fair across the whole band. Module docstring, calibration pt 5.

_TWO_STAR_STANDOFF_WINDOW = Window(lock_delay_s=15.0, resolve_delay_s=240.0)  # 255s total

TWO_STAR_STANDOFF_RATE = MeasuredRate(
    yes=5,
    n=17,
    fixture="tests/fixtures/real_session_2026-09-04.json",
    anchor="wanted_change with to >= 2 and to > from",
    settled_window_s=240.0,
    generation_delay_band_s=(0.0, 60.0),
)


def _trigger_two_star_standoff(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    return (
        not state.player.dead
        and not state.player.arrested
        and state.player.wanted >= MEASURED_PEAK_WANTED_LEVEL
        and _recent_wanted_gain_to(recent_events, MEASURED_PEAK_WANTED_LEVEL)
    )


def _score_two_star_standoff(state: GameState) -> float:
    # Seeded from the measured 29.4% (n=17) at the pessimistic end of the
    # delay band, nudged by the real-time signals the recording cannot see.
    p = _clamp(
        TWO_STAR_STANDOFF_RATE.rate
        + 0.25 * (_health_fraction(state) - 0.5)
        + (0.05 if state.player.in_vehicle else -0.05)
    )
    # Watchability pinned near the top, and weighted above uncertainty: this is
    # the hardest the game has ever come at the agent in 86.93 h of recording. A long
    # shot at the ceiling is worth showing precisely because it is a long shot —
    # which is also the argument for the boosted pool the brief asks for.
    return 0.4 * _uncertainty(p) + 0.6 * 0.95


TPL_TWO_STAR_STANDOFF = PredictionTemplate(
    prediction_type="two_star_standoff",
    question=(
        "WANTED EVENT — TWO STARS, THE HIGHEST HEAT EVER RECORDED: "
        "CAN WANTED STAY ALIVE AND OUT OF CUFFS FOR FOUR MINUTES?"
    ),
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule={"kind": "survives_window"},
    window=_TWO_STAR_STANDOFF_WINDOW,
    trigger=_trigger_two_star_standoff,
    score=_score_two_star_standoff,
    # A notch under `survives_a_chase`'s 0.9: same always-resolves rule, but a
    # 255s window is 1.7x the exposure to a session_end/bridge_down overlap,
    # which §3 makes a mandatory void. (Zero of the 17 real anchors actually
    # had one, so this is caution, not an observed failure rate.)
    reliability=0.85,
    is_event=True,
    measured=TWO_STAR_STANDOFF_RATE,
)


def _build_catalog() -> tuple[PredictionTemplate, ...]:
    catalog = (
        TPL_DEATH_IN_WINDOW,
        TPL_LOSES_THE_COPS,
        TPL_SURVIVES_A_CHASE,
        TPL_ENTERS_VEHICLE,
        TPL_EXITS_VEHICLE,
        TPL_SURVIVES_A_FIGHT,
        TPL_MISSION_OUTCOME,
        TPL_TWO_STAR_STANDOFF,
    )
    seen: set[str] = set()
    for tpl in catalog:
        if tpl.prediction_type in seen:
            raise ValueError(f"duplicate prediction_type in catalog: {tpl.prediction_type!r}")
        seen.add(tpl.prediction_type)
        if tpl.prediction_type in REJECTED_TEMPLATES:
            raise ValueError(
                f"{tpl.prediction_type!r} is in REJECTED_TEMPLATES and must not also be shipped"
            )
    return catalog


#: The full, real catalogue. Built (and validated) once at import time.
CATALOG: tuple[PredictionTemplate, ...] = _build_catalog()
