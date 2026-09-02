"""Perception: bridge poll loop helpers, screenshot capture and objective-change hashing.

- Poll cadence is 2-4 Hz (Settings.poll_hz, clamped there).
- Screenshots only exist on Windows (dxcam, ``[windows]`` extra) in the console
  session of the game server. On any other platform capture raises
  :class:`ScreenshotUnavailableError` — nothing is ever synthesized.
- The server has no physical monitor: the console session's display comes from
  an indirect display driver (IddCx virtual monitor). DXGI Desktop Duplication
  works against it, but the duplication object is lost whenever the session
  changes (RDP attach/detach, display mode change, driver restart), so the
  grabber re-creates its camera instead of going dark for the rest of the run.
- Encoded frames are 768-px-long-edge JPEG q=60 (CONTRACTS §3/D5: ~448 visual
  tokens; raw 1080p is never sent to a model).
- Objective changes are detected cheaply with a perceptual hash (dHash) of the
  HUD objective-text region so we don't need a model call to notice the
  objective text swapped.
"""

from __future__ import annotations

import io
import sys
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image

from .bridge_client import GameState
from .logsetup import get_logger

log = get_logger("wasted.perception")

JPEG_LONG_EDGE = 768
JPEG_QUALITY = 60

# HUD objective-text region as fractions of the frame (left, top, right, bottom).
# GTA V draws the objective line bottom-center, above the minimap row. Empirical
# convention validated on the server in Phase 3; tune here, shape stays.
OBJECTIVE_REGION = (0.20, 0.885, 0.80, 0.985)


class ScreenshotUnavailableError(RuntimeError):
    """Capture requested somewhere it cannot work; message explains why."""


#: Consecutive empty grabs before the camera is rebuilt. dxcam returns None
#: both for "no new frame since last grab" (normal, common at 3 Hz on a paused
#: game) and for "duplication lost" (needs a rebuild); only the count separates
#: them.
GRAB_FAILURES_BEFORE_REINIT = 15


class ScreenGrabber:
    """dxcam-backed frame grabber. Windows console session only.

    Construction fails loudly off-Windows or when dxcam is missing — callers
    (director vision, event screenshots) treat that as 'no screenshot', never
    as an invented frame.

    ``grab()`` returns the last real frame when dxcam reports "unchanged"; it
    never fabricates one, and after a run of empty grabs it rebuilds the
    capture device and then raises if that fails too. It always returns
    ``(frame, captured_at)`` where ``captured_at`` is the monotonic time the
    frame was actually taken off the wire — it is NOT refreshed when the cached
    frame is handed back, so a caller can tell "this is what the screen looks
    like now" from "the screen has not produced a new frame since".
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ScreenshotUnavailableError(
                f"screenshot capture requires Windows + dxcam; this platform is "
                f"{sys.platform!r}. Install with the [windows] extra on the game server."
            )
        try:
            import dxcam
        except ImportError as exc:
            raise ScreenshotUnavailableError(
                "dxcam is not installed. Install the harness with "
                "`pip install -e .[windows]` on the game server."
            ) from exc
        self._dxcam = dxcam
        self._camera: Any = None
        self._last: Image.Image | None = None
        #: Monotonic time `_last` was captured. Never refreshed by a cache hit.
        self._last_at = 0.0
        self._empty_grabs = 0
        self._create_camera()

    def _create_camera(self) -> None:
        self._camera = self._dxcam.create(output_color="RGB")
        if self._camera is None:
            raise ScreenshotUnavailableError(
                "dxcam.create() returned None — no capturable display. On the "
                "server this means the console session has no display target: "
                "the virtual display driver (IddCx) is not installed/active, or "
                "the harness is running in an RDP session instead of the "
                "console session (RDP breaks Desktop Duplication)."
            )

    def reset(self) -> None:
        """Rebuild the capture device after a duplication loss."""
        old, self._camera = self._camera, None
        try:
            if old is not None and hasattr(old, "release"):
                old.release()
        except Exception as exc:  # a dying camera must not block its replacement
            log.warning("dxcam release failed", extra={"kv": {"error": str(exc)[:120]}})
        self._create_camera()
        self._empty_grabs = 0
        log.info("dxcam camera re-created after capture loss")

    def grab(self) -> tuple[Image.Image, float]:
        """Returns (frame, captured_at) — `captured_at` is monotonic seconds.

        `captured_at` is stamped ONLY where a new frame really arrived. The
        cached-frame branch returns the ORIGINAL stamp, because that is when the
        picture was taken; stamping it "now" is what let a minutes-old frame be
        filed as the screenshot of this death, which is exactly what
        FRAME_MAX_AGE_S exists to prevent. On this box the display is known to
        stop producing frames whenever the game loses focus, so "dxcam has
        nothing new" is a routine state, not a rarity.
        """
        frame = self._camera.grab() if self._camera is not None else None
        if frame is not None:
            self._empty_grabs = 0
            self._last = Image.fromarray(frame)
            self._last_at = time.monotonic()
            return self._last, self._last_at
        self._empty_grabs += 1
        if self._empty_grabs >= GRAB_FAILURES_BEFORE_REINIT:
            self.reset()
            frame = self._camera.grab() if self._camera is not None else None
            if frame is not None:
                self._last = Image.fromarray(frame)
                self._last_at = time.monotonic()
                return self._last, self._last_at
            raise ScreenshotUnavailableError(
                f"dxcam returned no frame {GRAB_FAILURES_BEFORE_REINIT}x and "
                f"again after a device rebuild — Desktop Duplication is gone. "
                f"Check the virtual display and that this process is in the "
                f"console session."
            )
        if self._last is not None:
            # Unchanged frame: dxcam signals it with None. The pixels are still
            # what is on screen, but they are not a NEW observation, so the
            # original capture time travels with them.
            return self._last, self._last_at
        raise ScreenshotUnavailableError(
            "dxcam has not produced a first frame yet (screen unchanged since "
            "capture started, or duplication not ready)"
        )


def encode_jpeg(img: Image.Image, long_edge: int = JPEG_LONG_EDGE, quality: int = JPEG_QUALITY) -> bytes:
    """Downscale to `long_edge` on the long side and encode JPEG. Never upscales."""
    w, h = img.size
    scale = long_edge / max(w, h)
    if scale < 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def objective_region_hash(img: Image.Image, region: tuple[float, float, float, float] = OBJECTIVE_REGION) -> int:
    """dHash (64-bit difference hash) of the HUD objective-text region.

    Cheap, model-free change detector: crop → grayscale → 9x8 → horizontal
    gradient bits. Identical text hashes identically; a new objective line
    flips many bits.
    """
    w, h = img.size
    left, top, right, bottom = region
    crop = img.crop((int(left * w), int(top * h), int(right * w), int(bottom * h)))
    small = crop.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            i = row * 9 + col
            bits = (bits << 1) | (1 if px[i] > px[i + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


# A new objective line flips a large share of the 64 gradient bits; small HUD
# flicker (clock, minimap bleed) flips a few. Threshold tuned on-server Phase 3.
OBJECTIVE_HASH_THRESHOLD = 10


@dataclass
class Delta:
    """What changed between two consecutive /state snapshots."""

    died: bool = False
    respawned: bool = False
    busted: bool = False
    wanted_from: int = 0
    wanted_to: int = 0
    entered_vehicle: bool = False
    exited_vehicle: bool = False
    task_finished: bool = False
    task_failed: bool = False
    mission_started: bool = False
    mission_ended: bool = False
    cutscene_started: bool = False
    cutscene_ended: bool = False
    objective_changed: bool = False
    big_health_drop: bool = False
    danger: bool = False

    @property
    def wanted_changed(self) -> bool:
        return self.wanted_from != self.wanted_to


@dataclass
class Perceptor:
    """Holds the previous snapshot and objective hash; computes Deltas."""

    prev: GameState | None = None
    prev_objective_hash: int | None = None

    def observe(self, state: GameState, objective_hash: int | None = None) -> Delta:
        d = Delta()
        p = self.prev
        if p is not None:
            d.died = state.player.dead and not p.player.dead
            d.respawned = p.player.dead and not state.player.dead
            d.busted = state.player.arrested and not p.player.arrested
            d.wanted_from = p.player.wanted
            d.wanted_to = state.player.wanted
            d.entered_vehicle = state.player.in_vehicle and not p.player.in_vehicle
            d.exited_vehicle = p.player.in_vehicle and not state.player.in_vehicle
            d.mission_started = state.mission.active and not p.mission.active
            d.mission_ended = p.mission.active and not state.mission.active
            d.cutscene_started = state.mission.cutscene_active and not p.mission.cutscene_active
            d.cutscene_ended = p.mission.cutscene_active and not state.mission.cutscene_active
            d.big_health_drop = (p.player.health - state.player.health) >= 25
            lt, plt = state.last_task, p.last_task
            # v1.2: `id` is null until a task has ever been posted. Two nulls are
            # "no task on either side", NOT the same task finishing — comparing
            # them with a bare `==` would let an idle→idle pair look like a
            # completed task the moment a status ever disagreed.
            same_task = lt.id is not None and lt.id == plt.id
            if same_task and plt.status == "running" and lt.status in ("done", "failed"):
                d.task_finished = True
                d.task_failed = lt.status == "failed"
            # Objective movement from the blip (bridge-side identification)…
            ob, pob = state.mission.objective_blip, p.mission.objective_blip
            if (ob is None) != (pob is None):
                d.objective_changed = True
            elif ob is not None and pob is not None:
                moved = (
                    abs(ob.pos.x - pob.pos.x) + abs(ob.pos.y - pob.pos.y) + abs(ob.pos.z - pob.pos.z)
                ) > 5.0
                d.objective_changed = moved
        # …or from the HUD text hash when a frame is available.
        if objective_hash is not None:
            if (
                self.prev_objective_hash is not None
                and hamming(objective_hash, self.prev_objective_hash) >= OBJECTIVE_HASH_THRESHOLD
            ):
                d.objective_changed = True
            self.prev_objective_hash = objective_hash
        hostiles = any(ped.relationship == "hostile" for ped in state.nearby.peds)
        d.danger = (
            state.player.wanted > 0
            or d.big_health_drop
            or hostiles
            or bool(state.vehicle and state.vehicle.in_water)
        )
        self.prev = state
        return d
