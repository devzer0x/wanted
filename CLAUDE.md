# WANTED — working rules for this repo

WANTED is a live show + website where an AI character called **the agent** plays GTA V Story Mode
autonomously, 24/7 when we choose. Deliverables: `bridge/` (C# ScriptHookVDotNet script inside the
game), `harness/` (Python service running the agent's brain via the Claude API), `web/` (Next.js on
Vercel), `infra/` (Supabase), `docs/` (plan, status, contracts, runbook).

## Non-negotiables

1. **No mocks in product code.** No fake commentary, no sample events, no stubbed endpoints, no
   `TODO: replace with real data`, no demo mode that pretends the game is running. Test fixtures are
   allowed only when they are **recordings of real sessions** captured by the harness
   (`harness/tests/fixtures/*.json`), never invented by hand.
2. **Verified means verified.** A feature is done only when it has been exercised against the real
   thing: the real game process for bridge/harness, a real Supabase project for data, a real Vercel
   preview for the web. "Compiles" and "looks right" are not done.
3. **Loop until ready.** Run the plan → build → verify → fix loop. Do not stop at the first working
   version. Do not report success on something you have not run.
4. **Ask only for physical-world things.** The human is needed to: order the server (or hand over
   cloud credentials), buy the game on their Steam account, type Steam and Rockstar logins into the
   server once, provide API keys, approve the production deploy. Batch these into short checklists.
   Never block on a question answerable by reading docs or running a command. Nobody but the human
   ever types their passwords or 2FA codes; they do that in the remote-desktop session.
5. **Story Mode only, no cheats.** The bridge refuses to run if a network session is active. The agent
   gets no god mode, no teleports across the map, no free money, no invincible cars. A human can't
   do those things, so neither can he. (Narrow exception: an "unstick" nudge of a few meters when
   wedged, logged and announced in commentary.)
6. **Do not guess APIs.** ScriptHookVDotNet signatures, native function hashes, Claude model IDs and
   pricing, Supabase and Vercel behavior: verify against the installed version / official docs
   before writing code. Model IDs and prices live in `harness/config/pricing.yaml`, sourced from the
   official docs page, with the URL and date noted.
7. **Brand hygiene.** Product name is WASTED, character is the agent. The word "GTA", "Grand Theft
   Auto", Rockstar names, Rockstar art, and the game's logo font are not used in our name, logo,
   wordmark, or design system. Footer says we're not affiliated with Rockstar Games or Take-Two.
   Site labels the agent as an AI and commentary as AI-generated.
8. **Directory ownership.** Parallel executors never edit the same directory. Ownership is assigned
   in the brief. Interfaces between directories are frozen in `docs/CONTRACTS.md` before parallel
   work starts.
9. **Keep STATUS.md true.** `docs/STATUS.md` is the single source of truth for what works, what's
   verified (and how), what's broken, and current cost per hour.

## Repo layout

```
CLAUDE.md                 this file
.claude/agents/           researcher, executor, verifier, reviewer
docs/                     PLAN.md STATUS.md CONTRACTS.md RESEARCH.md RUNBOOK.md
bridge/                   C# SHVDN script            (owner: bridge executors)
harness/                  Python 3.12 service        (owner: harness executors)
web/                      Next.js app                (owner: web executors)
infra/supabase/           schema.sql, policies.sql, storage buckets (owner: infra executor)
infra/server/             IaC for a cloud VM, only if the human picks that route
scripts/                  server-setup.ps1, run.ps1, watchdog.ps1, deploy-bridge.ps1, smoke tests
                          (owner: Fable / dedicated ops executor only)
```

## Where things run

- Game, bridge, harness, OBS, watchdog: **the rented Windows GPU server only.** This repo may be
  checked out on a dev machine for docs/infra/web work, but nothing game-adjacent can be *verified*
  anywhere except the server with the game running.
- Web: Vercel. Data: Supabase. The harness writes with the service-role key (server only); the
  browser reads with the anon key under read-only RLS.

## How to run tests / verification

- **Bridge:** build with `dotnet build` in `bridge/`; deploy with `scripts/deploy-bridge.ps1`;
  verify with `scripts/bridge-smoke.ps1` **with the game running** — output goes into STATUS.md.
- **Harness:** `cd harness && pytest` (fixtures are real recorded sessions only), then the live
  check defined in docs/PLAN.md for the phase (e.g. 20 min unattended with the game running).
- **Web:** `cd web && npm run test:e2e` — Playwright against the current Vercel preview with real
  Supabase rows, plus a mobile-viewport check.
- **Cost:** every live check reports measured $/hour from `harness/budget.py` into STATUS.md.

## Contracts

`docs/CONTRACTS.md` is frozen per version. Executors treat it as read-only; changes go through the
orchestrator (Fable) and bump the contract version. If code and contract disagree, the contract
wins until formally changed.
