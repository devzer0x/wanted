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

## 2026-09-21: the entry-window floor, and what it moved (contract v2.4 §3)

`MIN_ENTRY_WINDOW_S` (30 s) now floors `lock_delay_s` in `Window` itself, and
every shipped template's lock was raised to meet it. **Settlement reads only
`[locks_at, resolves_at]`, so moving the lock moves the measured window**, and
every calibrated rate below was recounted from the recording against its NEW
window — at zero generation delay for the headline figure, and swept across
the whole 0-60 s band (`PREDICTION_RECENT_WINDOW_S` in `main.py`) for the
range. `tests/test_predictions_catalog.py` re-derives all of it.

| template | lock | settled span | old rate | **new rate** | band 0-60 s |
|---|---|---|---|---|---|
| `death_in_window`   | 15->30 | 225 s | 43.6% | **35.9%** (14/39) | 25.6-35.9% |
| `loses_the_cops`    | 10->30 |  80 s | 53.8% | **53.8%** (21/39) | 35.9-53.8% |
| `survives_a_chase`  | 10->30 | 140 s | 48.7% | **56.4%** (22/39) | 56.4-74.4% |
| `two_star_standoff` | 15->30 | 240 s | 29.4% | **41.2%** (7/17)  | 41.2-47.1% |
| `survives_a_fight`  | 10->30 |  50 s | — | — (no threat telemetry in an events-only export) |
| `mission_outcome`   | 20->30 | 280->270 s | — | — (no mission events in either recording) |

All four measured rates stay inside [`EVENT_MIN_BASE_RATE`,
`EVENT_MAX_BASE_RATE`] across the whole delay band, so nothing had to be
withdrawn. Two notes on the "old" column, because the comparison is not
like-for-like: those three figures were measured ANCHOR-TO-ANCHOR (`(gain,
gain+total]`), which is not what settlement reads; the new ones are the real
settled window. `loses_the_cops` landing on 53.8% again is a coincidence of
this recording, not a property of the change. `mission_outcome` was already at
the 300 s ceiling, so its ten seconds came off the settled span rather than
being added to the total.

## 2026-09-21: the rule SHAPE was wrong, and nothing could have settled

Found while re-measuring, fixed here, and worth stating plainly because it is
the whole prediction layer rather than a detail. `settle_due_predictions()`
reads `telemetry_rule -> 'params'` and `telemetry_rule ->> 'outcome_if_true'`
/ `'outcome_if_false'`. Every rule in this catalogue was written FLAT, and the
parameterless kinds carried no outcome keys at all. Against the real SQL:

* `death_in_window` and `mission_outcome` would void `malformed_rule` on every
  settlement (`coalesce(v_params ->> 'event_type', '') = ''` is true when
  `params` is absent) — real entries, no winner, ever;
* `loses_the_cops`, `survives_a_chase`, `survives_a_fight` and
  `two_star_standoff` would reach `update ... set status = 'settled', result =
  v_result` with `v_result` NULL, and
  `predictions_guard_status_transition` RAISES on a result that is not one of
  the row's outcome keys — aborting the whole settlement transaction, taking
  every other due prediction in that run down with it.

`_rule()` is now the single place the shape is written and `_validate_rule()`
refuses anything else at import time, so this class of break cannot recur. No
production row was ever affected: `predictions` has zero rows.
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
#: verbatim (kind names only). A rule whose kind is not in this set voids at
#: settlement and "credits nobody" per §3's "Voiding is mandatory" rule, so
#: nothing in this package may ever construct one.
#:
#: `event_matches` is the v2.4 addition: `event_occurs` plus a `payload_match`
#: filter, and a NEW kind rather than a new parameter on `event_occurs`
#: precisely so that a harness ahead of the database fails CLOSED — an
#: unrecognised kind voids, where an ignored extra parameter would have settled
#: every such question YES on the first `activity_end` of any shape.
TELEMETRY_RULE_KINDS: frozenset[str] = frozenset(
    {
        "event_occurs",
        "event_matches",
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

#: The params each §3 kind REQUIRES, by name. Settlement reads them out of
#: `telemetry_rule -> 'params'` (a nested object — see `_validate_rule`) and
#: voids `malformed_rule` when a required one is blank, so this table is what
#: stops such a rule being constructed in the first place.
REQUIRED_RULE_PARAMS: dict[str, tuple[str, ...]] = {
    "event_occurs": ("event_type",),
    "event_matches": ("event_type", "payload_match"),
    "wanted_reaches": ("level",),
    "wanted_clears": (),
    "wanted_gained": (),
    "survives_window": (),
    "mission_outcome": ("expect",),
    "activity_outcome": ("activity", "expect"),
}

#: What a `payload_match` value is allowed to be. `payload @> payload_match` is
#: a containment test against real event payloads, and the only values that
#: compare meaningfully there are JSON scalars: a nested object or array would
#: be a containment test nobody here has measured, and `None` is a real value
#: in these payloads (`activity_end.by` is null most of the time).
_JSON_SCALARS = (str, bool, int, float, type(None))

#: Kinds §3 RECOGNISES but settlement can never RESOLVE. `settle_due_predictions()`
#: (`20260908120000_predictions.sql:584-591`) matches these and unconditionally sets
#: `v_void_reason := 'missing_telemetry'`: there is no discrete vehicle-transition
#: event in the CONTRACTS.md §4 enum and `stats.hud.vehicle` is not time-versioned,
#: so there is nothing to settle against.
#:
#: "Recognised" and "resolvable" are DIFFERENT sets, and that gap is what shipped
#: `enters_vehicle`/`exits_vehicle` for a while: they passed `__post_init__` because
#: their kind is in TELEMETRY_RULE_KINDS, then voided 100% of the time — a live card
#: that takes real entries and never has a winner. `_build_catalog` now refuses them
#: at import time, and `test_predictions_catalog.py` re-derives this set from the
#: migration so it cannot drift away from the SQL that owns the truth.
UNSETTLEABLE_RULE_KINDS: frozenset[str] = frozenset({"vehicle_entered", "vehicle_exited"})

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

#: --- CONTRACTS-PREDICTIONS.md §3 "Cadence and entry windows" (v2.4) ---------
#:
#: **Entry window floor.** `locks_at - opened_at` is never less than 30 s for
#: any prediction. Contract's own measured need, quoted: "the page polls every
#: 8 s and the broadcast runs seconds behind the game, so the 10-20 s the first
#: catalogue allowed was mostly gone before a viewer saw the card". Enforced in
#: `Window.__post_init__`, "not by convention" — the contract names this file.
MIN_ENTRY_WINDOW_S = 30.0

#: **Scheduled rounds.** An always-available question gives "60 s to enter,
#: then a 180 s window". Both halves are fixed by §3, so an ambient template is
#: refused at construction unless its window is exactly these two numbers: the
#: 60 s is the contract's floor for a scheduled round, and the 180 s is the
#: window every rolling base rate in this system is measured over
#: (`baserate.RollingBaseRate`) — a template that resolved over some other span
#: would be offered on odds measured for a span it does not use.
AMBIENT_LOCK_DELAY_S = 60.0
AMBIENT_RESOLVE_DELAY_S = 180.0

#: **An always-available question must earn its place on every ask.** §3: it is
#: offered "only while that rate is inside [0.20, 0.80] on at least 12 sampled
#: windows". The measurement itself is `baserate.RollingBaseRate`; these three
#: constants are the admission test the generator applies to what it returns.
AMBIENT_MIN_SAMPLES = 12
AMBIENT_MIN_RATE = 0.20
AMBIENT_MAX_RATE = 0.80

#: §3's own default cadence for a scheduled round: "while the agent is live and
#: nothing situational has been asked for `round_interval_s` (default 300 s)".
#: Also the step the rolling base rate samples its windows at, so the odds a
#: viewer is shown are measured on the same grid the questions are asked on.
DEFAULT_ROUND_INTERVAL_S = 300.0

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
    # --- shipped once, then withdrawn: recognised by §3, resolvable by nobody ----
    "enters_vehicle": (
        "SHIPPED IN ERROR and withdrawn. 'WILL WANTED GET IN A VEHICLE?' passed "
        "validation because `vehicle_entered` is in TELEMETRY_RULE_KINDS, but "
        "`settle_due_predictions()` (20260908120000_predictions.sql:584-591) matches "
        "that kind and unconditionally voids with `missing_telemetry`: no discrete "
        "vehicle-transition event exists in the CONTRACTS.md §4 enum and "
        "`stats.hud.vehicle` is not time-versioned. It collected real entries and "
        "settled void 100% of the time — a prediction card that never has a winner. "
        "Same standard as `land_the_helicopter` below: unresolvable, not shipped. "
        "Re-propose only once §4 gains a real vehicle-transition event."
    ),
    "exits_vehicle": (
        "SHIPPED IN ERROR and withdrawn, identical reason to `enters_vehicle` above "
        "(`vehicle_exited` is the other kind 20260908120000_predictions.sql:584 "
        "always voids). Also note both carried reliability=0.75, ABOVE "
        "mission_outcome's honest 0.5, so the generator's ranking actively preferred "
        "the two questions that could not resolve."
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

    `lock_delay_s` is the whole of a viewer's chance to answer, and §3 (v2.4)
    puts a hard floor of :data:`MIN_ENTRY_WINDOW_S` under it — "enforced where
    the window is constructed (`harness/wasted_harness/predictions/catalog.py`),
    not by convention", which is this method.

    **Moving the lock moves what is MEASURED, not just who can enter.**
    Settlement reads `[locks_at, resolves_at]` and nothing else, so raising
    `lock_delay_s` shifts the whole settled window later by the same amount
    against the telemetry. Every base rate in this module was therefore
    re-measured when the floor landed; see the module docstring's 2026-09-21
    calibration section for the before/after numbers.
    """

    lock_delay_s: float
    resolve_delay_s: float

    def __post_init__(self) -> None:
        if self.lock_delay_s <= 0 or self.resolve_delay_s <= 0:
            raise ValueError(
                f"both delays must be > 0 (opened_at < locks_at < resolves_at); "
                f"got lock_delay_s={self.lock_delay_s}, resolve_delay_s={self.resolve_delay_s}"
            )
        # Total first, deliberately: a window that is out of range on BOTH
        # counts should report the range, which is the more basic fault.
        if not (MIN_WINDOW_S <= self.total_s <= MAX_WINDOW_S):
            raise ValueError(
                f"window total (lock_delay_s + resolve_delay_s) must be "
                f"{MIN_WINDOW_S}-{MAX_WINDOW_S}s (brief §9); got {self.total_s}s"
            )
        if self.lock_delay_s < MIN_ENTRY_WINDOW_S:
            raise ValueError(
                f"lock_delay_s is the whole time a viewer has to enter and "
                f"CONTRACTS-PREDICTIONS §3 floors it at {MIN_ENTRY_WINDOW_S}s "
                f"(the page polls every 8s and the broadcast runs seconds "
                f"behind the game); got lock_delay_s={self.lock_delay_s}"
            )

    @property
    def total_s(self) -> float:
        return self.lock_delay_s + self.resolve_delay_s

    @property
    def is_scheduled_round(self) -> bool:
        """Exactly §3's scheduled-round shape: 60 s to enter, then 180 s."""
        return (
            self.lock_delay_s == AMBIENT_LOCK_DELAY_S
            and self.resolve_delay_s == AMBIENT_RESOLVE_DELAY_S
        )


def _validate_rule(prediction_type: str, rule: dict[str, Any], outcomes: tuple[dict[str, str], ...]) -> None:
    """Refuse any `telemetry_rule` `settle_due_predictions()` cannot act on.

    **This is the shape the SQL actually reads, and it is not the shape this
    file used to write.** `settle_due_predictions()` does

        v_params        := p.telemetry_rule -> 'params';
        v_outcome_true  := p.telemetry_rule ->> 'outcome_if_true';
        v_outcome_false := p.telemetry_rule ->> 'outcome_if_false';

    (`20260908120000_predictions.sql`, the dispatch block), and
    CONTRACTS-PREDICTIONS §3 (v2.4) spells the same object out in full:

        {"kind": "event_matches",
         "params": {"event_type": "activity_end", "payload_match": {...}},
         "outcome_if_true": "yes", "outcome_if_false": "no"}

    Every rule in this catalogue used to be written FLAT — `{"kind":
    "event_occurs", "event_type": "death", ...}` — and with no outcome keys at
    all on the parameterless kinds. Against the real SQL that is not a cosmetic
    difference, it is total breakage, and it is why this validator exists:

    * a flat `event_occurs`/`mission_outcome` rule reads `v_params ->>
      'event_type'` as NULL, hits the `coalesce(..., '') = ''` guard and voids
      `malformed_rule` — a card that takes real entries and can never have a
      winner; and
    * a rule with no `outcome_if_true`/`outcome_if_false` leaves `v_result`
      NULL, and `predictions_guard_status_transition` then RAISES ("settled
      with result <NULL> which is not one of its outcome keys"), which aborts
      the whole settlement transaction — every other due prediction with it.

    So: params nested, outcome keys present, and each one an actual key of this
    template's own `outcomes`.
    """
    kind = rule.get("kind")
    if kind not in TELEMETRY_RULE_KINDS:
        raise ValueError(
            f"{prediction_type!r}: telemetry_rule.kind {kind!r} is not in the "
            f"CONTRACTS-PREDICTIONS.md §3 registry ({sorted(TELEMETRY_RULE_KINDS)}). "
            f"An unrecognised kind voids at settlement — refusing to build it."
        )
    keys = {o["key"] for o in outcomes}
    for field_name in ("outcome_if_true", "outcome_if_false"):
        value = rule.get(field_name)
        if value not in keys:
            raise ValueError(
                f"{prediction_type!r}: telemetry_rule.{field_name}={value!r} is not one of "
                f"this template's outcome keys {sorted(keys)}. settle_due_predictions() "
                f"writes it straight into `predictions.result`, and the status-transition "
                f"trigger raises on a result that is not an outcome key — aborting the "
                f"whole settlement run, not just this row."
            )
    params = rule.get("params", {})
    if not isinstance(params, dict):
        # ValueError, not TypeError (TRY004): every construction refusal in
        # this module is a ValueError, and a caller that catches one to mean
        # "this template will not ship" should not have to catch two.
        raise ValueError(  # noqa: TRY004
            f"{prediction_type!r}: telemetry_rule.params must be a JSON object "
            f"(settlement reads `telemetry_rule -> 'params'`); got {type(params).__name__}"
        )
    for required in REQUIRED_RULE_PARAMS.get(kind, ()):
        if required not in params:
            raise ValueError(
                f"{prediction_type!r}: kind {kind!r} requires params.{required}; "
                f"settlement voids `malformed_rule` without it"
            )
    if kind in ("event_occurs", "event_matches"):
        event_type = params.get("event_type")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError(
                f"{prediction_type!r}: params.event_type must be a non-empty string; "
                f"got {event_type!r} (settlement voids `malformed_rule` on a blank one)"
            )
    if kind == "event_matches":
        # §3: "a `payload_match` that is missing, not an object, or `{}`, voids
        # `malformed_rule` — an empty filter would be `event_occurs` under
        # another name". Refused here so it can never be written.
        match = params.get("payload_match")
        if not isinstance(match, dict) or not match:
            raise ValueError(
                f"{prediction_type!r}: params.payload_match must be a non-empty JSON object "
                f"(§3: an empty filter is `event_occurs` under another name); got {match!r}"
            )
        for key, value in match.items():
            if not isinstance(key, str) or not key:
                raise ValueError(
                    f"{prediction_type!r}: payload_match keys must be non-empty strings; "
                    f"got {key!r}"
                )
            if not isinstance(value, _JSON_SCALARS):
                raise ValueError(  # noqa: TRY004  — see _validate_rule's first raise
                    f"{prediction_type!r}: payload_match[{key!r}] must be a JSON scalar "
                    f"(`payload @> payload_match` is a containment test); "
                    f"got {type(value).__name__}"
                )


def _rule(
    kind: str,
    params: dict[str, Any] | None = None,
    *,
    outcome_if_true: str = "yes",
    outcome_if_false: str = "no",
) -> dict[str, Any]:
    """Build one §3-shaped `telemetry_rule`. The ONE place the shape is written.

    `params` is omitted entirely for the parameterless kinds rather than
    written as `{}`, matching what §3's table says those kinds take ("—") and
    what the SQL reads (it never touches `v_params` in those branches).
    """
    rule: dict[str, Any] = {"kind": kind}
    if params:
        rule["params"] = dict(params)
    rule["outcome_if_true"] = outcome_if_true
    rule["outcome_if_false"] = outcome_if_false
    return rule


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
    #: An ALWAYS-AVAILABLE question (CONTRACTS-PREDICTIONS §3, "Scheduled
    #: rounds"). It has no dramatic trigger — ordinary free roam is its trigger
    #: — so it is the thing the generator asks when nothing situational has
    #: happened for a round, and the ONLY thing whose fairness has to be
    #: re-established on every single ask (§3: "An always-available question
    #: must earn its place on every ask"). That re-measurement is
    #: `baserate.RollingBaseRate`, applied by the generator; this flag is what
    #: marks a template as subject to it.
    ambient: bool = False
    #: The real, recounted base rate behind this template, where one exists.
    #: Optional for an ordinary template (several in this catalog have no
    #: calibrating telemetry at all and say so at their own definition);
    #: MANDATORY for an event. When present it is ALWAYS held to
    #: [EVENT_MIN_BASE_RATE, EVENT_MAX_BASE_RATE] — a number somebody measured
    #: and then shipped outside the band is a foregone conclusion whether or
    #: not it carries the WANTED EVENT highlight.
    measured: MeasuredRate | None = None

    def __post_init__(self) -> None:
        _validate_rule(self.prediction_type, self.telemetry_rule, self.outcomes)
        if not (0.0 <= self.reliability <= 1.0):
            raise ValueError(f"{self.prediction_type!r}: reliability must be in [0,1]")
        if self.ambient:
            if self.is_event:
                raise ValueError(
                    f"{self.prediction_type!r}: a template cannot be both ambient and a "
                    f"WANTED EVENT. An event is the rarest thing on the card and an ambient "
                    f"question is the most routine; flagging both makes the rarity gate and "
                    f"the round scheduler fight over the same row."
                )
            if not self.window.is_scheduled_round:
                raise ValueError(
                    f"{self.prediction_type!r}: an ambient template must run §3's scheduled-"
                    f"round window exactly — lock_delay_s={AMBIENT_LOCK_DELAY_S}, "
                    f"resolve_delay_s={AMBIENT_RESOLVE_DELAY_S} ('60 s to enter, then a 180 s "
                    f"window'); got {self.window.lock_delay_s}/{self.window.resolve_delay_s}. "
                    f"RollingBaseRate measures this exact shape, so a different one would be "
                    f"offered on odds measured for a window it does not use."
                )
        if self.is_event:
            # A WANTED EVENT is highlighted and may carry a boosted pool. It
            # ships only on a rate somebody actually counted off a recording.
            if self.measured is None:
                raise ValueError(
                    f"{self.prediction_type!r}: an event template (is_event=True) must ship a "
                    f"MeasuredRate recounted from a real recording. An unmeasured 'major event' "
                    f"is a guess with a highlight on it — refusing to build it."
                )
            if self.measured.n < EVENT_MIN_SAMPLE:
                raise ValueError(
                    f"{self.prediction_type!r}: measured on {self.measured.n} real anchors, "
                    f"below EVENT_MIN_SAMPLE={EVENT_MIN_SAMPLE}. Not enough real moments to "
                    f"call this a base rate — refusing to build it."
                )
        # The admission band applies to ANY measured rate, event or not: a
        # number somebody counted and then shipped outside the band is a
        # foregone conclusion whether or not it carries the highlight. This is
        # the check that would have caught a re-measured window drifting out of
        # range when the §3 entry-window floor moved every lock.
        if self.measured is not None and not (
            EVENT_MIN_BASE_RATE <= self.measured.rate <= EVENT_MAX_BASE_RATE
        ):
            raise ValueError(
                f"{self.prediction_type!r}: measured base rate {self.measured.rate:.3f} is "
                f"outside [{EVENT_MIN_BASE_RATE}, {EVENT_MAX_BASE_RATE}] — a foregone "
                f"conclusion, not a question. Refusing to build it."
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
# RE-MEASURED 2026-09-21 for the §3 entry-window floor (lock 15s -> 30s, the
# settled window unchanged at 225s but now starting 15s later): 14/39 real
# gains (35.9%) are followed by a real `death` row inside [locks_at,
# resolves_at]. Band across the whole 0-60s generation delay: 25.6-35.9%.
# The old comment here said 43.6%; that figure was measured anchor-to-anchor
# (`(gain, gain+240s]`), which is NOT what settlement reads.

_DEATH_WINDOW = Window(lock_delay_s=30.0, resolve_delay_s=225.0)  # 255s total

DEATH_IN_WINDOW_RATE = MeasuredRate(
    yes=14,
    n=39,
    fixture="tests/fixtures/real_session_2026-09-04.json",
    anchor="wanted_change with to > from",
    settled_window_s=225.0,
    generation_delay_band_s=(0.0, 60.0),
)


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
    # Seeded from the re-measured 35.9% base rate (n=39), nudged by real-time
    # danger signals the calibration data could not see (it has no /state).
    p = _clamp(
        DEATH_IN_WINDOW_RATE.rate
        + 0.20 * (1.0 - _health_fraction(state))
        + 0.10 * (1.0 if _under_attack(state) else 0.0)
        + 0.05 * (1.0 if _hostile_nearby(state) else 0.0)
    )
    # Weighted toward watchability rather than 50/50 with uncertainty: the
    # measured base rate (35.9%) already sits close to 0.5, so pushing p up
    # with real danger signals trades a little uncertainty for a lot of
    # "this is the moment to watch" — a near-death instant is more must-see
    # than it is more predictable, and the blend should say so.
    return 0.35 * _uncertainty(p) + 0.65 * p





TPL_DEATH_IN_WINDOW = PredictionTemplate(
    prediction_type="death_in_window",
    question="NOW THAT THE COPS ARE ON HIM: WILL WANTED DIE IN THE NEXT 4 MINUTES?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule=_rule("event_occurs", {"event_type": "death"}),
    window=_DEATH_WINDOW,
    trigger=_trigger_death_in_window,
    score=_score_death_in_window,
    reliability=0.85,
    measured=DEATH_IN_WINDOW_RATE,
)


# --- 2. loses the cops, right after a real wanted-star gain -----------------------
# kind: wanted_clears — `wanted_change` payload `to = 0`.
# RE-MEASURED 2026-09-21 for the §3 entry-window floor (lock 10s -> 30s, the
# settled window unchanged at 80s): 21/39 real gains (53.8%) clear to 0 inside
# [locks_at, resolves_at]. The headline number is unchanged by coincidence, not
# by construction — the old 53.8% was the anchor-to-anchor `(gain, gain+90s]`
# approximation, and the true settled window `[gain+30, gain+110]` happens to
# catch the same 21 gains. Band across the 0-60s generation delay: 35.9-53.8%.

_LOSES_COPS_WINDOW = Window(lock_delay_s=30.0, resolve_delay_s=80.0)  # 110s total

LOSES_THE_COPS_RATE = MeasuredRate(
    yes=21,
    n=39,
    fixture="tests/fixtures/real_session_2026-09-04.json",
    anchor="wanted_change with to > from",
    settled_window_s=80.0,
    generation_delay_band_s=(0.0, 60.0),
)


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
        LOSES_THE_COPS_RATE.rate
        + 0.10 * (1.0 if state.player.in_vehicle else -0.10)
        - 0.05 * (state.player.wanted / 5.0)
    )
    watchability = 0.5 + 0.5 * (state.player.wanted / 5.0)
    return 0.5 * _uncertainty(p) + 0.5 * _clamp(watchability)


TPL_LOSES_THE_COPS = PredictionTemplate(
    prediction_type="loses_the_cops",
    question="THE COPS JUST CAME ON: WILL WANTED LOSE THEM?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule=_rule("wanted_clears"),
    window=_LOSES_COPS_WINDOW,
    trigger=_trigger_loses_the_cops,
    score=_score_loses_the_cops,
    reliability=0.85,
    measured=LOSES_THE_COPS_RATE,
)


# --- 3. survives the chase, right after a real wanted-star gain -------------------
# kind: survives_window — absence of `death`/`busted` in the window. `busted`
# IS emitted (EMITTED_EVENT_TYPES), same as `death`.
# CALIBRATED: an AMBIENT "survives 3 min" is a 99.1% giveaway (recomputed
# here; the coordinator measured 97.9% on their own denominator — see the
# module docstring for why the two differ). Anchored on a real gain instead:
# RE-MEASURED 2026-09-21 for the §3 entry-window floor (lock 10s -> 30s, the
# settled window unchanged at 140s), 22/39 (56.4%) survive with no death or
# busted row inside [locks_at, resolves_at]. The old 48.7% was the
# anchor-to-anchor `(gain, gain+150s]` approximation. Band across the 0-60s
# generation delay: 56.4-74.4% — the widest drift of any template here,
# because every second the window slides forward is a second further from the
# moment the heat came on. Still inside the admission band at both ends.

_SURVIVES_CHASE_WINDOW = Window(lock_delay_s=30.0, resolve_delay_s=140.0)  # 170s total

SURVIVES_A_CHASE_RATE = MeasuredRate(
    yes=22,
    n=39,
    fixture="tests/fixtures/real_session_2026-09-04.json",
    anchor="wanted_change with to > from",
    settled_window_s=140.0,
    generation_delay_band_s=(0.0, 60.0),
)


def _trigger_survives_a_chase(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    return (
        not state.player.dead
        and not state.player.arrested
        and state.player.wanted > 0
        and _recent_wanted_gain(recent_events)
    )


def _score_survives_a_chase(state: GameState) -> float:
    # Seeded from the re-measured 56.4% base rate (n=39); real-time health and
    # heat only nudge it.
    p = _clamp(
        SURVIVES_A_CHASE_RATE.rate
        + 0.15 * (_health_fraction(state) - 0.5)
        - 0.05 * (state.player.wanted / 5.0)
    )
    watchability = 0.5 + 0.5 * (state.player.wanted / 5.0)
    return 0.5 * _uncertainty(p) + 0.5 * _clamp(watchability)


TPL_SURVIVES_A_CHASE = PredictionTemplate(
    prediction_type="survives_a_chase",
    question="THE COPS JUST CAME ON: WILL WANTED SURVIVE THIS CHASE?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule=_rule("survives_window"),
    window=_SURVIVES_CHASE_WINDOW,
    trigger=_trigger_survives_a_chase,
    score=_score_survives_a_chase,
    reliability=0.9,
    measured=SURVIVES_A_CHASE_RATE,
)


# --- 4. survives a fight -----------------------------------------------------------------
# kind: survives_window, same rule as the chase question — a different
# prediction_type/question/trigger, the identical (parameterless) rule.
# NOT CALIBRATED against real_session_2026-09-04.json: nothing in an
# `events`-only export marks "under attack" (that is `/state.threat.
# attacker_handle`, never a §4 event), so no fight-specific window could be
# measured. `enters`/`exits_vehicle`'s caveat applies here identically.

# The §3 entry-window floor moved this lock 10s -> 30s (2026-09-21). There is
# no base rate to re-measure: see above, an events-only export carries no
# threat telemetry at all, so this template has never had one.
_SURVIVES_FIGHT_WINDOW = Window(lock_delay_s=30.0, resolve_delay_s=50.0)  # 80s total


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
    telemetry_rule=_rule("survives_window"),
    window=_SURVIVES_FIGHT_WINDOW,
    trigger=_trigger_survives_a_fight,
    score=_score_survives_a_fight,
    reliability=0.9,
)


# --- 5. mission complete / fail -----------------------------------------------------------
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

# The §3 entry-window floor moved this lock 20s -> 30s (2026-09-21). The
# window TOTAL is already at MAX_WINDOW_S, so the ten seconds came off the
# settled span (280s -> 270s) rather than being added to the total; there is no
# base rate to re-measure (this recording has zero mission events).
_MISSION_OUTCOME_WINDOW = Window(lock_delay_s=30.0, resolve_delay_s=270.0)  # 5 min total (capped)


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
    telemetry_rule=_rule("mission_outcome", {"expect": "passed"}),
    window=_MISSION_OUTCOME_WINDOW,
    trigger=_trigger_mission_outcome,
    score=_score_mission_outcome,
    reliability=0.5,
)


# --- 6. WANTED EVENT: two stars — his ceiling — held for four minutes ------------
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
# 240s settled window contains no `death`/`busted` row for 7 of them (41.2%) at
# zero generation delay and 8 (47.1%) at a 60s delay — fair across the whole
# band. Module docstring, calibration pt 5.
#
# RE-MEASURED 2026-09-21 for the §3 entry-window floor (lock 15s -> 30s): the
# settled window is the same 240s, fifteen seconds later, and the rate moves
# 29.4% -> 41.2% at zero delay. Band across 0-60s: 41.2-47.1%, tighter than
# before and closer to even — a rarer accident of this recording than it
# sounds, and the reason the sweep test exists rather than a single number.

_TWO_STAR_STANDOFF_WINDOW = Window(lock_delay_s=30.0, resolve_delay_s=240.0)  # 270s total

TWO_STAR_STANDOFF_RATE = MeasuredRate(
    yes=7,
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
    # Seeded from the re-measured 41.2% (n=17) at the pessimistic end of the
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
    telemetry_rule=_rule("survives_window"),
    window=_TWO_STAR_STANDOFF_WINDOW,
    trigger=_trigger_two_star_standoff,
    score=_score_two_star_standoff,
    # A notch under `survives_a_chase`'s 0.9: same always-resolves rule, but a
    # 270s window is 1.6x the exposure to a session_end/bridge_down overlap,
    # which §3 makes a mandatory void. (Zero of the 17 real anchors actually
    # had one, so this is caution, not an observed failure rate.)
    reliability=0.85,
    is_event=True,
    measured=TWO_STAR_STANDOFF_RATE,
)


# --- 7-9. the ALWAYS-AVAILABLE questions (CONTRACTS-PREDICTIONS §3, v2.4) --------
#
# Everything above needs a dramatic moment: a wanted star, a fight, a mission.
# The 2026-09-20 recording is 2 h 19 m of real live play with ZERO
# `wanted_change`, `death` or `busted` rows in it — so on a day like that one,
# every template above is unofferable and the card is empty for the entire
# broadcast. §3's answer is a scheduled round: one always-available question
# every `round_interval_s`, on the one thing the agent does constantly.
#
# THE ONE THING HE DOES CONSTANTLY is finish (or fail to finish) free-roam
# goals: `activity_end` is 2,668 of the 5,649 rows in the 2026-09-04 recording
# and 112 of the 230 in the 2026-09-20 one. So all three of these read
# `activity_end`, with `event_matches` — `event_occurs` plus a payload filter,
# the kind §3 added in v2.4 for exactly this.
#
# WHAT THE PAYLOAD REALLY CARRIES, checked in the emitters rather than assumed
# (`behavior/activities.py:finish`, `behavior/planner.py:_end_trip`, and
# `main.py:_end_activity` which merges `behavior/roam.py:close`):
#   * `outcome` — ALWAYS present. Both emitters build the payload with it, and
#     it is on 2,668/2,668 and 112/112 real rows.
#   * `category` — NOT always present. It comes from `roam.close()`, which
#     returns `{}` when no roam goal was locked, so the day planner's own
#     `go_start_a_job` rows carry none: 2,116/2,668 (79.3%) on 2026-09-04 and
#     108/112 (96.4%) on 2026-09-20 carry it, and every row missing it is a
#     `go_start_a_job`. That is FINE for a `payload_match` on
#     {"category": "trouble"} — `payload @> '{"category":"trouble"}'` is simply
#     false for a row with no `category` key, which is the right answer — but
#     it is not fine to describe the question as "any activity", so
#     `gets_into_trouble` is worded as him going looking for trouble, which is
#     what a `trouble`-category goal is.
#
# NONE OF THE THREE SHIPS A FIXED BASE RATE, and that is the point of §3. The
# same question measures 50% on 2026-09-20 and 8% on 2026-09-04 (see
# AMBIENT_RECORDED_RATES) — a fixed rate would have been a giveaway on one of
# those days. What decides whether one is asked is `baserate.RollingBaseRate`,
# re-measured from the agent's own recent telemetry on every ask.

#: What the two real recordings say about each ambient question, measured by
#: running the SHIPPED estimator (`baserate.RollingBaseRate`) over each file
#: with its clock pinned to the recording's last event: windows of the
#: template's exact shape ([t+60, t+240]), stepped by
#: DEFAULT_ROUND_INTERVAL_S back from the newest closed one, counting the
#: windows that contain a matching event. `(yes, n)` per file.
#:
#: These are NOT thresholds and nothing reads them at runtime — they are the
#: evidence for why the runtime gate has to exist, and
#: `tests/test_predictions_catalog.py` re-derives every pair from the files
#: through that same estimator.
#:
#: **The sampling grid is worth a sentence, because it moves the answer.**
#: Stepping FORWARDS from the first event instead gives (82, 1043), (32, 1043)
#: and (77, 1043) on 2026-09-04 — the same 1,043 windows, offset by 108 s, and
#: up to 22% different in count. Both are honest measurements of a bursty
#: process; neither is "the" rate. That is precisely why §3 asks for a live
#: rolling measurement with a minimum sample and a band around it rather than
#: a number written into a template, and why nothing here treats a single
#: point estimate as a fact about the agent.
AMBIENT_RECORDED_RATES: dict[str, dict[str, tuple[int, int]]] = {
    "pulls_off_a_goal": {
        "real_session_2026-09-20.json": (14, 28),  # 50%
        "real_session_2026-09-04.json": (75, 1043),  # 7%
    },
    "runs_out_of_time": {
        "real_session_2026-09-20.json": (9, 28),  # 32%
        "real_session_2026-09-04.json": (31, 1043),  # 3%
    },
    "gets_into_trouble": {
        "real_session_2026-09-20.json": (7, 28),  # 25%
        "real_session_2026-09-04.json": (94, 1043),  # 9%
    },
}

_AMBIENT_WINDOW = Window(
    lock_delay_s=AMBIENT_LOCK_DELAY_S, resolve_delay_s=AMBIENT_RESOLVE_DELAY_S
)


def _ordinary_free_roam(state: GameState, recent_events: Sequence[RecentEvent]) -> bool:
    """§3's "ordinary free roam": the moments the situational templates DON'T own.

    Alive, not in cuffs, no cutscene, no mission, and no heat — a wanted star,
    a bust or a mission start is a situational template's moment, and asking an
    always-available question over the top of one would put two cards on the
    screen describing the same minute.

    The other half of "believable state" is the CALLER's: `main.py`'s
    `_offer_prediction` refuses to offer anything at all while
    `_believe_state()` is false (the blocking-screen watchdog holding a frozen
    snapshot). That gate is upstream of every template here, ambient or not, so
    it is deliberately not repeated per-template.

    `recent_events` is unused: "ordinary" is a property of the live state, not
    of what happened to be recorded in the last minute.
    """
    return (
        not state.player.dead
        and not state.player.arrested
        and not state.mission.active
        and not state.mission.cutscene_active
        and state.player.wanted == 0
    )


def _ambient_score(state: GameState) -> float:
    """Ranking among ambient candidates is by MEASURED rate, not by this.

    `generator._pick_ambient` orders on |rate - 0.5| from the live rolling
    measurement and then on novelty, because for an always-available question
    the measurement is the only honest statement about how uncertain it is.
    This exists because `PredictionTemplate.score` is not optional; it returns
    the neutral value so that if an ambient template ever does reach the
    situational ranker it neither wins nor loses on a number nobody measured.
    """
    return 0.5


TPL_PULLS_OFF_A_GOAL = PredictionTemplate(
    prediction_type="pulls_off_a_goal",
    question="WANTED IS OFF DOING HIS OWN THING: WILL HE ACTUALLY PULL IT OFF IN THREE MINUTES?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule=_rule(
        "event_matches",
        {"event_type": "activity_end", "payload_match": {"outcome": "completed"}},
    ),
    window=_AMBIENT_WINDOW,
    trigger=_ordinary_free_roam,
    score=_ambient_score,
    # `event_matches` always produces an outcome — absence is a definite NO,
    # exactly as for `event_occurs` (§3) — so the only way this voids is a
    # session_end/bridge_down overlap or the completeness gate never opening.
    # Same standing as `death_in_window`, over a shorter window.
    reliability=0.85,
    ambient=True,
)

TPL_RUNS_OUT_OF_TIME = PredictionTemplate(
    prediction_type="runs_out_of_time",
    question="WILL WANTED RUN THE CLOCK OUT ON WHAT HE IS DOING IN THE NEXT THREE MINUTES?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule=_rule(
        "event_matches",
        {"event_type": "activity_end", "payload_match": {"outcome": "timeout"}},
    ),
    window=_AMBIENT_WINDOW,
    trigger=_ordinary_free_roam,
    score=_ambient_score,
    reliability=0.85,
    ambient=True,
)

TPL_GETS_INTO_TROUBLE = PredictionTemplate(
    prediction_type="gets_into_trouble",
    question="QUIET STREET, NO COPS: WILL WANTED GO LOOKING FOR TROUBLE IN THREE MINUTES?",
    outcomes=YES_NO_OUTCOMES,
    telemetry_rule=_rule(
        "event_matches",
        {"event_type": "activity_end", "payload_match": {"category": "trouble"}},
    ),
    window=_AMBIENT_WINDOW,
    trigger=_ordinary_free_roam,
    score=_ambient_score,
    reliability=0.85,
    ambient=True,
)


def _build_catalog() -> tuple[PredictionTemplate, ...]:
    catalog = (
        TPL_DEATH_IN_WINDOW,
        TPL_LOSES_THE_COPS,
        TPL_SURVIVES_A_CHASE,
        TPL_SURVIVES_A_FIGHT,
        TPL_MISSION_OUTCOME,
        TPL_TWO_STAR_STANDOFF,
        TPL_PULLS_OFF_A_GOAL,
        TPL_RUNS_OUT_OF_TIME,
        TPL_GETS_INTO_TROUBLE,
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
        kind = tpl.telemetry_rule["kind"]
        if kind in UNSETTLEABLE_RULE_KINDS:
            raise ValueError(
                f"{tpl.prediction_type!r} uses rule kind {kind!r}, which settlement "
                f"recognises but can never resolve — it voids 100% of the time "
                f"(20260908120000_predictions.sql:584). A question that can never have "
                f"a winner must not be offered; put it in REJECTED_TEMPLATES instead."
            )
    return catalog


#: The full, real catalogue. Built (and validated) once at import time.
CATALOG: tuple[PredictionTemplate, ...] = _build_catalog()
