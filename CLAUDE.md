# WANTED — working rules for this repo

**WANTED** is a live show + website where an AI agent plays GTA V Story Mode autonomously, 24/7
when we choose, and viewers predict what it will do next. Correct predictions earn TTWO from a
treasury we fund; there is no wager and nothing to lose by being wrong. Deliverables: `bridge/`
(C# ScriptHookVDotNet script inside the game), `harness/` (Python service running the agent's
brain via the Claude API, and generating predictions from live telemetry), `web/` (Next.js on
Vercel, site + prediction API), `infra/` (Supabase), `docs/` (plan, status, contracts, runbook).

Read `docs/CONTRACTS.md` for the game-side contracts and `docs/CONTRACTS-PREDICTIONS.md` for the
prediction layer. Rule 7 below explains which names are public and which are internal — that
distinction matters more here than it looks.

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
5. **Story Mode only. Cheats allowed, on purpose, since 2026-09-04.** The bridge still refuses to
   run if a network session is active, and the agent still never plays anywhere but single-player
   Story Mode. That part is not negotiable and never will be.

   What CHANGED: this rule used to end "no cheats — a human can't do those things, so neither can
   he." The operator lifted that on 2026-09-04, for the show rather than for the player: comedy and
   shareability now outrank the fair-play constraint. So god mode, spawned vehicles, gravity
   and similar effects are now legitimate material for deliberately absurd bits.

   The guardrails that survive the change, because they are what keep it honest rather than what
   kept it hard:
   * **Say so.** Anything cheat-driven is announced in commentary and carried in the event payload,
     the same way the unstick nudge always has been. The audience is never told he did something
     unaided when it did not. The "labelled as an AI" honesty in rule 8 applies to how it plays, too.
   * **No typed cheat codes.** The harness has no text entry and no menu navigation, so effects
     come from the bridge calling natives. Rule 6 still applies in full: do not guess a native or
     an SHVDN signature, verify against the installed version.
   * **Deliberate, not ambient.** A cheat belongs to a named bit with a `done_when`, not to his
     baseline. A permanently invincible agent is not a character, and there is no jeopardy left to
     be funny about.
   * The old "unstick nudge of a few metres when wedged" exception is unchanged and still logged.
6. **Do not guess APIs.** ScriptHookVDotNet signatures, native function hashes, Claude model IDs and
   pricing, Supabase and Vercel behavior: verify against the installed version / official docs
   before writing code. Model IDs and prices live in `harness/config/pricing.yaml`, sourced from the
   official docs page, with the URL and date noted.
7. **Names: what is public, what is internal.** The public product is **WANTED** — an AI playing
   GTA live, that viewers predict against, with correct predictions earning TTWO from a treasury
   we fund. Ticker `$WANTED`, pair `$WANTED / TTWO`, network Robinhood Chain (chain id 4663). The
   agent is referred to publicly as WANTED, or "the agent".

   **Internal identifiers are implementation details and are deliberately not renamed.** The
   `wasted_harness` Python package, the `WASTED_*` environment variables, every database table and
   column, the bridge protocol and its contract in `docs/CONTRACTS.md` all keep the names they
   have. They are load-bearing against a running game, live production data and rows already
   written. Renaming them buys nothing a viewer can see and risks everything the show runs on. Do
   not "tidy" them, and do not annotate them as belonging to anything other than this system.

8. **Brand hygiene.** The word "GTA", "Grand Theft Auto", Rockstar names, Rockstar art, and the
   game's logo font are not used in our name, logo, wordmark, or design system. Naming the game in
   body copy is fine; the constraint is on our own identity. Footer says we're not affiliated with
   Rockstar Games or Take-Two. The site labels the agent as an AI and commentary as AI-generated.

   The reward asset needs its own line, because it points at a real company: TTWO is a tokenized
   debt security issued by Robinhood Assets (Jersey) Limited that tracks the Take-Two share price.
   It is not issued by Take-Two, carries no shareholder rights, and using it implies no
   relationship with either company — say so plainly wherever it appears, and never imply an
   endorsement. It is also restricted or prohibited in several jurisdictions, which is why reward
   eligibility is enforced server-side (`docs/CONTRACTS-PREDICTIONS.md` §5) and never in the UI.
9. **Directory ownership.** Parallel executors never edit the same directory. Ownership is assigned
   in the brief. Interfaces between directories are frozen in `docs/CONTRACTS.md` before parallel
   work starts.
10. **Keep STATUS.md true.** `docs/STATUS.md` is the single source of truth for what works, what's
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
