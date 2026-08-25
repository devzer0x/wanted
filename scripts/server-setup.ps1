#Requires -Version 7.0
<#
.SYNOPSIS
    One-shot, idempotent bring-up of the WASTED Windows Server 2025 game server (PLAN Phase 0a).
.DESCRIPTION
    Installs the toolchain via winget, enables audio services, stages VB-CABLE and Sysinternals
    Autologon (interactive parts documented and prompted, never stored), applies the
    lock-screen / idle / screensaver / Windows Update registry policy, activates the
    high-performance power plan, enables OpenSSH with PowerShell 7 as default shell, restricts
    inbound RDP/SSH to -AdminIP, registers the console-session logon task for run.ps1, and
    documents + verifies the NVIDIA driver step. Safe to re-run; every step checks current state.

    Bootstrap on a fresh server (this script needs PowerShell 7):
        winget install --exact --id Microsoft.PowerShell --silent --accept-package-agreements --accept-source-agreements
.PARAMETER AdminIP
    IP address or CIDR (comma-separate multiple) allowed to reach RDP (3389) and SSH (22).
    Get the wrong value and you lock yourself out — double-check it.
.PARAMETER WastedRoot
    Root of the WASTED server layout. Default C:\wasted.
.PARAMETER RepoDir
    Where this repo is (or will be) cloned on the server. The logon task points at
    <RepoDir>\scripts\run.ps1. Default C:\wasted\repo.
.PARAMETER AutologonUser
    Account for Sysinternals Autologon and the logon task. Default: current user.
.EXAMPLE
    pwsh -File .\server-setup.ps1 -AdminIP 203.0.113.7
.EXAMPLE
    pwsh -File .\server-setup.ps1 -AdminIP 203.0.113.0/24 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$AdminIP,
    [string]$WastedRoot = 'C:\wasted',
    [string]$RepoDir = 'C:\wasted\repo',
    [string]$AutologonUser = [System.Environment]::UserName,
    [switch]$SkipVbCable,
    [switch]$SkipAutologon,
    [switch]$SkipFirewall,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
$script:WastedRoot = $WastedRoot
. $commonPath

Assert-WastedEnvironment -ScriptName 'server-setup.ps1' -RequireServerSku -RequireElevation -Force:$Force

# --- input validation -------------------------------------------------------------------------
$adminAddresses = @($AdminIP -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($entry in $adminAddresses) {
    $addressPart = ($entry -split '/')[0]
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($addressPart, [ref]$parsed)) {
        throw "-AdminIP entry '$entry' is not a valid IP address or CIDR."
    }
    if ($entry.Contains('/')) {
        $prefix = ($entry -split '/')[1]
        if ($prefix -notmatch '^\d{1,3}$' -or [int]$prefix -gt 128) {
            throw "-AdminIP entry '$entry' has an invalid CIDR prefix."
        }
    }
}
if ($adminAddresses.Count -eq 0) { throw '-AdminIP resolved to an empty list.' }

# --- layout (always created; everything else logs into it) ------------------------------------
$layoutDirs = @($WastedRoot) + @('logs', 'state', 'downloads', 'tools' | ForEach-Object { Join-Path $WastedRoot $_ })
foreach ($dir in $layoutDirs) {
    New-Item -ItemType Directory -Path $dir -Force -WhatIf:$false -Confirm:$false | Out-Null
}

Start-WastedTranscript -Name 'server-setup' | Out-Null
$manualActions = [System.Collections.Generic.List[string]]::new()
$warningsCount = 0

# --- step helpers -----------------------------------------------------------------------------

function Set-WastedRegistryValue {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][object]$Value,
        [Parameter(Mandatory)][Microsoft.Win32.RegistryValueKind]$Type,
        [Parameter(Mandatory)][string]$Why
    )
    $current = $null
    if (Test-Path -LiteralPath $Path) {
        $prop = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction SilentlyContinue
        if ($null -ne $prop) { $current = $prop.$Name }
    }
    if ("$current" -eq "$Value") {
        Write-WastedInfo "$Path\$Name already = $Value ($Why) — skipping."
        return
    }
    if ($PSCmdlet.ShouldProcess("$Path\$Name", "set to $Value ($Why)")) {
        New-Item -Path $Path -Force | Out-Null
        New-ItemProperty -LiteralPath $Path -Name $Name -Value $Value -PropertyType $Type -Force | Out-Null
        Write-WastedInfo "$Path\$Name = $Value ($Why)."
    }
}

function Get-WastedDownload {
    # Idempotent download into <root>\downloads; returns the local path.
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$FileName
    )
    $target = Join-Path (Join-Path $script:WastedRoot 'downloads') $FileName
    if (Test-Path -LiteralPath $target) {
        Write-WastedInfo "$FileName already downloaded — reusing $target."
        return $target
    }
    Write-WastedInfo "Downloading $Url -> $target"
    Invoke-WebRequest -Uri $Url -OutFile $target -UserAgent 'wasted-ops/1.0'
    return $target
}

# --- steps ------------------------------------------------------------------------------------

function Install-Toolchain {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep 'Step 1/10: toolchain via winget.'
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget not found. Windows Server 2025 ships it; if missing, install "App Installer" from Microsoft, then re-run.'
    }
    # IDs verified against microsoft/winget-pkgs manifests, 2026-08-25.
    $packages = @(
        @{ Id = 'Microsoft.PowerShell';                    Why = 'PowerShell 7 (ops scripts, SSH default shell)' }
        @{ Id = 'Git.Git';                                 Why = 'repo checkout' }
        @{ Id = 'Python.Python.3.12';                      Why = 'harness runtime (>=3.12)' }
        @{ Id = 'OpenJS.NodeJS.LTS';                       Why = 'web tooling' }
        @{ Id = 'Microsoft.DotNet.SDK.8';                  Why = 'bridge build (dotnet build)' }
        @{ Id = 'Microsoft.DotNet.Framework.DeveloperPack_4'; Why = '.NET Framework 4.8 targeting pack (bridge targets net48)' }
        @{ Id = 'OBSProject.OBSStudio';                    Why = 'stream + replay buffer (obs-websocket v5 bundled since OBS 28)' }
        @{ Id = 'Valve.Steam';                             Why = 'game platform (GTA V Legacy, app 271590)' }
        @{ Id = '7zip.7zip';                               Why = 'archive tooling' }
    )
    foreach ($pkg in $packages) {
        winget list --exact --id $pkg.Id --accept-source-agreements *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-WastedInfo "$($pkg.Id) already installed — skipping."
            continue
        }
        if ($PSCmdlet.ShouldProcess($pkg.Id, 'winget install')) {
            Write-WastedInfo "Installing $($pkg.Id) ($($pkg.Why))..."
            winget install --exact --id $pkg.Id --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
            if ($LASTEXITCODE -ne 0) {
                Write-WastedWarn "winget install $($pkg.Id) exited $LASTEXITCODE — install it manually, then re-run to confirm."
                $script:warningsCount++
            }
        }
    }
}

function Enable-AudioServices {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep 'Step 2/10: Windows Audio services (required BEFORE VB-CABLE installs).'
    foreach ($name in @('Audiosrv', 'AudioEndpointBuilder')) {
        $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
        if (-not $svc) { Write-WastedWarn "Service $name not found — unexpected on Server 2025."; $script:warningsCount++; continue }
        if ($svc.StartType -ne 'Automatic' -and $PSCmdlet.ShouldProcess($name, 'set StartupType Automatic')) {
            Set-Service -Name $name -StartupType Automatic
        }
        if ($svc.Status -ne 'Running' -and $PSCmdlet.ShouldProcess($name, 'start service')) {
            Start-Service -Name $name
        }
        $svc = Get-Service -Name $name
        Write-WastedInfo "$name status=$($svc.Status) startType=$($svc.StartType)."
    }
}

function Install-VbCable {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep 'Step 3/10: VB-CABLE virtual audio device.'
    if ($SkipVbCable) { Write-WastedInfo 'Skipped (-SkipVbCable).'; return }
    $existing = Get-CimInstance -ClassName Win32_SoundDevice -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like '*VB-Audio*' -or $_.Name -like '*CABLE*' }
    if ($existing) {
        Write-WastedInfo "VB-CABLE already present: $(@($existing)[0].Name) — skipping install."
        return
    }
    # v45 pack URL verified 2026-08-25 (docs/research/brief-winserver.json).
    $zip = Get-WastedDownload -Url 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip' -FileName 'VBCABLE_Driver_Pack45.zip'
    $dest = Join-Path (Join-Path $script:WastedRoot 'tools') 'vbcable'
    if ($PSCmdlet.ShouldProcess($dest, "extract $zip")) {
        Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
    }
    $setup = Join-Path $dest 'VBCABLE_Setup_x64.exe'
    # Driver installation cannot be fully silent: VB-Audio's installer needs the operator to
    # click "Install Driver" and Windows to confirm the driver signature. Documented manual step.
    $script:manualActions.Add("VB-CABLE: run '$setup' as admin, click 'Install Driver', accept the driver prompt, then REBOOT before OBS audio capture. In OBS, capture the 'CABLE Output' device explicitly — never 'Default' (RDP audio redirection can hijack Default).")
    if ([System.Environment]::UserInteractive -and (Test-Path -LiteralPath $setup)) {
        if ($PSCmdlet.ShouldProcess($setup, 'launch VB-CABLE installer (interactive)')) {
            Write-WastedInfo 'Launching the VB-CABLE installer — click "Install Driver" in its window.'
            Start-Process -FilePath $setup -Verb RunAs -Wait
            Write-WastedInfo 'VB-CABLE installer finished. A reboot is required before the device is usable.'
        }
    }
    else {
        Write-WastedWarn 'Non-interactive session or setup exe missing — VB-CABLE install left as a manual action (see summary).'
        $script:warningsCount++
    }
}

function Install-Autologon {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep 'Step 4/10: Sysinternals Autologon (password becomes an LSA secret; this script never stores it).'
    if ($SkipAutologon) { Write-WastedInfo 'Skipped (-SkipAutologon).'; return }
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    $currentAuto = (Get-ItemProperty -LiteralPath $winlogon -Name AutoAdminLogon -ErrorAction SilentlyContinue)
    if ($currentAuto -and "$($currentAuto.AutoAdminLogon)" -eq '1' -and -not $Force) {
        $who = (Get-ItemProperty -LiteralPath $winlogon -Name DefaultUserName -ErrorAction SilentlyContinue)
        $whoName = if ($who) { $who.DefaultUserName } else { '<unknown>' }
        Write-WastedInfo "Autologon already enabled for '$whoName' — skipping (re-run with -Force to change it)."
        return
    }
    $zip = Get-WastedDownload -Url 'https://download.sysinternals.com/files/AutoLogon.zip' -FileName 'AutoLogon.zip'
    $dest = Join-Path (Join-Path $script:WastedRoot 'tools') 'autologon'
    if ($PSCmdlet.ShouldProcess($dest, "extract $zip")) {
        Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
    }
    $exe = Join-Path $dest 'Autologon64.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        Write-WastedWarn "Autologon64.exe not found under $dest — configure autologon manually."
        $script:warningsCount++
        return
    }
    if (-not [System.Environment]::UserInteractive) {
        Write-WastedWarn 'Non-interactive session — cannot prompt for the password. Run Autologon64.exe manually.'
        $script:manualActions.Add("Autologon: run '$exe' /accepteula $AutologonUser $env:USERDOMAIN <password> in an interactive elevated session.")
        $script:warningsCount++
        return
    }
    if ($PSCmdlet.ShouldProcess("user $AutologonUser", 'configure autologon')) {
        $secure = Read-Host -Prompt "Password for '$AutologonUser' (passed once to Autologon64.exe, stored by Windows as an LSA secret, never by this script)" -AsSecureString
        $plain = [System.Net.NetworkCredential]::new('', $secure).Password
        try {
            & $exe /accepteula $AutologonUser $env:USERDOMAIN $plain
            if ($LASTEXITCODE -ne 0) {
                Write-WastedWarn "Autologon64.exe exited $LASTEXITCODE — autologon may not be configured."
                $script:warningsCount++
            }
            else {
                Write-WastedInfo "Autologon configured for $env:USERDOMAIN\$AutologonUser."
            }
        }
        finally {
            $plain = $null
            $secure = $null
        }
    }
}

function Set-LockdownPolicies {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep 'Step 5/10: lock screen / idle / screensaver / Windows Update policies.'
    Set-WastedRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' `
        -Name NoLockScreen -Value 1 -Type DWord -Why 'no lock screen'
    Set-WastedRegistryValue -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' `
        -Name InactivityTimeoutSecs -Value 0 -Type DWord -Why 'no idle session lock'
    # Screensaver policy is per-user (applies to the autologon account running this setup).
    Set-WastedRegistryValue -Path 'HKCU:\Software\Policies\Microsoft\Windows\Control Panel\Desktop' `
        -Name ScreenSaveActive -Value '0' -Type String -Why 'screensaver off'
    # Notify-only updates (AUOptions 2): NoAutoRebootWithLoggedOnUsers is unreliable for
    # disconnected sessions (RESEARCH.md D9) — maintenance happens in a manual window instead.
    Set-WastedRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' `
        -Name AUOptions -Value 2 -Type DWord -Why 'Windows Update notify-only'
    Set-WastedRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' `
        -Name NoAutoUpdate -Value 0 -Type DWord -Why 'AU enabled, but notify-only per AUOptions'
}

function Set-PowerPlan {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep 'Step 6/10: high-performance power plan, monitor never sleeps.'
    $highPerfGuid = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
    $active = (powercfg /getactivescheme) -join ' '
    if ($active -match $highPerfGuid) {
        Write-WastedInfo 'High-performance plan already active.'
    }
    elseif ($PSCmdlet.ShouldProcess('power plan', "activate high performance ($highPerfGuid)")) {
        powercfg /setactive $highPerfGuid
        if ($LASTEXITCODE -ne 0) { Write-WastedWarn "powercfg /setactive exited $LASTEXITCODE."; $script:warningsCount++ }
    }
    if ($PSCmdlet.ShouldProcess('display timeout', 'monitor-timeout-ac 0')) {
        powercfg /change monitor-timeout-ac 0
        if ($LASTEXITCODE -ne 0) { Write-WastedWarn "powercfg /change exited $LASTEXITCODE."; $script:warningsCount++ }
    }
}

function Enable-OpenSsh {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep 'Step 7/10: OpenSSH server + PowerShell 7 default shell.'
    $svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
    if (-not $svc) {
        # Ships with Server 2025; only older bases need the capability added.
        if ($PSCmdlet.ShouldProcess('OpenSSH.Server capability', 'Add-WindowsCapability')) {
            Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
            $svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
        }
    }
    if ($svc) {
        if ($svc.StartType -ne 'Automatic' -and $PSCmdlet.ShouldProcess('sshd', 'StartupType Automatic')) {
            Set-Service -Name sshd -StartupType Automatic
        }
        if ($svc.Status -ne 'Running' -and $PSCmdlet.ShouldProcess('sshd', 'start')) {
            Start-Service -Name sshd
        }
        Write-WastedInfo "sshd status=$((Get-Service sshd).Status)."
    }
    else {
        Write-WastedWarn 'sshd service unavailable — SSH access will not work until fixed.'
        $script:warningsCount++
    }
    $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwshCmd) {
        Set-WastedRegistryValue -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
            -Value $pwshCmd.Source -Type String -Why 'PowerShell 7 as SSH default shell'
    }
    else {
        Write-WastedWarn 'pwsh not on PATH — DefaultShell left unchanged (re-run after the toolchain step installed PowerShell 7).'
        $script:warningsCount++
    }
    $script:manualActions.Add('SSH keys: append your public key to C:\ProgramData\ssh\administrators_authorized_keys (ACL: SYSTEM + Administrators only), then Restart-Service sshd.')
}

function Set-AdminFirewall {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 8/10: firewall — inbound RDP/SSH only from $($adminAddresses -join ', ')."
    if ($SkipFirewall) { Write-WastedInfo 'Skipped (-SkipFirewall).'; return }
    if (-not $Force -and -not $WhatIfPreference) {
        $warning = "Restrict inbound RDP (3389) and SSH (22) to '$($adminAddresses -join ', ')'. A wrong -AdminIP LOCKS YOU OUT of this server."
        if (-not $PSCmdlet.ShouldContinue($warning, 'Apply firewall allowlist?')) {
            Write-WastedWarn 'Firewall step declined by operator — inbound RDP/SSH remain wide open.'
            $script:warningsCount++
            return
        }
    }
    $rules = @(
        @{ Name = 'WASTED-Admin-RDP'; Display = 'WASTED admin RDP (3389)'; Port = 3389 }
        @{ Name = 'WASTED-Admin-SSH'; Display = 'WASTED admin SSH (22)';   Port = 22 }
    )
    foreach ($rule in $rules) {
        if ($PSCmdlet.ShouldProcess($rule.Display, "allow TCP $($rule.Port) from $($adminAddresses -join ', ')")) {
            Get-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
            New-NetFirewallRule -Name $rule.Name -DisplayName $rule.Display -Direction Inbound `
                -Protocol TCP -LocalPort $rule.Port -RemoteAddress $adminAddresses -Action Allow -Profile Any | Out-Null
            Write-WastedInfo "$($rule.Display) applied."
        }
    }
    # Disable the wide-open built-ins so only the allowlist rules answer.
    if ($PSCmdlet.ShouldProcess('built-in Remote Desktop rules', 'disable (allowlist replaces them)')) {
        Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue |
            Where-Object Direction -eq 'Inbound' | Set-NetFirewallRule -Enabled False
    }
    if ($PSCmdlet.ShouldProcess('built-in OpenSSH-Server-In-TCP rule', 'disable (allowlist replaces it)')) {
        Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Set-NetFirewallRule -Enabled False
    }
}

function Register-RunTask {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep 'Step 9/10: scheduled task WASTED-Run (console session, at logon of the autologon user).'
    $runScript = Join-Path (Join-Path $RepoDir 'scripts') 'run.ps1'
    if (-not (Test-Path -LiteralPath $runScript)) {
        Write-WastedWarn "$runScript does not exist yet (repo not cloned?) — registering the task anyway; it becomes functional once the repo is at $RepoDir."
        $script:warningsCount++
    }
    $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if (-not $pwshCmd) {
        Write-WastedWarn 'pwsh not found — cannot register the logon task; re-run after the toolchain step.'
        $script:warningsCount++
        return
    }
    if ($PSCmdlet.ShouldProcess('WASTED-Run', "register logon task for $AutologonUser")) {
        $action = New-ScheduledTaskAction -Execute $pwshCmd.Source `
            -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $runScript)
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $AutologonUser
        # Interactive logon type = runs in the user's console session (required: game + capture
        # + OBS all need the interactive desktop, never session 0).
        $principal = New-ScheduledTaskPrincipal -UserId $AutologonUser -LogonType Interactive -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName 'WASTED-Run' -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Write-WastedInfo "WASTED-Run registered: at logon of $AutologonUser -> pwsh -File $runScript"
    }
}

function Test-NvidiaDriver {
    [CmdletBinding()]
    param()
    Write-WastedStep 'Step 10/10: NVIDIA driver (documented manual step) + nvidia-smi verification.'
    # The driver install is deliberately manual: Hetzner GEX44 carries an RTX 4000 SFF Ada —
    # install the current NVIDIA RTX/Quadro *workstation* driver from nvidia.com (cloud-VM
    # route: the vendor GRID driver). Reboot after install. The HDMI emulator dongle must sit
    # on the RTX card's output, not the motherboard (RESEARCH.md D9).
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) {
        $candidate = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
        if (Test-Path -LiteralPath $candidate) { $smi = @{ Source = $candidate } }
    }
    if ($smi) {
        $gpu = & $smi.Source --query-gpu=name,driver_version --format=csv,noheader
        if ($LASTEXITCODE -eq 0) {
            Write-WastedInfo "NVIDIA driver OK: $gpu"
        }
        else {
            Write-WastedWarn "nvidia-smi exited $LASTEXITCODE — driver present but not healthy."
            $script:warningsCount++
        }
    }
    else {
        Write-WastedWarn 'nvidia-smi not found — NVIDIA driver not installed yet.'
        $script:manualActions.Add('NVIDIA: download + install the RTX 4000 SFF Ada workstation driver from nvidia.com (GRID driver on a cloud VM), reboot, then re-run server-setup.ps1 to verify via nvidia-smi.')
        $script:warningsCount++
    }
}

# --- main -------------------------------------------------------------------------------------

try {
    Write-WastedInfo "server-setup.ps1 starting. Root=$WastedRoot Repo=$RepoDir AdminIP=$($adminAddresses -join ', ') User=$AutologonUser"

    Install-Toolchain
    Enable-AudioServices
    Install-VbCable
    Install-Autologon
    Set-LockdownPolicies
    Set-PowerPlan
    Enable-OpenSsh
    Set-AdminFirewall
    Register-RunTask
    Test-NvidiaDriver

    $script:manualActions.Add('Game (one-time, human): log into Steam, install GTA V Legacy via steam://install/271590, first launch installs Rockstar Launcher + Social Club, sign into Rockstar, set -nobattleye (fetch-shvdn.ps1 deploys args.txt which also carries it), then pre-seed Documents\Rockstar Games\GTA V\settings.xml to 1280x720 windowed-borderless AFTER the first auto-detect run.')
    $script:manualActions.Add('OBS (one-time): enable the WebSocket server (localhost, password into the harness .env), set replay buffer 30 s + raised memory cap, audio source = CABLE Output.')

    Write-WastedStep 'Setup finished.'
    if ($warningsCount -gt 0) { Write-WastedWarn "$warningsCount warning(s) above need attention." }
    if ($manualActions.Count -gt 0) {
        Write-WastedStep 'Manual actions still required:'
        $i = 0
        foreach ($item in $manualActions) { $i++; Write-WastedInfo ("  {0}. {1}" -f $i, $item) }
    }
    Write-WastedInfo 'A reboot is recommended after first-time setup (VB-CABLE + NVIDIA driver need it).'
    exit 0
}
catch {
    Write-WastedError "server-setup.ps1 failed: $($_.Exception.Message)"
    Write-WastedError $_.ScriptStackTrace
    exit 1
}
finally {
    Stop-WastedTranscript
}
