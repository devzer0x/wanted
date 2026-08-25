"""Prompt assembly for the two brain tiers.

The static prefixes are byte-stable across calls within a process — that is what
makes prompt caching work (cache_control on the prefix block; any change busts
the cache). Dynamic content (state, memory, deltas) never goes in the prefix.

Cache minimums (CONTRACTS §3): tactical (Haiku 4.5) prefix must exceed 4096
tokens or caching silently does nothing; director (Sonnet 5) needs 1024. Checked
at startup via count_tokens in brain.tactical.verify_prefix_cacheable.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

_TACTICAL_FILES = (
    "persona.md",
    "rules.md",
    "action_catalog.md",
    "driving_moods.md",
    "world.md",
    "situations.md",
    "commentary_style.md",
    "decision_guide.md",
)

_DIRECTOR_FILES = (
    "persona.md",
    "rules.md",
    "action_catalog.md",
    "driving_moods.md",
    "world.md",
    "commentary_style.md",
    "director.md",
)


def _read(name: str) -> str:
    return (resources.files(__package__) / name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def tactical_static_prefix() -> str:
    return "\n\n---\n\n".join(_read(f) for f in _TACTICAL_FILES)


@lru_cache(maxsize=1)
def director_static_prefix() -> str:
    return "\n\n---\n\n".join(_read(f) for f in _DIRECTOR_FILES)
