# WANTED — PLAN

Owner: Fable (orchestrator). Executors work only from written work-package briefs derived from this
plan. Phases run strictly in order; a phase starts only when the previous phase's definition of done
is met, verified, and committed.

## Product pillars (every feature serves one)

- **Readable mind** — people read the agent's decisions like sports commentary; the site exists for this.
- **Human, not robot** — the agent plays like a person with a personality, not a pathfinding script.
- **It actually works** — missions complete, deaths are handled, the stream doesn't die at 3am.
- **Cheap to run** — the brain is affordable at 24/7 cadence (target < ~$1.50/streamed hour).
- **Nothing fake** — no mock data, no placeholder feed, no simulated success.

## Phase map

| Phase | Name | Definition of done (summary) |
|---|---|---|
| 0 | Setup & research | CLAUDE.md, agents, docs skeleton, CONTRACTS.md v1 frozen, research synthesized with sources, human checklist delivered |
| 0a | Server ready | `scripts/server-setup.ps1` run; game launches in console session 1280×720 GPU-accelerated, survives disconnect + reboot; SHV loads; OBS captures A/V; 5-min test stream reaches channel; evidence in STATUS.md |
| 1 | Bridge | All endpoints live, online-session safety check, deploy script, smoke test passing with the game running; smoke output in STATUS.md; contract matches |
| 2 | Harness core | Bridge client, perception, tactical+director with decision schema, prompt caching, budget accounting, events → Supabase, local overlay; 20-min live check; measured $/hour reported |
| 3 | Human-like + activities | Humanizer, mood, idle behaviors, breaks, core activity catalog, recovery; 30-min live check with no robotic-behavior findings; every core activity observed completing once |
| 4 | Missions | Prologue + next three story missions, each 3/3 unattended attempts; `missions` rows; fail-handling exercised deliberately at least once |
| 5 | Web | All pages, Realtime, RLS, Vercel preview; Playwright green on preview; Realtime latency measured; mobile check |
| 6 | Stream & clips | OBS overlay, replay-buffer clips, clip pages; a real death produces a real clip on the site within 60 s |
| 7 | Cost tuning & soak | Cost target hit; 2-hour unattended soak: zero crashes, ≥1 mission attempted, deaths fully handled, governor ≤ L1, no commentary line repeated within 10 entries |
| 8 | Production & runbook | Human approves; production deploy; RUNBOOK covers start/stop/restart, game relaunch, key rotation, 3am playbook |
| 9 | (Optional) Viewer missions | Vote/submit next goal via edge function into the director — real, or not at all |

## Verification protocol (all phases)

- **Bridge:** `bridge-smoke.ps1` output pasted into STATUS.md; every endpoint exercised with the
  game running; task completion observed (drive_to arrives within radius; enter_nearest_vehicle
  yields in_vehicle=true).
- **Harness:** pytest against fixtures recorded from real sessions (`harness/tests/fixtures/`),
  plus the phase's live check: unattended run with the game, log shows decisions/commentary/
  activities, no unhandled exceptions, events visible in Supabase.
- **Web:** Playwright e2e against the Vercel preview with a real session's rows; mobile viewport;
  a Realtime insert observed arriving in the UI within 2 s.
- **Cost:** measured $/hour from `budget.py` over the live check, compared to target, tuned before
  moving on.
- **Sign-off:** a verifier signs off each work package; the reviewer signs off each phase; STATUS.md
  updated with evidence before the phase is marked done.

## Orchestration rules

- Research fan-out only for questions not already known with certainty; sourced briefs land in
  docs/RESEARCH.md.
- Execution fan-out uses non-overlapping directory ownership (CLAUDE.md §8). Briefs are
  self-contained: goal, owned directory, relevant contract text pasted in, fixture paths,
  acceptance checks, out-of-scope list.
- Executors never guess on architecture, data shape, or behavior design — their questions return in
  reports; Fable answers and resumes them.
- Two packages touching the same files are one package.

---

## Phase 0 spec — Setup & research (current)

**Scope:** repo skeleton, working rules, agent definitions, research fan-out, contracts v1,
human checklist. No product code.

**Environment note:** this session runs on the human's macOS laptop. Phase 0 and all
Supabase/web/docs work are server-independent. Everything game-adjacent (0a, 1–4, 6, 7) requires
the Windows GPU server, which the human orders via the Phase 0 checklist. When the server is up,
this repo is cloned there and Claude Code runs on the server for those phases (RUNBOOK.md §1).

**Work packages:**
1. Repo skeleton: CLAUDE.md (non-negotiables verbatim), `.claude/agents/` × 4, docs skeleton,
   `.gitignore`, git init + initial commit. *(Fable, done inline — no fan-out needed.)*
2. Research fan-out (parallel, one question each): SHVDN builds for Legacy vs Enhanced + Steam app
   IDs + BattlEye; native functions for tasks/blips/mission flag/cutscene/timescale/control/network
   detection; obs-websocket v5 + Python client + replay buffer; Supabase Realtime + RLS + service
   role patterns; Vercel/Next.js constraints (stream embeds, realtime in browser, OG images);
   Windows GPU server ops (console session, display/audio, auto-logon, dxcam, ViGEmBus state,
   Hetzner GEX44); Steam/Rockstar install & launch automation; current Claude model IDs/pricing/
   tool-use/caching from official docs.
3. Synthesis: docs/RESEARCH.md with per-question briefs + sources; unresolved items marked.
4. Contracts v1 frozen in docs/CONTRACTS.md: bridge JSON (every endpoint), task types + params,
   decision schema, event types + payloads, Supabase table shapes, overlay interface, governor
   levels, directory ownership.
5. Human checklist delivered in chat (server, game purchase, keys, stream channel, one-time logins).

**Acceptance checks:**
- All five docs exist and are non-empty; CONTRACTS.md carries a version + freeze date.
- RESEARCH.md cites a source (URL) for every API-shaped claim; unconfirmed items are listed, not
  papered over.
- git history has the skeleton commit and the Phase 0 completion commit.
- The human checklist covers everything Phases 0a–8 will need from a human, batched once.

**Definition of done:** docs exist, contracts frozen, human checklist delivered. (Met when STATUS.md
says so with evidence.)

---

## Phase 0a spec — Server ready (next; blocked on human checklist)

Research-informed scope (decisions D1, D9, D10 in docs/RESEARCH.md). ⚠ GEX44 lead time is
currently **"several weeks"** — the human orders on day one; if they also provide cloud
credentials, an interim AWS G4dn/G5 Windows VM (provisioned via `infra/server/`) unblocks Phases
0a–2 development sooner.

`scripts/server-setup.ps1` (idempotent, logged) covers: NVIDIA workstation driver; display via the
Hetzner **HDMI emulator on the RTX card** (cloud VM route: virtual display driver instead);
VB-CABLE **after** enabling Windows Audio + Endpoint Builder services (+ reboot; OBS captures the
CABLE device explicitly, never "Default"); Sysinternals Autologon (LSA secret); NoLockScreen=1,
InactivityTimeoutSecs=0, screensaver policy off; Windows Update notify-only (AUOptions 2/7 — the
no-reboot-with-users policy is unreliable for disconnected sessions); high-performance power plan +
monitor-timeout 0; startup task chain (Steam `-silent` → `steam.exe -applaunch 271590` → poll for
GTA5.exe → harness → OBS) in the **console session**; OpenSSH Server (ships with Server 2025) with
key auth + PowerShell default shell; firewall inbound RDP/SSH from the human's IP only; toolchain
(Git, Python 3.12+, Node LTS, .NET SDK + Framework 4.8 targeting pack, OBS with websocket,
Claude Code). **No Parsec** (commercial-license risk; HDMI emulator already provides the display) —
RDP for setup with the `tscon /dest:console` detach procedure scripted as `scripts/detach-rdp.ps1`.
**Day-one test:** vgamepad/ViGEmBus on Server 2025 (expected to fail with Code 28 → SendInput
keyboard/mouse is the plan of record, CONTRACTS §2).

Game install: human buys Enhanced (3240220), we install **Legacy (271590)** via
`steam://install/271590`; human does the two one-time logins (Steam incl. Steam Guard; Rockstar
sign-in + link); first launch auto-detects graphics, then we pre-seed
`Documents\Rockstar Games\GTA V\settings.xml` to 1280×720 windowed-borderless and set
`-nobattleye` (+ `-scofflineonly` best-effort) in commandline.txt / launcher settings. SHV
(current build) + SHVDN nightly installed; verified with the bundled Native Trainer; SkipIntro ASI
allowed.

**DoD (from master brief §0):** game launches into Story Mode in the console session at 1280×720,
GPU-accelerated (confirmed with vendor tool), keeps running after disconnect, comes back unattended
after a reboot, Script Hook V loads, OBS captures video and audio, and a 5-minute test stream
reaches the channel. Evidence in STATUS.md. While waiting on provisioning: Supabase schema and web
scaffolding only — nothing that needs the game.

---

## Wait-work spec — Mac-buildable build-out (authorized by the human 2026-08-25: "build
everything autonomously till everything is done")

**Scope:** author and locally verify everything that does not require the game server or missing
credentials. Verification honesty rule: STATUS.md distinguishes **verified-locally** (real
commands ran here) from **authored-awaiting-server** (compiles/tests locally, game-dependent
behavior unproven) — nothing is called done beyond its evidence. Phase 1/2/5 DoDs remain open
until their real-environment checks run.

**Local verification environment (discovered 2026-08-25):** macOS arm64, node 20.19.4, python
3.13.3 (harness targets ≥3.12), Docker 29.5.2 (daemon up), dotnet SDK 8 in `~/.dotnet`
(user-local), pwsh 7.4.6 in `~/.powershell` (user-local), local Supabase stack via
`npx supabase` under `infra/` — real Postgres + PostgREST + Realtime + Storage for verification
until cloud keys arrive. No ANTHROPIC_API_KEY, no Vercel token: brain calls and preview deploys
stay unverified, and code must fail loudly (never fake success) when they're absent.

### Work packages (directory ownership per CLAUDE.md §8; executors never commit)

**WP-I — infra/ (owner: Fable, done inline).** Migrations
`infra/supabase/migrations/20260825120000_schema.sql` + `..._policies.sql` implementing CONTRACTS
§5 exactly (identity PKs, text/timestamptz/numeric, check-constraint enums, `(session_id, ts)`
indexes, realtime publication on decisions/events/stats, buckets clips+shots, RLS public-SELECT +
privilege revokes). Acceptance: migrations apply clean on the local stack; as `anon`: SELECT
succeeds on all 7 tables, INSERT/UPDATE/DELETE all fail; service role writes succeed; publication
lists exactly decisions/events/stats; buckets exist and are public.

**WP-B — bridge/ (executor).** Full SHVDN script per CONTRACTS §1. Fetch the latest official
SHVDN nightly release into `bridge/lib/` (gitignored; record exact version in bridge/README.md and
the report). SDK-style csproj, TargetFramework net48, `Microsoft.NETFramework.ReferenceAssemblies`,
Newtonsoft.Json via NuGet, reference `lib/ScriptHookVDotNet3.dll`; assembly name **WastedBridge**.
Architecture (frozen): one `GTA.Script` subclass; `Tick` refreshes a JSON snapshot + drains a
command queue; `HttpListener` on a background thread serves 127.0.0.1:7777 from the cached
snapshot only — natives never called off the game thread. Online guard: `NETWORK_IS_SESSION_STARTED`
(+`NETWORK_IS_GAME_IN_PROGRESS`) checked at startup + every tick → all endpoints 503
`online_session_active`. No teleport/god/money endpoints; `/unstick` enforces its preconditions
server-side. Natives/wrappers from docs/research/brief-natives.json cross-checked against the
downloaded nightly's XML docs; raw `Function.Call` with the verified hash where no wrapper exists.
Acceptance: `~/.dotnet/dotnet build -c Release` clean; every §1 endpoint + all 11 task types
handled; driving styles map per contract; grep clean of TODO/mock/placeholder; README documents
in-game verification steps (deferred to Phase 1).

**WP-H — harness/ (executor).** Python ≥3.12 package `wasted_harness` with the master brief §6
module set: bridge_client, perception (dxcam behind a platform guard, in optional extra
`[windows]`), brain/ (schemas, tactical, director, memory, prompts/), behavior/ (humanizer,
activities, missions, recovery), commentary, events (batched inserts + on-disk offline queue
`harness/state/queue.jsonl`, flush on reconnect), budget (reads `harness/config/pricing.yaml`),
obs (obsws-python; SaveReplayBuffer synced on the ReplayBufferSaved event), overlay/ (FastAPI SSE
per CONTRACTS §6, self-contained page, transparent bg), main (supervised loop + `--check`
self-check that reports missing prerequisites and exits nonzero without faking anything), and
`tools/post_event.py` (CLI `python -m wasted_harness.tools.post_event --type bridge_down
--payload '{}'` — the watchdog's hook; queues offline when Supabase is absent).
pricing.yaml (exact values, RESEARCH.md §3): tactical `claude-haiku-4-5-20251001` in 1.00 / out
5.00 / cache-read 0.10 / cache-write-5m 1.25; director `claude-sonnet-5` in 2.00 / out 10.00 /
cache-read 0.20 / cache-write-5m 2.50; per MTok USD; source URL
`https://platform.claude.com/docs/en/about-claude/pricing` fetched 2026-08-25. Brain: Anthropic
Python SDK, structured decisions via `client.messages.parse` (pydantic DecisionModel per CONTRACTS
§2), prompt caching with `cache_control` on the static prefix, startup model-ID validation via a
1-token call + a tactical-prefix ≥4096-token check (Haiku cache minimum) — both fail loudly
without a key. Tests (**fixtures dir stays empty** — no fabricated game-state recordings; pure
functions may use constructed inputs): budget math from real pricing.yaml; commentary
no-repeat-in-10 rotation; humanizer bounds; activity weights/cooldowns; offline queue vs a real
unreachable URL; bridge_client clear errors vs nothing listening on 7777; overlay via its real
ASGI app. Acceptance: `pip install -e .[dev]` + `pytest` green on 3.13; `--check` output pasted;
if the local Supabase stack is up (`cd infra && npx supabase status`), events flush verified
against it with the printed service key, rows shown via SQL.

**WP-W — web/ (executor).** Next.js (latest stable 15.x, TS, App Router, Tailwind) implementing
master brief §7 + CONTRACTS §5: `/` (stream embed from `site_config` "stream" row with env
fallback — Twitch iframe w/ runtime `parent`, YouTube videoId variant; commentary feed newest-top
w/ expandable thought + mood + pause-on-scroll; HUD; counters; current goal; brain bill; offline
banner driven by `stats.heartbeat_at` staleness >60 s — never inferred from feed silence),
`/missions`, `/clips` (+ OG image via `next/og` per clip), `/agent` (AI disclosure +
non-affiliation), `/api/health`. Supabase via `@supabase/ssr`: server-render initial rows
(no-store), client component subscribes to postgres_changes inserts on decisions/events + updates
on stats, refetches rows newer than last-seen id on re-SUBSCRIBED (no gap-fill exists), renders
from rows only (D6 digest-migration safe). Design: black/red, grainy, deliberately loud; original
WANTED wordmark (**no Pricedown/game-font imitation, no Rockstar marks**); mobile-first; footer
non-affiliation + AI-generated labels. No fabricated data — empty DB renders honest empty/offline
states. Acceptance: `npm run build` + lint/typecheck clean; Playwright (chromium, incl. 390px
viewport) against `next dev` + the local Supabase stack: offline state, all pages render, feed
empty states; `.env.example` committed; no secrets.

**WP-S — scripts/ (executor).** PowerShell 7 ops per PLAN Phase 0a spec + RESEARCH D9/D10:
`server-setup.ps1` (idempotent + transcript-logged: winget toolchain, NVIDIA driver step
documented, Windows Audio services + VB-CABLE, Sysinternals Autologon, NoLockScreen/
InactivityTimeoutSecs/screensaver policies, Windows Update notify-only, high-perf power plan,
OpenSSH + default shell, firewall allowlist, scheduled console-session startup task),
`run.ps1` (Steam `-silent` → `-applaunch 271590` → poll GTA5.exe → harness → OBS),
`watchdog.ps1` (poll `GET /health`; relaunch chain; 3 fails/30 min → `post_event` bridge_down),
`deploy-bridge.ps1` (copy `bridge/bin/Release/net48/WastedBridge.dll` + Newtonsoft.Json.dll to
`<game>/scripts/`), `bridge-smoke.ps1` (exercise every CONTRACTS §1 endpoint, print PASS/FAIL),
`fetch-shvdn.ps1` (download SHV + SHVDN nightly on the server), `detach-rdp.ps1` (tscon
/dest:console). Ports/paths only from CONTRACTS. Acceptance: every file parses clean via
`$HOME/.powershell/pwsh` AST parse; no stream keys/secrets; each script `-WhatIf`-safe or guarded
so running on the wrong machine aborts loudly.

### Verification & integration
Each executor's report → an independent verifier re-runs the acceptance commands, greps for
mock/stub/placeholder/TODO/fake/lorem, and returns PASS/FAIL per check with evidence. Fable fixes
criticals, re-verifies, commits per package, updates STATUS.md with the evidence and the
verified-locally vs authored-awaiting-server split.
