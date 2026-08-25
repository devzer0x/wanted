#Requires -Version 7.0
<#
.SYNOPSIS
    Start the WASTED chain: Steam (silent) -> GTA V Legacy -> harness -> OBS (replay buffer).
.DESCRIPTION
    Runs in the console session (registered at logon by server-setup.ps1; also invoked by
    watchdog.ps1 on recovery). Every step is logged; steps whose process already runs are
    skipped, so re-running is safe. The spawned Steam process exits before the game is up —
    per RESEARCH.md D10 the script polls for GTA5.exe, never waits on a PID.
.PARAMETER GameTimeoutS
    How long to poll for GTA5.exe after the Steam launch (Rockstar launcher sits mid-chain).
.EXAMPLE
    pwsh -File .\run.ps1
.EXAMPLE
    pwsh -File .\run.ps1 -GameTimeoutS 600 -SkipObs
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [int]$GameTimeoutS = 300,
    [string]$SteamExe = "${env:ProgramFiles(x86)}\Steam\steam.exe",
    [string]$ObsExe = "$env:ProgramFiles\obs-studio\bin\64bit\obs64.exe",
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe = '',
    [switch]$SkipHarness,
    [switch]$SkipObs,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
. $commonPath

Assert-WastedEnvironment -ScriptName 'run.ps1' -RequireWastedRoot -Force:$Force
Start-WastedTranscript -Name 'run' | Out-Null

$steamAppId = 271590   # GTA V Legacy (CONTRACTS.md platform baseline / RESEARCH.md D1)
$bridgeHealthUrl = 'http://127.0.0.1:7777/health'   # CONTRACTS.md §1

function Wait-WastedProcess {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][int]$TimeoutS,
        [int]$PollS = 3
    )
    $deadline = (Get-Date).AddSeconds($TimeoutS)
    while ((Get-Date) -lt $deadline) {
        $proc = Get-Process -Name $Name -ErrorAction SilentlyContinue
        if ($proc) { return $proc | Select-Object -First 1 }
        Start-Sleep -Seconds $PollS
    }
    return $null
}

function Get-HarnessProcess {
    # The harness runs as "python -m wasted_harness.main"; match on the command line.
    Get-CimInstance -ClassName Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'wasted_harness\.main' }
}

try {
    Write-WastedInfo "run.ps1 starting. Repo=$RepoDir GameTimeoutS=$GameTimeoutS"

    # --- Steam --------------------------------------------------------------------------------
    Write-WastedStep 'Step 1/5: Steam (silent).'
    if (Get-Process -Name steam -ErrorAction SilentlyContinue) {
        Write-WastedInfo 'Steam already running — skipping.'
    }
    else {
        if (-not (Test-Path -LiteralPath $SteamExe)) { throw "Steam not found at '$SteamExe' (pass -SteamExe)." }
        if ($PSCmdlet.ShouldProcess($SteamExe, 'start Steam -silent')) {
            Start-Process -FilePath $SteamExe -ArgumentList '-silent'
            if (-not (Wait-WastedProcess -Name steam -TimeoutS 60)) {
                throw 'Steam process did not appear within 60 s.'
            }
            Write-WastedInfo 'Steam is up; giving it 15 s to finish logging in.'
            Start-Sleep -Seconds 15
        }
    }

    # --- Game ---------------------------------------------------------------------------------
    Write-WastedStep "Step 2/5: GTA V Legacy (steam.exe -applaunch $steamAppId)."
    if (Get-Process -Name GTA5 -ErrorAction SilentlyContinue) {
        Write-WastedInfo 'GTA5.exe already running — skipping launch.'
    }
    else {
        if ($PSCmdlet.ShouldProcess("app $steamAppId", 'steam.exe -applaunch')) {
            # Launch chain: steam.exe -> PlayGTAV.exe -> Rockstar Games Launcher -> GTA5.exe.
            # BattlEye stays off via args.txt / commandline.txt deployed by fetch-shvdn.ps1.
            Start-Process -FilePath $SteamExe -ArgumentList '-applaunch', "$steamAppId"
            Write-WastedInfo "Polling for GTA5.exe (up to $GameTimeoutS s; the Rockstar launcher appears mid-chain)..."
            $game = Wait-WastedProcess -Name GTA5 -TimeoutS $GameTimeoutS
            if (-not $game) {
                throw "GTA5.exe did not appear within $GameTimeoutS s. Check the Rockstar launcher state on the console session (offline-mode hiccups are a known failure mode)."
            }
            Write-WastedInfo "GTA5.exe up (pid $($game.Id)). Allowing 20 s for the story load + Script Hook V init."
            Start-Sleep -Seconds 20
        }
    }

    # --- Harness ------------------------------------------------------------------------------
    Write-WastedStep 'Step 3/5: harness (python -m wasted_harness.main).'
    if ($SkipHarness) {
        Write-WastedInfo 'Skipped (-SkipHarness).'
    }
    elseif (Get-HarnessProcess) {
        Write-WastedInfo 'Harness already running — skipping.'
    }
    else {
        $python = Resolve-WastedPython -Explicit $PythonExe
        $harnessDir = Join-Path $RepoDir 'harness'
        if (-not (Test-Path -LiteralPath $harnessDir)) { throw "Harness directory '$harnessDir' not found (pass -RepoDir)." }
        if ($PSCmdlet.ShouldProcess("$python -m wasted_harness.main", 'start harness')) {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $logDir = Join-Path $script:WastedRoot 'logs'
            $proc = Start-Process -FilePath $python -ArgumentList '-m', 'wasted_harness.main' `
                -WorkingDirectory $harnessDir -PassThru `
                -RedirectStandardOutput (Join-Path $logDir "harness_$stamp.out.log") `
                -RedirectStandardError (Join-Path $logDir "harness_$stamp.err.log")
            Write-WastedInfo "Harness started (pid $($proc.Id)); stdout/stderr in $logDir\harness_$stamp.*.log"
        }
    }

    # --- OBS ----------------------------------------------------------------------------------
    Write-WastedStep 'Step 4/5: OBS with replay buffer.'
    if ($SkipObs) {
        Write-WastedInfo 'Skipped (-SkipObs).'
    }
    elseif (Get-Process -Name obs64 -ErrorAction SilentlyContinue) {
        Write-WastedInfo 'OBS already running — skipping.'
    }
    else {
        if (-not (Test-Path -LiteralPath $ObsExe)) { throw "OBS not found at '$ObsExe' (pass -ObsExe)." }
        if ($PSCmdlet.ShouldProcess($ObsExe, 'start OBS --startreplaybuffer')) {
            # OBS must start from its own bin directory or it fails to find locale/modules.
            # --disable-shutdown-check suppresses the safe-mode dialog after a hard kill,
            # which would otherwise block the unattended chain.
            Start-Process -FilePath $ObsExe -WorkingDirectory (Split-Path -Parent $ObsExe) `
                -ArgumentList '--startreplaybuffer', '--disable-shutdown-check'
            if (-not (Wait-WastedProcess -Name obs64 -TimeoutS 60)) {
                throw 'OBS process did not appear within 60 s.'
            }
            Write-WastedInfo 'OBS is up with the replay buffer requested at launch.'
        }
    }

    # --- Bridge probe (informational) ---------------------------------------------------------
    Write-WastedStep "Step 5/5: bridge probe ($bridgeHealthUrl)."
    try {
        $health = Invoke-RestMethod -Uri $bridgeHealthUrl -TimeoutSec 5
        Write-WastedInfo "Bridge health: $($health | ConvertTo-Json -Compress)"
    }
    catch {
        Write-WastedWarn "Bridge not answering yet ($($_.Exception.Message)) — SHVDN loads with the game; watchdog.ps1 owns recovery."
    }

    Write-WastedStep 'run.ps1 finished — chain is up.'
    exit 0
}
catch {
    Write-WastedError "run.ps1 failed: $($_.Exception.Message)"
    Write-WastedError $_.ScriptStackTrace
    exit 1
}
finally {
    Stop-WastedTranscript
}
