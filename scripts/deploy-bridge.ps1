#Requires -Version 7.0
<#
.SYNOPSIS
    Deploy the built bridge (WastedBridge.dll + Newtonsoft.Json.dll) into the game's scripts folder.
.DESCRIPTION
    Copies bridge\bin\Release\net48\WastedBridge.dll and Newtonsoft.Json.dll into
    <GameDir>\scripts\ (SHVDN's script folder, created if missing), moving any previously
    deployed copies into a timestamped backup folder first. Verify afterwards with
    bridge-smoke.ps1 while the game is running.
.PARAMETER GameDir
    GTA V Legacy install folder (contains GTA5.exe).
.PARAMETER BuildDir
    Bridge Release output. Default: <repo>\bridge\bin\Release\net48.
.EXAMPLE
    pwsh -File .\deploy-bridge.ps1 -GameDir 'C:\Program Files (x86)\Steam\steamapps\common\Grand Theft Auto V'
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$GameDir,
    [string]$BuildDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'bridge\bin\Release\net48'),
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
. $commonPath

Assert-WastedEnvironment -ScriptName 'deploy-bridge.ps1' -RequireWastedRoot -Force:$Force
Start-WastedTranscript -Name 'deploy-bridge' | Out-Null

$artifacts = @('WastedBridge.dll', 'Newtonsoft.Json.dll')

try {
    if (-not (Test-Path -LiteralPath (Join-Path $GameDir 'GTA5.exe'))) {
        if ($Force) {
            Write-WastedWarn "GTA5.exe not found in '$GameDir' — continuing because -Force was given (staging use only)."
        }
        else {
            throw "'$GameDir' does not contain GTA5.exe — not a GTA V Legacy install. Aborting (use -Force only for a staging directory)."
        }
    }
    foreach ($name in $artifacts) {
        if (-not (Test-Path -LiteralPath (Join-Path $BuildDir $name))) {
            throw "Missing build artifact '$name' in '$BuildDir'. Build the bridge first: dotnet build bridge -c Release (see bridge/README.md)."
        }
    }

    $scriptsDir = Join-Path $GameDir 'scripts'
    New-Item -ItemType Directory -Path $scriptsDir -Force | Out-Null

    $backupDir = Join-Path $scriptsDir ('backup\{0}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    foreach ($name in $artifacts) {
        $source = Join-Path $BuildDir $name
        $target = Join-Path $scriptsDir $name
        if ($PSCmdlet.ShouldProcess($target, "deploy $name")) {
            if (Test-Path -LiteralPath $target) {
                New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
                Move-Item -LiteralPath $target -Destination (Join-Path $backupDir $name) -Force
                Write-WastedInfo "Previous $name backed up to $backupDir."
            }
            Copy-Item -LiteralPath $source -Destination $target -Force
            $version = (Get-Item -LiteralPath $target).VersionInfo.FileVersion
            Write-WastedInfo ("Deployed {0} (fileVersion={1}, sha256={2})" -f $name, $version, (Get-WastedFileSha256 -Path $target))
        }
    }

    Write-WastedStep "Bridge deployed to $scriptsDir. Restart the game (or reload SHVDN with its Insert-key default) and run bridge-smoke.ps1."
    exit 0
}
catch {
    Write-WastedError "deploy-bridge.ps1 failed: $($_.Exception.Message)"
    Write-WastedError $_.ScriptStackTrace
    exit 1
}
finally {
    Stop-WastedTranscript
}
