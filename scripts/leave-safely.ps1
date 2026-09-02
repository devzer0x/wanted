#Requires -Version 7.0
<#
.SYNOPSIS
    The one command to run before you walk away from the server. Proves the show survives your
    leaving, then hands the desktop to the console session.
.DESCRIPTION
    YOUR RDP WINDOW CLOSING IS THE SUCCESS SIGNAL. It is not a crash and it is not an error. If
    this script finishes its work, Remote Desktop disconnects and everything keeps running.

    AFTER IT CLOSES, CHECK TWO THINGS FROM YOUR OWN MACHINE — not from a new RDP session, because
    reconnecting re-adds the Microsoft Remote Display Adapter and changes the very thing you are
    trying to measure:
      1. The Twitch picture is MOVING (not just present — a frozen game still shows a picture).
      2. The site's heartbeat is fresh.

    AND THEN ASK FOR WANTED TO BE STARTED. This script deliberately does NOT start the harness.
    the agent's screen capture binds to whatever display adapter exists when he starts, and while you
    are connected that is the RDP display adapter, which vanishes the moment you disconnect. He
    has to start after you have gone. Nothing in this script, in the keepalive or in the focus
    keeper will ever start, restart or schedule him.

    HOW IT IS SAFE BY CONSTRUCTION: everything is checked while you can still see the screen and
    still have a session, and the detach is the last thing that happens. If any check fails, it
    refuses to detach and prints exactly what to fix. You cannot walk away from a broken state
    believing it is fine.

    WHAT IT CHECKS, in order:
      1. Elevated, on the server, in an RDP session. (tscon fails silently when not elevated.)
      2. Anti-lock: idle lock, screen saver (registry AND live, via SystemParametersInfo, because
         the registry values only bind at next logon), lock screen, foreground lock timeout,
         monitor/standby/hibernate power timeouts, and the Remote Desktop session time limits that
         would otherwise log the session off and kill the game outright. Applied idempotently
         every run, then read back — never assumed from a previous run.
      3. The game process is running, and at least one display adapter still reports a >=1280x720
         desktop mode (with no monitor, the virtual display driver is the only display target).
      4. THE REAL TEST: several /health samples a few seconds apart. tick_hz must be above zero
         and game_fps must actually advance between samples. If the game window is NOT the
         foreground window while those samples are taken — which is the normal case, because you
         are typing in this terminal — then that is direct proof the game keeps simulating while
         unfocused, i.e. exactly what has to be true for it to survive your leaving. That is a
         measurement of the game itself and owes nothing to the encoder.
         (game_fps alone cannot be trusted: the bridge only zeroes tick_hz when the script thread
         goes stale, while game_fps is passed through raw and freezes at its last value. So this
         checks both, and requires movement, not non-zero.)
      5. The Rockstar settings.xml corroboration: PauseOnFocusLoss must be 0 and Windowed must be
         2 (borderless). Only a warning when step 4 already proved the game runs unfocused; a hard
         failure when step 4 could not prove it because the game had focus during the samples.
      6. The WASTED-ConsoleKeepalive task exists, is enabled, runs as SYSTEM, and its action
         points at a script that is actually there; likewise WASTED-ForegroundAssert, which must
         be Interactive. Missing tasks are installed by install-focus-keeper.ps1 automatically.
      7. Reports whether the harness is running (report only — see above).
      8. Makes the game window the foreground window and verifies it by foreground PID.

    It never touches OBS. It runs no obs-websocket call, changes no scene, no source and no
    capture method. OBS is out of scope on purpose.
.PARAMETER CheckOnly
    Dry run: every read-only check, no settings applied, no focus taken, no detach. Run this
    first if you want to see where you stand without committing to leaving.
.PARAMETER Yes
    Skip the "press Enter to detach" confirmation.
.PARAMETER Samples / -SampleGapS
    How many /health samples and how far apart. Defaults 3 x 3s: enough that identical game_fps
    across the whole series cannot plausibly be jitter.
.EXAMPLE
    pwsh -File .\leave-safely.ps1 -CheckOnly
.EXAMPLE
    pwsh -File .\leave-safely.ps1
.OUTPUTS
    Exit 0: detached (your RDP window is gone) or -CheckOnly passed.
    Exit 1: refused — the reasons and their fixes are printed, and nothing was detached.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$BaseUrl = 'http://127.0.0.1:7777',
    [ValidateRange(1, 60)][int]$ProbeTimeoutS = 5,
    [ValidateRange(2, 20)][int]$Samples = 3,
    [ValidateRange(1, 60)][int]$SampleGapS = 3,
    [string[]]$GameProcessNames = @('GTA5', 'GTA5_Enhanced'),
    [string[]]$GameWindowTitles = @('Grand Theft Auto V'),
    [int]$MinDisplayWidth = 1280,
    [int]$MinDisplayHeight = 720,
    [string]$KeepaliveTaskName = 'WASTED-ConsoleKeepalive',
    [string]$ForegroundTaskName = 'WASTED-ForegroundAssert',
    [switch]$CheckOnly,
    [switch]$Yes,
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

# tscon fails silently and leaves the console locked when it is not elevated, so elevation is a
# hard requirement here exactly as it is in detach-rdp.ps1.
Assert-WastedEnvironment -ScriptName 'leave-safely.ps1' -RequireWastedRoot -RequireElevation -Force:$Force
Start-WastedTranscript -Name 'leave-safely' | Out-Null

$script:Failures = [System.Collections.Generic.List[string]]::new()
$script:Cautions = [System.Collections.Generic.List[string]]::new()

function Add-LeaveFailure {
    param([Parameter(Mandatory)][string]$Message)
    $script:Failures.Add($Message)
    Write-WastedError "FAIL  $Message"
}
function Add-LeaveCaution {
    param([Parameter(Mandatory)][string]$Message)
    $script:Cautions.Add($Message)
    Write-WastedWarn "WARN  $Message"
}
function Add-LeavePass {
    param([Parameter(Mandatory)][string]$Message)
    Write-WastedInfo "OK    $Message"
}

function Set-LeaveRegistryValue {
    <#
    Apply one registry value idempotently, then read it back and judge.

    HKCU here is deliberate and correct: this script runs interactively as the operator, so HKCU
    is the operator's hive. The same write from a SYSTEM task or an SSH shell would land in the
    wrong hive and do nothing useful.

    -TakesEffectAtReboot marks values (InactivityTimeoutSecs) that Microsoft documents as needing
    a restart: a fresh write is reported as a caution, not a pass, so the log never claims a
    policy is live when it is not.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][object]$Value,
        [Parameter(Mandatory)][Microsoft.Win32.RegistryValueKind]$Type,
        [Parameter(Mandatory)][string]$Why,
        [switch]$TakesEffectAtReboot
    )
    $current = $null
    if (Test-Path -LiteralPath $Path) {
        $prop = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction SilentlyContinue
        if ($null -ne $prop -and $prop.PSObject.Properties[$Name]) { $current = $prop.$Name }
    }
    if ("$current" -eq "$Value") {
        Add-LeavePass "$Why ($Name = $Value)"
        return
    }
    if ($CheckOnly) {
        Add-LeaveCaution "$Why is $Name = '$current', wants $Value. -CheckOnly changed nothing; a normal run fixes it."
        return
    }
    if (-not $PSCmdlet.ShouldProcess("$Path\$Name", "set to $Value ($Why)")) { return }

    try {
        if (-not (Test-Path -LiteralPath $Path)) { New-Item -Path $Path -Force | Out-Null }
        New-ItemProperty -LiteralPath $Path -Name $Name -Value $Value -PropertyType $Type -Force | Out-Null
    }
    catch {
        Add-LeaveFailure "$Why could not be set ($Path\$Name): $($_.Exception.Message). Fix it by hand, then re-run."
        return
    }
    $after = (Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction SilentlyContinue).$Name
    if ("$after" -ne "$Value") {
        Add-LeaveFailure "$Why did not stick: $Path\$Name reads '$after' after being set to '$Value' (Group Policy may be overriding it)."
        return
    }
    if ($TakesEffectAtReboot) {
        Add-LeaveCaution ("$Why was just changed ('$current' -> '$Value'). Microsoft documents this policy as needing a " +
            'RESTART to take effect, so it is not live in this boot. The screen saver and monitor timeouts below are ' +
            'live immediately and are what actually trigger a lock, so leaving now is still safe — but reboot when convenient.')
        return
    }
    Add-LeavePass "$Why set to $Value (was '$current')"
}

function Invoke-Powercfg {
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string[]]$Arguments, [Parameter(Mandatory)][string]$Why)
    # Not a caution: the read-back immediately after this block is the actual verdict, and seven
    # cautions saying "-CheckOnly changed nothing" would bury the ones that matter.
    if ($CheckOnly) { Write-WastedInfo "SKIP  $Why (-CheckOnly: would run 'powercfg $($Arguments -join ' ')')"; return }
    if (-not $PSCmdlet.ShouldProcess("powercfg $($Arguments -join ' ')", $Why)) { return }
    $out = & powercfg @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        Add-LeaveCaution "$Why : powercfg $($Arguments -join ' ') exited $LASTEXITCODE ($(($out | ForEach-Object { [string]$_ }) -join ' '))."
    }
    else {
        Add-LeavePass $Why
    }
}

function Get-HealthSample {
    <#
    One /health reading plus who owned the foreground at that instant. The foreground is captured
    with the sample on purpose: "tick_hz was fine" means nothing unless you know whether the game
    happened to be focused at the time.
    #>
    param([int]$GameProcessId)

    $fg = Get-WastedForegroundWindow
    $sample = [pscustomobject]@{
        Time           = Get-Date
        Reachable      = $false
        StatusCode     = 0
        TickHz         = [double]::NaN
        GameFps        = [double]::NaN
        QueueDepth     = -1
        OnlineBlocked  = $false
        GameForeground = ($GameProcessId -gt 0 -and $fg.ProcessId -eq $GameProcessId)
        ForegroundName = $fg.ProcessName
        Detail         = ''
    }
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUrl/health" -Method Get -TimeoutSec $ProbeTimeoutS -SkipHttpErrorCheck
        $body = if ($resp.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($resp.Content) } else { [string]$resp.Content }
        $sample.StatusCode = [int]$resp.StatusCode
        $sample.Detail = ($body -replace '\s+', ' ')
        if ($sample.StatusCode -eq 200) {
            $json = $body | ConvertFrom-Json
            if ($json.PSObject.Properties['tick_hz'])       { $sample.TickHz = [double]$json.tick_hz }
            if ($json.PSObject.Properties['game_fps'])      { $sample.GameFps = [double]$json.game_fps }
            if ($json.PSObject.Properties['queue_depth'])   { $sample.QueueDepth = [int]$json.queue_depth }
            if ($json.PSObject.Properties['online_blocked']) { $sample.OnlineBlocked = [bool]$json.online_blocked }
            $sample.Reachable = $true
        }
    }
    catch {
        $sample.Detail = "unreachable: $($_.Exception.Message)"
    }
    return $sample
}

function Get-GameSettingValue {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $xml = [System.Xml.XmlDocument]::new()
        $xml.Load($Path)
    }
    catch { return $null }
    $node = $xml.SelectSingleNode("//$Name")
    if ($null -eq $node) { return $null }
    $attr = $node.Attributes['value']
    if ($null -eq $attr) { return $null }
    return [string]$attr.Value
}

function Test-LeaveTask {
    <#
    A scheduled task is only useful if it exists, is enabled, runs as the right principal, and its
    action points at a file that is actually on disk. The keepalive was installed by hand on this
    server and lived in no repo file, so "the task exists" was never enough to trust.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet('Interactive', 'System')][string]$ExpectedPrincipal,
        [Parameter(Mandatory)][string]$WhyItMatters
    )
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) { return $false }

    $ok = $true
    if ($task.State -eq 'Disabled') {
        Add-LeaveFailure "$Name is DISABLED. $WhyItMatters Enable it: Enable-ScheduledTask -TaskName '$Name'"
        $ok = $false
    }
    $logonType = [string]$task.Principal.LogonType
    $userId = [string]$task.Principal.UserId
    if ($ExpectedPrincipal -eq 'Interactive') {
        # This one is load-bearing and strict: an Interactive logon type is the only way the task
        # lands in the logged-on session, and session 0 cannot touch session 1's windows at all.
        if ($logonType -notmatch '^(?i)interactive') {
            Add-LeaveFailure ("$Name runs with LogonType='$logonType' as '$userId', not Interactive. " +
                "$WhyItMatters Re-register it: pwsh -File $(Join-Path $PSScriptRoot 'install-focus-keeper.ps1')")
            $ok = $false
        }
    }
    else {
        # Deliberately loose: this task may have been created by hand with schtasks, and the exact
        # LogonType string that produces varies. What actually matters is that it runs as SYSTEM,
        # because tscon needs the privilege.
        if ($userId -notmatch '(?i)(system|S-1-5-18)') {
            Add-LeaveFailure ("$Name runs as '$userId' (LogonType=$logonType), not SYSTEM. tscon needs the privilege and " +
                "fails silently without it. $WhyItMatters Re-register it: pwsh -File $(Join-Path $PSScriptRoot 'install-focus-keeper.ps1') -ReplaceKeepalive")
            $ok = $false
        }
    }
    foreach ($action in @($task.Actions)) {
        $taskArgs = [string]$action.Arguments
        if ($taskArgs -match '-File\s+"?([^"]+)"?') {
            $file = $Matches[1].Trim()
            if (-not (Test-Path -LiteralPath $file)) {
                Add-LeaveFailure ("$Name runs '$file', which does not exist. The task is pointing at a stale copy " +
                    "and has been doing nothing. Re-register it: pwsh -File $(Join-Path $PSScriptRoot 'install-focus-keeper.ps1')")
                $ok = $false
            }
        }
    }
    if ($ok) { Add-LeavePass "$Name present, enabled, LogonType=$logonType as '$userId', action on disk" }
    return $ok
}

# ==============================================================================================

try {
    Write-WastedStep 'leave-safely.ps1 — preflight first, detach last.'

    # --- 1. where are we ------------------------------------------------------------------------
    Write-WastedStep 'Check 1/8: session.'
    $sessions = Get-WastedSessionTable
    foreach ($row in $sessions) {
        Write-WastedInfo ('  session {0,-12} user={1,-16} id={2,-5} state={3}' -f $row.SessionName, $row.UserName, $row.Id, $row.State)
    }
    $sessionName = $env:SESSIONNAME
    if (-not $sessionName) {
        Add-LeaveFailure ('SESSIONNAME is empty — this is not an interactive Remote Desktop session (an SSH shell?). ' +
            'tscon has to be run from the RDP session itself. Open Remote Desktop and run this there.')
    }
    elseif ($sessionName -eq 'Console') {
        Write-WastedInfo 'Already on the console session — there is nothing to detach. Running the checks anyway.'
    }
    elseif ($sessionName -notlike 'RDP-Tcp#*') {
        Add-LeaveCaution "SESSIONNAME='$sessionName' does not look like an RDP session (RDP-Tcp#N) or the console."
    }
    else {
        Add-LeavePass "in RDP session '$sessionName'"
    }
    $alreadyConsole = ($sessionName -eq 'Console')

    # --- 2. nothing may lock or blank this desktop ----------------------------------------------
    Write-WastedStep 'Check 2/8: anti-lock, screen saver and power settings (applied every run, then read back).'

    Set-LeaveRegistryValue -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' `
        -Name 'InactivityTimeoutSecs' -Value 0 -Type DWord -Why 'no idle session lock' -TakesEffectAtReboot
    Set-LeaveRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' `
        -Name 'NoLockScreen' -Value 1 -Type DWord -Why 'no lock screen image'
    foreach ($hive in @('HKCU:\Software\Policies\Microsoft\Windows\Control Panel\Desktop', 'HKCU:\Control Panel\Desktop')) {
        Set-LeaveRegistryValue -Path $hive -Name 'ScreenSaveActive'    -Value '0' -Type String -Why "screen saver off ($hive)"
        Set-LeaveRegistryValue -Path $hive -Name 'ScreenSaverIsSecure' -Value '0' -Type String -Why "screen saver does not lock ($hive)"
        Set-LeaveRegistryValue -Path $hive -Name 'ScreenSaveTimeOut'   -Value '0' -Type String -Why "screen saver timeout off ($hive)"
    }
    # SetForegroundWindow is refused while the foreground lock is armed, and it fails silently
    # when it is refused. 0 is the registry backing for SPI_SETFOREGROUNDLOCKTIMEOUT.
    Set-LeaveRegistryValue -Path 'HKCU:\Control Panel\Desktop' -Name 'ForegroundLockTimeout' -Value 0 -Type DWord `
        -Why 'foreground lock disabled so the focus keeper can work'
    # A disconnected session that gets logged off takes the game process with it, and then none of
    # the rest of this matters. These two value names come from the Terminal Services ADMX and are
    # written and read back here, but the mapping itself has not been confirmed on this box —
    # confirm once in gpedit.msc: Computer Configuration -> Administrative Templates -> Windows
    # Components -> Remote Desktop Services -> Remote Desktop Session Host -> Session Time Limits.
    Set-LeaveRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services' `
        -Name 'MaxDisconnectionTime' -Value 0 -Type DWord -Why 'a disconnected session is never ended'
    Set-LeaveRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services' `
        -Name 'MaxIdleTime' -Value 0 -Type DWord -Why 'an idle session is never ended'

    # Registry alone binds at next logon; this binds now, in the session that is about to be left.
    if (-not $CheckOnly) {
        $ss = Disable-WastedScreenSaverLive
        if ($null -eq $ss.StillActive) {
            Add-LeaveCaution 'could not read back SPI_GETSCREENSAVEACTIVE — the live screen-saver state is unknown.'
        }
        elseif ($ss.StillActive) {
            Add-LeaveFailure ('the screen saver is STILL enabled in this running session after being turned off. ' +
                'With an inactivity limit configured, a screen saver starting is a documented lock trigger, and a ' +
                'locked session renders the lock screen to the virtual display. Check for a Group Policy forcing it.')
        }
        else {
            Add-LeavePass 'screen saver disabled in the live session (SystemParametersInfo), not just in the registry'
        }
    }

    # Microsoft: with an inactivity limit configured the device also locks "when the display turns
    # off because of power settings". On a headless box that is the likeliest lock trigger of all.
    Invoke-Powercfg -Arguments @('/setactive', '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c') -Why 'high-performance power plan active'
    foreach ($pair in @(
            @{ a = @('/change', 'monitor-timeout-ac', '0');   w = 'monitor never turns off (AC)' },
            @{ a = @('/change', 'monitor-timeout-dc', '0');   w = 'monitor never turns off (DC)' },
            @{ a = @('/change', 'standby-timeout-ac', '0');   w = 'never sleeps (AC)' },
            @{ a = @('/change', 'standby-timeout-dc', '0');   w = 'never sleeps (DC)' },
            @{ a = @('/change', 'hibernate-timeout-ac', '0'); w = 'never hibernates (AC)' },
            @{ a = @('/change', 'disk-timeout-ac', '0');      w = 'disks never spin down (AC)' })) {
        Invoke-Powercfg -Arguments $pair.a -Why $pair.w
    }

    # Read the display timeout back. The text is locale-dependent, so a parse failure is a caution.
    $videoIdle = (& powercfg /query SCHEME_CURRENT SUB_VIDEO VIDEOIDLE 2>&1 | ForEach-Object { [string]$_ }) -join "`n"
    if ($videoIdle -match '(?im)AC Power Setting Index:\s*(0x[0-9a-f]+)') {
        $acIdle = [Convert]::ToInt32($Matches[1], 16)
        if ($acIdle -eq 0) { Add-LeavePass 'display timeout read back as 0 (never)' }
        else { Add-LeaveFailure "the display still turns off after $acIdle seconds on AC — that is a lock trigger. powercfg /change monitor-timeout-ac 0" }
    }
    else {
        Add-LeaveCaution 'could not parse the display timeout out of powercfg (non-English Windows?). Confirm by hand: powercfg /query SCHEME_CURRENT SUB_VIDEO VIDEOIDLE'
    }

    # Is the desktop unlocked right now?
    $desktop = Get-WastedInputDesktopName
    if ($desktop.Name -eq 'Default') {
        Add-LeavePass "input desktop is 'Default' (not locked)"
    }
    else {
        $what = if ($null -eq $desktop.Name) { "could not be opened (win32 error $($desktop.Win32Error))" } else { "is '$($desktop.Name)'" }
        Add-LeaveFailure ("the input desktop $what, not 'Default' — this session is locked or on the secure desktop. " +
            'The virtual display would stream the lock screen, which looks exactly like a frozen game. Unlock it and re-run.')
    }

    # --- 3. is the game up, and is there anything to render onto? -------------------------------
    Write-WastedStep 'Check 3/8: game process and display target.'
    $game = Find-WastedGameWindow -ProcessNames $GameProcessNames -WindowTitles $GameWindowTitles
    if ($null -eq $game) {
        Add-LeaveFailure ("no game process or window found (tried processes $($GameProcessNames -join ', ') and titles " +
            "$($GameWindowTitles -join ', ')). Start the game and get it into Story Mode before leaving. This script does not start it.")
    }
    elseif ($game.WindowMissing) {
        Add-LeaveFailure ("$($game.ProcessName) (pid $($game.ProcessId)) is running but has no top-level window yet — " +
            'still on the launcher or a loading screen. Wait until the game is in Story Mode, then re-run.')
    }
    else {
        Add-LeavePass ("game found: process '$($game.ProcessName)' pid $($game.ProcessId) session $($game.SessionId) " +
            "title '$($game.Title)' (found via $($game.Source))")
        Write-WastedInfo ("  ^ record that process name and window title: harness GAME_WINDOW_TITLES is flagged in-code " +
            'as never confirmed against a running game.')
        if ($sessionName -and $game.SessionId -ge 0) {
            $mySession = ($sessions | Where-Object { $_.Current } | Select-Object -First 1)
            if ($mySession -and $game.SessionId -ne $mySession.Id) {
                Add-LeaveCaution ("the game is in session $($game.SessionId) but you are in session $($mySession.Id). " +
                    'Detaching this session will not help a game living somewhere else.')
            }
        }
    }

    $adapters = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction SilentlyContinue)
    $usable = 0
    foreach ($vc in $adapters) {
        $w = [int]($vc.CurrentHorizontalResolution ?? 0)
        $h = [int]($vc.CurrentVerticalResolution ?? 0)
        Write-WastedInfo ("  adapter '{0}' mode {1}x{2} cmError {3}" -f $vc.Name, $w, $h, [int]($vc.ConfigManagerErrorCode ?? 0))
        if ($w -ge $MinDisplayWidth -and $h -ge $MinDisplayHeight) { $usable++ }
    }
    if ($usable -eq 0) {
        Add-LeaveFailure ("no display adapter reports a desktop mode of at least ${MinDisplayWidth}x${MinDisplayHeight}. " +
            'The virtual display driver has stopped providing a display target: the game cannot present and capture will be black. ' +
            'Check the display device in Device Manager (RUNBOOK Fallback C) before leaving.')
    }
    else {
        Add-LeavePass "$usable display adapter(s) report a >= ${MinDisplayWidth}x${MinDisplayHeight} desktop mode"
    }

    # --- 4. THE REAL TEST -----------------------------------------------------------------------
    Write-WastedStep "Check 4/8: is the game actually running right now? $Samples /health samples, ${SampleGapS}s apart."
    $gamePid = if ($null -ne $game -and -not $game.WindowMissing) { $game.ProcessId } else { 0 }
    # Set only by the one branch below that constitutes direct proof: the game kept ticking and
    # rendering across the whole series while some OTHER window held the foreground.
    $provedRunsUnfocused = $false
    $series = @()
    for ($i = 0; $i -lt $Samples; $i++) {
        if ($i -gt 0) { Start-Sleep -Seconds $SampleGapS }
        $s = Get-HealthSample -GameProcessId $gamePid
        $series += $s
        $whoMarker = ''
        if ($s.GameForeground) { $whoMarker = ' (THE GAME)' }
        $sampleFormat = '  sample {0}: http={1} tick_hz={2} game_fps={3} queue={4} online_blocked={5} foreground={6}{7}'
        Write-WastedInfo ($sampleFormat -f ($i + 1), $s.StatusCode, $s.TickHz, $s.GameFps, $s.QueueDepth, $s.OnlineBlocked, $s.ForegroundName, $whoMarker)
    }

    $unreachable = @($series | Where-Object { -not $_.Reachable })
    $gameHadFocus = @($series | Where-Object { $_.GameForeground }).Count -gt 0

    if ($unreachable.Count -gt 0) {
        $first = $unreachable[0]
        if ($first.StatusCode -eq 503) {
            Add-LeaveFailure ('the bridge returned 503 — it detected an online session and has disabled itself. The agent is ' +
                'Story Mode only; that latch clears only when the game restarts. Restart the game in Story Mode before leaving.')
        }
        elseif ($first.StatusCode -eq 0) {
            Add-LeaveFailure ("$BaseUrl/health is unreachable ($($first.Detail)). Either the game is not running, or Script Hook V / " +
                'SHVDN did not load the bridge. Do not leave: nothing would be measurable from outside. Check the game and ' +
                'C:\wasted\logs, and re-deploy the bridge if needed (deploy-bridge.ps1, then bridge-smoke.ps1).')
        }
        else {
            Add-LeaveFailure "$BaseUrl/health returned HTTP $($first.StatusCode): $($first.Detail)"
        }
    }
    else {
        $ticks = @($series | ForEach-Object { $_.TickHz })
        $fps = @($series | ForEach-Object { $_.GameFps })
        $tickDead = @($ticks | Where-Object { [double]::IsNaN($_) -or $_ -le 0 }).Count -gt 0
        $fpsKnown = @($fps | Where-Object { -not [double]::IsNaN($_) }).Count -eq $series.Count
        $fpsMoved = $false
        if ($fpsKnown) {
            for ($i = 1; $i -lt $fps.Count; $i++) { if ($fps[$i] -ne $fps[$i - 1]) { $fpsMoved = $true } }
        }

        if ($series[-1].OnlineBlocked) {
            Add-LeaveFailure 'the bridge reports online_blocked=true — an online session was detected and the bridge latched off. Restart the game in Story Mode.'
        }

        if ($tickDead) {
            if ($gameHadFocus) {
                Add-LeaveFailure ('tick_hz is 0 while the game HAS focus. The script thread has not ticked for over 2 seconds: the game ' +
                    'is sitting on a modal screen (pause menu, MISSION FAILED, a prompt) or SHVDN has stopped. Clear the screen ' +
                    'and get back into Story Mode, then re-run. Leaving now would stream a still frame.')
            }
            else {
                Add-LeaveFailure ('tick_hz is 0 while the game does NOT have focus. This is pause-on-focus-loss, and it is the whole ' +
                    'problem: the game freezes the moment its window is not focused, so it freezes the moment you leave. FIX IT NOW: ' +
                    'in the game press Esc -> Settings -> Graphics -> "Pause Game On Focus Loss" -> Off; while you are there, ' +
                    'Esc -> Settings -> Audio -> turn the mute-on-focus-loss option Off too, or the stream will move but be silent. ' +
                    'Back out of the menus so the game writes the preference, then re-run this script.')
            }
        }
        elseif (-not $fpsKnown) {
            Add-LeaveCaution 'game_fps was missing from /health — cannot confirm frames are advancing; judging on tick_hz alone.'
            Add-LeavePass "tick_hz above zero across all $Samples samples"
        }
        elseif (-not $fpsMoved) {
            Add-LeaveFailure ("game_fps did not change across $Samples samples over $((($Samples - 1) * $SampleGapS)) seconds " +
                "(stuck at $($fps[0])) even though tick_hz is $($ticks[-1]). game_fps is passed through /health raw, with no staleness " +
                'gate, so it freezes at its last value when the game stops producing frames. The game is not rendering. Do not leave.')
        }
        elseif ($gameHadFocus) {
            Add-LeaveCaution ('the game had the foreground during the samples, so this run proved only that it runs WHEN FOCUSED. ' +
                'The settings.xml check below has to carry the proof instead. To get the direct measurement, click this terminal ' +
                'window so the game is unfocused and run -CheckOnly again.')
        }
        elseif ($gamePid -le 0) {
            Add-LeaveCaution 'the bridge is ticking but no game window was identified, so nothing can be said about focus behaviour.'
        }
        else {
            $provedRunsUnfocused = $true
            Add-LeavePass ("PROVEN: tick_hz $($ticks[-1]) and game_fps advancing ($($fps -join ' -> ')) while the game did NOT have " +
                "the foreground (it was '$($series[-1].ForegroundName)'). The game keeps simulating and rendering unfocused, which " +
                'is exactly what has to be true for it to survive you leaving.')
        }
    }

    # --- 5. settings.xml corroboration -----------------------------------------------------------
    Write-WastedStep 'Check 5/8: Rockstar settings.xml.'
    $settingsXml = Join-Path ([System.Environment]::GetFolderPath('MyDocuments')) 'Rockstar Games\GTA V\settings.xml'
    $pauseOnFocusLoss = Get-GameSettingValue -Path $settingsXml -Name 'PauseOnFocusLoss'
    $windowed = Get-GameSettingValue -Path $settingsXml -Name 'Windowed'
    Write-WastedInfo "  $settingsXml : PauseOnFocusLoss='$pauseOnFocusLoss' Windowed='$windowed'"

    # The measurement from check 4 outranks the file: the file can be stale, and the game rewrites
    # it on exit. But when check 4 could not prove anything — because the game happened to hold the
    # foreground during the samples — the file becomes the only evidence there is, and then a
    # PauseOnFocusLoss that is not 0 has to block.
    $provedUnfocused = $provedRunsUnfocused
    if ($pauseOnFocusLoss -eq '0') {
        Add-LeavePass 'PauseOnFocusLoss = 0 (the game does not pause when it loses focus)'
    }
    elseif ($null -eq $pauseOnFocusLoss) {
        $msg = ("settings.xml has no PauseOnFocusLoss value (or the file is missing at $settingsXml). Set it from the game: " +
                'Esc -> Settings -> Graphics -> "Pause Game On Focus Loss" -> Off, and back out of the menus so it is written. ' +
                'Do not invent the element by hand while the game is running — the game rewrites this file on exit and would clobber it.')
        if ($provedUnfocused) { Add-LeaveCaution $msg } else { Add-LeaveFailure $msg }
    }
    else {
        $msg = ("PauseOnFocusLoss = $pauseOnFocusLoss (the game is set to PAUSE when it loses focus). This is the setting behind " +
                '"the moment I stop, the game freezes". Fix it from the game: Esc -> Settings -> Graphics -> ' +
                '"Pause Game On Focus Loss" -> Off. Also Esc -> Settings -> Audio -> the mute-on-focus-loss option -> Off, ' +
                'which is a Rockstar profile setting and is NOT in this file. Editing settings.xml while the game runs is ' +
                'wasted work: it is rewritten on exit.')
        if ($provedUnfocused) { Add-LeaveCaution ($msg + ' (Downgraded to a warning because check 4 measured the game running unfocused anyway — but fix it, because anything that resets settings.xml will bring the pause back silently.)') }
        else { Add-LeaveFailure $msg }
    }
    if ($windowed -eq '2') {
        Add-LeavePass 'Windowed = 2 (borderless — the only mode that works on a virtual display)'
    }
    elseif ($null -eq $windowed) {
        Add-LeaveCaution "settings.xml has no Windowed value at $settingsXml."
    }
    else {
        Add-LeaveFailure ("Windowed = $windowed, expected 2 (borderless). 0 is exclusive fullscreen, which DXGI relinquishes " +
            'whenever the window is occluded and which cannot be entered over Terminal Server at all. Set borderless ' +
            '(run.ps1 -EnforceDisplaySettings does it with the game closed) before leaving.')
    }

    # --- 6. the two tasks that fix this while you are away ---------------------------------------
    Write-WastedStep 'Check 6/8: recovery tasks.'
    $installer = Join-Path $PSScriptRoot 'install-focus-keeper.ps1'
    $keepaliveOk = $null -ne (Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction SilentlyContinue)
    $foregroundOk = $null -ne (Get-ScheduledTask -TaskName $ForegroundTaskName -ErrorAction SilentlyContinue)

    if ((-not $keepaliveOk -or -not $foregroundOk) -and -not $CheckOnly) {
        Write-WastedWarn "A recovery task is missing — running $installer (idempotent, touches no OBS setting and never starts the harness)."
        if ($PSCmdlet.ShouldProcess('recovery scheduled tasks', 'install via install-focus-keeper.ps1')) {
            $pwshPath = (Get-Process -Id $PID).Path
            & $pwshPath -NoProfile -File $installer 2>&1 | ForEach-Object { Write-WastedInfo "install-focus-keeper: $_" }
            if ($LASTEXITCODE -ne 0) {
                Add-LeaveFailure "install-focus-keeper.ps1 exited $LASTEXITCODE — the recovery tasks are not in place. Run it by hand and read the error."
            }
        }
    }

    if (-not (Test-LeaveTask -Name $KeepaliveTaskName -ExpectedPrincipal 'System' `
                -WhyItMatters 'It is the only thing that re-attaches your parked session to the console after you disconnect; without it the desktop stops rendering entirely.')) {
        if (-not (Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction SilentlyContinue)) {
            Add-LeaveFailure "$KeepaliveTaskName does not exist. Nothing would re-attach the session after you disconnect. Run: pwsh -File $installer"
        }
    }
    if (-not (Test-LeaveTask -Name $ForegroundTaskName -ExpectedPrincipal 'Interactive' `
                -WhyItMatters 'It is the only thing that gives the game window the foreground back after a re-attach; a SYSTEM task cannot do it, because session 0 cannot touch session 1 windows.')) {
        if (-not (Get-ScheduledTask -TaskName $ForegroundTaskName -ErrorAction SilentlyContinue)) {
            Add-LeaveFailure "$ForegroundTaskName does not exist. After a re-attach nothing would refocus the game. Run: pwsh -File $installer"
        }
    }

    # --- 7. the harness: look, report, do not touch ----------------------------------------------
    Write-WastedStep 'Check 7/8: harness (report only — this script never starts the agent).'
    $harness = @(Get-CimInstance -ClassName Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'wasted_harness' })
    if ($harness.Count -eq 0) {
        Add-LeavePass 'the harness is NOT running — which is correct at this point. Ask for the agent to be started after this window closes.'
    }
    else {
        Add-LeaveCaution ("the harness IS already running (pid $(($harness | ForEach-Object { $_.ProcessId }) -join ', ')). It was started " +
            'while you were connected, so its screen capture is bound to the Microsoft Remote Display Adapter, which disappears when ' +
            'you disconnect — Desktop Duplication will then fail with DXGI_ERROR_SESSION_DISCONNECTED. Leave anyway if you like, but ' +
            'the agent has to be restarted after you are gone. This script will not do it.')
    }

    # --- 8. give the game the foreground ---------------------------------------------------------
    Write-WastedStep 'Check 8/8: game window foreground.'
    if ($null -eq $game -or $game.WindowMissing) {
        Write-WastedWarn 'No game window to focus (already reported above).'
    }
    elseif ($CheckOnly) {
        $fgNow = Test-WastedGameForeground -GameProcessId $game.ProcessId
        if ($fgNow.IsForeground) { Add-LeavePass 'the game already has the foreground' }
        else { Add-LeaveCaution "the game does not have the foreground right now (it is '$($fgNow.Foreground.ProcessName)'). A normal run takes it just before detaching." }
    }
    # The real focus attempt happens after the summary, so that this window stays readable.

    # --- verdict ---------------------------------------------------------------------------------
    Write-Host ''
    Write-WastedStep "Verdict: $($script:Failures.Count) blocking problem(s), $($script:Cautions.Count) warning(s)."
    if ($script:Cautions.Count -gt 0) {
        Write-Host ''
        Write-WastedWarn 'Warnings (not blocking):'
        for ($i = 0; $i -lt $script:Cautions.Count; $i++) { Write-WastedWarn "  $($i + 1). $($script:Cautions[$i])" }
    }
    if ($script:Failures.Count -gt 0) {
        Write-Host ''
        Write-WastedError 'REFUSING TO DETACH. Fix these, then run this script again:'
        for ($i = 0; $i -lt $script:Failures.Count; $i++) { Write-WastedError "  $($i + 1). $($script:Failures[$i])" }
        Write-Host ''
        Write-WastedError 'Nothing was detached. Your session is exactly as you left it.'
        exit 1
    }

    if ($CheckOnly) {
        Write-Host ''
        Write-WastedStep '-CheckOnly: every check passed and nothing was changed or detached. Re-run without -CheckOnly to leave.'
        exit 0
    }
    if ($alreadyConsole) {
        Write-Host ''
        Write-WastedStep 'Every check passed. You are already on the console session, so there is nothing to detach.'
        exit 0
    }

    # --- what happens next -----------------------------------------------------------------------
    Write-Host ''
    Write-Host '================ WHAT HAPPENS NEXT ================' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  1. The game window takes the foreground. Your screen will be covered by the game.'
    Write-Host '  2. This session is handed to the console (tscon). YOUR REMOTE DESKTOP WINDOW WILL'
    Write-Host '     CLOSE. That is the success signal, not a crash. Do not reconnect to "check".'
    Write-Host '  3. The machine keeps running: game, bridge and OBS all carry on. If Windows ever'
    Write-Host '     parks the session again, WASTED-ConsoleKeepalive re-attaches it within about a'
    Write-Host '     minute and WASTED-ForegroundAssert gives the game window its focus back.'
    Write-Host ''
    Write-Host '  THEN, FROM YOUR OWN MACHINE (not a new RDP session):'
    Write-Host '   * Look at Twitch. The picture must be MOVING. A still picture is a frozen game.'
    Write-Host '   * Look at the site heartbeat. It must be fresh.'
    Write-Host ''
    Write-Host '  AND THEN ASK FOR WANTED TO BE STARTED.' -ForegroundColor Yellow
    Write-Host '   the agent (the harness) is deliberately NOT running. His screen capture binds to'
    Write-Host '   whatever display adapter exists when he starts; while you were connected that was'
    Write-Host '   the Remote Desktop display adapter, and it disappears the moment you disconnect.'
    Write-Host '   Starting him only after you have gone is what makes him bind to the virtual'
    Write-Host '   display instead. Nothing here will start him for you.'
    Write-Host ''
    Write-Host '  IF THE PICTURE IS FROZEN when you look: see RUNBOOK section "Starting the show and'
    Write-Host '  leaving without freezing it" -> the symptom table. Read'
    Write-Host '  C:\wasted\logs\console-keepalive.log and C:\wasted\logs\foreground-assert.log;'
    Write-Host '  they are the black box for exactly this.'
    Write-Host ''
    Write-Host '===================================================' -ForegroundColor Cyan
    Write-Host ''

    if (-not $Yes -and [System.Environment]::UserInteractive) {
        Read-Host 'Press Enter to focus the game and detach (Ctrl+C to abort)' | Out-Null
    }

    # Focus last, because it covers this window.
    $focus = Set-WastedGameForeground -Hwnd $game.Hwnd -GameProcessId $game.ProcessId
    if (-not $focus.Success -and $focus.Method -ne 'skipped-whatif') {
        Write-WastedError ("Could not give the game the foreground: it is still '$($focus.After.ProcessName)' " +
            "(pid $($focus.After.ProcessId)). SetForegroundWindow returned $($focus.Rc), AttachThreadInput=$($focus.Attached). " +
            'REFUSING TO DETACH — leaving now would very likely freeze the stream. Click the game window yourself and re-run, ' +
            'or check HKCU\Control Panel\Desktop\ForegroundLockTimeout.')
        exit 1
    }
    Write-WastedInfo "Game foreground confirmed by PID ($($focus.Method))."

    # --- detach ------------------------------------------------------------------------------------
    # Reusing detach-rdp.ps1 rather than duplicating tscon: it is the audited path (hard elevation
    # requirement, empty/console SESSIONNAME handled, tscon exit code checked, and every failure of
    # it happens BEFORE any disconnect, so it cannot strand anyone). A child pwsh inherits this
    # session and this elevation, so tscon still runs from the session being redirected.
    $detach = Join-Path $PSScriptRoot 'detach-rdp.ps1'
    if (-not (Test-Path -LiteralPath $detach)) {
        Write-WastedError "detach-rdp.ps1 is missing next to this script — not detaching. Run: tscon $sessionName /dest:console"
        exit 1
    }
    if ($PSCmdlet.ShouldProcess("session $sessionName", 'hand the desktop to the console via detach-rdp.ps1')) {
        Write-WastedStep 'Detaching now. Goodnight.'
        $pwshPath = (Get-Process -Id $PID).Path
        & $pwshPath -NoProfile -File $detach
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            Write-WastedError "detach-rdp.ps1 exited $code — the session was NOT redirected and you are still connected. Read its transcript in C:\wasted\logs."
            exit 1
        }
    }
    exit 0
}
catch {
    Write-WastedError "leave-safely.ps1 failed: $($_.Exception.Message)"
    Write-WastedError $_.ScriptStackTrace
    Write-WastedError 'Nothing was detached.'
    exit 1
}
finally {
    Stop-WastedTranscript
}
