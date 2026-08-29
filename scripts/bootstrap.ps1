#Requires -Version 7.0
<#
.SYNOPSIS
    The one command to run on a freshly delivered WASTED game server. Sequences every other
    script, pauses where a human must type a password, and refuses to continue past a failure.
.DESCRIPTION
    Phase order (each phase is skipped when the state file says it already succeeded):

      1. preflight  - preflight.ps1, read-only go/no-go. Any hard blocker stops everything.
      2. setup      - server-setup.ps1: toolchain, Server features, Intel graphics INF, virtual
                      display driver, audio, autologon, policies, firewall, logon task.
      3. reboot     - gate: if Windows reports a pending reboot, stop and say so. Drivers and
                      Media Foundation are not live until the box has restarted.
      4. game       - HUMAN: Steam sign-in, install GTA V Legacy (271590), Rockstar/Social Club
                      sign-in, settings.xml at 1280x720 windowed-borderless. Verified by finding
                      GTA5.exe, not by taking anyone's word for it.
      5. shvdn      - fetch-shvdn.ps1: Script Hook V + the pinned SHVDN nightly.
      6. bridge     - deploy-bridge.ps1: the built WastedBridge.dll into <GameDir>\scripts.
      7. obs        - HUMAN: pick the (already installed) OBS profile, build the scene collection
                      under the same name, WebSocket, CABLE Output audio. Verified by finding
                      both the profile and the collection run.ps1 will launch OBS with.
      8. run        - Steam -> game -> harness -> OBS, always in the console session. From the
                      console this calls run.ps1 directly; from RDP it starts the WASTED-Run
                      scheduled task (Interactive principal => console session) and waits for
                      GTA5.exe, because run.ps1 correctly refuses to launch the game into an
                      RDP session.
      9. smoke      - bridge-smoke.ps1: every CONTRACTS.md §1 endpoint, output goes in STATUS.md.

    The game phase deliberately runs BEFORE fetch-shvdn: fetch-shvdn.ps1 needs GTA5.exe to
    already exist, so the human's Steam and Rockstar sign-ins have to happen first.

    Resumable: a state file at <WastedRoot>\state\bootstrap-state.json records which phases
    succeeded. Re-running skips them. -StartAt jumps to a phase; -Rerun ignores the state file.
    A failed critical phase always stops the run with a nonzero exit code and a printed
    remediation - nothing is ever silently skipped past.

    Human phases have exactly three outcomes and they are kept distinct end to end: DONE (only
    counts once the phase's own verification passes; the phase is recorded complete), SKIP (the
    operator accepts the risk - the phase is NOT recorded, the run continues, the summary names
    it and the exit code is nonzero), ABORT (stops the run). A skipped phase therefore reappears
    as outstanding on the next pass instead of looking finished.
.PARAMETER AdminIP
    Passed to server-setup.ps1. Required only when the setup phase is actually going to run.
.PARAMETER GameDir
    GTA V Legacy install folder. Auto-detected from the usual Steam locations when omitted.
.PARAMETER ObsProfileName
    OBS profile and scene-collection name. server-setup.ps1 installs the profile under this
    name and run.ps1 launches OBS with --profile/--collection set to it; the obs phase verifies
    both exist. Change it here only together with those two scripts.
.PARAMETER StartAt
    Begin at this phase; earlier phases are marked skipped for this run.
.PARAMETER Rerun
    Ignore the state file and run every phase from -StartAt onwards.
.PARAMETER NonInteractive
    Do not wait at human-input phases. The checklist is printed and the run stops with exit
    code 2 so a person can do the work and resume with -StartAt.
.EXAMPLE
    pwsh -File .\bootstrap.ps1 -AdminIP 203.0.113.7
.EXAMPLE
    pwsh -File .\bootstrap.ps1 -AdminIP 203.0.113.7 -StartAt shvdn
.EXAMPLE
    pwsh -File .\bootstrap.ps1 -AdminIP 203.0.113.7 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$AdminIP = '',
    [string]$GameDir = '',
    [string]$WastedRoot = 'C:\wasted',
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [string]$ObsProfileName = 'WASTED',
    [ValidateSet('preflight', 'setup', 'reboot', 'game', 'shvdn', 'bridge', 'obs', 'run', 'smoke')]
    [string]$StartAt = 'preflight',
    [int]$RunTimeoutS = 420,
    [switch]$Rerun,
    [switch]$NonInteractive,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
$script:WastedRoot = $WastedRoot
. $commonPath

Assert-WastedEnvironment -ScriptName 'bootstrap.ps1' -RequireServerSku -RequireElevation -Force:$Force

New-Item -ItemType Directory -Path (Join-Path $WastedRoot 'logs'), (Join-Path $WastedRoot 'state') `
    -Force -WhatIf:$false -Confirm:$false | Out-Null
Start-WastedTranscript -Name 'bootstrap' | Out-Null

$statePath = Join-Path (Join-Path $WastedRoot 'state') 'bootstrap-state.json'
$pwshPath = (Get-Process -Id $PID).Path

# --- state -------------------------------------------------------------------------------------

function Get-BootstrapState {
    if (-not (Test-Path -LiteralPath $statePath)) {
        return [ordered]@{ version = 1; updated_at = ''; completed = @{} }
    }
    try {
        $raw = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        $completed = @{}
        if ($raw.PSObject.Properties['completed']) {
            foreach ($prop in $raw.completed.PSObject.Properties) {
                # Normalise: ConvertFrom-Json turns an ISO-8601 string back into a local [datetime],
                # so a reloaded entry would otherwise have a different shape (and a different
                # printed timezone) from one Set-PhaseComplete just created.
                $entry = $prop.Value
                $at = ''
                if ($null -ne $entry -and $entry.PSObject.Properties['at'] -and $null -ne $entry.at) {
                    if ($entry.at -is [datetime]) { $at = $entry.at.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
                    else { $at = [string]$entry.at }
                }
                $entryExit = 0
                if ($null -ne $entry -and $entry.PSObject.Properties['exit_code'] -and $null -ne $entry.exit_code) {
                    $entryExit = [int]$entry.exit_code
                }
                $completed[$prop.Name] = [ordered]@{ at = $at; exit_code = $entryExit }
            }
        }
        return [ordered]@{ version = 1; updated_at = [string]$raw.updated_at; completed = $completed }
    }
    catch {
        Write-WastedWarn "Could not read $statePath ($($_.Exception.Message)) — treating every phase as not yet done."
        return [ordered]@{ version = 1; updated_at = ''; completed = @{} }
    }
}

function Save-BootstrapState {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$State)
    $State.updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $State | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $statePath -Encoding utf8 -WhatIf:$false -Confirm:$false
}

function Set-PhaseComplete {
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary]$State,
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][int]$ExitCode
    )
    $State.completed[$Phase] = [ordered]@{
        at        = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        exit_code = $ExitCode
    }
    Save-BootstrapState -State $State
}

# --- helpers -----------------------------------------------------------------------------------

function Invoke-ChildScript {
    <#
    Run one of the sibling scripts in its own pwsh process so its transcript, exit code and
    interactive prompts all behave normally. Returns the exit code.
    #>
    param(
        [Parameter(Mandatory)][string]$ScriptName,
        [string[]]$ScriptArgs = @()
    )
    $path = Join-Path $PSScriptRoot $ScriptName
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing sibling script '$path'." }
    $allArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $path) + $ScriptArgs
    if ($WhatIfPreference) {
        # Only the state-changing scripts declare SupportsShouldProcess; preflight.ps1 and
        # bridge-smoke.ps1 are read-only and would reject an unknown -WhatIf outright.
        $childCommand = Get-Command -Name $path -CommandType ExternalScript -ErrorAction SilentlyContinue
        if ($childCommand -and $childCommand.Parameters.ContainsKey('WhatIf')) {
            $allArgs += '-WhatIf'
        }
        else {
            Write-WastedInfo "$ScriptName does not support -WhatIf (it changes nothing); running it for real."
        }
    }
    Write-WastedInfo ("Running: {0} {1}" -f $pwshPath, ($allArgs -join ' '))
    # The child's output goes straight to the host: leaving it in the pipeline would make this
    # function return the whole transcript alongside the exit code.
    & $pwshPath @allArgs 2>&1 | ForEach-Object { Write-Host $_ }
    $code = $LASTEXITCODE
    Write-WastedInfo "$ScriptName exited $code."
    return $code
}

function Resolve-GameDir {
    if ($GameDir) { return $GameDir }
    $candidates = @(
        "${env:ProgramFiles(x86)}\Steam\steamapps\common\Grand Theft Auto V",
        "$env:ProgramFiles\Steam\steamapps\common\Grand Theft Auto V",
        'D:\Steam\steamapps\common\Grand Theft Auto V',
        'D:\SteamLibrary\steamapps\common\Grand Theft Auto V'
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'GTA5.exe')) { return $candidate }
    }
    return ''
}

function Test-PendingReboot {
    $reasons = @()
    if (Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') {
        $reasons += 'Component Based Servicing\RebootPending'
    }
    if (Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') {
        $reasons += 'WindowsUpdate\RebootRequired'
    }
    $sm = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' `
        -Name PendingFileRenameOperations -ErrorAction SilentlyContinue
    if ($null -ne $sm -and $sm.PSObject.Properties['PendingFileRenameOperations']) {
        $reasons += 'Session Manager\PendingFileRenameOperations'
    }
    return $reasons
}

function Show-Checklist {
    param(
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string[]]$Steps
    )
    Write-Host ''
    Write-Host ('=' * 90) -ForegroundColor Cyan
    Write-Host "  HUMAN ACTION REQUIRED — $Title" -ForegroundColor Cyan
    Write-Host ('=' * 90) -ForegroundColor Cyan
    $i = 0
    foreach ($step in $Steps) {
        $i++
        Write-Host ("  {0,2}. {1}" -f $i, $step) -ForegroundColor White
    }
    Write-Host ('=' * 90) -ForegroundColor Cyan
    Write-Host ''
}

function Wait-ForHuman {
    <#
    Block until the operator says the manual work is done, then run $Verify.

    Returns one of three DISTINCT outcomes, never a bare boolean — a boolean forced SKIP to
    share a value with either "verified" (so callers recorded a skipped phase as complete,
    contradicting the message printed at the prompt) or "deferred" (so an accepted risk stopped
    the run like a failure):
      'verified' - $Verify actually passed. The only outcome that may be recorded complete.
      'skipped'  - operator accepted the risk. NOT complete: the caller must move on without
                   recording anything, and the run summary names the phase.
      'deferred' - -NonInteractive: nothing was done, the operator has to come back.
    A claimed-but-unverified DONE never counts; the prompt simply repeats.
    #>
    param(
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][scriptblock]$Verify
    )
    if ($NonInteractive) {
        Write-WastedWarn "Non-interactive run: stopping at the '$Phase' phase. Do the checklist above, then resume with: pwsh -File .\bootstrap.ps1 -StartAt $Phase"
        return 'deferred'
    }
    while ($true) {
        $answer = Read-Host "Type DONE when the checklist above is complete, SKIP to accept the risk and move on, or ABORT to stop"
        switch ($answer.Trim().ToUpperInvariant()) {
            'ABORT' { throw "Operator aborted at the '$Phase' phase." }
            'SKIP' {
                Write-WastedWarn "Operator chose SKIP at '$Phase' — the phase is NOT recorded as complete, it stays outstanding in the state file, and downstream phases may fail."
                return 'skipped'
            }
            'DONE' {
                $result = & $Verify
                if ($result.Ok) {
                    Write-WastedInfo "Verified: $($result.Detail)"
                    return 'verified'
                }
                Write-WastedError "Not verified yet: $($result.Detail)"
            }
            default { Write-Host 'Please type DONE, SKIP or ABORT.' -ForegroundColor Yellow }
        }
    }
}

# --- phase definitions ---------------------------------------------------------------------------

$phaseOrder = @('preflight', 'setup', 'reboot', 'game', 'shvdn', 'bridge', 'obs', 'run', 'smoke')

$phaseTitles = @{
    preflight = 'read-only go/no-go diagnostic'
    setup     = 'server-setup.ps1 (toolchain, drivers, display, hardening)'
    reboot    = 'pending-reboot gate'
    game      = 'HUMAN: Steam + Rockstar sign-in and GTA V Legacy install'
    shvdn     = 'Script Hook V + ScriptHookVDotNet nightly'
    bridge    = 'deploy the built bridge into the game'
    obs       = 'HUMAN: OBS profile selection, scene collection, WebSocket, audio'
    run       = 'start the chain (Steam -> game -> harness -> OBS)'
    smoke     = 'bridge smoke test against CONTRACTS.md §1'
}

# --- main ----------------------------------------------------------------------------------------

$exitCode = 0
$ranPhases = [System.Collections.Generic.List[string]]::new()
$skippedPhases = [System.Collections.Generic.List[string]]::new()
# Phases the operator waved through with SKIP. Deliberately separate from $skippedPhases (which
# is "already done / before -StartAt"): these are NOT complete and must be reported as such.
$operatorSkipped = [System.Collections.Generic.List[string]]::new()

try {
    Write-WastedStep "bootstrap.ps1 — WASTED delivery-day sequence. Root=$WastedRoot Repo=$RepoDir StartAt=$StartAt"
    $state = Get-BootstrapState
    if ($Rerun) { Write-WastedInfo '-Rerun given: the state file is ignored for this run.' }

    $startIndex = [array]::IndexOf($phaseOrder, $StartAt)
    $resolvedGameDir = Resolve-GameDir

    # Labelled so a human-input phase can stop the whole run: a bare `break` inside the switch
    # below would only leave the switch and silently continue to the next phase.
    :phaseLoop foreach ($phase in $phaseOrder) {
        $index = [array]::IndexOf($phaseOrder, $phase)
        $title = $phaseTitles[$phase]

        if ($index -lt $startIndex) {
            Write-WastedInfo "[$phase] skipped (before -StartAt $StartAt)."
            $skippedPhases.Add($phase)
            continue
        }
        if (-not $Rerun -and $state.completed.ContainsKey($phase)) {
            Write-WastedInfo "[$phase] already completed at $($state.completed[$phase].at) — skipping (use -Rerun to force)."
            $skippedPhases.Add($phase)
            continue
        }

        Write-WastedStep "[$phase] $title"
        $ranPhases.Add($phase)

        switch ($phase) {

            'preflight' {
                $jsonOut = Join-Path (Join-Path $WastedRoot 'state') 'preflight.json'
                $code = Invoke-ChildScript -ScriptName 'preflight.ps1' -ScriptArgs @('-JsonOut', $jsonOut)
                if ($code -ne 0) {
                    throw "preflight.ps1 reported hard blockers (exit $code). Fix every FAIL row it printed — they are listed with the exact next action — then re-run bootstrap.ps1. Full results: $jsonOut"
                }
                Set-PhaseComplete -State $state -Phase $phase -ExitCode $code
            }

            'setup' {
                if (-not $AdminIP) {
                    throw 'The setup phase needs -AdminIP (the IP or CIDR allowed to reach RDP and SSH). A wrong value locks you out of the server, so bootstrap.ps1 will not guess one.'
                }
                $setupArgs = @('-AdminIP', $AdminIP, '-WastedRoot', $WastedRoot, '-RepoDir', $RepoDir)
                if ($Force) { $setupArgs += '-Force' }
                $code = Invoke-ChildScript -ScriptName 'server-setup.ps1' -ScriptArgs $setupArgs
                if ($code -ne 0) {
                    throw "server-setup.ps1 failed (exit $code). Read the transcript in $WastedRoot\logs, fix the failing step, then re-run bootstrap.ps1 (completed phases are skipped automatically)."
                }
                Set-PhaseComplete -State $state -Phase $phase -ExitCode $code
            }

            'reboot' {
                $reasons = Test-PendingReboot
                if ($reasons.Count -gt 0) {
                    Show-Checklist -Title 'reboot the server' -Steps @(
                        "Windows reports a pending reboot: $($reasons -join '; ')."
                        'The Intel graphics driver, the virtual display driver, VB-CABLE and Media Foundation are not live until the box restarts.'
                        'Reboot now:  Restart-Computer -Force'
                        'Autologon brings the console session back on its own; wait ~2 minutes, then RDP in again.'
                        'Resume with:  pwsh -File .\bootstrap.ps1 -StartAt reboot'
                    )
                    if ($WhatIfPreference) {
                        Write-WastedWarn 'What if: a reboot is pending; a real run would stop here until the server has restarted.'
                    }
                    elseif ($NonInteractive) {
                        throw 'A reboot is pending. Restart the server, then resume with: pwsh -File .\bootstrap.ps1 -StartAt reboot'
                    }
                    else {
                        $rebooted = Wait-ForHuman -Phase 'reboot' -Verify {
                            $left = Test-PendingReboot
                            if ($left.Count -eq 0) { return @{ Ok = $true; Detail = 'no pending reboot flags remain.' } }
                            return @{ Ok = $false; Detail = "still pending: $($left -join '; ')" }
                        }
                        if ($rebooted -eq 'deferred') { throw 'Reboot gate not satisfied.' }
                        if ($rebooted -eq 'skipped') {
                            # The reboot flags are still set. Recording the gate as satisfied
                            # here would be a lie, and the drivers really are not live yet.
                            Write-WastedWarn "[reboot] SKIPPED by the operator with a reboot still pending ($($reasons -join '; ')). The Intel graphics driver, the virtual display driver, VB-CABLE and Media Foundation are NOT live; expect the display and audio phases to fail."
                            $operatorSkipped.Add($phase)
                            continue phaseLoop
                        }
                    }
                }
                else {
                    Write-WastedInfo 'No pending reboot flags.'
                }
                Set-PhaseComplete -State $state -Phase $phase -ExitCode 0
            }

            'game' {
                Show-Checklist -Title 'Steam + Rockstar sign-in and the game install' -Steps @(
                    'Do all of this in an RDP session on the CONSOLE desktop — nobody but you ever types these passwords.'
                    'Open Steam, sign in (including Steam Guard). Let it finish updating.'
                    'Install GTA V **Legacy**: steam://install/271590 . Do NOT install Enhanced (3240220): it needs a 4 GB DirectX 12 GPU, an SSD and BattlEye, none of which fit this box.'
                    'Launch the game once from Steam. The first launch installs the Rockstar Games Launcher and Social Club — sign in there and link the account. Run the launcher as Administrator if it misbehaves.'
                    'Let the game finish its one-time graphics auto-detect, then quit it.'
                    'Edit Documents\Rockstar Games\GTA V\settings.xml : ScreenWidth value="1280", ScreenHeight value="720", Windowed value="2" (2 = borderless). Never exclusive fullscreen — it is impossible over Terminal Server and fragile on a virtual display.'
                    'Confirm the game runs at 1280x720 borderless in the console session before continuing.'
                    'run.ps1 -EnforceDisplaySettings will re-apply those three values for you if you would rather not hand-edit the XML.'
                )
                $ok = Wait-ForHuman -Phase 'game' -Verify {
                    $dir = Resolve-GameDir
                    if ($dir) { return @{ Ok = $true; Detail = "GTA5.exe found at $dir" } }
                    return @{ Ok = $false; Detail = 'GTA5.exe not found in any known Steam library path. Pass -GameDir if the game is installed somewhere unusual.' }
                }
                if ($ok -eq 'deferred') { $exitCode = 2; break phaseLoop }
                if ($ok -eq 'skipped') {
                    Write-WastedWarn '[game] SKIPPED by the operator — GTA5.exe was never found, so the phase is NOT complete. shvdn/bridge/run/smoke all need the game installed and will fail next.'
                    $operatorSkipped.Add($phase)
                    continue phaseLoop
                }
                $resolvedGameDir = Resolve-GameDir
                if (-not $resolvedGameDir) { throw 'Game phase accepted but GTA5.exe is still not findable — pass -GameDir explicitly.' }
                Set-PhaseComplete -State $state -Phase $phase -ExitCode 0
            }

            'shvdn' {
                if (-not $resolvedGameDir) { $resolvedGameDir = Resolve-GameDir }
                if (-not $resolvedGameDir) {
                    throw 'Cannot run fetch-shvdn.ps1: GTA5.exe was not found. Complete the game phase first, or pass -GameDir.'
                }
                $code = Invoke-ChildScript -ScriptName 'fetch-shvdn.ps1' -ScriptArgs @('-GameDir', $resolvedGameDir)
                if ($code -ne 0) {
                    throw "fetch-shvdn.ps1 failed (exit $code). Its most common failure is the dev-c.com Referer gate: download the current ScriptHookV_*.zip in a browser into $WastedRoot\downloads and re-run bootstrap.ps1 -StartAt shvdn."
                }
                Set-PhaseComplete -State $state -Phase $phase -ExitCode $code
            }

            'bridge' {
                $buildDir = Join-Path $RepoDir 'bridge\bin\Release\net48'
                if (-not (Test-Path -LiteralPath (Join-Path $buildDir 'WastedBridge.dll'))) {
                    throw "The bridge is not built: '$buildDir\WastedBridge.dll' is missing. Build it first (dotnet build bridge -c Release from $RepoDir), then re-run bootstrap.ps1 -StartAt bridge."
                }
                # -BuildDir is passed explicitly: deploy-bridge.ps1 otherwise derives it from its
                # own location, which is not necessarily the -RepoDir this run was given.
                $code = Invoke-ChildScript -ScriptName 'deploy-bridge.ps1' -ScriptArgs @('-GameDir', $resolvedGameDir, '-BuildDir', $buildDir)
                if ($code -ne 0) { throw "deploy-bridge.ps1 failed (exit $code)." }
                Set-PhaseComplete -State $state -Phase $phase -ExitCode $code
            }

            'obs' {
                Show-Checklist -Title 'OBS profile, scene, WebSocket and audio' -Steps @(
                    "Start OBS and pick Profile -> $ObsProfileName. server-setup.ps1 step 15 already installed it at %APPDATA%\obs-studio\basic\profiles\$ObsProfileName\basic.ini (720p30 x264, replay buffer 30 s / 1024 MB); if it is not in the list, re-run server-setup.ps1 as the account that runs the stream. Rationale for every value: scripts\obs-profile\README.md"
                    "Scene collection: create one and name it EXACTLY '$ObsProfileName'. run.ps1 launches OBS with --collection `"$ObsProfileName`", and OBS silently keeps the previous scenes when that name does not exist."
                    'Scene: add a Game Capture source (mode "Capture specific window", window = GTA5.exe) as the primary. Game Capture hooks the game''s Direct3D 11 swapchain, so it does not depend on Desktop Duplication or on the compositor. Uncheck "Capture Cursor".'
                    'Fallback source, disabled by default: Window Capture with the Windows Graphics Capture method. Do NOT rely on Display Capture — it uses Desktop Duplication and dies with DXGI_ERROR_SESSION_DISCONNECTED whenever anyone attaches or detaches RDP.'
                    'Audio: add an Audio Output Capture source and select "CABLE Output (VB-Audio Virtual Cable)" explicitly. Never "Default": RDP audio redirection hijacks Default.'
                    'Add a Browser source pointing at http://127.0.0.1:7788/overlay (CONTRACTS.md §6), 1280x720, transparent background.'
                    'Tools -> WebSocket Server Settings: enable the server on 127.0.0.1:4455, set a password, and put that password in the harness .env (never in this repo).'
                    'Settings -> Stream: enter the stream key by hand. It lives only in OBS; no script here ever touches it.'
                    'Then quit OBS, so run.ps1 starts it fresh on that profile and collection.'
                )
                # Both names are checked, because both are what run.ps1 passes on the command
                # line and OBS ignores either one silently when it does not resolve.
                $obsName = $ObsProfileName
                $ok = Wait-ForHuman -Phase 'obs' -Verify {
                    $obs = "$env:ProgramFiles\obs-studio\bin\64bit\obs64.exe"
                    if (-not (Test-Path -LiteralPath $obs)) {
                        return @{ Ok = $false; Detail = "OBS not found at $obs — install it (server-setup.ps1 does this via winget)." }
                    }
                    if (-not $env:APPDATA) {
                        return @{ Ok = $false; Detail = 'APPDATA is not set, so the OBS config directory cannot be located.' }
                    }
                    # OBS matches --profile against [General] Name in each profile's basic.ini,
                    # not against the directory name (OBS 32.2.2 OBSBasic_Profiles.cpp).
                    $profilesRoot = Join-Path $env:APPDATA 'obs-studio\basic\profiles'
                    $profileIni = ''
                    foreach ($dir in @(Get-ChildItem -LiteralPath $profilesRoot -Directory -ErrorAction SilentlyContinue)) {
                        $ini = Join-Path $dir.FullName 'basic.ini'
                        if (-not (Test-Path -LiteralPath $ini)) { continue }
                        $line = Select-String -LiteralPath $ini -Pattern '^\s*Name\s*=\s*(.+?)\s*$' | Select-Object -First 1
                        if ($line -and $line.Matches[0].Groups[1].Value -eq $obsName) { $profileIni = $ini; break }
                    }
                    if (-not $profileIni) {
                        return @{ Ok = $false; Detail = "No OBS profile named '$obsName' under $profilesRoot (OBS reads the name from [General] Name, not the folder name). Re-run server-setup.ps1 as the streaming account." }
                    }
                    # --collection is matched against the "name" field inside each scenes JSON.
                    $scenesRoot = Join-Path $env:APPDATA 'obs-studio\basic\scenes'
                    $collectionFile = ''
                    foreach ($file in @(Get-ChildItem -LiteralPath $scenesRoot -Filter '*.json' -ErrorAction SilentlyContinue)) {
                        try { $data = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json } catch { continue }
                        if ($data.PSObject.Properties['name'] -and [string]$data.name -eq $obsName) { $collectionFile = $file.FullName; break }
                    }
                    if (-not $collectionFile) {
                        return @{ Ok = $false; Detail = "OBS profile '$obsName' exists, but no scene collection is named '$obsName' under $scenesRoot. Create the collection in OBS with exactly that name (run.ps1 passes --collection `"$obsName`")." }
                    }
                    return @{ Ok = $true; Detail = "OBS present, profile '$obsName' at $profileIni, scene collection '$obsName' at $collectionFile." }
                }
                if ($ok -eq 'deferred') { $exitCode = 2; break phaseLoop }
                if ($ok -eq 'skipped') {
                    Write-WastedWarn "[obs] SKIPPED by the operator — the phase is NOT complete. run.ps1 launches OBS with --profile '$ObsProfileName' --collection '$ObsProfileName'; OBS ignores names it cannot find and silently keeps its previous profile/scenes, so the stream can come up at the wrong settings or with a black/silent scene."
                    $operatorSkipped.Add($phase)
                    continue phaseLoop
                }
                Set-PhaseComplete -State $state -Phase $phase -ExitCode 0
            }

            'run' {
                # The chain must start in the CONSOLE session. bootstrap.ps1 is normally driven
                # from RDP, and run.ps1 rightly refuses to launch the game there, so from RDP we
                # start it through the WASTED-Run scheduled task instead: that task is registered
                # with an Interactive principal for the autologon user, so it lands in the
                # console session rather than this one.
                if ($env:SESSIONNAME -eq 'Console') {
                    $code = Invoke-ChildScript -ScriptName 'run.ps1' -ScriptArgs @('-RepoDir', $RepoDir)
                    if ($code -ne 0) {
                        throw "run.ps1 failed (exit $code). Check $WastedRoot\logs for its transcript; the usual causes are the Rockstar launcher sitting mid-chain and the game having no display target."
                    }
                    Set-PhaseComplete -State $state -Phase $phase -ExitCode $code
                }
                else {
                    Write-WastedInfo "This is session '$env:SESSIONNAME', not Console. Starting the chain through the WASTED-Run scheduled task so it lands in the console session."
                    $task = Get-ScheduledTask -TaskName 'WASTED-Run' -ErrorAction SilentlyContinue
                    if (-not $task) {
                        throw "The WASTED-Run scheduled task does not exist, so the chain cannot be started in the console session from here. Either re-run the setup phase (it registers the task), or leave RDP with detach-rdp.ps1 and run run.ps1 on the console yourself."
                    }
                    if ($PSCmdlet.ShouldProcess('WASTED-Run', 'Start-ScheduledTask, then wait for GTA5.exe')) {
                        Start-ScheduledTask -TaskName 'WASTED-Run'
                        Write-WastedInfo "Waiting up to $RunTimeoutS s for GTA5.exe to appear (Steam, then the Rockstar launcher, then the game)."
                        $deadline = (Get-Date).AddSeconds($RunTimeoutS)
                        $game = $null
                        while ((Get-Date) -lt $deadline) {
                            $game = Get-Process -Name GTA5 -ErrorAction SilentlyContinue | Select-Object -First 1
                            if ($game) { break }
                            Start-Sleep -Seconds 5
                        }
                        if (-not $game) {
                            throw "GTA5.exe did not appear within $RunTimeoutS s of starting WASTED-Run. Read the run.ps1 transcript in $WastedRoot\logs: it records the console session's display mode, which is the usual culprit on this machine, as well as the Rockstar launcher state."
                        }
                        Write-WastedInfo "GTA5.exe is up (pid $($game.Id)) in the console session."
                    }
                    Set-PhaseComplete -State $state -Phase $phase -ExitCode 0
                }
            }

            'smoke' {
                Write-WastedInfo 'Giving the game and Script Hook V 30 s to finish loading the bridge before probing.'
                Start-Sleep -Seconds 30
                $code = Invoke-ChildScript -ScriptName 'bridge-smoke.ps1'
                if ($code -ne 0) {
                    throw "bridge-smoke.ps1 reported failures (exit $code). Paste its table into docs/STATUS.md as-is; a failing smoke test is a real finding, not a formality."
                }
                Set-PhaseComplete -State $state -Phase $phase -ExitCode $code
                Write-WastedStep 'Smoke test passed. Paste its markdown table into docs/STATUS.md.'
            }
        }
    }

    Write-Host ''
    Write-WastedStep 'bootstrap.ps1 summary'
    $ranList = (($ranPhases | Where-Object { $_ }) -join ', ')
    $skipList = (($skippedPhases | Where-Object { $_ }) -join ', ')
    Write-WastedInfo ("phases run this pass         : " + $(if ($ranList) { $ranList } else { '(none)' }))
    Write-WastedInfo ("skipped, already done/StartAt: " + $(if ($skipList) { $skipList } else { '(none)' }))
    if ($operatorSkipped.Count -gt 0) {
        Write-WastedWarn ("SKIPPED BY OPERATOR (risk accepted, NOT complete): " + (($operatorSkipped) -join ', '))
        foreach ($sk in $operatorSkipped) {
            Write-WastedWarn ("  - $sk was waved through with SKIP. It is not in the state file, so it will run again on the next pass: pwsh -File .\bootstrap.ps1 -StartAt $sk")
        }
        if ($exitCode -eq 0) { $exitCode = 2 }
    }
    Write-WastedInfo "state file                   : $statePath"
    if ($exitCode -eq 0) {
        $done = @($phaseOrder | Where-Object { $state.completed.ContainsKey($_) })
        $remaining = @($phaseOrder | Where-Object { -not $state.completed.ContainsKey($_) })
        Write-WastedInfo ("completed to date            : " + ($done -join ', '))
        if ($remaining.Count -gt 0) {
            Write-WastedWarn ("still outstanding            : " + ($remaining -join ', ') + "  -> pwsh -File .\bootstrap.ps1 -StartAt $($remaining[0])")
            $exitCode = 2
        }
        else {
            Write-WastedStep 'Every phase is complete. Start watchdog.ps1 alongside the stream, then leave RDP with detach-rdp.ps1.'
        }
    }
    else {
        $remaining = @($phaseOrder | Where-Object { -not $state.completed.ContainsKey($_) })
        if ($remaining.Count -gt 0) {
            Write-WastedWarn ("still outstanding            : " + ($remaining -join ', ') + "  -> pwsh -File .\bootstrap.ps1 -StartAt $($remaining[0])")
        }
    }
}
catch {
    Write-WastedError "bootstrap.ps1 stopped: $($_.Exception.Message)"
    Write-WastedError 'Nothing after this point was run. Fix the cause above and re-run bootstrap.ps1 — completed phases are skipped automatically.'
    $exitCode = 1
}
finally {
    Stop-WastedTranscript
}

exit $exitCode
