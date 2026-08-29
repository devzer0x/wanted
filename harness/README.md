# harness — the agent's brain and body (Python 3.12+)

Owner: harness executors. Package: `wasted_harness`. Interfaces: docs/CONTRACTS.md **v1.2**.

v1.2 in this package: `last_task.id`/`type` are Optional and every consumer treats them as
nullable (the first poll of a session really is `null`); `/health.edition` may be `unknown`
and `unknown` is the default written to `sessions.game_edition` until the bridge says
otherwise; the bridge error-code set is enumerated in `bridge_client.BRIDGE_ERROR_STATUS`,
with `not_ready`/`game_thread_stalled`/`queue_full` — **and any code this version has never
heard of** — raised as `BridgeTransientError` and handled by waiting, never by crashing.

## Layout

- `wasted_harness/bridge_client.py` — typed client for the bridge API (§1)
- `wasted_harness/perception.py` — poll deltas, dxcam capture (win32-only, rebuilds itself
  when Desktop Duplication is lost), 768px JPEG, objective dHash
- `wasted_harness/brain/` — DecisionModel (§2), tactical (Haiku) + director (Sonnet) via
  `messages.parse`, cached static prefixes, memory files, `prompts/` (the agent's character)
- `wasted_harness/behavior/` — humanizer (mood, breaks, idle), activity catalog + runner,
  mission skeleton, recovery reflexes
- `wasted_harness/commentary.py` — feed lines, recent-line memory, persisted death/busted
  rotation (no repeat in 10)
- `wasted_harness/events.py` — batched service-role Supabase writes + locked, capped offline queue
- `wasted_harness/budget.py` — pricing.yaml costs + L0-L3 governor (§7)
- `wasted_harness/obs.py` — replay-buffer clips synced on ReplayBufferSaved
- `wasted_harness/overlay/` — OBS browser-source overlay + SSE (§6)
- `wasted_harness/primitives.py` — SendInput manual-control primitives (win32-only)
- `wasted_harness/main.py` — supervised loop; `--check`; `--prompt-audit`
- `wasted_harness/tools/post_event.py` — watchdog CLI (queues offline when Supabase is absent)
- `config/pricing.yaml` — model IDs + prices, sourced + dated (the runtime source of truth)

## Setup

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'            # on the Windows game server: '.[dev,windows]'
cp .env.example .env               # fill in keys on the server
pytest                             # unit tests (fixtures arrive from real sessions only)
ruff check wasted_harness tests    # lint (config in pyproject.toml)
python -m wasted_harness.main --check         # readiness report, exits nonzero if missing
python -m wasted_harness.main --prompt-audit  # prompts/schema/token counts, no game needed
python -m wasted_harness.main                 # the real loop (needs bridge + ANTHROPIC_API_KEY)
```

`--check` is the single source of truth for server readiness. It probes, for real: pricing
file, state directory (writability + offline-queue backlog), bridge `/health`, the Claude key
plus both static-prefix cache minimums, Supabase, the OBS websocket and replay buffer,
dxcam screenshot capture, SendInput availability **and whether this process is in the console
session**, and whether the overlay port is free.

`--prompt-audit` validates the brain's static half without a game: prompt files assemble to a
byte-stable prefix, the action catalog names exactly the schema's action types, the decision
schema is well-formed, and (with a key) the real token count of each prefix plus the per-call
cost. It deliberately does **not** produce a decision — that needs a game-state snapshot, and
inventing one would be fabricating game data.

## Windows-server notes (the machine this runs on)

- The box has **no discrete GPU and no physical monitor**: an Intel UHD 770 iGPU with an
  indirect display driver (IddCx virtual monitor) providing the console session's display.
  `ScreenGrabber` treats a lost duplication as recoverable and rebuilds its camera.
- **The harness must run in the console session**, not an RDP session. SendInput cannot reach
  the game from another session and Desktop Duplication cannot see it. `--check` reports the
  session ids and warns when they differ.
- **No virtual gamepad.** ViGEmBus is archived and refuses Windows Server SKUs; keyboard and
  mouse via SendInput is the only input path and the only one the code has.
- Console output is forced to UTF-8 — a legacy code page cannot encode the section marks and
  dashes in log lines, and a crashed logging handler loses the run log.
- The offline queue is guarded by a cross-process lock and an atomic, retrying rewrite,
  because the watchdog CLI writes the same file and Windows turns that race into a sharing
  violation.

## Verification status (honest)

- **Verified locally** (real commands, this machine): 151 unit tests — including the
  cross-package conformance suite above (the harness models against the bridge's real
  serialized `/state`, `/health` and error bodies, replayed over a real socket), the supervised
  loop surviving every v1.2 transient 503 and an unknown error code, the governor-L3 scenic
  park, the activity runner's task-id matching, and the cadence/cost table; plus budget math
  against the
  real pricing.yaml, commentary rotation and recent-line memory, humanizer bounds and the mood
  clock, activity selection/variety/runner lifecycle, decision-schema enforcement, prompt
  catalog-vs-schema agreement, SendInput struct layout against the documented Windows x64
  sizes, offline queue vs a real refused connection including a concurrent-handle rewrite and
  the size cap, bridge client vs a dead port, overlay via its real ASGI app; `ruff check`
  clean; `--check` and `--prompt-audit` failure paths.
- **Authored, awaiting the game server** (Phase 2 live check): bridge task execution, dxcam
  capture against the virtual display, SendInput actually reaching the game window, the OBS
  replay pipeline, Supabase flush against the real project, live model calls and prompt-cache
  telemetry, measured $/hour.
- **Blocked on the API quota**: the Anthropic key in `.env` is at its usage limit until
  2026-09-01, so the live `count_tokens` prefix check could not be re-run after the prompt
  rewrite. `--prompt-audit` prints a clearly-labelled estimate in that case and still exits
  nonzero; the real number must be confirmed once the quota resets.
- Mission completion logic is a Phase 4 deliverable; `behavior/missions.py` is the
  flag-driven state machine only and says so.
- Landmark and stunt coordinates in `behavior/activities.py` are curated, **not measured**.
  They are tuned in Phase 3; the activity runner times out a step that cannot complete so a
  wrong number degrades to a short detour rather than a parked stream.

## Declared gaps — §4 events nothing emits yet

Every §4 type that has no producer is listed in `events.UNPRODUCED_EVENT_REASONS` with its
reason, and every trigger set (director vision triggers, director big events, humanizer mood
rules) is built by *subtracting* that set. Wiring one up is a one-line change there, and
`tests/test_event_wiring.py` fails if a declaration and the code ever disagree in either
direction.

- **`stunt` — Phase 3, and it needs a bridge change first.** The `stunt` event
  (`{kind: "jump|big_air", airtime_s}`) is in CONTRACTS §4 and was wired into the director's
  vision/big-event sets and the humanizer's mood table while **nothing produced it**. It has
  been unwired rather than faked. It cannot be derived honestly from /state v1.2: there is no
  on-ground flag and no vertical velocity, and at the 2-4 Hz poll rate a large z delta between
  snapshots is a jump, a hill, a car-park ramp or a lift with equal probability — `airtime_s`
  would be an invented number, which CLAUDE.md rule 1 forbids. **Phase 3 work:** add an
  airborne/airtime field to /state on the bridge side (a contract version bump), then emit
  `stunt` from the harness and delete the entry from `UNPRODUCED_EVENT_REASONS`.
- **`mission_end` / `mission_fail` — Phase 4.** Outcome detection (reading the passed/failed
  screen) is the Phase 4 deliverable; the skeleton emits only `mission_start`.

## Cadence and cost — the real numbers

Tactical calls are bounded by `brain.tactical.MIN_TACTICAL_GAP_S`, an 8 s floor applied to
**every** trigger, not only the timer. That floor is what makes the contracted 8-25 s cadence
real: `delta.danger` is true on every poll while `wanted > 0`, so without it a police chase
fired one decision per poll (3 Hz ⇒ 10,800 calls/h ≈ $18/h).

At the measured warm tactical cost of **$0.001714/call** (docs/STATUS.md, 2026-08-25, real API
— $0.011331 cold):

| | tactical calls/hour | $/hour (tactical) |
|---|---|---|
| enforced ceiling (continuous triggers) | 450 | $0.77 |
| governor L1 (floor doubles to 16 s) | 225 | $0.39 |
| timer-only, mood `hyped` (8-15 s) | 313 | $0.54 |
| timer-only, mood `scared` (8-17 s) | 288 | $0.49 |
| timer-only, mood `smug` (10-20 s) | 240 | $0.41 |
| timer-only, mood `chill` (12-25 s) | 195 | $0.33 |
| timer-only, mood `bored` (15-25 s) | 180 | $0.31 |

Mood therefore **does** change calls/hour — an earlier docstring and test claimed it could not,
which was false; narrowing a window moves its midpoint and so moves the rate. What mood cannot
change is the ceiling. Director calls are on top (60-120 s ⇒ 30-60 Sonnet calls/h); their
per-call cost has not been measured yet, so no $/hour is claimed for that tier here. The
governor's $1.50/h target (§7) is enforced independently by `budget.BudgetGovernor` against
real per-call `usage`, not by these projections. `tests/test_windows_readiness.py` asserts
every number in this table.

## Governor L3 — "asleep in the car"

§7 L3 is *"parks somewhere scenic"*. It now does that: `activities.scenic_park_plan` picks the
nearest of `SCENIC_PARK_SPOTS` (the same landmarks the scenic activities use), the harness
posts a `drive_to` and then a `stop` when that task — matched by the id `POST /task` returned,
not by whatever the next snapshot shows — completes, times out after `L3_PARK_TIMEOUT_S`, or is
taken over. On foot, or if the drive cannot be posted, he stops where he is and the feed line
says so. The drive is an ordinary bridge task, so the engine does it and L3 still makes no
model calls. The reflex layer's "keep a wander task alive" branch is now L2-only; at
`level >= 2` it posted a `wander_drive` the tick after the L3 stop, which is why L3 previously
amounted to nothing.

## Cross-package contract test

`tests/test_contract_conformance.py` validates the harness's pydantic models against the
bridge package's **real** serialized output in `bridge/contract-samples/` (produced by its
compiled DLL and its real HTTP router), including replaying those exact bytes over a real
socket through `BridgeClient`. A bridge/harness shape drift fails here rather than on the
server. The samples are read from the bridge path and never copied into
`harness/tests/fixtures/`, which stays reserved for recordings of real game sessions; the
module skips with a clear message when the bridge package is absent.
