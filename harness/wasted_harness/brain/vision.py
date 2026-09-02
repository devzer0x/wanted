"""Two tiny vision calls: read the mission title off a mission-start screenshot, and the
PASSED/FAILED verdict (plus the game's own reason line) off a mission-end screenshot.

GTA V prints the mission name on screen when a mission begins, and prints "MISSION PASSED" or
"MISSION FAILED" (usually with a short reason line under it, e.g. "Franklin lost Lamar") when
one ends. /state never carries any of this - no mission name field, no outcome field - so the
screen the game itself draws is the only honest source for either question. Each call runs once
per mission start / mission end (both rare), on the cheap tactical model, with a hard token cap.
Any failure returns the "nothing learned" case (None for the title, `MissionOutcome("unknown",
None)` for the outcome) and the caller falls back accordingly - zone-based identification, or a
plain logged transition with nothing reported to the public site. A vision failure here must
never propagate as an exception: this call sits on the main tick, not behind a retry queue.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass

import anthropic

from ..logsetup import get_logger

log = get_logger("wasted.vision")

_PROMPT = (
    "This is a screenshot from a video game at the moment a story mission starts. "
    "If a mission title is printed on screen (usually large text near the bottom-left or "
    "center), reply with ONLY that title text, nothing else. If no title is visible, reply "
    "with exactly: NONE"
)


#: How a vision call reports what it cost. It is handed the model id and the
#: SDK usage object and does the pricing itself, so this module never needs the
#: pricing table. Passing None means "nobody is keeping books", which is only
#: ever true in a test: these are real billed calls WITH AN IMAGE attached, and
#: leaving them out of the governor is what let real spend pass the hourly cap
#: while the site still published L0.
UsageSink = Callable[[str, object], None]


def read_mission_title(
    client: anthropic.Anthropic,
    model_id: str,
    jpeg: bytes,
    on_usage: UsageSink | None = None,
) -> str | None:
    try:
        resp = client.messages.create(
            model=model_id,
            max_tokens=24,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64.b64encode(jpeg).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": _PROMPT},
                    ],
                }
            ],
        )
        if on_usage is not None:
            on_usage(model_id, resp.usage)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except anthropic.APIError as exc:
        log.warning("mission title read failed", extra={"kv": {"error": f"{type(exc).__name__}: {exc}"}})
        return None
    if not text or text.upper().startswith("NONE"):
        return None
    return text.strip().strip('"').strip()


@dataclass(frozen=True)
class MissionOutcome:
    """A closed vision read of the MISSION PASSED / MISSION FAILED screen.

    `outcome` is always exactly one of `"passed" | "failed" | "unknown"` - never a raw model
    guess, so a caller can branch on it directly. `reason_text` is the game's OWN on-screen
    explanation ("Franklin lost Lamar", "You killed the target") when the screen printed one;
    it is the single most valuable thing on a failure screen, because it is the game explaining
    in English what went wrong rather than this package inferring it. `unknown` always pairs
    with `reason_text=None`: if the read could not even tell pass from fail, a leftover reason
    string would be unverifiable noise, not signal, and must not be reported anywhere.
    """

    outcome: str
    reason_text: str | None


_UNKNOWN_OUTCOME = MissionOutcome("unknown", None)

_OUTCOME_PROMPT = (
    "This is a screenshot from a video game at the moment a story mission has just ended. "
    "The game prints one of two banners: \"MISSION PASSED\" or \"MISSION FAILED\". A FAILED "
    "banner is very often followed by a short reason line in plain English explaining what "
    "went wrong (for example \"Franklin lost Lamar\", \"You died\", \"The vehicle was "
    "destroyed\", \"You strayed too far from the mission area\"). "
    "Reply with EXACTLY two lines and nothing else:\n"
    "Line 1: PASSED, FAILED, or UNKNOWN (UNKNOWN only if the screen shows neither banner).\n"
    "Line 2: the reason line printed on screen, copied exactly as shown, or NONE if there is "
    "no reason line."
)


def read_mission_outcome(
    client: anthropic.Anthropic,
    model_id: str,
    jpeg: bytes,
    on_usage: UsageSink | None = None,
) -> MissionOutcome:
    """Read PASSED/FAILED (+ the screen's own reason line) off a mission-end screenshot.

    Same shape as `read_mission_title`: one capped call, same narrow error handling (an
    `anthropic.APIError` is logged and swallowed, never raised), never called more than once per
    mission end (as rare as a mission start - CONTRACTS v1.3's bounded-cost argument applies
    here unchanged; this must never be called on a tick timer). `max_tokens` is a little higher
    than the title read's 24 because the answer is two lines, not one, but it is still a hard,
    tiny cap - nowhere near what a free-form description would cost.

    Anything the model returns that does not cleanly parse to PASSED/FAILED is treated as
    `unknown`, on purpose: guessing an outcome from a garbled read would fabricate the exact
    number (`missions_passed`) this call exists to make honest.
    """
    try:
        resp = client.messages.create(
            model=model_id,
            max_tokens=30,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64.b64encode(jpeg).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": _OUTCOME_PROMPT},
                    ],
                }
            ],
        )
        if on_usage is not None:
            on_usage(model_id, resp.usage)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except anthropic.APIError as exc:
        log.warning("mission outcome read failed", extra={"kv": {"error": f"{type(exc).__name__}: {exc}"}})
        return _UNKNOWN_OUTCOME
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return _UNKNOWN_OUTCOME
    verdict = lines[0].strip().strip('"').upper()
    if verdict.startswith("PASS"):
        outcome = "passed"
    elif verdict.startswith("FAIL"):
        outcome = "failed"
    else:
        return _UNKNOWN_OUTCOME
    reason_line = lines[1].strip().strip('"') if len(lines) > 1 else ""
    reason_text = None if (not reason_line or reason_line.upper() == "NONE") else reason_line
    return MissionOutcome(outcome, reason_text)
