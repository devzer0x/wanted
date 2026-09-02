<#
.SYNOPSIS
    One-time configuration run at the first logon of a freshly installed WASTED game server.

.DESCRIPTION
    Everything here exists to make the machine reachable and keep it reachable, before any
    game-specific setup happens. It runs inside the QEMU installer VM; the parts that depend on
    real hardware (the static address) are no-ops there and take effect on the first real boot.

    Deliberately establishes TWO independent ways in - RDP and OpenSSH. RDP is needed for the
    human's Steam and Rockstar logins and for watching the game; SSH is what everything else is
    automated over, and it is the fallback if the graphics/display work ever kills the desktop.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$LogDir  = 'C:\wasted\logs'
$LogFile = Join-Path $LogDir 'firstboot.log'

function Write-Log {
    param([string]$Message)
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
    $line = "{0}  {1}" -f [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'), $Message
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

function Step {
    param([string]$Name, [scriptblock]$Body)
    Write-Log "==> $Name"
    try { & $Body; Write-Log "    ok: $Name" }
    catch { Write-Log "    FAILED: $Name :: $($_.Exception.Message)" }
}

Write-Log '=========== WASTED first boot ==========='

# The previous install produced a machine whose logon account was NOT an administrator (OOBE's
# group-add failed with ERROR_NO_SUCH_MEMBER). Every step below then failed silently - even
# "md C:\wasted" is denied to a standard user. Check once, loudly, up front.
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Log "running as: $env:USERNAME    elevated/administrator: $isAdmin"
if (-not $isAdmin) {
    Write-Log 'FATAL: not running with administrator rights - nothing below can succeed.'
    Write-Log 'Leaving the machine powered ON so this is visible rather than silently broken.'
    exit 1
}
Write-Log "OS      : $((Get-CimInstance Win32_OperatingSystem).Caption)"
Write-Log "Build   : $([System.Environment]::OSVersion.Version)"
Write-Log "Host    : $env:COMPUTERNAME"

# ---------------------------------------------------------------- network (real hardware only)
Step 'Register WASTED-Network scheduled task (runs at every boot)' {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument '-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File C:\wasted\wasted-network.ps1'
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'WASTED-Network' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
}

Step 'Apply network configuration now' {
    & powershell.exe -ExecutionPolicy Bypass -NoProfile -File C:\wasted\wasted-network.ps1
}

# ---------------------------------------------------------------- access path 1: RDP
Step 'Enable RDP' {
    Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' `
        -Name 'fDenyTSConnections' -Value 0 -Type DWord
    Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' `
        -Name 'UserAuthentication' -Value 1 -Type DWord
    Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue
    Set-Service -Name TermService -StartupType Automatic
    Start-Service -Name TermService -ErrorAction SilentlyContinue
}

# ------------------------------------------------- access path 2: OpenSSH (needs real internet)
Step 'Register WASTED-Provision scheduled task (installs OpenSSH once online)' {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument '-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File C:\wasted\wasted-provision.ps1'
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName 'WASTED-Provision' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
}

# ---------------------------------------------------------------- keep the box awake and present
Step 'Power settings: never sleep, never blank the display' {
    powercfg /setactive SCHEME_MIN            # High performance
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /change monitor-timeout-ac 0
    powercfg /change disk-timeout-ac 0
    powercfg /hibernate off
}

Step 'Do not lock the screen (the game needs a live interactive session)' {
    New-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' -Force | Out-Null
    Set-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' `
        -Name 'NoLockScreen' -Value 1 -Type DWord
    Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name 'ScreenSaveActive' -Value '0'
    Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name 'ScreenSaverIsSecure' -Value '0'
}

Step 'Report' {
    "firstboot completed $([DateTime]::UtcNow.ToString('o'))" |
        Set-Content 'C:\wasted\FIRSTBOOT-COMPLETE.txt'
}

Write-Log '=========== WASTED first boot done ==========='

# The install VM has no network, so there is no service to probe from outside. Powering off is
# the completion signal: when the QEMU process exits, the install finished successfully.
Write-Log 'shutting down to signal the installer that configuration is complete'
Start-Sleep -Seconds 5
& shutdown.exe /s /t 5 /c "WASTED first-boot complete" 
