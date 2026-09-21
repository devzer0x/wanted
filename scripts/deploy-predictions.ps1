#Requires -Version 7.0
<#
.SYNOPSIS
    Bring the box up to the checked-out commit and turn the prediction layer on.

.DESCRIPTION
    Predictions are created on this machine and nowhere else: no web deploy and no database
    change can make one appear (RUNBOOK §5.7 step 2). This is that step, as one command, for
    the case where SSH to the box is unavailable and the only way in is a Remote Desktop
    session.

    It builds the harness package FROM THE REPO CHECKOUT already on this machine rather than
    from an uploaded file, so there is nothing to transfer, and then hands over to
    `deploy-all.ps1`, which is the tested path: it refuses to run unless the game is alive and
    ticking, backs the current package up, restores that backup if the new code does not
    import, and starts exactly one harness. Nothing here touches OBS, the game process or the
    stream, and nothing here starts the game.

    It also restarts sshd on the way out. SSH to this box has been refusing connections all
    day (`Connection reset`), and a restart of the service is the one fix that can only be
    applied from a session that is already inside — which is exactly where this script runs.

.PARAMETER Repo
    The checkout to deploy from. Cloned if missing, fast-forwarded if present.

.PARAMETER SkipSshRestart
    Leave sshd alone.

.EXAMPLE
    pwsh -File C:\wasted\repo\scripts\deploy-predictions.ps1
#>
[CmdletBinding()]
param(
    [string]$Repo = 'C:\wasted\repo',
    [string]$RepoUrl = 'https://github.com/devzer0x/wanted.git',
    [string]$HarnessDir = 'C:\wasted\harness',
    [switch]$SkipSshRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Step([string]$m) { Write-Host "[predictions] $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "[predictions] WARNING: $m" -ForegroundColor Yellow }

try {
    # deploy-all.ps1 registers scheduled tasks, which needs a full admin token.
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Not elevated. Right-click Start -> Terminal (Admin) and run this again.'
    }

    # --- 1. the code ----------------------------------------------------------------------
    if (Test-Path -LiteralPath (Join-Path $Repo '.git')) {
        Step "1/5 updating $Repo"
        git -C $Repo fetch --quiet origin main
        git -C $Repo checkout --quiet main
        git -C $Repo reset --hard --quiet origin/main
    }
    else {
        Step "1/5 cloning $RepoUrl -> $Repo (public repo; no credentials needed)"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Repo) | Out-Null
        git clone --quiet $RepoUrl $Repo
    }
    Step "    at $(git -C $Repo log --oneline -1)"

    # The prediction layer must actually be in what we just fetched, or the rest is theatre.
    foreach ($required in 'harness\wasted_harness\predictions\baserate.py',
                          'harness\wasted_harness\predictions\generator.py') {
        if (-not (Test-Path -LiteralPath (Join-Path $Repo $required))) {
            throw "$required is missing from the checkout — wrong branch or a failed fetch."
        }
    }

    # --- 2. the package deploy-all.ps1 expects --------------------------------------------
    New-Item -ItemType Directory -Force -Path 'C:\wasted\tmp' | Out-Null
    $tgz = 'C:\wasted\tmp\harness-today.tgz'
    Step "2/5 packaging wasted_harness -> $tgz"
    # Same command docs/go-live.md documents, run here instead of on a dev machine. __pycache__
    # is excluded because a .pyc compiled against another interpreter is worse than no .pyc.
    Push-Location (Join-Path $Repo 'harness')
    try { tar czf $tgz --exclude='__pycache__' --exclude='*.pyc' wasted_harness }
    finally { Pop-Location }
    if (-not (Test-Path -LiteralPath $tgz)) { throw "tar produced no $tgz" }
    Step "    $([math]::Round((Get-Item $tgz).Length / 1KB)) KB"

    # --- 3. the switch --------------------------------------------------------------------
    # Appended to the SERVER's own .env, which holds the real keys and is never overwritten
    # from a dev machine. Idempotent: the last assignment wins, so a stale `false` is replaced
    # rather than fought with.
    $envFile = Join-Path $HarnessDir '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { throw "$envFile is missing — this is not a provisioned game server." }
    $kept = @(Get-Content -LiteralPath $envFile | Where-Object { $_ -notmatch '^\s*WASTED_PREDICTIONS_ENABLED\s*=' })
    Set-Content -LiteralPath $envFile -Value ($kept + 'WASTED_PREDICTIONS_ENABLED=true')
    Step '3/5 WASTED_PREDICTIONS_ENABLED=true'

    # --- 4. the tested deploy path --------------------------------------------------------
    $deployAll = Join-Path $Repo 'scripts\deploy-all.ps1'
    if (-not (Test-Path -LiteralPath $deployAll)) { throw "Missing $deployAll" }
    Step '4/5 handing over to deploy-all.ps1 (it refuses to run unless the game is ticking)'
    & (Get-Process -Id $PID).Path -NoProfile -File $deployAll
    if ($LASTEXITCODE -ne 0) { throw "deploy-all.ps1 exited $LASTEXITCODE — the harness was NOT replaced (it restores its own backup on a failed import)." }

    # --- 5. sshd, so this does not have to happen in person next time ---------------------
    if (-not $SkipSshRestart) {
        try {
            Restart-Service sshd -ErrorAction Stop
            Step '5/5 sshd restarted'
        }
        catch { Warn "could not restart sshd ($($_.Exception.Message)); remote access may still refuse connections." }
    }
    else { Step '5/5 -SkipSshRestart given' }

    Write-Host ''
    Step 'DONE. A scheduled round is asked about every 5 minutes while the agent is playing,'
    Step 'so the first row should appear within ~6 minutes. It is NOT instant, and the first'
    Step 'one needs an hour of telemetry behind it unless the warm start finds it in Supabase.'
    Step 'Rewards stay OFF: rounds will settle and credit nobody until the operator says so.'
}
catch {
    Write-Host "[predictions] FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
