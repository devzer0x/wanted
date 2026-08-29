#Requires -Version 7.0
<#
.SYNOPSIS
    One-shot, idempotent bring-up of the WASTED Windows Server 2025 game server (PLAN Phase 0a).
.DESCRIPTION
    Targets the machine actually delivered: a Hetzner auction dedicated server, Intel Core
    i5-12500 with **Intel UHD Graphics 770 integrated graphics and no discrete GPU**, 64 GB RAM,
    2x512 GB NVMe, Windows Server 2025 Standard, **no monitor and no HDMI emulator**.

    Two things follow from that hardware and drive most of this script:

      1. Intel's consumer graphics installer refuses Windows Server SKUs, but the DCH graphics
         INF itself is not product-type gated (its TargetOSVersion leaves ProductType and
         SuiteMask blank, so it applies to NT 10.0 amd64 build >= 16225; Server 2025 is 26100).
         So the driver is installed by INF with pnputil, never by Intel's setup.exe, and no
         test-signing is needed - which matters, because a display driver is kernel-mode and a
         permanently test-signed 24/7 box is not acceptable.
      2. With no monitor and no dummy plug the iGPU has zero connected outputs, so the console
         session has no display target: the desktop collapses to a fallback mode and a windowed
         game has nowhere to present. An Indirect Display Driver (IddCx) supplies a virtual
         display target; the game still renders on the Intel adapter.

    Also installs the Windows Server specific prerequisites the game needs (Media Foundation,
    VC++ x64+x86, the June 2010 DirectX side-by-side runtime, audio services), then the
    unchanged hardening: VB-CABLE staging, Sysinternals Autologon, lock-screen/idle/screensaver
    and notify-only Windows Update policy, high-performance power plan, OpenSSH with PowerShell 7
    as the default shell, an RDP/SSH firewall allowlist, the console-session logon task for
    run.ps1, and the OBS profile run.ps1 selects with --profile at launch.
    Safe to re-run; every step checks current state first.

    Bootstrap on a fresh server (this script needs PowerShell 7):
        winget install --exact --id Microsoft.PowerShell --silent --accept-package-agreements --accept-source-agreements

    Run preflight.ps1 first. It is Windows PowerShell 5.1 compatible and tells you whether this
    machine can host the show at all.
.PARAMETER AdminIP
    IP address or CIDR (comma-separate multiple) allowed to reach RDP (3389) and SSH (22).
    Get the wrong value and you lock yourself out - double-check it.
.PARAMETER WastedRoot
    Root of the WASTED server layout. Default C:\wasted.
.PARAMETER RepoDir
    Where this repo is (or will be) cloned on the server. The logon task points at
    <RepoDir>\scripts\run.ps1. Default C:\wasted\repo.
.PARAMETER AutologonUser
    Account for Sysinternals Autologon and the logon task. Default: current user.
.PARAMETER IntelDriverPackage
    Path to a staged Intel graphics package: either the downloaded .exe (extracted with 7-Zip,
    never executed) or a directory already containing the INF. When omitted the script looks in
    <WastedRoot>\downloads for gfx_win_*.exe / intel-gfx\. When nothing is staged the driver
    install becomes a documented manual action - this script never downloads from Intel, because
    the package URL is version specific and gated.
.PARAMETER VddTag
    Release tag of VirtualDrivers/Virtual-Display-Driver to install. The asset is resolved
    through the GitHub API, so a tag bump does not need a code change - but asset names differ
    wildly between that project's releases, so a tag bump usually does need -VddAssetPattern.
.PARAMETER VddAssetPattern
    Which asset of that release to install. Must match exactly one asset or the script aborts.
    Default 'VirtualDisplayDriver-x86.Driver.Only.zip' - the display driver's x64 build.
    Two traps this default exists to avoid, both verified against the real release 25.7.23:
      * The tag also ships VirtualAudioDriver-x86.Driver.Only.zip. That companion AUDIO driver
        must NEVER be installed here: it is rejected on Server 2025 (device lands on Code 52),
        and VB-CABLE is this project's virtual audio device. A loose pattern such as
        '*Driver*Only*.zip' matches it first and installs the wrong driver.
      * 'x86' in that project's asset names means Intel-arch, i.e. x64 - the archive's
        MttVDD.inf declares [Standard.NTamd64]. The Arm build is named ARM64 separately.
.PARAMETER VddSettingsDir
    Where the virtual display driver reads vdd_settings.xml. Default C:\VirtualDisplayDriver
    (fixed by the driver, not by us).
.PARAMETER ObsProfileName
    Name of the OBS profile installed from scripts\obs-profile\basic.ini, and the name run.ps1
    passes to OBS as --profile / --collection. Default WASTED. Change it in both places or not
    at all: OBS resolves --profile by the profile's [General] Name, and silently falls back to
    whatever profile was last used when the name does not exist.
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
    [string]$IntelDriverPackage = '',
    [string]$VddTag = '25.7.23',
    [string]$VddAssetPattern = 'VirtualDisplayDriver-x86.Driver.Only.zip',
    [string]$VddSettingsDir = 'C:\VirtualDisplayDriver',
    [string]$ObsProfileName = 'WASTED',
    [string]$DirectXRedistUrl = 'https://download.microsoft.com/download/8/4/A/84A35BF1-DAFE-4AE8-82AF-AD2AE20B6B14/directx_Jun2010_redist.exe',
    [switch]$InstallDirectPlay,
    [switch]$SkipFeatures,
    [switch]$SkipRuntimes,
    [switch]$SkipGraphicsDriver,
    [switch]$SkipVirtualDisplay,
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
$script:warningsCount = 0
$script:rebootRequired = $false
$totalSteps = 16

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
    Invoke-WebRequest -Uri $Url -OutFile $target -UserAgent 'wasted-ops/1.0' -TimeoutSec 300
    return $target
}

function Get-WastedGitHubAsset {
    <#
    Resolve EXACTLY ONE release asset through the GitHub API and download it into
    <root>\downloads. Returns the local path.

    The pattern must be unambiguous. A pattern that matches several assets is a bug in the
    caller, not something to resolve by taking whichever one the API happened to list first:
    VirtualDrivers/Virtual-Display-Driver 25.7.23 ships VirtualAudioDriver-x86.Driver.Only.zip,
    VirtualDisplayDriver-ARM64.Driver.Only.zip and VirtualDisplayDriver-x86.Driver.Only.zip, and
    'Select-Object -First 1' over '*Driver*Only*.zip' picks the AUDIO driver. So: zero matches
    throws, more than one match throws, and both messages list every asset in the release.
    #>
    param(
        [Parameter(Mandatory)][string]$Repo,
        [Parameter(Mandatory)][string]$Tag,
        [Parameter(Mandatory)][string]$AssetPattern
    )
    $api = "https://api.github.com/repos/$Repo/releases/tags/$Tag"
    if ($Tag -eq 'latest') { $api = "https://api.github.com/repos/$Repo/releases/latest" }
    $release = Invoke-RestMethod -Uri $api -TimeoutSec 60 -Headers @{
        'User-Agent' = 'wasted-ops/1.0'; Accept = 'application/vnd.github+json'
    }
    $allNames = @(@($release.assets) | ForEach-Object { [string]$_.name })
    $matched = @(@($release.assets) | Where-Object { $_.name -like $AssetPattern })
    if ($matched.Count -eq 0) {
        throw "Release $Repo@$($release.tag_name) has no asset matching '$AssetPattern'. Available: $($allNames -join ', ')"
    }
    if ($matched.Count -gt 1) {
        $matchedNames = (@($matched | ForEach-Object { [string]$_.name })) -join ', '
        throw ("Release $Repo@$($release.tag_name): pattern '$AssetPattern' is ambiguous — it matches " +
            "$($matched.Count) assets ($matchedNames). Refusing to guess which one is wanted; narrow the " +
            "pattern so it matches exactly one. All assets in this release: $($allNames -join ', ')")
    }
    $asset = $matched[0]
    Write-WastedInfo "$Repo@$($release.tag_name): selected asset $($asset.name) (sole match for '$AssetPattern')."
    return (Get-WastedDownload -Url $asset.browser_download_url -FileName $asset.name)
}

function Get-VideoControllerSummary {
    # One-line-per-adapter view used by several steps and by the final verification.
    $out = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($vc in @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction SilentlyContinue)) {
        $out.Add([pscustomobject]@{
                Name          = [string]$vc.Name
                PnpId         = [string]$vc.PNPDeviceID
                DriverVersion = [string]$vc.DriverVersion
                ErrorCode     = [int]($vc.ConfigManagerErrorCode ?? 0)
                Width         = [int]($vc.CurrentHorizontalResolution ?? 0)
                Height        = [int]($vc.CurrentVerticalResolution ?? 0)
                IsIntel       = ([string]$vc.PNPDeviceID) -like 'PCI\VEN_8086*'
                IsBasic       = (([string]$vc.Name) -like '*Basic Display*' -or ([string]$vc.Name) -like '*Basic Render*')
                IsVirtual     = (([string]$vc.PNPDeviceID) -like 'ROOT\MTTVDD*' -or ([string]$vc.PNPDeviceID) -like 'ROOT\IDDSAMPLEDRIVER*' -or
                    ([string]$vc.PNPDeviceID) -like 'ROOT\USBMMIDD*' -or ([string]$vc.Name) -like '*Virtual Display*' -or
                    ([string]$vc.Name) -like '*Parsec Virtual*' -or ([string]$vc.Name) -like '*usbmmidd*')
                IsDiscrete    = (([string]$vc.PNPDeviceID) -like 'PCI\VEN_10DE*' -or ([string]$vc.PNPDeviceID) -like 'PCI\VEN_1002*')
            })
    }
    return $out
}

function Invoke-Dism {
    # DISM instead of the ServerManager cmdlets: dism.exe is a native binary that behaves
    # identically under PowerShell 7, where Install-WindowsFeature only works through the
    # Windows PowerShell compatibility layer.
    param([Parameter(Mandatory)][string[]]$DismArgs)
    $dism = Join-Path $env:SystemRoot 'System32\dism.exe'
    $output = & $dism @DismArgs 2>&1
    return @{ ExitCode = $LASTEXITCODE; Output = ($output | Out-String) }
}

function Get-WindowsFeatureState {
    # Returns 'Enabled' | 'Disabled' | 'Unknown', plus the feature name DISM accepted.
    param([Parameter(Mandatory)][string[]]$CandidateNames)
    foreach ($name in $CandidateNames) {
        $res = Invoke-Dism -DismArgs @('/online', '/english', '/get-featureinfo', "/featurename:$name")
        if ($res.Output -match 'State\s*:\s*(\w+)') {
            return @{ State = $Matches[1]; Name = $name }
        }
    }
    return @{ State = 'Unknown'; Name = $CandidateNames[0] }
}

# --- steps ------------------------------------------------------------------------------------

function Install-Toolchain {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 1/$totalSteps`: toolchain via winget."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget not found. Windows Server 2025 ships it; if missing, install "App Installer" from Microsoft, then re-run.'
    }
    # IDs verified against microsoft/winget-pkgs manifests, 2026-08-25.
    $packages = @(
        @{ Id = 'Microsoft.PowerShell'; Why = 'PowerShell 7 (ops scripts, SSH default shell)' }
        @{ Id = 'Git.Git'; Why = 'repo checkout' }
        @{ Id = 'Python.Python.3.12'; Why = 'harness runtime (>=3.12)' }
        @{ Id = 'OpenJS.NodeJS.LTS'; Why = 'web tooling' }
        @{ Id = 'Microsoft.DotNet.SDK.8'; Why = 'bridge build (dotnet build)' }
        @{ Id = 'Microsoft.DotNet.Framework.DeveloperPack_4'; Why = '.NET Framework 4.8 targeting pack (bridge targets net48)' }
        @{ Id = 'OBSProject.OBSStudio'; Why = 'stream + replay buffer (obs-websocket v5 bundled since OBS 28)' }
        @{ Id = 'Valve.Steam'; Why = 'game platform (GTA V Legacy, app 271590)' }
        @{ Id = '7zip.7zip'; Why = 'archive tooling + extracting the Intel graphics package' }
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

function Install-ServerFeatures {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 2/$totalSteps`: Windows Server features (Desktop Experience check, Media Foundation)."
    if ($SkipFeatures) { Write-WastedInfo 'Skipped (-SkipFeatures).'; return }

    $installationType = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name InstallationType -ErrorAction SilentlyContinue).InstallationType
    if ($installationType -eq 'Server Core') {
        # Desktop Experience is chosen at setup and cannot be added afterwards on Server 2016+.
        throw 'This is Server Core. The game, OBS and Desktop Duplication all need the full desktop shell, and Desktop Experience cannot be added after installation. Ask Hetzner to reinstall Windows Server 2025 with the GUI image.'
    }
    Write-WastedInfo "InstallationType=$installationType (Desktop Experience present)."

    # Media Foundation is NOT installed by default on server OSes. GTA V's installer/launcher
    # check the Windows media feature set - the same dependency that makes Windows N editions
    # fail with the "Media Feature Pack" error family.
    $mf = Get-WindowsFeatureState -CandidateNames @('ServerMediaFoundation', 'Server-Media-Foundation')
    Write-WastedInfo "Server-Media-Foundation state=$($mf.State) (DISM feature name '$($mf.Name)')."
    if ($mf.State -eq 'Enabled') {
        Write-WastedInfo 'Media Foundation already enabled — skipping.'
    }
    elseif ($PSCmdlet.ShouldProcess($mf.Name, 'DISM /enable-feature')) {
        $res = Invoke-Dism -DismArgs @('/online', '/english', '/enable-feature', "/featurename:$($mf.Name)", '/all', '/norestart')
        Write-WastedInfo "DISM exit $($res.ExitCode)."
        if ($res.ExitCode -eq 3010) {
            Write-WastedInfo 'Media Foundation enabled; a reboot is required.'
            $script:rebootRequired = $true
        }
        elseif ($res.ExitCode -ne 0) {
            Write-WastedWarn "Enabling Media Foundation failed (exit $($res.ExitCode)). Output: $($res.Output)"
            $script:warningsCount++
            $script:manualActions.Add('Media Foundation: Install-WindowsFeature Server-Media-Foundation -Restart (from Windows PowerShell). On 0x800f081f add -Source wim:D:\sources\install.wim:2 or allow internet sourcing in the WSUS policy. Without it the Rockstar installer/launcher fails with the "Windows Media Player / Media Feature Pack" error family.')
        }
        else {
            Write-WastedInfo 'Media Foundation enabled.'
            $script:rebootRequired = $true
        }
    }

    if ($InstallDirectPlay) {
        # Legacy DirectX networking. GTA V does not need it; only some old mod tooling does.
        $dp = Get-WindowsFeatureState -CandidateNames @('DirectPlay')
        if ($dp.State -eq 'Enabled') {
            Write-WastedInfo 'DirectPlay already enabled — skipping.'
        }
        elseif ($PSCmdlet.ShouldProcess('DirectPlay', 'DISM /enable-feature')) {
            $res = Invoke-Dism -DismArgs @('/online', '/english', '/enable-feature', '/featurename:DirectPlay', '/all', '/norestart')
            if ($res.ExitCode -eq 3010) { $script:rebootRequired = $true }
            elseif ($res.ExitCode -ne 0) {
                Write-WastedWarn "Enabling DirectPlay failed (exit $($res.ExitCode))."
                $script:warningsCount++
            }
        }
    }
    else {
        Write-WastedInfo 'DirectPlay not requested (-InstallDirectPlay) — GTA V does not need it.'
    }
}

function Install-GameRuntimes {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 3/$totalSteps`: VC++ redistributables (x64 + x86) and the June 2010 DirectX runtime."
    if ($SkipRuntimes) { Write-WastedInfo 'Skipped (-SkipRuntimes).'; return }

    # .NET Framework 4.8.1 ships preinstalled on Server 2025; SHVDN v3 needs 4.8. Verify only.
    $ndp = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -Name Release -ErrorAction SilentlyContinue).Release
    if ($null -ne $ndp -and [int]$ndp -ge 528040) {
        Write-WastedInfo ".NET Framework 4.8+ present (Release=$ndp)."
    }
    else {
        Write-WastedWarn ".NET Framework Release=$ndp — expected >= 528040 (4.8). ScriptHookVDotNet v3 will not load."
        $script:warningsCount++
        $script:manualActions.Add('.NET Framework 4.8: unexpected on Server 2025 (4.8.1 is preinstalled). Install it from microsoft.com before deploying the bridge.')
    }

    # Both architectures: Social Club and launcher components are 32-bit.
    $redists = @(
        @{ Name = 'vc_redist.x64.exe'; Url = 'https://aka.ms/vc14/vc_redist.x64.exe'; Key = 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' }
        @{ Name = 'vc_redist.x86.exe'; Url = 'https://aka.ms/vc14/vc_redist.x86.exe'; Key = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86' }
    )
    foreach ($redist in $redists) {
        $installed = (Get-ItemProperty -LiteralPath $redist.Key -Name Installed -ErrorAction SilentlyContinue).Installed
        if ($null -ne $installed -and [int]$installed -eq 1) {
            $ver = (Get-ItemProperty -LiteralPath $redist.Key -Name Version -ErrorAction SilentlyContinue).Version
            Write-WastedInfo "$($redist.Name) already installed (version $ver) — skipping."
            continue
        }
        if ($PSCmdlet.ShouldProcess($redist.Name, 'download + install /quiet /norestart')) {
            try {
                $exe = Get-WastedDownload -Url $redist.Url -FileName $redist.Name
                $proc = Start-Process -FilePath $exe -ArgumentList '/install', '/quiet', '/norestart' -Wait -PassThru
                if ($proc.ExitCode -eq 3010) {
                    Write-WastedInfo "$($redist.Name) installed; reboot required."
                    $script:rebootRequired = $true
                }
                elseif ($proc.ExitCode -ne 0) {
                    Write-WastedWarn "$($redist.Name) exited $($proc.ExitCode)."
                    $script:warningsCount++
                }
                else {
                    Write-WastedInfo "$($redist.Name) installed."
                }
            }
            catch {
                Write-WastedWarn "$($redist.Name) failed: $($_.Exception.Message)"
                $script:warningsCount++
                $script:manualActions.Add("VC++ runtime: download $($redist.Url) manually and run it with /install /quiet /norestart.")
            }
        }
    }

    # The June 2010 DirectX End-User Runtime supplies the side-by-side legacy DLLs modern Windows
    # omits (D3DX9/10/11, d3dcompiler_43, XAudio 2.7, xinput1_3). Script Hook V and many ASI mods
    # link them. It does not touch the in-box DirectX.
    $legacyMarkers = @(
        (Join-Path $env:SystemRoot 'System32\d3dcompiler_43.dll'),
        (Join-Path $env:SystemRoot 'SysWOW64\d3dx9_43.dll'),
        (Join-Path $env:SystemRoot 'SysWOW64\xinput1_3.dll')
    )
    $missing = @($legacyMarkers | Where-Object { -not (Test-Path -LiteralPath $_) })
    if ($missing.Count -eq 0) {
        Write-WastedInfo 'June 2010 DirectX side-by-side runtime already present — skipping.'
        return
    }
    Write-WastedInfo ("Legacy DirectX DLLs missing: " + (($missing | ForEach-Object { Split-Path -Leaf $_ }) -join ', '))
    if (-not $PSCmdlet.ShouldProcess('DirectX End-User Runtime (June 2010)', 'download, extract, DXSETUP /silent')) { return }
    try {
        $redistExe = Join-Path (Join-Path $script:WastedRoot 'downloads') 'directx_Jun2010_redist.exe'
        if (-not (Test-Path -LiteralPath $redistExe)) {
            $redistExe = Get-WastedDownload -Url $DirectXRedistUrl -FileName 'directx_Jun2010_redist.exe'
        }
        $extractDir = Join-Path (Join-Path $script:WastedRoot 'tools') 'directx'
        New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
        # The redist is a self-extractor: /Q silent, /T:<dir> target, /C extract only.
        $proc = Start-Process -FilePath $redistExe -ArgumentList '/Q', "/T:$extractDir", '/C' -Wait -PassThru
        if ($proc.ExitCode -ne 0) { throw "self-extractor exited $($proc.ExitCode)" }
        $dxsetup = Join-Path $extractDir 'DXSETUP.exe'
        if (-not (Test-Path -LiteralPath $dxsetup)) { throw "DXSETUP.exe not found under $extractDir after extraction" }
        $proc = Start-Process -FilePath $dxsetup -ArgumentList '/silent' -Wait -PassThru
        if ($proc.ExitCode -ne 0) { throw "DXSETUP.exe exited $($proc.ExitCode)" }
        Write-WastedInfo 'June 2010 DirectX runtime installed.'
    }
    catch {
        Write-WastedWarn "DirectX End-User Runtime install failed: $($_.Exception.Message)"
        $script:warningsCount++
        $script:manualActions.Add("DirectX (June 2010): download directx_Jun2010_redist.exe from the Microsoft Download Center (id 8109) into $($script:WastedRoot)\downloads, then re-run this script. It supplies d3dx9_43/d3dcompiler_43/xinput1_3, which Script Hook V and many ASI mods link.")
    }
}

function Enable-AudioServices {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 4/$totalSteps`: Windows Audio services (required BEFORE VB-CABLE installs)."
    # Order matters: Audiosrv depends on AudioEndpointBuilder. MMCSS is the multimedia scheduler
    # that keeps audio glitch-free under game load. All three are off by default on Server.
    foreach ($name in @('AudioEndpointBuilder', 'Audiosrv', 'MMCSS')) {
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
    Write-WastedStep "Step 5/$totalSteps`: VB-CABLE virtual audio device."
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
            $script:rebootRequired = $true
        }
    }
    else {
        Write-WastedWarn 'Non-interactive session or setup exe missing — VB-CABLE install left as a manual action (see summary).'
        $script:warningsCount++
    }
}

function Install-IntelGraphicsDriver {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 6/$totalSteps`: Intel UHD 770 graphics driver (INF install; Intel's setup.exe blocks Server SKUs)."
    if ($SkipGraphicsDriver) { Write-WastedInfo 'Skipped (-SkipGraphicsDriver).'; return }

    $adapters = Get-VideoControllerSummary
    $intel = @($adapters | Where-Object { $_.IsIntel })
    $discrete = @($adapters | Where-Object { $_.IsDiscrete })

    # Cheap discrete-GPU branch: this auction server has none, but if one is ever fitted we
    # should say so rather than silently assume the iGPU is the render device.
    if ($discrete.Count -gt 0) {
        Write-WastedInfo ("Discrete GPU present: " + (($discrete | ForEach-Object { $_.Name }) -join ', ') + '. Install its vendor driver too; the iGPU work below still applies for the display target.')
        $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
        if ($smi) {
            $gpu = & $smi.Source --query-gpu=name,driver_version --format=csv,noheader
            if ($LASTEXITCODE -eq 0) { Write-WastedInfo "nvidia-smi: $gpu" }
        }
    }
    else {
        Write-WastedInfo 'No discrete GPU (expected: Intel UHD 770 is the only render device).'
    }

    if ($intel.Count -eq 0) {
        Write-WastedWarn 'No PCI\VEN_8086 display adapter enumerated — the iGPU is not visible to Windows.'
        $script:warningsCount++
        $script:manualActions.Add('Intel iGPU missing: request the free 3h Hetzner KVM console (or use the self-service vKVM rescue mode) and confirm the board posts video on the iGPU and that it is enabled in BIOS. Nothing else works until it enumerates.')
        return
    }

    $current = $intel[0]
    Write-WastedInfo "Intel adapter: $($current.Name) driver=$($current.DriverVersion) cmError=$($current.ErrorCode) pnp=$($current.PnpId)"
    if (-not $current.IsBasic -and $current.ErrorCode -eq 0 -and $current.DriverVersion -notmatch '^10\.0\.') {
        Write-WastedInfo 'A vendor graphics driver is already bound to the Intel adapter — skipping install.'
        return
    }

    # Locate a staged package. We never download from Intel: the package URL is version specific
    # and gated, and the installer must not be executed (it enforces a client-OS check).
    $downloads = Join-Path $script:WastedRoot 'downloads'
    $extractRoot = Join-Path (Join-Path $script:WastedRoot 'tools') 'intel-gfx'
    $infPath = ''
    $searchRoots = [System.Collections.Generic.List[string]]::new()
    if ($IntelDriverPackage) { $searchRoots.Add($IntelDriverPackage) }
    $searchRoots.Add((Join-Path $downloads 'intel-gfx'))
    $searchRoots.Add($extractRoot)

    foreach ($root in $searchRoots) {
        if (-not $root) { continue }
        if (Test-Path -LiteralPath $root -PathType Container) {
            $found = Get-ChildItem -LiteralPath $root -Recurse -Filter '*.inf' -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like 'iigd*' -or $_.Name -like '*gfx*' } | Select-Object -First 1
            if (-not $found) {
                $found = Get-ChildItem -LiteralPath $root -Recurse -Filter '*.inf' -ErrorAction SilentlyContinue | Select-Object -First 1
            }
            if ($found) { $infPath = $found.FullName; break }
        }
    }

    if (-not $infPath) {
        $packageExe = ''
        if ($IntelDriverPackage -and (Test-Path -LiteralPath $IntelDriverPackage -PathType Leaf)) {
            $packageExe = $IntelDriverPackage
        }
        else {
            $candidate = Get-ChildItem -LiteralPath $downloads -Filter 'gfx_win_*.exe' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($candidate) { $packageExe = $candidate.FullName }
        }
        if ($packageExe) {
            $sevenZip = Get-Command 7z -ErrorAction SilentlyContinue
            if (-not $sevenZip) {
                $fallback = Join-Path $env:ProgramFiles '7-Zip\7z.exe'
                if (Test-Path -LiteralPath $fallback) { $sevenZip = @{ Source = $fallback } }
            }
            if (-not $sevenZip) {
                Write-WastedWarn '7-Zip not available — cannot extract the Intel package without running its installer.'
                $script:warningsCount++
            }
            elseif ($PSCmdlet.ShouldProcess($packageExe, "extract with 7-Zip to $extractRoot")) {
                # Never extract into a Windows system folder: Intel KB 000088280 - the DCH driver
                # refuses to install from one.
                New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
                & $sevenZip.Source x $packageExe "-o$extractRoot" -y | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    Write-WastedWarn "7z exited $LASTEXITCODE extracting $packageExe."
                    $script:warningsCount++
                }
                else {
                    $found = Get-ChildItem -LiteralPath $extractRoot -Recurse -Filter '*.inf' -ErrorAction SilentlyContinue |
                        Where-Object { $_.Name -like 'iigd*' } | Select-Object -First 1
                    if (-not $found) {
                        $found = Get-ChildItem -LiteralPath $extractRoot -Recurse -Filter '*.inf' -ErrorAction SilentlyContinue | Select-Object -First 1
                    }
                    if ($found) { $infPath = $found.FullName }
                }
            }
        }
    }

    if (-not $infPath) {
        Write-WastedWarn 'No Intel graphics INF staged — driver install left as a manual action.'
        $script:warningsCount++
        $script:manualActions.Add(@(
                'Intel graphics driver (REQUIRED — without it there is no Direct3D 11 and GTA V will not start):'
                "  1. On any machine, download the Intel 11th-14th Gen Processor Graphics package (Intel download #864990; recon recorded 32.0.101.7088, 2026-06-22) from https://www.intel.com/content/www/us/en/download/864990/ and copy the .exe into $downloads."
                '  2. Re-run this script. It extracts the package with 7-Zip (never runs Intel setup.exe, which blocks Server SKUs) and installs the INF with pnputil.'
                "  3. Manual equivalent: 7z x <pkg>.exe -o$extractRoot ; pnputil /add-driver `"$extractRoot\*.inf`" /subdirs /install ; then verify with Get-CimInstance Win32_VideoController."
                '  4. If pnputil will not replace an already-bound driver, use Device Manager -> Update driver -> Browse -> Let me pick -> Have Disk, and point at the INF. No INF editing and no test-signing: the DCH INF already applies to Server (NT 10.0 amd64, build >= 16225).'
            ) -join [Environment]::NewLine)
        return
    }

    Write-WastedInfo "Intel graphics INF: $infPath"
    if ($PSCmdlet.ShouldProcess($infPath, 'pnputil /add-driver /install')) {
        $pnputil = Join-Path $env:SystemRoot 'System32\pnputil.exe'
        $out = & $pnputil /add-driver $infPath /install 2>&1 | Out-String
        Write-WastedInfo "pnputil exit $LASTEXITCODE`: $out"
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3010) {
            Write-WastedWarn "pnputil /add-driver exited $LASTEXITCODE."
            $script:warningsCount++
            $script:manualActions.Add("Intel graphics driver: pnputil refused '$infPath' (exit $LASTEXITCODE). pnputil will not force a lower-ranked driver onto a bound device — use Device Manager -> Update driver -> Browse my computer -> Let me pick -> Have Disk and point at that INF.")
        }
        if ($LASTEXITCODE -eq 3010) { $script:rebootRequired = $true }
        $after = @(Get-VideoControllerSummary | Where-Object { $_.IsIntel })
        if ($after.Count -gt 0) {
            Write-WastedInfo "Intel adapter after install: $($after[0].Name) driver=$($after[0].DriverVersion) cmError=$($after[0].ErrorCode)"
        }
    }
}

function Install-VirtualDisplay {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 7/$totalSteps`: virtual display driver (no monitor, no HDMI emulator on this server)."
    if ($SkipVirtualDisplay) { Write-WastedInfo 'Skipped (-SkipVirtualDisplay).'; return }

    $existing = @(Get-VideoControllerSummary | Where-Object { $_.IsVirtual })
    if ($existing.Count -gt 0 -and -not $Force) {
        Write-WastedInfo "Virtual display already present: $($existing[0].Name) — skipping (re-run with -Force to reinstall)."
        Write-VddSettings
        return
    }

    # An Indirect Display Driver creates a virtual adapter + monitor and receives finished frames
    # as a DirectX surface. The game still renders on the Intel adapter; it simply has a desktop
    # to present into. This is the configuration headless streaming hosts run at scale.
    try {
        # -VddAssetPattern must resolve to the DISPLAY driver's x64 build. Get-WastedGitHubAsset
        # already aborts on an ambiguous pattern; the assertions after this block are the second
        # lock, so that even a hand-supplied -VddAssetPattern cannot stage the companion AUDIO
        # driver (rejected on Server 2025 — Code 52; VB-CABLE is this project's audio device).
        $vddZip = Get-WastedGitHubAsset -Repo 'VirtualDrivers/Virtual-Display-Driver' -Tag $VddTag -AssetPattern $VddAssetPattern
        $nefconZip = Get-WastedGitHubAsset -Repo 'nefarius/nefcon' -Tag 'v1.14.0' -AssetPattern 'nefcon_v*.zip'
    }
    catch {
        Write-WastedWarn "Could not fetch the virtual display driver: $($_.Exception.Message)"
        $script:warningsCount++
        $script:manualActions.Add('Virtual display driver: download VirtualDisplayDriver-x86.Driver.Only.zip (the DISPLAY driver; "x86" is that project''s name for the Intel-arch/x64 build, and its MttVDD.inf is [Standard.NTamd64]) from https://github.com/VirtualDrivers/Virtual-Display-Driver/releases and nefcon from https://github.com/nefarius/nefcon/releases into ' + (Join-Path $script:WastedRoot 'downloads') + ', or run the project''s own Community Scripts/silent-install.ps1. Do NOT take VirtualAudioDriver-*.zip from the same release: that companion audio driver is rejected on Server 2025 (Code 52) and VB-CABLE is the audio device here. Without a display target the console session has no desktop for the game to present into.')
        return
    }

    # Wrong-package assertions are fatal, not warnings: unlike a failed download they mean the
    # operator asked for the wrong thing, and continuing would install a driver that must not
    # be installed on this box.
    $vddZipName = Split-Path -Leaf $vddZip
    if ($vddZipName -match 'Audio') {
        throw ("-VddAssetPattern '$VddAssetPattern' resolved to '$vddZipName', which is the VirtualAudioDriver " +
            'companion package, not the virtual DISPLAY driver. That audio driver is rejected on Windows ' +
            'Server 2025 (the device lands on Code 52) and must never be installed here — VB-CABLE is this ' +
            "server's virtual audio device. Point -VddAssetPattern at the display asset (25.7.23: " +
            "'VirtualDisplayDriver-x86.Driver.Only.zip', whose MttVDD.inf declares [Standard.NTamd64], i.e. x64).")
    }
    if ($vddZipName -match 'ARM64') {
        throw "-VddAssetPattern '$VddAssetPattern' resolved to '$vddZipName' — this server is an Intel i5-12500 (x64), not Arm."
    }

    $toolsDir = Join-Path (Join-Path $script:WastedRoot 'tools') 'vdd'
    $nefconDir = Join-Path (Join-Path $script:WastedRoot 'tools') 'nefcon'
    if ($PSCmdlet.ShouldProcess($toolsDir, 'extract virtual display driver + nefcon')) {
        foreach ($pair in @(@{ Zip = $vddZip; Dest = $toolsDir }, @{ Zip = $nefconZip; Dest = $nefconDir })) {
            if (Test-Path -LiteralPath $pair.Dest) { Remove-Item -LiteralPath $pair.Dest -Recurse -Force }
            Expand-Archive -LiteralPath $pair.Zip -DestinationPath $pair.Dest -Force
        }
    }

    $inf = Get-ChildItem -LiteralPath $toolsDir -Recurse -Filter 'MttVDD.inf' -ErrorAction SilentlyContinue | Select-Object -First 1
    $cat = Get-ChildItem -LiteralPath $toolsDir -Recurse -Filter 'MttVDD.cat' -ErrorAction SilentlyContinue | Select-Object -First 1
    # nefcon_v1.14.0.zip lays out ARM64\, x64\ and x86\ side by side. Match on the containing
    # directory NAME, not anywhere in the full path: a WastedRoot that happened to contain "x64"
    # would otherwise make all three "match" and hand -First 1 the ARM64 build.
    $nefconw = Get-ChildItem -LiteralPath $nefconDir -Recurse -Filter 'nefconw.exe' -ErrorAction SilentlyContinue |
        Where-Object { $_.Directory.Name -in @('x64', 'amd64') } | Select-Object -First 1
    if (-not $nefconw) {
        $nefconw = Get-ChildItem -LiteralPath $nefconDir -Recurse -Filter 'nefconw.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($nefconw) {
            Write-WastedWarn "No x64\nefconw.exe in $nefconDir; falling back to '$($nefconw.FullName)'. Check it is the x64 build — this is an Intel i5-12500."
            $script:warningsCount++
        }
    }
    if (-not $inf -or -not $nefconw) {
        Write-WastedWarn "Expected MttVDD.inf under $toolsDir and nefconw.exe under $nefconDir — archive layout changed."
        $script:warningsCount++
        $script:manualActions.Add("Virtual display driver: inspect $toolsDir and $nefconDir and install manually, or run the project's Community Scripts/silent-install.ps1.")
        return
    }

    # The catalog is signed by SignPath Foundation with a publicly chaining certificate; Windows
    # still requires the publisher in TrustedPublisher before it will install the package
    # unattended (otherwise the device lands on Code 52).
    if ($cat -and $PSCmdlet.ShouldProcess('LocalMachine\TrustedPublisher', "trust the publisher of $($cat.Name)")) {
        try {
            $sig = Get-AuthenticodeSignature -LiteralPath $cat.FullName
            if ($null -eq $sig.SignerCertificate) { throw 'catalog carries no signer certificate' }
            Write-WastedInfo "Driver catalog signer: $($sig.SignerCertificate.Subject) thumbprint=$($sig.SignerCertificate.Thumbprint) status=$($sig.Status)"
            $store = [System.Security.Cryptography.X509Certificates.X509Store]::new('TrustedPublisher', 'LocalMachine')
            $store.Open('ReadWrite')
            try { $store.Add($sig.SignerCertificate) } finally { $store.Close() }
            Write-WastedInfo 'Publisher certificate imported into LocalMachine\TrustedPublisher.'
        }
        catch {
            Write-WastedWarn "Could not trust the driver publisher: $($_.Exception.Message) — the install may fail with Code 52."
            $script:warningsCount++
        }
    }

    # Settings must exist before the device starts: on Server the driver falls back to the FIRST
    # entry in the resolution list after a restart, so 1280x720 has to be first.
    Write-VddSettings

    if ($PSCmdlet.ShouldProcess('Root\MttVDD', "install $($inf.FullName) via nefconw")) {
        # devcon-compatible syntax, exactly what the VDD project's own Community Scripts/
        # silent-install.ps1 runs: nefconw install <inf> <hardware-id>. nefcon's devcon-emulation
        # branch (src/NefConUtil.cpp) creates the ROOT device node, calls devcon::Update, and
        # returns ERROR_SUCCESS_REBOOT_REQUIRED (3010) when the install succeeded but needs a
        # restart — a SUCCESS code. Treating it as failure would both hide a good install and
        # drop the reboot flag, which is the one thing that actually has to happen next.
        $out = & $nefconw.FullName install $inf.FullName 'Root\MttVDD' 2>&1 | Out-String
        $nefconExit = $LASTEXITCODE
        Write-WastedInfo "nefconw exit $nefconExit`: $out"
        if ($nefconExit -eq 3010) {
            $script:rebootRequired = $true
            Write-WastedInfo 'Virtual display driver installed; nefconw returned ERROR_SUCCESS_REBOOT_REQUIRED (3010), so a reboot is required. Verify a new display adapter and a 1280x720 desktop mode after it.'
        }
        elseif ($nefconExit -eq 0) {
            $script:rebootRequired = $true
            Write-WastedInfo 'Virtual display driver installed. Verify a new display adapter and a 1280x720 desktop mode after the reboot.'
        }
        else {
            Write-WastedWarn "nefconw install exited $nefconExit (0 = installed, 3010 = installed + reboot required; anything else is a failure)."
            $script:warningsCount++
            $script:manualActions.Add("Virtual display driver: nefconw exited $nefconExit. Fall back to the project's own installer (Community Scripts/silent-install.ps1 in the Virtual-Display-Driver repo), or to Amyuni usbmmidd_v2 (deviceinstaller64 install usbmmidd.inf usbmmidd; deviceinstaller64 enableidd 1 — note it does NOT persist across reboot and needs a boot task).")
        }
    }
}

function Write-VddSettings {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    $settingsPath = Join-Path $VddSettingsDir 'vdd_settings.xml'
    # Element names and nesting mirror the vdd_settings.xml shipped inside
    # VirtualDisplayDriver-x86.Driver.Only.zip (25.7.23); only the values differ.
    #   * 1280x720 is FIRST in <resolutions>: on Server the driver reverts to the first entry
    #     after a restart, so anything else here becomes the desktop mode the game wakes up in.
    #   * <gpu><friendlyname> pins the render adapter to the Intel iGPU (the shipped default is
    #     "default", which lets it land on a software renderer). It must match the adapter's
    #     Win32_VideoController Name exactly.
    #   * <options> carries the driver's own shipped defaults; leaving the block out entirely is
    #     not something the shipped file ever does, so it is written rather than omitted.
    $xml = @'
<?xml version='1.0' encoding='utf-8'?>
<vdd_settings>
  <monitors>
    <count>1</count>
  </monitors>
  <gpu>
    <friendlyname>Intel(R) UHD Graphics 770</friendlyname>
  </gpu>
  <global>
    <g_refresh_rate>60</g_refresh_rate>
  </global>
  <resolutions>
    <resolution>
      <width>1280</width>
      <height>720</height>
      <refresh_rate>60</refresh_rate>
    </resolution>
    <resolution>
      <width>1920</width>
      <height>1080</height>
      <refresh_rate>60</refresh_rate>
    </resolution>
  </resolutions>
  <options>
    <CustomEdid>false</CustomEdid>
    <PreventSpoof>false</PreventSpoof>
    <EdidCeaOverride>false</EdidCeaOverride>
    <HardwareCursor>true</HardwareCursor>
    <SDR10bit>false</SDR10bit>
    <HDRPlus>false</HDRPlus>
    <logging>false</logging>
    <debuglogging>false</debuglogging>
  </options>
</vdd_settings>
'@
    if ($PSCmdlet.ShouldProcess($settingsPath, 'write vdd_settings.xml (1280x720 first)')) {
        New-Item -ItemType Directory -Path $VddSettingsDir -Force | Out-Null
        if (Test-Path -LiteralPath $settingsPath) {
            $backup = "$settingsPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            Copy-Item -LiteralPath $settingsPath -Destination $backup -Force
            Write-WastedInfo "Existing vdd_settings.xml backed up to $backup."
        }
        Set-Content -LiteralPath $settingsPath -Value $xml -Encoding utf8
        # Read back rather than assert from memory: the first <resolution> is the mode the
        # console session comes up in after every restart, so it is worth one parse to prove.
        try {
            $check = [xml](Get-Content -LiteralPath $settingsPath -Raw)
            $first = @($check.vdd_settings.resolutions.resolution)[0]
            $pinnedGpu = [string]$check.vdd_settings.gpu.friendlyname
            Write-WastedInfo ("Wrote {0}: first resolution {1}x{2}@{3}, GPU pinned to '{4}'." -f `
                    $settingsPath, $first.width, $first.height, $first.refresh_rate, $pinnedGpu)
            if ("$($first.width)x$($first.height)" -ne '1280x720') {
                Write-WastedWarn "vdd_settings.xml first resolution is $($first.width)x$($first.height), not 1280x720 — the desktop will come back at that mode after every restart."
                $script:warningsCount++
            }
        }
        catch {
            Write-WastedWarn "Wrote $settingsPath but could not parse it back: $($_.Exception.Message)"
            $script:warningsCount++
        }
    }
}

function Enable-RdsHardwareAdapter {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 8/$totalSteps`: let RDP sessions enumerate the hardware adapter."
    # By default every Remote Desktop session uses the Microsoft Basic Render Driver (software).
    # This makes RDP diagnostics honest. It does NOT make RDP a place to run the game: exclusive
    # fullscreen is impossible over Terminal Server by design, and the session dies on disconnect.
    Set-WastedRegistryValue -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services' `
        -Name bEnumerateHWBeforeSW -Value 1 -Type DWord -Why 'RDS sessions enumerate the hardware GPU before WARP'
}

function Install-Autologon {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 9/$totalSteps`: Sysinternals Autologon (password becomes an LSA secret; this script never stores it)."
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
    Write-WastedStep "Step 10/$totalSteps`: lock screen / idle / screensaver / Windows Update policies."
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
    Write-WastedStep "Step 11/$totalSteps`: high-performance power plan, monitor never sleeps."
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
    Write-WastedStep "Step 12/$totalSteps`: OpenSSH server + PowerShell 7 default shell."
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
    Write-WastedStep "Step 13/$totalSteps`: firewall — inbound RDP/SSH only from $($adminAddresses -join ', ')."
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
        @{ Name = 'WASTED-Admin-SSH'; Display = 'WASTED admin SSH (22)'; Port = 22 }
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
    Write-WastedStep "Step 14/$totalSteps`: scheduled task WASTED-Run (console session, at logon of the autologon user)."
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

function Install-ObsProfile {
    <#
    Install scripts\obs-profile\basic.ini as an OBS profile so run.ps1's `--profile` actually
    resolves to something. Verified against OBS Studio 32.2.2 source:
      * frontend/OBSApp.cpp    - profiles live under <app config>\obs-studio\basic\profiles
                                 (GetAppConfigPath => %APPDATA% on Windows).
      * frontend/widgets/OBSBasic_Profiles.cpp - one directory per profile, settings file name
                                 "basic.ini", and the profile's NAME is read from [General] Name
                                 (config_get_string(config, "General", "Name")).
      * frontend/widgets/OBSBasic.cpp InitBasicConfig - `--profile` is matched by that name via
                                 GetProfileByName, and OBS falls back SILENTLY to the last-used
                                 profile when the name is unknown. Hence installing it here.
    Per-user by construction: %APPDATA% belongs to whoever runs this script, and the stream runs
    as the autologon user, so a mismatch is called out rather than papered over.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Write-WastedStep "Step 15/$totalSteps`: OBS profile '$ObsProfileName' into %APPDATA%\obs-studio."
    $source = Join-Path (Join-Path $PSScriptRoot 'obs-profile') 'basic.ini'
    if (-not (Test-Path -LiteralPath $source)) {
        Write-WastedWarn "OBS profile source '$source' is missing — run this from a full checkout of scripts/."
        $script:warningsCount++
        return
    }
    # The file is only useful if its declared name is the one run.ps1 will ask OBS for.
    $declared = (Select-String -LiteralPath $source -Pattern '^\s*Name\s*=\s*(.+?)\s*$' |
            Select-Object -First 1)
    $declaredName = if ($declared) { $declared.Matches[0].Groups[1].Value } else { '' }
    if ($declaredName -ne $ObsProfileName) {
        Write-WastedWarn "obs-profile\basic.ini declares [General] Name='$declaredName' but -ObsProfileName is '$ObsProfileName'. OBS resolves --profile by that Name, so they must match."
        $script:warningsCount++
    }
    if (-not $env:APPDATA) {
        Write-WastedWarn 'APPDATA is not set — cannot locate the OBS config directory.'
        $script:warningsCount++
        $script:manualActions.Add("OBS profile: copy $source to %APPDATA%\obs-studio\basic\profiles\$ObsProfileName\basic.ini by hand.")
        return
    }
    if ($AutologonUser -and $env:USERNAME -and $AutologonUser -ne $env:USERNAME) {
        Write-WastedWarn "Installing the OBS profile into '$env:USERNAME's %APPDATA%, but the stream runs as '$AutologonUser'. Re-run this step (or copy the file) while logged in as $AutologonUser."
        $script:warningsCount++
    }
    $profileDir = Join-Path (Join-Path $env:APPDATA 'obs-studio\basic\profiles') $ObsProfileName
    $target = Join-Path $profileDir 'basic.ini'
    # The scene collection stays a human step: it is a serialization of each source plugin's own
    # settings object, and an invented one loads as an empty scene. run.ps1 passes
    # --collection "<name>", which OBS matches against the "name" field inside
    # %APPDATA%\obs-studio\basic\scenes\*.json, so the collection must carry exactly this name.
    # Queued before the idempotency check on purpose: a re-run of setup must still say it.
    $script:manualActions.Add("OBS scene collection: in OBS pick Profile -> $ObsProfileName (already installed by this script), then build the scene collection by hand and name it exactly '$ObsProfileName' — run.ps1 launches OBS with --profile `"$ObsProfileName`" --collection `"$ObsProfileName`" and OBS silently keeps the previous collection if that name does not exist. Source list, order and rationale: scripts\obs-profile\README.md.")
    if ((Test-Path -LiteralPath $target) -and
        (Get-WastedFileSha256 -Path $target) -eq (Get-WastedFileSha256 -Path $source)) {
        Write-WastedInfo "OBS profile already installed and identical: $target — skipping."
        return
    }
    if ($PSCmdlet.ShouldProcess($target, "install OBS profile from $source")) {
        New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
        if (Test-Path -LiteralPath $target) {
            $backup = "$target.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            Copy-Item -LiteralPath $target -Destination $backup -Force
            Write-WastedInfo "Existing profile backed up to $backup."
        }
        Copy-Item -LiteralPath $source -Destination $target -Force
        Write-WastedInfo "OBS profile '$ObsProfileName' installed at $target (sha256 $(Get-WastedFileSha256 -Path $target))."
    }
}

function Test-DisplayStack {
    [CmdletBinding()]
    param()
    Write-WastedStep "Step 16/$totalSteps`: display stack verification."
    $adapters = Get-VideoControllerSummary
    if ($adapters.Count -eq 0) {
        Write-WastedWarn 'No display adapter enumerated at all.'
        $script:warningsCount++
        return
    }
    foreach ($a in $adapters) {
        Write-WastedInfo ("adapter: {0} | driver {1} | {2} | mode {3}x{4} | cmError {5}" -f $a.Name, $a.DriverVersion, $a.PnpId, $a.Width, $a.Height, $a.ErrorCode)
    }
    $renderCapable = @($adapters | Where-Object { -not $_.IsBasic -and $_.ErrorCode -eq 0 })
    if ($renderCapable.Count -eq 0) {
        Write-WastedWarn 'Every adapter is on a Microsoft basic driver or reports a problem code — no Direct3D 11, so GTA V will not start.'
        $script:warningsCount++
    }
    $withMode = @($adapters | Where-Object { $_.Width -ge 1280 -and $_.Height -ge 720 })
    if ($withMode.Count -eq 0) {
        Write-WastedWarn 'No adapter reports an active desktop mode of at least 1280x720 — the console session has no usable display target yet.'
        $script:warningsCount++
        $script:manualActions.Add('Display target: after the reboot, RDP in and confirm a 1280x720 (or larger) desktop exists in the CONSOLE session. If not, check the virtual display driver device in Device Manager (Code 52 = trust the publisher certificate; Code 31 = driver did not load) and re-run this script with -Force.')
    }
    else {
        Write-WastedInfo ("Active desktop mode: {0}x{1} on {2}." -f $withMode[0].Width, $withMode[0].Height, $withMode[0].Name)
    }
}

# --- main -------------------------------------------------------------------------------------

try {
    Write-WastedInfo "server-setup.ps1 starting. Root=$WastedRoot Repo=$RepoDir AdminIP=$($adminAddresses -join ', ') User=$AutologonUser"
    Write-WastedInfo 'Target hardware: Intel i5-12500 / UHD 770 iGPU, no discrete GPU, no monitor, no HDMI emulator.'

    Install-Toolchain
    Install-ServerFeatures
    Install-GameRuntimes
    Enable-AudioServices
    Install-VbCable
    Install-IntelGraphicsDriver
    Install-VirtualDisplay
    Enable-RdsHardwareAdapter
    Install-Autologon
    Set-LockdownPolicies
    Set-PowerPlan
    Enable-OpenSsh
    Set-AdminFirewall
    Register-RunTask
    Install-ObsProfile
    Test-DisplayStack

    $script:manualActions.Add('Game (one-time, human): log into Steam, install GTA V Legacy via steam://install/271590 (NOT Enhanced/3240220 — Enhanced needs a 4 GB DirectX 12 GPU and ships BattlEye), first launch installs Rockstar Launcher + Social Club, sign into Rockstar, then set Documents\Rockstar Games\GTA V\settings.xml to ScreenWidth=1280, ScreenHeight=720, Windowed=2 (borderless) AFTER the first auto-detect run. Exclusive fullscreen must never be used: it is impossible over Terminal Server and fragile with a virtual display.')
    $script:manualActions.Add("OBS (one-time): the '$ObsProfileName' profile (720p30 x264, replay buffer 30 s / 1024 MB cap) was installed by step 15 — everything left is UI-only: enable the WebSocket server (127.0.0.1:4455, password into the harness .env), add the sources listed in scripts\obs-profile\README.md (Game Capture on GTA5.exe as primary — it hooks the game's D3D11 swapchain and does not depend on Desktop Duplication; Audio Output Capture on CABLE Output, never Default; browser source on http://127.0.0.1:7788/overlay), and type the stream key by hand.")

    Write-WastedStep 'Setup finished.'
    if ($script:warningsCount -gt 0) { Write-WastedWarn "$($script:warningsCount) warning(s) above need attention." }
    if ($manualActions.Count -gt 0) {
        Write-WastedStep 'Manual actions still required:'
        $i = 0
        foreach ($item in $manualActions) { $i++; Write-WastedInfo ("  {0}. {1}" -f $i, $item) }
    }
    if ($script:rebootRequired) {
        Write-WastedStep 'REBOOT REQUIRED before the display/audio/feature changes take effect. Reboot, then re-run this script to verify.'
    }
    else {
        Write-WastedInfo 'A reboot is still recommended after first-time setup.'
    }
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
