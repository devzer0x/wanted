"""No §4 event type may be wired up without something that emits it.

The `stunt` event was wired at both ends and produced at neither: the director
listed it as a vision trigger and as a big event, the humanizer mapped a mood
for it, and nothing anywhere in the package ever called
`record_event("stunt", ...)`. That reads like a working feature in review and is
a silent dead path in production.

It cannot be implemented honestly today. `stunt` needs `airtime_s`, and /state
v1.2 exposes no on-ground flag and no vertical velocity — at a 2-4 Hz poll a
"large z delta" is a jump, a hill, a car-park ramp or a lift with equal
probability, so any detector built on the documented fields would be inventing
the number it reports (CLAUDE.md rule 1). It needs a bridge-side field, which is
a contract change, and it is recorded as Phase 3 work in harness/README.md.

So it is declared unproduced and subtracted from every trigger set. This test
keeps that declaration honest in both directions.
"""

from __future__ import annotations

import re
from pathlib import Path

from wasted_harness.brain.director import (
    BIG_EVENTS,
    CONTRACT_BIG_EVENTS,
    CONTRACT_VISION_EVENTS,
    VISION_TRIGGERS,
)
from wasted_harness.events import (
    EMITTED_EVENT_TYPES,
    EVENT_TYPES,
    UNPRODUCED_EVENT_REASONS,
    UNPRODUCED_EVENT_TYPES,
)

PACKAGE = Path(__file__).resolve().parents[1] / "wasted_harness"
README = Path(__file__).resolve().parents[1] / "README.md"


def _event_types_the_code_actually_emits() -> set[str]:
    """Every literal event type this package hands to an emitter.

    `record_event(...)` is the Supabase writer; `_emit(...)` is MissionTracker's
    queue, which main forwards to record_event verbatim. `_record_big_event(...)`
    and `insert_event_now(...)` are the same write for the two clip-worthy
    events (death, busted) — they go in singly so the row id is known and
    `clips.event_id` can point back at them.
    """
    found: set[str] = set()
    pattern = re.compile(
        r"""(?:record_event|_record_big_event|insert_event_now|_emit)\(\s*["'](\w+)["']"""
    )
    for path in PACKAGE.rglob("*.py"):
        found |= set(pattern.findall(path.read_text(encoding="utf-8")))
    return found


def test_unproduced_types_are_really_not_produced() -> None:
    emitted = _event_types_the_code_actually_emits()
    wrongly_declared = sorted(UNPRODUCED_EVENT_TYPES & emitted)
    assert not wrongly_declared, (
        f"{wrongly_declared} are declared unproduced but the code emits them — "
        f"remove them from events.UNPRODUCED_EVENT_TYPES and re-arm the triggers"
    )


def test_the_unproduced_list_is_a_subset_of_the_contract_enum() -> None:
    assert UNPRODUCED_EVENT_TYPES <= EVENT_TYPES
    assert EMITTED_EVENT_TYPES == EVENT_TYPES - UNPRODUCED_EVENT_TYPES


def test_no_trigger_set_waits_for_an_event_that_never_arrives() -> None:
    assert not (VISION_TRIGGERS & UNPRODUCED_EVENT_TYPES)
    assert not (BIG_EVENTS & UNPRODUCED_EVENT_TYPES)
    # …and the trigger sets are the contract lists minus exactly that, so a
    # contract event is never quietly dropped for any other reason.
    assert VISION_TRIGGERS == CONTRACT_VISION_EVENTS - UNPRODUCED_EVENT_TYPES
    assert BIG_EVENTS == CONTRACT_BIG_EVENTS - UNPRODUCED_EVENT_TYPES
    assert CONTRACT_VISION_EVENTS <= EVENT_TYPES
    assert CONTRACT_BIG_EVENTS <= EVENT_TYPES


def test_stunt_is_the_known_gap_and_is_documented_as_phase_3() -> None:
    assert "stunt" in UNPRODUCED_EVENT_TYPES
    readme = README.read_text(encoding="utf-8")
    assert "stunt" in readme and "Phase 3" in readme, (
        "harness/README.md must record the stunt-detection gap as Phase 3 work"
    )
    # The gap has to be findable by searching for the event name, not buried.
    stunt_lines = [line for line in readme.splitlines() if "`stunt`" in line]
    assert stunt_lines, "README must name the `stunt` event type explicitly"


def test_the_other_contract_event_types_all_have_a_producer() -> None:
    """Nothing else may quietly join `stunt` without being declared."""
    orphans = sorted(EMITTED_EVENT_TYPES - _event_types_the_code_actually_emits())
    assert not orphans, (
        f"{orphans} are in the §4 enum, are not declared unproduced, and nothing "
        f"emits them — either wire them up or add them to UNPRODUCED_EVENT_REASONS"
    )


def test_every_unproduced_type_carries_a_reason() -> None:
    for event_type, reason in UNPRODUCED_EVENT_REASONS.items():
        assert event_type in EVENT_TYPES, f"{event_type} is not a §4 type"
        assert len(reason) > 40, f"{event_type} needs a real reason, not a label"
        assert "Phase" in reason or "Phase" in UNPRODUCED_EVENT_REASONS.get(
            "mission_end", ""
        ), f"{event_type} must say when it lands"
