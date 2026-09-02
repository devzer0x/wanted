#Requires -Version 7.0
<#
.SYNOPSIS
    Dot-source library: find the game window, prove who owns the foreground, and take it back.
.DESCRIPTION
    Dot-source only (". $PSScriptRoot/window-focus.ps1") — defines functions, no side effects
    until something is called. Windows-only; every function throws a clear error off Windows.

    Why this exists. Closing Remote Desktop parks the session; `console-keepalive.ps1` re-attaches
    it with tscon, and after that re-attach NOTHING on this machine re-asserts which window is
    foreground. That matters twice over:

      * GTA V's `PauseOnFocusLoss` video preference (RAGE profile setting
        PREF_VID_PAUSE_ON_FOCUS_LOSS, shipped ON) freezes the game when its window is not
        focused — the stream then shows a still frame with no encoder fault at all.
      * SendInput reaches only the FOREGROUND window of the caller's desktop, so an unfocused
        game also cannot be driven. The harness already learned this the hard way
        (harness/wasted_harness/primitives.py: foreground window observed as '' at the exact
        moment a recovery keypress needed to land).

    Focus is taken the way Win32 actually allows it, not the way that looks right:

      SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0)   drop the foreground lock
      ShowWindow(hwnd, SW_RESTORE)                             un-minimize
      AttachThreadInput(me, current-foreground-thread, TRUE)   share input state
      BringWindowToTop(hwnd) + SetForegroundWindow(hwnd)
      AttachThreadInput(..., FALSE)
      GetForegroundWindow() -> GetWindowThreadProcessId()      VERIFY BY PID
      SwitchToThisWindow(hwnd, TRUE)                           documented-unsupported fallback

    Three deliberate refusals:
      * The return value of SetForegroundWindow is never trusted. Microsoft: "It is possible for
        a process to be denied the right to set the foreground window even if it meets these
        conditions." Verification is GetForegroundWindow's owning PID, nothing else.
      * Verification is by PID, never by window title. GAME_WINDOW_TITLES in the harness is
        flagged in-code as never confirmed against a running game; a title check can report a
        false failure on a correctly focused window.
      * No synthetic ALT keystroke. The "tap ALT so you own the last input event" trick works,
        but on this box SendInput scancodes are the only input path the game accepts, so a fake
        ALT is a real keypress delivered into the game. AttachThreadInput does the same job
        without pressing anything.

    AllowSetForegroundWindow is deliberately absent: it is called BY the process that already
    owns the foreground to grant the right to another PID, so it cannot be invoked on the game's
    behalf.

    None of this touches OBS, and none of it starts, stops or schedules the harness.
.NOTES
    Every function here must run INSIDE the interactive session (session 1). A SYSTEM/session-0
    caller gets a non-interactive window station and can neither enumerate nor focus session 1's
    windows (AttachThreadInput: "You cannot attach a thread to a thread in another desktop").
    Callers are expected to check that themselves; Test-WastedInteractiveSession is provided.
#>

Set-StrictMode -Version Latest

# --- Win32 constants (values from the Microsoft API docs cited in the header) ------------------
$script:WF_SW_RESTORE                   = 9        # ShowWindow
$script:WF_SPI_SETSCREENSAVEACTIVE      = 0x0011
$script:WF_SPI_SETSCREENSAVETIMEOUT     = 0x000F
$script:WF_SPI_GETSCREENSAVEACTIVE      = 0x0010
$script:WF_SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
$script:WF_SPIF_UPDATEINIFILE           = 0x01
$script:WF_SPIF_SENDCHANGE              = 0x02
$script:WF_UOI_NAME                     = 2
$script:WF_DESKTOP_READOBJECTS          = 0x0001
$script:WF_ERROR_ACCESS_DENIED          = 5

function Initialize-WastedWindowNative {
    <#
    Compile the P/Invoke surface once per process. Idempotent: Add-Type throws if the type
    already exists, so the type is probed first (this library is dot-sourced by a scheduled task
    that may run every minute — recompiling per call would be pure waste).
    #>
    [CmdletBinding()]
    param()

    if (-not $IsWindows) {
        throw 'window-focus.ps1 is Windows-only: it P/Invokes user32.dll on the interactive desktop.'
    }
    if ('Wasted.WindowNative' -as [type]) { return }

    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

namespace Wasted
{
    public static class WindowNative
    {
        [DllImport("user32.dll", SetLastError = true)]
        public static extern IntPtr GetForegroundWindow();

        [DllImport("user32.dll", SetLastError = true)]
        public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool BringWindowToTop(IntPtr hWnd);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool IsIconic(IntPtr hWnd);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool IsWindow(IntPtr hWnd);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo,
                                                    [MarshalAs(UnmanagedType.Bool)] bool fAttach);

        [DllImport("kernel32.dll")]
        public static extern uint GetCurrentThreadId();

        [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "FindWindowW")]
        public static extern IntPtr FindWindowW(string lpClassName, string lpWindowName);

        [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "GetWindowTextW")]
        public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

        [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "SystemParametersInfoW")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool SystemParametersInfoW(uint uiAction, uint uiParam, IntPtr pvParam, uint fWinIni);

        [DllImport("user32.dll", SetLastError = true)]
        public static extern IntPtr OpenInputDesktop(uint dwFlags,
                                                     [MarshalAs(UnmanagedType.Bool)] bool fInherit,
                                                     uint dwDesiredAccess);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool CloseDesktop(IntPtr hDesktop);

        [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "GetUserObjectInformationW")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetUserObjectInformationW(IntPtr hObj, int nIndex, byte[] pvInfo,
                                                            uint nLength, out uint lpnLengthNeeded);

        [DllImport("user32.dll", SetLastError = true)]
        public static extern void SwitchToThisWindow(IntPtr hWnd, [MarshalAs(UnmanagedType.Bool)] bool fUnknown);
    }
}
'@
}

function Test-WastedInteractiveSession {
    <#
    True when this process is in an interactive Windows session (has a window station that owns
    a desktop). False for session 0 / SYSTEM scheduled tasks, where nothing in this file works.
    #>
    [CmdletBinding()]
    param()
    $name = $env:SESSIONNAME
    if (-not $name) { return $false }                      # services and SSH shells have none
    if ($name -eq 'Services') { return $false }
    return $true
}

function Get-WastedInputDesktopName {
    <#
    Name of the desktop currently receiving user input: 'Default' when the user's desktop is up,
    'Winlogon' when the secure desktop owns it (locked / Ctrl+Alt+Del / UAC prompt).

    A locked session renders the lock screen to the virtual display, which on the stream is
    indistinguishable from a frozen game — and no amount of SetForegroundWindow helps, because
    the game's desktop is not the input desktop. So callers abort on anything but 'Default'.

    Returns @{ Name; Win32Error }. Name is $null when the desktop could not be opened at all;
    Win32Error 5 (ACCESS_DENIED) is the normal signature of Winlogon owning the input desktop.

    Caveat, from the OpenInputDesktop docs: called from a DISCONNECTED session it returns "a
    handle to the desktop that becomes active when the user restores the connection" — so a
    reading taken while the session is parked is a prediction, not an observation.
    #>
    [CmdletBinding()]
    param()
    Initialize-WastedWindowNative

    $h = [Wasted.WindowNative]::OpenInputDesktop(0, $false, $script:WF_DESKTOP_READOBJECTS)
    if ($h -eq [IntPtr]::Zero) {
        return [pscustomobject]@{ Name = $null; Win32Error = [Runtime.InteropServices.Marshal]::GetLastWin32Error() }
    }
    try {
        $buffer = [byte[]]::new(512)
        $needed = [uint32]0
        $ok = [Wasted.WindowNative]::GetUserObjectInformationW($h, $script:WF_UOI_NAME, $buffer, [uint32]$buffer.Length, [ref]$needed)
        if (-not $ok) {
            return [pscustomobject]@{ Name = $null; Win32Error = [Runtime.InteropServices.Marshal]::GetLastWin32Error() }
        }
        $text = [System.Text.Encoding]::Unicode.GetString($buffer, 0, $buffer.Length)
        $nul = $text.IndexOf([char]0)
        if ($nul -ge 0) { $text = $text.Substring(0, $nul) }
        return [pscustomobject]@{ Name = $text; Win32Error = 0 }
    }
    finally {
        [void][Wasted.WindowNative]::CloseDesktop($h)
    }
}

function Get-WastedForegroundWindow {
    <#
    Who owns the foreground right now: @{ Hwnd; ProcessId; ProcessName; Title }.

    Hwnd can legitimately be zero — GetForegroundWindow "can be NULL in certain circumstances,
    such as when a window is losing activation" — and an empty foreground is exactly the state
    the harness observed live. Zero is reported, not treated as an error.
    #>
    [CmdletBinding()]
    param()
    Initialize-WastedWindowNative

    $hwnd = [Wasted.WindowNative]::GetForegroundWindow()
    if ($hwnd -eq [IntPtr]::Zero) {
        return [pscustomobject]@{ Hwnd = [IntPtr]::Zero; ProcessId = 0; ProcessName = ''; Title = '' }
    }
    $procId = [uint32]0
    [void][Wasted.WindowNative]::GetWindowThreadProcessId($hwnd, [ref]$procId)

    $sb = [System.Text.StringBuilder]::new(512)
    [void][Wasted.WindowNative]::GetWindowTextW($hwnd, $sb, $sb.Capacity)

    $procName = ''
    if ($procId -ne 0) {
        $p = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
        if ($p) { $procName = $p.ProcessName }
    }
    return [pscustomobject]@{
        Hwnd        = $hwnd
        ProcessId   = [int]$procId
        ProcessName = $procName
        Title       = $sb.ToString()
    }
}

function Find-WastedGameWindow {
    <#
    Locate GTA V's top-level window. Process handle FIRST, window title only as a fallback.

    Order matters. harness/wasted_harness/primitives.py finds the window by the title
    "Grand Theft Auto V" and says so in a comment: NOT independently confirmed against a running
    game. MainWindowHandle off the actual process needs no such guess, and it hands back the PID
    that every later verification compares against. The title path stays as a fallback for the
    case where the process name differs (Legacy vs Enhanced), and whichever path matched is
    reported so the operator can finally write the true values down.

    Returns $null when nothing matched. Returns a record with Hwnd = 0 and
    WindowMissing = $true when the process IS running but owns no top-level window yet
    (still loading, or the window was destroyed) — a different diagnosis from "game not running",
    and worth telling apart at 3am.
    #>
    [CmdletBinding()]
    param(
        [string[]]$ProcessNames = @('GTA5', 'GTA5_Enhanced'),
        [string[]]$WindowTitles = @('Grand Theft Auto V')
    )
    Initialize-WastedWindowNative

    $running = @()
    foreach ($name in $ProcessNames) {
        foreach ($p in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) { $running += $p }
    }

    foreach ($p in $running) {
        if ($p.MainWindowHandle -ne [IntPtr]::Zero) {
            return [pscustomobject]@{
                Hwnd          = $p.MainWindowHandle
                ProcessId     = $p.Id
                ProcessName   = $p.ProcessName
                SessionId     = $p.SessionId
                Title         = $p.MainWindowTitle
                Source        = 'process-mainwindow'
                WindowMissing = $false
            }
        }
    }

    foreach ($title in $WindowTitles) {
        $hwnd = [Wasted.WindowNative]::FindWindowW($null, $title)
        if ($hwnd -ne [IntPtr]::Zero) {
            $procId = [uint32]0
            [void][Wasted.WindowNative]::GetWindowThreadProcessId($hwnd, [ref]$procId)
            $p = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
            $pName = ''
            $pSession = -1
            if ($p) { $pName = $p.ProcessName; $pSession = $p.SessionId }
            return [pscustomobject]@{
                Hwnd          = $hwnd
                ProcessId     = [int]$procId
                ProcessName   = $pName
                SessionId     = $pSession
                Title         = $title
                Source        = 'findwindow-title'
                WindowMissing = $false
            }
        }
    }

    if ($running.Count -gt 0) {
        $p = $running[0]
        return [pscustomobject]@{
            Hwnd          = [IntPtr]::Zero
            ProcessId     = $p.Id
            ProcessName   = $p.ProcessName
            SessionId     = $p.SessionId
            Title         = ''
            Source        = 'process-without-window'
            WindowMissing = $true
        }
    }
    return $null
}

function Test-WastedGameForeground {
    <#
    Is the game the foreground window right now? Compared by PID, never by title.
    Returns @{ IsForeground; Foreground } where Foreground is Get-WastedForegroundWindow's record.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][int]$GameProcessId)

    $fg = Get-WastedForegroundWindow
    return [pscustomobject]@{
        IsForeground = ($GameProcessId -gt 0 -and $fg.ProcessId -eq $GameProcessId)
        Foreground   = $fg
    }
}

function Set-WastedGameForeground {
    <#
    Take the foreground for the given window and PROVE it, or say plainly that it failed.

    Returns a record:
      Success        bool   — verified by foreground PID, not by any API return value
      Method         string — 'already' | 'setforegroundwindow' | 'switchtothiswindow' | 'failed'
      Rc             bool   — what SetForegroundWindow claimed (logged, never trusted)
      Attached       bool   — whether AttachThreadInput to the outgoing foreground thread worked
      LockTimeoutSet bool   — whether SPI_SETFOREGROUNDLOCKTIMEOUT=0 was accepted this call
      Before/After   the foreground records either side of the attempt
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][IntPtr]$Hwnd,
        [Parameter(Mandatory)][int]$GameProcessId
    )
    Initialize-WastedWindowNative

    $before = Get-WastedForegroundWindow
    if ($GameProcessId -gt 0 -and $before.ProcessId -eq $GameProcessId) {
        return [pscustomobject]@{
            Success = $true; Method = 'already'; Rc = $true; Attached = $false
            LockTimeoutSet = $false; Before = $before; After = $before
        }
    }
    if (-not $PSCmdlet.ShouldProcess("window 0x$($Hwnd.ToString('X')) (pid $GameProcessId)", 'set foreground')) {
        return [pscustomobject]@{
            Success = $false; Method = 'skipped-whatif'; Rc = $false; Attached = $false
            LockTimeoutSet = $false; Before = $before; After = $before
        }
    }

    # Drop the foreground lock for this call. Documented chicken-and-egg: "The calling thread
    # must be able to change the foreground window, otherwise the call fails" — so a $false here
    # is informational, and the persistent HKCU ForegroundLockTimeout value (applied by
    # leave-safely.ps1) is the belt to this braces.
    $lockSet = [Wasted.WindowNative]::SystemParametersInfoW(
        [uint32]$script:WF_SPI_SETFOREGROUNDLOCKTIMEOUT, [uint32]0, [IntPtr]::Zero,
        [uint32]($script:WF_SPIF_UPDATEINIFILE -bor $script:WF_SPIF_SENDCHANGE))

    [void][Wasted.WindowNative]::ShowWindow($Hwnd, $script:WF_SW_RESTORE)

    $myTid = [Wasted.WindowNative]::GetCurrentThreadId()
    $fgTid = [uint32]0
    if ($before.Hwnd -ne [IntPtr]::Zero) {
        $tmpPid = [uint32]0
        $fgTid = [Wasted.WindowNative]::GetWindowThreadProcessId($before.Hwnd, [ref]$tmpPid)
    }

    $attached = $false
    if ($fgTid -ne 0 -and $fgTid -ne $myTid) {
        $attached = [Wasted.WindowNative]::AttachThreadInput($myTid, $fgTid, $true)
    }
    try {
        [void][Wasted.WindowNative]::BringWindowToTop($Hwnd)
        $rc = [Wasted.WindowNative]::SetForegroundWindow($Hwnd)
    }
    finally {
        if ($attached) { [void][Wasted.WindowNative]::AttachThreadInput($myTid, $fgTid, $false) }
    }

    Start-Sleep -Milliseconds 250
    $after = Get-WastedForegroundWindow
    if ($GameProcessId -gt 0 -and $after.ProcessId -eq $GameProcessId) {
        return [pscustomobject]@{
            Success = $true; Method = 'setforegroundwindow'; Rc = $rc; Attached = $attached
            LockTimeoutSet = $lockSet; Before = $before; After = $after
        }
    }

    # Last resort. Microsoft: "not intended for general use. It may be altered or unavailable in
    # subsequent versions of Windows." Kept behind the supported path, never as the primary.
    [Wasted.WindowNative]::SwitchToThisWindow($Hwnd, $true)
    Start-Sleep -Milliseconds 250
    $after = Get-WastedForegroundWindow
    $ok = ($GameProcessId -gt 0 -and $after.ProcessId -eq $GameProcessId)
    return [pscustomobject]@{
        Success = $ok; Method = ($ok ? 'switchtothiswindow' : 'failed'); Rc = $rc; Attached = $attached
        LockTimeoutSet = $lockSet; Before = $before; After = $after
    }
}

function Disable-WastedScreenSaverLive {
    <#
    Turn the screen saver off in the RUNNING session, immediately.

    The registry values under HKCU\Control Panel\Desktop only bind at the next logon, and on a
    box with "Interactive logon: Machine inactivity limit" configured, a screen saver starting is
    itself a documented lock trigger. This calls SPI_SETSCREENSAVEACTIVE / SPI_SETSCREENSAVETIMEOUT
    so the current session cannot start one before the next reboot.

    Returns @{ ActiveSet; TimeoutSet; StillActive } — StillActive is the read-back through
    SPI_GETSCREENSAVEACTIVE, i.e. what the session actually believes now.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()
    Initialize-WastedWindowNative

    if (-not $PSCmdlet.ShouldProcess('current session', 'disable the screen saver via SystemParametersInfo')) {
        return [pscustomobject]@{ ActiveSet = $false; TimeoutSet = $false; StillActive = $null }
    }
    $flags = [uint32]($script:WF_SPIF_UPDATEINIFILE -bor $script:WF_SPIF_SENDCHANGE)
    $activeSet = [Wasted.WindowNative]::SystemParametersInfoW([uint32]$script:WF_SPI_SETSCREENSAVEACTIVE, [uint32]0, [IntPtr]::Zero, $flags)
    $timeoutSet = [Wasted.WindowNative]::SystemParametersInfoW([uint32]$script:WF_SPI_SETSCREENSAVETIMEOUT, [uint32]0, [IntPtr]::Zero, $flags)

    $stillActive = $null
    $buf = [System.Runtime.InteropServices.Marshal]::AllocHGlobal(4)
    try {
        [System.Runtime.InteropServices.Marshal]::WriteInt32($buf, 0)
        if ([Wasted.WindowNative]::SystemParametersInfoW([uint32]$script:WF_SPI_GETSCREENSAVEACTIVE, [uint32]0, $buf, [uint32]0)) {
            $stillActive = ([System.Runtime.InteropServices.Marshal]::ReadInt32($buf) -ne 0)
        }
    }
    finally {
        [System.Runtime.InteropServices.Marshal]::FreeHGlobal($buf)
    }
    return [pscustomobject]@{ ActiveSet = $activeSet; TimeoutSet = $timeoutSet; StillActive = $stillActive }
}
