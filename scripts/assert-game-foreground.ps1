#Requires -Version 7.0
<#
.SYNOPSIS
    Re-assert GTA V as the foreground window after the session comes back to the console.
.DESCRIPTION
    THE GAP THIS CLOSES. `console-keepalive.ps1` re-attaches a parked session with
    `tscon <id> /dest:console`, and that is where the recovery stopped: nothing on this machine
    re-asserted which window is FOREGROUND afterwards. GTA V's PauseOnFocusLoss preference
    freezes the game while it is unfocused, and SendInput only reaches the foreground window, so
    "the session came back but nothing has focus" is a permanent freeze that lasts until a human
    touches the machine — which is exactly the symptom: "the moment I stop, the game freezes".

    WHY THIS IS A SEPARATE SCRIPT AND A SEPARATE SCHEDULED TASK.
    console-keepalive.ps1 runs as SYSTEM, because tscon needs the privilege. A SYSTEM task runs
    in Terminal Services session 0 on a non-interactive window station: it cannot enumerate,
    activate or even see session 1's windows (AttachThreadInput is explicit — "You cannot attach
    a thread to a thread in another desktop"). Putting the focus code inside the keepalive would
    therefore be a bug that logs confident nonsense forever. Nor can the keepalive simply call
    this script: a child process inherits session 0. The one supported bridge is Task Scheduler,
    so the keepalive asks Task Scheduler to run THIS script's task, which is registered with
    LogonType = Interactive against the logged-on user and therefore starts in session 1.
    install-focus-keeper.ps1 registers it that way and verifies the logon type afterwards.

    WHAT IT DOES
      1. Refuses to run in session 0 (it would be pointless there, and silence would be a lie).
      2. Refuses to touch anything while an operator is sitting in a live RDP session, unless
         -EvenIfOperatorConnected is given (leave-safely.ps1 does, deliberately, as its last act).
      3. Aborts if the input desktop is not 'Default' — a locked session renders the lock screen
         to the virtual display, which looks exactly like a frozen game, and no focus call can
         help because the game's desktop is not the input desktop.
      4. Finds the game window by process handle first, title second.
      5. Takes the foreground the way Win32 allows (see window-focus.ps1) and VERIFIES with
         GetForegroundWindow's owning PID. The return code of SetForegroundWindow is logged and
         never believed.
      6. Stays quiet when nothing needed doing. It only writes to the log when it acted, when it
         failed, or when it found something worth telling a human about.

    It does not touch OBS. It does not start, stop, restart or schedule the harness. It does not
    restart the game. It presses no keys.
.PARAMETER ProcessNames
    Candidate game process names, in order. GTA5 is the Legacy-edition name this repo already
    uses elsewhere (watchdog.ps1). The Enhanced name is a guess and is only ever tried after the
    Legacy one; whichever actually matched is written to the log so it can be recorded as fact.
.PARAMETER WindowTitles
    Fallback window titles, used only when no candidate process is found. Matches the harness's
    GAME_WINDOW_TITLES, which its own comment flags as unconfirmed against a running game.
.PARAMETER EvenIfOperatorConnected
    Take the foreground even though somebody is connected over RDP right now. Only
    leave-safely.ps1 (and a human debugging) should pass this.
.PARAMETER LogPath
    Append-only log. Default C:\wasted\logs\foreground-assert.log. This script does NOT start a
    per-run transcript: it runs on a one-minute task, and a transcript per run would be 1440 new
    files a day.
.EXAMPLE
    pwsh -File .\assert-game-foreground.ps1
.EXAMPLE
    pwsh -File .\assert-game-foreground.ps1 -EvenIfOperatorConnected -Verbose
.OUTPUTS
    Exit codes: 0 ok or nothing to do; 2 input desktop is not Default (session locked);
    3 no game window; 4 focus attempt failed and was verified to have failed;
    5 not an interactive session; 1 unexpected error.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string[]]$ProcessNames = @('GTA5', 'GTA5_Enhanced'),
    [string[]]$WindowTitles = @('Grand Theft Auto V'),
    [switch]$EvenIfOperatorConnected,
    [string]$LogPath = '',
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
. $commonPath
$focusLib = Join-Path $PSScriptRoot 'window-focus.ps1'
if (-not (Test-Path -LiteralPath $focusLib)) { Write-Error "Missing $focusLib — run from a full checkout of scripts/."; exit 1 }
. $focusLib

Assert-WastedEnvironment -ScriptName 'assert-game-foreground.ps1' -RequireWastedRoot -Force:$Force

if (-not $LogPath) { $LogPath = Join-Path (Join-Path $script:WastedRoot 'logs') 'foreground-assert.log' }

function Write-FocusLog {
    <# Append to the focus log and echo to the host. The log is the black box: this task usually
       runs with nowhere for host output to go. #>
    param(
        [Parameter(Mandatory)][ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level,
        [Parameter(Mandatory)][string]$Message
    )
    switch ($Level) {
        'ERROR' { Write-WastedError $Message }
        'WARN'  { Write-WastedWarn  $Message }
        default { Write-WastedInfo  $Message }
    }
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force -WhatIf:$false -Confirm:$false | Out-Null
        }
        Add-Content -LiteralPath $LogPath -Value ('{0:o} [{1}] {2}' -f (Get-Date), $Level, $Message) -WhatIf:$false
    }
    catch {
        Write-WastedWarn "could not write $($LogPath): $($_.Exception.Message)"
    }
}

try {
    # --- 1. session 0 is a dead end -------------------------------------------------------------
    if (-not (Test-WastedInteractiveSession)) {
        Write-FocusLog -Level ERROR -Message ("running in a non-interactive session (SESSIONNAME='$($env:SESSIONNAME)'). " +
            'Window focus cannot be changed from session 0. Register this via install-focus-keeper.ps1, ' +
            'which sets LogonType=Interactive so the task starts in the logged-on session.')
        exit 5
    }

    # --- 2. never steal focus from a connected operator -----------------------------------------
    $sessions = Get-WastedSessionTable
    $operatorConnected = Test-WastedOperatorConnected -SessionTable $sessions
    if ($operatorConnected -and -not $EvenIfOperatorConnected) {
        Write-Verbose 'An RDP session is Active — leaving the foreground alone.'
        exit 0
    }

    # --- 3. is the user's desktop even the input desktop? ---------------------------------------
    $desktop = Get-WastedInputDesktopName
    if ($desktop.Name -ne 'Default') {
        $what = if ($null -eq $desktop.Name) { "could not be opened (win32 error $($desktop.Win32Error))" } else { "is '$($desktop.Name)'" }
        Write-FocusLog -Level ERROR -Message ("the input desktop $what, not 'Default' — the session is locked or on the secure desktop. " +
            'The virtual display is rendering the lock screen, which on the stream is indistinguishable from a frozen game. ' +
            'No focus call can fix this: run leave-safely.ps1, which applies and verifies the anti-lock settings.')
        exit 2
    }

    # --- 4. find the game -----------------------------------------------------------------------
    $game = Find-WastedGameWindow -ProcessNames $ProcessNames -WindowTitles $WindowTitles
    if ($null -eq $game) {
        Write-FocusLog -Level WARN -Message ("no game window found (processes tried: $($ProcessNames -join ', '); " +
            "titles tried: $($WindowTitles -join ', ')). The game is not running — this script does not start it.")
        exit 3
    }
    if ($game.WindowMissing) {
        Write-FocusLog -Level WARN -Message ("$($game.ProcessName) (pid $($game.ProcessId), session $($game.SessionId)) is running but owns no " +
            'top-level window yet — still loading, or the window was destroyed. Nothing to focus.')
        exit 3
    }

    # --- 5. take the foreground and verify by PID ----------------------------------------------
    $result = Set-WastedGameForeground -Hwnd $game.Hwnd -GameProcessId $game.ProcessId

    if ($result.Method -eq 'already') {
        Write-Verbose "Game (pid $($game.ProcessId)) already has the foreground — nothing to do."
        exit 0
    }
    if ($result.Method -eq 'skipped-whatif') {
        Write-WastedInfo "WhatIf: would focus $($game.ProcessName) pid $($game.ProcessId) hwnd 0x$($game.Hwnd.ToString('X'))."
        exit 0
    }

    $contextFormat = "game={0} pid={1} hwnd=0x{2} found_via={3} title='{4}' was='{5}'(pid {6}) rc={7} attached={8} locktimeout_set={9}"
    $context = $contextFormat -f $game.ProcessName, $game.ProcessId, $game.Hwnd.ToString('X'), $game.Source, $game.Title, $result.Before.ProcessName, $result.Before.ProcessId, $result.Rc, $result.Attached, $result.LockTimeoutSet

    if ($result.Success) {
        Write-FocusLog -Level INFO -Message "VERIFIED foreground restored via $($result.Method): $context"
        exit 0
    }

    Write-FocusLog -Level ERROR -Message ("FAILED to take the foreground; it is still '$($result.After.ProcessName)' " +
        "(pid $($result.After.ProcessId), title '$($result.After.Title)'). $context. " +
        'Windows can deny a foreground change with no error — check HKCU\Control Panel\Desktop\ForegroundLockTimeout=0 ' +
        '(leave-safely.ps1 sets it) and that this task really runs in the logged-on session.')
    exit 4
}
catch {
    Write-FocusLog -Level ERROR -Message "assert-game-foreground.ps1 failed: $($_.Exception.Message)"
    Write-WastedError $_.ScriptStackTrace
    exit 1
}
