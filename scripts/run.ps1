#Requires -Version 7.0
<#
.SYNOPSIS
    Start the WASTED chain: Steam (silent) -> GTA V Legacy -> harness -> OBS (replay buffer).
.DESCRIPTION
    Runs in the console session (registered at logon by server-setup.ps1; also invoked by
    watchdog.ps1 on recovery). Every step is logged; steps whose process already runs are
    skipped, so re-running is safe. The spawned Steam process exits before the game is up —
    per RESEARCH.md D10 the script polls for GTA5.exe, never waits on a PID.

    Two checks run before anything is launched, because on this server (Intel UHD 770 iGPU,
    no monitor, no HDMI emulator) both are real failure modes rather than formalities:

      * Session. The game and OBS must live in the CONSOLE session. A Remote Desktop session
        gets the Microsoft Basic Render Driver by default, cannot do exclusive fullscreen at
        all (DXGI_ERROR_NOT_CURRENTLY_AVAILABLE over Terminal Server), and takes the desktop
        with it when you disconnect. Use -AllowRdpSession only for a deliberate experiment.
      * Display target. With no physical monitor the desktop only exists because an indirect
        display driver supplies one. If no adapter reports a >= 1280x720 mode, the game has
        nowhere to present and OBS capture will be black, so we say so instead of launching
        into a broken state.
.PARAMETER GameTimeoutS
    How long to poll for GTA5.exe after the Steam launch (Rockstar launcher sits mid-chain).
.PARAMETER GameArgs
    Extra arguments appended to `steam.exe -applaunch 271590`. Default is the CONTRACTS.md
    platform baseline plus the windowed-borderless 720p geometry.
.PARAMETER EnforceDisplaySettings
    Rewrite ScreenWidth / ScreenHeight / Windowed in the Rockstar settings.xml (backed up
    first) so the game starts at 1280x720 borderless. Without this switch the values are only
    reported.
.PARAMETER ObsProfile
    OBS profile name passed as `--profile`. Installed by server-setup.ps1 from
    scripts\obs-profile\basic.ini; OBS matches it against the profile's [General] Name.
.PARAMETER ObsSceneCollection
    OBS scene collection name passed as `--collection`. Built by hand (obs-profile\README.md)
    and must be named exactly this: OBS matches it against the "name" field inside
    %APPDATA%\obs-studio\basic\scenes\*.json.
.PARAMETER AllowRdpSession
    Launch even though this is not the console session. Diagnostics only.
.EXAMPLE
    pwsh -File .\run.ps1
.EXAMPLE
    pwsh -File .\run.ps1 -GameTimeoutS 600 -SkipObs
.EXAMPLE
    pwsh -File .\run.ps1 -EnforceDisplaySettings
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [int]$GameTimeoutS = 300,
    [string]$SteamExe = "${env:ProgramFiles(x86)}\Steam\steam.exe",
    [string]$ObsExe = "$env:ProgramFiles\obs-studio\bin\64bit\obs64.exe",
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe = '',
    [string[]]$GameArgs = @('-nobattleye', '-windowed', '-borderless', '-width', '1280', '-height', '720'),
    [int]$ScreenWidth = 1280,
    [int]$ScreenHeight = 720,
    [string]$ObsProfile = 'WASTED',
    [string]$ObsSceneCollection = 'WASTED',
    [switch]$EnforceDisplaySettings,
    [switch]$AllowRdpSession,
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
$settingsXml = Join-Path ([System.Environment]::GetFolderPath('MyDocuments')) 'Rockstar Games\GTA V\settings.xml'

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

function Get-ActiveDisplayModes {
    $modes = [System.Collections.Generic.List[pscustomobject]]::new()
    foreach ($vc in @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction SilentlyContinue)) {
        $w = [int]($vc.CurrentHorizontalResolution ?? 0)
        $h = [int]($vc.CurrentVerticalResolution ?? 0)
        if ($w -gt 0 -and $h -gt 0) {
            $modes.Add([pscustomobject]@{ Name = [string]$vc.Name; Width = $w; Height = $h })
        }
    }
    return $modes
}

function Test-ObsProfileInstalled {
    <#
    Warn (loudly, with the fix) when the OBS profile --profile names does not exist.
    Layout verified against OBS Studio 32.2.2: profiles live in one directory each under
    <app config>\obs-studio\basic\profiles (%APPDATA% on Windows), the settings file is
    basic.ini, and the profile's name is its [General] Name key — not the directory name
    (frontend/OBSApp.cpp, frontend/widgets/OBSBasic_Profiles.cpp).
    #>
    param([Parameter(Mandatory)][string]$ProfileName)
    if (-not $env:APPDATA) { Write-WastedWarn 'APPDATA is not set — cannot check the OBS profile.'; return }
    $profilesRoot = Join-Path $env:APPDATA 'obs-studio\basic\profiles'
    $found = $false
    if (Test-Path -LiteralPath $profilesRoot) {
        foreach ($dir in @(Get-ChildItem -LiteralPath $profilesRoot -Directory -ErrorAction SilentlyContinue)) {
            $ini = Join-Path $dir.FullName 'basic.ini'
            if (-not (Test-Path -LiteralPath $ini)) { continue }
            $nameLine = Select-String -LiteralPath $ini -Pattern '^\s*Name\s*=\s*(.+?)\s*$' | Select-Object -First 1
            if ($nameLine -and $nameLine.Matches[0].Groups[1].Value -eq $ProfileName) {
                Write-WastedInfo "OBS profile '$ProfileName' found at $ini."
                $found = $true
                break
            }
        }
    }
    if (-not $found) {
        Write-WastedWarn "No OBS profile named '$ProfileName' under $profilesRoot. OBS ignores an unknown --profile and silently keeps the last-used one, so the stream would run on whatever settings that profile has. Fix: re-run server-setup.ps1 (step 15 installs it), or copy scripts\obs-profile\basic.ini to $profilesRoot\$ProfileName\basic.ini."
    }
}

function Test-ObsSceneCollectionInstalled {
    <#
    Same for --collection. OBS stores each collection as one JSON under
    <app config>\obs-studio\basic\scenes and matches --collection against the "name" field
    inside the file, not the file name (frontend/widgets/OBSBasic_SceneCollections.cpp).
    #>
    param([Parameter(Mandatory)][string]$CollectionName)
    if (-not $env:APPDATA) { return }
    $scenesRoot = Join-Path $env:APPDATA 'obs-studio\basic\scenes'
    $found = $false
    if (Test-Path -LiteralPath $scenesRoot) {
        foreach ($file in @(Get-ChildItem -LiteralPath $scenesRoot -Filter '*.json' -ErrorAction SilentlyContinue)) {
            try {
                $data = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            }
            catch {
                Write-WastedWarn "Could not parse OBS scene collection '$($file.Name)': $($_.Exception.Message)"
                continue
            }
            if ($data.PSObject.Properties['name'] -and [string]$data.name -eq $CollectionName) {
                Write-WastedInfo "OBS scene collection '$CollectionName' found at $($file.FullName)."
                $found = $true
                break
            }
        }
    }
    if (-not $found) {
        Write-WastedWarn "No OBS scene collection named '$CollectionName' under $scenesRoot. OBS ignores an unknown --collection and keeps the previous scenes, so capture may be black or silent. Build it once in the UI and name it exactly '$CollectionName' — the source list is in scripts\obs-profile\README.md."
    }
}

function Set-GameDisplaySettings {
    <#
    Read (and with -EnforceDisplaySettings, write) ScreenWidth / ScreenHeight / Windowed /
    PauseOnFocusLoss in the Rockstar settings.xml. The elements are located by name anywhere in
    the document rather than by a hardcoded path, and anything the file does not already contain
    is reported instead of invented.

    PauseOnFocusLoss is the fourth key for a reason. It is GTA V's own video preference (the RAGE
    profile setting PREF_VID_PAUSE_ON_FOCUS_LOSS, exposed in-game as Settings -> Graphics ->
    "Pause Game On Focus Loss") and it ships ON, which freezes the game whenever its window is not
    the foreground window. On a headless box that nobody is sitting in front of, that is the
    difference between a live stream and a still frame. Anything that resets settings.xml — a
    graphics auto-detect after a driver change, a game update, verifying files — silently puts it
    back to On, so it is re-checked on every launch rather than assumed.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        Write-WastedWarn "Rockstar settings.xml not found at '$Path' — it appears after the game's first launch and its graphics auto-detect. Set 1280x720 borderless there before the first unattended run."
        return
    }
    # Windowed: 0 = fullscreen, 1 = windowed, 2 = borderless. Borderless is mandatory here:
    # exclusive fullscreen cannot be entered over Terminal Server and is fragile on an
    # indirect display. PauseOnFocusLoss: 0 = keep running when unfocused (what we need), 1 = pause.
    $wanted = @{ ScreenWidth = "$ScreenWidth"; ScreenHeight = "$ScreenHeight"; Windowed = '2'; PauseOnFocusLoss = '0' }

    # The game rewrites settings.xml when it exits, so an edit made while it is running is thrown
    # away and would leave the log claiming a fix that is not there.
    $gameUp = @(Get-Process -Name 'GTA5' -ErrorAction SilentlyContinue) + @(Get-Process -Name 'GTA5_Enhanced' -ErrorAction SilentlyContinue)
    $mayWrite = $EnforceDisplaySettings
    if ($EnforceDisplaySettings -and $gameUp.Count -gt 0) {
        $mayWrite = $false
        Write-WastedWarn ('The game is already running, so settings.xml will only be REPORTED, not written: the game rewrites ' +
            'this file on exit and would clobber the edit. Change these from the in-game menus, or close the game and re-run.')
    }
    try {
        $xml = [System.Xml.XmlDocument]::new()
        $xml.PreserveWhitespace = $true
        $xml.Load($Path)
    }
    catch {
        Write-WastedWarn "Could not parse '$Path' ($($_.Exception.Message)) — leaving it alone. Fix it by hand."
        return
    }

    $changes = @()
    foreach ($name in @('ScreenWidth', 'ScreenHeight', 'Windowed', 'PauseOnFocusLoss')) {
        $node = $xml.SelectSingleNode("//$name")
        if ($null -eq $node) {
            Write-WastedWarn "settings.xml has no <$name> element — not creating one. Set it in the game's graphics menu, then re-run."
            continue
        }
        $attr = $node.Attributes['value']
        if ($null -eq $attr) {
            Write-WastedWarn "settings.xml <$name> has no 'value' attribute — leaving it alone."
            continue
        }
        $current = [string]$attr.Value
        if ($current -eq $wanted[$name]) {
            Write-WastedInfo "settings.xml $name=$current (correct)."
            continue
        }
        if ($mayWrite) {
            $attr.Value = $wanted[$name]
            $changes += "$name $current -> $($wanted[$name])"
        }
        elseif ($name -eq 'PauseOnFocusLoss') {
            Write-WastedWarn ("settings.xml PauseOnFocusLoss=$current, expected 0. The game will FREEZE whenever its window is not " +
                'focused, which is every moment nobody is at the keyboard. Fix it in-game: Esc -> Settings -> Graphics -> ' +
                '"Pause Game On Focus Loss" -> Off (and Settings -> Audio -> mute-on-focus-loss -> Off, which is a Rockstar ' +
                'profile setting and is not in this file), or close the game and re-run with -EnforceDisplaySettings.')
        }
        else {
            Write-WastedWarn "settings.xml $name=$current, expected $($wanted[$name]). Re-run with -EnforceDisplaySettings to fix it, or edit the file by hand."
        }
    }

    if ($changes.Count -gt 0 -and $PSCmdlet.ShouldProcess($Path, "apply " + ($changes -join ', '))) {
        $backup = "$Path.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Copy-Item -LiteralPath $Path -Destination $backup -Force
        $xml.Save($Path)
        Write-WastedInfo "settings.xml updated ($($changes -join '; ')); previous file kept at $backup."
    }
}

try {
    Write-WastedInfo "run.ps1 starting. Repo=$RepoDir GameTimeoutS=$GameTimeoutS"

    # --- Session + display preflight ------------------------------------------------------------
    Write-WastedStep 'Step 1/7: session and display target.'
    $sessionName = $env:SESSIONNAME
    if (-not $sessionName) { $sessionName = '<unset>' }
    Write-WastedInfo "SESSIONNAME=$sessionName"
    if ($sessionName -ne 'Console') {
        $message = "run.ps1 is not in the console session (SESSIONNAME=$sessionName). The game would render on the Basic Render Driver, could never go fullscreen, and would die when this session disconnects. Launch it from the console session (autologon + the WASTED-Run task do this), or leave RDP with detach-rdp.ps1 first."
        if ($AllowRdpSession -or $Force) {
            Write-WastedWarn "$message Continuing because -AllowRdpSession/-Force was given."
        }
        else {
            throw $message
        }
    }

    $modes = Get-ActiveDisplayModes
    if ($modes.Count -eq 0) {
        $message = 'No display adapter reports an active desktop mode: this server has no monitor and no HDMI emulator, so the console session has no display target. Install/repair the virtual display driver (server-setup.ps1) before launching the game.'
        if ($Force) { Write-WastedWarn "$message Continuing because -Force was given." } else { throw $message }
    }
    else {
        foreach ($mode in $modes) { Write-WastedInfo ("display: {0} at {1}x{2}" -f $mode.Name, $mode.Width, $mode.Height) }
        $usable = @($modes | Where-Object { $_.Width -ge $ScreenWidth -and $_.Height -ge $ScreenHeight })
        if ($usable.Count -eq 0) {
            $best = $modes | Sort-Object Width -Descending | Select-Object -First 1
            $message = "The largest active desktop mode is $($best.Width)x$($best.Height), smaller than the required ${ScreenWidth}x${ScreenHeight}. 1024x768 is the classic no-display-target fallback: check the virtual display driver device and that 1280x720 is FIRST in its resolution list."
            if ($Force) { Write-WastedWarn "$message Continuing because -Force was given." } else { throw $message }
        }
    }

    # --- Game display settings ------------------------------------------------------------------
    Write-WastedStep 'Step 2/7: GTA V display settings (1280x720 windowed-borderless).'
    Set-GameDisplaySettings -Path $settingsXml

    # --- Steam ------------------------------------------------------------------------------
    Write-WastedStep 'Step 3/7: Steam (silent).'
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
    Write-WastedStep "Step 4/7: GTA V Legacy (steam.exe -applaunch $steamAppId $($GameArgs -join ' '))."
    if (Get-Process -Name GTA5 -ErrorAction SilentlyContinue) {
        Write-WastedInfo 'GTA5.exe already running — skipping launch.'
    }
    else {
        if ($PSCmdlet.ShouldProcess("app $steamAppId", 'steam.exe -applaunch')) {
            # Launch chain: steam.exe -> PlayGTAV.exe -> Rockstar Games Launcher -> GTA5.exe.
            # Steam forwards the trailing arguments to the game. The Rockstar launcher sits in
            # the middle of that chain, so the durable place for -nobattleye is the args.txt /
            # commandline.txt that fetch-shvdn.ps1 deploys next to GTA5.exe; these arguments are
            # belt-and-braces on top of it.
            $launchArgs = @('-applaunch', "$steamAppId") + $GameArgs
            Start-Process -FilePath $SteamExe -ArgumentList $launchArgs
            Write-WastedInfo "Polling for GTA5.exe (up to $GameTimeoutS s; the Rockstar launcher appears mid-chain)..."
            $game = Wait-WastedProcess -Name GTA5 -TimeoutS $GameTimeoutS
            if (-not $game) {
                throw "GTA5.exe did not appear within $GameTimeoutS s. Check the Rockstar launcher state on the console session (offline-mode hiccups are a known failure mode), and confirm the Intel graphics driver is bound — without Direct3D 11 the game exits silently."
            }
            Write-WastedInfo "GTA5.exe up (pid $($game.Id)). Allowing 20 s for the story load + Script Hook V init."
            Start-Sleep -Seconds 20
        }
    }

    # --- Harness ------------------------------------------------------------------------------
    Write-WastedStep 'Step 5/7: harness (python -m wasted_harness.main).'
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
    Write-WastedStep 'Step 6/7: OBS with replay buffer.'
    if ($SkipObs) {
        Write-WastedInfo 'Skipped (-SkipObs).'
    }
    elseif (Get-Process -Name obs64 -ErrorAction SilentlyContinue) {
        Write-WastedInfo 'OBS already running — skipping.'
    }
    else {
        if (-not (Test-Path -LiteralPath $ObsExe)) { throw "OBS not found at '$ObsExe' (pass -ObsExe)." }

        # OBS resolves --profile / --collection by NAME and falls back SILENTLY to whatever was
        # last used when the name is unknown (OBS 32.2.2: OBSBasic::InitBasicConfig ->
        # GetProfileByName, OBSBasic::Load -> GetSceneCollectionByName). Silent fallback here
        # means streaming at the wrong resolution/bitrate with no replay buffer, so check the
        # names exist and say which one is missing instead of letting OBS shrug.
        Test-ObsProfileInstalled -ProfileName $ObsProfile
        Test-ObsSceneCollectionInstalled -CollectionName $ObsSceneCollection

        # PowerShell 7's Start-Process does NOT quote -ArgumentList elements that contain spaces
        # (checked on 7.4.6: @('--profile','WASTED Live') arrives as three separate arguments), so
        # a name with a space would lose its tail and land right back in OBS's silent fallback.
        # Quote the two operator-supplied values here; CommandLineToArgvW strips the quotes again
        # before OBS parses them.
        foreach ($name in @($ObsProfile, $ObsSceneCollection)) {
            if ([string]::IsNullOrWhiteSpace($name) -or $name.Contains('"')) {
                throw "OBS profile / scene-collection names must be non-empty and free of double quotes; got '$name'."
            }
        }
        $obsArgs = @(
            '--profile', "`"$ObsProfile`""            # 720p30 x264 canvas + replay buffer settings
            '--collection', "`"$ObsSceneCollection`"" # Game Capture / CABLE Output / overlay sources
            '--startreplaybuffer'
            '--disable-shutdown-check'                # present through OBS 31.x, ignored by 32.x
        )
        if ($PSCmdlet.ShouldProcess($ObsExe, "start OBS $($obsArgs -join ' ')")) {
            # OBS must start from its own bin directory or it fails to find locale/modules.
            # --disable-shutdown-check suppressed the safe-mode dialog after a hard kill on
            # OBS <= 31.x; 32.x dropped the flag and ignores unknown arguments, so it is
            # harmless to keep passing while the installed version is uncertain.
            Start-Process -FilePath $ObsExe -WorkingDirectory (Split-Path -Parent $ObsExe) `
                -ArgumentList $obsArgs
            if (-not (Wait-WastedProcess -Name obs64 -TimeoutS 60)) {
                throw 'OBS process did not appear within 60 s.'
            }
            Write-WastedInfo "OBS is up on profile '$ObsProfile' / collection '$ObsSceneCollection' with the replay buffer requested at launch."
        }
    }

    # --- Bridge probe (informational) ---------------------------------------------------------
    Write-WastedStep "Step 7/7: bridge probe ($bridgeHealthUrl)."
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
