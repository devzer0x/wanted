#Requires -Version 7.0
# common.ps1 — shared helpers for the WASTED Windows ops scripts.
# Dot-source only (". $PSScriptRoot/common.ps1"); defines functions, no side effects.

Set-StrictMode -Version Latest

# When $PSNativeCommandUseErrorActionPreference is $true, a native command's nonzero exit code
# throws under $ErrorActionPreference = 'Stop'. Every script here treats nonzero exits as data,
# not failure, and inspects $LASTEXITCODE itself (winget probes, pnputil, dism, tscon, nefconw,
# child pwsh runs). It is $false on the PowerShell 7.4.6 this was authored against, but the
# default has moved before and the server will have whatever winget installs, so pin it rather
# than inherit it. Dot-sourcing runs in the caller's scope, so this binds the sourcing script.
$PSNativeCommandUseErrorActionPreference = $false

# Callers that expose a -WastedRoot parameter bind it before dot-sourcing; do not clobber it.
if (-not (Test-Path variable:script:WastedRoot)) {
    $script:WastedRoot = 'C:\wasted'
}

function Write-WastedLog {
    param(
        [Parameter(Mandatory)][ValidateSet('STEP', 'INFO', 'WARN', 'ERROR')][string]$Level,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Message
    )
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $line = '[{0}][{1,-5}] {2}' -f $ts, $Level, $Message
    switch ($Level) {
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        'STEP'  { Write-Host $line -ForegroundColor Cyan }
        default { Write-Host $line }
    }
}

function Write-WastedStep  { param([Parameter(Mandatory)][string]$Message) Write-WastedLog -Level STEP -Message $Message }
function Write-WastedInfo  { param([Parameter(Mandatory)][AllowEmptyString()][string]$Message) Write-WastedLog -Level INFO -Message $Message }
function Write-WastedWarn  { param([Parameter(Mandatory)][string]$Message) Write-WastedLog -Level WARN -Message $Message }
function Write-WastedError { param([Parameter(Mandatory)][string]$Message) Write-WastedLog -Level ERROR -Message $Message }

function Test-WastedElevated {
    # Windows-only; call after the Windows check has passed.
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-WastedEnvironment {
    <#
    Environment guard for every ops script. Aborts loudly (throws) unless this host looks like
    the WASTED Windows game server. -Force overrides the marker checks where that is sensible;
    the Windows check itself is only overridable when the caller opts in
    (-AllowNonWindowsWithForce, e.g. bridge-smoke.ps1 through an SSH tunnel).
    #>
    param(
        [Parameter(Mandatory)][string]$ScriptName,
        [switch]$AllowNonWindowsWithForce,
        [switch]$RequireServerSku,
        [switch]$RequireWastedRoot,
        [switch]$RequireElevation,
        [switch]$Force
    )

    if (-not $IsWindows) {
        if ($AllowNonWindowsWithForce -and $Force) {
            Write-WastedWarn "$ScriptName is running on a non-Windows host; continuing only because -Force was given."
            return
        }
        throw ("{0} must run on the WASTED Windows game server (PowerShell 7 on Windows Server). " -f $ScriptName) +
              "This host is '$([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)'. Aborting."
    }

    if ($RequireServerSku) {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem
        if ($os.ProductType -eq 1) {
            # 1 = workstation, 2 = domain controller, 3 = server
            if ($Force) {
                Write-WastedWarn "$($ScriptName): '$($os.Caption)' is a workstation SKU, not Windows Server; continuing because -Force was given."
            }
            else {
                throw "$ScriptName expects a Windows Server SKU (the WASTED game server) but found '$($os.Caption)'. " +
                      'Re-run with -Force only if this really is an intentional non-server test box.'
            }
        }
        else {
            Write-WastedInfo "Windows Server detected: $($os.Caption) (build $($os.BuildNumber))."
        }
    }

    if ($RequireWastedRoot -and -not (Test-Path -LiteralPath $script:WastedRoot)) {
        if ($Force) {
            Write-WastedWarn "$($ScriptName): marker directory '$script:WastedRoot' is missing; continuing because -Force was given."
        }
        else {
            throw "$ScriptName expects the WASTED layout at '$script:WastedRoot' (created by server-setup.ps1). " +
                  'It is missing, so this is probably not the game server. Run server-setup.ps1 first, or re-run with -Force.'
        }
    }

    if ($RequireElevation -and -not (Test-WastedElevated)) {
        throw "$ScriptName must run elevated (Run as administrator). Aborting."
    }
}

function Start-WastedTranscript {
    # Transcript into C:\wasted\logs (temp dir on a -Force non-Windows run). Returns the path.
    param([Parameter(Mandatory)][string]$Name)
    $dir = if ($IsWindows) { Join-Path $script:WastedRoot 'logs' } else { [System.IO.Path]::GetTempPath() }
    New-Item -ItemType Directory -Path $dir -Force -WhatIf:$false -Confirm:$false | Out-Null
    $path = Join-Path $dir ('{0}_{1}.log' -f $Name, (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Start-Transcript -Path $path -Append | Out-Null
    Write-WastedInfo "Transcript: $path"
    return $path
}

function Stop-WastedTranscript {
    try { Stop-Transcript | Out-Null } catch { <# no transcript active — nothing to stop #> }
}

function Resolve-WastedPython {
    <#
    Locate the python that has the wasted_harness package: the server venv at
    C:\wasted\venv first, then PATH. -Explicit wins when given. Throws when nothing is found.
    #>
    param([string]$Explicit = '')
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit) { return $Explicit }
        $cmd = Get-Command $Explicit -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        throw "Python executable '$Explicit' not found."
    }
    # String concat instead of Join-Path: Join-Path validates the drive, which errors when
    # this helper is exercised on a non-Windows dev box.
    $venvPython = "$($script:WastedRoot)\venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "No python found: expected the harness venv at '$venvPython' or 'python' on PATH. " +
          'Create the venv and pip-install the harness (see scripts/README.md) before running this script.'
}

function Get-WastedFileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
