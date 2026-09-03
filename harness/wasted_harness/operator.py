"""The operator's button: un-stick the agent from outside, live, without playing for him.

WHY THIS EXISTS. In one week three automatic stall detectors each failed him in a
different way, and every time the fix was another detector. A live stream cannot
wait for the fourth: a human watching the feed needs a command that works no
matter which watchdog is asleep. This module is that command's entire surface on
the box. It is deliberately tiny — five shapes, all of them things the harness
already knows how to do — so that it stays auditable and so that nothing about
normal play changes unless a command is actually fired.

WHAT IT IS NOT. It is not a remote controller. The five shapes:

* ``nudge``            — "you're stuck, pick something NEW yourself". Closes the
                         locked free-roam goal, bans the stalled task type for a
                         while (the same mechanism the idle watchdog uses), and
                         arms a one-shot ``force_pick`` so the very next roam
                         pick ignores its boredom timer and gap. HE picks.
* ``goal <catalog id>``— lock one free-roam catalog goal now. This one IS the
                         human choosing, which is why every accepted command is
                         logged and emitted as an event: the feed shows it.
* ``task <type> [h]``  — post ONE existing bridge task through the ordinary
                         choke point, exactly as a brain decision would. The
                         friendly ``drive`` expands to enter-a-car-then-wander,
                         the one recovery that always produces motion.
* ``stop``             — clear the task and end the goal without picking a new
                         one; for "let the mission script have him".
* ``status``           — a GET: where he is, how long he has been still, what
                         he is doing, and WHAT IS HOLDING HIM. The diagnostic.

WHERE IT LIVES. The harness already serves a localhost-only FastAPI app for the
OBS overlay (``overlay/app.py``, 127.0.0.1:7788). ``attach`` adds two routes to
it. The app never binds anywhere else, so the only way to reach this is an SSH
session on the box — which is exactly the operator's ``scripts/wanted``.

HONESTY ON STREAM. The show's promise is "no human plays for him". The human is
allowed to say "do something else"; every accepted command becomes an
``operator_nudge`` event so nobody has to take that on trust.

THREADING. FastAPI handles requests on its own thread; the game loop is another.
Commands cross that boundary through a stdlib queue and are applied by the loop,
once per tick, before any planner runs — never from the HTTP thread. ``status``
reads a snapshot the loop publishes each tick, never the loop's live objects.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Module-level on purpose: this file uses `from __future__ import annotations`, so route
# signatures are strings that FastAPI resolves against the module globals. A `Request` imported
# inside `attach` is invisible to that lookup, and FastAPI then treats the parameter as a request
# BODY — every call comes back 422. fastapi is already a hard dependency (the overlay).
from fastapi import Request
from fastapi.responses import JSONResponse

from .logsetup import get_logger

log = get_logger("wasted.operator")

#: How long the task type that was running when the operator nudged stays banned.
#: Same figure the idle watchdog uses (`recovery.IDLE_TYPE_BAN_S`): long enough
#: that the planner cannot immediately re-post the thing that was not working.
NUDGE_TYPE_BAN_S = 90.0

#: The closed vocabulary. Anything else is a 400, by construction.
COMMANDS: frozenset[str] = frozenset({"nudge", "goal", "task", "stop"})

#: Bridge task types an operator may post directly. Not the whole §1 table on
#: purpose: these are the ones that make sense as a one-shot poke from outside.
#: `drive` is a friendly alias this module expands (see :meth:`Directive.steps`).
OPERATOR_TASK_TYPES: frozenset[str] = frozenset(
    {"drive", "shoot_at", "fight_ped", "enter_nearest_vehicle", "wander_drive", "flee_police"}
)

#: Task types that need a target handle.
HANDLE_TASK_TYPES: frozenset[str] = frozenset({"fight_ped"})


class OperatorRefused(ValueError):
    """A command the harness will not queue; the message is the reason the
    operator sees. Carries the currently available goals when that helps."""

    def __init__(self, reason: str, *, available_goals: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.available_goals = available_goals


@dataclass(frozen=True)
class Directive:
    """One validated operator command, ready for the game loop to apply."""

    cmd: str
    arg: str = ""
    handle: int | None = None
    received_at: float = 0.0

    def steps(self) -> list[dict[str, Any]]:
        """The bridge task(s) a `task` directive posts, in order. Empty for the
        non-task shapes. `drive` is the universal un-stick: get into the
        nearest car, then wander — both existing §1 tasks, both always legal
        when he has control."""
        if self.cmd != "task":
            return []
        if self.arg == "drive":
            return [
                {"type": "enter_nearest_vehicle", "params": {"prefer": "any", "search_radius_m": 40.0}},
                {"type": "wander_drive", "params": {"style": "rushed"}},
            ]
        if self.arg == "shoot_at":
            return [{"type": "shoot_at", "params": {}}]
        if self.arg == "fight_ped":
            return [{"type": "fight_ped", "params": {"handle": self.handle}}]
        if self.arg == "enter_nearest_vehicle":
            return [{"type": "enter_nearest_vehicle", "params": {"prefer": "any", "search_radius_m": 40.0}}]
        if self.arg == "wander_drive":
            return [{"type": "wander_drive", "params": {"style": "rushed"}}]
        if self.arg == "flee_police":
            return [{"type": "flee_police", "params": {}}]
        return []


@dataclass
class StatusSnapshot:
    """What `GET /operator/status` returns. Published by the game loop each tick
    (a plain dict copy), read by the HTTP thread — never the loop's live state."""

    data: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def publish(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.data = dict(data)

    def read(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.data)


class OperatorQueue:
    """Validated directives, HTTP thread in, game loop out.

    Validation that needs no game state (shape, vocabulary, handle format)
    happens at enqueue time so the operator gets a 400 immediately. Validation
    that DOES need game state — is he in a cutscene, is that goal on the menu —
    happens at apply time in the loop, and its refusal is logged and surfaced on
    the next `status`.
    """

    def __init__(self, *, clock=time.monotonic, maxsize: int = 8) -> None:
        self._q: queue.Queue[Directive] = queue.Queue(maxsize=maxsize)
        self.clock = clock
        self.status = StatusSnapshot()
        #: Last apply-time refusal, for `status` — so "I ran it and nothing
        #: happened" has an answer.
        self.last_refusal: str | None = None
        self.last_applied: str | None = None

    # -- HTTP side ---------------------------------------------------------------

    def submit(self, cmd: str, arg: str = "", handle: str | int | None = None) -> Directive:
        cmd = (cmd or "").strip().lower()
        arg = (arg or "").strip()
        if cmd not in COMMANDS:
            raise OperatorRefused(
                f"unknown command {cmd!r}; the harness accepts: {', '.join(sorted(COMMANDS))}, status"
            )
        parsed_handle: int | None = None
        if cmd == "goal":
            if not arg:
                raise OperatorRefused("goal needs a catalog id (GET /operator/status lists what is available)")
            if not arg.replace("_", "").isalnum():
                raise OperatorRefused(f"goal id {arg!r} is not a catalog id")
        elif cmd == "task":
            if arg not in OPERATOR_TASK_TYPES:
                raise OperatorRefused(
                    f"task {arg!r} is not operator-postable; allowed: {', '.join(sorted(OPERATOR_TASK_TYPES))}"
                )
            if arg in HANDLE_TASK_TYPES:
                if handle in (None, ""):
                    raise OperatorRefused(f"task {arg} needs a ped handle (see nearby_peds in status)")
                try:
                    parsed_handle = int(handle)  # type: ignore[arg-type]
                except (TypeError, ValueError) as exc:
                    raise OperatorRefused(f"handle {handle!r} is not an integer") from exc
        elif arg:
            # nudge/stop take no argument; a stray one is a typo worth telling the
            # operator about rather than silently dropping.
            raise OperatorRefused(f"{cmd} takes no argument (got {arg!r})")
        d = Directive(cmd=cmd, arg=arg, handle=parsed_handle, received_at=self.clock())
        try:
            self._q.put_nowait(d)
        except queue.Full as exc:
            raise OperatorRefused("operator queue is full; the loop is not draining it — is the harness ticking?") from exc
        log.info("operator command queued", extra={"kv": {"cmd": cmd, "arg": arg, "handle": parsed_handle}})
        return d

    @property
    def pending(self) -> int:
        return self._q.qsize()

    # -- loop side -----------------------------------------------------------------

    def drain(self) -> list[Directive]:
        """Everything queued since the last tick, oldest first. Non-blocking."""
        out: list[Directive] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def refused(self, d: Directive, reason: str) -> None:
        self.last_refusal = f"{d.cmd} {d.arg}".strip() + f": {reason}"
        log.warning("operator command refused at apply time", extra={"kv": {"cmd": d.cmd, "arg": d.arg, "reason": reason}})

    def applied(self, d: Directive, note: str = "") -> None:
        self.last_refusal = None
        self.last_applied = f"{d.cmd} {d.arg}".strip() + (f" ({note})" if note else "")
        log.info("operator command applied", extra={"kv": {"cmd": d.cmd, "arg": d.arg, "note": note}})


# -- apply-time gating (needs game state; pure so it is testable) --------------


def blocking_reason(state: Any, d: Directive) -> str | None:
    """Why the game would not honour this command RIGHT NOW, or None.

    A refused command is better than a swallowed one: the operator sees the
    reason in the reply to `status` and knows to wait or to use a different
    verb. `nudge`/`goal` are also refused inside an active mission, because
    missions belong to the follower — `task drive` and `stop` are the tools
    there, and the message says so.
    """
    p = state.player
    m = state.mission
    if getattr(p, "dead", False):
        return "he is dead; wait for the respawn"
    if getattr(p, "arrested", False):
        return "he is being arrested; wait for the release"
    if getattr(m, "cutscene_active", False):
        return "a cutscene is playing; the game ignores tasks until it ends"
    if getattr(p, "switch_in_progress", False):
        return "a protagonist switch is playing"
    if getattr(m, "retry_in_flight", False):
        return "a mission retry/reload is in flight"
    if not getattr(p, "control_enabled", True):
        return "the game has player control disabled (scripted beat)"
    if d.cmd in ("nudge", "goal") and getattr(m, "active", False):
        return "a mission is active — the mission follower owns him; use `drive` or `stop` here"
    return None


def status_from(
    state: Any,
    *,
    still_for_s: float,
    goal_id: str | None,
    available_goals: list[str],
    held_by: str | None,
    governor_level: int,
    last_applied: str | None,
    last_refusal: str | None,
) -> dict[str, Any]:
    """The `status` document. Built by the loop from things it already has;
    keys are stable because `scripts/wanted` prints them."""
    p = state.player
    m = state.mission
    peds = []
    for ped in (getattr(getattr(state, "nearby", None), "peds", None) or [])[:8]:
        peds.append(
            {
                "handle": ped.handle,
                "model": ped.model,
                "name": getattr(ped, "name", None),
                "distance": round(float(ped.distance), 1),
                "relationship": ped.relationship,
                "attacking_me": bool(getattr(ped, "attacking_me", False)),
            }
        )
    threat = getattr(state, "threat", None)
    veh = getattr(state, "vehicle", None)
    lt = state.last_task
    return {
        "pos": [round(p.pos.x, 1), round(p.pos.y, 1), round(p.pos.z, 1)],
        "street": getattr(state.location, "street", None),
        "zone": getattr(state.location, "zone", None),
        "still_for_s": round(float(still_for_s), 1),
        "in_vehicle": bool(p.in_vehicle),
        "vehicle_speed": None if veh is None else getattr(veh, "speed", None),
        "health": getattr(p, "health", None),
        "wanted": getattr(p, "wanted", None),
        "last_task": {"type": lt.type, "status": lt.status, "detail": lt.detail},
        "goal": goal_id,
        "available_goals": list(available_goals),
        "mission_active": bool(m.active),
        "mission_script": getattr(m, "script", None),
        "cutscene": bool(m.cutscene_active),
        "switch_in_progress": bool(getattr(p, "switch_in_progress", False)),
        "retry_in_flight": bool(getattr(m, "retry_in_flight", False)),
        "control_enabled": bool(getattr(p, "control_enabled", True)),
        "held_by": held_by,
        "governor_level": governor_level,
        "attacker_handle": None if threat is None else getattr(threat, "attacker_handle", None),
        "nearby_peds": peds,
        "last_applied": last_applied,
        "last_refusal": last_refusal,
    }


# -- HTTP routes -----------------------------------------------------------------


def attach(app: Any, q: OperatorQueue) -> None:
    """Add `POST /operator` and `GET /operator/status` to the overlay app.

    Kept as a function the harness calls once at startup (rather than baked
    into `overlay.create_app`) so the overlay stays exactly what it was and the
    operator surface is one obvious, greppable attachment point.
    """
    @app.post("/operator")
    async def operator_post(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"accepted": False, "error": "body must be JSON {cmd, arg?, handle?}"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"accepted": False, "error": "body must be a JSON object"}, status_code=400)
        try:
            d = q.submit(str(body.get("cmd", "")), str(body.get("arg", "") or ""), body.get("handle"))
        except OperatorRefused as exc:
            payload: dict[str, Any] = {"accepted": False, "error": exc.reason}
            if exc.available_goals is not None:
                payload["available_goals"] = exc.available_goals
            return JSONResponse(payload, status_code=400)
        return JSONResponse(
            {
                "accepted": True,
                "cmd": d.cmd,
                "arg": d.arg,
                "handle": d.handle,
                "queued": q.pending,
                "note": "applied by the game loop on its next tick; GET /operator/status shows the result",
            }
        )

    @app.get("/operator/status")
    async def operator_status() -> JSONResponse:
        data = q.status.read()
        if not data:
            return JSONResponse(
                {"error": "no status published yet — the harness has not completed a tick"},
                status_code=503,
            )
        return JSONResponse(data)
