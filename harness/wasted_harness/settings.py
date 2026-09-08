"""Environment-driven settings.

Reads `.env` (python-dotenv) then the process environment. Nothing here invents
defaults for credentials: absent keys stay ``None`` and every consumer fails
loudly (or queues offline, where the spec says so) instead of pretending.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_HARNESS_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """A required setting is missing or invalid. The message says which and how to fix it."""


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is not None and v.strip() == "":
        return default
    return v if v is not None else default


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    bridge_url: str
    supabase_url: str | None
    supabase_secret_key: str | None
    obs_ws_host: str
    obs_ws_port: int
    obs_ws_password: str | None
    overlay_host: str
    overlay_port: int
    hourly_cap_usd: float
    state_dir: Path
    pricing_file: Path
    poll_hz: float = field(default=3.0)  # perception poll rate, clamped 2-4 Hz
    #: Whether the agent may START story missions. Off means: `start_nearest_mission`
    #: is never offered or forced, the day planner never schedules a mission block,
    #: and an incoming story call is left to ring out (answering one starts a
    #: mission whether he is ready or not). Missions that begin some other way are
    #: still played — this is about not walking into them on purpose.
    missions_enabled: bool = field(default=True)
    #: Touch the in-game phone at all? OFF by default (operator, 2026-09-03).
    #: Measured live: `answer_call`/`reject_call` are POST /task, so each one
    #: PREEMPTS whatever he was doing — with Simeon calling every ~30 s the
    #: bridge log was a wall of "walk_to failed: preempted by reject_call" and
    #: he never finished a goal. An unanswered phone rings out on its own and
    #: costs him nothing, so the harness simply leaves it alone.
    phone_enabled: bool = field(default=False)
    #: Generate viewer predictions from live telemetry and drive their
    #: lifecycle (docs/CONTRACTS-PREDICTIONS.md §4 names the harness the
    #: PRIMARY driver of `lock_due_predictions()` / `settle_due_predictions()`;
    #: the Vercel cron is only the backstop for a harness that has died). ON by
    #: default, because a live show with nothing to predict against is the
    #: whole feature missing. `WASTED_PREDICTIONS_ENABLED=false` is the kill
    #: switch on the box — set it there if the prediction migrations have not
    #: been applied to that Supabase project yet, or the inserts will simply
    #: fail and pile up in the offline queue.
    #: OFF by default, deliberately. The prediction layer needs schema that a
    #: Supabase project may not have yet, and a box that starts generating
    #: against a missing table requeues every insert on the game-loop thread
    #: until the loop collapses. The safe default for a machine that runs
    #: unattended is "do nothing until told", so this is opt-in per deployment.
    predictions_enabled: bool = field(default=False)

    @classmethod
    def load(cls, env_file: Path | None = None) -> Settings:
        load_dotenv(
            env_file if env_file is not None else _HARNESS_ROOT / ".env",
            encoding="utf-8",
        )
        try:
            hourly_cap = float(_env("WASTED_HOURLY_CAP_USD", "1.50"))  # type: ignore[arg-type]
        except ValueError as exc:
            raise ConfigError(
                f"WASTED_HOURLY_CAP_USD must be a number, got "
                f"{os.environ.get('WASTED_HOURLY_CAP_USD')!r}"
            ) from exc
        try:
            obs_port = int(_env("OBS_WS_PORT", "4455"))  # type: ignore[arg-type]
            overlay_port = int(_env("WASTED_OVERLAY_PORT", "7788"))  # type: ignore[arg-type]
        except ValueError as exc:
            raise ConfigError(f"port env vars must be integers: {exc}") from exc
        try:
            poll_hz = float(_env("WASTED_POLL_HZ", "3.0"))  # type: ignore[arg-type]
        except ValueError as exc:
            raise ConfigError(
                f"WASTED_POLL_HZ must be a number, got "
                f"{os.environ.get('WASTED_POLL_HZ')!r}"
            ) from exc
        poll_hz = min(4.0, max(2.0, poll_hz))
        return cls(
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            bridge_url=_env("WASTED_BRIDGE_URL", "http://127.0.0.1:7777"),  # type: ignore[arg-type]
            supabase_url=_env("SUPABASE_URL"),
            supabase_secret_key=_env("SUPABASE_SECRET_KEY"),
            obs_ws_host=_env("OBS_WS_HOST", "127.0.0.1"),  # type: ignore[arg-type]
            obs_ws_port=obs_port,
            obs_ws_password=_env("OBS_WS_PASSWORD"),
            overlay_host=_env("WASTED_OVERLAY_HOST", "127.0.0.1"),  # type: ignore[arg-type]
            overlay_port=overlay_port,
            hourly_cap_usd=hourly_cap,
            missions_enabled=str(_env("WASTED_MISSIONS_ENABLED", "true")).strip().lower()
            not in ("0", "false", "no", "off"),
            phone_enabled=str(_env("WASTED_PHONE_ENABLED", "false")).strip().lower()
            in ("1", "true", "yes", "on"),
            predictions_enabled=str(_env("WASTED_PREDICTIONS_ENABLED", "false")).strip().lower()
            not in ("0", "false", "no", "off"),
            # Both paths are anchored to the package, never to the CWD: on the
            # server the harness starts from a scheduled task whose working
            # directory is not the repo. expanduser() so %USERPROFILE%-style
            # overrides written as ~/... behave on Windows too.
            state_dir=Path(
                _env("WASTED_STATE_DIR", str(_HARNESS_ROOT / "state"))  # type: ignore[arg-type]
            ).expanduser(),
            pricing_file=Path(
                _env("WASTED_PRICING_FILE", str(_HARNESS_ROOT / "config" / "pricing.yaml"))  # type: ignore[arg-type]
            ).expanduser(),
            poll_hz=poll_hz,
        )

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)

    def ensure_state_dir(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir
