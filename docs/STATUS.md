# WANTED — STATUS

Single source of truth. Nothing appears in "Works / verified" without evidence (command output,
run log, or URL) noted next to it. Last updated: 2026-08-25 (evening).

## Delivery-day readiness pass — 2026-08-29 (commit `71140c0`)

Server ordered (Hetzner auction i5-12500, ref B20260829-3496963, awaiting delivery). While
waiting, a full readiness pass ran: 2 recon agents + 4 hardening agents + 4 verifiers + 4 fixers
+ 1 confirmer. **Confirmer verdict: SAFE TO COMMIT, 15/15 checks PASS**, each falsification-tested.

**The machine changed under us** — we bought an Intel-iGPU auction box, not the planned
NVIDIA GEX44, so scripts/ were pointed at hardware we do not own. Retargeted; new findings in
docs/RESEARCH.md §7b (D11–D15) and the delivery-day sequence in docs/RUNBOOK.md §0.

**Defects caught before delivery day (all fixed + confirmed):**
- 🔴 The harness would have **crashed on its first poll of every session** — the bridge legitimately
  emits `last_task.id: null` before any task; the pydantic model required `str`. Nobody had ever
  validated the bridge's real output against the harness's model. Fixed, contract clarified
  (v1.2), and a **cross-package conformance test** now validates real bridge serializations
  against the harness models (proven to reject renamed/dropped/retyped fields).
- 🔴 `server-setup.ps1` would have downloaded the VDD **audio** driver instead of the display
  driver (ambiguous asset pattern + `Select-Object -First 1`) — and that audio driver is rejected
  on Server 2025 (Code 52). Ambiguous patterns now fail loudly.
- 🟠 Operator-skipped bootstrap phases were recorded as complete; nefcon's reboot-required exit
  code (3010) was treated as failure; the OBS profile was never selected by `run.ps1`.
- 🟠 Governor L3 issued `stop` wherever the agent was (possibly mid-freeway) instead of parking
  somewhere scenic; activity task-tracking raced against the 3 Hz poll; `stunt`/`mission_end`/
  `mission_fail` were wired but never emitted (now honestly declared as Phase 3/4 gaps).
- 🟠 web: per-route `og:title`/`og:url`/`canonical` were wrong (a regression introduced and fixed
  within the same pass); test runs poisoned `.next` and `tsconfig.json`.

**Verified locally after fixes:** harness **151 tests** green (was 42) + ruff clean; bridge Release
build 0 warnings/0 errors + 185 offline checks; 10 PowerShell files 0 parse + 0 analyzer errors;
web build/tsc/lint clean, 36 Playwright passed. Everything game-adjacent remains BLOCKED on the
undelivered server — nothing about the game is claimed as working.

⚠ **Anthropic key is at its configured spend limit** — verified today: `You have reached your
specified API usage limits. You will regain access on 2026-09-01`. Raise the limit in the console
before the live checks, or the agent cannot think.

## Current phase

**Phase 0 done · wait-work build-out done · CLOUD LAYER LIVE (2026-08-25 evening).** With the
human's credentials, the entire non-game stack is now verified against real services and the site
is publicly live. **Remaining work needs only: the Windows game server, the game purchase, and a
Twitch channel name.**

## Cloud layer — verified against REAL services (2026-08-25, workflow wf_04a3dc63-84d + inline)

- **Site LIVE (production): https://wasted-lemon.vercel.app** — `/api/health` `{"ok":true,
  "supabase_configured":true}`; renders the honest OFFLINE banner *and* the first real decision
  rows server-side. Vercel project `wasted` in scope `<redacted>`; env vars set for
  preview+production. ⚠ Production went live via Vercel CLI v53's changed default (plain
  `vercel deploy` now targets production) — disclosed to the human; brand-new project, nothing
  overwritten. Preview URLs are SSO-protected (Vercel Authentication) — disable in Project
  Settings → Deployment Protection if preview access is wanted.
- **Cloud Supabase schema applied + verified** (project wwluuzkboosvtupcexsp, empty pre-apply):
  full RLS suite passed on the production DB (anon read-only ×7, writes denied ×3, service-role
  writes ok, constraint rejections ×2), publication = exactly decisions/events/stats, buckets
  clips+shots public. All verification rows cleaned up. The earlier "Secret API key required"
  gate on publishable keys is **gone** — anon REST reads return 200; anon writes correctly denied.
- **Brain verified vs real Claude API**: both model IDs valid (1-token calls); tactical static
  prefix **7617 tokens** (≥4096 Haiku cache min), director 8532; two real structured decisions via
  `messages.parse` — valid DecisionModels, word limits enforced; **prompt caching proven**
  (call 2: `cache_read_input_tokens=8373`); measured cost **$0.011331 cold / $0.001714 warm** per
  tactical call → ≈ **$0.41/h at full 240-calls/h cadence** (design estimate was $0.37 — confirmed).
  Total verification spend ≈ $0.013.
- **First honest data**: session `297839d7-c0cb-4907-bba1-fed9f85d8140` (harness_version
  `api-verify`) with session_start event, 2 real decisions, stats heartbeat — left in place as the
  project's first rows; the live site renders them.
- **Realtime delivery measured**: postgres_changes INSERT → subscriber in **574–857 ms**
  (Phase 5 bar: < 2 s). First-ever subscription failed silently during replication-slot warm-up —
  the site's refetch-on-SUBSCRIBED covers this; test rows cleaned up.
- `tools/post_event` online path verified (wrote → confirmed → test row deleted).

Phase 5 DoD progress: Realtime latency ✔ measured; mobile ✔ (local Playwright); Playwright against
the deployed URL still open — `web/playwright.config.ts` needs a BASE_URL env override (backlog,
web executor next pass). `NEXT_PUBLIC_SITE_URL` env to set once the final domain is chosen.

## Works / verified locally (real commands, re-run independently by a verifier)

| Package | Verified here (evidence) | Deferred to real env |
|---|---|---|
| infra/ | Migrations apply clean on real Postgres 16 (`infra/verify-local.sh` reproduces): anon SELECT on all 7 tables; anon INSERT/UPDATE/DELETE denied ×3; service_role writes ok (incl. identity sequences); enum CHECKs reject bad values; publication = exactly decisions/events/stats | Apply to cloud project; PostgREST/Realtime/Storage behavior (needs keys) |
| bridge/ | `dotnet build -c Release` clean (0 warn/0 err); SHVDN v3.7.0-nightly.189 pinned + hash-verified; **38/38 real HTTP transport checks** by loading the compiled HttpServer under .NET 8 (state/health shapes, all 11 task types → 202 + `t-` ids, param/unknown-type 400s, unstick 409, online kill-switch 503 on every endpoint) | Everything touching natives: Phase 1 smoke with the game (`scripts/bridge-smoke.ps1`) |
| harness/ | 42/42 pytest green (py 3.13); `--check` honest (exit ≠ 0 listing missing prereqs); post_event queues offline for real; all five writer row shapes proven against the real schema as service_role; pricing.yaml byte-exact to D4 with source URL + date; product-code grep clean | PostgREST flush success path; real Claude API calls (startup model check, prefix ≥4096 check, live decisions); OBS replay pipeline; everything game-adjacent; 20-min live check (Phase 2) |
| web/ | build/tsc/lint clean; **12/12 Playwright** incl. offline banner with unreachable backend, honest empty states, per-clip OG image, 390×844 no horizontal scroll; no fabricated data anywhere | Realtime delivery + real rows; Vercel preview + Playwright there; live Twitch/YouTube embed (needs HTTPS host + channel) |
| scripts/ | PowerShell AST parse 0 errors ×8 files; PSScriptAnalyzer 0 errors; env guards abort loudly on non-server hosts (proven); bridge-smoke honest-FAIL run vs dead port (22 checks, exit 1); live checks of every external URL/API the scripts rely on (dev-c Referer gating, SHVDN release asset, winget IDs, VB-CABLE, Autologon) | Real execution on Windows Server 2025 (Phase 0a) |

Commits: `50d1cd9` (bridge), `ecc39ff` (harness), `0e02430` (web), `64ce38d` (scripts),
`22a3a53`/`a1aa924` (infra + contracts v1.1). Full executor/verifier reports: workflow
wf_29b91fcf-c97 journal (session transcript dir).

Notable implementation decisions accepted from executor reports (contract-conforming):
governor L1/L2/L3 at 70/90/100% of the configurable hourly cap; director screenshots allowed only
on §4 screenshot-bearing events; §2 word limits via pydantic validators (retry once → reflex keeps
control); watchdog posts only `bridge_down` (harness owns `bridge_up`/downtime accounting);
`stop` → `last_task.status: idle`; online-session latch is permanent until restart;
supabase-js pinned 2.109.0 until Node ≥22 baseline; drive/walk arrival = planar (XY) distance.

## Broken / known gaps

- **Nothing further is verifiable on this Mac.** The remaining work requires: the Windows GPU
  server (Phases 0a,1,2,3,4,6,7), an Anthropic API key (brain verification), Supabase keys
  (cloud migration apply + Realtime), Vercel access (Phase 5 previews), a stream channel (0a/6).
- vgamepad/ViGEmBus on Server 2025 untested (known Code-28 risk) — SendInput is the plan of record.
- Curated landmark/stunt coordinates in `behavior/activities.py` need live tuning in Phase 3.
- `-scofflineonly` durability is best-effort (periodic Rockstar revalidation reported).

## Blockers on the human (updated 2026-08-25 evening — keys DONE)

1. **The server** — cheapest plan: hourly cloud GPU (TensorDock/AWS, ~$10 total) to test first,
   then a monthly box (Hetzner EX44-class ~€70/mo or budget GPU host ~€90–130/mo). Human decides
   and provides IP + admin password.
2. **Buy "Grand Theft Auto V Enhanced" on Steam** (app 3240220; includes Legacy 271590 which we run).
3. ~~Keys~~ ✔ DONE — Anthropic + Supabase + Vercel all provided and verified 2026-08-25.
   (Anthropic org spend-cap tier still to confirm before 24/7.)
4. **Twitch channel name** (free) — then it goes into `site_config` and OBS.
5. Later, on the server: one-time Steam + Rockstar logins, offline args, BattlEye off.
6. Housekeeping: free disk space on this Mac; decide whether the accidental production URL
   (wasted-lemon.vercel.app) stays live (it only shows the honest offline page + verify data).

## Cost

- Brain (design model, replace with measurement in Phase 2): ≈ **$0.70/streamed hour**
  (tactical Haiku ~240 calls/h ≈ $0.37 + director Sonnet 5 ~40 calls/h ≈ $0.33) — under the
  $1.50 target; ≈ $500/month at 24/7 → needs Build tier.
- Server ≈ €184/mo + ~€28 Windows + €1.10 HDMI emulator + €79 setup. VB-CABLE pro license TBD.
- Supabase Realtime bills per recipient (≈$1,900/mo at 300 viewers × 1 msg/s) — v1 fine at launch
  scale; digest migration path is contracted (CONTRACTS §5 / D6).

## Environment / operations notes (2026-08-25)

- **Host disk is effectively full** (~421 of 460 GB is user data). During the build the disk hit
  0 bytes twice; recovered by clearing rebuildable caches (npm/pip/Chrome/updater ≈ 7 GB) and
  restarting colima's VM (its FS wedged on ENOSPC both times — `colima restart` heals it). The
  full local Supabase stack (~6 GB images) is **not viable on this machine**; SQL verification
  runs via `infra/verify-local.sh` (throwaway postgres:16-alpine). **Human: free some tens of GB
  for comfort**, and note `~/Library/Containers/com.docker.docker/…/Docker.raw` (1.1 GB) is a
  remnant of Docker Desktop — you run colima now; delete it if you no longer use Docker Desktop.
- Local toolchain installed user-locally (removable): .NET SDK 8 in `~/.dotnet`, PowerShell 7.4.6
  in `~/.powershell`.
- Game target: GTA V **Legacy** (271590) via Enhanced purchase; SHV 1.0.3889.0/1158.13; SHVDN
  **v3.7.0-nightly.189 pinned** (recorded in bridge/README); .NET Framework 4.8; `-nobattleye`.
- Paste-corruption note: master brief arrived with minor copy damage; reconstructed spots flagged
  in CONTRACTS.md §2 (mood enum — `scared` restored).
