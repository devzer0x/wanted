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
