#Requires -Version 7.0
<#
.SYNOPSIS
    Register the two scheduled tasks that keep the desktop attached and the game window focused.
.DESCRIPTION
    Run once, elevated, in the operator's interactive session. Idempotent — re-running verifies
    and reports rather than churning.

    It registers:

    1. WASTED-ForegroundAssert — assert-game-foreground.ps1, LogonType = **Interactive**, run as
       the logged-on (autologon) user, highest privileges.
       THE LOGON TYPE IS THE WHOLE POINT. A SYSTEM task runs in Terminal Services session 0 on a
       non-interactive window station and cannot see, activate or attach to any window in the
       interactive session, so a SYSTEM focus task would be a bug that logs confident nonsense
       forever. This script registers Interactive and then reads the task back and FAILS if
       Windows did not honour it.
       Triggers: at logon of that user; a session-state-change trigger on ConsoleConnect (which
       is precisely what `tscon <id> /dest:console` produces); and a one-minute repeating
       backstop, because the keepalive is itself on a timer and a ConsoleConnect can land in a
       gap. The script is quiet when nothing needs doing, so the backstop costs a log line only
       when it actually fixes something.

    2. WASTED-ConsoleKeepalive — console-keepalive.ps1 as SYSTEM (tscon needs the privilege),
       every minute. This task was previously installed by hand on the server and existed in no
       repo file, so nothing could verify its trigger or its action path. It is only created here
       if it is missing, or with -ReplaceKeepalive.
       Optionally a second SYSTEM task, WASTED-ConsoleKeepalive-OnDisconnect, triggered by
       TerminalServices-LocalSessionManager/Operational event 24 (session disconnected), so the
       re-attach happens within seconds instead of up to a minute. That event id is NOT assumed:
       the script queries the log first and skips the trigger, loudly, if this box has never
       written one.

    Nothing here touches OBS. Nothing here starts, stops or schedules the harness.
.PARAMETER RunAsUser
    Account for the interactive task. Default: the autologon user from
    HKLM\...\Winlogon\DefaultUserName, falling back to the account running this script.
.PARAMETER ReplaceKeepalive
    Re-register WASTED-ConsoleKeepalive even if it already exists (it may have been created by
    hand with an action pointing somewhere stale).
.EXAMPLE
    pwsh -File .\install-focus-keeper.ps1
.EXAMPLE
    pwsh -File .\install-focus-keeper.ps1 -ReplaceKeepalive -Verbose
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$RunAsUser = '',
    [string]$ForegroundTaskName = 'WASTED-ForegroundAssert',
    [string]$KeepaliveTaskName = 'WASTED-ConsoleKeepalive',
    [string]$KeepaliveEventTaskName = 'WASTED-ConsoleKeepalive-OnDisconnect',
    [int]$BackstopIntervalMin = 1,
    [switch]$ReplaceKeepalive,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
. $commonPath

Assert-WastedEnvironment -ScriptName 'install-focus-keeper.ps1' -RequireWastedRoot -RequireElevation -Force:$Force
Start-WastedTranscript -Name 'install-focus-keeper' | Out-Null

$tsSchema = 'Root/Microsoft/Windows/TaskScheduler'
$assertScript = Join-Path $PSScriptRoot 'assert-game-foreground.ps1'
$keepaliveScript = Join-Path $PSScriptRoot 'console-keepalive.ps1'
$disconnectLog = 'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational'
$disconnectEventId = 24

function Resolve-RunAsUser {
    if ($RunAsUser) { return $RunAsUser }
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    $name = $null
    $domain = $null
    $prop = Get-ItemProperty -LiteralPath $winlogon -ErrorAction SilentlyContinue
    if ($prop) {
        if ($prop.PSObject.Properties['DefaultUserName']) { $name = [string]$prop.DefaultUserName }
        if ($prop.PSObject.Properties['DefaultDomainName']) { $domain = [string]$prop.DefaultDomainName }
    }
    if ($name) {
        $resolved = if ($domain) { "$domain\$name" } else { $name }
        Write-WastedInfo "Autologon user from the registry: $resolved"
        return $resolved
    }
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    Write-WastedWarn ("No autologon DefaultUserName in the registry — using the account running this script ($me). " +
                      'If the show runs under a different account, re-run with -RunAsUser.')
    return $me
}

function Get-PwshPath {
    $cmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'pwsh is not on PATH — cannot register a task that runs a PowerShell 7 script.' }
    return $cmd.Source
}

try {
    foreach ($p in @($assertScript, $keepaliveScript)) {
        if (-not (Test-Path -LiteralPath $p)) { throw "Missing $p — run from a full checkout of scripts/." }
    }
    $pwshPath = Get-PwshPath
    $user = Resolve-RunAsUser
    Write-WastedInfo "pwsh: $pwshPath"

    # --- 1. the interactive foreground task -----------------------------------------------------
    Write-WastedStep "Step 1/3: $ForegroundTaskName (interactive, runs in the logged-on session)."

    $assertAction = New-ScheduledTaskAction -Execute $pwshPath `
        -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $assertScript) `
        -WorkingDirectory $PSScriptRoot

    # Plain array, not a typed List: Register-ScheduledTask's binder coerces each element to
    # CimInstance, and naming that type here would tie the script to an assembly load order.
    $triggers = @(New-ScheduledTaskTrigger -AtLogOn -User $user)

    # Session-state-change triggers have no New-ScheduledTaskTrigger switch; they only exist as
    # a CIM class. ConsoleConnect (TASK_CONSOLE_CONNECT = 1) is what tscon /dest:console raises;
    # SessionUnlock (8) covers the operator unlocking a locked desktop.
    $sscOk = $true
    try {
        $sscClass = Get-CimClass -ClassName MSFT_TaskSessionStateChangeTrigger -Namespace $tsSchema -ErrorAction Stop
        foreach ($state in @(1, 8)) {
            $t = New-CimInstance -CimClass $sscClass -ClientOnly
            $t.StateChange = $state
            $t.UserId = $user
            $t.Enabled = $true
            $triggers += $t
        }
    }
    catch {
        $sscOk = $false
        Write-WastedWarn ("Could not build a session-state-change trigger ($($_.Exception.Message)). " +
            "Falling back to the ${BackstopIntervalMin}-minute backstop only, which means up to " +
            "$BackstopIntervalMin minute(s) of frozen stream after a re-attach instead of seconds.")
    }

    # Backstop: repeat forever. Registered even when ConsoleConnect worked, because the keepalive
    # that produces the ConsoleConnect is itself on a timer.
    $backstop = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $BackstopIntervalMin)
    $triggers += $backstop

    $assertSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    $assertPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

    if ($PSCmdlet.ShouldProcess($ForegroundTaskName, "register interactive task for $user")) {
        Register-ScheduledTask -TaskName $ForegroundTaskName -Action $assertAction -Trigger $triggers `
            -Principal $assertPrincipal -Settings $assertSettings -Force | Out-Null

        # Read it back. If Windows did not honour Interactive, the task is in session 0 and can
        # never set a foreground window — that must be a hard failure, not a shrug.
        $registered = Get-ScheduledTask -TaskName $ForegroundTaskName -ErrorAction Stop
        $logonType = [string]$registered.Principal.LogonType
        $asUser = [string]$registered.Principal.UserId
        Write-WastedInfo "$ForegroundTaskName registered: LogonType=$logonType UserId=$asUser triggers=$($registered.Triggers.Count) sessionStateTrigger=$sscOk"
        if ($logonType -ne 'Interactive') {
            throw ("$ForegroundTaskName registered with LogonType='$logonType', not 'Interactive'. " +
                   'It would run in session 0, where SetForegroundWindow cannot reach the game window. ' +
                   'Re-run in the interactive session with -RunAsUser <the logged-on account>.')
        }
    }

    # --- 2. the SYSTEM keepalive ----------------------------------------------------------------
    Write-WastedStep "Step 2/3: $KeepaliveTaskName (SYSTEM, tscon)."
    $existing = Get-ScheduledTask -TaskName $KeepaliveTaskName -ErrorAction SilentlyContinue
    if ($existing -and -not $ReplaceKeepalive) {
        $execs = @($existing.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" })
        Write-WastedInfo "$KeepaliveTaskName already exists (State=$($existing.State), RunAs=$($existing.Principal.UserId)). Action: $($execs -join ' ; ')"
        Write-WastedInfo 'Left alone. Re-run with -ReplaceKeepalive to point it at this checkout.'
    }
    elseif ($PSCmdlet.ShouldProcess($KeepaliveTaskName, 'register SYSTEM keepalive task')) {
        $kaAction = New-ScheduledTaskAction -Execute $pwshPath `
            -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $keepaliveScript) `
            -WorkingDirectory $PSScriptRoot
        $kaTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes 1)
        $kaPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $kaSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName $KeepaliveTaskName -Action $kaAction -Trigger $kaTrigger `
            -Principal $kaPrincipal -Settings $kaSettings -Force | Out-Null
        Write-WastedInfo "$KeepaliveTaskName registered: SYSTEM, every minute -> $keepaliveScript"
    }

    # --- 3. the on-disconnect event trigger, only if this box really logs that event -------------
    Write-WastedStep "Step 3/3: $KeepaliveEventTaskName (event-driven re-attach, verified before use)."
    $sawEvent = $false
    try {
        $evt = Get-WinEvent -FilterHashtable @{ LogName = $disconnectLog; Id = $disconnectEventId } -MaxEvents 1 -ErrorAction Stop
        if ($evt) {
            $sawEvent = $true
            Write-WastedInfo ("Event $disconnectEventId in '$disconnectLog' confirmed on this machine " +
                "(most recent: $($evt.TimeCreated.ToString('o'))).")
        }
    }
    catch {
        Write-WastedWarn ("No event $disconnectEventId found in '$disconnectLog' ($($_.Exception.Message)). " +
            'NOT registering the event-driven re-attach — an unverified event filter would be a task that ' +
            'silently never fires. Disconnect RDP once, then re-run this script to pick it up.')
    }

    if ($sawEvent -and $PSCmdlet.ShouldProcess($KeepaliveEventTaskName, 'register event-triggered keepalive')) {
        $evtClass = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace $tsSchema -ErrorAction Stop
        $evtTrigger = New-CimInstance -CimClass $evtClass -ClientOnly
        $evtTrigger.Subscription = ('<QueryList><Query Id="0" Path="{0}"><Select Path="{0}">*[System[EventID={1}]]</Select></Query></QueryList>' -f $disconnectLog, $disconnectEventId)
        $evtTrigger.Enabled = $true

        $evtAction = New-ScheduledTaskAction -Execute $pwshPath `
            -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $keepaliveScript) `
            -WorkingDirectory $PSScriptRoot
        $evtPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $evtSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName $KeepaliveEventTaskName -Action $evtAction -Trigger $evtTrigger `
            -Principal $evtPrincipal -Settings $evtSettings -Force | Out-Null
        Write-WastedInfo "$KeepaliveEventTaskName registered: on event $disconnectEventId, SYSTEM. The one-minute timer task stays as the backstop."
    }

    Write-WastedStep 'Focus keeper installed. Verify with: Get-ScheduledTask WASTED-* | Format-Table TaskName,State; and after a disconnect cycle read C:\wasted\logs\foreground-assert.log.'
    exit 0
}
catch {
    Write-WastedError "install-focus-keeper.ps1 failed: $($_.Exception.Message)"
    Write-WastedError $_.ScriptStackTrace
    exit 1
}
finally {
    Stop-WastedTranscript
}
