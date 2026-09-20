#Requires -Version 7.0
<#
.SYNOPSIS
    One command from an RDP session: predictions on, game unpaused, session handed to
    the console, agent started.

.DESCRIPTION
    RUNBOOK §6 is a seven-step manual procedure ending in "now ask for the agent to be
    started", because the agent must NOT be running while an RDP display adapter exists:
    dxcam binds to whatever display is present when it starts, and the Remote Desktop
    adapter disappears the moment you disconnect. Starting it before you leave captures a
    display that no longer exists; the site then shows a live heartbeat over a dead picture,
    which is worse than OFF AIR because it is dishonest.

    The usual resolution is an operator disconnecting and then someone starting the agent
    over SSH. When SSH is not available that is a deadlock, and this script breaks it: it
    runs INSIDE the session being redirected, so `tscon` closes the RDP window while this
    process keeps running on the console display. It then waits for the display to settle
    and starts the agent into the correct adapter.

    Elevation is required. An unelevated tscon fails silently AND locks the console
    (detach-rdp.ps1 documents the same trap).

.EXAMPLE
    pwsh -File C:\wasted\repo\scripts\go-live.ps1
#>
[CmdletBinding()]
param(
    # Seconds to wait after tscon before starting the agent. The console switch is not
    # instant; starting into a half-switched display is the failure this guards.
    [int]$SettleSeconds = 25,
    # Skip the harness start (game + unpause + detach only).
    [switch]$SkipAgent
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$logDir = 'C:\wasted\logs'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$transcript = Join-Path $logDir "go-live_$stamp.log"
Start-Transcript -Path $transcript -Force | Out-Null

function Step([string]$m) { Write-Host "[go-live] $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "[go-live] WARNING: $m" -ForegroundColor Yellow }

try {
    # --- elevation, before anything that depends on it -----------------------------------
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Not elevated. Right-click Start -> Terminal (Admin) and run this again. An unelevated tscon fails silently and locks the console.'
    }

    # --- must be an RDP session: tscon needs a session to redirect ------------------------
    $session = $env:SESSIONNAME
    if (-not $session) { throw 'SESSIONNAME is empty. Run this in the RDP session itself, not over SSH.' }
    if ($session -eq 'Console') { Warn 'Already on the console; tscon will be skipped.' }
    Step "session: $session"

    # --- 1. predictions on ----------------------------------------------------------------
    # The layer ships off per deployment (WASTED_PREDICTIONS_ENABLED, default false), so
    # without this the agent plays and never creates a single prediction.
    $envFile = Join-Path $repo 'harness\.env'
    if (-not (Test-Path -LiteralPath $envFile)) { throw "Missing $envFile — the harness cannot start without its secrets." }
    $kept = @(Get-Content -LiteralPath $envFile | Where-Object { $_ -notmatch '^\s*WASTED_PREDICTIONS_ENABLED\s*=' })
    Set-Content -LiteralPath $envFile -Value ($kept + 'WASTED_PREDICTIONS_ENABLED=true')
    Step '1/5 predictions enabled'

    foreach ($required in 'ANTHROPIC_API_KEY', 'SUPABASE_URL', 'SUPABASE_SECRET_KEY') {
        if (-not (Select-String -LiteralPath $envFile -Pattern "^\s*$required\s*=\s*\S" -Quiet)) {
            Warn "$required is missing or empty in harness\.env — the agent will not work without it."
        }
    }

    # --- 2. locate the interpreter that has the package ------------------------------------
    $python = if (Test-Path -LiteralPath 'C:\wasted\venv\Scripts\python.exe') { 'C:\wasted\venv\Scripts\python.exe' }
              else { (Get-Command python -ErrorAction Stop).Source }
    Step "2/5 python: $python"

    # --- 3. unpause: GTA V ships Pause Game On Focus Loss ON -------------------------------
    $foreground = Join-Path $PSScriptRoot 'assert-game-foreground.ps1'
    if (Test-Path -LiteralPath $foreground) {
        & $foreground
        Step '3/5 game focused (unpaused)'
    } else {
        Warn "assert-game-foreground.ps1 not found; skipping the pre-detach unpause."
    }

    # --- 4. hand the desktop to the console ------------------------------------------------
    # The RDP window closes here. THIS PROCESS KEEPS RUNNING: it lives in the session being
    # redirected, which is precisely why the agent can be started afterwards from here.
    if ($session -ne 'Console') {
        Step "4/5 tscon $session -> console (your RDP window closes now; this script continues)"
        Start-Sleep -Seconds 1
        & tscon.exe $session /dest:console
        if ($LASTEXITCODE -ne 0) { throw "tscon failed with exit code $LASTEXITCODE" }
    } else {
        Step '4/5 already on console; nothing to redirect'
    }

    Step "waiting ${SettleSeconds}s for the console display to settle"
    Start-Sleep -Seconds $SettleSeconds

    # Re-assert focus: the console switch can leave the game without the foreground window,
    # which re-freezes it under Pause Game On Focus Loss.
    if (Test-Path -LiteralPath $foreground) { & $foreground }

    # --- 5. start the agent -----------------------------------------------------------------
    if ($SkipAgent) {
        Step '5/5 -SkipAgent given; agent not started'
    } else {
        $already = Get-CimInstance -ClassName Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'wasted_harness\.main' }
        if ($already) {
            Warn "agent already running (pid $($already.ProcessId)); not starting a second one"
        } else {
            $harnessDir = Join-Path $repo 'harness'
            $proc = Start-Process -FilePath $python -ArgumentList '-m', 'wasted_harness.main' `
                -WorkingDirectory $harnessDir -PassThru `
                -RedirectStandardOutput (Join-Path $logDir "harness_$stamp.out.log") `
                -RedirectStandardError  (Join-Path $logDir "harness_$stamp.err.log")
            Step "5/5 agent started (pid $($proc.Id))"
            Step "logs: $logDir\harness_$stamp.out.log / .err.log"

            # Give it long enough to either come up or die, then say which.
            Start-Sleep -Seconds 20
            if ($proc.HasExited) {
                Warn "agent EXITED with code $($proc.ExitCode). Last lines of stderr:"
                Get-Content (Join-Path $logDir "harness_$stamp.err.log") -Tail 30 -ErrorAction SilentlyContinue
            } else {
                Step 'agent still running after 20s'
            }
        }
    }

    # --- report -------------------------------------------------------------------------------
    Step '--- bridge health (game_fps must CHANGE between the two samples) ---'
    foreach ($i in 1, 2) {
        try {
            $h = Invoke-RestMethod -Uri 'http://127.0.0.1:7777/health' -TimeoutSec 5
            Write-Host "  sample ${i}: tick_hz=$($h.tick_hz) game_fps=$($h.game_fps)"
        } catch {
            Warn "  sample ${i}: /health unreachable — $($_.Exception.Message)"
        }
        if ($i -eq 1) { Start-Sleep -Seconds 3 }
    }

    Step 'DONE. Check https://wanted.money — OFF AIR should clear within a minute or two.'
    Step "full transcript: $transcript"
}
catch {
    Write-Host "[go-live] FAILED: $($_.Exception.Message)" -ForegroundColor Red
    throw
}
finally {
    Stop-Transcript | Out-Null
}
