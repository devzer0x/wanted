"""Typed httpx client for the bridge HTTP API (CONTRACTS.md §1, v1.1).

The bridge is the SHVDN script inside the game serving http://127.0.0.1:7777.
Connection refused / timeouts raise :class:`BridgeDownError` with a message the
watchdog and the operator can act on. A 503 ``online_session_active`` raises
:class:`OnlineSessionActiveError` — the harness must stop issuing tasks, that is
the anti-cheat/AUP safety rail, not a transient error.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .logsetup import get_logger

log = get_logger("wasted.bridge")

# The 11 bridge task types (CONTRACTS §1). Harness-side primitives are NOT here:
# the bridge rejects them; they are executed via SendInput (see primitives.py).
BRIDGE_TASK_TYPES: tuple[str, ...] = (
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

DrivingStyle = Literal["normal", "rushed", "ignore_lights", "avoid_traffic"]


class BridgeError(RuntimeError):
    """Base class for bridge failures."""


class BridgeDownError(BridgeError):
    """The bridge did not answer (connection refused / timeout / transport error)."""


class OnlineSessionActiveError(BridgeError):
    """The bridge detected a GTA Online session and disabled itself (503)."""


class BridgeApiError(BridgeError):
    """The bridge answered with an error status."""

    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(f"bridge returned {status} {error}: {detail}")
        self.status = status
        self.error = error
        self.detail = detail


# ---- /state response models (CONTRACTS §1) -----------------------------------


class Vec3(BaseModel):
    model_config = ConfigDict(extra="ignore")
    x: float
    y: float
    z: float


class PlayerState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pos: Vec3
    heading: float
    health: int
    max_health: int
    armor: int
    wanted: int
    cash: int
    dead: bool
    arrested: bool
    in_vehicle: bool
    control_enabled: bool


class VehicleState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    handle: int
    model: str
    display_name: str
    vehicle_class: str = Field(alias="class")
    speed: float
    health: float
    upside_down: bool
    in_water: bool
    stopped_for_s: float


class LocationState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    street: str
    zone: str


class WorldState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    clock: str
    weather: str
    timescale: float


class ObjectiveBlip(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pos: Vec3
    kind: Literal["coord", "entity"]
    handle: int


class MissionState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    active: bool
    random_event_active: bool
    cutscene_active: bool
    objective_blip: ObjectiveBlip | None = None


class NearbyVehicle(BaseModel):
    model_config = ConfigDict(extra="ignore")
    handle: int
    model: str
    display_name: str
    vehicle_class: str = Field(alias="class")
    distance: float
    driver: Literal["player", "npc", "empty"]


class NearbyPed(BaseModel):
    model_config = ConfigDict(extra="ignore")
    handle: int
    model: str
    distance: float
    relationship: Literal["neutral", "hostile"]


class Nearby(BaseModel):
    model_config = ConfigDict(extra="ignore")
    vehicles: list[NearbyVehicle] = []
    peds: list[NearbyPed] = []


class LastTask(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    type: str
    status: Literal["idle", "running", "done", "failed"]
    detail: str = ""


class BridgeInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str
    edition: str


class GameState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ts: str
    tick: int
    player: PlayerState
    vehicle: VehicleState | None = None
    location: LocationState
    world: WorldState
    mission: MissionState
    nearby: Nearby
    last_task: LastTask
    bridge: BridgeInfo


class Health(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str
    edition: str
    tick_hz: float
    queue_depth: int
    game_fps: float
    online_blocked: bool


class UnstickResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    moved: bool
    distance_m: float


# ---- client ------------------------------------------------------------------


class BridgeClient:
    """Synchronous client. One instance per harness process; httpx pools the socket."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:7777",
        timeout_s: float = 2.0,
        connect_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=httpx.HTTPTransport(retries=connect_retries),
        )

    def close(self) -> None:
        self._client.close()

    # -- low-level -------------------------------------------------------------

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._client.request(method, path, json=json_body)
        except httpx.TransportError as exc:
            raise BridgeDownError(
                f"bridge unreachable at {self.base_url}{path} "
                f"({type(exc).__name__}: {exc}). Is the game running with "
                f"WastedBridge loaded? The watchdog treats this as bridge-down."
            ) from exc
        if resp.status_code >= 400:
            error, detail = self._error_fields(resp)
            if resp.status_code == 503 and error == "online_session_active":
                raise OnlineSessionActiveError(
                    "bridge disabled itself: a GTA Online session is active. "
                    "The harness must not drive the game until the bridge reports clear."
                )
            raise BridgeApiError(resp.status_code, error, detail)
        return resp.json()

    @staticmethod
    def _error_fields(resp: httpx.Response) -> tuple[str, str]:
        try:
            body = resp.json()
            return str(body.get("error", "unknown_error")), str(body.get("detail", ""))
        except ValueError:
            return "unknown_error", resp.text[:200]

    # -- endpoints -------------------------------------------------------------

    def get_state(self) -> GameState:
        return GameState.model_validate(self._request("GET", "/state"))

    def get_health(self) -> Health:
        return Health.model_validate(self._request("GET", "/health"))

    def post_task(self, task_type: str, params: dict[str, Any] | None = None) -> str:
        """POST /task. Returns the new task id. Preempts any running task (bridge-side)."""
        if task_type not in BRIDGE_TASK_TYPES:
            raise ValueError(
                f"{task_type!r} is not a bridge task (bridge tasks: "
                f"{', '.join(BRIDGE_TASK_TYPES)}). Harness primitives go through "
                f"primitives.py, not the bridge."
            )
        body = {"type": task_type, "params": params or {}}
        data = self._request("POST", "/task", body)
        return str(data["task_id"])

    def set_timescale(self, value: float) -> float:
        data = self._request("POST", "/timescale", {"value": value})
        return float(data["value"])

    def set_control(self, enabled: bool) -> bool:
        data = self._request("POST", "/control", {"enabled": enabled})
        return bool(data["enabled"])

    def set_radio(self, station: str) -> None:
        self._request("POST", "/radio", {"station": station})

    def horn(self, ms: int) -> None:
        self._request("POST", "/horn", {"ms": max(1, min(3000, int(ms)))})

    def unstick(self) -> UnstickResult:
        """POST /unstick. Raises BridgeApiError(409, unstick_conditions_not_met) when refused."""
        return UnstickResult.model_validate(self._request("POST", "/unstick", {}))
