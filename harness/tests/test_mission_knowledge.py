"""Mission knowledge: identification, the bounded card, and the mission_start wiring.

The sample mission below is STRUCTURALLY DERIVED from the missions.json schema the research
workflow produces; it is a unit-test input, not a fabricated game recording.
"""

from __future__ import annotations

from types import SimpleNamespace

from wasted_harness.behavior.missions import MissionEvent
from wasted_harness.behavior.planner import DayPlanner
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
    return h


_STATE = SimpleNamespace(
    mission=SimpleNamespace(active=True, cutscene_active=False, random_event_active=False),
    player=SimpleNamespace(dead=False, arrested=False),
    location=SimpleNamespace(zone="North Yankton", street="Cavalry Blvd"),
)


def test_mission_start_reads_the_title_and_identifies(monkeypatch) -> None:
    monkeypatch.setattr("wasted_harness.main.identify_mission",
                        lambda title, zone: SAMPLE if title == "Prologue" else None)
    h = _harness([MissionEvent("mission_start", {"name": "unknown"})], "Prologue")
    Harness._handle_mission_events(h, _STATE, None)
    assert h.reads == [b"jpeg-bytes"], "the title must be read from the mission_start screenshot"
    assert h.current_mission is SAMPLE
    assert h._pending_screenshot_trigger == "mission_start"


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
