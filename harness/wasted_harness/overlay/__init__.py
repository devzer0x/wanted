"""Local overlay for the OBS browser source (CONTRACTS §6) — http://127.0.0.1:7788."""

from .app import OverlayBus, create_app

__all__ = ["OverlayBus", "create_app"]
