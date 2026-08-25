# WANTED — STATUS

Single source of truth. Nothing appears in "Works / verified" without evidence (command output,
run log, or URL) noted next to it. Last updated: 2026-08-25 (evening).

## Current phase

**Phase 0 done · wait-work build-out done.** All four packages + infra are authored and verified
to the maximum extent possible on this machine. **Everything further is blocked on the human
checklist** (server, game purchase, keys) — see Blockers.

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

## Blockers on the human (unchanged checklist, delivered 2026-08-25)

1. **Order the server** (Hetzner GEX44 + Windows Server 2025 + "HDMI emulator" — lead time
   currently "several weeks", this is the critical path) — or hand over cloud credentials for an
   interim GPU VM.
2. **Buy "Grand Theft Auto V Enhanced" on Steam** (app 3240220; includes Legacy 271590 which we run).
3. **Keys:** Anthropic API key (org must clear >$500/month — Build tier), Supabase project
   (URL + publishable/anon + secret/service-role), Vercel access.
4. **Stream channel** (Twitch recommended); stream key goes into OBS by hand only.
5. Later, on the server: one-time Steam + Rockstar logins, offline args, BattlEye off.

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
