#Requires -Version 5.1
<#
.SYNOPSIS
    Read-only go/no-go diagnostic for a freshly delivered WASTED game server. Run this FIRST.
.DESCRIPTION
    Answers, in about two minutes and without changing a single thing on the box, whether this
    machine can host the show: Windows edition/build and Desktop-Experience-vs-Core, CPU, RAM,
    disk, the display stack (Intel iGPU present? which driver is bound? is there any display
    target at all? is a virtual display driver installed?), Direct3D feature levels, the current
    session (console vs RDP), the Windows Server components the game needs (Media Foundation,
    .NET 4.8, VC++ runtimes, legacy DirectX, audio services), and outbound reachability to the
    services the pipeline depends on.

    Prints a PASS/WARN/FAIL table with the specific next action for every FAIL and WARN, and
    exits nonzero when a hard blocker is present.

    DELIBERATELY WINDOWS POWERSHELL 5.1 COMPATIBLE and deliberately standalone (it does not
    dot-source common.ps1, which is PowerShell 7 only). Windows Server 2025 ships 5.1 and
    nothing else; this script has to run before PowerShell 7 is installed. Keep it free of
    PS7-only syntax: no ternaries, no ??, no -SkipHttpErrorCheck, no multi-argument Join-Path.
.PARAMETER GameDrive
    Drive letter the game will be installed on (free-space blocker is checked against it).
.PARAMETER MinFreeGB
    Hard blocker threshold for free space on -GameDrive. GTA V Legacy needs ~100 GB plus room
    for the OS, clips and logs.
.PARAMETER SkipDxdiag
    Skip the dxdiag pass (the only built-in way to read Direct3D feature levels). Saves ~30-60 s.
.PARAMETER SkipNetwork
    Skip the outbound reachability probes.
.PARAMETER ExtraProbeHost
    Additional hostnames to TCP-probe on 443 (for example your Supabase project host).
.PARAMETER JsonOut
    Also write the full result set as JSON to this path (bootstrap.ps1 reads it).
.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\preflight.ps1
.EXAMPLE
    pwsh -File .\preflight.ps1 -JsonOut C:\wasted\state\preflight.json
#>
[CmdletBinding()]
param(
    [string]$GameDrive = 'C:',
    [int]$MinFreeGB = 120,
    [switch]$SkipDxdiag,
    [switch]$SkipNetwork,
    [string[]]$ExtraProbeHost = @(),
    [string]$JsonOut = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- environment guard (must abort loudly anywhere that is not the Windows game server) --------
# $IsWindows only exists in PowerShell Core; its absence means Windows PowerShell, which only
# ships on Windows.
$onWindows = $true
$isWindowsVar = Get-Variable -Name IsWindows -ErrorAction SilentlyContinue
if ($null -ne $isWindowsVar) { $onWindows = [bool]$isWindowsVar.Value }
if (-not $onWindows) {
    throw ("preflight.ps1 must run on the WASTED Windows game server. This host is " +
        "'$([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)'. Aborting.")
}

# --- tiny logging + result model ---------------------------------------------------------------

$script:Results = [System.Collections.Generic.List[psobject]]::new()

function Write-Line {
    param(
        [ValidateSet('STEP', 'INFO', 'WARN', 'ERROR')][string]$Level = 'INFO',
        [AllowEmptyString()][string]$Message = ''
    )
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $line = '[{0}][{1,-5}] {2}' -f $ts, $Level, $Message
    switch ($Level) {
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'WARN' { Write-Host $line -ForegroundColor Yellow }
        'STEP' { Write-Host $line -ForegroundColor Cyan }
        default { Write-Host $line }
    }
}

function Add-Result {
    param(
        [Parameter(Mandatory)][string]$Id,
        [Parameter(Mandatory)][string]$Category,
        [Parameter(Mandatory)][ValidateSet('PASS', 'WARN', 'FAIL', 'INFO', 'SKIP')][string]$Result,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Detail,
        [string]$NextAction = ''
    )
    $script:Results.Add([pscustomobject]@{
            Id         = $Id
            Category   = $Category
            Result     = $Result
            Detail     = $Detail
            NextAction = $NextAction
        })
    $level = 'INFO'
    if ($Result -eq 'FAIL') { $level = 'ERROR' }
    elseif ($Result -eq 'WARN') { $level = 'WARN' }
    Write-Line -Level $level -Message ('{0,-16} {1,-4} {2}' -f $Id, $Result, $Detail)
}

function Get-Prop {
    # StrictMode-safe property read: missing property or null value yields $Default.
    param($InputObject, [Parameter(Mandatory)][string]$Name, $Default = $null)
    if ($null -eq $InputObject) { return $Default }
    $prop = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $Default }
    if ($null -eq $prop.Value) { return $Default }
    return $prop.Value
}

function Get-RegistryValue {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name, $Default = $null)
    try {
        if (-not (Test-Path -LiteralPath $Path)) { return $Default }
        $item = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction SilentlyContinue
        return (Get-Prop -InputObject $item -Name $Name -Default $Default)
    }
    catch { return $Default }
}

function Get-Cim {
    param(
        [Parameter(Mandatory)][string]$ClassName,
        [string]$Namespace = 'root\cimv2',
        [string]$Filter = ''
    )
    try {
        $query = @{ ClassName = $ClassName; Namespace = $Namespace; ErrorAction = 'Stop' }
        if ($Filter) { $query.Filter = $Filter }
        return @(Get-CimInstance @query)
    }
    catch {
        Write-Line -Level WARN -Message "WMI query $Namespace/$ClassName failed: $($_.Exception.Message)"
        return @()
    }
}

function Test-Elevated {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-TcpEndpoint {
    param(
        [Parameter(Mandatory)][string]$TargetHost,
        [int]$Port = 443,
        [int]$TimeoutMs = 6000
    )
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            return @{ Ok = $false; Detail = "no answer within $TimeoutMs ms" }
        }
        $client.EndConnect($async)
        return @{ Ok = $true; Detail = "TCP $($TargetHost):$Port open" }
    }
    catch {
        return @{ Ok = $false; Detail = $_.Exception.Message }
    }
    finally {
        $client.Close()
    }
}

# Known Device Manager problem codes we can act on (Win32_PnPEntity.ConfigManagerErrorCode).
$script:ProblemCodes = @{
    10 = 'device cannot start'
    12 = 'not enough free resources'
    18 = 'reinstall the drivers'
    22 = 'device is disabled'
    28 = 'drivers are not installed'
    31 = 'device is not working properly (Windows cannot load the required drivers)'
    43 = 'Windows stopped the device because it reported problems'
    52 = 'Windows cannot verify the digital signature of the drivers'
}

# --- transcript ---------------------------------------------------------------------------------

$logDir = 'C:\wasted\logs'
if (-not (Test-Path -LiteralPath $logDir)) { $logDir = $env:TEMP }
$logPath = Join-Path $logDir ('preflight_{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$transcriptStarted = $false
try {
    Start-Transcript -Path $logPath -Append | Out-Null
    $transcriptStarted = $true
}
catch {
    Write-Line -Level WARN -Message "Could not start a transcript at $($logPath): $($_.Exception.Message)"
}

$exitCode = 0
try {
    Write-Line -Level STEP -Message 'WASTED preflight — read-only. Nothing on this machine is modified.'
    Write-Line -Message "Transcript: $logPath"

    # =========================================================================================
    Write-Line -Level STEP -Message 'Section 1/6: host, OS, session.'
    # =========================================================================================

    $os = @(Get-Cim -ClassName Win32_OperatingSystem)[0]
    $cs = @(Get-Cim -ClassName Win32_ComputerSystem)[0]
    $cpu = @(Get-Cim -ClassName Win32_Processor)[0]

    $caption = Get-Prop $os 'Caption' 'unknown'
    $build = Get-Prop $os 'BuildNumber' '0'
    $productType = [int](Get-Prop $os 'ProductType' 1)
    $displayVersion = Get-RegistryValue 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'DisplayVersion' 'unknown'
    $ubr = Get-RegistryValue 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'UBR' 0
    $installationType = Get-RegistryValue 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'InstallationType' 'unknown'

    $osDetail = "$caption / build $build.$ubr / $displayVersion / InstallationType=$installationType"
    if ($productType -eq 1) {
        Add-Result -Id 'OS-SKU' -Category 'system' -Result 'WARN' -Detail "$osDetail (workstation SKU, not Windows Server)" `
            -NextAction 'Expected Windows Server 2025 Standard from the Hetzner add-on. Confirm the OS image before continuing.'
    }
    else {
        # Windows Server 2025 is build 26100 (the Intel graphics INF gates on NT 10.0 build >= 16225).
        $buildOk = $false
        $buildInt = 0
        if ([int]::TryParse([string]$build, [ref]$buildInt)) { $buildOk = ($buildInt -ge 16225) }
        if ($buildOk) {
            Add-Result -Id 'OS-SKU' -Category 'system' -Result 'PASS' -Detail $osDetail
        }
        else {
            Add-Result -Id 'OS-SKU' -Category 'system' -Result 'FAIL' -Detail $osDetail `
                -NextAction 'Build is below 16225; the Intel DCH graphics INF will not apply to this OS. Reinstall Windows Server 2025.'
        }
    }

    if ($installationType -eq 'Server Core') {
        Add-Result -Id 'OS-DESKTOP' -Category 'system' -Result 'FAIL' -Detail 'Server Core — no interactive desktop.' `
            -NextAction 'Desktop Experience cannot be added after install on Server 2016+. Ask Hetzner to reinstall Windows Server 2025 with the GUI (Desktop Experience) image.'
    }
    elseif ($installationType -eq 'Server') {
        Add-Result -Id 'OS-DESKTOP' -Category 'system' -Result 'PASS' -Detail 'Desktop Experience (GUI) present.'
    }
    else {
        Add-Result -Id 'OS-DESKTOP' -Category 'system' -Result 'WARN' -Detail "InstallationType='$installationType' (not 'Server')." `
            -NextAction 'Confirm this image has the full desktop shell; the game, OBS and Desktop Duplication all need it.'
    }

    $psVersion = $PSVersionTable.PSVersion.ToString()
    $pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwsh) {
        Add-Result -Id 'PS-VERSION' -Category 'system' -Result 'PASS' -Detail "running $psVersion; pwsh at $($pwsh.Source)"
    }
    else {
        Add-Result -Id 'PS-VERSION' -Category 'system' -Result 'WARN' -Detail "running $psVersion; PowerShell 7 (pwsh) is not installed." `
            -NextAction 'winget install --exact --id Microsoft.PowerShell --silent --accept-package-agreements --accept-source-agreements   (every other WASTED script requires PowerShell 7).'
    }

    $elevated = Test-Elevated
    if ($elevated) {
        Add-Result -Id 'ELEVATION' -Category 'system' -Result 'PASS' -Detail 'running elevated; all checks available.'
    }
    else {
        Add-Result -Id 'ELEVATION' -Category 'system' -Result 'WARN' -Detail 'not elevated — DISM/feature checks will be skipped.' `
            -NextAction 'Re-run this script from an elevated prompt for the complete picture.'
    }

    $cpuName = (Get-Prop $cpu 'Name' 'unknown').ToString().Trim()
    $cores = Get-Prop $cpu 'NumberOfCores' 0
    $threads = Get-Prop $cpu 'NumberOfLogicalProcessors' 0
    $cpuDetail = "$cpuName ($cores cores / $threads threads)"
    if ($threads -ge 8) {
        Add-Result -Id 'CPU' -Category 'system' -Result 'PASS' -Detail $cpuDetail
    }
    else {
        Add-Result -Id 'CPU' -Category 'system' -Result 'WARN' -Detail $cpuDetail `
            -NextAction 'Fewer than 8 logical processors: x264 software encoding plus the game will contend. Expect to drop the stream to 720p30 veryfast or lower.'
    }

    $ramBytes = [double](Get-Prop $cs 'TotalPhysicalMemory' 0)
    $ramGB = [math]::Round($ramBytes / 1GB, 1)
    if ($ramGB -ge 8) {
        Add-Result -Id 'RAM' -Category 'system' -Result 'PASS' -Detail "$ramGB GB physical memory."
    }
    else {
        Add-Result -Id 'RAM' -Category 'system' -Result 'FAIL' -Detail "$ramGB GB physical memory." `
            -NextAction 'GTA V + OBS + the harness need 8 GB minimum. This machine is undersized.'
    }

    $disks = Get-Cim -ClassName Win32_LogicalDisk -Filter 'DriveType=3'
    $diskSummary = @()
    $gameDriveLetter = $GameDrive.TrimEnd('\')
    if ($gameDriveLetter -notmatch ':$') { $gameDriveLetter = $gameDriveLetter + ':' }
    $gameDriveFreeGB = -1
    foreach ($disk in $disks) {
        $id = Get-Prop $disk 'DeviceID' '?'
        $free = [math]::Round(([double](Get-Prop $disk 'FreeSpace' 0)) / 1GB, 1)
        $size = [math]::Round(([double](Get-Prop $disk 'Size' 0)) / 1GB, 1)
        $diskSummary += "$id $free GB free of $size GB"
        if ($id -eq $gameDriveLetter) { $gameDriveFreeGB = $free }
    }
    $diskDetail = ($diskSummary -join '; ')
    if ($gameDriveFreeGB -lt 0) {
        Add-Result -Id 'DISK' -Category 'system' -Result 'FAIL' -Detail "$diskDetail (no fixed disk '$gameDriveLetter')." `
            -NextAction "Pass -GameDrive with a real drive letter, or check why $gameDriveLetter is missing."
    }
    elseif ($gameDriveFreeGB -lt $MinFreeGB) {
        Add-Result -Id 'DISK' -Category 'system' -Result 'FAIL' -Detail "$diskDetail (need >= $MinFreeGB GB on $gameDriveLetter)." `
            -NextAction "Free space or install the game on the second NVMe. GTA V Legacy alone is ~100 GB; clips and logs grow on top."
    }
    else {
        Add-Result -Id 'DISK' -Category 'system' -Result 'PASS' -Detail $diskDetail
    }

    $sessionName = $env:SESSIONNAME
    if (-not $sessionName) { $sessionName = '<unset>' }
    $sessionTable = @()
    try {
        $sessionTable = @(& "$env:SystemRoot\System32\query.exe" session 2>&1 | ForEach-Object { $_.ToString().TrimEnd() })
    }
    catch {
        $sessionTable = @("query session failed: $($_.Exception.Message)")
    }
    foreach ($row in $sessionTable) { Write-Line -Message "  $row" }
    if ($sessionName -eq 'Console') {
        Add-Result -Id 'SESSION' -Category 'system' -Result 'PASS' -Detail "SESSIONNAME=$sessionName — this is the console session."
    }
    elseif ($sessionName -like 'RDP-Tcp#*') {
        Add-Result -Id 'SESSION' -Category 'system' -Result 'WARN' -Detail "SESSIONNAME=$sessionName — this is an RDP session." `
            -NextAction 'Fine for setup. The game and OBS must NOT be launched from here: RDP sessions get the Microsoft Basic Render Driver by default and die on disconnect. Use detach-rdp.ps1 (tscon /dest:console) before leaving.'
    }
    else {
        Add-Result -Id 'SESSION' -Category 'system' -Result 'WARN' -Detail "SESSIONNAME=$sessionName — not a recognised interactive session (SSH?)." `
            -NextAction 'Display and capture checks below reflect this session only. Re-run from the console or an RDP session for display truth.'
    }

    # =========================================================================================
    Write-Line -Level STEP -Message 'Section 2/6: display adapters, drivers, display targets.'
    # =========================================================================================

    $videoControllers = Get-Cim -ClassName Win32_VideoController
    $adapterLines = @()
    $intelAdapters = @()
    $basicAdapters = @()
    $virtualAdapters = @()
    $discreteAdapters = @()
    $activeResolutions = @()

    foreach ($vc in $videoControllers) {
        $name = [string](Get-Prop $vc 'Name' 'unknown')
        $pnp = [string](Get-Prop $vc 'PNPDeviceID' '')
        $drv = [string](Get-Prop $vc 'DriverVersion' 'none')
        $drvDate = Get-Prop $vc 'DriverDate' $null
        $hres = Get-Prop $vc 'CurrentHorizontalResolution' 0
        $vres = Get-Prop $vc 'CurrentVerticalResolution' 0
        $err = [int](Get-Prop $vc 'ConfigManagerErrorCode' 0)
        $dateText = 'unknown'
        if ($null -ne $drvDate) { $dateText = ([datetime]$drvDate).ToString('yyyy-MM-dd') }
        $line = "$name | driver $drv ($dateText) | $pnp | mode ${hres}x${vres} | cmError $err"
        $adapterLines += $line
        Write-Line -Message "  $line"

        if ($hres -gt 0 -and $vres -gt 0) { $activeResolutions += "${hres}x${vres} on $name" }
        if ($pnp -like 'PCI\VEN_8086*') { $intelAdapters += $vc }
        if ($pnp -like 'PCI\VEN_10DE*' -or $pnp -like 'PCI\VEN_1002*') { $discreteAdapters += $vc }
        if ($name -like '*Basic Display*' -or $name -like '*Basic Render*' -or $pnp -like 'ROOT\BASICDISPLAY*' -or $pnp -like 'ROOT\BASICRENDER*') {
            $basicAdapters += $vc
        }
        if ($pnp -like 'ROOT\MTTVDD*' -or $pnp -like 'ROOT\IDDSAMPLEDRIVER*' -or $pnp -like 'ROOT\USBMMIDD*' -or
            $name -like '*Virtual Display*' -or $name -like '*IddSampleDriver*' -or $name -like '*usbmmidd*' -or
            $name -like '*Parsec Virtual*' -or $name -like '*Amyuni*') {
            $virtualAdapters += $vc
        }
    }

    if ($videoControllers.Count -eq 0) {
        Add-Result -Id 'GPU-ADAPTERS' -Category 'display' -Result 'FAIL' -Detail 'Win32_VideoController returned nothing.' `
            -NextAction 'No display adapter is enumerated at all. Request the free Hetzner KVM console and confirm the board posts video on the iGPU / that the iGPU is enabled in BIOS.'
    }
    else {
        Add-Result -Id 'GPU-ADAPTERS' -Category 'display' -Result 'INFO' -Detail (($adapterLines) -join ' || ')
    }

    if ($intelAdapters.Count -gt 0) {
        $ig = $intelAdapters[0]
        $igName = [string](Get-Prop $ig 'Name' 'Intel display adapter')
        $igDrv = [string](Get-Prop $ig 'DriverVersion' 'none')
        $igErr = [int](Get-Prop $ig 'ConfigManagerErrorCode' 0)
        if ($igErr -ne 0) {
            $meaning = 'see Device Manager'
            if ($script:ProblemCodes.ContainsKey($igErr)) { $meaning = $script:ProblemCodes[$igErr] }
            Add-Result -Id 'GPU-INTEL' -Category 'display' -Result 'FAIL' -Detail "$igName present but Device Manager problem code $igErr ($meaning)." `
                -NextAction 'Install the Intel 11th-14th Gen graphics package by INF (pnputil /add-driver ...\iigd_dch.inf /install) as documented in scripts/README.md. Do NOT run Intel setup.exe: it blocks Server SKUs.'
        }
        elseif ($igName -like '*Basic*') {
            Add-Result -Id 'GPU-INTEL' -Category 'display' -Result 'FAIL' -Detail "Intel device present ($igName) but bound to the Microsoft basic driver." `
                -NextAction 'No WDDM driver means no Direct3D 11, and GTA V will not start. Install the Intel graphics INF (see scripts/README.md, "Intel graphics driver").'
        }
        else {
            Add-Result -Id 'GPU-INTEL' -Category 'display' -Result 'PASS' -Detail "$igName, driver $igDrv."
        }
    }
    else {
        Add-Result -Id 'GPU-INTEL' -Category 'display' -Result 'FAIL' -Detail 'No PCI\VEN_8086 display adapter enumerated.' `
            -NextAction 'The i5-12500 UHD 770 iGPU is the only render device on this box. Request the free Hetzner KVM console and check the BIOS iGPU setting before doing anything else.'
    }

    if ($discreteAdapters.Count -gt 0) {
        $names = @()
        foreach ($d in $discreteAdapters) { $names += [string](Get-Prop $d 'Name' 'unknown') }
        Add-Result -Id 'GPU-DISCRETE' -Category 'display' -Result 'INFO' -Detail ("discrete GPU present: " + ($names -join ', '))
    }
    else {
        Add-Result -Id 'GPU-DISCRETE' -Category 'display' -Result 'INFO' -Detail 'no discrete GPU (expected on this auction server).'
    }

    $renderCapable = @($videoControllers | Where-Object {
            $n = [string](Get-Prop $_ 'Name' '')
            $p = [string](Get-Prop $_ 'PNPDeviceID' '')
            ($n -notlike '*Basic Display*') -and ($n -notlike '*Basic Render*') -and
            ($p -notlike 'ROOT\BASICDISPLAY*') -and ($p -notlike 'ROOT\BASICRENDER*')
        })
    if ($renderCapable.Count -gt 0) {
        Add-Result -Id 'GPU-DRIVER' -Category 'display' -Result 'PASS' -Detail "$($renderCapable.Count) adapter(s) bound to a vendor WDDM driver."
    }
    else {
        Add-Result -Id 'GPU-DRIVER' -Category 'display' -Result 'FAIL' -Detail 'Every adapter is on a Microsoft basic driver — no Direct3D 11.' `
            -NextAction 'GTA V cannot start without a real WDDM driver. Install the Intel graphics INF first; nothing else matters until this is PASS.'
    }

    $displayPnp = Get-Cim -ClassName Win32_PnPEntity -Filter "PNPClass='Display'"
    $problemDevices = @()
    foreach ($dev in $displayPnp) {
        $code = [int](Get-Prop $dev 'ConfigManagerErrorCode' 0)
        if ($code -ne 0) {
            $meaning = 'see Device Manager'
            if ($script:ProblemCodes.ContainsKey($code)) { $meaning = $script:ProblemCodes[$code] }
            $problemDevices += "$([string](Get-Prop $dev 'Name' 'unknown')): code $code ($meaning)"
        }
    }
    if ($problemDevices.Count -eq 0) {
        Add-Result -Id 'GPU-PROBLEM' -Category 'display' -Result 'PASS' -Detail 'no display-class device reports a Device Manager problem code.'
    }
    else {
        Add-Result -Id 'GPU-PROBLEM' -Category 'display' -Result 'FAIL' -Detail ($problemDevices -join '; ') `
            -NextAction 'Code 52 = unsigned/untrusted driver (import the publisher certificate into LocalMachine\TrustedPublisher). Code 31/28 = driver not loaded (re-run pnputil /add-driver ... /install). Code 43 = the device failed; check the KVM console.'
    }

    if ($virtualAdapters.Count -gt 0) {
        $names = @()
        foreach ($v in $virtualAdapters) { $names += [string](Get-Prop $v 'Name' 'unknown') }
        Add-Result -Id 'VDD' -Category 'display' -Result 'PASS' -Detail ('virtual display driver present: ' + ($names -join ', '))
    }
    else {
        Add-Result -Id 'VDD' -Category 'display' -Result 'WARN' -Detail 'no virtual (indirect) display driver installed.' `
            -NextAction 'This server has no monitor and no HDMI emulator, so the console session has no display target. Run server-setup.ps1 (it installs the Virtual Display Driver) before expecting the game to start.'
    }

    $monitors = Get-Cim -ClassName Win32_PnPEntity -Filter "PNPClass='Monitor'"
    $wmiMonitors = Get-Cim -ClassName WmiMonitorBasicDisplayParams -Namespace 'root\wmi'
    $monitorCount = $monitors.Count
    if ($monitorCount -eq 0 -and $activeResolutions.Count -eq 0) {
        Add-Result -Id 'DISPLAY-TARGET' -Category 'display' -Result 'WARN' -Detail 'no monitor device and no adapter reporting an active display mode.' `
            -NextAction 'Expected before the virtual display driver is installed. After server-setup.ps1 this must become a 1280x720 (or larger) active mode, or the game has nowhere to present.'
    }
    else {
        $resText = 'none'
        if ($activeResolutions.Count -gt 0) { $resText = ($activeResolutions -join ', ') }
        $detail = "$monitorCount monitor device(s), $($wmiMonitors.Count) WMI monitor(s); active mode: $resText"
        if ($resText -like '1024x768*') {
            Add-Result -Id 'DISPLAY-TARGET' -Category 'display' -Result 'WARN' -Detail $detail `
                -NextAction '1024x768 is the classic no-display-target fallback mode. Install/repair the virtual display driver and pin 1280x720 first in its resolution list.'
        }
        else {
            Add-Result -Id 'DISPLAY-TARGET' -Category 'display' -Result 'PASS' -Detail $detail
        }
    }

    $rdsHw = Get-RegistryValue 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services' 'bEnumerateHWBeforeSW' $null
    if ($null -eq $rdsHw) {
        Add-Result -Id 'RDS-HWGPU' -Category 'display' -Result 'INFO' -Detail 'bEnumerateHWBeforeSW not set: RDP sessions use the Microsoft Basic Render Driver (software).' `
            -NextAction 'server-setup.ps1 sets it to 1. It only makes RDP diagnostics honest — the game still must run in the console session.'
    }
    else {
        Add-Result -Id 'RDS-HWGPU' -Category 'display' -Result 'PASS' -Detail "bEnumerateHWBeforeSW=$rdsHw (RDP sessions may enumerate the hardware adapter)."
    }

    # =========================================================================================
    Write-Line -Level STEP -Message 'Section 3/6: Direct3D feature levels (dxdiag).'
    # =========================================================================================

    if ($SkipDxdiag) {
        Add-Result -Id 'D3D-LEVEL' -Category 'display' -Result 'SKIP' -Detail 'skipped (-SkipDxdiag).'
    }
    else {
        $dxdiag = Join-Path $env:SystemRoot 'System32\dxdiag.exe'
        if (-not (Test-Path -LiteralPath $dxdiag)) {
            Add-Result -Id 'D3D-LEVEL' -Category 'display' -Result 'WARN' -Detail 'dxdiag.exe not present on this image.' `
                -NextAction 'Feature levels cannot be read with in-box tooling. Judge the display stack from GPU-DRIVER and DISPLAY-TARGET instead.'
        }
        else {
            # dxdiag /t is the only in-box way to read Direct3D feature levels. /whql:off skips the
            # driver-signature web check, which otherwise blocks for minutes on a fresh server.
            $report = Join-Path $env:TEMP 'wasted-dxdiag.txt'
            if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report -Force -ErrorAction SilentlyContinue }
            Write-Line -Message 'Running dxdiag (up to 120 s)...'
            $proc = Start-Process -FilePath $dxdiag -ArgumentList '/whql:off', '/t', $report -PassThru -WindowStyle Hidden
            $null = $proc.WaitForExit(120000)
            $waited = 0
            while (-not (Test-Path -LiteralPath $report) -and $waited -lt 20) { Start-Sleep -Seconds 1; $waited++ }
            if (-not (Test-Path -LiteralPath $report)) {
                Add-Result -Id 'D3D-LEVEL' -Category 'display' -Result 'WARN' -Detail 'dxdiag produced no report within the timeout.' `
                    -NextAction 'Run "dxdiag /whql:off /t %TEMP%\dx.txt" by hand and read the "Feature Levels" line under Display Devices.'
            }
            else {
                $text = Get-Content -LiteralPath $report -Raw -ErrorAction SilentlyContinue
                $levels = @()
                $ddi = @()
                $wddm = @()
                foreach ($l in ($text -split "`r?`n")) {
                    if ($l -match '^\s*Feature Levels:\s*(.+)$') { $levels += $Matches[1].Trim() }
                    elseif ($l -match '^\s*DDI Version:\s*(.+)$') { $ddi += $Matches[1].Trim() }
                    elseif ($l -match '^\s*Driver Model:\s*(.+)$') { $wddm += $Matches[1].Trim() }
                }
                $detail = "FeatureLevels=[$($levels -join ' | ')] DDI=[$($ddi -join ' | ')] DriverModel=[$($wddm -join ' | ')] (report: $report)"
                $has11 = $false
                foreach ($lv in $levels) { if ($lv -match '11_0|11_1|12_0|12_1|12_2') { $has11 = $true } }
                if ($levels.Count -eq 0) {
                    Add-Result -Id 'D3D-LEVEL' -Category 'display' -Result 'WARN' -Detail "no 'Feature Levels' line in the dxdiag report. $detail" `
                        -NextAction "Open $report and read the Display Devices section by hand."
                }
                elseif ($has11) {
                    Add-Result -Id 'D3D-LEVEL' -Category 'display' -Result 'PASS' -Detail $detail
                }
                else {
                    Add-Result -Id 'D3D-LEVEL' -Category 'display' -Result 'FAIL' -Detail $detail `
                        -NextAction 'GTA V Legacy is a Direct3D 11 client and needs feature level 11_0 or better. Install the Intel graphics driver; until then the only adapter is the software/basic one.'
                }
            }
        }
    }

    # =========================================================================================
    Write-Line -Level STEP -Message 'Section 4/6: Windows Server components the game needs.'
    # =========================================================================================

    # Media Foundation. Server SKUs behave like Windows N: the launcher/game check the media
    # feature set, so its absence shows up as the "Windows Media Player" family of errors.
    $mfState = 'unknown'
    $mfMethod = 'none'
    $getWindowsFeature = Get-Command Get-WindowsFeature -ErrorAction SilentlyContinue
    if ($getWindowsFeature) {
        try {
            $feature = Get-WindowsFeature -Name 'Server-Media-Foundation' -ErrorAction Stop
            if ($null -ne $feature) {
                $installed = Get-Prop $feature 'Installed' $false
                $mfState = 'Installed'
                if (-not $installed) { $mfState = 'NotInstalled' }
                $mfMethod = 'Get-WindowsFeature'
            }
        }
        catch {
            Write-Line -Level WARN -Message "Get-WindowsFeature failed ($($_.Exception.Message)); falling back to DISM."
        }
    }
    if ($mfState -eq 'unknown' -and $elevated) {
        foreach ($featureName in @('ServerMediaFoundation', 'Server-Media-Foundation')) {
            try {
                $dismOut = & "$env:SystemRoot\System32\dism.exe" /online /english /get-featureinfo /featurename:$featureName 2>&1
                $joined = ($dismOut | Out-String)
                if ($joined -match 'State\s*:\s*(\w+)') {
                    $mfState = $Matches[1]
                    $mfMethod = "dism /featurename:$featureName"
                    break
                }
            }
            catch { continue }
        }
    }
    $mfPlat = Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\mfplat.dll')
    if ($mfState -eq 'Installed' -or $mfState -eq 'Enabled') {
        Add-Result -Id 'MEDIA-FOUNDATION' -Category 'components' -Result 'PASS' -Detail "Server-Media-Foundation $mfState (via $mfMethod); mfplat.dll present=$mfPlat"
    }
    elseif ($mfState -eq 'unknown') {
        Add-Result -Id 'MEDIA-FOUNDATION' -Category 'components' -Result 'WARN' -Detail "could not determine feature state (elevated=$elevated); mfplat.dll present=$mfPlat" `
            -NextAction 'From an elevated prompt: Get-WindowsFeature Server-Media-Foundation. If not installed: Install-WindowsFeature Server-Media-Foundation -Restart.'
    }
    else {
        Add-Result -Id 'MEDIA-FOUNDATION' -Category 'components' -Result 'FAIL' -Detail "Server-Media-Foundation $mfState (via $mfMethod); mfplat.dll present=$mfPlat" `
            -NextAction 'Install-WindowsFeature Server-Media-Foundation -Restart  (server-setup.ps1 does this). Without it the Rockstar installer/launcher fails with the "Windows Media Player / Media Feature Pack" family of errors.'
    }

    $ndpRelease = [int](Get-RegistryValue 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' 'Release' 0)
    if ($ndpRelease -ge 528040) {
        Add-Result -Id 'DOTNET48' -Category 'components' -Result 'PASS' -Detail ".NET Framework 4.8+ present (Release=$ndpRelease)."
    }
    else {
        Add-Result -Id 'DOTNET48' -Category 'components' -Result 'FAIL' -Detail ".NET Framework release=$ndpRelease (need >= 528040 for 4.8)." `
            -NextAction 'ScriptHookVDotNet v3 targets .NET Framework 4.8. Server 2025 ships 4.8.1 preinstalled — if this is missing the image is wrong.'
    }

    $vcx64 = Get-RegistryValue 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' 'Installed' 0
    $vcx64Ver = Get-RegistryValue 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' 'Version' 'none'
    $vcx86 = Get-RegistryValue 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86' 'Installed' 0
    $vcx86Ver = Get-RegistryValue 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86' 'Version' 'none'
    if ([int]$vcx64 -eq 1 -and [int]$vcx86 -eq 1) {
        Add-Result -Id 'VCREDIST' -Category 'components' -Result 'PASS' -Detail "VC++ 14 x64=$vcx64Ver x86=$vcx86Ver."
    }
    else {
        Add-Result -Id 'VCREDIST' -Category 'components' -Result 'WARN' -Detail "VC++ 14 x64 installed=$vcx64 ($vcx64Ver), x86 installed=$vcx86 ($vcx86Ver)." `
            -NextAction 'Both architectures are needed (Social Club / launcher components are 32-bit). server-setup.ps1 installs them from https://aka.ms/vc14/vc_redist.x64.exe and .../vc_redist.x86.exe.'
    }

    $dxLegacy = @()
    foreach ($pair in @(
            @{ Dir = 'System32'; File = 'd3dcompiler_43.dll' },
            @{ Dir = 'SysWOW64'; File = 'd3dx9_43.dll' },
            @{ Dir = 'SysWOW64'; File = 'xinput1_3.dll' }
        )) {
        $p = Join-Path (Join-Path $env:SystemRoot $pair.Dir) $pair.File
        if (-not (Test-Path -LiteralPath $p)) { $dxLegacy += "$($pair.Dir)\$($pair.File)" }
    }
    if ($dxLegacy.Count -eq 0) {
        Add-Result -Id 'DIRECTX-LEGACY' -Category 'components' -Result 'PASS' -Detail 'June 2010 DirectX side-by-side runtime DLLs present.'
    }
    else {
        Add-Result -Id 'DIRECTX-LEGACY' -Category 'components' -Result 'WARN' -Detail ('missing: ' + ($dxLegacy -join ', ')) `
            -NextAction 'Install the DirectX End-User Runtime (June 2010) — DXSETUP.exe /silent. Script Hook V and many ASI mods link D3DX9/D3DCompiler_43/XInput1_3.'
    }

    $audioRows = @()
    $audioBad = @()
    foreach ($svcName in @('AudioEndpointBuilder', 'Audiosrv', 'MMCSS')) {
        $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
        if ($null -eq $svc) {
            $audioRows += "$svcName MISSING"
            $audioBad += $svcName
            continue
        }
        $status = [string](Get-Prop $svc 'Status' 'unknown')
        $start = [string](Get-Prop $svc 'StartType' 'unknown')
        $audioRows += "$svcName $status/$start"
        if ($status -ne 'Running') { $audioBad += $svcName }
    }
    if ($audioBad.Count -eq 0) {
        Add-Result -Id 'AUDIO-SERVICES' -Category 'components' -Result 'PASS' -Detail ($audioRows -join '; ')
    }
    else {
        Add-Result -Id 'AUDIO-SERVICES' -Category 'components' -Result 'WARN' -Detail ($audioRows -join '; ') `
            -NextAction 'Audio is disabled by default on Server. server-setup.ps1 sets AudioEndpointBuilder first, then Audiosrv and MMCSS. VB-CABLE must be installed only AFTER those are running, or OBS will not see the device.'
    }

    $vbCable = Get-Cim -ClassName Win32_SoundDevice
    $cableFound = @($vbCable | Where-Object {
            $n = [string](Get-Prop $_ 'Name' '')
            $n -like '*VB-Audio*' -or $n -like '*CABLE*'
        })
    if ($cableFound.Count -gt 0) {
        Add-Result -Id 'VB-CABLE' -Category 'components' -Result 'PASS' -Detail ([string](Get-Prop $cableFound[0] 'Name' 'VB-CABLE'))
    }
    else {
        Add-Result -Id 'VB-CABLE' -Category 'components' -Result 'WARN' -Detail 'no VB-Audio virtual cable device found.' `
            -NextAction 'server-setup.ps1 stages it; the driver click is interactive and a reboot is required. OBS must capture "CABLE Output" explicitly, never "Default".'
    }

    # =========================================================================================
    Write-Line -Level STEP -Message 'Section 5/6: outbound reachability.'
    # =========================================================================================

    if ($SkipNetwork) {
        Add-Result -Id 'NET' -Category 'network' -Result 'SKIP' -Detail 'skipped (-SkipNetwork).'
    }
    else {
        $probes = @(
            @{ Id = 'NET-STEAM'; Host = 'store.steampowered.com'; Critical = $true; Why = 'Steam client + GTA V download' },
            @{ Id = 'NET-ANTHROPIC'; Host = 'api.anthropic.com'; Critical = $true; Why = "the agent's brain (Claude API)" },
            @{ Id = 'NET-SUPABASE'; Host = 'supabase.com'; Critical = $true; Why = 'events/decisions write path' },
            @{ Id = 'NET-GITHUB'; Host = 'api.github.com'; Critical = $true; Why = 'SHVDN nightly + virtual display driver releases' },
            @{ Id = 'NET-DEVC'; Host = 'www.dev-c.com'; Critical = $false; Why = 'Script Hook V download' },
            @{ Id = 'NET-ROCKSTAR'; Host = 'socialclub.rockstargames.com'; Critical = $false; Why = 'Rockstar / Social Club sign-in' }
        )
        foreach ($extra in $ExtraProbeHost) {
            if ($extra) { $probes += @{ Id = "NET-EXTRA-$extra"; Host = $extra; Critical = $true; Why = 'operator-supplied host' } }
        }
        foreach ($probe in $probes) {
            $res = Test-TcpEndpoint -TargetHost $probe.Host -Port 443
            if ($res.Ok) {
                Add-Result -Id $probe.Id -Category 'network' -Result 'PASS' -Detail "$($probe.Host):443 reachable — $($probe.Why)."
            }
            elseif ($probe.Critical) {
                Add-Result -Id $probe.Id -Category 'network' -Result 'FAIL' -Detail "$($probe.Host):443 unreachable ($($res.Detail)) — $($probe.Why)." `
                    -NextAction 'Check the outbound firewall and DNS. Nothing downstream works without this host.'
            }
            else {
                Add-Result -Id $probe.Id -Category 'network' -Result 'WARN' -Detail "$($probe.Host):443 unreachable ($($res.Detail)) — $($probe.Why)." `
                    -NextAction 'Not a hard blocker: the affected download has a documented manual fallback in scripts/README.md.'
            }
        }
    }

    # =========================================================================================
    Write-Line -Level STEP -Message 'Section 6/6: tooling already on the box (informational).'
    # =========================================================================================

    $tools = @(
        @{ Name = 'winget'; Cmd = 'winget' },
        @{ Name = 'git'; Cmd = 'git' },
        @{ Name = 'python'; Cmd = 'python' },
        @{ Name = 'dotnet'; Cmd = 'dotnet' },
        @{ Name = '7z'; Cmd = '7z' }
    )
    $present = @()
    $absent = @()
    foreach ($tool in $tools) {
        $cmd = Get-Command $tool.Cmd -ErrorAction SilentlyContinue
        if ($cmd) { $present += $tool.Name } else { $absent += $tool.Name }
    }
    foreach ($appPath in @(
            @{ Name = 'Steam'; Path = "${env:ProgramFiles(x86)}\Steam\steam.exe" },
            @{ Name = 'OBS'; Path = "$env:ProgramFiles\obs-studio\bin\64bit\obs64.exe" }
        )) {
        if (Test-Path -LiteralPath $appPath.Path) { $present += $appPath.Name } else { $absent += $appPath.Name }
    }
    Add-Result -Id 'TOOLING' -Category 'tooling' -Result 'INFO' `
        -Detail ("present: [" + ($present -join ', ') + "]; absent: [" + ($absent -join ', ') + "]")
    if ($absent -contains 'winget') {
        Add-Result -Id 'WINGET' -Category 'tooling' -Result 'FAIL' -Detail 'winget is not on PATH.' `
            -NextAction 'server-setup.ps1 installs the whole toolchain through winget. Install "App Installer" from Microsoft first, then re-run.'
    }

    $gameDirCandidates = @(
        "${env:ProgramFiles(x86)}\Steam\steamapps\common\Grand Theft Auto V",
        "$env:ProgramFiles\Steam\steamapps\common\Grand Theft Auto V"
    )
    $gameFound = ''
    foreach ($candidate in $gameDirCandidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'GTA5.exe')) { $gameFound = $candidate; break }
    }
    if ($gameFound) {
        Add-Result -Id 'GAME' -Category 'tooling' -Result 'PASS' -Detail "GTA5.exe found at $gameFound."
    }
    else {
        Add-Result -Id 'GAME' -Category 'tooling' -Result 'INFO' -Detail 'GTA V not installed yet (expected on a fresh server).'
    }

    # =========================================================================================
    # Verdict
    # =========================================================================================

    $fails = @($script:Results | Where-Object { $_.Result -eq 'FAIL' })
    $warns = @($script:Results | Where-Object { $_.Result -eq 'WARN' })
    $passes = @($script:Results | Where-Object { $_.Result -eq 'PASS' })

    Write-Host ''
    Write-Host '================================ WASTED PREFLIGHT VERDICT ================================'
    Write-Host ('{0,-18} {1,-11} {2,-6} {3}' -f 'CHECK', 'CATEGORY', 'RESULT', 'DETAIL')
    Write-Host ('-' * 88)
    foreach ($row in $script:Results) {
        $colour = 'Gray'
        switch ($row.Result) {
            'PASS' { $colour = 'Green' }
            'WARN' { $colour = 'Yellow' }
            'FAIL' { $colour = 'Red' }
            'SKIP' { $colour = 'DarkGray' }
            default { $colour = 'Gray' }
        }
        $detail = $row.Detail
        if ($detail.Length -gt 200) { $detail = $detail.Substring(0, 200) + '...' }
        Write-Host ('{0,-18} {1,-11} {2,-6} {3}' -f $row.Id, $row.Category, $row.Result, $detail) -ForegroundColor $colour
    }
    Write-Host ('-' * 88)
    Write-Host ("PASS={0}  WARN={1}  FAIL={2}" -f $passes.Count, $warns.Count, $fails.Count)

    if ($fails.Count -gt 0) {
        Write-Host ''
        Write-Host 'BLOCKERS — fix these before anything else:' -ForegroundColor Red
        $n = 0
        foreach ($row in $fails) {
            $n++
            Write-Host ("  {0}. [{1}] {2}" -f $n, $row.Id, $row.Detail) -ForegroundColor Red
            if ($row.NextAction) { Write-Host ("     -> {0}" -f $row.NextAction) -ForegroundColor Red }
        }
    }
    if ($warns.Count -gt 0) {
        Write-Host ''
        Write-Host 'WARNINGS — expected on a fresh box, resolved by server-setup.ps1 unless noted:' -ForegroundColor Yellow
        $n = 0
        foreach ($row in $warns) {
            $n++
            Write-Host ("  {0}. [{1}] {2}" -f $n, $row.Id, $row.Detail) -ForegroundColor Yellow
            if ($row.NextAction) { Write-Host ("     -> {0}" -f $row.NextAction) -ForegroundColor Yellow }
        }
    }

    if ($JsonOut) {
        $payload = [ordered]@{
            generated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            host_name    = $env:COMPUTERNAME
            os           = $osDetail
            session      = $sessionName
            pass         = $passes.Count
            warn         = $warns.Count
            fail         = $fails.Count
            checks       = @($script:Results)
        }
        $dir = Split-Path -Parent $JsonOut
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $JsonOut -Encoding UTF8
        Write-Line -Message "Machine-readable results written to $JsonOut"
    }

    Write-Host ''
    if ($fails.Count -gt 0) {
        Write-Line -Level ERROR -Message "PREFLIGHT: NO-GO — $($fails.Count) hard blocker(s). Do not run server-setup.ps1 until they are cleared."
        $exitCode = 1
    }
    elseif ($warns.Count -gt 0) {
        Write-Line -Level WARN -Message "PREFLIGHT: GO WITH WARNINGS — $($warns.Count) item(s) that server-setup.ps1 is expected to fix. Proceed to bootstrap.ps1."
        $exitCode = 0
    }
    else {
        Write-Line -Level STEP -Message 'PREFLIGHT: GO — no blockers, no warnings. Proceed to bootstrap.ps1.'
        $exitCode = 0
    }
}
catch {
    Write-Line -Level ERROR -Message "preflight.ps1 failed: $($_.Exception.Message)"
    Write-Line -Level ERROR -Message $_.ScriptStackTrace
    $exitCode = 3
}
finally {
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch { Write-Host 'No transcript to stop.' }
    }
}

exit $exitCode
