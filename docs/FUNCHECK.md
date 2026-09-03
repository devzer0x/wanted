# Offline fun-to-watch pipeline (fix-sonnet-e)

Three pieces, run in order. Nothing here needs the API key or the game to be
running except step 1 (which needs the bridge, on the game box).

```
1. record on the box   harness/tools/record_state.py
2. replay locally       harness/tests/support/replayer.py
3. read the table       harness/tools/funcheck.py
```

## 1. Record (on the game server, bridge running)

```
python harness/tools/record_state.py --out session.states.jsonl --hz 5
```

Polls `GET /state` at 5 Hz, appends one line per tick:
`{"ts": "<wall-clock ISO-8601>", "state": {<raw /state body>}}`. Robust to the
bridge being down/not-ready (logs, keeps polling). Refuses to record while a
network session is active (CLAUDE.md rule 5) — same as everything else in
this repo. Ctrl-C to stop.

`--from-log <harness-run.log> --out derived.jsonl` does **not** reconstruct a
state stream — a harness run log never carries a raw `/state` body, verified
against `logsetup.py` and every `log.*` call in `main.py`. It extracts the
log-derived events that ARE recoverable (decisions, §4 events, wheel
transitions) into a differently-shaped file; do not pass that file to
`replay_from_jsonl`.

## 2. Replay (any machine, no game, no network)

```python
from support.replayer import replay_from_jsonl
log = replay_from_jsonl(Path("session.states.jsonl"), brain_mode="policy")
log.write_jsonl(Path("session.replay.jsonl"))
log.write_states_jsonl(Path("session.replay_states.jsonl"))
```

Feeds the recorded states through the **real** selection code
(`Harness._reflex`, `_drive_day_plan`, `_drive_activities`, its roam-goal
helpers, `_drive_mission_objective`, `_think`, `_dynamic_context`,
`_apply_decision`, `_execute_action` — all bound from production, unmodified)
with the network seam replaced by `support.fakebrain` (`mode=`: `policy`
deterministic play-it-straight, `misbehave` for a scripted bad decision that
still passes pydantic, `reject_once` for one that pydantic really rejects, to
exercise the real retry loop). Run from `harness/tests/` (or with it on
`PYTHONPATH`) — `support` is test-support, not a shipped package.

**One recording, one replay.** A real recording came from exactly one bridge,
so `last_task.id` values are globally consistent and `replay_from_jsonl`
resolves "is this the task I'm waiting on" correctly. Two different
`ReplayHarness` instances issue independent id sequences — do not feed one
harness's own postings back into a second one and expect task-completion
tracking to line up (see `tools/soak.py`'s `run()` docstring for the failure
mode this produces, and its NOT VERIFIED entry).

## 3. Read the table

```
python harness/tools/funcheck.py --log session.replay.jsonl --states session.replay_states.jsonl
```

```
CHECK  RESULT  DETAIL
-----  ------  ------------------------------------------------
F1     PASS    worst window idle_ratio=1.9% over t=0s-312s
F2     FAIL    longest gap 180s (starting t=37s); 40 moments total
F3     PASS    12 lines checked, 0 offenders
F4     PASS    16/18 completed (89%)
F5     PASS    1 roam deaths over 0.20h -> 5.0/h
F6     PASS    2/2 edges answered within 3s
```

Non-zero exit on any FAIL (`--exit-zero` for a dashboard that wants the
numbers without failing a job; `--json out.json` also writes them machine-
readable).

| # | Checks | Numbers PASS needs |
|---|---|---|
| F1 | idle ratio | < 5% of any measurable (>=60s) 10-min trailing window is "control_enabled, no cutscene/switch/retry, not dead/arrested, no movement task running" |
| F2 | something new | no gap > 60s between a goal pick/end, a vehicle change, a wanted change, a fight (`fight_ped`/`combat_hated_targets_around`/the `threat`/`flip` wheel owner), or a death |
| F3 | commentary hygiene | every `say` line: no banned phrase (`brain/prompts/banned_phrases()`, parsed from `commentary_style.md`; a small fallback list if that returns nothing), < 60% Jaccard overlap with any of the last 5 lines (`brain.schemas.jaccard_similarity` — the real T4 function), every name in `present_names(state)` at that tick (`brain.characters`, also real), and something else logged within ±20s |
| F4 | roam completion | `activity_end` events with a real `verified` flag (i.e. a locked roam goal, not a mission/wanted/day-plan interruption), excluding preempted ones: `verified=True` rate >= 60%, and no two `timeout` outcomes back to back |
| F5 | roam deaths | `death` events whose nearest state has `mission.active=false`, rate < 6/hour (extrapolated honestly on a short log) |
| F6 | resume after control | every respawn / mission-end / cutscene-end / interior-exit edge (from consecutive states) has a movement-class posted task within 3s — the same edges `behavior.vehicle.ControlRegained` (F6 in production) already watches for real |

Unit tests: `harness/tests/test_funcheck.py` (a hand-built PASS log and at
least one FAIL log per check, `harness/tests/support/states.py::make_state`
inputs — never files under `tests/fixtures/`).

## The 12-minute demo

```
python harness/tools/soak.py --minutes 12 --out-dir /tmp/wasted-soak
```

Synthesises idle car -> goal pick(s) -> death -> respawn (indoors) ->
`InteriorEscape` walks him out -> more free roam, via a small reactive world
model that grants only the outcome an action physically produces, never an
outcome with no action behind it:

- `drive_to` / `walk_to` travel at 15 m/s / 3 m/s (capped at 45 s so a
  cross-map landmark does not eat the run) and arrive; `set_waypoint` is
  `done` the same tick; `wander_drive` / `flee_police` / etc. never complete
  (CONTRACTS §1) and accrue distance instead.
- He is armed the way bridge 1.7.0's default `ammunation` loadout leaves him
  (`player.weapon` present, the three tracked weapons owned), and two
  bystanders stand on the pavement — without a ped in `nearby.peds` no heat
  goal can open with its violence verb, so the soak was measuring their
  unarmed branches only.
- Gunfire costs rounds (`player.weapon.owned` goes down) and draws the police:
  one star on the burst, two after 8 s if he keeps going; 30 s of
  `flee_police` clears them, and so does 90 s of nothing.
- A rushed run-up after a rushed, fast `drive_to` (`big_jump`'s approach) gets
  two polls of `vehicle.in_air`, once per approach.

**This is a synthetic demonstration of the pipeline, not a fixture and not a
claim about real gameplay** (CLAUDE.md rule 1 is about `tests/fixtures/*.json`;
nothing here is written there or presented as a recording). Every one of the
grants above is a place where the real game may say no — which is exactly what
the recorder + funcheck over the first live 20 minutes is for.

Numbers on 2026-09-03 (HEAD of the fun-to-watch program), 12 min seed 3 /
seed 7 / 20 min seed 3:

```
F1  PASS  idle 3.9% / 3.6% / 3.9%   (worst window)
F2  PASS  gap 46 s / 51 s / 51 s     (52 / 52 / 79 moments)
F3  PASS  0 offenders in 11 / 9 / 10 lines
F4  PASS  95% / 95% / 97% completed
F5  PASS  5.0 / 5.0 / 3.0 deaths per hour (the one scripted death)
F6  PASS  2/2 edges answered within 3 s
```

What the soak found on the way to those numbers, all fixed in product code
(see `docs/findings.md`, "Integration findings"): a goal died as `stuck`
after every phone task; `earn_two_stars` was passive; the wanted gate closed
heat goals one poll after they earned the star; the threat reflex fled the
star a heat goal wanted; the brain's `wait` looped him into standing still;
`big_jump` refused a jump made from next to the ramp; `flee_ped` was missing
from the client's task table.
