"""Unit tests for `brain/vision.py`'s mission-outcome screen read.

`read_mission_title` has only ever had indirect coverage, through the
mission_start wiring tests in test_mission_following.py / test_mission_knowledge.py
(its own `_read_mission_title` seam is stubbed out there, never exercised for
real). This file is the first direct coverage of the vision module, added for
`read_mission_outcome` (root cause: a mission that fails without killing or
arresting anyone — "MISSION FAILED / Franklin lost Lamar" — emitted nothing at
all, because nothing read the screen).

No real API call is made anywhere here (CLAUDE.md rule 2/the brief's own
constraint: no live API calls from this task). A minimal stand-in plays back a
canned response, and the error-handling test raises a REAL
`anthropic.APIError` subclass, built the same way test_recovery.py already
does for `classify_api_failure`, so the catch is proven against the actual SDK
exception hierarchy rather than an invented stand-in exception.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import anthropic
import httpx

from wasted_harness.brain.vision import MissionOutcome, read_mission_outcome

JPEG = b"\xff\xd8\xff\xd9"  # not a decodable image; the fake client never looks at it


class _FakeMessages:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responder(kwargs)


class _FakeClient:
    def __init__(self, responder) -> None:
        self.messages = _FakeMessages(responder)


def _text_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def _client_returning(text: str) -> _FakeClient:
    return _FakeClient(lambda _kwargs: _text_response(text))


def _api_error(cls, status: int = 429, message: str = "boom"):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        status,
        request=request,
        json={"type": "error", "error": {"type": "x", "message": message}},
    )
    return cls(message, response=response, body=None)


# --- the happy paths ----------------------------------------------------------


def test_passed_with_no_reason_line() -> None:
    client = _client_returning("PASSED\nNONE")
    assert read_mission_outcome(client, "model-x", JPEG) == MissionOutcome("passed", None)


def test_failed_carries_the_screens_own_reason_text_verbatim() -> None:
    """The single most valuable thing on the screen, per the brief: the
    game's own explanation, not this package's guess."""
    client = _client_returning("FAILED\nFranklin lost Lamar")
    result = read_mission_outcome(client, "model-x", JPEG)
    assert result == MissionOutcome("failed", "Franklin lost Lamar")


def test_failed_with_explicit_none_reason_reports_none() -> None:
    client = _client_returning("FAILED\nNONE")
    assert read_mission_outcome(client, "model-x", JPEG) == MissionOutcome("failed", None)


def test_failed_with_only_one_line_still_parses() -> None:
    client = _client_returning("FAILED")
    assert read_mission_outcome(client, "model-x", JPEG) == MissionOutcome("failed", None)


def test_case_and_quote_insensitive_verdict_and_reason() -> None:
    client = _client_returning('"passed"\n"Franklin lost Lamar"')
    result = read_mission_outcome(client, "model-x", JPEG)
    assert result.outcome == "passed"
    assert result.reason_text == "Franklin lost Lamar"


def test_explicit_unknown_screen() -> None:
    client = _client_returning("UNKNOWN\nNONE")
    assert read_mission_outcome(client, "model-x", JPEG) == MissionOutcome("unknown", None)


# --- never a guess -------------------------------------------------------------


def test_unparseable_text_is_treated_as_unknown_not_a_guess() -> None:
    client = _client_returning("I think the mission might have passed?")
    assert read_mission_outcome(client, "model-x", JPEG) == MissionOutcome("unknown", None)


def test_empty_response_is_unknown() -> None:
    client = _client_returning("")
    assert read_mission_outcome(client, "model-x", JPEG) == MissionOutcome("unknown", None)


def test_whitespace_only_response_is_unknown() -> None:
    client = _client_returning("   \n   \n")
    assert read_mission_outcome(client, "model-x", JPEG) == MissionOutcome("unknown", None)


# --- CONSTRAINT: never let a vision failure raise ------------------------------


def test_a_real_anthropic_api_error_is_caught_and_returns_unknown_not_raised() -> None:
    def _raise(_kwargs):
        raise _api_error(anthropic.RateLimitError, 429)

    client = _FakeClient(_raise)
    assert read_mission_outcome(client, "model-x", JPEG) == MissionOutcome("unknown", None)


def test_an_overloaded_error_is_also_swallowed() -> None:
    def _raise(_kwargs):
        raise _api_error(anthropic.InternalServerError, 529)

    client = _FakeClient(_raise)
    assert read_mission_outcome(client, "model-x", JPEG) == MissionOutcome("unknown", None)


# --- request shape: same idiom as read_mission_title, image + capped tokens ---


def test_request_carries_the_jpeg_as_base64_and_stays_capped() -> None:
    client = _client_returning("UNKNOWN\nNONE")
    read_mission_outcome(client, "model-x", JPEG)
    [kwargs] = client.messages.calls
    assert kwargs["model"] == "model-x"
    assert kwargs["max_tokens"] <= 64, "capped, not a free-form description"
    content = kwargs["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    assert base64.b64decode(image_block["source"]["data"]) == JPEG
    assert image_block["source"]["media_type"] == "image/jpeg"


def test_only_called_once_per_invocation() -> None:
    """Not a rate-limit test — just confirms the seam makes exactly one call
    per read, the same bounded-cost shape as `read_mission_title`."""
    client = _client_returning("PASSED\nNONE")
    read_mission_outcome(client, "model-x", JPEG)
    assert len(client.messages.calls) == 1
