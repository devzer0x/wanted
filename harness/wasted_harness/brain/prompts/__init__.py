"""Prompt assembly for the two brain tiers.

The static prefixes are byte-stable across calls within a process — that is what
makes prompt caching work (cache_control on the prefix block; any change busts
the cache). Dynamic content (state, memory, deltas) never goes in the prefix.

Cache minimums (CONTRACTS §3): tactical (Haiku 4.5) prefix must exceed 4096
tokens or caching silently does nothing; director (Sonnet 5) needs 1024. Checked
at startup via count_tokens in brain.tactical.verify_prefix_cacheable.
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources

_TACTICAL_FILES = (
    "persona.md",
    "rules.md",
    "action_catalog.md",
    "driving_moods.md",
    "world.md",
    "hud_legend.md",
    "mechanics.md",
    "situations.md",
    "thinking.md",
    "commentary_style.md",
    "decision_guide.md",
)

_DIRECTOR_FILES = (
    "persona.md",
    "rules.md",
    "action_catalog.md",
    "driving_moods.md",
    "world.md",
    "hud_legend.md",
    "mechanics.md",
    "thinking.md",
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


#: T4 (findings.md): the output validator's hard-banned-phrase list is
#: PARSED from commentary_style.md's own "## Banned phrases" section rather
#: than hand-duplicated in code, so the text the model is shown and the list
#: the validator enforces can never drift apart.
_BANNED_PHRASES_HEADING = "## Banned phrases"
_BANNED_PHRASE_LINE = re.compile(r'^\s*-\s*"([^"]+)"\s*$')


@lru_cache(maxsize=1)
def banned_phrases() -> tuple[str, ...]:
    """The curated hard-filter list, lowercased, in file order.

    Returns an empty tuple (not an error) if the section is missing or empty
    — the validator treats that as "nothing to check", same as any other
    empty rule input; it must never crash the decision loop over a prompt
    file being briefly out of sync.
    """
    text = _read("commentary_style.md")
    phrases: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.strip().startswith(_BANNED_PHRASES_HEADING):
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            m = _BANNED_PHRASE_LINE.match(line)
            if m:
                phrases.append(m.group(1).strip().lower())
    return tuple(phrases)
