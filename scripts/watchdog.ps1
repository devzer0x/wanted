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
.EXAMPLE
    pwsh -File .\watchdog.ps1
.EXAMPLE
    pwsh -File .\watchdog.ps1 -IntervalS 10 -SlowIntervalS 600
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
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUrl/health" -Method Get -TimeoutSec $ProbeTimeoutS -SkipHttpErrorCheck
        $body = if ($resp.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($resp.Content) } else { [string]$resp.Content }
        $excerpt = ($body -replace '\s+', ' ')
        if ($excerpt.Length -gt 160) { $excerpt = $excerpt.Substring(0, 160) + '…' }
        if ($resp.StatusCode -eq 200) {
            return @{ Ok = $true; Detail = $excerpt }
        }
        # 503 online_session_active is a hard failure: the agent must never be in an online session.
        return @{ Ok = $false; Detail = "HTTP $($resp.StatusCode): $excerpt" }
    }
    catch {
        return @{ Ok = $false; Detail = "unreachable: $($_.Exception.Message)" }
    }
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

Write-WastedStep "Watchdog up. url=$BaseUrl/health interval=${IntervalS}s threshold=$FailThreshold window=${RelaunchWindowMin}min slow=${SlowIntervalS}s"

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
        }
        else {
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
