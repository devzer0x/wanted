"""Typed httpx client for the bridge HTTP API (CONTRACTS.md §1, v1.2).

The bridge is the SHVDN script inside the game serving http://127.0.0.1:7777.
Connection refused / timeouts raise :class:`BridgeDownError` with a message the
watchdog and the operator can act on. A 503 ``online_session_active`` raises
:class:`OnlineSessionActiveError` — the harness must stop issuing tasks, that is
the anti-cheat/AUP safety rail, not a transient error.

**v1.2 error handling.** The bridge's error-code set is enumerated and closed
per contract version (:data:`BRIDGE_ERROR_STATUS`). Three of them —
``not_ready``, ``game_thread_stalled``, ``queue_full`` — are the bridge working
correctly and saying "not now": they are the normal answers while the game is on
its loading screen. They raise :class:`BridgeTransientError` so the loop can
wait instead of dying. Codes this version has never heard of are **also**
treated as transient (logged loudly, never crashed on), which is exactly what
the contract requires of consumers.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    "fight_ped",
    "set_waypoint",
    "stop",
)

DrivingStyle = Literal["normal", "rushed", "ignore_lights", "avoid_traffic"]

#: CONTRACTS v1.2 — the closed bridge error-code set and its HTTP status.
#: Adding a code bumps the contract version; this table is the harness's copy of
#: it, and anything NOT in it is handled by the unknown-code rule below.
BRIDGE_ERROR_STATUS: dict[str, int] = {
    "online_session_active": 503,
    "not_ready": 503,
    "game_thread_stalled": 503,
    "queue_full": 503,
    "unknown_task_type": 400,
    "invalid_params": 400,
    "invalid_json": 400,
    "not_in_vehicle": 409,
    "unstick_conditions_not_met": 409,
}

#: Codes that mean "the bridge is fine, the game is not ready yet". Normal
#: during startup, a loading screen, or a heavy stream-in; the loop waits and
#: retries and must never treat them as fatal.
TRANSIENT_BRIDGE_ERRORS: frozenset[str] = frozenset(
    {"not_ready", "game_thread_stalled", "queue_full"}
)

#: /health.edition and /state.bridge.edition (v1.2 (b)): `unknown` is a legal
#: value before edition detection completes, so nothing may assume `legacy`.
KNOWN_EDITIONS: frozenset[str] = frozenset({"legacy", "enhanced", "unknown"})
UNKNOWN_EDITION = "unknown"

#: CONTRACTS v1.7 `player.protagonist` / `mission.starts[].protagonist`. Kept as
#: a plain `str` on the models (same reasoning as `bridge.edition`): a value a
#: later bridge invents must be tolerated, not crash the show. This set is what
#: the harness knows how to act on; anything else behaves like `unknown`.
KNOWN_PROTAGONISTS: frozenset[str] = frozenset(
    {"michael", "franklin", "trevor", "unknown"}
)
UNKNOWN_PROTAGONIST = "unknown"


class BridgeError(RuntimeError):
    """Base class for bridge failures."""


class BridgeDownError(BridgeError):
    """The bridge did not answer (connection refused / timeout / transport error)."""


class OnlineSessionActiveError(BridgeError):
    """The bridge detected a GTA Online session and disabled itself (503)."""

    def __init__(self, message: str, status: int = 503, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.error = "online_session_active"
        self.detail = detail


class BridgeApiError(BridgeError):
    """The bridge answered with an error status that is the caller's problem."""

    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(f"bridge returned {status} {error}: {detail}")
        self.status = status
        self.error = error
        self.detail = detail


class BridgeTransientError(BridgeApiError):
    """"Not now": a v1.2 transient 503, or a code this contract version does not know.

    Callers retry these. It subclasses :class:`BridgeApiError` so existing
    ``except BridgeApiError`` handlers still see them, but the loop catches this
    class first and simply waits.
    """


def classify_bridge_error(
    status: int, error: str, detail: str, method: str = "", path: str = ""
) -> BridgeError:
    """Map one bridge error body onto the exception the caller should see.

    The whole v1.2 code set is handled explicitly. The unknown-code rule is the
    contract's own: *log it and treat it as a transient failure*, so a bridge
    that gains a code before the harness does degrades into a retry rather than
    a crashed show.
    """
    where = f"{method} {path}".strip() or "bridge"
    expected = BRIDGE_ERROR_STATUS.get(error)
    if expected is None:
        log.warning(
            "bridge returned an error code outside CONTRACTS v1.2; treating it "
            "as transient and retrying (contract rule for unknown codes)",
            extra={"kv": {"where": where, "status": status, "error": error, "detail": detail[:160]}},
        )
        return BridgeTransientError(status, error, detail)
    if expected != status:
        # Not fatal — the code decides the handling — but somebody's contract
        # copy is stale and that is worth seeing in the log.
        log.warning(
            "bridge error code arrived with an off-contract status",
            extra={"kv": {"where": where, "error": error, "status": status, "expected": expected}},
        )
    if error == "online_session_active":
        return OnlineSessionActiveError(
            "bridge disabled itself: a GTA Online session is active. "
            "The harness must not drive the game until the bridge reports clear.",
            status=status,
            detail=detail,
        )
    if error in TRANSIENT_BRIDGE_ERRORS:
        log.info(
            "bridge not ready yet; retrying",
            extra={"kv": {"where": where, "error": error, "detail": detail[:160]}},
        )
        return BridgeTransientError(status, error, detail)
    return BridgeApiError(status, error, detail)


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
    #: v1.7: which of the three protagonists is being played right now
    #: (`michael|franklin|trevor|unknown`). Optional with the contract's own
    #: default so a pre-v1.7 bridge still parses — the same widening rule
    #: v1.5's `friendly` and v1.6's `pos` went through before the bridge
    #: started emitting them.
    protagonist: str = UNKNOWN_PROTAGONIST
    #: v1.11: a protagonist switch (the aerial fly-over between Michael/
    #: Franklin/Trevor) is playing. Treat exactly like `mission.cutscene_active`:
    #: act on nothing, say nothing about "being the wrong character" — this is
    #: the real signal behind that narration, which used to have no field to
    #: read. Optional so a pre-v1.11 bridge still parses.
    switch_in_progress: bool = False


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


class RouteBlip(BaseModel):
    """CONTRACTS v1.8 `mission.route_blips[]`: one blip the game has plotted a GPS route to.

    This is the yellow line on the minimap, made readable as data. `objective_blip`
    is the bridge's single best pick out of this same set; the list is here so a
    wrong pick is recoverable instead of invisible, and so the case with two live
    routes (a follow target plus a drop-off) can be reasoned about at all.

    `kind == "entity"` means the route points at something that MOVES, and `handle`
    can be handed straight to `follow_entity` — which is the whole point: on a
    follow mission the game draws a route to the car you are tailing and never
    draws a destination marker, so this is the only structured evidence that the
    objective is a moving thing rather than a place.
    """

    model_config = ConfigDict(extra="ignore")
    pos: Vec3
    kind: Literal["coord", "entity"]
    handle: int
    #: SHVDN BlipColor member name, e.g. "Yellow". Plain `str`, not a Literal: the
    #: bridge serializes an enum index with no name as its integer rendered as a
    #: string, and CONTRACTS v1.8 requires consumers to tolerate that. A closed
    #: Literal here would reject the whole /state over a cosmetic field.
    color: str = ""


class MissionStart(BaseModel):
    """CONTRACTS v1.7 `mission.starts[]`: one mission-start marker on the map.

    These are the game's own per-protagonist letter blips (M/F/T), identified
    bridge-side by the blip colours the game assigns them. Walking or driving
    into the corona at `pos` is what starts that job — there is no keypress and
    no harness-side "start mission" call, which is exactly why this is Story
    Mode legal: it is the thing a human does with the same two hands.
    """

    model_config = ConfigDict(extra="ignore")
    pos: Vec3
    #: michael | franklin | trevor | unknown. Plain `str` for the same reason
    #: `player.protagonist` is: an unknown value degrades, it never crashes.
    protagonist: str = UNKNOWN_PROTAGONIST


class EntityBlip(BaseModel):
    """CONTRACTS v1.11 `mission.entity_blips[]`: a blip pinned to a ped/vehicle,
    whether or not the game has plotted a route to it.

    This is the long-range source that outlives `nearby.peds`' ~50 m radius:
    when a followed crewmate drives off, his dot (and his name) are still here
    while he has long since vanished from `nearby`. `name` is the game's own
    map-legend text (`Blip.GetAppropriateName()`, e.g. "Lamar"), so it is a
    legitimate source for `present_names` grounding even when the ped himself
    is out of scan range.
    """

    model_config = ConfigDict(extra="ignore")
    pos: Vec3
    handle: int
    #: SHVDN BlipColor member name; see RouteBlip.color for the same tolerance
    #: note (an unnamed enum index serializes as its integer as a string).
    color: str = ""
    is_route: bool = False
    distance: float = 0.0
    name: str | None = None


class MissionState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    active: bool
    random_event_active: bool
    cutscene_active: bool
    objective_blip: ObjectiveBlip | None = None
    #: v1.7: the mission-start markers currently on the map. Optional (default
    #: `[]`) so a pre-v1.7 bridge still parses; an empty list and an absent
    #: field mean the same honest thing — no job markers are known right now.
    starts: list[MissionStart] = []
    #: v1.8: blips the game has an active GPS route to, nearest first, at most 5.
    #: Optional (default `[]`) so a pre-v1.8 bridge still parses. The bridge has
    #: emitted this since 2026-09-02; nothing on the harness side read it until
    #: now, so the agent was blind to the one piece of state that says "the game has
    #: already worked out where you are supposed to go".
    route_blips: list[RouteBlip] = []
    #: v1.10: the active story-mission script name (e.g. "armenian1"), from the
    #: pinned mission-script table in docs/RESEARCH.md; null when unknown. Only
    #: ever non-null while `active`. Optional (default `None`) so a pre-v1.10
    #: bridge still parses. There is no verified script-name -> English-title
    #: table (docs/research/brief-mission-scripts.json), so this is an exact
    #: identity key for `brain.mission_knowledge`'s LEARNED mapping, never a
    #: display name on its own.
    script: str | None = None
    #: v1.11: every blip pinned to a ped/vehicle, nearest first, at most 8 — see
    #: :class:`EntityBlip`. Optional (default `[]`) so a pre-v1.11 bridge still
    #: parses.
    entity_blips: list[EntityBlip] = []
    #: v1.11: the game's own `mission_repeat_controller` thread is running — a
    #: retry / checkpoint reload is in progress. Suppress tasks and commentary
    #: while true, same as `cutscene_active`. Optional so a pre-v1.11 bridge
    #: still parses.
    retry_in_flight: bool = False


class NearbyVehicle(BaseModel):
    model_config = ConfigDict(extra="ignore")
    handle: int
    model: str
    display_name: str
    vehicle_class: str = Field(alias="class")
    distance: float
    driver: Literal["player", "npc", "empty"]
    #: v1.6: world position of the vehicle. Optional so a pre-v1.6 bridge still parses.
    pos: Vec3 | None = None


class NearbyPed(BaseModel):
    model_config = ConfigDict(extra="ignore")
    handle: int
    model: str
    distance: float
    #: v1.5 adds "friendly" (mission crew / companions). Widened here BEFORE the bridge emits it:
    #: a closed Literal would otherwise reject every /state the moment a crewmate is nearby.
    relationship: Literal["neutral", "hostile", "friendly"]
    #: v1.6: world position of the ped - the minimap dot as data (red = hostile, blue = friendly).
    #: Optional so a pre-v1.6 bridge still parses.
    pos: Vec3 | None = None
    #: v1.10: the vehicle this ped is seated in, or null on foot. Occupants of
    #: nearby vehicles are now included in the ped scan (the raw native alone
    #: omits them — the exact bug that made a followed friendly vanish the
    #: moment he got into a car). Optional so a pre-v1.10 bridge still parses.
    in_vehicle_handle: int | None = None
    #: v1.11: true while this ped's melee swing targets the player (before
    #: impact), while the ped is tasked into combat against the player (the
    #: frame the game tasks them, before the first punch), or once the player
    #: has actually been damaged by them. Deliberately ignores vehicle damage —
    #: a ped whose car clipped us is not an attacker. Optional so a pre-v1.11
    #: bridge still parses.
    attacking_me: bool = False
    #: v1.11: what this ped is attacking with, when known. Optional (default
    #: None) so a pre-v1.11 bridge still parses.
    weapon_class: Literal["unarmed", "melee", "gun", "projectile", "unknown"] | None = None


#: Animal ped models. In this engine animals ARE peds, so `World.GetNearbyPeds` returns cats,
#: dogs, coyotes and the rest, and the engine reports a stray cat's relationship to the player as
#: `hostile`. Observed live 2026-09-02: a cat 29 m away was the ONLY hostile in the snapshot, the
#: threat reflex issued `combat_hated_targets_around`, that task never completes while the cat is
#: alive and nearby, and the agent stood in a hedge "fighting" it for minutes with a story mission
#: waiting — moving 0.2 m in 20 s. Filtering them here keeps them out of BOTH the brain's context
#: and the threat reflex. Names are the model strings the bridge emits (lowercase).
ANIMAL_PED_MODELS: frozenset[str] = frozenset(
    {
        "cat", "chop", "chimp", "cow", "coyote", "deer", "dolphin", "fish", "hen", "humpback",
        "husky", "killerwhale", "mtlion", "pig", "poodle", "pug", "rabbit", "retriever",
        "rottweiler", "seagull", "shepherd", "stingray", "tigershark", "westy", "boar", "crow",
        "hammerhead", "rat", "pigeon",
    }
)


def is_animal_model(model: str | None) -> bool:
    """True when this ped model is an animal rather than a person."""
    return (model or "").strip().lower() in ANIMAL_PED_MODELS


class Nearby(BaseModel):
    model_config = ConfigDict(extra="ignore")
    vehicles: list[NearbyVehicle] = []
    peds: list[NearbyPed] = []

    @field_validator("peds", mode="after")
    @classmethod
    def _drop_animals(cls, peds: list[NearbyPed]) -> list[NearbyPed]:
        """Animals are peds in this engine and arrive flagged `hostile`; see ANIMAL_PED_MODELS."""
        return [p for p in peds if not is_animal_model(p.model)]


class LastTask(BaseModel):
    """CONTRACTS §1 `last_task`.

    **v1.2:** `id` and `type` are `null` until a task has been posted (fresh
    bridge load / script reload); `status` and `detail` are always present. The
    harness's very first poll of a session sees exactly that document, so these
    two fields are Optional and every consumer treats them as nullable.
    """

    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    type: str | None = None
    status: Literal["idle", "running", "done", "failed"]
    detail: str = ""


class BridgeInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str
    #: legacy | enhanced | unknown (v1.2 (b)) — kept as a plain str so an
    #: edition value a later bridge invents is tolerated, not crashed on.
    edition: str


class ThreatState(BaseModel):
    """CONTRACTS v1.11 `threat`: the nearest ped actively attacking, and
    whoever is pulling him out of a car.

    This is the field that fixed the "beaten to death while standing still"
    failure: `combat_hated_targets_around` silently no-ops unless a nearby
    ped's relationship is Neutral/Dislike/Hate, and a carjack victim is
    plausibly still Respect/Like. `attacker_handle` is target-explicit and
    goes straight to `fight_ped`, no relationship lookup required.
    """

    model_config = ConfigDict(extra="ignore")
    attacker_handle: int | None = None
    being_jacked_by: int | None = None


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
    #: v1.11. Optional with its own empty default so a pre-v1.11 bridge still
    #: parses; both handles are then None, which is exactly the "no info yet"
    #: state the DamageTracker fallback path expects.
    threat: ThreatState = ThreatState()


class Health(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str
    #: legacy | enhanced | unknown (v1.2 (b)); `unknown` before detection finishes.
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
            raise classify_bridge_error(resp.status_code, error, detail, method, path)
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
