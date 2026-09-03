# wasted

**the agent is an AI agent playing GTA V.**

He sees the game, decides what to do, performs the action, observes what happened, and continues —
without a human controlling the gameplay.

- **Website:** the production domain
- **Contact:** agent@wanted.run

The agent is an AI. Everything he says on stream is AI-generated. He is not good at the game, and this
repository does not claim he is. He gets stuck, he crashes cars, he dies, he misreads situations and
occasionally stands still while the game waits for him. That is the experiment: not a bot that wins,
but an agent that has to cope with a world it only partly understands, live, with no one to bail it
out.

---

## Contents

- [The loop](#the-loop)
- [What the agent actually perceives](#what-the agent-actually-perceives)
- [The three layers](#the-three-layers)
- [What the agent can do](#what-the agent-can-do)
- [What the agent cannot do](#what-the agent-cannot-do)
- [The rules](#the-rules)
- [Architecture](#architecture)
- [Running it](#running-it)
- [Operating cost](#operating-cost)
- [What is verified, and what isn't](#what-is-verified-and-what-isnt)
- [Contributing](#contributing)
- [License and disclaimer](#license-and-disclaimer)

---

## The loop

```
   ┌──────────────────────────────────────────────────────────────┐
   │                                                              │
   │   game world (GTA V, Story Mode, running on a Windows GPU    │
   │   machine with a real copy of the game)                      │
   │                                                              │
   └───────────────┬──────────────────────────────────▲───────────┘
                   │                                  │
       in-game script reads state          in-game script executes
       (ScriptHookVDotNet, C#)             the chosen task
                   │                                  │
                   ▼                                  │
   ┌──────────────────────────────────────────────────┴───────────┐
   │  BRIDGE  —  local HTTP API on 127.0.0.1:7777                 │
   │  GET /state   structured world snapshot                      │
   │  POST /task   one action from a fixed vocabulary             │
   └───────────────┬──────────────────────────────────▲───────────┘
                   │  polled at 2–4 Hz                │
                   ▼                                  │
   ┌──────────────────────────────────────────────────┴───────────┐
   │  HARNESS  (Python)                                           │
   │                                                              │
   │   perception ──▶ situation + retrieved knowledge             │
   │                        │                                     │
   │                        ▼                                     │
   │              ┌─────────────────────┐                         │
   │              │  reflex   (no model) │  survival, instant      │
   │              │  tactical (Haiku 4.5)│  what to do next        │
   │              │  director (Sonnet 5) │  what to do with the hour│
   │              └──────────┬──────────┘                         │
   │                         │ decision (structured JSON)          │
   │                         ▼                                     │
   │              action selection ──▶ POST /task                  │
   │                         │                                     │
   │                         └──▶ commentary, events, stats        │
   └───────────────┬──────────────────────────────────────────────┘
                   │
                   ▼
   ┌──────────────────────────┐        ┌──────────────────────────┐
   │  Supabase (Postgres +    │───────▶│  production (Next.js on  │
   │  Realtime)               │        │  Vercel) + OBS overlay   │
   └──────────────────────────┘        └──────────────────────────┘
```

Then it repeats. New state, new decision, forever.

---

## What the agent actually perceives

This is the part most "AI plays a game" projects describe loosely, so here it is precisely.

**the agent's primary sense is structured game state, not pixels.** A C# script running inside the game
(ScriptHookVDotNet) reads the engine directly and serves a JSON snapshot over a loopback HTTP API,
polled at 2–4 Hz (default 3 Hz). That snapshot carries, among other fields:

| Group | Fields |
|---|---|
| Player | `health`, `max_health`, `armor`, `wanted`, `dead`, `arrested`, `in_vehicle`, `in_water`, `cash` |
| Position | `pos {x,y,z}`, `heading`, `speed`, `street`, `zone`, `stopped_for_s`, `upside_down` |
| Vehicle | `model`, `class`, `display_name`, `color`, `driver` |
| World | `clock`, `weather`, `nearby` peds and vehicles (handle, kind, relationship, distance) |
| Mission | `mission.active`, `cutscene_active`, `random_event_active`, `objective_blip`, `route_blips`, `starts[]` |
| Engine | `tick`, `timescale`, `edition`, `control_enabled` |

**Vision is used for exactly two things**, both rare and both cheap: reading the mission *title* off
the screen when a mission begins, and reading the `MISSION PASSED` / `MISSION FAILED` verdict (plus
the game's own reason line, e.g. *"Franklin lost Lamar"*) when one ends. Neither fact exists anywhere
in the state API, so the screen the game itself draws is the only honest source. Screenshots are
downscaled to a 768-px long edge before being sent; raw 1080p frames are never sent to a model.

So: the agent is not staring at the screen and interpreting it frame by frame. He reads instruments. Some
of his worst failures come exactly from that — the state API tells him a blip exists but not what the
mission wants from him, and he has to infer the rest.

---

## The three layers

Decisions are made by three layers that disagree on purpose, with a fixed precedence. The tactical
layer runs on one of two models depending on whether a story mission is active.

| Layer | Model | Runs | Job |
|---|---|---|---|
| **Reflex** | none — plain Python | every tick | Survival and stuck-detection. No API call, no latency, no cost. Overrides the others when something is actively going wrong. |
| **Tactical** | `claude-haiku-4-5` | every 8–25 s | "What do I do right now?" Picks one action from the vocabulary and writes the line of commentary that goes with it. |
| **Tactical (mission)** | `claude-sonnet-5` | same cadence, only while `mission.active` | The *same* tactical decision on the same prompt, but on the smarter model. Story missions punish a wrong move in a way free roam does not, so the tier swaps up when one is running — and swaps back when it ends. This is the single biggest driver of cost variance; see [Operating cost](#operating-cost). |
| **Director** | `claude-sonnet-5` | occasionally | "What am I doing with this hour?" Sets the mission or free-roam goal the tactical layer works inside. Text, plus the occasional screenshot. |

When the director fails, it is retried once, and then the reflex layer keeps control and the failure
is logged rather than hidden. When the budget governor escalates, the layers are shed from the top
down (see [Operating cost](#operating-cost)).

Decisions come back as **structured output** against a JSON schema, not free text that gets parsed
hopefully. A decision that does not validate is a logged failure, not a guess.

The agent also has a **retrieval-backed knowledge base** — 632 curated items across 13 domain files
(driving, combat, police, vehicles, aircraft, HUD icons, map markers, random events, NPCs, failure
recovery, world common sense, and more), plus mission data: 72 mission state machines in
`mission_states.json`. Every count here is reproducible from the files in
`harness/wasted_harness/brain/knowledge/`. Retrieval is situational: four wanted stars pulls police
knowledge, being in a helicopter pulls aircraft knowledge, a cutscene pulls only locked-control
items. Selection and rendering measured at **0.28 ms per decision**, so against an 8–25 s cadence it
is effectively free.

---

## What the agent can do

The vocabulary is **20 actions and nothing else**. Twelve are tasks handed to the game engine, which
may take seconds to minutes; eight are direct key presses that are near-instant.

**Engine tasks:** `drive_to` · `walk_to` · `enter_nearest_vehicle` · `exit_vehicle` · `wander_drive` ·
`flee_police` · `combat_hated_targets_around` · `fight_ped` · `seek_cover` · `follow_entity` ·
`set_waypoint` · `stop`

**Manual primitives:** `look_around` · `brake_tap` · `swerve` · `reverse_out` · `press_prompt_key` ·
`wait` · `radio` · `horn`

If it is not on that list, the agent cannot do it, and the prompt tells him in those words — because on a
live stream, claiming to have done something the viewer can see did not happen is worse than failing.

---

## What the agent cannot do

Stated plainly, because it shapes everything about how he plays:

- **He cannot aim, and he cannot choose a target.** `combat_hated_targets_around` hands the engine a
  radius and *the engine* picks who to shoot. He cannot shoot one named person, a tyre, a lock, an
  alarm, or one man in a crowd.
- **He has no special ability.** No slow motion, no driving focus, no rage.
- **He cannot pick a weapon**, reload, or open the weapon wheel. He uses whatever is in his hands.
- **He cannot** crouch, jump, climb, swim on command, deploy a parachute, punch, switch character,
  use the phone, or open a menu.
- **`press_prompt_key` is not a keyboard.** It presses whichever contextual prompt the game is
  currently offering. He does not choose which key.

His whole leverage is **position, timing and vehicle** — where he is, when he moves, what he is
driving, and whether he is behind cover when the shooting starts.

---

## The rules

These are enforced in code, not just promised:

- **No human is playing.** Nobody is at the controls, and nobody corrects his decisions mid-session.
- **Story Mode only.** If the bridge sees `NETWORK_IS_SESSION_STARTED` or
  `NETWORK_IS_GAME_IN_PROGRESS`, it latches disabled and every endpoint returns
  `503 online_session_active` until the game is restarted. It cannot un-latch itself mid-session.
- **No cheats.** No god mode. No teleporting across the map. No cheat money. No invincible cars. A
  human player cannot do those things, so neither can the agent.
- **The one exception**, stated so it is not a secret: when he is physically wedged in world
  geometry, an "unstick" nudge of **≤ 3 m** is allowed. The bridge enforces the preconditions
  itself — speed ≈ 0 for more than 20 s *and* a drive task already running, otherwise it returns
  `409 unstick_conditions_not_met`. Every use is logged and announced in commentary.
- **Mistakes and deaths are real** and are counted. The site publishes deaths, arrests and failed
  missions alongside the successes.
- **No mocks in product code.** No fake commentary, no sample events, no demo mode that pretends the
  game is running. When the game is down, the site says the game is down. Test fixtures are
  recordings of real sessions, never hand-invented data.

---

## Architecture

```
bridge/      C# ScriptHookVDotNet script that runs inside the game.
             Serves GET /state and accepts POST /task on 127.0.0.1:7777.
harness/     Python 3.12 service: perception loop, the three brain tiers,
             knowledge retrieval, commentary, budget governor, OBS overlay.
web/         Next.js site (production), deployed on Vercel.
infra/       Supabase schema, RLS policies, storage buckets.
scripts/     PowerShell ops suite for the Windows game machine.
docs/        PLAN · STATUS · CONTRACTS · RUNBOOK · RESEARCH.
```

**`docs/CONTRACTS.md` is the frozen interface** between those directories — the state schema, the
decision schema, event types, the data model, and the budget levels. If code and contract disagree,
the contract wins until it is formally revised. `docs/STATUS.md` is the honesty ledger: what works,
how it was verified, what is broken, and the measured cost per hour.

**Data flow out:** the harness writes sessions, events, decisions, stats and clips to Supabase with a
service-role key (server-side only). The browser reads with a publishable key under read-only RLS and
subscribes to Realtime for live updates. The harness also serves a local transparent-background
overlay page that OBS embeds as a browser source.

**Where it runs:** the game, bridge, harness, OBS and watchdog all run on one Windows GPU machine.
The web layer runs on Vercel and the database on Supabase. Nothing game-related can be *verified*
anywhere except that machine with the game actually running.

---

## Running it

You need a legitimately purchased copy of GTA V. This project does not distribute the game, any
Rockstar asset, or any means of obtaining either.

**Prerequisites**

- Windows machine with a GPU, and GTA V (Legacy build) installed
- ScriptHookV + ScriptHookVDotNet v3.7.0-nightly.189 (pinned; see `bridge/README.md`)
- .NET Framework 4.8 (in-game script) and .NET SDK 8 (to build)
- Python 3.12
- Node.js (for the site)
- An Anthropic API key; a Supabase project; optionally OBS 32.x for streaming

**Bridge** (in the game)

```bash
cd bridge
dotnet build
# then deploy the built script into the game's scripts folder:
pwsh -File ../scripts/deploy-bridge.ps1
pwsh -File ../scripts/bridge-smoke.ps1     # requires the game to be running
```

**Harness** (the brain)

```bash
cd harness
cp .env.example .env          # fill in ANTHROPIC_API_KEY, SUPABASE_*, OBS_WS_PASSWORD
pip install -e .
python -m wasted_harness.main --check     # verifies config, models and prompt caching
pytest                                     # unit tests; fixtures are real recorded sessions
python -m wasted_harness.main              # run it
```

**Web**

```bash
cd web
cp .env.example .env.local    # NEXT_PUBLIC_SUPABASE_* etc.
npm install
npm run dev
npm run test:e2e              # Playwright against a deployment with real rows
```

Every secret is read from the environment. `.env.example` files document the shape; no real key is
ever committed. The harness **fails loudly** when a required value is absent — it never pretends a
dependency is up.

---

## Operating cost

Running an agent 24/7 is not free, and the number is not hidden. Costs split into a **fixed**
infrastructure floor and a **variable** inference cost that scales with how often the agent thinks —
and, more than anything else, with *what he is doing at the time*.

### What drives it

```
inference cost/hour  =  (tactical calls/hour × cost per tactical call)
                      + (director  calls/hour × cost per director  call)
```

Two things make that more interesting than it looks:

1. **Cost per call is dominated by input, not output.** The system prompt is large and the decision
   is small. Prompt caching is therefore load-bearing: a cache read is billed at 0.1× the input
   price, and the 8–25 s cadence keeps the 5-minute cache warm, every read refreshing the TTL free.
2. **The tactical tier changes model mid-mission.** While `mission.active` is true, tactical
   decisions run on Sonnet 5 instead of Haiku 4.5 — same prompt, same cadence, ~4× the price per
   call. A mission hour and a free-roam hour are genuinely different products.

### Model prices

From `harness/config/pricing.yaml`, which cites the official pricing page and its retrieval date and
is re-verified at harness startup with a 1-token call per model. USD per million tokens:

| Tier | Model | Input | Output | Cache read | Cache write (5m) |
|---|---|---|---|---|---|
| tactical | `claude-haiku-4-5` | $1.00 | $5.00 | $0.10 | $1.25 |
| tactical (mission) | `claude-sonnet-5` | $2.00 | $10.00 | $0.20 | $2.50 |
| director | `claude-sonnet-5` | $2.00 | $10.00 | $0.20 | $2.50 |

### Measured inputs

| Quantity | Value | Status |
|---|---|---|
| Tactical static prefix | **14,484 tokens** | measured (`--prompt-audit`) |
| Director static prefix | **15,144 tokens** | measured (`--prompt-audit`) |
| Warm tactical call (Haiku, cache hit) | **$0.0026** | measured |
| Cold tactical call (cache write) | $0.0113 | measured |
| Knowledge-retrieval block | ~450 uncached input tokens/call | measured |

The prompt has grown substantially as the agent was taught the game: the tactical prefix went from
7,617 to **14,484 tokens** (1.9×), and a warm tactical call from $0.001714 to **$0.0026** (+52%).

### Derived per-call and per-hour cost

Sonnet-tier calls are built from first principles rather than measured directly, so they are
estimates. **Assumptions, stated:** ~1,000 uncached dynamic input tokens per call (knowledge block +
state context), and ~600 output tokens including Sonnet's thinking block, which is billed as output.

| Call type | Cost/call | Basis |
|---|---|---|
| Tactical, free roam (Haiku) | **$0.0026** | measured |
| Tactical, mid-mission (Sonnet) | ≈ $0.0109 | derived — **4.2× a free-roam call** |
| Director (Sonnet) | ≈ $0.0110 | derived |

At 240 tactical calls/hour (one every 15 s, the midpoint of the 8–25 s cadence) and ~40 director
calls/hour:

| | Free-roam hour | Mission hour | Governor ceiling |
|---|---|---|---|
| tactical | $0.62 | $2.62 | — |
| director | $0.44 | $0.44 | — |
| **total/hour** | **≈ $1.07** | **≈ $3.06** | **$1.50** |
| per minute | ≈ $0.018 | ≈ $0.051 | $0.025 |
| per 24 hours | ≈ $25.60 | ≈ $73.40 | $36.00 |
| per 30 days, continuous | ≈ **$770** | ≈ **$2,200** | **$1,080** |

### The budget governor — which now actually bites

A governor tracks spend against an hourly cap (`WASTED_HOURLY_CAP_USD`, default **$1.50**) and sheds
capability from the top down. Every level change is announced on stream as an event and a feed line,
so the audience is told when the agent is being throttled:

| Level | Engages at | Behaviour |
|---|---|---|
| **L0** | — | normal cadence |
| **L1** | 70% of cap | slower tactical timers, no flavour shots |
| **L2** | 90% of cap | director only; the reflex layer drives |
| **L3** | 100% of cap | "asleep in the car" — parks somewhere scenic, commentary paused with an honest on-screen note, resumes when the hourly window resets |

**This is the headline change since the mission tier was introduced.** A free-roam hour (≈$1.07)
fits under the cap, only touching L1 right at the hour boundary. A sustained mission hour (≈$3.06)
does not:

| | L1 (70%, $1.05) | L2 (90%, $1.35) | L3 (100%, $1.50) |
|---|---|---|---|
| free-roam hour | ~59 min | not reached | not reached |
| mission hour | **~21 min** | ~26 min | **~29 min** |

So roughly half an hour of continuous story-mission play exhausts the default hourly budget and
parks him. In practice the cap, not the model, decides how much mission play a 24/7 stream can
afford, and the honest ceiling for continuous operation is the **$1,080/month** governor line rather
than either uncapped figure above.

Raising `WASTED_HOURLY_CAP_USD` raises the ceiling proportionally. Nothing in the design assumes the
default.

### Fixed infrastructure

Approximate monthly figures, from the hardware research in `docs/research/brief-winserver.json`
(list prices at time of research, ex VAT, and they will drift):

| Item | Approx. cost | Note |
|---|---|---|
| Windows GPU machine (Hetzner GEX44: RTX 4000 SFF Ada 20 GB, i5-13500, 64 GB) | ≈ €184/mo | plus ≈ €79 one-time setup |
| Windows Server licence | ≈ €28/mo | SPLA, priced by core count |
| HDMI dummy plug (headless capture needs a display) | ≈ €1.10/mo | required for GPU capture |
| Supabase | free → Pro tier | see scaling note below |
| Vercel | free → Pro tier | static shell + dynamic snapshot |
| GTA V | one-time purchase | not distributed by this project |

**≈ €215/month fixed**, before any inference.

### The scaling cliff worth knowing about

Supabase Realtime bills **per recipient**, not per message. At roughly 300 concurrent viewers
receiving ~1 message/second, that is on the order of **$1,900/month** — comparable to the entire
model bill even at the mission-hour rate, and an order of magnitude above the fixed infrastructure.
It is fine at launch scale and becomes the dominant cost long before the model bill does. The
migration path (batching updates into periodic digests instead of fanning out every event) is
specified in `docs/CONTRACTS.md` §5 rather than left to be discovered in a bill.

### Assumptions and caveats

- 240 tactical calls/hour ≈ one every 15 s, the midpoint of the 8–25 s cadence.
- The ~40 director calls/hour figure is a design number and remains the least certain input.
- Sonnet-tier per-call costs are derived, not measured; the ~600-token output assumption is the
  largest single source of error, because Sonnet's thinking block is billed as output and scales
  with the `max_tokens` it is offered.
- The free-roam/mission split assumes an hour is entirely one or the other. Real hours are mixed,
  so a real bill lands between $1.07 and $3.06 — weighted by how much of the hour was on-mission.
- Cache-warm pricing assumes continuous operation; a cold start costs ~4.3× a warm tactical call.
- Prices are USD/EUR list prices at their stated retrieval dates and are not contractual.
- Streaming bandwidth is not itemised; it is included in the machine's allowance.

## What is verified, and what isn't

`docs/STATUS.md` is maintained as the single source of truth, and a feature is only called done when
it has been exercised against the real thing — the real game process, a real Supabase project, a real
deployment. "It compiles" and "it looks right" do not count.

**Verified against the real game:** the bridge state/task API, the knowledge base loading (632 items
across 13 domain files, plus 72 mission state machines), OBS and Supabase connectivity, the day planner, and the
follow-the-objective fix that resolved a real mission failure within 20 seconds of a live session.

**Verified against the real Claude API:** both model IDs, prompt caching (proven via
`cache_read_input_tokens`), structured-output decision parsing, and the per-call costs quoted above.

**Not yet verified:** long unattended behavioural samples, and — stated plainly — whether the agent plays
*well* as opposed to merely playing *at all*. Do not read "he is running" as "he is good."

A worked example of the difference: the director tier was silently dead for a period because Sonnet's
thinking block is billed against the same `max_tokens` budget as the response, so at a shared 500-token
cap the decision JSON was truncated mid-object. The logged error claimed the model returned nothing
parseable; the real cause was a truncated response. Both the cap and the misleading error message were
fixed, with regression tests. Failures like that are in `STATUS.md` on purpose.

---

## Contributing

Issues and pull requests are welcome. Two things are worth knowing before you open one:

1. **No mocks in product code.** No fake commentary, no sample events, no stubbed endpoints, no demo
   mode that pretends the game is running. Test fixtures must be recordings of real sessions.
2. **`docs/CONTRACTS.md` is frozen per version.** If your change needs a different interface between
   `bridge/`, `harness/`, `web/` and `infra/`, the contract change is part of the discussion, not a
   silent edit.

`CLAUDE.md` documents the full working rules for the repository.

Questions: agent@wanted.run

---

## License and disclaimer

Licensed under the [MIT License](LICENSE).

This project is **not affiliated with, endorsed by, or associated with Rockstar Games or Take-Two
Interactive**. All trademarks are the property of their respective owners. This repository contains no
game assets and no means of obtaining the game; running it requires your own legitimately purchased
copy. The agent is an AI agent, and all commentary he produces is AI-generated.
