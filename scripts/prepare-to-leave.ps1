#Requires -Version 5.1
<#
.SYNOPSIS
    Run this INSIDE the RDP session, before you disconnect. Hardens access so the account cannot
    be locked out again, then reports whether the show is actually in a state you can walk away
    from. It does NOT disconnect you — that is a separate, deliberate step.

.DESCRIPTION
    Three jobs, in this order, safest first:

    1. LOCKOUT POLICY (zero risk). On 2026-09-02 this box locked its own Administrator account
       out twice (RDP error 0xd07). Threshold goes to 50 so a burst of failures cannot trip it,
       and duration drops from 30 minutes to 5 so if it ever does trip it costs minutes.
       This does NOT disable lockout — the box is internet-facing and that would be worse.

    2. FIREWALL (the actual fix, made safe). TCP 3389 open to the whole internet is why the
       account keeps locking: bots try passwords continuously and every failure feeds the same
       counter. They do not need to succeed. This restricts RDP and SSH to YOUR address —
       and it reads that address from the RDP connection you are sitting on RIGHT NOW rather
       than asking you to type it, because a typo here locks you out for real, which is worse
       than an account lockout since waiting will not fix it.
       Skip it with -SkipFirewall if you would rather do it by hand.

    3. READINESS REPORT (read-only). Is the game up, is the bridge ticking, is the keepalive task
       armed, which session is which. Nothing here changes anything.

.EXAMPLE
    pwsh -File .\prepare-to-leave.ps1
.EXAMPLE
    pwsh -File .\prepare-to-leave.ps1 -SkipFirewall
#>
[CmdletBinding()]
param(
    [switch]$SkipFirewall,
    # Extra addresses/ranges to allow in as well as the detected one (e.g. a phone hotspot).
    [string[]]$AlsoAllow = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

function Say([string]$m, [string]$c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Ok([string]$m) { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Bad([string]$m) { Write-Host "  [FAIL] $m" -ForegroundColor Red }
function Warn([string]$m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }

$elevated = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) {
    Bad 'Not elevated. Re-open PowerShell as Administrator — steps 1 and 2 need it.'
    exit 1
}

Say ''
Say '=== 1. LOCKOUT POLICY ===' 'Cyan'
net accounts /lockoutthreshold:50 /lockoutduration:5 /lockoutwindow:5 | Out-Null
$pol = net accounts | Select-String -Pattern 'Lockout'
$pol | ForEach-Object { Say "  $_" }
Ok 'A retry storm can no longer lock this account; a real lockout now clears in 5 minutes.'

Say ''
Say '=== 2. FIREWALL ===' 'Cyan'
if ($SkipFirewall) {
    Warn 'Skipped by request. RDP stays open to the internet and the lockouts will continue.'
} else {
    # The address you are connected FROM, taken from the live RDP session. No typing, no typos.
    $peers = @(Get-NetTCPConnection -LocalPort 3389 -State Established -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty RemoteAddress -Unique)
    if (-not $peers -or $peers.Count -eq 0) {
        Bad 'Could not detect your RDP source address (are you on the console rather than RDP?).'
        Warn 'Firewall NOT changed. Re-run over RDP, or set the rule by hand.'
    } else {
        $allow = @($peers) + $AlsoAllow | Where-Object { $_ } | Select-Object -Unique
        Say ("  detected your address(es): {0}" -f ($allow -join ', '))

        foreach ($grp in @('Remote Desktop')) {
            Get-NetFirewallRule -DisplayGroup $grp -ErrorAction SilentlyContinue |
                Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' } |
                ForEach-Object { Disable-NetFirewallRule -Name $_.Name }
        }
        Get-NetFirewallRule -DisplayName 'OpenSSH*' -ErrorAction SilentlyContinue |
            Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' } |
            ForEach-Object { Disable-NetFirewallRule -Name $_.Name }

        foreach ($r in @(@{n = 'WASTED-RDP-AllowOperator'; p = 3389 }, @{n = 'WASTED-SSH-AllowOperator'; p = 22 })) {
            Remove-NetFirewallRule -DisplayName $r.n -ErrorAction SilentlyContinue
            New-NetFirewallRule -DisplayName $r.n -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort $r.p -RemoteAddress $allow -Profile Any | Out-Null
        }
        Ok ("RDP (3389) and SSH (22) now reachable only from {0}" -f ($allow -join ', '))
        Warn 'If your home IP changes you will need Hetzner KVM to get back in. Undo with:'
        Say  '    Remove-NetFirewallRule -DisplayName "WASTED-*-AllowOperator"; Enable-NetFirewallRule -DisplayGroup "Remote Desktop"'
    }
}

Say ''
Say '=== 3. READY TO LEAVE? ===' 'Cyan'

$sessions = query session 2>&1
$sessions | ForEach-Object { Say "  $_" }

$game = Get-Process -Name 'GTA5', 'GTA5_Enhanced' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($game) { Ok ("game running (pid {0}, session {1})" -f $game.Id, $game.SessionId) }
else { Bad 'GTA V is NOT running — start it before you leave.' }

$obs = Get-Process -Name 'obs64' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($obs) { Ok ("OBS running (pid {0}, session {1})" -f $obs.Id, $obs.SessionId) }
else { Bad 'OBS is NOT running.' }

# The honest "is the game actually advancing" probe: two samples, a few seconds apart.
# tick_hz is the in-game script thread and game_fps the game's own frame rate; both stop when
# the game is paused. This is what the stream freeze looks like from the inside, and it is
# completely independent of OBS.
function Get-Health {
    try { (Invoke-WebRequest -Uri 'http://127.0.0.1:7777/health' -TimeoutSec 5 -UseBasicParsing).Content | ConvertFrom-Json }
    catch { $null }
}
$h1 = Get-Health
if (-not $h1) {
    Bad 'bridge /health unreachable — the in-game script is not answering. Is the game fully loaded into the world (not a menu)?'
} else {
    Say ("  sample 1: tick_hz={0} game_fps={1} edition={2}" -f $h1.tick_hz, $h1.game_fps, $h1.edition)
    Start-Sleep -Seconds 5
    $h2 = Get-Health
    if (-not $h2) {
        Bad 'bridge answered once then stopped — the game may have paused or crashed.'
    } elseif ($h2.tick_hz -le 0) {
        Bad ("tick_hz is {0} — the game is PAUSED or on a modal screen. Fix this before leaving." -f $h2.tick_hz)
    } else {
        Ok ("game is genuinely running: tick_hz={0}, game_fps={1}" -f $h2.tick_hz, $h2.game_fps)
    }
}

$task = schtasks /query /tn 'WASTED-ConsoleKeepalive' 2>&1
if ($LASTEXITCODE -eq 0) { Ok 'WASTED-ConsoleKeepalive task is installed' }
else { Warn 'WASTED-ConsoleKeepalive task NOT found — the session will stay parked when you leave.' }

Say ''
Say '=== NEXT ===' 'Cyan'
Say '  If everything above is [OK], disconnect with this (NOT the X button):' 'White'
Say '      tscon 1 /dest:console' 'Yellow'
Say '  Your RDP window will close immediately. That is success, not a failure.' 'White'
Say '  Then tell Fable you are out, so the agent can be started with his eyes on the'  'White'
Say '  virtual display instead of the RDP one.' 'White'
Say ''
