"""OBS clips: SaveReplayBuffer synced on the ReplayBufferSaved event, then upload.

Facts from docs/research/brief-obs.json (D7):
- ``SaveReplayBuffer`` returns before the file exists — the sync point is the
  ``ReplayBufferSaved`` event (carries ``savedReplayPath``); polling
  ``GetLastReplayBufferReplay`` right after returns the PREVIOUS replay.
- After the event, Windows may briefly hold the file handle — open with retry.
- Uploads go to the `clips` bucket at a NEW timestamped path with explicit
  content-type (CDN staleness on overwrite; supabase-py defaults to text/html).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .events import SupabaseWriter
from .logsetup import get_logger
from .settings import Settings

log = get_logger("wasted.obs")

REPLAY_SAVE_TIMEOUT_S = 20.0
FILE_OPEN_RETRIES = 12
FILE_OPEN_DELAY_S = 0.5
CONNECT_RETRIES = 3


class ObsError(RuntimeError):
    """OBS is unreachable or the replay pipeline failed; message says which step."""


class ObsClips:
    """Owns the ReqClient + EventClient pair and the clip pipeline."""

    def __init__(self, settings: Settings, writer: SupabaseWriter) -> None:
        self._settings = settings
        self._writer = writer
        self._req = None
        self._events = None
        self._saved_path: str | None = None
        self._saved_event = threading.Event()

    # -- connection ------------------------------------------------------------

    def connect(self) -> None:
        """Connect with retry. Raises ObsError with the real reason after retries."""
        import obsws_python as obs  # deferred import: OBS is optional off-server

        last_exc: Exception | None = None
        for attempt in range(1, CONNECT_RETRIES + 1):
            try:
                kwargs = dict(
                    host=self._settings.obs_ws_host,
                    port=self._settings.obs_ws_port,
                    password=self._settings.obs_ws_password or "",
                    timeout=5,
                )
                self._req = obs.ReqClient(**kwargs)
                self._events = obs.EventClient(**kwargs)
                self._events.callback.register(self._make_saved_callback())
                version = self._req.get_version()
                log.info(
                    "obs connected",
                    extra={"kv": {"obs": version.obs_version, "ws": version.obs_web_socket_version}},
                )
                return
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "obs connect failed",
                    extra={"kv": {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"[:160]}},
                )
                time.sleep(min(2.0 * attempt, 5.0))
        raise ObsError(
            f"cannot connect to obs-websocket at "
            f"{self._settings.obs_ws_host}:{self._settings.obs_ws_port} after "
            f"{CONNECT_RETRIES} attempts ({type(last_exc).__name__}: {last_exc}). "
            f"Is OBS running with the websocket server enabled?"
        ) from last_exc

    def _make_saved_callback(self):
        parent = self

        # obsws-python dispatches by function NAME: on_<snake_case(EventType)>.
        def on_replay_buffer_saved(data):
            parent._saved_path = data.saved_replay_path
            parent._saved_event.set()

        return on_replay_buffer_saved

    @property
    def connected(self) -> bool:
        return self._req is not None

    # -- clip pipeline ---------------------------------------------------------

    def ensure_replay_buffer_active(self) -> None:
        assert self._req is not None, "call connect() first"
        status = self._req.get_replay_buffer_status()
        if not status.output_active:
            log.info("replay buffer inactive; starting it")
            self._req.start_replay_buffer()
            time.sleep(1.0)
            status = self._req.get_replay_buffer_status()
            if not status.output_active:
                raise ObsError(
                    "replay buffer did not start (check OBS Output settings; it is "
                    "unavailable with the Custom Output (FFmpeg) recording type)"
                )

    def save_replay(self) -> Path:
        """SaveReplayBuffer and wait for the real file. Returns the local path."""
        if self._req is None:
            raise ObsError("not connected to OBS; call connect() first")
        self.ensure_replay_buffer_active()
        self._saved_event.clear()
        self._saved_path = None
        self._req.save_replay_buffer()
        if not self._saved_event.wait(REPLAY_SAVE_TIMEOUT_S):
            raise ObsError(
                f"ReplayBufferSaved event did not arrive within {REPLAY_SAVE_TIMEOUT_S}s "
                f"after SaveReplayBuffer — replay was not written"
            )
        assert self._saved_path is not None
        return Path(self._saved_path)

    @staticmethod
    def _read_with_retry(path: Path) -> bytes:
        """Windows can hold the handle briefly after the saved event; retry."""
        last_exc: Exception | None = None
        for _ in range(FILE_OPEN_RETRIES):
            try:
                with path.open("rb") as f:
                    return f.read()
            except OSError as exc:
                last_exc = exc
                time.sleep(FILE_OPEN_DELAY_S)
        raise ObsError(
            f"replay file {path} unreadable after "
            f"{FILE_OPEN_RETRIES * FILE_OPEN_DELAY_S:.0f}s ({last_exc})"
        ) from last_exc

    def capture_clip(
        self,
        event_id: int | None,
        event_type: str,
        caption: str,
        duration_s: float = 30.0,
    ) -> int | None:
        """Full pipeline: save → wait → read → upload → clips row (+ §4 clip event).

        Returns the clips row id, or None when the row had to be queued offline
        (in which case the dependent `clip` event is skipped — its payload
        requires a real clip_id, and we don't invent ids).
        """
        local = self.save_replay()
        data = self._read_with_retry(local)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        storage_path = f"{self._writer.session_id or 'nosession'}/{ts}-{event_type}{local.suffix or '.mp4'}"
        content_type = "video/mp4" if local.suffix.lower() in ("", ".mp4") else "video/x-matroska"
        if not self._writer.configured:
            log.warning(
                "supabase not configured: replay saved locally only",
                extra={"kv": {"path": str(local)}},
            )
        else:
            try:
                self._writer._get_client().storage.from_("clips").upload(
                    path=storage_path,
                    file=data,
                    file_options={"content-type": content_type, "cache-control": "3600"},
                )
            except Exception as exc:
                raise ObsError(
                    f"clip upload to bucket 'clips' failed "
                    f"({type(exc).__name__}: {exc}); local file kept at {local}"
                ) from exc
        clip_id = self._writer.insert_clip(
            {
                "event_id": event_id,
                "storage_path": storage_path,
                "duration_s": duration_s,
                "caption": caption,
            }
        )
        if clip_id is not None:
            self._writer.record_event("clip", {"clip_id": clip_id, "event_type": event_type})
        else:
            log.warning(
                "clips row queued offline; §4 clip event skipped (needs a real clip_id)"
            )
        return clip_id

    def close(self) -> None:
        for client in (self._events, self._req):
            try:
                if client is not None:
                    client.disconnect()
            except Exception:
                pass
        self._req = self._events = None
