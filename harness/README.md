# harness — the agent's brain and body (Python 3.12+)

Owner: harness executors. Package: `wasted_harness`. Interfaces: docs/CONTRACTS.md v1.1.

## Layout

- `wasted_harness/bridge_client.py` — typed client for the bridge API (§1)
- `wasted_harness/perception.py` — poll deltas, dxcam capture (win32-only), 768px JPEG, objective dHash
- `wasted_harness/brain/` — DecisionModel (§2), tactical (Haiku) + director (Sonnet) via
  `messages.parse`, cached static prefixes, memory files
- `wasted_harness/behavior/` — humanizer, activity catalog, mission skeleton, recovery
- `wasted_harness/commentary.py` — feed lines + persisted death/busted rotation (no repeat in 10)
- `wasted_harness/events.py` — batched service-role Supabase writes + offline queue
- `wasted_harness/budget.py` — pricing.yaml costs + L0-L3 governor (§7)
- `wasted_harness/obs.py` — replay-buffer clips synced on ReplayBufferSaved
- `wasted_harness/overlay/` — OBS browser-source overlay + SSE (§6)
- `wasted_harness/primitives.py` — SendInput manual-control primitives (win32-only)
- `wasted_harness/main.py` — supervised loop; `--check` prerequisite probe
- `wasted_harness/tools/post_event.py` — watchdog CLI (queues offline when Supabase is absent)
- `config/pricing.yaml` — model IDs + prices, sourced + dated (the runtime source of truth)

## Setup

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'            # on the Windows game server: '.[dev,windows]'
cp .env.example .env               # fill in keys on the server
pytest                             # unit tests (fixtures arrive from real sessions only)
python -m wasted_harness.main --check   # honest prerequisite report, exits nonzero if missing
python -m wasted_harness.main           # the real loop (needs bridge + ANTHROPIC_API_KEY)
```

## Verification status (honest)

- **Verified locally**: unit tests (budget math, commentary rotation, humanizer bounds,
  activity selection, schema enforcement, offline queue vs a real refused connection,
  bridge client vs a dead port, overlay via its real ASGI app); `--check` failure paths.
- **Authored, awaiting the game server** (Phase 2 live check): bridge task execution,
  dxcam capture, SendInput primitives, OBS replay pipeline, Supabase flush against a real
  project, model calls + prompt-cache hit telemetry, measured $/hour.
- Mission completion logic is a Phase 4 deliverable; `behavior/missions.py` is the
  flag-driven state machine only and says so.
