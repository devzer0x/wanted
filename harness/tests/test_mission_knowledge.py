"""Mission knowledge: identification, the bounded card, and the mission_start wiring.

The sample mission below is STRUCTURALLY DERIVED from the missions.json schema the research
workflow produces; it is a unit-test input, not a fabricated game recording.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from support import throwaway_learned_scripts

from wasted_harness.behavior.missions import MissionEvent
from wasted_harness.behavior.planner import DayPlanner
from wasted_harness.behavior.roam import RoamEngine
from wasted_harness.brain import mission_knowledge as mk
from wasted_harness.main import Harness

SAMPLE = {
    "name": "Prologue",
    "order": 1,
    "protagonist": "Michael/Trevor",
    "giver": "-",
    "start_zone": "North Yankton",
    "summary": "A bank job in the snow goes wrong.",
    "objectives": ["Follow Michael", "Get to the getaway car", "Lose the cops"],
    "fail_conditions": ["Michael dies", "Brad dies", "The getaway car is destroyed"],
    "tips": ["Stay with the crew", "Use cover at the gate"],
    "crew_or_companions": ["Michael", "Brad"],
    "vehicles_involved": ["Stockade"],
    "unlocks_next": "Franklin and Lamar",
    "notes": "",
}


def test_card_is_bounded_and_carries_the_essentials() -> None:
    card = mk.mission_card(SAMPLE)
    assert card.startswith("MISSION KNOWLEDGE — Prologue")
    assert "1. Follow Michael" in card and "3. Lose the cops" in card
    assert "Crew with you: Michael, Brad" in card
    assert "Fails if: Michael dies" in card
    assert len(card) <= mk.MAX_CARD_CHARS
    huge = dict(SAMPLE, tips=["x" * 500] * 20, objectives=["y" * 300] * 30)
    assert len(mk.mission_card(huge)) <= mk.MAX_CARD_CHARS


def test_identification_never_guesses_without_data(monkeypatch) -> None:
    monkeypatch.setattr(mk, "load_missions", lambda: [])
    assert mk.identify_mission("Prologue", "North Yankton") is None


def test_identification_title_then_zone(monkeypatch) -> None:
    other = dict(SAMPLE, name="Franklin and Lamar", order=2, start_zone="Vespucci Beach")
    monkeypatch.setattr(mk, "load_missions", lambda: [SAMPLE, other])
    assert mk.identify_mission("PROLOGUE", None)["name"] == "Prologue"          # exact, case-insensitive
    assert mk.identify_mission("Prologe", None)["name"] == "Prologue"           # screen-read typo
    assert mk.identify_mission(None, "North Yankton")["name"] == "Prologue"     # zone fallback
    assert mk.identify_mission("Unrelated Title", "Downtown") is None           # no guess


class _Missions:
    def __init__(self, events): self._e = events
    def feed(self, *_a, **_k): return list(self._e)


class _Writer:
    #: Stands in for `SupabaseWriter`, which carries this flag; `Harness._heartbeat`
    #: reads it to refuse publishing a heartbeat over an unflushed backlog.
    unflushed = False

    def __init__(self):
        self.events = []
        self.missions = []

    def record_event(self, t, p, screenshot_url=None, **_k): self.events.append((t, p, screenshot_url))
    def insert_mission(self, row): self.missions.append(dict(row))


def _harness(events, title):
    h = Harness.__new__(Harness)
    h.missions = _Missions(events)
    # `_handle_mission_events` feeds the day planner the same events (attempt
    # counting / roam-block back-off), so the real planner is wired in.
    h.planner = DayPlanner()
    # The real roam engine: free roam's single owner. `_handle_mission_events`
    # resets its "the story has to move" clock and `_reflex` reads its
    # standing-still measure, so stubbing it out would stop testing exactly the
    # machinery that keeps him from standing there.
    h.roam = RoamEngine()
    h.writer = _Writer()
    h._pending_screenshot_trigger = None
    h._pending_big_event = None
    h.current_mission = None
    h.reads = []
    h._capture_screenshot = lambda hint: (b"jpeg-bytes", "https://x/shot.jpg")
    h._read_mission_title = lambda jpeg: (h.reads.append(jpeg) or title)
    # A mission that ends now also writes a `missions` row (§5), which needs the
    # start it was observed at and the tokens spent inside it.
    h._mission_started_iso = None
    h._mission_tokens = 0
    h.deaths_this_mission = 0
    h.learned_scripts = throwaway_learned_scripts()
    return h


_STATE = SimpleNamespace(
    mission=SimpleNamespace(
        active=True, cutscene_active=False, random_event_active=False, script=None
    ),
    player=SimpleNamespace(dead=False, arrested=False),
    location=SimpleNamespace(zone="North Yankton", street="Cavalry Blvd"),
)


def test_mission_start_reads_the_title_and_identifies(monkeypatch) -> None:
    monkeypatch.setattr(
        "wasted_harness.main.identify_mission_with_source",
        lambda title, zone, *, script=None, learned=None: (
            (SAMPLE, "title") if title == "Prologue" else (None, None)
        ),
    )
    h = _harness([MissionEvent("mission_start", {"name": "unknown"})], "Prologue")
    Harness._handle_mission_events(h, _STATE, None)
    assert h.reads == [b"jpeg-bytes"], "the title must be read from the mission_start screenshot"
    assert h.current_mission is SAMPLE
    assert h._pending_screenshot_trigger == "mission_start"


def test_mission_start_learns_the_script_when_identified_by_title() -> None:
    """End-to-end wiring: a real `mission.script` plus a title match must
    reach `learned_scripts` — this is the ONLY place a pairing is ever
    learned (CLAUDE.md rule 1: never a hardcoded guess)."""
    state = SimpleNamespace(
        mission=SimpleNamespace(
            active=True, cutscene_active=False, random_event_active=False, script="prologue1"
        ),
        player=SimpleNamespace(dead=False, arrested=False),
        location=SimpleNamespace(zone="North Yankton", street="Cavalry Blvd"),
    )
    h = _harness([MissionEvent("mission_start", {"name": "unknown"})], "Prologue")
    Harness._handle_mission_events(h, state, None)
    assert h.current_mission["name"] == "Prologue"
    assert h.learned_scripts.get("prologue1") == "Prologue"


def test_mission_start_does_not_learn_from_a_zone_only_match() -> None:
    """A zone fallback is a weaker signal than a read title; learning from it
    would risk teaching a wrong pairing from an ambiguous zone."""
    state = SimpleNamespace(
        mission=SimpleNamespace(
            active=True, cutscene_active=False, random_event_active=False, script="prologue1"
        ),
        player=SimpleNamespace(dead=False, arrested=False),
        location=SimpleNamespace(zone="North Yankton", street="Cavalry Blvd"),
    )
    # No title read this time (None) — falls back to the zone.
    h = _harness([MissionEvent("mission_start", {"name": "unknown"})], None)
    Harness._handle_mission_events(h, state, None)
    assert h.learned_scripts.get("prologue1") is None


# --- LearnedScripts: persistence and the never-overwrite rule ----------------


def test_learned_scripts_round_trips_through_a_fresh_instance() -> None:
    path = Path(tempfile.mkdtemp(prefix="wasted-learned-scripts-")) / "learned.json"
    first = mk.LearnedScripts(path)
    first.learn("armenian1", "Hang Ten")
    assert first.get("armenian1") == "Hang Ten"
    reloaded = mk.LearnedScripts(path)
    assert reloaded.get("armenian1") == "Hang Ten"
    assert reloaded.mapping == {"armenian1": "Hang Ten"}


def test_learned_scripts_never_overwrites_a_conflicting_pair(caplog) -> None:
    scripts = throwaway_learned_scripts()
    scripts.learn("armenian1", "Hang Ten")
    with caplog.at_level("WARNING"):
        scripts.learn("armenian1", "A Different Mission Entirely")
    assert scripts.get("armenian1") == "Hang Ten", "the first learned pair must survive"
    assert any("conflict" in r.message for r in caplog.records)


def test_learned_scripts_learning_the_same_pair_twice_is_a_silent_no_op(caplog) -> None:
    scripts = throwaway_learned_scripts()
    scripts.learn("armenian1", "Hang Ten")
    with caplog.at_level("WARNING"):
        scripts.learn("armenian1", "Hang Ten")
    assert scripts.get("armenian1") == "Hang Ten"
    assert not any("conflict" in r.message for r in caplog.records)


def test_learned_scripts_degrades_gracefully_on_a_missing_or_corrupt_file() -> None:
    path = Path(tempfile.mkdtemp(prefix="wasted-learned-scripts-")) / "learned.json"
    assert mk.LearnedScripts(path).mapping == {}
    path.write_text("{not valid json", encoding="utf-8")
    scripts = mk.LearnedScripts(path)
    assert scripts.mapping == {}
    # And it must still be usable afterwards — a corrupt file costs learning,
    # never the show.
    scripts.learn("armenian1", "Hang Ten")
    assert json.loads(path.read_text(encoding="utf-8")) == {"armenian1": "Hang Ten"}


# --- identify_mission_with_source: the script signal -------------------------


def test_a_learned_script_wins_over_everything_else(monkeypatch) -> None:
    other = dict(SAMPLE, name="Franklin and Lamar", order=2, start_zone="Vespucci Beach")
    monkeypatch.setattr(mk, "load_missions", lambda: [SAMPLE, other])
    mission, source = mk.identify_mission_with_source(
        "Unrelated Title", "Vespucci Beach",
        script="prologue1", learned={"prologue1": "Prologue"},
    )
    assert mission["name"] == "Prologue"
    assert source == "script"


def test_an_unknown_learned_mission_name_is_ignored_not_guessed(monkeypatch) -> None:
    monkeypatch.setattr(mk, "load_missions", lambda: [SAMPLE])
    mission, source = mk.identify_mission_with_source(
        "Prologue", None, script="weird1", learned={"weird1": "Nonexistent Mission"},
    )
    assert mission["name"] == "Prologue", "falls through to the title match"
    assert source == "title"


def test_identify_mission_thin_wrapper_still_returns_just_the_mission() -> None:
    assert mk.identify_mission("Prologue", None, script=None, learned=None)["name"] == "Prologue"


def test_mission_end_clears_the_card() -> None:
    h = _harness([MissionEvent("mission_end", {"name": "unknown", "outcome": "passed"})], None)
    h.current_mission = SAMPLE
    Harness._handle_mission_events(h, _STATE, None)
    assert h.current_mission is None


# --- the REAL data file (produced by the research workflow, QA-corrected) --------------------


def test_real_mission_file_loads_and_identifies() -> None:
    ms = mk.load_missions()
    assert len(ms) >= 69, "the story has 69 missions; the file must cover them"
    orders = [m["order"] for m in ms]
    assert orders == sorted(orders) and len(set(orders)) == len(orders), "orders unique + sorted"
    assert ms[0]["name"] == "Prologue"
    assert mk.identify_mission("Prologue", None)["name"] == "Prologue"
    assert mk.identify_mission(None, "North Yankton")["name"] == "Prologue"
    assert mk.identify_mission("Franklin and Lamar", None)["order"] == 2
    for m in ms:
        assert len(m.get("objectives", [])) >= 2, m["name"]
        assert len(mk.mission_card(m)) <= mk.MAX_CARD_CHARS, m["name"]
        assert not any("opposition" in str(c).lower() for c in m.get("crew_or_companions", [])), m["name"]


def test_a_known_script_never_loses_to_a_zone_guess() -> None:
    """OBSERVED LIVE 2026-09-02, first run after the v1.10 deploy:

        mission identified: title_read=None zone="Pacific Bluffs"
                            script=Armenian1 source=zone mission="The Wrap Up"

    The engine said the running mission script was `Armenian1`; the harness
    ignored that, guessed from the ZONE, and fed the agent the walkthrough card for
    a completely different mission. Wrong objectives, wrong crew, wrong tips —
    exactly the confabulation `mission.script` was added to end.

    A zone is a weak, ambiguous signal. When the engine has NAMED the running
    script and we simply have not learned that name yet, the honest answer is
    "unknown" — never a contradicting guess. A read TITLE may still win: it is a
    direct observation of this mission, and it is what teaches us the pairing.
    """
    unknown_to_us = mk.identify_mission_with_source(
        None, "Pacific Bluffs", script="Armenian1", learned={}
    )
    assert unknown_to_us == (None, None), (
        "a zone guess must not override the engine's own script identity"
    )
    # With no script at all, the zone fallback is still legitimate.
    by_zone, source = mk.identify_mission_with_source(None, "North Yankton", script=None, learned={})
    if by_zone is not None:
        assert source == "zone"


def test_learned_script_lookup_is_case_insensitive() -> None:
    """The bridge emits the game's own thread name (`Armenian1`); the research
    allowlist and every doc write it lowercase (`armenian1`). A pairing learned
    under one casing must resolve under the other, or we would re-learn (and
    re-conflict on) the same mission forever."""
    missions = mk.load_missions()
    assert missions, "missions.json must be present for this test to mean anything"
    real_name = missions[0]["name"]

    found, source = mk.identify_mission_with_source(
        None, None, script="ARMENIAN1", learned={"armenian1": real_name}
    )
    assert source == "script"
    assert found is not None and found["name"] == real_name
