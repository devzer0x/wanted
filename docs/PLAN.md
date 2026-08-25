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
