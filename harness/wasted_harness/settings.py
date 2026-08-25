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

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file if env_file is not None else _HARNESS_ROOT / ".env")
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
        poll_hz = float(_env("WASTED_POLL_HZ", "3.0"))  # type: ignore[arg-type]
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
            state_dir=Path(_env("WASTED_STATE_DIR", str(_HARNESS_ROOT / "state"))),  # type: ignore[arg-type]
            pricing_file=Path(
                _env("WASTED_PRICING_FILE", str(_HARNESS_ROOT / "config" / "pricing.yaml"))  # type: ignore[arg-type]
            ),
            poll_hz=poll_hz,
        )

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)

    def ensure_state_dir(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir
