"""Manual-control primitives via SendInput keyboard/mouse (CONTRACTS §2, D9).

GTA V is fully keyboard-playable; ViGEmBus/vgamepad is archived with known
Code-28 failures on Windows Server, so keyboard/mouse-first is the plan of
record. Everything here is win32-only and fails loudly elsewhere — primitives
are physical inputs to a real game window; there is nothing to simulate.

Scan codes are sent as hardware scan codes (KEYEVENTF_SCANCODE) because GTA V
reads DirectInput-level input and ignores plain virtual-key events.
"""

from __future__ import annotations

import random
import sys
import time
from typing import Any

from .logsetup import get_logger

log = get_logger("wasted.primitives")


class PrimitivesUnavailableError(RuntimeError):
    """SendInput primitives need Windows; message names the platform."""


# DirectInput scan codes (US layout) for the keys the primitives use.
SCAN = {
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


def _require_win32() -> None:
    if sys.platform != "win32":
        raise PrimitivesUnavailableError(
            f"manual-control primitives use SendInput and only work on Windows; "
            f"platform is {sys.platform!r}. On the game server this module drives "
            f"the real keyboard/mouse."
        )


def _send_key(scan: int, down: bool) -> None:
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 40)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    flags = _KEYEVENTF_SCANCODE | (0 if down else _KEYEVENTF_KEYUP)
    inp = INPUT(
        type=_INPUT_KEYBOARD,
        union=_INPUTUNION(ki=KEYBDINPUT(0, scan, flags, 0, None)),
    )
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        raise PrimitivesUnavailableError(
            f"SendInput reported {sent} of 1 events injected "
            f"(GetLastError={ctypes.get_last_error()}); is the session interactive?"
        )


def _send_mouse_move(dx: int, dy: int) -> None:
    import ctypes
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("padding", ctypes.c_byte * 40)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    inp = INPUT(
        type=_INPUT_MOUSE,
        union=_INPUTUNION(mi=MOUSEINPUT(dx, dy, 0, _MOUSEEVENTF_MOVE, 0, None)),
    )
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _hold_key(key: str, ms: int) -> None:
    scan = SCAN[key]
    _send_key(scan, True)
    try:
        time.sleep(ms / 1000.0)
    finally:
        _send_key(scan, False)


class Primitives:
    """Executes the §2 manual-control primitive actions.

    `radio` and `horn` are routed through the bridge endpoints by main (they
    exist as bridge HTTP calls); the rest are raw inputs here.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        _require_win32()
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
            _send_mouse_move(dx * 10, self._rng.randint(-8, 8))
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
        seconds = float(params.get("seconds", 5))
        time.sleep(max(0.0, min(60.0, seconds)))
