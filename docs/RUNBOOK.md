# WANTED — RUNBOOK

Operational truth for the machine we actually bought. Sections marked (unexercised) become
observed fact once run on the real server.

**The machine:** Hetzner Server Auction (ordered 2026-08-29, ref B20260829-3496963) — Intel
Core i5-12500 (**Intel UHD 770 iGPU, no discrete GPU**), 64 GB DDR4, 2×512 GB NVMe (Hetzner
auto-mirrors equal drives → ~476 GiB usable), Germany FSN1, **Windows Server 2025 Standard**
(Hetzner add-on, auto-installed), **no HDMI dummy plug**, RDP + free-3h KVM console on request.

---

## 0. Delivery day — the whole sequence

Everything is scripted; `scripts/bootstrap.ps1` sequences it and is resumable (`-StartAt`).
Run `scripts/preflight.ps1` **first** — it is read-only and answers the go/no-go questions in
~2 minutes. Each gate below is a stop-and-decide point: **do not push past a red gate.**

| # | Step | Gate / what must be true |
|---|---|---|
| 0 | RDP in as `Administrator` (first login has **no NLA** — set your RDP client accordingly), change the password, create `C:\wasted\` | Confirm **Desktop Experience**, not Server Core: `(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').InstallationType` must be `Server`. Core ⇒ reinstall (it cannot be converted) |
| 1 | `preflight.ps1` | An Intel display device (`PCI\VEN_8086…`) is enumerated. **No `VEN_8086` device at all ⇒ Fallback B before anything else** |
| 2 | Windows Server prerequisites (`server-setup.ps1`): **Media Foundation first**, .NET, VC++ redists, DirectX runtime, audio services, autologon, no-lock, updates notify-only, power plan, firewall, SSH | Media Foundation installed **before** Steam/the game — its absence is what causes the classic launcher failures on Server |
| 3 | Intel iGPU driver — extract the package to `C:\wasted\drivers` (**never** a Windows system folder) and install the INF with `pnputil /add-driver … /install`. **Never run Intel's `setup.exe`** (it OS-gates) and **never edit the INF** (breaks WHQL signing → would force permanent test-signing, which breaks game DRM) | `dxdiag` shows an **Intel** adapter, driver `32.0.101.x`, D3D feature level ≥ 11_1 — **not** "Microsoft Basic Render Driver" ⇒ Fallback A |
| 4 | Virtual display driver (VDD) so a real 1280×720 desktop exists with no monitor: `1280x720` **first** in `vdd_settings.xml` (Server reverts to the first entry after restart), render adapter pinned by name, **display driver only — never VDD's audio driver** (Code 52 on Server 2025) | A Monitor device exists and the **console session** desktop is 1280×720 ⇒ else Fallback C |
| 5 | `fetch-shvdn.ps1` (Script Hook V + SHVDN nightly ≥ v3.7.0-nightly.189), then Steam | — |
| 6 | **HUMAN, ~10 min:** log into Steam (incl. Steam Guard), install **GTA V Legacy, app 271590** (not Enhanced), log into the Rockstar launcher once, then set launcher offline mode + `-nobattleye` | Only step that needs a person. Passwords are typed by the human; nobody else ever sees them |
| 7 | Force `Documents\Rockstar Games\GTA V\settings.xml` to 1280×720 **`Windowed=2` (borderless)** — exclusive fullscreen must not be used with a virtual display | Game reaches Story Mode in the **console session** |
| 8 | `deploy-bridge.ps1` → `bridge-smoke.ps1` | Every CONTRACTS §1 endpoint passes; paste the table into STATUS.md (Phase 1 DoD) |
| 9 | OBS (profile from `scripts/obs-profile`, x264 720p30) + `run.ps1` + `watchdog.ps1` | Video and audio captured; 5-minute test stream reaches the channel (Phase 0a DoD) |
| 10 | `python -m wasted_harness.main --check` then the 20-minute live check | Decisions, commentary, events in Supabase, measured $/hour (Phase 2 DoD) |

**Golden rule from step 4 onward:** the game must run in the **console session**. Never leave RDP
with the ✕ — always run `scripts/detach-rdp.ps1` (`tscon … /dest:console`), or the desktop dies and
capture goes with it.

### Fallbacks (in order, from the recon brief)
- **A — Intel driver won't bind:** Device Manager → *Have Disk* on the extracted INF (ranking beats
  `pnputil`); then Windows Update optional drivers; then an older `32.0.101.x` build.
- **B — iGPU absent/disabled:** order the **free 3-hour KVM console** in Robot (Servers → your
  server → Support → Remote Console), enter BIOS, enable IGD / set internal graphics primary.
  This is the one thing worth burning the free KVM hours on — do it early, not late.
  (Evidence says this is unlikely: Hetzner's iGPU "disable" is an OS-level Linux blacklist, not a
  BIOS setting, and their own KVM console is a physical device on the board's video port.)
- **C — VDD misbehaves on Server 2025:** switch to **usbmmidd_v2** (signed, free for commercial
  use, simplest CLI). Note it does **not** survive reboot — re-enable it from a boot task.
- **D — no indirect display works at all:** ask Hetzner support whether they will fit a physical
  HDMI/DP dummy plug (they do hands-on work on request; cost/feasibility unknown — ask).
- **E — iGPU works but GTA V is unplayable:** the real fallback. Move to a discrete-GPU machine
  (Hetzner GEX line with the HDMI-emulator add-on, or HostKey's 1080 Ti box). The auction server
  bills **pro-rata on cancellation**, so this exit is cheap.
- **Not a fallback:** WARP/software rendering. Don't spend time on it.

---

## 1. Moving work onto the server

1. RDP in once with the Hetzner credentials; `server-setup.ps1` installs Git + Claude Code.
2. `git clone` (or copy) the repo into `C:\wasted\repo`, then run Claude Code there.
3. Prefer **SSH** (OpenSSH ships with Server 2025) over RDP for everything except the desktop-bound
   steps (the two human logins, BIOS/KVM work, OBS scene checks).
4. Secrets do **not** live in the repo: recreate `harness/.env` on the server from the values in
   the local gitignored copies (Anthropic key, Supabase URL + secret key, OBS websocket password).

## 2. Start / stop / restart

- **Start everything:** `scripts/run.ps1` (Steam `-silent` → `-applaunch 271590` → poll for
  `GTA5.exe` → harness → OBS with replay buffer). Registered as a logon scheduled task by
  `server-setup.ps1` so a reboot self-heals.
- **Keep it alive:** `scripts/watchdog.ps1` — polls `GET /health`; 3 consecutive failures ⇒ kill +
  relaunch via `run.ps1`; 3 failed relaunches within 30 min ⇒ posts a `bridge_down` event (the site
  then shows the agent offline honestly) and keeps retrying on a slow cadence.
- **Stop:** stop the watchdog first, then the harness, then the game — otherwise the watchdog
  relaunches what you just stopped.
- **After any RDP session:** `scripts/detach-rdp.ps1`.

## 3. OBS (Phase 6 — settings authored, unexercised)

720p30, x264 `veryfast`, ~3500 kbps, 30-second replay buffer; game audio via VB-CABLE (capture the
**CABLE device explicitly**, never "Default" — RDP audio redirection hijacks the default device).
Browser source → the harness overlay at `http://127.0.0.1:7788/overlay`. The **stream key is typed
into OBS by the human only** and never enters the repo, the harness, or any log.

## 4. Failure playbook — the 3am page

| Symptom | What happens automatically | What a human does |
|---|---|---|
| Game crash | Watchdog relaunches the whole chain | Nothing, unless it repeats |
| 3 failed relaunches in 30 min | `bridge_down` event → site shows the agent offline (honest) | Check the game/launcher state over RDP |
| Claude API rate limit / error | Exponential backoff; reflex layer keeps him alive | Nothing |
| Supabase unreachable | Events queue to `harness/state/queue.jsonl`, flush on reconnect | Nothing |
| Spend spike | Budget governor L1→L3 (announced on stream); L3 parks him | Adjust `WASTED_HOURLY_CAP_USD` |
| Desktop gone / capture black | — | You almost certainly closed RDP without `detach-rdp.ps1`; reconnect, run it |
| Windows rebooted | Autologon → logon task → chain restarts | Verify the VDD display came back (see Fallback C note) |

## 5. Key rotation

Anthropic key → `harness/.env` (`ANTHROPIC_API_KEY`) + Anthropic console. Supabase secret →
`harness/.env` + `infra/.env.cloud`; publishable key → Vercel project env (`wasted`, scope
`<redacted>`) + `web/.env.local`. Stream key → OBS only. After any rotation:
`python -m wasted_harness.main --check` must pass, and the site's `/api/health` must stay `ok`.
