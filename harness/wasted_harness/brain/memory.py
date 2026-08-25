"""the agent's memory: rolling summary, journal of learned facts, day log.

Plain files under harness/state/ so a crash loses nothing and the operator can
read them with `cat`:

- ``summary.txt``      — rolling summary, kept under ~300 tokens (approximated
                         as 225 words; see _SUMMARY_MAX_WORDS).
- ``journal.jsonl``    — append-only learned facts ({ts, fact, source}).
- ``daylog/<date>.jsonl`` — one file per UTC day: what happened, when.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..logsetup import get_logger

log = get_logger("wasted.memory")

# Contract: summary <= 300 tokens. There is no offline tokenizer for Claude
# models, so we enforce a conservative word bound (~1.33 tokens/word English
# on Haiku => 225 words stays under 300 tokens with margin).
_SUMMARY_MAX_WORDS = 225
_JOURNAL_RECENT = 12


class Memory:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._summary_path = state_dir / "summary.txt"
        self._journal_path = state_dir / "journal.jsonl"
        self._daylog_dir = state_dir / "daylog"
        self._daylog_dir.mkdir(exist_ok=True)

    # -- rolling summary -------------------------------------------------------

    def summary(self) -> str:
        if self._summary_path.exists():
            return self._summary_path.read_text(encoding="utf-8").strip()
        return ""

    def set_summary(self, text: str) -> None:
        words = text.split()
        if len(words) > _SUMMARY_MAX_WORDS:
            log.warning(
                "summary over budget, truncating",
                extra={"kv": {"words": len(words), "max": _SUMMARY_MAX_WORDS}},
            )
            text = " ".join(words[:_SUMMARY_MAX_WORDS])
        self._summary_path.write_text(text.strip() + "\n", encoding="utf-8")

    # -- journal ---------------------------------------------------------------

    def append_fact(self, fact: str, source: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "fact": fact,
            "source": source,
        }
        with self._journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent_facts(self, n: int = _JOURNAL_RECENT) -> list[str]:
        if not self._journal_path.exists():
            return []
        lines = self._journal_path.read_text(encoding="utf-8").splitlines()
        facts: list[str] = []
        for line in lines[-n:]:
            try:
                facts.append(str(json.loads(line)["fact"]))
            except (json.JSONDecodeError, KeyError):
                log.warning("skipping malformed journal line", extra={"kv": {"line": line[:80]}})
        return facts

    # -- day log ---------------------------------------------------------------

    def log_day(self, kind: str, text: str) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "text": text}
        with (self._daylog_dir / f"{day}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def today_tail(self, n: int = 20) -> list[dict]:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._daylog_dir / f"{day}.jsonl"
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # -- prompt block ----------------------------------------------------------

    def context_block(self) -> str:
        parts: list[str] = []
        s = self.summary()
        if s:
            parts.append(f"MEMORY SUMMARY:\n{s}")
        facts = self.recent_facts()
        if facts:
            parts.append("LEARNED FACTS:\n" + "\n".join(f"- {f}" for f in facts))
        tail = self.today_tail(8)
        if tail:
            parts.append(
                "TODAY SO FAR:\n"
                + "\n".join(f"- [{e.get('kind', '?')}] {e.get('text', '')}" for e in tail)
            )
        return "\n\n".join(parts) if parts else "MEMORY: fresh boot, no history yet."
