<#
.SYNOPSIS
    Finishes setting up the machine - the parts that need a working internet connection.

.DESCRIPTION
    Split out of wasted-firstboot.ps1 deliberately. The Windows install runs in an offline VM
    (a half-working QEMU NAT is what broke the first attempt: Windows detected "internet",
    committed to online code paths, then timed out on all 39 of them and OOBE gave up). So
    anything needing real connectivity cannot run during the install.

    This runs at every boot instead, and does nothing once it has succeeded. On the first real
    boot - after wasted-network.ps1 has applied the static address - it finds a genuine internet
    connection and completes.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$LogDir  = 'C:\wasted\logs'
$LogFile = Join-Path $LogDir 'provision.log'
$Marker  = 'C:\wasted\PROVISION-COMPLETE.txt'

function Write-Log {
    param([string]$Message)
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
    $line = "{0}  {1}" -f [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'), $Message
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

if (Test-Path $Marker) { Write-Log 'already provisioned - nothing to do'; exit 0 }

Write-Log '--- wasted-provision.ps1 start ---'

# Only proceed once the machine can actually reach the internet, otherwise leave the marker
# unset so this runs again at the next boot.
#
# Deliberately NOT an ICMP ping. Outbound ICMP is blocked often enough - by hosting providers,
# by firewall policy - that "ping fails" is a bad proxy for "no internet", and a false negative
# here silently skips the whole of provisioning. What we actually need is an outbound TCP
# connection to Windows Update, so test exactly that.
function Test-Online {
    foreach ($t in @(@{h='windowsupdate.microsoft.com';p=443},
                     @{h='www.microsoft.com';       p=443},
                     @{h='1.1.1.1';                 p=443})) {
        try {
            $c = [System.Net.Sockets.TcpClient]::new()
            if ($c.ConnectAsync($t.h, $t.p).Wait(5000) -and $c.Connected) { $c.Close(); return $true }
            $c.Close()
        } catch { }
    }
    return $false
}
$online = Test-Online
Write-Log "internet reachable (tcp/443): $online"
if (-not $online) {
    Write-Log 'offline - deferring to next boot'
    exit 0
}

$ok = $true

try {
    Write-Log 'installing OpenSSH Server'
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
    if ($cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
    Set-Service -Name sshd -StartupType Automatic
    Start-Service -Name sshd
    Write-Log "sshd state: $((Get-Service sshd).Status)"
} catch { Write-Log "FAILED installing OpenSSH: $($_.Exception.Message)"; $ok = $false }

try {
    $keySrc = 'C:\wasted\authorized_keys'
    $keyDst = 'C:\ProgramData\ssh\administrators_authorized_keys'
    if (Test-Path $keySrc) {
        New-Item -ItemType Directory -Force -Path 'C:\ProgramData\ssh' | Out-Null
        Copy-Item $keySrc $keyDst -Force
        # Windows OpenSSH ignores this file unless only Administrators/SYSTEM can write it.
        icacls $keyDst /inheritance:r         | Out-Null
        icacls $keyDst /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
        Restart-Service sshd -ErrorAction SilentlyContinue
        Write-Log 'ssh key authorised'
    } else { Write-Log "no key payload at $keySrc"; $ok = $false }
} catch { Write-Log "FAILED authorising key: $($_.Exception.Message)"; $ok = $false }

try {
    if (-not (Get-NetFirewallRule -Name 'WASTED-SSH' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'WASTED-SSH' -DisplayName 'WASTED OpenSSH (22)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    }
    Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue
    Write-Log 'firewall rules in place'
} catch { Write-Log "FAILED firewall: $($_.Exception.Message)"; $ok = $false }

try {
    # The install VM has no network, so the evaluation could never activate and Windows shows a
    # "license is expired" watermark. With real connectivity this clears it and starts the 90 days.
    Write-Log 'activating the Windows evaluation'
    $out = & cscript.exe //Nologo "$env:SystemRoot\System32\slmgr.vbs" /ato 2>&1 | Out-String
    Write-Log ("slmgr /ato: " + ($out -replace '\s+', ' ').Trim())
    $st = & cscript.exe //Nologo "$env:SystemRoot\System32\slmgr.vbs" /dli 2>&1 | Out-String
    Write-Log ("license status: " + ($st -replace '\s+', ' ').Trim())
} catch { Write-Log "FAILED activating Windows: $($_.Exception.Message)" }

if ($ok) {
    "provisioned $([DateTime]::UtcNow.ToString('o'))" | Set-Content $Marker
    Write-Log 'provisioning COMPLETE - will not run again'
} else {
    Write-Log 'provisioning incomplete - will retry at next boot'
}
Write-Log '--- wasted-provision.ps1 end ---'
