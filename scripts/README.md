# WANTED — Windows server ops scripts

Owner: Fable / ops executor only. PowerShell 7 scripts for the Windows Server 2025 game server
(PLAN Phase 0a, RESEARCH.md D9/D10). Nothing here runs on the dev Mac except parsing/lint —
every script opens with an environment guard that aborts loudly unless it is on the Windows
game server (`-Force` overrides the marker checks where that is sensible).

Conventions (all scripts):

- PowerShell 7 (`#Requires -Version 7.0`), `Set-StrictMode -Version Latest`.
- Transcript logging into `C:\wasted\logs\<script>_<timestamp>.log`.
- `-WhatIf` supported wherever state changes (setup, fetch, deploy, run, watchdog, detach).
- **No secrets.** Nothing here stores passwords, stream keys, or API keys. The Autologon
  password is typed at runtime and lands only in the Windows LSA secret store; the OBS
  websocket password and Supabase/Anthropic keys live in the harness `.env`, never here.
- Ports and paths come from `docs/CONTRACTS.md` (bridge `127.0.0.1:7777`, overlay `7788`,
  GTA V Legacy Steam app `271590`).
- `common.ps1` is the shared helper library (guards, logging, transcript, python resolution).
  It is dot-sourced by every script and is not runnable on its own.

## Bootstrap (fresh server, once)

Windows Server 2025 ships only Windows PowerShell 5.1 — install PowerShell 7 first, from an
elevated 5.1 prompt:

```powershell
winget install --exact --id Microsoft.PowerShell --silent --accept-package-agreements --accept-source-agreements
git clone <repo> C:\wasted\repo   # after server-setup created C:\wasted, or clone anywhere and pass -RepoDir
```

## Order of use

| Order | Script | When |
|---|---|---|
| 1 | `server-setup.ps1` | Once per server (idempotent — re-run after driver installs to verify) |
| 2 | `fetch-shvdn.ps1` | After the game is installed; again whenever SHV/SHVDN must be updated |
| 3 | `deploy-bridge.ps1` | After every bridge build |
| 4 | `run.ps1` | At logon (scheduled task registers it); manually after maintenance |
| 5 | `bridge-smoke.ps1` | After every deploy, with the game running — output goes into STATUS.md |
| 6 | `watchdog.ps1` | Started alongside the stream (own console/pwsh, or its own scheduled task) |
| — | `detach-rdp.ps1` | Every time you leave an RDP session and the stream must keep running |

## Scripts

### server-setup.ps1

Idempotent Phase 0a bring-up. `-AdminIP` is mandatory (IP or CIDR, comma-separated list) —
inbound RDP/SSH get restricted to it, so double-check it.

```powershell
pwsh -File .\server-setup.ps1 -AdminIP 203.0.113.7            # real run
pwsh -File .\server-setup.ps1 -AdminIP 203.0.113.7 -WhatIf    # dry run
```

Covers: winget toolchain (PowerShell 7, Git, Python 3.12, Node LTS, .NET SDK 8 + Framework 4.8
targeting pack, OBS, Steam, 7-Zip); Windows Audio + Endpoint Builder services; VB-CABLE fetch +
staged install (driver click is interactive — documented in the summary output; reboot after);
Sysinternals Autologon (password prompted at runtime, stored by Windows as an LSA secret, never
by us); NoLockScreen / InactivityTimeoutSecs=0 / screensaver-off / Windows Update notify-only
(AUOptions 2) registry policy; high-performance power plan + monitor timeout 0; OpenSSH server
with PowerShell 7 default shell; firewall allowlist (custom RDP/SSH rules from `-AdminIP`,
built-in wide-open rules disabled); scheduled task `WASTED-Run` (console session, at logon →
`run.ps1`); NVIDIA driver step (documented manual install, verified via `nvidia-smi` when
present); the `C:\wasted` layout (`logs`, `state`, `downloads`, `tools`).

Ends with a numbered list of remaining manual actions (VB-CABLE click + reboot, NVIDIA driver,
SSH keys, Steam/Rockstar one-time logins, OBS websocket + replay-buffer + CABLE-Output config).

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

The startup chain, in the console session: Steam `-silent` (skipped if running) →
`steam.exe -applaunch 271590` → poll for `GTA5.exe` (`-GameTimeoutS`, default 300 s — the
spawned process exits and the Rockstar launcher sits mid-chain, so polling is the only correct
wait) → harness (`python -m wasted_harness.main`, venv at `C:\wasted\venv` preferred, stdout/err
into `C:\wasted\logs`) → OBS (`--startreplaybuffer --disable-shutdown-check`, started from its
own bin directory). Each step is skipped when its process already runs, so re-running is safe.
Finishes with an informational bridge `/health` probe.

```powershell
pwsh -File .\run.ps1
pwsh -File .\run.ps1 -GameTimeoutS 600 -SkipObs
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
the physical console (HDMI emulator) so capture and the game keep running. Aborts when not
elevated (unelevated tscon fails silently and locks the console) or when already on the
console. Success closes your RDP window — that is the intended behavior.

```powershell
pwsh -File .\detach-rdp.ps1
```

## Verification

Static (dev machine): every `.ps1` parses clean via
`[System.Management.Automation.Language.Parser]::ParseFile` under PowerShell 7, and a secret
grep stays empty. Real verification happens only on the server with the game running
(CLAUDE.md "Where things run"): `bridge-smoke.ps1` output and the Phase 0a evidence go into
`docs/STATUS.md`.
