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


def test_catalog_tells_the_truth_about_where_coordinates_come_from() -> None:
    """The catalog used to promise positions that /state never carries.

    CONTRACTS §1 gives `nearby.vehicles[]`/`nearby.peds[]` a `distance` and a `handle`
    and no `pos`, but the catalog's "WHERE COORDINATES COME FROM" block listed "a
    `nearby` entity's position" as a source. Telling the model to read a field that
    does not exist is how it ends up inventing one.
    """
    catalog = _prompt_text("action_catalog.md")
    block = catalog.split("WHERE COORDINATES COME FROM", 1)[1].split("### wander_drive", 1)[0]
    assert "player.pos" in block
    assert "mission.objective_blip.pos" in block
    # CONTRACTS v1.6: nearby entities DO carry pos now - the block must say so, not the reverse.
    assert "NO positions" not in block, "v1.6 gives nearby entities a pos; the old claim is false"
    assert "nearby.peds[].pos" in block, "the block must name nearby positions as a source"
    assert "Never invent coordinates" in block
    assert "follow_entity" in block, "the only legal way to approach a nearby entity"
    assert "nearby` entity's position" not in block


def test_crewmates_are_covered_where_the_tactical_tier_will_see_them() -> None:
    """CONTRACTS v1.5: `relationship` can be `friendly` — the engine's own companion
    flag, i.e. mission crew. Observed live: he could not follow his crewmates, so the
    mission failed. The word has to reach the assembled prefix, not just a file."""
    assert "friendly" in tactical_static_prefix()
    situations = _prompt_text("situations.md")
    assert "friendly" in situations
    crew = situations.split("Stay with the crew", 1)[1].split("\n- ", 1)[0]
    assert "follow_entity" in crew
    assert "in_vehicle" in crew
    assert "objective_blip" in crew, "the blip must be named as outranking the crew"
    # The director sets goals from the same fact, so it must be in its prefix too.
    assert "friendly" in director_static_prefix()


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


def test_radar_legend_is_in_both_prefixes_and_maps_icons_to_state_fields() -> None:
    """The user's ask: yellow / red / blue points and the GPS route. The legend must translate
    each icon into the /state field the brain actually receives."""
    from wasted_harness.brain.prompts import director_static_prefix, tactical_static_prefix

    for prefix in (tactical_static_prefix(), director_static_prefix()):
        assert "Yellow dot" in prefix and "objective_blip" in prefix
        assert "Red dots" in prefix and "hostile" in prefix
        assert "Blue dots" in prefix and "friendly" in prefix
        assert "GPS" in prefix
        assert "Re-read the yellow dot every decision" in prefix


def test_mechanics_brief_teaches_losing_stars_retry_and_health() -> None:
    from wasted_harness.brain.prompts import director_static_prefix, tactical_static_prefix

    for prefix in (tactical_static_prefix(), director_static_prefix()):
        assert "Losing them" in prefix and "line of sight" in prefix
        assert "Retry from the last checkpoint" in prefix
        assert "never take it" in prefix  # Skip
        assert "regenerates only up to half" in prefix
        assert "Michael = **blue**, Franklin = **green**, Trevor = **orange**" in prefix
        assert "no fixed search circle" in prefix


def test_follow_missions_and_locked_controls_are_taught() -> None:
    """Live failure: he waited for a marker that never comes in a follow mission and lost Lamar."""
    from wasted_harness.brain.prompts import tactical_static_prefix

    p = tactical_static_prefix()
    assert "there is NO marker" in p or "NO marker" in p
    assert "follow_entity" in p and "friendly" in p
    assert "getting further away" in p, "must teach that a widening gap is the mission failing"
    assert "control_enabled" in p, "must teach what locked controls mean"


def test_the_gps_route_is_taught_as_a_coordinate_source() -> None:
    """CONTRACTS v1.8 route_blips: the bridge emitted it for a day before anything read it."""
    from wasted_harness.brain.prompts import director_static_prefix, tactical_static_prefix

    for prefix in (tactical_static_prefix(), director_static_prefix()):
        assert "route_blips" in prefix, "the yellow GPS line must be reachable as data"
        assert "four places" in prefix, "the coordinate-source list must still be exhaustive"
    tactical = tactical_static_prefix()
    assert '"kind": "entity"' in tactical, "must teach that a routed entity is a follow target"


def test_the_anti_loop_discipline_is_in_both_prefixes() -> None:
    """The most expensive observed failures were loops nobody inside the loop noticed:
    the enter/exit-vehicle cycle, and fighting a cat for twenty seconds mid-mission."""
    from wasted_harness.brain.prompts import director_static_prefix, tactical_static_prefix

    for prefix in (tactical_static_prefix(), director_static_prefix()):
        assert "anti-loop ladder" in prefix.lower(), "the ladder itself must be present"
        assert "is this the same thing I tried last time" in prefix, "action memory"
        assert "hypothesis" in prefix.lower(), "guess, act, then CHECK the guess"
        assert "Do not invent coordinates" in prefix, "anti-hallucination, coordinates"
        assert "do not assert it as fact" in prefix, "anti-hallucination, general"
        assert "Critical" in prefix and "Low" in prefix, "urgency tiers"


def test_the_catalog_states_what_he_cannot_do() -> None:
    """Three adversarial critics found five knowledge domains building policy on abilities
    the agent does not have (Caps Lock specials, aimed fire, weapon select). Knowledge was fixed;
    the prompt has to say it too, or he narrates actions the viewer can see did not happen."""
    from wasted_harness.brain.prompts import director_static_prefix, tactical_static_prefix

    for prefix in (tactical_static_prefix(), director_static_prefix()):
        assert "What you CANNOT do" in prefix
        assert "You cannot aim" in prefix
        assert "no special ability" in prefix.lower()
        # Bridge 1.7.0 (T6) makes "you cannot pick a weapon" untrue — `fight_ped`
        # now takes fists-or-gun — so the claim the prompt must still make is the
        # one that stayed true: he cannot ACQUIRE one or open the wheel.
        assert "cannot buy, find or open a weapon wheel" in prefix


def test_the_police_rule_is_scoped_to_free_roam_not_absolute() -> None:
    """Stated absolutely, the rule fails eight scripted missions (Prologue, Blitz Play, The
    Paleto Score, The Bureau Raid, ...) whose objective IS surviving a police assault. Stated
    with no rule at all, he farms stars in free roam and the stream dies. It has to be scoped."""
    from wasted_harness.brain.prompts import director_static_prefix, tactical_static_prefix

    for prefix in (tactical_static_prefix(), director_static_prefix()):
        assert "In free roam you never start a fight with police" in prefix
        assert "mission.active" in prefix, "he must be told which flag decides"
        assert "fails the mission" in prefix, "the cost of refusing to fight a scripted assault"


def test_area_combat_warns_about_who_is_inside_the_radius() -> None:
    """Two mission states called for an area combat task with Amanda and Tracey, and Franklin
    and Chop, inside it. He cannot choose the target, so the only defence is not issuing it."""
    from wasted_harness.brain.prompts import tactical_static_prefix

    p = tactical_static_prefix()
    assert "You cannot choose who this hits" in p
    assert "this is the wrong action, and there is no right one" in p

