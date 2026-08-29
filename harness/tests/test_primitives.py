"""SendInput structure layout and platform guards.

The struct layout is the important part and it is verifiable from any platform:
every field uses a fixed-width ctypes type, so ``sizeof`` here equals ``sizeof``
on Windows x64. A wrong ``sizeof(INPUT)`` makes SendInput reject every call with
ERROR_INVALID_PARAMETER and no other symptom — the failure mode this test
exists to prevent.

Actually pressing a key is Windows-only and cannot be exercised here; that is
the Phase 2 live check on the game server.
"""

import ctypes
import sys

import pytest

from wasted_harness.primitives import (
    INPUT,
    INPUT_SIZE_X64,
    KEYBDINPUT,
    MOUSEINPUT,
    SCAN,
    Primitives,
    PrimitivesUnavailableError,
    foreground_window_title,
    gamepad_status,
    session_diagnostics,
)


def test_input_struct_matches_windows_x64_layout() -> None:
    # Win32 SDK: KEYBDINPUT 24 bytes, MOUSEINPUT 32 bytes, INPUT 40 bytes on x64.
    assert ctypes.sizeof(KEYBDINPUT) == 24
    assert ctypes.sizeof(MOUSEINPUT) == 32
    assert ctypes.sizeof(INPUT) == INPUT_SIZE_X64 == 40


def test_input_field_offsets() -> None:
    assert INPUT.type.offset == 0
    assert INPUT.union.offset == 8  # 4-byte type + 4 bytes of padding to 8-align
    assert KEYBDINPUT.wVk.offset == 0
    assert KEYBDINPUT.wScan.offset == 2
    assert KEYBDINPUT.dwFlags.offset == 4
    assert KEYBDINPUT.dwExtraInfo.offset == 16
    assert MOUSEINPUT.dx.offset == 0
    assert MOUSEINPUT.dy.offset == 4
    assert MOUSEINPUT.dwExtraInfo.offset == 24


def test_scan_codes_are_set1_directinput_values() -> None:
    # GTA V reads DirectInput scan codes; these are the set-1 US-layout values.
    assert SCAN == {"w": 0x11, "s": 0x1F, "a": 0x1E, "d": 0x20, "e": 0x12}


@pytest.mark.skipif(sys.platform == "win32", reason="off-Windows guard")
def test_primitives_refuse_to_construct_off_windows() -> None:
    with pytest.raises(PrimitivesUnavailableError, match="only work on Windows"):
        Primitives()


def test_diagnostics_never_raise() -> None:
    diag = session_diagnostics()
    assert diag["platform"] == sys.platform
    assert diag["supported"] is (sys.platform == "win32")
    assert isinstance(foreground_window_title(), str)


def test_gamepad_is_documented_as_absent_not_broken() -> None:
    status = gamepad_status()
    assert "not used by design" in status
    assert "ViGEmBus" in status
