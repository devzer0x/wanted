"""Decision schema — EXACTLY CONTRACTS.md §2.

One model call produces the decision AND the commentary; this object is the
whole public feed. Word limits are contract text, enforced as validators: a
response failing validation is retried once by the caller, then the reflex
layer keeps control (CONTRACTS §2).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Mood = Literal["chill", "bored", "hyped", "scared", "smug"]
MOODS: tuple[str, ...] = ("chill", "bored", "hyped", "scared", "smug")

# 11 bridge task types (CONTRACTS §1) + v1 harness-side manual-control
# primitives (CONTRACTS §2). Adding a primitive bumps the contract version.
BRIDGE_TASKS: tuple[str, ...] = (
    "drive_to",
    "walk_to",
    "enter_nearest_vehicle",
    "exit_vehicle",
    "wander_drive",
    "flee_police",
    "combat_hated_targets_around",
    "seek_cover",
    "follow_entity",
    "set_waypoint",
    "stop",
)
PRIMITIVES: tuple[str, ...] = (
    "look_around",
    "brake_tap",
    "swerve",
    "reverse_out",
    "press_prompt_key",
    "wait",
    "radio",
    "horn",
)
ACTION_TYPES: tuple[str, ...] = BRIDGE_TASKS + PRIMITIVES

ActionType = Literal[
    "drive_to",
    "walk_to",
    "enter_nearest_vehicle",
    "exit_vehicle",
    "wander_drive",
    "flee_police",
    "combat_hated_targets_around",
    "seek_cover",
    "follow_entity",
    "set_waypoint",
    "stop",
    "look_around",
    "brake_tap",
    "swerve",
    "reverse_out",
    "press_prompt_key",
    "wait",
    "radio",
    "horn",
]


def _word_count(text: str) -> int:
    return len(text.split())


class ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ActionType = Field(description="One bridge task type or manual-control primitive.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for the action, per the action catalog.",
    )


class DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thought: str = Field(
        description="<=40 words. The agent's private-ish reasoning, shown on site as "
        "'what he was thinking'."
    )
    say: str = Field(
        description="<=20 words. The agent's out-loud line in his voice. This is the commentary."
    )
    mood: Mood
    action: ActionModel
    goal: str = Field(
        description="Current goal in <=12 words, unchanged unless the director changed it."
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("thought")
    @classmethod
    def _thought_words(cls, v: str) -> str:
        if _word_count(v) > 40:
            raise ValueError(f"thought is {_word_count(v)} words; contract max is 40")
        return v

    @field_validator("say")
    @classmethod
    def _say_words(cls, v: str) -> str:
        if _word_count(v) > 20:
            raise ValueError(f"say is {_word_count(v)} words; contract max is 20")
        return v

    @field_validator("goal")
    @classmethod
    def _goal_words(cls, v: str) -> str:
        if _word_count(v) > 12:
            raise ValueError(f"goal is {_word_count(v)} words; contract max is 12")
        return v
