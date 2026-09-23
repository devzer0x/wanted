#Requires -Version 7.0
<#
.SYNOPSIS
    Bring the box up to the checked-out commit and turn the prediction layer on.

.DESCRIPTION
    Predictions are created on this machine and nowhere else: no web deploy and no database
    change can make one appear (RUNBOOK §5.7 step 2). This is that step, as one command, for
    the case where SSH to the box is unavailable and the only way in is a Remote Desktop
    session.

    It builds the harness package FROM THE REPO CHECKOUT already on this machine rather than
    from an uploaded file, so there is nothing to transfer, and then hands over to
    `deploy-all.ps1`, which is the tested path: it refuses to run unless the game is alive and
    ticking, backs the current package up, restores that backup if the new code does not
    import, and starts exactly one harness. Nothing here touches OBS, the game process or the
    stream, and nothing here starts the game.

    It also restarts sshd on the way out. SSH to this box has been refusing connections all
    day (`Connection reset`), and a restart of the service is the one fix that can only be
    applied from a session that is already inside — which is exactly where this script runs.

    Two things deploy-all.ps1 does that are wrong for THIS entry point, and what is done about
    them:

    * It hot-reloads `C:\wasted\tmp\WastedBridge.dll` whenever that file exists. Predictions
      need no bridge change, and a DLL left there by an earlier deploy can be OLDER than the
      bridge that is running (1.7.0 was uploaded on 2026-09-03; the repo is at 1.9.0). So a
      DLL found there is renamed aside first, and the running bridge is left alone.
    * It starts the agent in the session it runs in, which here is the Remote Desktop session.
      dxcam binds to the display that exists when the agent starts, and the RDP adapter
      disappears on disconnect (RUNBOOK §6) — the stream would show a live heartbeat over a
      dead picture. So deploy-all's run is only the proof that the new code imports: it runs
      with the predictions switch still as it was, is then stopped, and only THEN is the switch
      turned on, the session handed to the console with tscon (the RDP window closes; this
      script keeps running) and the agent started again there, in this same session, which is
      checked. Must be run as Administrator, the account deploy-all's task runs as.
    * If deploy-all fails after stopping the agent (its import gate restores the old code), the
      restored build is started again the same way, with the switch untouched, rather than
      leaving the show without an agent.
    Everything after the RDP window closes is in the transcript under C:\wasted\logs.

.PARAMETER Repo
    The checkout to deploy from. Cloned if missing, fast-forwarded if present.

.PARAMETER SkipSshRestart
    Leave sshd alone.

.PARAMETER SettleSeconds
    Seconds to wait after tscon before starting the agent, so it binds to the console display
    rather than a half-switched one.

.EXAMPLE
    pwsh -File C:\wasted\repo\scripts\deploy-predictions.ps1
#>
[CmdletBinding()]
param(
    [string]$Repo = 'C:\wasted\repo',
    [string]$RepoUrl = 'https://github.com/devzer0x/wanted.git',
    [string]$HarnessDir = 'C:\wasted\harness',
    [switch]$SkipSshRestart,
    [int]$SettleSeconds = 25
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Step([string]$m) { Write-Host "[predictions] $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "[predictions] WARNING: $m" -ForegroundColor Yellow }

# The agent's own processes: the venv trampoline and its worker, both `-m wasted_harness.main`.
# Narrower than deploy-all.ps1's 'wasted_harness' so the watchdog's short-lived
# `-m wasted_harness.tools.post_event` is never caught.
function Get-AgentProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'wasted_harness\.main' })
}

function Stop-Agent {
    Get-AgentProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
    $left = Get-AgentProcesses
    if ($left.Count -gt 0) {
        throw "could not stop the agent (pid $($left.ProcessId -join ', ')); stopping here so a second one is never started beside it."
    }
}

# Hands this session to the console, where the agent's screen capture has a display that
# survives. Only meaningful from Remote Desktop; the RDP window closes and this keeps running.
function Move-SessionToConsole([string]$Foreground) {
    Step "    tscon $session -> console: your Remote Desktop window closes now and this keeps running"
    Start-Sleep -Seconds 1
    & tscon.exe $session /dest:console
    if ($LASTEXITCODE -ne 0) {
        throw "tscon failed with exit code $LASTEXITCODE. The agent is STOPPED; from the console run: Start-ScheduledTask -TaskName 'WASTED-Harness'"
    }
    Start-Sleep -Seconds $SettleSeconds
    # GTA V ships "Pause Game On Focus Loss" on, and the console switch can leave it unfocused.
    # (Called only now: before tscon it sees the operator's active RDP row and does nothing.)
    if (Test-Path -LiteralPath $Foreground) {
        & $Foreground
        $fg = $LASTEXITCODE
        if ($fg -in 2, 3, 4) {
            Warn "assert-game-foreground.ps1 exited $fg (2 desktop locked, 3 no game window, 4 focus change not verified): the game may be paused."
        }
    }
    else { Warn 'assert-game-foreground.ps1 not found; the game may be left paused.' }
}

# Starts the task deploy-all.ps1 registered (hidden, unbuffered, C:\wasted\harness) and proves
# it came up once, in THIS session when a session is known.
function Start-Agent([string]$What) {
    # The task's action reuses deploy-all's log path, which a restart would overwrite: keep the
    # run it replaces.
    Get-ChildItem 'C:\wasted\logs' -Filter 'harness-*.log*' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch '\.before-' } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 2 |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination "$($_.FullName).before-$stamp" -Force }
    Start-ScheduledTask -TaskName 'WASTED-Harness'
    Start-Sleep -Seconds 20
    $procs = Get-AgentProcesses
    if ($procs.Count -eq 0) {
        throw "the $What did not start. From the console run: Start-ScheduledTask -TaskName 'WASTED-Harness'"
    }
    $here = (Get-Process -Id $PID).SessionId
    if ($session) {
        $elsewhere = @($procs | Where-Object { $_.SessionId -ne $here })
        if ($elsewhere.Count -gt 0) {
            throw "the $What started in session $($elsewhere[0].SessionId), not this one ($here): its screen capture has no display there. Stop it and start WASTED-Harness from the console."
        }
    }
    if ($procs.Count -ne 2) { Warn "$($procs.Count) agent processes running (2 is correct: trampoline + worker)." }
    Step "    $What running in session $($procs[0].SessionId): $($procs.Count) processes"
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
New-Item -ItemType Directory -Force -Path 'C:\wasted\logs' | Out-Null
# The RDP window closes at step 7 while this keeps running, so everything goes to a file too.
$transcript = "C:\wasted\logs\deploy-predictions_$stamp.log"
Start-Transcript -Path $transcript -Force | Out-Null
# Captured now: after tscon this process is on the console, but the decision was made here.
$session = $env:SESSIONNAME

try {
    # deploy-all.ps1 registers scheduled tasks, and tscon needs a full admin token too (an
    # unelevated tscon fails silently AND locks the console — detach-rdp.ps1).
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Not elevated. Right-click Start -> Terminal (Admin) and run this again.'
    }
    Step "session: $(if ($session) { $session } else { '(none: SSH?)' }); transcript: $transcript"
    # deploy-all.ps1 registers WASTED-Harness for the Administrator account (Interactive), so the
    # agent can only be moved onto the console display if THIS session is Administrator's.
    if ($session -and $env:USERNAME -ne 'Administrator') {
        throw "signed in as '$env:USERNAME'. The agent's task runs as Administrator: sign in to Remote Desktop as Administrator and run this again. Nothing was changed."
    }

    # --- 1. the code ----------------------------------------------------------------------
    if (Test-Path -LiteralPath (Join-Path $Repo '.git')) {
        Step "1/7 updating $Repo"
        git -C $Repo fetch --quiet origin main
        git -C $Repo checkout --quiet main
        git -C $Repo reset --hard --quiet origin/main
    }
    else {
        Step "1/7 cloning $RepoUrl -> $Repo (public repo; no credentials needed)"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Repo) | Out-Null
        git clone --quiet $RepoUrl $Repo
    }
    Step "    at $(git -C $Repo log --oneline -1)"

    # The prediction layer must actually be in what we just fetched, or the rest is theatre.
    foreach ($required in 'harness\wasted_harness\predictions\baserate.py',
                          'harness\wasted_harness\predictions\generator.py') {
        if (-not (Test-Path -LiteralPath (Join-Path $Repo $required))) {
            throw "$required is missing from the checkout — wrong branch or a failed fetch."
        }
    }

    # --- 2. the package deploy-all.ps1 expects --------------------------------------------
    New-Item -ItemType Directory -Force -Path 'C:\wasted\tmp' | Out-Null
    $tgz = 'C:\wasted\tmp\harness-today.tgz'
    Step "2/7 packaging wasted_harness -> $tgz"
    # Same command docs/go-live.md documents, run here instead of on a dev machine. __pycache__
    # is excluded because a .pyc compiled against another interpreter is worse than no .pyc.
    Push-Location (Join-Path $Repo 'harness')
    try { tar czf $tgz --exclude='__pycache__' --exclude='*.pyc' wasted_harness }
    finally { Pop-Location }
    if (-not (Test-Path -LiteralPath $tgz)) { throw "tar produced no $tgz" }
    Step "    $([math]::Round((Get-Item $tgz).Length / 1KB)) KB"

    # --- 3. keep the running bridge -------------------------------------------------------
    # deploy-all.ps1 hot-reloads C:\wasted\tmp\WastedBridge.dll whenever it exists. Nothing in
    # the prediction layer needs a bridge change, and a DLL left behind by an earlier deploy
    # may be older than the one running, so it is renamed aside, never deleted.
    $tmpDll = 'C:\wasted\tmp\WastedBridge.dll'
    try { $running = (Invoke-RestMethod 'http://127.0.0.1:7777/health' -TimeoutSec 5).version } catch { $running = 'unreachable' }
    if (Test-Path -LiteralPath $tmpDll) {
        $aside = "$tmpDll.set-aside-$stamp"
        Move-Item -LiteralPath $tmpDll -Destination $aside
        Step "3/7 set aside $tmpDll -> $aside; running bridge $running is kept"
    }
    else { Step "3/7 no bridge DLL waiting in C:\wasted\tmp; running bridge $running is kept" }

    $envFile = Join-Path $HarnessDir '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { throw "$envFile is missing — this is not a provisioned game server." }
    $foreground = Join-Path $Repo 'scripts\assert-game-foreground.ps1'

    # --- 4. the tested deploy path --------------------------------------------------------
    # The predictions switch is NOT on yet: deploy-all starts the new build in THIS session to
    # prove it imports, and a build that is about to be stopped should not ask a question.
    $deployAll = Join-Path $Repo 'scripts\deploy-all.ps1'
    if (-not (Test-Path -LiteralPath $deployAll)) { throw "Missing $deployAll" }
    Step '4/7 handing over to deploy-all.ps1 (it refuses to run unless the game is ticking)'
    & (Get-Process -Id $PID).Path -NoProfile -File $deployAll
    $deployExit = $LASTEXITCODE

    # --- 5. sshd, so this does not have to happen in person next time ---------------------
    if (-not $SkipSshRestart) {
        try {
            Restart-Service sshd -ErrorAction Stop
            Step '5/7 sshd restarted'
        }
        catch { Warn "could not restart sshd ($($_.Exception.Message)); remote access may still refuse connections." }
    }
    else { Step '5/7 -SkipSshRestart given' }

    if ($deployExit -ne 0) {
        # deploy-all exits before stopping anything (game paused, bridge down) or AFTER stopping
        # the agent (package missing, or the new code failed its import gate and the old code was
        # restored). Only the second leaves the show with no agent, and that is fixed here: the
        # RESTORED build goes back on, on the console, with the predictions switch untouched.
        if ((Get-AgentProcesses).Count -gt 0) {
            throw "deploy-all.ps1 exited $deployExit before replacing anything; the agent that was running is still running, unchanged."
        }
        Warn "deploy-all.ps1 exited $deployExit after stopping the agent; the old code is in place. Starting it again."
        if ($session -and $session -ne 'Console') { Move-SessionToConsole $foreground }
        Start-Agent 'restored build'
        throw "deploy-all.ps1 exited ${deployExit}: the new code was NOT deployed and predictions were NOT switched on. The previous build is playing again."
    }

    # --- 6. stop the proving run, then the switch -----------------------------------------
    # deploy-all started the new build in this session. Inside Remote Desktop its screen capture
    # is bound to the RDP display adapter, which vanishes on disconnect (RUNBOOK §6), so it is
    # stopped before anything else, and started again from the console below.
    Stop-Agent
    # Appended to the SERVER's own .env, which holds the real keys and is never overwritten from
    # a dev machine. Idempotent: the last assignment wins, so a stale `false` is replaced.
    $kept = @(Get-Content -LiteralPath $envFile | Where-Object { $_ -notmatch '^\s*WASTED_PREDICTIONS_ENABLED\s*=' })
    Set-Content -LiteralPath $envFile -Value ($kept + 'WASTED_PREDICTIONS_ENABLED=true')
    Step '6/7 proving run stopped; WASTED_PREDICTIONS_ENABLED=true'

    # --- 7. the agent, on the console display ---------------------------------------------
    if ($session -and $session -ne 'Console') {
        Step "7/7 moving to the console, then starting the agent there"
        Move-SessionToConsole $foreground
    }
    else { Step '7/7 not a Remote Desktop session; starting the agent without tscon' }
    Start-Agent 'agent'
    $fps = @()
    foreach ($i in 1, 2) {
        try {
            $h = Invoke-RestMethod 'http://127.0.0.1:7777/health' -TimeoutSec 5
            $fps += $h.game_fps
            Step "    bridge sample ${i}: tick_hz=$($h.tick_hz) game_fps=$($h.game_fps)"
        } catch { Warn "bridge sample ${i}: unreachable" }
        if ($i -eq 1) { Start-Sleep -Seconds 3 }
    }
    if ($fps.Count -eq 2 -and $fps[0] -eq $fps[1]) {
        Warn 'game_fps did not change between samples: the game may be paused (RUNBOOK §6).'
    }

    Write-Host ''
    Step 'DONE. A question is asked at most every 5 minutes, and only while its odds, re-measured'
    Step 'from the last 3 hours of real play, are between 20% and 80%. After a stretch where he'
    Step 'finished nothing, expect the first one after 1-2 hours of normal play, not minutes.'
    Step 'Rewards stay OFF: rounds will settle and credit nobody until the operator says so.'
}
catch {
    Write-Host "[predictions] FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    Stop-Transcript | Out-Null
}
