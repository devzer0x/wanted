#Requires -Version 7.0
<#
.SYNOPSIS
    Leave RDP without killing the interactive desktop: redirect this session to the console.
.DESCRIPTION
    Runs `tscon <current session> /dest:console` (RESEARCH.md §7). Closing an RDP window
    normally leaves the machine without a GUI session, which kills Desktop Duplication capture
    and the game's display; this script hands the desktop back to the console session instead.

    On this server the console session's display is the **virtual display driver's** monitor,
    not a physical one: the machine is an auction box with an Intel UHD 770 iGPU, no monitor
    and no HDMI emulator, so an indirect display driver supplies the only display target.
    That does not change the procedure — it is why the procedure matters.

    Must run elevated — an unelevated tscon fails silently and locks the console. Your RDP
    window will close when it succeeds; that is the success mode.
.EXAMPLE
    pwsh -File .\detach-rdp.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
. $commonPath

Assert-WastedEnvironment -ScriptName 'detach-rdp.ps1' -RequireWastedRoot -RequireElevation -Force:$Force
Start-WastedTranscript -Name 'detach-rdp' | Out-Null

try {
    $session = $env:SESSIONNAME
    if (-not $session) {
        throw 'SESSIONNAME environment variable is empty — cannot determine the current session (are you in an SSH session? tscon needs the RDP session itself).'
    }
    Write-WastedInfo "Current session: $session"
    Write-WastedInfo 'Session table before detach:'
    query session | ForEach-Object { Write-WastedInfo "  $_" }

    if ($session -eq 'Console') {
        Write-WastedInfo 'Already the console session — nothing to detach.'
        exit 0
    }
    if ($session -notlike 'RDP-Tcp#*') {
        Write-WastedWarn "Session name '$session' does not look like an RDP session (RDP-Tcp#N) — proceeding anyway."
    }

    if ($PSCmdlet.ShouldProcess("session $session", 'redirect to console via tscon /dest:console')) {
        & (Join-Path $env:SystemRoot 'System32\tscon.exe') $session /dest:console
        if ($LASTEXITCODE -ne 0) {
            throw "tscon exited $LASTEXITCODE — the session was NOT redirected. Confirm this shell is elevated and the session name '$session' is correct (query session)."
        }
        # On success the RDP connection drops immediately; this line lands in the transcript.
        Write-WastedStep 'tscon succeeded — desktop handed to the console session.'
    }
    exit 0
}
catch {
    Write-WastedError "detach-rdp.ps1 failed: $($_.Exception.Message)"
    exit 1
}
finally {
    Stop-WastedTranscript
}
