# WANTED — RUNBOOK

Operational truth for the machine we actually bought. Sections marked (unexercised) become
observed fact once run on the real server.

**The machine:** Hetzner Server Auction — Intel
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
with the ✕ — always run **`scripts/leave-safely.ps1`** (section 6), which checks everything and
then does the `tscon … /dest:console` for you. Closing RDP with the ✕ kills the desktop, and
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
- **After any RDP session:** `scripts/leave-safely.ps1` — see **section 6**, which is the full
  procedure. (`scripts/detach-rdp.ps1` is the bare `tscon` underneath it, and `leave-safely.ps1`
  calls it as its last act. Use `detach-rdp.ps1` on its own only when you already know the state
  is good.)

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
| Desktop gone / capture black | `WASTED-ConsoleKeepalive` re-attaches the session within ~1 min | You almost certainly closed RDP with the ✕; if it is still black after a minute, see section 6 |
| Stream shows a **still picture**, everything else looks healthy | Watchdog detects paused-but-alive (`tick_hz`=0 or `game_fps` not advancing) → re-attach → refocus → alert | Read `C:\wasted\logs\foreground-assert.log`; see section 6 |
| Windows rebooted | Autologon → logon task → chain restarts | Verify the VDD display came back (see Fallback C note) |

## 5. Key rotation

Anthropic key → `harness/.env` (`ANTHROPIC_API_KEY`) + Anthropic console. Supabase secret →
`harness/.env` + `infra/.env.cloud`; publishable key → Vercel project env (`wasted`) +
`web/.env.local`. Stream key → OBS only. After any rotation:
`python -m wasted_harness.main --check` must pass, and the site's `/api/health` must stay `ok`.

---

## 6. Starting the show and leaving without freezing it

This is the whole procedure, in the order you actually do it. If you are tired, do exactly these
steps and nothing else.

### The two things that freeze the stream

They are different, they stack, and fixing one hides the other.

- **Freeze A — the session is parked.** Closing Remote Desktop leaves the session as `Disc`
  (`query session`). A parked session has no display output at all: Desktop Duplication fails
  with `DXGI_ERROR_SESSION_DISCONNECTED`, nothing renders. `tscon <id> /dest:console` fixes it,
  and `WASTED-ConsoleKeepalive` does that automatically — but on a timer, so there is a window of
  up to a minute.
- **Freeze B — nothing has focus.** GTA V has a setting called **Pause Game On Focus Loss** and
  it ships **On**. With it on, the game freezes whenever its window is not the foreground window.
  After a re-attach, nothing used to put the focus back, so the game stayed frozen until a human
  touched the machine. That is "the moment I stop, the game freezes". `WASTED-ForegroundAssert`
  now fixes it, and `leave-safely.ps1` refuses to let you leave until it is verified.

### Do it in this order

1. **Connect over RDP** as the show account.
2. **Start the game** (`scripts/run.ps1`, or by hand) and get it into **Story Mode** — not the
   launcher, not a loading screen. Leave OBS alone; if it is already streaming, that is fine.
3. **Confirm `/health` is ticking.** From a PowerShell 7 window on the box:
   ```powershell
   Invoke-RestMethod http://127.0.0.1:7777/health
   ```
   `tick_hz` must be above zero. Run it twice — **`game_fps` must be a different number the
   second time.** `game_fps` freezes at its last value when the game stops, it does not fall to
   zero, so "non-zero" proves nothing and "changed" proves everything.
4. **Dry-run the leave check** (optional but cheap; changes nothing, detaches nothing):
   ```powershell
   pwsh -File C:\wasted\repo\scripts\leave-safely.ps1 -CheckOnly
   ```
5. **Leave.** Elevated PowerShell 7, inside the RDP session:
   ```powershell
   pwsh -File C:\wasted\repo\scripts\leave-safely.ps1
   ```
   It applies and verifies the anti-lock/screensaver/power settings, checks the game and the
   display target, takes several `/health` samples **while the game does not have focus** (that
   is the real proof it will survive you leaving), checks `settings.xml`, checks and if necessary
   installs the two recovery tasks, tells you whether the harness is running, prints a "what
   happens next" block, waits for you to press Enter, focuses the game window and detaches.
   **If anything fails it refuses to detach and prints exactly what to fix.**
6. **Your Remote Desktop window closes. That is the success signal.** It is not a crash. Do not
   reconnect "just to check" — reconnecting re-adds the Microsoft Remote Display Adapter and
   changes the very thing you would be measuring.
7. **Check two things from your own machine:**
   - the **Twitch picture is moving** (a frozen game still shows a picture — watch it for a few
     seconds, do not just confirm something is there);
   - the **site's heartbeat is fresh**.
8. **Now ask for the agent to be started.** He is deliberately not running yet.
   **Why:** the harness captures the screen with dxcam, which binds to whatever display adapter
   exists when it starts. While you were connected, that was the Remote Desktop display adapter —
   and that adapter disappears the moment you disconnect, so a harness started before you left
   would be capturing a display that no longer exists. Started after you have gone, it binds to
   the virtual display, which is the one OBS is showing. Nothing in `leave-safely.ps1`,
   `console-keepalive.ps1`, `assert-game-foreground.ps1` or the watchdog will ever start him;
   they only ever report whether he is running.
   One extra wait: the harness also needs the session to be **back on the console**, not parked.
   If you left with `leave-safely.ps1` it already is.

### What is running while you are away

| Piece | Runs as | Does |
|---|---|---|
| `WASTED-ConsoleKeepalive` | SYSTEM, every minute (+ on session-disconnect event when this box logs one) | `tscon <id> /dest:console` when the game's session is parked. Derives the session id from the game process; logs tscon's exit code honestly to `C:\wasted\logs\console-keepalive.log` |
| `WASTED-ForegroundAssert` | **the logged-on user, Interactive** — a SYSTEM task is in session 0 and physically cannot focus a session-1 window | Puts the game window back in the foreground after a re-attach and **verifies it by foreground PID**. Quiet when nothing needs doing; writes to `C:\wasted\logs\foreground-assert.log` when it acts or fails. Never steals focus while you are connected |
| `scripts/watchdog.ps1` | you, in its own console | Bridge liveness, plus paused-but-alive: `/health` answering 200 while `tick_hz` is 0 or `game_fps` is not advancing → re-attach → refocus → alert. It never relaunches on that path, never touches OBS, never starts the harness |

Install the two tasks once with `pwsh -File scripts\install-focus-keeper.ps1` (elevated, in the
interactive session). `leave-safely.ps1` runs it for you if a task is missing.

### Symptom → cause → fix

Read the logs, not the reconnected desktop.

| Symptom | Most likely cause | Fix |
|---|---|---|
| **Still frame on Twitch**, game process alive, `/health` answers 200 | Freeze B: the game is paused because nothing holds the foreground (Pause Game On Focus Loss is On, or the re-attach left focus empty) | `C:\wasted\logs\foreground-assert.log` → a `FAILED` line, or no line at all. Run `schtasks /run /tn WASTED-ForegroundAssert`. Permanently: in-game **Esc → Settings → Graphics → Pause Game On Focus Loss → Off**, and **Esc → Settings → Audio → mute-on-focus-loss → Off** (that one is not in `settings.xml`; without it the stream moves but goes silent). Then `leave-safely.ps1` again |
| **Still frame on Twitch** and `tick_hz` is 0 while the game *does* have focus | Not a focus problem: a modal screen (pause menu, MISSION FAILED, a prompt) or SHVDN stopped | Clear the screen in-game and get back into Story Mode. If SHVDN is gone, `deploy-bridge.ps1` then `bridge-smoke.ps1` |
| **Black screen**, not a still frame | The virtual display driver stopped providing a display target: no adapter reports ≥1280×720 | `Get-CimInstance Win32_VideoController \| Select Name,CurrentHorizontalResolution,CurrentVerticalResolution`. The watchdog logs this as `forensics: NO adapter reports a desktop mode…`. Relaunching does not fix it — check the display device in Device Manager, then **Fallback C** in section 0 |
| **Black screen** right after you disconnected | Freeze A: the session is parked and the keepalive has not fired yet (up to ~1 min) | Wait a minute. Then `C:\wasted\logs\console-keepalive.log` — a `reattached session N to console` line means it worked; an `EXITED <code>` line means tscon failed (almost always: the task is not running as SYSTEM) |
| **Game window is not foreground** and refocusing "succeeds" but nothing changes | `SetForegroundWindow` was refused silently — Windows can deny a foreground change with no error | The log records the *verified* result, not the API return. Check `HKCU\Control Panel\Desktop\ForegroundLockTimeout` is `0` (`leave-safely.ps1` sets it) and that `WASTED-ForegroundAssert` really shows `LogonType=Interactive` in `Get-ScheduledTask WASTED-ForegroundAssert \| Select -Expand Principal`. A SYSTEM task there is the bug |
| **Session locked** (lock screen on the stream) | Idle lock, screen saver, or — most likely on a headless box — the **monitor power timeout**, which is itself a documented lock trigger when an inactivity limit is configured | `foreground-assert.log` says `the input desktop is 'Winlogon'`. Unlock over RDP, then run `leave-safely.ps1`, which sets and *verifies* the inactivity limit, both screen-saver key sets (registry **and** live via SystemParametersInfo), the lock-screen policy and all the `powercfg` timeouts. Note `InactivityTimeoutSecs` needs a **reboot** to take effect — the script tells you when it just changed it |
| **`/health` unreachable** (connection refused) | The game is not running, or Script Hook V / SHVDN did not load the bridge | `Get-Process GTA5`. If the game is up but the port is dead, the bridge did not load: `deploy-bridge.ps1`, then `bridge-smoke.ps1`. The watchdog treats this as a real failure and relaunches the chain — that path is unchanged |
| **`/health` returns 503** | An online session was detected; the bridge latched itself off and every endpoint returns 503 | Story Mode only. The latch clears only when the game restarts — restart it in Story Mode |
| **You reconnected and everything looks fine** | You changed what you were measuring: reconnecting re-adds the RDP display adapter | Judge from the log files and from Twitch, never from the reconnected desktop |

### Things that look like fixes and are not

- **Do not switch to exclusive fullscreen.** DXGI relinquishes fullscreen whenever the window is
  occluded, and it cannot be entered over Terminal Server at all. Borderless (`Windowed=2`) is
  mandatory here.
- **Do not add `-noBlockOnLostFocus` to `commandline.txt`.** That is a GTA IV argument. It does
  nothing for GTA V and will make you think you fixed something you did not. GTA V has **no**
  launch argument for focus or pausing — the only lever is the `settings.xml` key / in-game menu.
- **Do not edit `settings.xml` while the game is running.** The game rewrites it on exit and will
  clobber your change. Use the in-game menu, or close the game first (`run.ps1` now refuses to
  write it while the game is up, and says so).
- **Do not fix this in OBS.** It is not an encoder problem. `tick_hz` from `/health` is the
  game's own script thread; when it is zero the game is genuinely stopped, and no capture setting
  changes that.
