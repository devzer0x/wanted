#Requires -Version 7.0
<#
.SYNOPSIS
    Fetch Script Hook V (dev-c.com) and the official SHVDN nightly (GitHub) into the game folder.
.DESCRIPTION
    Per RESEARCH.md D1: GTA V Legacy + Script Hook V + official ScriptHookVDotNet nightly
    >= v3.7.0-nightly.189. Downloads both, deploys them next to GTA5.exe (Script Hook V's own
    2026 instructions say: copy the whole bin\ folder — that includes args.txt, which carries
    "-nobattleye -noBE"), backs up replaced files, and writes a version manifest to
    C:\wasted\state\shvdn-manifest.json.

    dev-c.com gates the zip behind a Referer check (verified 2026-08-25). If the direct
    download still fails, this script prints the manual step: download the zip in a browser
    from https://www.dev-c.com/gtav/scripthookv/ into C:\wasted\downloads and re-run —
    the newest ScriptHookV_*.zip found there is used automatically.
.PARAMETER GameDir
    GTA V Legacy install folder (contains GTA5.exe).
.PARAMETER ShvdnTag
    SHVDN nightly tag to fetch ('latest' or e.g. 'v3.7.0-nightly.189').
.EXAMPLE
    pwsh -File .\fetch-shvdn.ps1 -GameDir 'C:\Program Files (x86)\Steam\steamapps\common\Grand Theft Auto V'
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$GameDir,
    [string]$ShvdnTag = 'latest',
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
. $commonPath

Assert-WastedEnvironment -ScriptName 'fetch-shvdn.ps1' -RequireWastedRoot -Force:$Force
Start-WastedTranscript -Name 'fetch-shvdn' | Out-Null

$userAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) wasted-ops/1.0'
$downloadDir = Join-Path $script:WastedRoot 'downloads'
$stateDir = Join-Path $script:WastedRoot 'state'
$minNightly = 189   # CONTRACTS.md platform baseline: SHVDN >= v3.7.0-nightly.189

function Test-ZipFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $bytes = Get-Content -LiteralPath $Path -AsByteStream -TotalCount 2
    return ($bytes.Count -eq 2 -and $bytes[0] -eq 0x50 -and $bytes[1] -eq 0x4B)  # 'PK'
}

function Copy-WithBackup {
    # Copies $Source into $GameDir, moving any existing same-named file into $BackupDir first.
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$BackupDir
    )
    $name = Split-Path -Leaf $Source
    $target = Join-Path $GameDir $name
    if ($PSCmdlet.ShouldProcess($target, "deploy $name")) {
        if (Test-Path -LiteralPath $target) {
            New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
            Move-Item -LiteralPath $target -Destination (Join-Path $BackupDir $name) -Force
        }
        Copy-Item -LiteralPath $Source -Destination $target -Force
        Write-WastedInfo ("Deployed {0} (sha256 {1})" -f $name, (Get-WastedFileSha256 -Path $target))
    }
    return $target
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $GameDir 'GTA5.exe'))) {
        if ($Force) {
            Write-WastedWarn "GTA5.exe not found in '$GameDir' — continuing because -Force was given (staging use only)."
        }
        else {
            throw "'$GameDir' does not contain GTA5.exe — not a GTA V Legacy install. Aborting (use -Force only for a staging directory)."
        }
    }
    New-Item -ItemType Directory -Path $downloadDir, $stateDir -Force -WhatIf:$false -Confirm:$false | Out-Null
    $backupDir = Join-Path $stateDir ('backup\shvdn\{0}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

    # --- Script Hook V ------------------------------------------------------------------------
    Write-WastedStep 'Step 1/3: Script Hook V from dev-c.com.'
    $shvPage = 'https://www.dev-c.com/gtav/scripthookv/'
    $shvZip = $null
    try {
        $page = Invoke-WebRequest -Uri $shvPage -UserAgent $userAgent -TimeoutSec 30
        if ($page.Content -notmatch 'href="(/files/ScriptHookV_[^"]+\.zip)"') {
            throw "Could not find a ScriptHookV_*.zip link on $shvPage — page layout changed."
        }
        $shvUrl = "https://www.dev-c.com$($Matches[1])"
        $shvName = Split-Path -Leaf $shvUrl
        $shvZip = Join-Path $downloadDir $shvName
        if (Test-ZipFile -Path $shvZip) {
            Write-WastedInfo "$shvName already downloaded — reusing."
        }
        elseif ($PSCmdlet.ShouldProcess($shvUrl, 'download Script Hook V')) {
            # The file server 302-redirects to the page unless the Referer header is present.
            Invoke-WebRequest -Uri $shvUrl -OutFile $shvZip -UserAgent $userAgent `
                -Headers @{ Referer = $shvPage } -MaximumRedirection 0 -TimeoutSec 120
            if (-not (Test-ZipFile -Path $shvZip)) {
                Remove-Item -LiteralPath $shvZip -Force -ErrorAction SilentlyContinue
                throw "Downloaded '$shvName' is not a zip — dev-c.com likely served an HTML page instead."
            }
        }
    }
    catch {
        Write-WastedWarn "Direct Script Hook V download failed: $($_.Exception.Message)"
        $manual = Get-ChildItem -Path $downloadDir -Filter 'ScriptHookV_*.zip' -ErrorAction SilentlyContinue |
            Where-Object { Test-ZipFile -Path $_.FullName } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($manual) {
            $shvZip = $manual.FullName
            Write-WastedInfo "Using manually downloaded $($manual.Name) from $downloadDir."
        }
        else {
            throw ("Script Hook V could not be fetched automatically. MANUAL STEP: open $shvPage in a browser, " +
                "download the current ScriptHookV_*.zip into $downloadDir, then re-run this script.")
        }
    }

    $shvVersion = if ((Split-Path -Leaf $shvZip) -match 'ScriptHookV_(.+)\.zip') { $Matches[1] } else { 'unknown' }
    $shvExtract = Join-Path $downloadDir 'extract_shv'
    $shvFiles = @()
    if ($PSCmdlet.ShouldProcess($GameDir, "deploy Script Hook V $shvVersion (contents of bin\)")) {
        if (Test-Path -LiteralPath $shvExtract) { Remove-Item -LiteralPath $shvExtract -Recurse -Force }
        Expand-Archive -LiteralPath $shvZip -DestinationPath $shvExtract -Force
        $binDir = Join-Path $shvExtract 'bin'
        if (-not (Test-Path -LiteralPath $binDir)) {
            throw "Script Hook V zip has no bin\ folder — layout changed; inspect $shvExtract and deploy manually."
        }
        foreach ($file in Get-ChildItem -Path $binDir -File) {
            $deployed = Copy-WithBackup -Source $file.FullName -BackupDir $backupDir
            $shvFiles += @{ name = $file.Name; sha256 = (Get-WastedFileSha256 -Path $deployed) }
        }
    }

    # --- SHVDN nightly ------------------------------------------------------------------------
    Write-WastedStep "Step 2/3: ScriptHookVDotNet nightly ($ShvdnTag) via the GitHub API."
    $apiBase = 'https://api.github.com/repos/scripthookvdotnet/scripthookvdotnet-nightly'
    $apiUrl = if ($ShvdnTag -eq 'latest') { "$apiBase/releases/latest" } else { "$apiBase/releases/tags/$ShvdnTag" }
    $release = Invoke-RestMethod -Uri $apiUrl -Headers @{
        'User-Agent' = 'wasted-ops/1.0'; Accept = 'application/vnd.github+json'
    } -TimeoutSec 30
    $tag = $release.tag_name
    Write-WastedInfo "Resolved SHVDN release: $tag (published $($release.published_at))."

    if ($tag -match '^v3\.7\.0-nightly\.(\d+)$') {
        $nightlyNumber = [int]$Matches[1]
        if ($nightlyNumber -lt $minNightly -and -not $Force) {
            throw "SHVDN $tag is older than the contract minimum v3.7.0-nightly.$minNightly — refusing (override with -Force)."
        }
    }
    else {
        Write-WastedWarn "SHVDN tag '$tag' does not match the v3.7.0-nightly.N pattern — cannot compare against the contract minimum; verify manually."
    }

    $asset = @($release.assets) | Where-Object { $_.name -like 'ScriptHookVDotNet-*.zip' } | Select-Object -First 1
    if (-not $asset) { throw "Release $tag has no ScriptHookVDotNet-*.zip asset." }
    $shvdnZip = Join-Path $downloadDir $asset.name
    if (Test-ZipFile -Path $shvdnZip) {
        Write-WastedInfo "$($asset.name) already downloaded — reusing."
    }
    elseif ($PSCmdlet.ShouldProcess($asset.browser_download_url, 'download SHVDN nightly')) {
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $shvdnZip -UserAgent $userAgent -TimeoutSec 120
        if (-not (Test-ZipFile -Path $shvdnZip)) {
            Remove-Item -LiteralPath $shvdnZip -Force -ErrorAction SilentlyContinue
            throw "Downloaded '$($asset.name)' is not a zip."
        }
    }

    $shvdnFiles = @()
    if ($PSCmdlet.ShouldProcess($GameDir, "deploy SHVDN $tag runtime files")) {
        $shvdnExtract = Join-Path $downloadDir 'extract_shvdn'
        if (Test-Path -LiteralPath $shvdnExtract) { Remove-Item -LiteralPath $shvdnExtract -Recurse -Force }
        Expand-Archive -LiteralPath $shvdnZip -DestinationPath $shvdnExtract -Force
        # Runtime set only; Docs/ (XML API docs) and Licenses/ stay out of the game folder.
        $wanted = @('ScriptHookVDotNet.asi', 'ScriptHookVDotNet.ini',
            'ScriptHookVDotNet2.dll', 'ScriptHookVDotNet2.pdb',
            'ScriptHookVDotNet3.dll', 'ScriptHookVDotNet3.pdb', 'ScriptHookVDotNet.pdb')
        foreach ($name in $wanted) {
            $src = Join-Path $shvdnExtract $name
            if (-not (Test-Path -LiteralPath $src)) {
                if ($name -like '*.pdb') { continue }  # pdbs are optional debug symbols
                throw "SHVDN zip is missing expected file '$name' — layout changed; inspect $shvdnExtract."
            }
            $deployed = Copy-WithBackup -Source $src -BackupDir $backupDir
            $shvdnFiles += @{ name = $name; sha256 = (Get-WastedFileSha256 -Path $deployed) }
        }
        # SHVDN loads compiled scripts from <game root>\scripts (ScriptsLocation default).
        New-Item -ItemType Directory -Path (Join-Path $GameDir 'scripts') -Force | Out-Null
    }

    # --- manifest -----------------------------------------------------------------------------
    Write-WastedStep 'Step 3/3: version manifest.'
    $manifestPath = Join-Path $stateDir 'shvdn-manifest.json'
    $manifest = [ordered]@{
        fetched_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        game_dir   = $GameDir
        backup_dir = $backupDir
        script_hook_v = [ordered]@{
            version    = $shvVersion
            zip        = (Split-Path -Leaf $shvZip)
            source_url = $shvPage
            files      = $shvFiles
        }
        shvdn = [ordered]@{
            tag        = $tag
            asset      = $asset.name
            source_url = $asset.browser_download_url
            files      = $shvdnFiles
        }
    }
    if ($PSCmdlet.ShouldProcess($manifestPath, 'write version manifest')) {
        $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding utf8
        Write-WastedInfo "Manifest written: $manifestPath"
        Get-Content -LiteralPath $manifestPath | Write-Host
    }

    Write-WastedStep "Done. Script Hook V $shvVersion + SHVDN $tag deployed to $GameDir."
    Write-WastedInfo 'Record the pinned SHVDN tag in STATUS.md and bridge/README per CONTRACTS.md.'
    exit 0
}
catch {
    Write-WastedError "fetch-shvdn.ps1 failed: $($_.Exception.Message)"
    Write-WastedError $_.ScriptStackTrace
    exit 1
}
finally {
    Stop-WastedTranscript
}
