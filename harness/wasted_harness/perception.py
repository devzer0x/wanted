"""Perception: bridge poll loop helpers, screenshot capture and objective-change hashing.

- Poll cadence is 2-4 Hz (Settings.poll_hz, clamped there).
- Screenshots only exist on Windows (dxcam, ``[windows]`` extra) in the console
  session of the game server. On any other platform capture raises
  :class:`ScreenshotUnavailableError` — nothing is ever synthesized.
- Encoded frames are 768-px-long-edge JPEG q=60 (CONTRACTS §3/D5: ~448 visual
  tokens; raw 1080p is never sent to a model).
- Objective changes are detected cheaply with a perceptual hash (dHash) of the
  HUD objective-text region so we don't need a model call to notice the
  objective text swapped.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field

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


class ScreenGrabber:
    """dxcam-backed frame grabber. Windows console session only.

    Construction fails loudly off-Windows or when dxcam is missing — callers
    (director vision, event screenshots) treat that as 'no screenshot', never
    as an invented frame.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ScreenshotUnavailableError(
                f"screenshot capture requires Windows + dxcam; this platform is "
                f"{sys.platform!r}. Install with the [windows] extra on the game server."
            )
        try:
            import dxcam  # noqa: PLC0415  (windows-only optional dependency)
        except ImportError as exc:
            raise ScreenshotUnavailableError(
                "dxcam is not installed. Install the harness with "
                "`pip install -e .[windows]` on the game server."
            ) from exc
        self._camera = dxcam.create(output_color="RGB")
        if self._camera is None:
            raise ScreenshotUnavailableError(
                "dxcam.create() returned None — no capturable display. On the "
                "server this means the HDMI emulator/console session is not up "
                "(RDP-attached sessions break Desktop Duplication)."
            )

    def grab(self) -> Image.Image:
        frame = self._camera.grab()
        if frame is None:
            # dxcam returns None when the frame did not change or capture was lost.
            raise ScreenshotUnavailableError(
                "dxcam returned no frame (duplication lost or unchanged frame); "
                "re-init the grabber — this happens after RDP attach/detach."
            )
        return Image.fromarray(frame)


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
    _last_task_id: str | None = field(default=None, repr=False)

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
            same_task = lt.id == plt.id
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
