# WASTED: deploy the fun-to-watch program to the box. Run ON the box in an elevated
# PowerShell after uploading C:\wasted\tmp\WastedBridge.dll (bridge 1.7.0) and
# C:\wasted\tmp\harness-today.tgz (the wasted_harness/ package). Never touches OBS,
# the game process, the stream, or the box's .env keys. See docs/go-live.md.
$ErrorActionPreference = 'Continue'
# PowerShell variable names are CASE-INSENSITIVE. Never name a local $h beside a
# path in $H — that collision silently redirected a tar extraction once already.
$HarnessDir = 'C:\wasted\harness'
$py = "$HarnessDir\.venv\Scripts\python.exe"
$GameDir = 'C:\Program Files (x86)\Steam\steamapps\common\Grand Theft Auto V'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

Write-Output '=== 0. the game must be alive and TICKING before we touch anything ==='
try {
    $before = Invoke-RestMethod 'http://127.0.0.1:7777/health' -TimeoutSec 5
    Write-Output ("bridge={0} tick_hz={1} fps={2}" -f $before.version, $before.tick_hz, $before.game_fps)
    if ($before.tick_hz -le 0) { Write-Output 'FATAL: the game is paused. Fix that first — deploying now only adds a variable.'; exit 1 }
} catch { Write-Output 'FATAL: bridge unreachable'; exit 1 }

Write-Output ''
Write-Output '=== 1. stop the harness (game, OBS and the stream are untouched) ==='
Stop-ScheduledTask -TaskName 'WASTED-Harness' -ErrorAction SilentlyContinue
@(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'wasted_harness' }) |
    ForEach-Object { Write-Output ("  stopping pid {0}" -f $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

Write-Output '=== 2. bridge 1.7.0 -> scripts folder, then HOT RELOAD (no game restart) ==='
# ReloadKeyBinding was set to Insert last session, so SHVDN can swap the DLL in
# place. bridge/README documents that the listener is released on Aborted, so the
# HTTP port frees itself and a reload heals rather than conflicting.
$dst = Join-Path $GameDir 'scripts\WastedBridge.dll'
if (Test-Path 'C:\wasted\tmp\WastedBridge.dll') {
    if (Test-Path $dst) { Copy-Item $dst "$dst.bak-$stamp" -Force }
    Copy-Item 'C:\wasted\tmp\WastedBridge.dll' $dst -Force
    Get-Item $dst | Select-Object Name, Length, LastWriteTime | Format-List | Out-String

    $reload = @(
        'import time, json, urllib.request',
        'from wasted_harness import primitives as P',
        'EXT = 0x0001   # KEYEVENTF_EXTENDEDKEY - Insert is an extended key',
        'def key(scan, down):',
        '    flags = P._KEYEVENTF_SCANCODE | EXT | (0 if down else P._KEYEVENTF_KEYUP)',
        '    return P.INPUT(type=P._INPUT_KEYBOARD, union=P._INPUTUNION(ki=P.KEYBDINPUT(0, scan, flags, 0, 0)))',
        'p = P.Primitives()',
        'print("focus:", p.focus_game_window(), flush=True)',
        'time.sleep(2)',
        'print("foreground:", P.foreground_window_title(), flush=True)',
        'P._send([key(0x52, True)]); time.sleep(0.08); P._send([key(0x52, False)])',
        'print("insert sent", flush=True)',
        'time.sleep(10)',
        'try:',
        '    with urllib.request.urlopen("http://127.0.0.1:7777/health", timeout=5) as r:',
        '        print("health after reload:", json.load(r), flush=True)',
        'except Exception as e:',
        '    print("health error:", e, flush=True)'
    )
    Set-Content -Path 'C:\wasted\tmp\reload.py' -Value $reload -Encoding ASCII
    $rlog = 'C:\wasted\logs\reload.log'
    Remove-Item $rlog -ErrorAction SilentlyContinue
    $ra = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -Command `"Start-Process -FilePath '$py' -ArgumentList 'C:\wasted\tmp\reload.py' -WorkingDirectory '$HarnessDir' -WindowStyle Hidden -Wait -RedirectStandardOutput '$rlog' -RedirectStandardError 'C:\wasted\logs\reload.err'`""
    $rp = New-ScheduledTaskPrincipal -UserId 'Administrator' -LogonType Interactive -RunLevel Highest
    Unregister-ScheduledTask -TaskName 'WASTED-Reload' -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName 'WASTED-Reload' -Action $ra -Principal $rp | Out-Null
    Start-ScheduledTask -TaskName 'WASTED-Reload'
    Start-Sleep -Seconds 22
    if (Test-Path $rlog) { Get-Content $rlog }
} else { Write-Output 'no bridge DLL uploaded - skipping (harness will run against the old bridge)' }

Write-Output ''
Write-Output '=== 3. harness package ==='
if (-not (Test-Path 'C:\wasted\tmp\harness-today.tgz')) { Write-Output 'FATAL: harness package not uploaded'; exit 1 }
New-Item -ItemType Directory -Force -Path 'C:\wasted\deploy' | Out-Null
Copy-Item "$HarnessDir\wasted_harness" "C:\wasted\deploy\wasted_harness-$stamp" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$HarnessDir\wasted_harness" -ErrorAction SilentlyContinue
tar -xzf 'C:\wasted\tmp\harness-today.tgz' -C $HarnessDir

Write-Output '=== 3b. missions OFF (operator call: not ready for jobs yet) ==='
# Appended to the SERVER's own .env — never overwritten from a dev machine, it
# holds the real API and Supabase keys. Idempotent.
$envFile = Join-Path $HarnessDir '.env'
if ((Get-Content $envFile -Raw) -notmatch 'WASTED_MISSIONS_ENABLED') {
    Add-Content $envFile "`n# Operator call 2026-09-03: roam only until he is ready for jobs.`nWASTED_MISSIONS_ENABLED=false"
}
Get-Content $envFile | Select-String 'WASTED_MISSIONS_ENABLED'

Write-Output '=== 4. GATE: the new brain must import and be the one we shipped ==='
$probe = & $py -c "from wasted_harness.behavior.roam import CATALOG, UNBUILDABLE_GOALS, RoamEngine; from wasted_harness.behavior.vehicle import MovementWheel, ControlRegained; import wasted_harness.brain.knowledge_base as kb; k=kb.load_all(); from wasted_harness.settings import Settings; from wasted_harness.bridge_client import GameState, BRIDGE_TASK_TYPES; from wasted_harness.behavior.recovery import ClearedByGameBackoff, threat_action; from wasted_harness.main import Harness; assert hasattr(MovementWheel,'acquire'); assert 'phone' in GameState.model_fields; assert 'flee_ped' in BRIDGE_TASK_TYPES and 'answer_call' in BRIDGE_TASK_TYPES; assert hasattr(RoamEngine,'heat_is_the_goal'); assert hasattr(Harness,'_restart_locked_plan') and hasattr(Harness,'_wait_has_a_reason'); print('GOALS',len(CATALOG),'IMPOSSIBLE',len(UNBUILDABLE_GOALS),'ITEMS',sum(len(v) for v in k.values()),'MISSIONS',Settings.load().missions_enabled,'WHEEL ok')" 2>&1
Write-Output ($probe -join ' ')
if (($probe -join ' ') -notmatch 'WHEEL ok') {
    Write-Output 'FATAL: new brain did not import — restoring the backup and leaving him on the old build'
    Remove-Item -Recurse -Force "$HarnessDir\wasted_harness" -ErrorAction SilentlyContinue
    Copy-Item "C:\wasted\deploy\wasted_harness-$stamp" "$HarnessDir\wasted_harness" -Recurse -Force
    exit 1
}

Write-Output ''
Write-Output '=== 5. start ONE harness, hidden, unbuffered ==='
# Hidden everywhere: a visible cmd window steals the foreground, which pauses the
# game AND swallows every SendInput keypress meant for it. -u so the log is
# readable while it runs rather than block-buffered into silence.
$log = "C:\wasted\logs\harness-$stamp.log"
$act = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -Command `"Start-Process -FilePath '$py' -ArgumentList '-u','-m','wasted_harness.main' -WorkingDirectory '$HarnessDir' -WindowStyle Hidden -RedirectStandardOutput '$log' -RedirectStandardError '$log.err'`""
$prin = New-ScheduledTaskPrincipal -UserId 'Administrator' -LogonType Interactive -RunLevel Highest
Unregister-ScheduledTask -TaskName 'WASTED-Harness' -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName 'WASTED-Harness' -Action $act -Principal $prin | Out-Null
Start-ScheduledTask -TaskName 'WASTED-Harness'
Start-Sleep -Seconds 50

Write-Output ''
Write-Output '=== 6. verify ==='
# TWO python processes is CORRECT: the venv python.exe is a ~4 MB trampoline that
# execs the base interpreter as a child. The child (~160 MB, 20+ threads) is the
# real harness. Killing it as a "duplicate" takes the brain down — done once already.
$procs = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'wasted_harness' })
Write-Output ("harness processes: {0} (2 is correct: trampoline + worker)" -f $procs.Count)
try { $after = Invoke-RestMethod 'http://127.0.0.1:7777/health' -TimeoutSec 5; Write-Output ("bridge={0} tick_hz={1} fps={2}" -f $after.version, $after.tick_hz, $after.game_fps) } catch { Write-Output 'bridge unreachable' }
if (Test-Path "$log.err") { Write-Output '--- log tail (structured logging writes to stderr) ---'; Get-Content "$log.err" -Tail 30 }
elseif (Test-Path $log) { Get-Content $log -Tail 30 }
