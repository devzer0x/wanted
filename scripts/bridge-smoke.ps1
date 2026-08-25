#Requires -Version 7.0
<#
.SYNOPSIS
    Smoke test for every bridge HTTP endpoint in CONTRACTS.md §1, with a paste-ready table.
.DESCRIPTION
    Exercises GET /state and /health, POST /task for all 11 task types (params built from the
    live /state snapshot so coordinates are contract-valid world positions), an unknown-type
    negative check (expects 400), /timescale (set + restore), /control (off + on), /radio,
    /horn, and /unstick (200, or 409 unstick_conditions_not_met — both contract-conform).
    Ends with `stop` and verifies last_task.status returns to idle. Prints a markdown PASS/FAIL
    table with response excerpts (paste into docs/STATUS.md) and exits nonzero on any FAIL.
    Run with the game running and the WastedBridge script loaded.
.PARAMETER BaseUrl
    Bridge base URL. Default http://127.0.0.1:7777 (CONTRACTS.md §1).
.EXAMPLE
    pwsh -File .\bridge-smoke.ps1
.EXAMPLE
    pwsh -File .\bridge-smoke.ps1 -BaseUrl http://127.0.0.1:7777
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:7777',
    [int]$TimeoutS = 10,
    [double]$InterTaskDelayS = 1.5,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$commonPath = Join-Path $PSScriptRoot 'common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) { Write-Error "Missing $commonPath — run from a full checkout of scripts/."; exit 1 }
. $commonPath

# -AllowNonWindowsWithForce: the smoke test may target the bridge through an SSH tunnel.
Assert-WastedEnvironment -ScriptName 'bridge-smoke.ps1' -RequireWastedRoot -AllowNonWindowsWithForce -Force:$Force
Start-WastedTranscript -Name 'bridge-smoke' | Out-Null

$results = [System.Collections.Generic.List[pscustomobject]]::new()
$checkNum = 0

function Invoke-Bridge {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [object]$Body = $null
    )
    $req = @{ Uri = "$BaseUrl$Path"; Method = $Method; TimeoutSec = $TimeoutS; SkipHttpErrorCheck = $true }
    if ($null -ne $Body) {
        $req.Body = ($Body | ConvertTo-Json -Depth 6 -Compress)
        $req.ContentType = 'application/json'
    }
    try {
        $resp = Invoke-WebRequest @req
        $text = if ($resp.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($resp.Content) } else { [string]$resp.Content }
        $json = $null
        try { $json = $text | ConvertFrom-Json } catch { <# non-JSON body; Json stays null #> }
        return @{ Status = [int]$resp.StatusCode; Text = $text; Json = $json; Err = $null }
    }
    catch {
        return @{ Status = 0; Text = ''; Json = $null; Err = $_.Exception.Message }
    }
}

function Get-JsonProp {
    # StrictMode-safe property access on ConvertFrom-Json output.
    param($Object, [Parameter(Mandatory)][string]$Name)
    if ($null -eq $Object) { return $null }
    $prop = $Object.PSObject.Properties[$Name]
    if ($prop) { return $prop.Value } else { return $null }
}

function Format-Excerpt {
    param([AllowNull()][AllowEmptyString()][string]$Text)
    if (-not $Text) { return '' }
    $flat = ($Text -replace '\r?\n', ' ' -replace '\|', '\|').Trim()
    if ($flat.Length -gt 90) { $flat = $flat.Substring(0, 90) + '…' }
    return $flat
}

function Add-Result {
    param(
        [Parameter(Mandatory)][string]$Check,
        [Parameter(Mandatory)][string]$Expect,
        [Parameter(Mandatory)][string]$Got,
        [Parameter(Mandatory)][ValidateSet('PASS', 'FAIL', 'SKIP')][string]$Outcome,
        [AllowEmptyString()][string]$Excerpt = ''
    )
    $script:checkNum++
    $script:results.Add([pscustomobject]@{
            Num = $script:checkNum; Check = $Check; Expect = $Expect; Got = $Got
            Outcome = $Outcome; Excerpt = $Excerpt
        })
    $level = if ($Outcome -eq 'FAIL') { 'WARN' } else { 'INFO' }
    Write-WastedLog -Level $level -Message ('{0,-4} {1} — {2}' -f $Outcome, $Check, $Got)
}

function Test-MissingKeys {
    param($Json, [Parameter(Mandatory)][string[]]$Keys)
    if ($null -eq $Json) { return $Keys }
    return @($Keys | Where-Object { -not $Json.PSObject.Properties[$_] })
}

function Add-HttpResult {
    # Generic check: expected status + optionally required JSON keys.
    param(
        [Parameter(Mandatory)][string]$Check,
        [Parameter(Mandatory)][hashtable]$Response,
        [Parameter(Mandatory)][int[]]$ExpectStatus,
        [string[]]$RequireKeys = @()
    )
    $expectText = "HTTP $($ExpectStatus -join '/')" + $(if ($RequireKeys.Count) { " + keys: $($RequireKeys -join ',')" } else { '' })
    if ($Response.Err) {
        Add-Result -Check $Check -Expect $expectText -Got "error: $($Response.Err)" -Outcome FAIL
        return
    }
    if ($Response.Status -notin $ExpectStatus) {
        Add-Result -Check $Check -Expect $expectText -Got "HTTP $($Response.Status)" -Outcome FAIL -Excerpt (Format-Excerpt $Response.Text)
        return
    }
    $missing = Test-MissingKeys -Json $Response.Json -Keys $RequireKeys
    if ($missing.Count -gt 0) {
        Add-Result -Check $Check -Expect $expectText -Got "HTTP $($Response.Status), missing keys: $($missing -join ',')" -Outcome FAIL -Excerpt (Format-Excerpt $Response.Text)
        return
    }
    Add-Result -Check $Check -Expect $expectText -Got "HTTP $($Response.Status)" -Outcome PASS -Excerpt (Format-Excerpt $Response.Text)
}

try {
    Write-WastedStep "bridge-smoke against $BaseUrl (CONTRACTS.md v1.1 §1)."

    # --- GET /health, GET /state --------------------------------------------------------------
    $health = Invoke-Bridge -Method GET -Path '/health'
    Add-HttpResult -Check 'GET /health' -Response $health -ExpectStatus 200 `
        -RequireKeys @('version', 'edition', 'tick_hz', 'queue_depth', 'game_fps', 'online_blocked')

    $state = Invoke-Bridge -Method GET -Path '/state'
    Add-HttpResult -Check 'GET /state' -Response $state -ExpectStatus 200 `
        -RequireKeys @('ts', 'tick', 'player', 'vehicle', 'location', 'world', 'mission', 'nearby', 'last_task', 'bridge')

    # Base coordinates for movement tasks come from the live snapshot so targets are real
    # world positions near the player; harmless fallback of 0,0,0 if /state failed.
    $base = @{ x = 0.0; y = 0.0; z = 0.0 }
    $playerPos = Get-JsonProp (Get-JsonProp $state.Json 'player') 'pos'
    foreach ($axis in 'x', 'y', 'z') {
        $value = Get-JsonProp $playerPos $axis
        if ($null -ne $value) { $base[$axis] = [double]$value }
    }

    $followHandle = $null
    $nearby = Get-JsonProp $state.Json 'nearby'
    foreach ($kind in 'peds', 'vehicles') {
        if ($null -ne $followHandle) { break }
        $entities = @(Get-JsonProp $nearby $kind)
        if ($entities.Count -gt 0) {
            $handle = Get-JsonProp $entities[0] 'handle'
            if ($null -ne $handle) { $followHandle = [long]$handle }
        }
    }

    # --- POST /task: all 11 contract task types (each preempts the previous) -------------------
    $taskChecks = @(
        @{ Type = 'walk_to';                Params = @{ x = $base.x + 5; y = $base.y + 5; z = $base.z; run = $false } }
        @{ Type = 'enter_nearest_vehicle';  Params = @{ prefer = 'any'; search_radius_m = 30 } }
        @{ Type = 'drive_to';               Params = @{ x = $base.x + 150; y = $base.y + 150; z = $base.z; speed_mps = 15.0; style = 'normal'; arrive_radius_m = 8.0 } }
        @{ Type = 'wander_drive';           Params = @{ style = 'normal' } }
        @{ Type = 'flee_police';            Params = @{} }
        @{ Type = 'combat_hated_targets_around'; Params = @{ radius_m = 40.0 } }
        @{ Type = 'seek_cover';             Params = @{ duration_s = 10 } }
        @{ Type = 'follow_entity';          Params = $null }   # filled below when a handle exists
        @{ Type = 'set_waypoint';           Params = @{ x = $base.x + 500; y = $base.y + 500 } }
        @{ Type = 'exit_vehicle';           Params = @{} }
    )

    foreach ($task in $taskChecks) {
        if ($task.Type -eq 'follow_entity') {
            if ($null -eq $followHandle) {
                Add-Result -Check 'POST /task follow_entity' -Expect 'HTTP 202 + task_id' `
                    -Got 'no nearby ped/vehicle handle in /state — cannot build a real handle' -Outcome SKIP
                continue
            }
            $task.Params = @{ handle = $followHandle; in_vehicle = $false }
        }
        $resp = Invoke-Bridge -Method POST -Path '/task' -Body @{ type = $task.Type; params = $task.Params }
        $taskId = Get-JsonProp $resp.Json 'task_id'
        if ($resp.Status -eq 202 -and $taskId -and "$taskId" -match '^t-') {
            Add-Result -Check "POST /task $($task.Type)" -Expect 'HTTP 202 + task_id' `
                -Got "HTTP 202, task_id=$taskId" -Outcome PASS -Excerpt (Format-Excerpt $resp.Text)
        }
        else {
            $got = if ($resp.Err) { "error: $($resp.Err)" } else { "HTTP $($resp.Status), task_id=$taskId" }
            Add-Result -Check "POST /task $($task.Type)" -Expect 'HTTP 202 + task_id' `
                -Got $got -Outcome FAIL -Excerpt (Format-Excerpt $resp.Text)
        }
        Start-Sleep -Seconds $InterTaskDelayS
    }

    # --- negative check: unknown task type must 400 --------------------------------------------
    $bad = Invoke-Bridge -Method POST -Path '/task' -Body @{ type = 'fly_to_moon'; params = @{} }
    Add-HttpResult -Check 'POST /task <unknown type> (negative)' -Response $bad -ExpectStatus 400

    # --- /timescale set + restore ---------------------------------------------------------------
    $ts1 = Invoke-Bridge -Method POST -Path '/timescale' -Body @{ value = 0.5 }
    Add-HttpResult -Check 'POST /timescale {value:0.5}' -Response $ts1 -ExpectStatus 200 -RequireKeys @('value')
    $ts2 = Invoke-Bridge -Method POST -Path '/timescale' -Body @{ value = 1.0 }
    Add-HttpResult -Check 'POST /timescale {value:1.0} (restore)' -Response $ts2 -ExpectStatus 200 -RequireKeys @('value')

    # --- /control off + on ----------------------------------------------------------------------
    $ctrlOff = Invoke-Bridge -Method POST -Path '/control' -Body @{ enabled = $false }
    Add-HttpResult -Check 'POST /control {enabled:false}' -Response $ctrlOff -ExpectStatus 200 -RequireKeys @('enabled')
    $ctrlOn = Invoke-Bridge -Method POST -Path '/control' -Body @{ enabled = $true }
    Add-HttpResult -Check 'POST /control {enabled:true} (restore)' -Response $ctrlOn -ExpectStatus 200 -RequireKeys @('enabled')

    # --- /radio, /horn --------------------------------------------------------------------------
    $radio = Invoke-Bridge -Method POST -Path '/radio' -Body @{ station = 'off' }
    Add-HttpResult -Check 'POST /radio {station:"off"}' -Response $radio -ExpectStatus 200
    $horn = Invoke-Bridge -Method POST -Path '/horn' -Body @{ ms = 250 }
    Add-HttpResult -Check 'POST /horn {ms:250}' -Response $horn -ExpectStatus 200

    # --- /unstick: 200, or 409 unstick_conditions_not_met — both are contract-conform ----------
    $unstick = Invoke-Bridge -Method POST -Path '/unstick'
    if ($unstick.Err) {
        Add-Result -Check 'POST /unstick' -Expect 'HTTP 200 or 409 unstick_conditions_not_met' `
            -Got "error: $($unstick.Err)" -Outcome FAIL
    }
    elseif ($unstick.Status -eq 200 -and $null -ne (Get-JsonProp $unstick.Json 'moved')) {
        Add-Result -Check 'POST /unstick' -Expect 'HTTP 200 or 409 unstick_conditions_not_met' `
            -Got "HTTP 200, moved=$(Get-JsonProp $unstick.Json 'moved')" -Outcome PASS -Excerpt (Format-Excerpt $unstick.Text)
    }
    elseif ($unstick.Status -eq 409 -and (Get-JsonProp $unstick.Json 'error') -eq 'unstick_conditions_not_met') {
        Add-Result -Check 'POST /unstick' -Expect 'HTTP 200 or 409 unstick_conditions_not_met' `
            -Got 'HTTP 409, unstick_conditions_not_met (preconditions correctly enforced)' -Outcome PASS -Excerpt (Format-Excerpt $unstick.Text)
    }
    else {
        Add-Result -Check 'POST /unstick' -Expect 'HTTP 200 or 409 unstick_conditions_not_met' `
            -Got "HTTP $($unstick.Status)" -Outcome FAIL -Excerpt (Format-Excerpt $unstick.Text)
    }

    # --- stop (11th task type) + verify idle ----------------------------------------------------
    $stop = Invoke-Bridge -Method POST -Path '/task' -Body @{ type = 'stop'; params = @{} }
    Add-HttpResult -Check 'POST /task stop' -Response $stop -ExpectStatus 202 -RequireKeys @('task_id')
    Start-Sleep -Seconds 2
    $finalState = Invoke-Bridge -Method GET -Path '/state'
    $lastStatus = Get-JsonProp (Get-JsonProp $finalState.Json 'last_task') 'status'
    if ($finalState.Status -eq 200 -and $lastStatus -eq 'idle') {
        Add-Result -Check 'GET /state after stop' -Expect 'last_task.status == idle' `
            -Got "last_task.status=$lastStatus" -Outcome PASS
    }
    else {
        $got = if ($finalState.Err) { "error: $($finalState.Err)" } else { "HTTP $($finalState.Status), last_task.status=$lastStatus" }
        Add-Result -Check 'GET /state after stop' -Expect 'last_task.status == idle' -Got $got -Outcome FAIL `
            -Excerpt (Format-Excerpt $finalState.Text)
    }

    # --- report (paste-ready for docs/STATUS.md) ------------------------------------------------
    $passed = @($results | Where-Object Outcome -eq 'PASS').Count
    $failed = @($results | Where-Object Outcome -eq 'FAIL').Count
    $skipped = @($results | Where-Object Outcome -eq 'SKIP').Count
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

    Write-Host ''
    Write-Host "### bridge-smoke — $stamp — $BaseUrl"
    Write-Host ''
    Write-Host '| # | Check | Expect | Got | Result | Response excerpt |'
    Write-Host '|---|---|---|---|---|---|'
    foreach ($row in $results) {
        Write-Host ('| {0} | {1} | {2} | {3} | {4} | {5} |' -f $row.Num, $row.Check, $row.Expect,
            (Format-Excerpt $row.Got), $row.Outcome, $row.Excerpt)
    }
    Write-Host ''
    Write-Host "**Result: $passed passed, $failed failed, $skipped skipped (of $($results.Count) checks).**"
    Write-Host ''

    if ($failed -gt 0) {
        Write-WastedError "$failed check(s) FAILED."
        exit 1
    }
    Write-WastedStep 'All executed checks passed.'
    exit 0
}
catch {
    Write-WastedError "bridge-smoke.ps1 crashed: $($_.Exception.Message)"
    Write-WastedError $_.ScriptStackTrace
    exit 1
}
finally {
    Stop-WastedTranscript
}
