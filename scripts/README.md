# WANTED — Windows server ops scripts

Owner: Fable / ops executor only. PowerShell scripts for the Windows Server 2025 game server
(PLAN Phase 0a, RESEARCH.md D9/D10). Nothing here runs on the dev Mac except parsing/lint —
every script opens with an environment guard that aborts loudly unless it is on the Windows
game server (`-Force` overrides the marker checks where that is sensible).

## The machine these scripts target

**Ordered 2026-08-29 (Hetzner Server Auction, ref B20260829-3496963), awaiting delivery.**

| | |
|---|---|
| Host | Hetzner auction dedicated server, Germany FSN1, consumer-class hardware |
| CPU | Intel Core i5-12500 (6C/12T, Alder Lake) |
| GPU | **Intel UHD Graphics 770 integrated only — there is no discrete GPU** |
| RAM / disk | 64 GB DDR4, 2× 512 GB NVMe |
| Network | 1 Gbit, Intel I219-V, one primary IPv4 |
| OS | Windows Server 2025 Standard (Hetzner add-on, auto-installed) |
| Display | **No monitor, no HDMI emulator** — auction servers do not offer that add-on |
| Access | RDP; free 3 h KVM console on request; self-service vKVM rescue mode |

This is **not** the machine the original plan assumed (a GEX44 with an NVIDIA RTX 4000 and an
HDMI emulator add-on). Two consequences drive most of what these scripts do:

1. **Intel graphics driver on a Server SKU.** Intel's consumer installer refuses Windows Server,
   but the DCH graphics *INF* is not product-type gated — its `TargetOSVersion` leaves
   ProductType and SuiteMask blank, so it applies to NT 10.0 amd64 build ≥ 16225, and Server
   2025 is build 26100. So the driver is installed **by INF with `pnputil`, never by Intel's
   `setup.exe`**, and **no test-signing is required** — which matters, because a display driver
   is kernel-mode and a permanently test-signed 24/7 box is not acceptable (test mode also
   breaks some games outright).
2. **No display target.** With no monitor and no dummy plug the iGPU has zero connected outputs,
   so the console session has no desktop to present into: the classic symptom is everything
   collapsing to 1024×768. An **Indirect Display Driver (IddCx)** supplies a virtual display
   target; the game still renders on the Intel adapter, exactly as it would with a physical
   monitor. This is the configuration headless streaming hosts run at scale.

Everything Windows-only in this package is **authored and statically verified, not executed** —
there is no Windows machine to test on until the server is delivered. See "Verification" below
for exactly what was and was not run.

## Conventions (all scripts)

- **PowerShell 7** (`#Requires -Version 7.0`), `Set-StrictMode -Version Latest` — with one
  deliberate exception: `preflight.ps1` is **Windows PowerShell 5.1 compatible and standalone**,
  because Server 2025 ships only 5.1 and preflight has to run before anything is installed.
- Transcript logging into `C:\wasted\logs\<script>_<timestamp>.log`.
- `-WhatIf` supported wherever state changes (bootstrap, setup, fetch, deploy, run, watchdog,
  detach).
- **No secrets.** Nothing here stores passwords, stream keys, or API keys. The Autologon
  password is typed at runtime and lands only in the Windows LSA secret store; the OBS
  websocket password and Supabase/Anthropic keys live in the harness `.env`, never here; the
  stream key is typed into OBS and lives only in OBS's own profile directory.
- Ports and paths come from `docs/CONTRACTS.md` (bridge `127.0.0.1:7777`, overlay `7788`,
  GTA V Legacy Steam app `271590`).
- `common.ps1` is the shared helper library (guards, logging, transcript, python resolution).
  It is dot-sourced by every PowerShell 7 script and is not runnable on its own. It also pins
  `$PSNativeCommandUseErrorActionPreference = $false`: a native command's nonzero exit code is
  *data* here (winget probes, `pnputil`, `dism`, `tscon`, `nefconw`, child `pwsh` runs all check
  `$LASTEXITCODE` themselves) and must not throw. That is already the default on the PowerShell
  7.4.6 this was authored against, but the server gets whatever winget installs, so it is pinned
  rather than inherited.
- Every `.ps1` is saved **UTF-8 with a BOM**. Windows PowerShell 5.1 reads BOM-less files as
  ANSI and mangles non-ASCII characters.

---

## Delivery day — the exact sequence

### 0. Before you touch anything (5 min)

RDP in with the credentials Hetzner sent. Then, from an **elevated Windows PowerShell 5.1**
prompt (PowerShell 7 is not installed yet):

```powershell
# Get the repo onto the box however you like; C:\wasted\repo is what everything defaults to.
# Then:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\wasted\repo\scripts\preflight.ps1
```

`preflight.ps1` changes nothing and takes about two minutes. Read its verdict table. **If it
exits nonzero, stop** — it prints the specific next action for every blocker.

### 1. Install PowerShell 7 (2 min)

```powershell
winget install --exact --id Microsoft.PowerShell --silent --accept-package-agreements --accept-source-agreements
```

### 2. Stage the Intel graphics driver (5 min, needs a browser somewhere)

`server-setup.ps1` never downloads from Intel — the package URL is version-specific and gated.
Download the **Intel 11th–14th Gen Processor Graphics** package (Intel download **#864990**;
recon recorded `32.0.101.7088`, dated 2026-06-22) from
<https://www.intel.com/content/www/us/en/download/864990/> and copy the `.exe` into
`C:\wasted\downloads\`. Do **not** run it — `server-setup.ps1` extracts it with 7-Zip and
installs the INF with `pnputil`.

You can skip this and do it later; setup will print it as a manual action. But nothing works
without it: no vendor WDDM driver means no Direct3D 11, and GTA V will not start at all.

### 3. Run bootstrap (the rest of the day)

```powershell
pwsh -File C:\wasted\repo\scripts\bootstrap.ps1 -AdminIP <your.ip.or.cidr>
```

`bootstrap.ps1` sequences everything and stops at the points where a human is genuinely needed.
It is resumable — every phase that succeeds is recorded in
`C:\wasted\state\bootstrap-state.json` and skipped on the next run.

| # | Phase | What happens | Human needed? |
|---|---|---|---|
| 1 | `preflight` | `preflight.ps1` again, machine-readable this time (`state\preflight.json`). Any hard blocker stops the run. | no |
| 2 | `setup` | `server-setup.ps1`: toolchain, Server features, Intel INF, virtual display driver, audio, autologon, policies, firewall, logon task | one password prompt (autologon), one confirm (firewall), one click (VB-CABLE driver) |
| 3 | `reboot` | Gate. Drivers and Media Foundation are not live until the box restarts. | **yes** — `Restart-Computer -Force`, then resume |
| 4 | `game` | Prints the Steam/Rockstar checklist, then **verifies GTA5.exe actually exists** before continuing | **yes** — see below |
| 5 | `shvdn` | `fetch-shvdn.ps1`: Script Hook V + the pinned SHVDN nightly | no |
| 6 | `bridge` | `deploy-bridge.ps1`: the built `WastedBridge.dll` into `<GameDir>\scripts` | no (build the bridge first) |
| 7 | `obs` | Prints the OBS checklist, then verifies OBS is installed **and** that both the `WASTED` profile and a `WASTED` scene collection exist — the two names `run.ps1` passes to OBS as `--profile` / `--collection` | **yes** — the profile is already installed by setup; the scene collection is built by hand, see `obs-profile/README.md` |
| 8 | `run` | Steam → game → harness → OBS, **always in the console session**. From the console this calls `run.ps1` directly; from RDP it starts the `WASTED-Run` scheduled task (Interactive principal ⇒ console session) and waits up to `-RunTimeoutS` for `GTA5.exe`, because `run.ps1` correctly refuses to launch the game into an RDP session | no |
| 9 | `smoke` | `bridge-smoke.ps1` against every CONTRACTS.md §1 endpoint | no — paste its table into `docs/STATUS.md` |

Resume at any point:

```powershell
pwsh -File .\bootstrap.ps1 -AdminIP <ip> -StartAt shvdn      # jump to a phase
pwsh -File .\bootstrap.ps1 -AdminIP <ip> -Rerun              # ignore the state file
pwsh -File .\bootstrap.ps1 -AdminIP <ip> -WhatIf             # dry run
pwsh -File .\bootstrap.ps1 -AdminIP <ip> -NonInteractive     # stop at each human phase instead of waiting
```

A failed critical phase always stops the run with a nonzero exit code and a printed remediation.
Nothing is ever silently skipped past.

### 4. What the human must actually do

Batched, in order. Nobody but the human types these passwords — always in the RDP session.

1. **Reboot** when the `reboot` phase asks.
2. **Steam**: sign in (incl. Steam Guard); install **GTA V Legacy** via `steam://install/271590`.
   *Not* Enhanced (3240220) — Enhanced needs a 4 GB DirectX 12 GPU, an SSD and BattlEye, none of
   which fit this box, and the whole ScriptHookV/SHVDN ecosystem targets Legacy anyway.
3. **Rockstar**: launch the game once; the first launch installs the Rockstar Games Launcher and
   Social Club. Sign in and link. Run the launcher as Administrator if it misbehaves.
4. **Game display**: after the first auto-detect run, set
   `Documents\Rockstar Games\GTA V\settings.xml` to `ScreenWidth=1280`, `ScreenHeight=720`,
   `Windowed=2` (borderless). Or let `run.ps1 -EnforceDisplaySettings` do it for you.
   **Never exclusive fullscreen** — it is impossible over Terminal Server by design and fragile
   on an indirect display.
5. **VB-CABLE**: click "Install Driver" in the installer setup launches, then reboot.
6. **OBS**: `server-setup.ps1` already installed the `WASTED` profile; pick it in OBS, then build
   the scene collection and **name it exactly `WASTED`** — `run.ps1` launches OBS with
   `--profile WASTED --collection WASTED`, and OBS silently keeps the previous profile/scenes for
   any name it cannot find. `obs-profile/README.md` has the exact source list.
   Stream key and websocket password are typed by hand; no script here touches either.
7. **SSH key**: append your public key to
   `C:\ProgramData\ssh\administrators_authorized_keys`, then `Restart-Service sshd`.

### 5. Leaving the server running

```powershell
pwsh -File .\watchdog.ps1          # start alongside the stream, own console
pwsh -File .\detach-rdp.ps1        # ALWAYS leave RDP this way
```

**Never close the RDP window.** Closing it leaves the machine with no interactive desktop; the
game loses its display and capture goes black. `detach-rdp.ps1` runs an elevated
`tscon <session> /dest:console`, which hands the desktop back to the console session instead.
Your RDP window closing is the success signal.

---

## Scripts

### preflight.ps1  *(new — run this first)*

Read-only go/no-go diagnostic. **Windows PowerShell 5.1 compatible and standalone** (it does not
dot-source `common.ps1`, which is PS7-only) so it can run on a completely untouched server.
Changes nothing. About two minutes, most of it `dxdiag`.

Answers, with a PASS/WARN/FAIL/INFO row each: Windows edition, build and `InstallationType`
(Server Core is a hard blocker — Desktop Experience cannot be added after install); PowerShell
version and whether `pwsh` exists; elevation; CPU model and thread count; RAM; free space per
fixed disk; console-vs-RDP session (with the full `query session` table); every display adapter
with its driver, PnP id, active mode and Device Manager problem code; whether the **Intel iGPU**
is present and whether a *vendor* driver is bound (a Microsoft basic driver is a FAIL — no D3D11
means the game will not start); whether a discrete GPU exists; whether any **virtual display
driver** is installed; whether a display target and a sane desktop mode exist (1024×768 is
flagged as the no-display-target fallback); the RDS `bEnumerateHWBeforeSW` policy; **Direct3D
feature levels** via `dxdiag /whql:off /t` — the only in-box way to read them — parsed for
`Feature Levels`, `DDI Version` and `Driver Model`; Media Foundation feature state
(`Get-WindowsFeature`, falling back to `dism /get-featureinfo` under both documented feature-name
spellings, corroborated by `mfplat.dll`); .NET Framework 4.8 release; VC++ 14 x64 **and** x86;
the June 2010 DirectX side-by-side DLLs; the three audio services; VB-CABLE; TCP:443 reachability
to Steam, Anthropic, Supabase, GitHub, dev-c.com and Rockstar; and what tooling is already on
the box.

Exits **nonzero on any hard blocker**, and prints the specific next action for every FAIL and
WARN. `-JsonOut` writes the whole result set as JSON (that is how `bootstrap.ps1` consumes it).

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\preflight.ps1
pwsh -File .\preflight.ps1 -JsonOut C:\wasted\state\preflight.json -SkipDxdiag
pwsh -File .\preflight.ps1 -GameDrive D: -MinFreeGB 150 -ExtraProbeHost myproject.supabase.co
```

### bootstrap.ps1  *(new — the one command)*

Sequences `preflight` → `setup` → `reboot` gate → **human: game install/logins** → `fetch-shvdn`
→ `deploy-bridge` → **human: OBS** → `run` → `bridge-smoke`, printing an explicit numbered
checklist at each human pause. Resumable via `C:\wasted\state\bootstrap-state.json` and
`-StartAt`; `-Rerun` ignores the state file; `-NonInteractive` prints the checklist and stops
with exit code 2 instead of waiting.

Human pauses are **verified, not trusted**: the game phase only completes when `GTA5.exe` is
actually found, the OBS phase only when `obs64.exe`, the `WASTED` profile and the `WASTED` scene
collection all exist, the reboot gate only when the pending-reboot registry flags are gone.
Answering `DONE` without having done the work just re-prompts. Each phase runs its child script
in its own `pwsh` process, so exit codes, transcripts and interactive prompts all behave normally.

A human pause has exactly three outcomes and they stay distinct all the way to the exit code:

| Answer | What happens |
|---|---|
| `DONE` | The phase's own verification runs. Only when it **passes** is the phase recorded complete in the state file. A failing verification just re-prompts. |
| `SKIP` | The operator accepts the risk. The phase is **not** recorded complete, the run continues, the summary lists it under `SKIPPED BY OPERATOR (risk accepted, NOT complete)` with the resume command, and bootstrap exits **2**. The next pass runs the phase again. |
| `ABORT` | The run stops with an error. |

`-NonInteractive` is a fourth, separate outcome: the checklist is printed and the run stops with
exit code 2 without touching the state file.

Note on ordering: the game phase deliberately runs **before** `fetch-shvdn`, because
`fetch-shvdn.ps1` needs `GTA5.exe` to already exist.

```powershell
pwsh -File .\bootstrap.ps1 -AdminIP 203.0.113.7
pwsh -File .\bootstrap.ps1 -AdminIP 203.0.113.7 -StartAt obs
pwsh -File .\bootstrap.ps1 -AdminIP 203.0.113.7 -WhatIf
```

### server-setup.ps1

Idempotent Phase 0a bring-up, 16 steps. `-AdminIP` is mandatory (IP or CIDR, comma-separated
list) — inbound RDP/SSH get restricted to it, so double-check it.

```powershell
pwsh -File .\server-setup.ps1 -AdminIP 203.0.113.7            # real run
pwsh -File .\server-setup.ps1 -AdminIP 203.0.113.7 -WhatIf    # dry run
```

1. **Toolchain via winget** — PowerShell 7, Git, Python 3.12, Node LTS, .NET SDK 8 + Framework
   4.8 targeting pack, OBS, Steam, 7-Zip (7-Zip is also how the Intel package gets extracted).
2. **Windows Server features** — hard-fails on Server Core; enables **Media Foundation** via
   DISM (`dism.exe` rather than `Install-WindowsFeature`, because the ServerManager cmdlets only
   work through PowerShell 7's Windows-PowerShell compatibility layer). Media Foundation is not
   installed by default on Server, and GTA V's installer/launcher check the Windows media
   feature set — its absence produces the "Windows Media Player / Media Feature Pack" family of
   errors that Windows N users hit. `-InstallDirectPlay` adds DirectPlay; GTA V does not need it.
3. **Runtimes** — verifies .NET Framework ≥ 4.8 (4.8.1 ships preinstalled on Server 2025);
   installs **VC++ 14 x64 *and* x86** (Social Club and launcher components are 32-bit); installs
   the **June 2010 DirectX End-User Runtime** for the side-by-side legacy DLLs modern Windows
   omits (D3DX9, `d3dcompiler_43`, XAudio 2.7, `xinput1_3`), which Script Hook V and many ASI
   mods link. Each has a documented manual fallback if the download fails.
4. **Audio services** — `AudioEndpointBuilder` first (Audiosrv depends on it), then `Audiosrv`,
   then `MMCSS`. All three are off by default on Server.
5. **VB-CABLE** — fetch + stage; the driver click is interactive and a reboot is required.
6. **Intel graphics driver** — the replacement for the old NVIDIA step. Detects the
   `PCI\VEN_8086` display adapter, skips if a vendor driver is already bound, otherwise finds a
   staged Intel package (`-IntelDriverPackage`, or `gfx_win_*.exe` / `intel-gfx\` under
   `C:\wasted\downloads`), extracts it with 7-Zip into `C:\wasted\tools\intel-gfx` (**never** a
   Windows system folder — Intel KB 000088280: the DCH driver refuses to install from one), and
   runs `pnputil /add-driver <inf> /install`. Never executes Intel's `setup.exe`. Also carries a
   zero-cost discrete-GPU branch (reports any NVIDIA/AMD adapter and `nvidia-smi` if present).
   With nothing staged it prints the full manual procedure instead of failing silently.
7. **Virtual display driver** — resolves the VirtualDrivers/Virtual-Display-Driver release
   (`-VddTag`, default `25.7.23`) and nefcon `v1.14.0` through the GitHub API, imports the
   driver catalog's signer certificate into `LocalMachine\TrustedPublisher` (without it the
   device lands on Code 52), writes `C:\VirtualDisplayDriver\vdd_settings.xml` with **1280×720
   first** in the resolution list (Server reverts to the first entry after a restart) and the
   GPU pinned to the Intel adapter by friendly name, then installs the root-enumerated device
   with `nefconw install <inf> Root\MttVDD` — the same devcon-compatible call the VDD project's
   own `Community Scripts/silent-install.ps1` makes. Any failure prints the two documented
   fallbacks.

   Two things this step is deliberately fussy about:

   - **Which asset.** `-VddAssetPattern` (default `VirtualDisplayDriver-x86.Driver.Only.zip`)
     must match **exactly one** asset or the script aborts and lists everything in the release.
     Tag 25.7.23 ships three `*.Driver.Only.zip` files — `VirtualAudioDriver-x86`,
     `VirtualDisplayDriver-ARM64` and `VirtualDisplayDriver-x86` — so a loose wildcard plus
     "take the first" silently downloads the **audio** driver. That companion audio driver is
     rejected on Server 2025 (Code 52) and must never be installed here; VB-CABLE is this
     machine's virtual audio device. A second assertion rejects any resolved file whose name
     contains `Audio` or `ARM64` even if the pattern is overridden by hand. Note that `x86` in
     that project's asset names means *Intel-arch*, i.e. x64: the archive's `MttVDD.inf`
     declares `[Standard.NTamd64]`. Arm builds are named `ARM64` separately.
   - **Exit code 3010.** `nefconw` returns `ERROR_SUCCESS_REBOOT_REQUIRED` (3010) when the
     install succeeded but needs a restart — a **success** code, documented in nefcon's own
     README and returned from its devcon-emulation branch. It is treated as success and sets the
     reboot flag; only other nonzero codes are failures.
8. **RDS hardware adapter** — `bEnumerateHWBeforeSW=1` so Remote Desktop sessions enumerate the
   real GPU instead of the Microsoft Basic Render Driver. This only makes RDP *diagnostics*
   honest; it does not make RDP a place to run the game.
9. **Autologon** — Sysinternals, password prompted at runtime and stored by Windows as an LSA
   secret, never by us.
10. **Lockdown policies** — `NoLockScreen=1`, `InactivityTimeoutSecs=0`, screensaver off,
    Windows Update notify-only (`AUOptions=2`).
11. **Power plan** — high performance, `monitor-timeout-ac 0`.
12. **OpenSSH** — server enabled, PowerShell 7 as the default shell.
13. **Firewall** — RDP/SSH allowlist from `-AdminIP`; the wide-open built-in rules are disabled.
14. **Scheduled task `WASTED-Run`** — at logon of the autologon user, Interactive logon type
    (i.e. the console session, never session 0) → `run.ps1`.
15. **OBS profile** — copies `obs-profile/basic.ini` to
    `%APPDATA%\obs-studio\basic\profiles\WASTED\basic.ini` (backing up any existing copy, and
    skipping when the file is already byte-identical), so `run.ps1`'s `--profile WASTED`
    resolves to something. Checks that the file's `[General] Name` matches `-ObsProfileName`,
    since OBS matches profiles by that name and not by the folder name, and warns when the
    account running setup is not `-AutologonUser` (`%APPDATA%` is per-user). The scene
    collection stays a human step and is added to the manual-action list.
16. **Display stack verification** — prints every adapter with driver, mode and problem code,
    and warns loudly if no adapter is on a vendor driver or none reports a ≥ 1280×720 mode.

Ends with a numbered list of remaining manual actions and whether a reboot is required.

### fetch-shvdn.ps1

Fetches Script Hook V from dev-c.com (Referer-gated download; on failure it prints the manual
step — download the zip in a browser into `C:\wasted\downloads` and re-run) and the official
SHVDN nightly via the GitHub API (`-ShvdnTag latest` by default; refuses tags older than the
contract minimum `v3.7.0-nightly.189` unless `-Force`). Deploys SHV's whole `bin\` contents
(per its 2026 install instructions this includes `args.txt` carrying `-nobattleye -noBE`) plus
the SHVDN runtime files next to `GTA5.exe`, backs up whatever it replaces into
`C:\wasted\state\backup\shvdn\<timestamp>\`, and writes a version manifest (versions + SHA256
per file) to `C:\wasted\state\shvdn-manifest.json`. Record the pinned SHVDN tag in STATUS.md
and bridge/README per CONTRACTS.md.

```powershell
pwsh -File .\fetch-shvdn.ps1 -GameDir 'C:\Program Files (x86)\Steam\steamapps\common\Grand Theft Auto V'
```

### deploy-bridge.ps1

Copies `bridge\bin\Release\net48\WastedBridge.dll` + `Newtonsoft.Json.dll` into
`<GameDir>\scripts\` (created if missing), moving previous copies to
`<GameDir>\scripts\backup\<timestamp>\` first. Prints file version + SHA256 of what landed.
Build first: `dotnet build bridge -c Release`.

```powershell
pwsh -File .\deploy-bridge.ps1 -GameDir 'C:\Program Files (x86)\Steam\steamapps\common\Grand Theft Auto V'
```

### run.ps1

The startup chain, in the console session. Seven steps:

1. **Session and display preflight.** Refuses to launch unless `SESSIONNAME=Console`
   (`-AllowRdpSession` overrides for a deliberate experiment) — an RDP session gets the
   Microsoft Basic Render Driver, can never enter exclusive fullscreen
   (`DXGI_ERROR_NOT_CURRENTLY_AVAILABLE` over Terminal Server, by design), and takes the desktop
   with it on disconnect. Then refuses to launch unless some adapter reports a ≥ 1280×720 active
   desktop mode, naming 1024×768 as the classic no-display-target fallback.
2. **Game display settings.** Reads `Documents\Rockstar Games\GTA V\settings.xml` and reports
   `ScreenWidth` / `ScreenHeight` / `Windowed`; with `-EnforceDisplaySettings` it rewrites them
   to 1280 / 720 / 2 (borderless) after backing the file up. Elements are located by name
   anywhere in the document rather than by a hardcoded path, and anything the file does not
   already contain is reported rather than invented.
3. Steam `-silent` (skipped if running).
4. `steam.exe -applaunch 271590` plus `-GameArgs` (default
   `-nobattleye -windowed -borderless -width 1280 -height 720`), then polls for `GTA5.exe`
   (`-GameTimeoutS`, default 300 s — the spawned process exits and the Rockstar launcher sits
   mid-chain, so polling is the only correct wait). The durable home for `-nobattleye` remains
   the `args.txt` that `fetch-shvdn.ps1` deploys next to `GTA5.exe`; these arguments are
   belt-and-braces on top of it.
5. Harness (`python -m wasted_harness.main`, venv at `C:\wasted\venv` preferred, stdout/err into
   `C:\wasted\logs`).
6. OBS, started from its own bin directory with
   `--profile WASTED --collection WASTED --startreplaybuffer --disable-shutdown-check`
   (`-ObsProfile` / `-ObsSceneCollection`). The profile carries the 720p30 x264 canvas and the
   replay-buffer settings; the collection carries the Game Capture / CABLE Output / overlay
   sources. OBS resolves both **by name** and falls back silently to whatever was last used when
   a name does not exist, which is why `run.ps1` checks first and warns with the exact fix:
   profiles are matched on `[General] Name` inside each
   `%APPDATA%\obs-studio\basic\profiles\*\basic.ini`, collections on the `name` field inside each
   `%APPDATA%\obs-studio\basic\scenes\*.json`. `--disable-shutdown-check` suppressed the
   safe-mode dialog after a hard kill on OBS ≤ 31.x; OBS 32 removed the flag and ignores unknown
   arguments, so it stays harmless either way.
7. Informational bridge `/health` probe.

Each step is skipped when its process already runs, so re-running is safe.

```powershell
pwsh -File .\run.ps1
pwsh -File .\run.ps1 -GameTimeoutS 600 -SkipObs
pwsh -File .\run.ps1 -EnforceDisplaySettings
```

### watchdog.ps1

Liveness loop against `GET http://127.0.0.1:7777/health` (CONTRACTS §1; a 503
`online_session_active` counts as failure — the agent must never be online). Three consecutive
failures → kill chain (GTA5, OBS, harness) and relaunch via `run.ps1`; a fresh run of three
failures is required between relaunch attempts. If three relaunches inside a 30-minute window
still leave `/health` failing, it posts a `bridge_down` event
(`python -m wasted_harness.tools.post_event --type bridge_down --payload
'{"consecutive_failures": N}'` — the harness tool queues offline if Supabase is unreachable)
and keeps retrying on a slow cadence (default 300 s). It never exits silently: every probe,
kill, relaunch, and error is logged, and iteration errors are caught and survived. The
`bridge_up` recovery event is the harness's job, not the watchdog's.

Before every relaunch it logs **display forensics**: the owning session name, and every adapter
with its driver, active mode and problem code. If no adapter reports ≥ 1280×720 it says so
explicitly — a game that cannot come back because the indirect display driver dropped its
virtual monitor looks exactly like a game that crashed, and only that line tells them apart
at 3am.

```powershell
pwsh -File .\watchdog.ps1
pwsh -File .\watchdog.ps1 -IntervalS 10 -SlowIntervalS 600
```

### bridge-smoke.ps1

Exercises **every** CONTRACTS §1 endpoint against `-BaseUrl` (default `http://127.0.0.1:7777`):
`GET /state` + `/health` (shape-checked), `POST /task` for all 11 task types with
contract-valid params (movement targets are offsets from the live player position;
`follow_entity` uses a real handle from `/state.nearby` and is SKIPped with a reason when none
exists), an unknown-type negative check (expects 400), `/timescale` (0.5 then restored to 1.0),
`/control` (off then restored on), `/radio off`, `/horn 250 ms`, `/unstick` (200, or 409
`unstick_conditions_not_met` — both contract-conform), then `stop` and a final `/state` check
that `last_task.status` returned to `idle`. Prints a markdown PASS/FAIL table with response
excerpts — paste it into `docs/STATUS.md` — and exits nonzero on any FAIL. Requires the game
running with the bridge loaded.

```powershell
pwsh -File .\bridge-smoke.ps1
pwsh -File .\bridge-smoke.ps1 -BaseUrl http://127.0.0.1:7777
```

### detach-rdp.ps1

Elevated `tscon <session> /dest:console` (RESEARCH §7): leaves RDP while handing the desktop to
the console session so capture and the game keep running. On this server the console session's
display is the **virtual display driver's** monitor rather than a physical one — that does not
change the procedure, it is why the procedure matters. Aborts when not elevated (unelevated
`tscon` fails silently and locks the console) or when already on the console. Success closes
your RDP window — that is the intended behavior.

```powershell
pwsh -File .\detach-rdp.ps1
```

### common.ps1

Shared helper library, dot-sourced by every PowerShell 7 script here: environment guards
(`Assert-WastedEnvironment`), structured logging, transcript start/stop, elevation test, python
resolution, SHA256. Not runnable on its own. Also disables
`$PSNativeCommandUseErrorActionPreference` (see Conventions).

### obs-profile/

`basic.ini` — the OBS profile for 720p30 x264 on this CPU, plus `README.md` with the exact
scene-collection checklist, the encoder rationale, the Quick Sync upgrade path and the OBS-side
risk table. Section/key/value formats were verified against the OBS Studio 32.2.2 source that
reads the file (`frontend/widgets/OBSBasic.cpp` `InitBasicConfigDefaults`,
`frontend/widgets/OBSBasic.hpp` for the `SIMPLE_ENCODER_*` strings,
`frontend/widgets/OBSBasic_Profiles.cpp` for the path and the `[General] Name` key).

It is **wired up, not decoration**: `server-setup.ps1` step 15 installs it as the profile named
`WASTED`, `run.ps1` starts OBS with `--profile WASTED`, and the `obs` bootstrap phase refuses to
complete until that profile exists. The **scene collection JSON is deliberately not authored** —
the per-plugin settings keys could not be verified here and a malformed collection loads as an
empty scene — so it is built once in the UI, must be named `WASTED`, and is verified by name
(not by filename) before the phase completes.

---

## Known risks, with fallbacks

Ranked by how likely they are to eat delivery day.

| # | Risk | Symptom | Fallback |
|---|---|---|---|
| 1 | **GTA V may simply be too slow on a UHD 770.** No first-party statement exists either way, and there is no primary source for GTA V running *or* failing on a Server SKU. | Game runs but at unplayable frame rates | Drop to the lowest graphics preset at 1280×720; if still unplayable this is a hardware decision, not a scripting one — escalate to the human |
| 2 | **Intel driver will not bind** | `preflight.ps1` GPU-DRIVER = FAIL; only Microsoft basic adapters | `pnputil` will not force a lower-ranked driver onto a bound device: use Device Manager → Update driver → Browse → Let me pick → **Have Disk** and point at the INF. Do **not** edit the INF (breaks the catalog signature) and do **not** enable test-signing (kernel-mode driver on a 24/7 box; test mode is also documented to stop some games launching) |
| 3 | **No display target after the VDD install** | Desktop stuck at 1024×768, or `run.ps1` refuses to launch | Check the display device's problem code: **52** = trust the publisher certificate in `LocalMachine\TrustedPublisher`; **31** = driver did not load. Fallback A: the VDD project's own `Community Scripts/silent-install.ps1`. Fallback B: Amyuni `usbmmidd_v2` (`deviceinstaller64 install usbmmidd.inf usbmmidd`; `deviceinstaller64 enableidd 1`) — note it does **not** persist across reboot and needs a boot task. Avoid `parsec-vdd` (needs a resident process pinging it or displays unplug after ~1 s) and `IddSampleDriver` (self-signed; needs a trusted self-signed root on a production box) |
| 4 | **iGPU not enumerated at all** | `preflight.ps1` GPU-INTEL = FAIL | Request the free 3 h Hetzner KVM console (or use self-service vKVM rescue) and confirm the board posts video on the iGPU and that it is enabled in BIOS. Hetzner's iGPU "disable" on their Linux images is an OS-level `i915` blacklist + `nomodeset`, not a BIOS change — and none of that exists on Windows — so the expectation is that it is present |
| 5 | **Media Foundation missing** | Rockstar installer/launcher fails with a Windows Media Player / Media Feature Pack error | `Install-WindowsFeature Server-Media-Foundation -Restart`; on `0x800f081f` add `-Source wim:D:\sources\install.wim:2` or allow internet sourcing in the WSUS policy |
| 6 | **Rockstar Launcher / Social Club friction** | First-run failures, sign-in loops | Undocumented-but-not-blocked territory. Known fixes: install both VC++ architectures (sometimes downgrading to the 2015–2019 line), run the launcher **as Administrator**, or delete `Documents\Rockstar Games` and reinstall Social Club from rockstargames.com |
| 7 | **x264 and the game contend for the CPU** | OBS reports dropped/skipped frames | Lower the bitrate to 2500, then 720p24; try `qsv` only after the Intel driver is verified, and re-measure before committing |
| 8 | **Virtual gamepad is a dead end** | — | Expected, and already the plan of record: ViGEmBus is archived and its installer explicitly refuses every Windows Server SKU; its successor is commercial-only. Manual-control primitives are SendInput keyboard/mouse (CONTRACTS §2), with SHVDN control natives for analog input. Do not spend delivery day here |
| 9 | **Steam / OBS are not supported on Server SKUs** | Blank web panes in Steam; OBS misbehaving through RDP | Install Media Foundation *before* Steam; do OBS setup over RDP but run the stream from the console session only |
| 10 | **Game launched from an RDP session** | Software rendering, no fullscreen, capture dies on disconnect | `run.ps1` refuses to launch outside the console session. Always leave RDP with `detach-rdp.ps1` |

---

## Verification

**Static, on the dev Mac (real commands, real output):**

- Every `.ps1` parses clean via `[System.Management.Automation.Language.Parser]::ParseFile`
  under `$HOME/.powershell/pwsh` (PowerShell 7.4.6) — 0 errors across all 10 files.
- `Invoke-ScriptAnalyzer -Severity Error` — 0 errors across the package.
- All 9 runnable scripts abort loudly with exit 1 on macOS (exercised, one run each).
- Secret / mock / TODO / placeholder greps are clean.
- **Release-asset resolution, against the live GitHub API.** `Get-WastedGitHubAsset` (the real
  function text, lifted out of `server-setup.ps1` by the parser) run against
  `VirtualDrivers/Virtual-Display-Driver@25.7.23`: the old `*Driver*Only*.zip` pattern now throws
  `pattern '*Driver*Only*.zip' is ambiguous — it matches 3 assets (VirtualAudioDriver-x86…,
  VirtualDisplayDriver-ARM64…, VirtualDisplayDriver-x86…)`, and the shipped default resolves to
  `VirtualDisplayDriver-x86.Driver.Only.zip` (sha256 `e2421069…e418b3a`), whose contents are
  `mttvdd.cat, MttVDD.dll, MttVDD.inf, vdd_settings.xml`. `nefcon_v*.zip` still resolves to
  exactly one asset.
- **`nefconw` exit-code handling.** The real branch from `server-setup.ps1` executed for exit 0,
  3010 and 1: `0 → rebootRequired=True, 0 warnings`; `3010 → rebootRequired=True, 0 warnings`;
  `1 → rebootRequired=False, 1 warning + 1 manual action`. 3010 is
  `ERROR_SUCCESS_REBOOT_REQUIRED`, returned by nefcon's devcon-emulation branch on success
  (`src/NefConUtil.cpp`: `return (rebootRequired) ? ERROR_SUCCESS_REBOOT_REQUIRED : EXIT_SUCCESS`).
- **`Wait-ForHuman` outcomes.** The real function driven through every answer: `SKIP → 'skipped'`,
  `DONE` + passing verify `→ 'verified'`, `DONE` + failing verify → re-prompts, `ABORT` → throws,
  `-NonInteractive → 'deferred'`. SKIP and DONE no longer share a value, and a SKIPped phase is
  confirmed absent from `bootstrap-state.json` so the next pass re-runs it; the summary block
  (real text) reports it and forces exit code 2.
- **`vdd_settings.xml`.** The real `Write-VddSettings` executed and the output parsed back:
  resolution order `1280x720@60, 1920x1080@60` (1280×720 first), `<gpu><friendlyname>` =
  `Intel(R) UHD Graphics 770`, one monitor — and every element name it writes also appears in the
  `vdd_settings.xml` shipped inside the real driver archive (no invented elements).
- **OBS profile/collection lookups.** The real `Test-ObsProfileInstalled` /
  `Test-ObsSceneCollectionInstalled` from `run.ps1` against a throwaway `%APPDATA%`: both warn
  when nothing is installed, both find the real `obs-profile/basic.ini` once copied into place,
  and a profile folder named `WASTED2` whose `[General] Name` is `Untitled` is correctly **not**
  matched — which is the behavior OBS itself has.
- **bootstrap resume state machine** still round-trips (2 phases saved and reloaded) and still
  degrades to "nothing done" on a corrupt state file.
- `preflight.ps1`'s PowerShell 5.1 claim is checked by an AST scan: no ternaries, no `&&`/`||`
  chains, no `??`/`?.`, no bare `$IsWindows`, no PS7-only cmdlet parameters, no 3-argument
  `Join-Path`. Its `Get-Prop` and `Test-TcpEndpoint` helpers are exercised against real inputs
  (reachable host, unresolvable host, closed port).
- `run.ps1`'s settings.xml rewriter and `bootstrap.ps1`'s resume state machine are exercised
  end-to-end (20 assertions: report-only leaves the file byte-identical, `-EnforceDisplaySettings`
  rewrites all three values and backs up, second run is a no-op, missing elements are never
  fabricated, malformed XML warns instead of throwing, completed phases survive a JSON round
  trip, a corrupt state file degrades to "nothing done", `-StartAt` resumes at the right phase).
- `obs-profile/basic.ini` section and key names checked against the OBS Studio 32.2.2 source that
  reads the file, and the `--profile` / `--collection` / `--startreplaybuffer` flags against
  `frontend/obs-main.cpp`'s argument parser (see `obs-profile/README.md` for the exact files).

**Not verified — and cannot be, until the server is delivered:** every Windows-only behavior.
The Intel INF install, the virtual display driver install, Media Foundation, the DirectX and
VC++ installs, dxdiag parsing, the session and display checks, the settings.xml rewrite, and the
whole bootstrap sequence are **authored and statically checked, not executed**. Real verification
happens only on the server (CLAUDE.md "Where things run"): `bridge-smoke.ps1` output and the
Phase 0a evidence go into `docs/STATUS.md`.
