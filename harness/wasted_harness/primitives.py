"""Manual-control primitives via SendInput keyboard/mouse (CONTRACTS §2, D9).

GTA V is fully keyboard/mouse-playable, so keyboard/mouse is the plan of record
and the ONLY input path this harness has. A virtual gamepad is explicitly not
used: ViGEmBus is archived (final v1.22.0, 2023-11-02), its installer refuses
every Windows Server SKU, and its successor is not publicly available. If a
gamepad path is ever added it must stay optional and non-fatal — see
:func:`gamepad_status`, which is what ``--check`` reports.

Everything here is win32-only and fails loudly elsewhere: primitives are
physical inputs to a real game window; there is nothing to simulate.

Two constraints that are easy to get wrong and expensive to debug on a headless
server:

1. **Scan codes, not virtual keys.** GTA V reads DirectInput-level input and
   ignores plain virtual-key events, so every key goes out with
   ``KEYEVENTF_SCANCODE`` and ``wVk = 0``.
2. **``cbSize`` must equal ``sizeof(INPUT)`` exactly** (40 bytes on x64). A
   union padded to the wrong width makes ``SendInput`` return 0 for every call
   with ``ERROR_INVALID_PARAMETER`` and no other symptom. The structures below
   use fixed-width types so the layout is identical on Windows x64 and can be
   asserted from any platform (see tests/test_primitives.py).
3. **SendInput reaches the foreground window of the caller's desktop.** The
   harness must run in the same interactive console session as the game; from
   an RDP session or a service (session 0) the keys go nowhere. ``--check``
   reports the session and the foreground window title for exactly this reason.
"""

from __future__ import annotations

import ctypes
import random
import sys
import time
from typing import Any

from .logsetup import get_logger

log = get_logger("wasted.primitives")


class PrimitivesUnavailableError(RuntimeError):
    """SendInput primitives need Windows; the message names the platform."""


# DirectInput scan codes (US layout, set 1) for the keys the primitives use.
SCAN: dict[str, int] = {
    "w": 0x11,
    "s": 0x1F,
    "a": 0x1E,
    "d": 0x20,
    "e": 0x12,
}

_KEYEVENTF_SCANCODE = 0x0008
_KEYEVENTF_KEYUP = 0x0002
_INPUT_KEYBOARD = 1
_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001

# Fixed-width aliases so the layout below is byte-identical to the Windows x64
# definition and can be verified from a non-Windows dev machine.
_WORD = ctypes.c_uint16
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
_ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", _WORD),
        ("wScan", _WORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", _LONG),
        ("dy", _LONG),
        ("mouseData", _DWORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD)]


class _INPUTUNION(ctypes.Union):
    # No manual padding: the union sizes itself off its largest member
    # (MOUSEINPUT, 32 bytes on x64), which is what SendInput expects.
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", _DWORD), ("union", _INPUTUNION)]


#: sizeof(INPUT) on Windows x64. SendInput rejects any other cbSize.
INPUT_SIZE_X64 = 40

#: Longest a primitive may block its caller. Anything longer is the loop's job.
MAX_BLOCKING_WAIT_S = 2.0

_user32: Any = None


def _require_win32() -> None:
    if sys.platform != "win32":
        raise PrimitivesUnavailableError(
            f"manual-control primitives use SendInput and only work on Windows; "
            f"platform is {sys.platform!r}. On the game server this module drives "
            f"the real keyboard/mouse."
        )


def _u32() -> Any:
    """user32 with use_last_error so GetLastError() is meaningful after a failure."""
    global _user32
    if _user32 is None:
        _require_win32()
        _user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        _user32.SendInput.argtypes = (
            ctypes.c_uint,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        )
        _user32.SendInput.restype = ctypes.c_uint
    return _user32


def _send(inputs: list[INPUT]) -> None:
    """One SendInput call for a whole batch; raises when Windows drops events."""
    n = len(inputs)
    array = (INPUT * n)(*inputs)
    sent = _u32().SendInput(n, array, ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error()
        raise PrimitivesUnavailableError(
            f"SendInput injected {sent} of {n} events (GetLastError={err}). "
            f"Common causes: the harness is not in the interactive console "
            f"session (RDP or session 0), or a higher-integrity window has "
            f"focus. Foreground window: {foreground_window_title()!r}."
        )


def _key_input(scan: int, down: bool) -> INPUT:
    flags = _KEYEVENTF_SCANCODE | (0 if down else _KEYEVENTF_KEYUP)
    return INPUT(
        type=_INPUT_KEYBOARD,
        union=_INPUTUNION(ki=KEYBDINPUT(0, scan, flags, 0, 0)),
    )


def _mouse_input(dx: int, dy: int) -> INPUT:
    return INPUT(
        type=_INPUT_MOUSE,
        union=_INPUTUNION(mi=MOUSEINPUT(dx, dy, 0, _MOUSEEVENTF_MOVE, 0, 0)),
    )


def _hold_key(key: str, ms: int) -> None:
    scan = SCAN[key]
    _send([_key_input(scan, True)])
    try:
        time.sleep(ms / 1000.0)
    finally:
        _send([_key_input(scan, False)])


def foreground_window_title() -> str:
    """Title of the foreground window, or a reason string. Never raises."""
    if sys.platform != "win32":
        return f"<no windows on {sys.platform}>"
    try:
        u32 = _u32()
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return "<no foreground window>"
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value or "<untitled window>"
    except Exception as exc:  # diagnostics must never raise
        return f"<unavailable: {type(exc).__name__}>"


def session_diagnostics() -> dict[str, Any]:
    """Whether SendInput can actually reach the game. Never raises.

    ``in_console_session`` False means the harness is running in an RDP session
    or as a service; keys will not reach the game window and dxcam Desktop
    Duplication will not see the game either.
    """
    info: dict[str, Any] = {
        "platform": sys.platform,
        "supported": sys.platform == "win32",
        "session_id": None,
        "console_session_id": None,
        "in_console_session": None,
        "foreground_window": foreground_window_title(),
    }
    if sys.platform != "win32":
        return info
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        pid = k32.GetCurrentProcessId()
        sid = ctypes.c_uint32(0)
        if k32.ProcessIdToSessionId(ctypes.c_uint32(pid), ctypes.byref(sid)):
            info["session_id"] = int(sid.value)
        console = int(k32.WTSGetActiveConsoleSessionId())
        info["console_session_id"] = None if console == 0xFFFFFFFF else console
        if info["session_id"] is not None and info["console_session_id"] is not None:
            info["in_console_session"] = info["session_id"] == info["console_session_id"]
    except Exception as exc:  # diagnostics must never raise
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def gamepad_status() -> str:
    """One honest line about the (deliberately absent) virtual-gamepad path."""
    return (
        "not used by design — ViGEmBus is archived and refuses Windows Server "
        "SKUs (RESEARCH D9); keyboard/mouse SendInput is the only input path"
    )


class Primitives:
    """Executes the §2 manual-control primitive actions.

    `radio` and `horn` are routed through the bridge endpoints by main (they
    exist as bridge HTTP calls); the rest are raw inputs here.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        _require_win32()
        if ctypes.sizeof(INPUT) != INPUT_SIZE_X64:
            # A wrong cbSize makes every SendInput call fail silently-ish; refuse
            # to start rather than press keys nobody receives.
            raise PrimitivesUnavailableError(
                f"sizeof(INPUT) is {ctypes.sizeof(INPUT)}, expected "
                f"{INPUT_SIZE_X64} on Windows x64 — SendInput would reject every "
                f"call. Is this a 32-bit Python?"
            )
        _u32()  # bind user32 now so a broken environment fails at construction
        self._rng = rng or random.Random()

    def execute(self, action_type: str, params: dict[str, Any]) -> None:
        handler = {
            "look_around": self._look_around,
            "brake_tap": self._brake_tap,
            "swerve": self._swerve,
            "reverse_out": self._reverse_out,
            "press_prompt_key": self._press_prompt,
            "wait": self._wait,
        }.get(action_type)
        if handler is None:
            raise ValueError(f"{action_type!r} is not a SendInput primitive")
        log.info("primitive", extra={"kv": {"type": action_type, **params}})
        handler(params)

    def _look_around(self, _params: dict) -> None:
        # A human-ish camera sweep: right, pause, left, settle.
        for dx in (12, 12, 8, 0, -10, -14, -10, 0, 4):
            _send([_mouse_input(dx * 10, self._rng.randint(-8, 8))])
            time.sleep(self._rng.uniform(0.05, 0.12))

    def _brake_tap(self, _params: dict) -> None:
        _hold_key("s", self._rng.randint(180, 320))

    def _swerve(self, params: dict) -> None:
        direction = params.get("direction", "left")
        key = "a" if direction == "left" else "d"
        _hold_key(key, self._rng.randint(220, 380))

    def _reverse_out(self, params: dict) -> None:
        ms = int(params.get("ms", 1200))
        _hold_key("s", max(200, min(2000, ms)))

    def _press_prompt(self, _params: dict) -> None:
        _hold_key("e", self._rng.randint(80, 140))

    @staticmethod
    def _wait(params: dict) -> None:
        """Block for the requested time, bounded by MAX_BLOCKING_WAIT_S.

        The harness loop owns long waits (see main: a `wait` becomes a quiet
        period the loop honours while it keeps polling at 2-4 Hz). Blocking the
        loop for the full duration would stop perception, so a death mid-wait
        would go unseen and the site's heartbeat would go stale.
        """
        seconds = float(params.get("seconds", 5))
        time.sleep(max(0.0, min(MAX_BLOCKING_WAIT_S, seconds)))
