# Test fixtures

Per CLAUDE.md non-negotiable #1, fixtures here may only ever be **recordings of real sessions** —
never hand-written or synthesised game data. `test_contract_conformance.py::
test_every_fixture_is_a_real_recording` enforces that: every `*.json` here must carry a
`_provenance` block naming the session it came from, how it was captured and when, and no bridge
contract-sample may be copied in under any name.

## What is here

### `real_session_2026-09-04.json`

5,649 `public.events` rows from session `2c135a5f-f040-4c2f-970f-f52bc034e9cb`, pulled unmodified
from the production Supabase project with the read-only publishable key, ordered by `ts` ascending.
Spans `2026-09-04T00:59:06Z` to `2026-09-07T15:55:02Z` (the session row stayed open across days;
the actual gameplay is concentrated in bursts on 2026-09-04).

It contains 12 deaths, 4 arrests, 63 `wanted_change` transitions, 2,668 `activity_end` outcomes,
172 breaks, 59 unstick nudges, and the session's own `session_start`/`session_end`.

Its `_provenance.absent_types_and_why` records what the recording **cannot** exercise, so nobody
calibrates against a hole and calls it a result:

- no `mission_start`/`mission_end`/`mission_fail` — missions were switched off by operator decision
  for that session, so mission rules and mission base rates are untestable against it;
- no `bridge_down`/`bridge_up` — no bridge outage occurred, so the outage void path is not
  exercised here;
- no `stunt` — never produced by the harness at all (see `UNPRODUCED_EVENT_REASONS` in
  `wasted_harness/events.py`).

Used by the prediction catalogue's calibration tests, which re-derive their base rates from this
file on every run rather than trusting a number written in a comment, and by
`infra/verify-predictions.sh`, which replays it into a real Postgres to prove settlement reaches the
outcomes that actually happened.

### `real_session_2026-09-20.json`

230 `public.events` rows from session `77650284-d0c3-4c72-9af2-b51a08ef738e`, pulled unmodified
from the production Supabase project with the read-only publishable key while the session was
still live, ordered by `ts` ascending. Spans `2026-09-20T20:17:50Z` to `2026-09-20T22:36:57Z`:
2 h 19 m of ordinary free roam — 113 `activity_start`, 112 `activity_end`, 4 breaks, and the
session's own `session_start`. No `session_end`, because it had not ended.

It exists because the 2026-09-04 recording predates the free-roam rework: goals completed 7 % of
the time there and 30 % here, so a base rate measured on that file alone describes an agent that
no longer plays that way. "Pulls off a goal within three minutes" is 8 % on one and 50 % on the
other, which is the whole argument for re-measuring an always-available question's odds on every
ask (CONTRACTS-PREDICTIONS §3, "Cadence and entry windows").

Its `_provenance.absent_types_and_why` is longer than the other file's, and that is the point of
reading it: no `wanted_change`, `death` or `busted` occurred, so this recording cannot exercise
any wanted, survival or death rule — and every situational template in the catalogue was
unofferable for its entire length.
