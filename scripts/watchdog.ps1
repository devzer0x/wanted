#Requires -Version 7.0
<#
.SYNOPSIS
    24/7 liveness watchdog: polls the bridge /health endpoint, relaunches the chain on failure.
.DESCRIPTION
    Loop: GET http://127.0.0.1:7777/health (CONTRACTS.md §1 — connection refused means
    game/bridge down; 503 means an online session was detected, which is equally
    unacceptable for the stream). On -FailThreshold consecutive failures the game chain is
    killed and relaunched via run.ps1. If -MaxRelaunchesPerWindow relaunches inside
    -RelaunchWindowMin minutes still leave /health failing, the watchdog posts a
    `bridge_down` event ({"consecutive_failures": N}) through
    `python -m wasted_harness.tools.post_event` and keeps retrying on a slow cadence.
    It never exits silently: every probe, kill, relaunch, and error is logged, and any
    unexpected exception is caught, logged, and survived.

    Before every relaunch it records the state of the two things that actually break on this
    server (Intel UHD 770 iGPU, no monitor, no HDMI emulator): which session owns the desktop,
    and whether any display adapter still reports a usable desktop mode. A game that will not
    come back because the indirect display driver dropped its virtual monitor looks exactly
    like a game that crashed, and only that logged line tells them apart at 3am.

    PAUSED-BUT-ALIVE. There is a second failure that the HTTP status alone cannot see: the game
    process is up, the bridge answers 200, and yet the game is FROZEN — because its window lost
    the foreground and GTA V's PauseOnFocusLoss preference stopped it, or because the session was
    parked and came back with nothing focused. Everything looks healthy and the stream shows a
    still frame. So the body is parsed, not just the status:

      * tick_hz at or below zero. The bridge zeroes it whenever the last script tick is older
        than 2000 ms, so this is a real staleness signal.
      * game_fps not advancing between polls. This one has to be judged on MOVEMENT, never on
        being non-zero: game_fps is written only inside the tick and passed through /health raw,
        with no staleness gate, so when the script thread stops it FREEZES at its last value
        instead of falling to zero.

    On -PauseThreshold consecutive paused samples it escalates, logging every step:
      1. re-run the console re-attach (the WASTED-ConsoleKeepalive task, which is SYSTEM and so
         actually has the privilege for tscon),
      2. restore the game window's foreground (the WASTED-ForegroundAssert task, which is
         Interactive and so actually runs in the session that owns the window),
      3. alert in the log and back off.
    It does NOT relaunch, kill or restart anything on this path: the game is alive, and killing a
    live game because a window lost focus would turn a two-second fix into a two-minute outage.
    It touches no OBS setting and never starts, stops or restarts the harness.
.EXAMPLE
    pwsh -File .\watchdog.ps1
.EXAMPLE
    pwsh -File .\watchdog.ps1 -IntervalS 10 -SlowIntervalS 600
.EXAMPLE
    pwsh -File .\watchdog.ps1 -PauseThreshold 4 -PauseCooldownMin 10
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$BaseUrl = 'http://127.0.0.1:7777',
    [int]$IntervalS = 15,
    [int]$ProbeTimeoutS = 5,
    [int]$FailThreshold = 3,
    [int]$RelaunchWindowMin = 30,
    [int]$MaxRelaunchesPerWindow = 3,
    [int]$SlowIntervalS = 300,
    [ValidateRange(2, 100)][int]$PauseThreshold = 3,
    [ValidateRange(1, 1440)][int]$PauseCooldownMin = 5,
    [string]$KeepaliveTaskName = 'WASTED-ConsoleKeepalive',
    [string]$ForegroundTaskName = 'WASTED-ForegroundAssert',
    [string[]]$GameProcessNames = @('GTA5', 'GTA5_Enhanced'),
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe = '',
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
. $commonPath

Assert-WastedEnvironment -ScriptName 'watchdog.ps1' -RequireWastedRoot -Force:$Force
Start-WastedTranscript -Name 'watchdog' | Out-Null

$runScript = Join-Path $PSScriptRoot 'run.ps1'
if (-not (Test-Path -LiteralPath $runScript)) { Write-WastedError "run.ps1 not found next to watchdog.ps1 — cannot relaunch."; exit 1 }

function Test-BridgeHealth {
    <#
    Returns @{ Ok; Detail; TickHz; GameFps }. TickHz/GameFps are [double]::NaN when the probe
    failed or the field was absent, so "not measured" is never silently confused with zero.
    #>
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUrl/health" -Method Get -TimeoutSec $ProbeTimeoutS -SkipHttpErrorCheck
        $body = if ($resp.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($resp.Content) } else { [string]$resp.Content }
        $excerpt = ($body -replace '\s+', ' ')
        if ($excerpt.Length -gt 160) { $excerpt = $excerpt.Substring(0, 160) + '…' }
        if ($resp.StatusCode -eq 200) {
            $tickHz = [double]::NaN
            $gameFps = [double]::NaN
            try {
                $json = $body | ConvertFrom-Json
                if ($json.PSObject.Properties['tick_hz'])  { $tickHz = [double]$json.tick_hz }
                if ($json.PSObject.Properties['game_fps']) { $gameFps = [double]$json.game_fps }
            }
            catch {
                # A 200 whose body will not parse is still a live listener; liveness detail is
                # simply unavailable this cycle. Never let it take the watchdog down.
            }
            return @{ Ok = $true; Detail = $excerpt; TickHz = $tickHz; GameFps = $gameFps }
        }
        # 503 online_session_active is a hard failure: the agent must never be in an online session.
        return @{ Ok = $false; Detail = "HTTP $($resp.StatusCode): $excerpt"; TickHz = [double]::NaN; GameFps = [double]::NaN }
    }
    catch {
        return @{ Ok = $false; Detail = "unreachable: $($_.Exception.Message)"; TickHz = [double]::NaN; GameFps = [double]::NaN }
    }
}

function Test-GamePaused {
    <#
    Is the bridge answering while the GAME is frozen? Returns @{ Paused; Reason }.

    tick_hz is the trustworthy signal (BridgeRouter zeroes it past 2000 ms of staleness).
    game_fps is judged only on movement: it is passed through raw and freezes at its last value
    when the script thread stops, so a non-zero game_fps proves nothing at all.
    #>
    param(
        [Parameter(Mandatory)][hashtable]$Health,
        [Parameter(Mandatory)][double]$PreviousGameFps
    )
    if (-not $Health.Ok) { return @{ Paused = $false; Reason = '' } }

    if (-not [double]::IsNaN($Health.TickHz) -and $Health.TickHz -le 0) {
        return @{ Paused = $true; Reason = 'tick_hz=0 (the script thread has not ticked for over 2 seconds)' }
    }
    if (-not [double]::IsNaN($Health.GameFps) -and -not [double]::IsNaN($PreviousGameFps) -and
        $Health.GameFps -eq $PreviousGameFps) {
        return @{ Paused = $true; Reason = "game_fps frozen at $($Health.GameFps) between polls (it is not staleness-gated, so it freezes rather than falling to zero)" }
    }
    return @{ Paused = $false; Reason = '' }
}

function Write-PauseForensics {
    # Context for the pause path only. Observes; changes nothing. In particular it only notes
    # whether obs64 exists as a process — OBS is out of scope and nothing here queries or
    # configures it.
    $gameProcs = @()
    foreach ($name in $GameProcessNames) {
        foreach ($p in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) { $gameProcs += $p }
    }
    if ($gameProcs.Count -eq 0) {
        Write-WastedWarn 'pause forensics: no game process found — this is not paused-but-alive, the game is gone.'
    }
    else {
        foreach ($p in $gameProcs) {
            Write-WastedInfo "pause forensics: $($p.ProcessName) pid $($p.Id) session $($p.SessionId) responding=$($p.Responding) mainwindow=0x$(($p.MainWindowHandle).ToString('X'))"
        }
    }
    $obs = @(Get-Process -Name 'obs64' -ErrorAction SilentlyContinue)
    Write-WastedInfo "pause forensics: obs64 process present=$($obs.Count -gt 0) (observed only — OBS is never touched by this script)"
    try {
        foreach ($row in Get-WastedSessionTable) {
            Write-WastedInfo ('pause forensics: session {0,-12} user={1,-16} id={2,-5} state={3}' -f $row.SessionName, $row.UserName, $row.Id, $row.State)
        }
    }
    catch {
        Write-WastedWarn "pause forensics: could not read the session table: $($_.Exception.Message)"
    }
    Write-DisplayForensics
}

function Start-RecoveryTask {
    <#
    Start one of the recovery scheduled tasks and say honestly whether it could be started.

    Task Scheduler is used rather than running the scripts inline, and that is the point: the
    re-attach needs SYSTEM (tscon's privilege) and the focus assert needs to run INSIDE the
    logged-on session (session 0 cannot touch session 1's windows). The watchdog is neither of
    those things reliably, so it asks the two tasks that are.
    #>
    param([Parameter(Mandatory)][string]$TaskName, [Parameter(Mandatory)][string]$What)
    if (-not $TaskName) { return $false }
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        if ($task.State -eq 'Disabled') {
            Write-WastedError "pause recovery: $What — task '$TaskName' exists but is DISABLED. Enable it: Enable-ScheduledTask -TaskName '$TaskName'"
            return $false
        }
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-WastedStep "pause recovery: $What — started '$TaskName'."
        return $true
    }
    catch {
        Write-WastedError ("pause recovery: $What — could not start '$TaskName' ($($_.Exception.Message)). " +
            "Install it: pwsh -File $(Join-Path $PSScriptRoot 'install-focus-keeper.ps1')")
        return $false
    }
}

function Invoke-PauseEscalation {
    <#
    The paused-but-alive ladder. Re-attach, then refocus, then alert. Deliberately never kills or
    relaunches the game, never touches OBS, and never starts or restarts the harness: the game is
    ALIVE here, and the fix is a window-management fix.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Reason, [Parameter(Mandatory)][int]$Streak)

    Write-WastedError "PAUSED-BUT-ALIVE: /health answers 200 but the game is frozen — $Reason (for $Streak consecutive probes)."
    Write-PauseForensics

    if (-not $PSCmdlet.ShouldProcess('frozen game', 'console re-attach, then foreground restore')) { return }

    Write-WastedStep 'pause recovery step 1/3: re-attach the session to the console.'
    [void](Start-RecoveryTask -TaskName $KeepaliveTaskName -What 'console re-attach')
    Start-Sleep -Seconds 10
    $probe = Test-BridgeHealth
    if ($probe.Ok -and -not [double]::IsNaN($probe.TickHz) -and $probe.TickHz -gt 0) {
        Write-WastedStep "pause recovery: recovered after the console re-attach (tick_hz=$($probe.TickHz))."
        return
    }

    Write-WastedStep 'pause recovery step 2/3: restore the game window foreground.'
    [void](Start-RecoveryTask -TaskName $ForegroundTaskName -What 'foreground restore')
    Start-Sleep -Seconds 10
    $probe = Test-BridgeHealth
    if ($probe.Ok -and -not [double]::IsNaN($probe.TickHz) -and $probe.TickHz -gt 0) {
        Write-WastedStep "pause recovery: recovered after the foreground restore (tick_hz=$($probe.TickHz))."
        return
    }

    Write-WastedStep 'pause recovery step 3/3: alert.'
    Write-WastedError ('PAUSED-BUT-ALIVE PERSISTS after a console re-attach and a foreground restore. The game process is up and ' +
        'the bridge answers, but the game is not ticking. Most likely causes, in order: (a) GTA V is set to pause on focus loss ' +
        'and something else holds the foreground — check C:\wasted\logs\foreground-assert.log for a FAILED line; (b) the session ' +
        'is locked, so the input desktop is Winlogon and no window can be focused — the same log says so explicitly; (c) the game ' +
        'is sitting on a modal screen (pause menu / MISSION FAILED). Not relaunching: the game is alive, and this watchdog will ' +
        'not kill a live game over a focus problem. A human should look.')
}

function Stop-GameChain {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    foreach ($name in @('GTA5', 'obs64')) {
        foreach ($proc in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
            if ($PSCmdlet.ShouldProcess("$name (pid $($proc.Id))", 'kill')) {
                Write-WastedInfo "Killing $name (pid $($proc.Id))."
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            }
        }
    }
    $harness = Get-CimInstance -ClassName Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'wasted_harness\.main' }
    foreach ($proc in @($harness)) {
        if ($PSCmdlet.ShouldProcess("harness (pid $($proc.ProcessId))", 'kill')) {
            Write-WastedInfo "Killing harness python (pid $($proc.ProcessId))."
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Write-DisplayForensics {
    <#
    Snapshot the session and display state. On this server the desktop only exists because an
    indirect display driver supplies one, so "no adapter reports an active mode" and "the
    console session is gone" are the two diagnoses worth having in the log before a relaunch.
    #>
    $sessionName = $env:SESSIONNAME
    if (-not $sessionName) { $sessionName = '<unset>' }
    Write-WastedInfo "forensics: SESSIONNAME=$sessionName"
    try {
        $adapters = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop)
    }
    catch {
        Write-WastedWarn "forensics: Win32_VideoController query failed: $($_.Exception.Message)"
        return
    }
    if ($adapters.Count -eq 0) {
        Write-WastedError 'forensics: no display adapter is enumerated at all.'
        return
    }
    $active = 0
    foreach ($vc in $adapters) {
        $w = [int]($vc.CurrentHorizontalResolution ?? 0)
        $h = [int]($vc.CurrentVerticalResolution ?? 0)
        $err = [int]($vc.ConfigManagerErrorCode ?? 0)
        Write-WastedInfo ("forensics: adapter '{0}' driver {1} mode {2}x{3} cmError {4}" -f $vc.Name, $vc.DriverVersion, $w, $h, $err)
        if ($w -ge 1280 -and $h -ge 720) { $active++ }
    }
    if ($active -eq 0) {
        Write-WastedError 'forensics: NO adapter reports a desktop mode of at least 1280x720. The virtual display driver has stopped providing a display target — the game cannot present and OBS capture will be black. Relaunching will not fix this; check the display device in Device Manager.'
    }
}

function Invoke-Relaunch {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    if (-not $PSCmdlet.ShouldProcess('game chain', 'kill and relaunch via run.ps1')) { return }
    Write-DisplayForensics
    Stop-GameChain
    Start-Sleep -Seconds 5
    $pwshPath = (Get-Process -Id $PID).Path
    Write-WastedInfo "Relaunching chain: $pwshPath -File $runScript"
    & $pwshPath -NoProfile -File $runScript 2>&1 | ForEach-Object { Write-WastedInfo "run.ps1: $_" }
    Write-WastedInfo "run.ps1 exited with code $LASTEXITCODE."
    # Give SHVDN + the bridge script time to come up before the next probe judges the relaunch.
    Start-Sleep -Seconds 30
}

function Send-BridgeDownEvent {
    param([Parameter(Mandatory)][int]$ConsecutiveFailures)
    try {
        $python = Resolve-WastedPython -Explicit $PythonExe
        $payload = @{ consecutive_failures = $ConsecutiveFailures } | ConvertTo-Json -Compress
        $harnessDir = Join-Path $RepoDir 'harness'
        Write-WastedInfo "Posting bridge_down event (payload $payload)."
        Push-Location $harnessDir
        try {
            # Tool output is routed to the log stream so it cannot pollute this function's
            # boolean return value.
            & $python -m wasted_harness.tools.post_event --type bridge_down --payload $payload 2>&1 |
                ForEach-Object { Write-WastedInfo "post_event: $_" }
            $code = $LASTEXITCODE
        }
        finally {
            Pop-Location
        }
        if ($code -ne 0) {
            Write-WastedWarn "post_event exited $code — will retry on the next escalation cycle."
            return $false
        }
        Write-WastedInfo 'bridge_down event posted (or queued offline by the harness tool).'
        return $true
    }
    catch {
        Write-WastedWarn "post_event failed: $($_.Exception.Message) — will retry on the next escalation cycle."
        return $false
    }
}

# --- main loop --------------------------------------------------------------------------------

$consecutiveFailures = 0
$relaunchTimes = [System.Collections.Generic.List[datetime]]::new()
$degraded = $false
$bridgeDownPosted = $false
$probeCount = 0
$pauseStreak = 0
$lastGameFps = [double]::NaN
$lastPauseEscalation = [datetime]::MinValue

Write-WastedStep "Watchdog up. url=$BaseUrl/health interval=${IntervalS}s threshold=$FailThreshold window=${RelaunchWindowMin}min slow=${SlowIntervalS}s"
Write-WastedStep "Paused-but-alive detection: $PauseThreshold consecutive frozen probes, then re-attach ($KeepaliveTaskName) -> refocus ($ForegroundTaskName) -> alert. Cooldown ${PauseCooldownMin}min. Never relaunches, never touches OBS, never starts the harness."

while ($true) {
    try {
        $probeCount++
        $health = Test-BridgeHealth

        if ($health.Ok) {
            if ($consecutiveFailures -gt 0 -or $degraded) {
                Write-WastedStep "Bridge recovered after $consecutiveFailures consecutive failure(s). Back to normal cadence."
            }
            $consecutiveFailures = 0
            $degraded = $false
            $bridgeDownPosted = $false
            Write-WastedInfo "probe #$probeCount OK: $($health.Detail)"

            # --- paused-but-alive: 200 OK is not the same as "the game is running" ---------------
            $pause = Test-GamePaused -Health $health -PreviousGameFps $lastGameFps
            if ($pause.Paused) {
                $pauseStreak++
                Write-WastedWarn "probe #$probeCount FROZEN ($pauseStreak/$PauseThreshold): $($pause.Reason)"
                if ($pauseStreak -ge $PauseThreshold) {
                    if ((Get-Date) -lt $lastPauseEscalation.AddMinutes($PauseCooldownMin)) {
                        Write-WastedWarn ('Still frozen, but inside the ' + $PauseCooldownMin +
                            '-minute escalation cooldown (last attempt ' + $lastPauseEscalation.ToString('o') + '). Waiting.')
                    }
                    else {
                        $lastPauseEscalation = Get-Date
                        Invoke-PauseEscalation -Reason $pause.Reason -Streak $pauseStreak
                        $pauseStreak = 0
                    }
                }
            }
            elseif ($pauseStreak -gt 0) {
                Write-WastedStep "Game is ticking again after $pauseStreak frozen probe(s) (tick_hz=$($health.TickHz), game_fps=$($health.GameFps))."
                $pauseStreak = 0
            }
            $lastGameFps = $health.GameFps
        }
        else {
            # A failed probe says nothing about pausing; do not carry a stale fps across it.
            $pauseStreak = 0
            $lastGameFps = [double]::NaN
            $consecutiveFailures++
            Write-WastedWarn "probe #$probeCount FAIL ($consecutiveFailures consecutive): $($health.Detail)"

            if ($consecutiveFailures -ge $FailThreshold) {
                # Prune relaunch attempts that left the rolling window.
                $cutoff = (Get-Date).AddMinutes(-$RelaunchWindowMin)
                while ($relaunchTimes.Count -gt 0 -and $relaunchTimes[0] -lt $cutoff) { $relaunchTimes.RemoveAt(0) }

                if ($degraded) {
                    # Slow cadence: keep alerting (until a post succeeds) and keep retrying.
                    if (-not $bridgeDownPosted) {
                        $bridgeDownPosted = Send-BridgeDownEvent -ConsecutiveFailures $consecutiveFailures
                    }
                    $relaunchTimes.Add((Get-Date))
                    Invoke-Relaunch
                }
                elseif ($consecutiveFailures % $FailThreshold -eq 0) {
                    # A fresh run of $FailThreshold failures since the last relaunch attempt.
                    if ($relaunchTimes.Count -ge $MaxRelaunchesPerWindow) {
                        Write-WastedError "$($relaunchTimes.Count) relaunches within $RelaunchWindowMin min did not restore /health — degrading to slow cadence (${SlowIntervalS}s) and alerting."
                        $degraded = $true
                        $bridgeDownPosted = Send-BridgeDownEvent -ConsecutiveFailures $consecutiveFailures
                        $relaunchTimes.Add((Get-Date))
                        Invoke-Relaunch
                    }
                    else {
                        Write-WastedStep "Failure threshold hit — relaunch $($relaunchTimes.Count + 1)/$MaxRelaunchesPerWindow in the current $RelaunchWindowMin-min window."
                        $relaunchTimes.Add((Get-Date))
                        Invoke-Relaunch
                    }
                }
            }
        }
    }
    catch {
        # Never exit silently: log and keep watching.
        Write-WastedError "Watchdog iteration error: $($_.Exception.Message)"
        Write-WastedError $_.ScriptStackTrace
    }

    Start-Sleep -Seconds ($degraded ? $SlowIntervalS : $IntervalS)
}
