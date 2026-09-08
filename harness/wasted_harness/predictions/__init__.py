"""Prediction generation: real `/state` -> a prediction row.

This package only PROPOSES predictions (`generator.generate`) and writes them
through the harness's existing Supabase writer (`writer.PredictionWriter`).
Settlement — deciding who was right — is a Postgres function owned by a
different workstream (docs/CONTRACTS-PREDICTIONS.md §3); nothing here reads a
prediction back or judges an outcome.

See `catalog.py` for the template registry and its module docstring for what
was and was not included and why.
"""

from __future__ import annotations

from .catalog import (
    CATALOG,
    TELEMETRY_RULE_KINDS,
    MeasuredRate,
    PredictionTemplate,
    RecentEvent,
    Window,
)
from .generator import GeneratorConfig, PredictionGenerator
from .ticker import PredictionTicker
from .writer import PredictionWriter

__all__ = [
    "CATALOG",
    "TELEMETRY_RULE_KINDS",
    "GeneratorConfig",
    "MeasuredRate",
    "PredictionGenerator",
    "PredictionTemplate",
    "PredictionTicker",
    "PredictionWriter",
    "RecentEvent",
    "Window",
]
