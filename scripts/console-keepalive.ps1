#Requires -Version 7.0
<#
.SYNOPSIS
    Keep the game's session attached to the console, then ask for the game window to be refocused.
.DESCRIPTION
    Why: when the operator disconnects Remote Desktop, Windows parks the session as "Disc". A
    parked session stops rendering, Desktop Duplication fails outright
    (DXGI_ERROR_SESSION_DISCONNECTED, "because the session is currently disconnected"), the
    virtual display has nothing to show, and the stream freezes on the last frame. Observed on
    2026-09-01/02 every time RDP was closed. `tscon <id> /dest:console` re-attaches the session
    to the console and rendering resumes.

    Runs as SYSTEM via the WASTED-ConsoleKeepalive scheduled task (every minute, plus an
    on-disconnect event trigger when install-focus-keeper.ps1 could verify that this box really
    logs TerminalServices-LocalSessionManager event 24). Idempotent and quiet: it writes to the
    log only when it acted or when something is wrong, and does nothing at all while the operator
    is connected or the session is already on the console.

    TWO CHANGES FROM THE ORIGINAL, both because the log has to be trustworthy at 3am:

      * The session id is derived, not hardcoded. It is the session the GAME is actually in
        (Get-Process -> .SessionId), which is exact and locale-proof; only if the game is not
        running does it fall back to "the one disconnected user session", and if that is
        ambiguous it says so and does nothing rather than reattaching a guess. -SessionId pins it
        by hand if the derivation is ever wrong.
      * tscon's exit code is checked. The original logged "reattached session 1 to console"
        unconditionally, so a failed tscon produced a log line that read like a success.

    AND THE THING IT COULD NOT DO ITSELF: re-assert the foreground window. This task is SYSTEM,
    i.e. Terminal Services session 0 on a non-interactive window station; it cannot see, activate
    or attach to any window in the interactive session, and a child process it launched would
    inherit session 0. So after a successful re-attach it asks Task Scheduler to start
    WASTED-ForegroundAssert, which is registered with LogonType=Interactive and therefore runs
    assert-game-foreground.ps1 in the logged-on session where SetForegroundWindow can work.

    Touches no OBS setting. Never starts, stops or schedules the harness.
.PARAMETER SessionId
    Pin the session to re-attach instead of deriving it. 0 (default) means derive.
.PARAMETER GameProcessNames
    Candidate game process names used to derive the session id. GTA5 is the Legacy-edition name
    already used by watchdog.ps1; the Enhanced name is only tried second.
.PARAMETER ForegroundTaskName
    Interactive task to start after a successful re-attach. Empty string disables that step.
.EXAMPLE
    pwsh -NoProfile -ExecutionPolicy Bypass -File C:\wasted\repo\scripts\console-keepalive.ps1
.EXAMPLE
    pwsh -File .\console-keepalive.ps1 -SessionId 1 -Verbose
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [int]$SessionId = 0,
    [string[]]$GameProcessNames = @('GTA5', 'GTA5_Enhanced'),
    [string]$ForegroundTaskName = 'WASTED-ForegroundAssert',
    [string]$LogPath = 'C:\wasted\logs\console-keepalive.log'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-KeepaliveLog {
    # Append-only; this task has nowhere for host output to go. No transcript on purpose:
    # a per-run transcript on a one-minute task is 1440 new files a day.
    param(
        [Parameter(Mandatory)][ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level,
        [Parameter(Mandatory)][string]$Message
    )
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force -WhatIf:$false -Confirm:$false | Out-Null
        }
        Add-Content -LiteralPath $LogPath -Value ('{0:o} [{1}] {2}' -f (Get-Date), $Level, $Message) -WhatIf:$false
    }
    catch {
        # Nothing sensible left to do — never let logging kill the keepalive.
    }
    Write-Verbose "[$Level] $Message"
}

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) {
    Write-KeepaliveLog -Level ERROR -Message ("common.ps1 is missing next to '$PSCommandPath'. The WASTED-ConsoleKeepalive task " +
        'must point at the script inside a full checkout of scripts/, not a lone copy. The session is NOT being kept alive.')
    exit 1
}
. $commonPath

try {
    if (-not $IsWindows) { throw 'console-keepalive.ps1 is Windows-only.' }

    $sessions = Get-WastedSessionTable

    # --- which session are we responsible for? --------------------------------------------------
    $target = $SessionId
    $how = 'pinned by -SessionId'
    if ($target -le 0) {
        $gameProc = $null
        foreach ($name in $GameProcessNames) {
            $found = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
            if ($found.Count -gt 0) { $gameProc = $found[0]; break }
        }
        if ($gameProc) {
            $target = $gameProc.SessionId
            $how = "the session $($gameProc.ProcessName) (pid $($gameProc.Id)) is running in"
        }
        else {
            # No game: still worth keeping the desktop attached so the logon task can start one.
            $candidates = @($sessions | Where-Object { $_.Id -gt 0 -and $_.UserName -and $_.State -eq 'Disc' })
            if ($candidates.Count -eq 1) {
                $target = $candidates[0].Id
                $how = "the only disconnected user session ($($candidates[0].UserName))"
            }
            elseif ($candidates.Count -gt 1) {
                Write-KeepaliveLog -Level ERROR -Message ('the game is not running and there are ' +
                    "$($candidates.Count) disconnected user sessions (" +
                    (($candidates | ForEach-Object { "$($_.Id)/$($_.UserName)" }) -join ', ') +
                    ') — refusing to guess which one to re-attach. Re-register this task with -SessionId <n>.')
                exit 1
            }
            else {
                Write-Verbose 'Game not running and no disconnected user session — nothing to do.'
                exit 0
            }
        }
    }

    $row = $sessions | Where-Object { $_.Id -eq $target } | Select-Object -First 1
    if (-not $row) {
        Write-KeepaliveLog -Level ERROR -Message ("session $target ($how) does not appear in 'query session' at all. " +
            'Session table: ' + (($sessions | ForEach-Object { "$($_.Id)/$($_.SessionName)/$($_.UserName)/$($_.State)" }) -join ' | '))
        exit 1
    }

    if ($row.State -ne 'Disc') {
        Write-Verbose "Session $target is '$($row.State)' on '$($row.SessionName)' — not parked, nothing to do."
        exit 0
    }

    # --- re-attach ------------------------------------------------------------------------------
    if (-not $PSCmdlet.ShouldProcess("session $target", 'tscon /dest:console')) { exit 0 }

    $tscon = Join-Path $env:SystemRoot 'System32\tscon.exe'
    $out = & $tscon $target /dest:console 2>&1
    $code = $LASTEXITCODE
    $outText = (($out | ForEach-Object { [string]$_ }) -join ' ').Trim()

    if ($code -ne 0) {
        Write-KeepaliveLog -Level ERROR -Message ("tscon $target /dest:console EXITED $code — the session was NOT re-attached " +
            "and the stream is still frozen. ($how.) Output: '$outText'. tscon needs SYSTEM/elevation; " +
            'confirm the WASTED-ConsoleKeepalive task runs as SYSTEM with highest privileges.')
        exit 1
    }

    Write-KeepaliveLog -Level INFO -Message "reattached session $target to console ($how). tscon output: '$outText'"

    # --- hand the foreground problem to a task that can actually solve it -----------------------
    if (-not $ForegroundTaskName) { exit 0 }
    try {
        $task = Get-ScheduledTask -TaskName $ForegroundTaskName -ErrorAction Stop
        if ($task.State -eq 'Disabled') {
            Write-KeepaliveLog -Level WARN -Message ("$ForegroundTaskName exists but is Disabled — the game window will not be " +
                'refocused after this re-attach, so the game may stay paused on focus loss. Enable it.')
        }
        else {
            Start-ScheduledTask -TaskName $ForegroundTaskName -ErrorAction Stop
            Write-Verbose "Started $ForegroundTaskName."
        }
    }
    catch {
        Write-KeepaliveLog -Level WARN -Message ("could not start '$ForegroundTaskName' ($($_.Exception.Message)). " +
            'The session is back but nothing re-asserted the foreground window; run install-focus-keeper.ps1.')
    }
    exit 0
}
catch {
    Write-KeepaliveLog -Level ERROR -Message "console-keepalive.ps1 failed: $($_.Exception.Message)"
    exit 1
}
