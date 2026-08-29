"""The static prompt half: catalog/schema agreement, stability, size floors.

The authoritative token count comes from the live count_tokens endpoint
(`--prompt-audit` / `--check`). These tests are the offline floor that stops
someone gutting a prompt file without noticing, using a chars-per-token ratio
CALIBRATED against a real measurement — see the constants below.
"""

import re
from importlib import resources

from wasted_harness.brain.prompts import director_static_prefix, tactical_static_prefix
from wasted_harness.brain.schemas import ACTION_TYPES, MOODS

# Calibration from the live count_tokens run recorded in docs/STATUS.md
# (2026-08-25): the tactical prefix measured 7617 tokens at 27168 chars on
# claude-haiku-4-5, the director prefix 8532 tokens at 22862 chars on
# claude-sonnet-5 (Sonnet tokenizes the same English ~30% more finely).
# A 15% safety margin is applied so this floor can never pass a prefix the real
# endpoint would reject.
HAIKU_CHARS_PER_TOKEN = 3.567
SONNET_CHARS_PER_TOKEN = 2.680
SAFETY = 1.15


def _prompt_text(name: str) -> str:
    return (resources.files("wasted_harness.brain.prompts") / name).read_text(encoding="utf-8")


def test_action_catalog_documents_exactly_the_schema_actions() -> None:
    """Drift here means the model is told about actions the harness rejects."""
    catalog = _prompt_text("action_catalog.md")
    documented = re.findall(r"^### (\w+)$", catalog, re.MULTILINE)
    assert len(documented) == len(set(documented)), "an action is documented twice"
    assert set(documented) == set(ACTION_TYPES)
    # The flat list near the top must be complete too — that is what the model
    # actually scans before answering.
    for action in ACTION_TYPES:
        assert f"`{action}`" in catalog


def test_catalog_names_no_cheat_actions() -> None:
    catalog = _prompt_text("action_catalog.md").lower()
    for forbidden in ("teleport", "god mode", "spawn vehicle", "give weapon", "add money"):
        # They may appear in the "these do not exist" sentence, never as a heading.
        assert f"### {forbidden}" not in catalog


def test_every_mood_is_specified_in_the_mood_prompt() -> None:
    moods = _prompt_text("driving_moods.md")
    for mood in MOODS:
        assert f"### {mood}" in moods, f"{mood} has no section"
        # Each mood must change behaviour, voice AND cadence, not just adjectives.
    for required in ("**Does:**", "**Sounds like:**", "**Says how much:**"):
        assert moods.count(required) == len(MOODS), f"{required} missing for some mood"


def test_commentary_style_carries_good_and_bad_exemplars() -> None:
    style = _prompt_text("commentary_style.md")
    assert style.count("- BAD:") >= 6
    assert style.count("- GOOD:") >= 6
    assert "GOOD vs BAD" in style


def test_prefixes_are_byte_stable() -> None:
    """Any variation between calls silently destroys prompt caching."""
    assert tactical_static_prefix() == tactical_static_prefix()
    assert director_static_prefix() is director_static_prefix()  # lru_cache holds


def test_prefixes_clear_the_cache_minimums_offline() -> None:
    tactical_min_chars = 4096 * HAIKU_CHARS_PER_TOKEN * SAFETY
    director_min_chars = 1024 * SONNET_CHARS_PER_TOKEN * SAFETY
    assert len(tactical_static_prefix()) > tactical_min_chars
    assert len(director_static_prefix()) > director_min_chars


def test_persona_keeps_the_ai_thing_in_proportion() -> None:
    persona = _prompt_text("persona.md")
    assert "You are an AI" in persona
    assert "do not make it a whole thing" in persona.lower()
    rules = _prompt_text("rules.md").lower()
    for forbidden in ("slur", "real, living people", "no politics of the real world"):
        assert forbidden in rules
