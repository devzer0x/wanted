"""Feed a recorded (or synthetic) `/state` stream through the REAL selection code.

**What "real" means here.** Every method that decides what the agent does —
`Harness._reflex`, `Harness._drive_day_plan`, `Harness._drive_activities` (and
its roam-goal helpers), `Harness._drive_mission_objective`, `Harness._think`,
`Harness._dynamic_context`, `Harness._apply_decision`, `Harness._execute_action`
— is bound onto :class:`ReplayHarness` UNMODIFIED, by class-level attribute
assignment, the same idiom `tests/test_mission_following.py::_ReflexStub` and
`tests/test_interior_escape.py::_LoopStub` already use (read both before
writing this — this file does not import them; see `tests/support/states.py`'s
docstring for why it is an independent copy rather than an import from a file
owned by a different, concurrently-active executor). Only the collaborators
that would touch the network, the filesystem beyond a throwaway temp dir, or a
real game are stood in for: the bridge (`RecordingBridge`), SendInput
(`RecordingPrimitives`), Supabase (`RecordingWriter`), the overlay bus
(`RecordingBus`), and the brain's network seam (`support.fakebrain`).

**The tick this drives**, matching `Harness.run()`'s body from the point a
`/state` snapshot is in hand (see `main.py:run()`, the `while not self._stop`
loop) through the end of the movement tick:

    reflex -> (director or tactical decision, else idle) -> day plan ->
    free-roam activities -> mission-objective following -> wheel.end_tick()

**What is deliberately NOT driven** (see the module docstring's own "NOT
VERIFIED" list and this package's brief for why): bridge polling and its
exception handling (there is no bridge), game-restart detection, the
blocking-screen watchdog and its SendInput keypress (there is no real screen
to freeze), the timescale guard, the screenshot-grab pump, humanizer breaks,
the governor L3 scenic-park flow, heartbeat/flush. `self._screen_blocked`
therefore never becomes True and `self.breaks.on_break` never becomes True in
a replay — both are real fields the production code reads, just never driven
here.

**Real-time neutralised, not game-time.** `main.py` reads `time.monotonic()`/
`time.sleep()` directly (module-level `import time`) in a few places —
`_quiet_until` bookkeeping and `_apply_decision`'s ~300-900 ms "reaction delay"
sleeps — rather than through an injectable clock the way every `behavior.*`
component does. Replaying a 12-minute recording must not cost 12 minutes of
wall clock, so :func:`replay` patches `wasted_harness.main.time` (module
attribute, not the stdlib module itself) to a shim whose `monotonic()` reads
the SAME `FakeClock` every `behavior.*` component was constructed with and
whose `sleep()` is a no-op. This changes no logic: `_quiet_until` still
expires at the right SIMULATED instant, because the clock backing both
`time.monotonic()` calls and every component's own `clock=` is the one clock
driven by the recorded `ts` deltas.
"""

from __future__ import annotations

import json
import random
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import wasted_harness.main as main_mod
from support.fakebrain import install as install_fake_brain
from support.states import make_state
from wasted_harness.behavior.activities import ActivityPicker, ActivityRunner
from wasted_harness.behavior.humanizer import IdlePicker, MoodModel
from wasted_harness.behavior.missions import MissionFollower, MissionTracker
from wasted_harness.behavior.planner import DayPlanner
from wasted_harness.behavior.recovery import (
    ApiBackoff,
    ClearedByGameBackoff,
    DamageTracker,
    DeathArrestRecovery,
    IdleBreaker,
    JackHandoffGate,
    RoadDodge,
    StrandedEscalator,
    StuckDetector,
    TaskStallDetector,
    ThreatLatch,
    WaterEscalator,
)
from wasted_harness.behavior.roam import HouseEscape, InteriorEscape, RoamEngine
from wasted_harness.behavior.vehicle import ControlRegained, MovementWheel, VehicleController
from wasted_harness.brain.director import DirectorBrain
from wasted_harness.brain.director import DirectorCadence as _DirectorCadence
from wasted_harness.brain.memory import Memory
from wasted_harness.brain.mission_knowledge import LearnedScripts
from wasted_harness.brain.tactical import TacticalBrain
from wasted_harness.brain.tactical import TacticalCadence as _TacticalCadence
from wasted_harness.bridge_client import BridgeApiError, GameState
from wasted_harness.budget import BudgetGovernor
from wasted_harness.commentary import Commentary
from wasted_harness.main import INITIAL_GOAL, Harness
from wasted_harness.perception import Delta, Perceptor
from wasted_harness.settings import Settings
from wasted_harness.totals import TOTALS_FILENAME, LifetimeTotals

# --------------------------------------------------------------------------- #
#  clock                                                                      #
# --------------------------------------------------------------------------- #


class FakeClock:
    """Same tiny idiom as `test_mission_following.FakeClock` — advanced
    explicitly by the caller, never by the wall clock."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"clock cannot go backwards ({seconds}s)")
        self.t += seconds


class _FakeTimeModule:
    """Stands in for `wasted_harness.main`'s module-level `import time`.

    Only `monotonic()` and `sleep()` are used by `main.py` (verified by
    grepping every `time\\.` occurrence in it before writing this) — nothing
    else needs a shim.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.t

    def sleep(self, _seconds: float) -> None:
        # Real time neutralised, not simulated time: the recorded ts deltas
        # already model however long the harness "spent" reacting. Sleeping
        # for real here would make replaying a 12-minute recording take
        # 12-real-minutes-plus for no behavioural gain.
        return None


def _throwaway_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"wasted-replay-{prefix}-"))


# --------------------------------------------------------------------------- #
#  recording collaborators                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class ReplayLog:
    """Every structured fact the replay observed, in tick order.

    Kept as one flat list of dicts (jsonl-shaped, `kind` discriminates) rather
    than five separate lists: `tools/funcheck.py` reads this exact shape off
    disk and a single ordered stream is what lets it reconstruct "what
    happened in this 10-minute window" without reassembling five timelines.
    """

    records: list[dict[str, Any]] = field(default_factory=list)
    #: One entry per fed state, `{tick, clock_t, wall_ts, state}` — funcheck's
    #: window-based checks (F1 idle ratio, F4/F5 per-hour rates) need the
    #: state itself (control_enabled, cutscene, task status), not just events.
    states: list[dict[str, Any]] = field(default_factory=list)

    def add(self, kind: str, tick: int, clock_t: float, **fields: Any) -> None:
        self.records.append({"kind": kind, "tick": tick, "clock_t": round(clock_t, 3), **fields})

    def add_state(self, tick: int, clock_t: float, wall_ts: str, state: GameState) -> None:
        self.states.append(
            {
                "tick": tick,
                "clock_t": round(clock_t, 3),
                "wall_ts": wall_ts,
                "state": json.loads(state.model_dump_json(by_alias=True)),
            }
        )

    def write_jsonl(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for rec in self.records:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def write_states_jsonl(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for rec in self.states:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")


class RecordingBridge:
    """Stands in for `BridgeClient`. Every `POST /task` is recorded with the
    movement wheel's CURRENT holder — accurate for every :data:`MOVEMENT_TASKS`
    post, because `Harness._execute_action` only reaches `bridge.post_task`
    after confirming `wheel.holds(token)`, i.e. after `wheel.owner == token.owner`
    already. Never actually posts anything anywhere."""

    def __init__(self, wheel: MovementWheel, log: ReplayLog, clock: FakeClock) -> None:
        self._wheel = wheel
        self._log = log
        self._clock = clock
        self._n = 0
        self.tick = 0

    def post_task(self, task_type: str, params: dict[str, Any]) -> str:
        self._n += 1
        task_id = f"t-{self._n}"
        self._log.add(
            "posted_task",
            self.tick,
            self._clock.t,
            movement=True,
            type=task_type,
            params=dict(params),
            owner=self._wheel.owner,
            task_id=task_id,
        )
        return task_id

    def set_radio(self, station: str) -> None:
        self._log.add("radio", self.tick, self._clock.t, station=station, owner=self._wheel.owner)

    def horn(self, ms: int) -> None:
        self._log.add("horn", self.tick, self._clock.t, ms=ms, owner=self._wheel.owner)

    def set_timescale(self, value: float) -> float:
        # Never actually reached: THINKING_TIMESCALE == NORMAL_TIMESCALE == 1.0
        # in the shipped code, so `Harness._think`'s dip branch is dead code
        # (see main.py's own comment on THINKING_TIMESCALE). Implemented
        # harmlessly in case that ever changes.
        return value

    def unstick(self) -> Any:
        # A replay has no wedged car to nudge for real; refusing exactly the
        # way the bridge refuses when its own preconditions are not met is the
        # honest default (CLAUDE.md rule 5: no invented movement, ever).
        self._log.add("unstick_attempted", self.tick, self._clock.t, refused=True)
        raise BridgeApiError(409, "unstick_conditions_not_met", "replay stub: never stuck for real")


class RecordingPrimitives:
    """Stands in for `primitives.Primitives` (SendInput, Windows-only)."""

    def __init__(self, wheel: MovementWheel, log: ReplayLog, clock: FakeClock, rng: random.Random) -> None:
        self._wheel = wheel
        self._log = log
        self._clock = clock
        self.rng = rng  # some primitives.py callers read `.rng` off this object

    def execute(self, action_type: str, params: dict[str, Any]) -> None:
        self._log.add(
            "posted_task",
            self._wheel.tick,
            self._clock.t,
            movement=False,
            type=action_type,
            params=dict(params),
            owner=self._wheel.owner,
        )

    def press_key(self, key: str) -> None:
        self._log.add("keypress", self._wheel.tick, self._clock.t, key=key)


class RecordingBus:
    """Stands in for `overlay.OverlayBus`. Every publish is a `say` line, a
    banner, a counters update or a governor-level note — CONTRACTS §6's event
    types — and every one is what a viewer's browser would have received."""

    def __init__(self, log: ReplayLog, wheel: MovementWheel, clock: FakeClock) -> None:
        self._log = log
        self._wheel = wheel
        self._clock = clock

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        self._log.add("bus", self._wheel.tick, self._clock.t, bus_kind=kind, payload=dict(payload))
        if kind == "say":
            self._log.add(
                "say",
                self._wheel.tick,
                self._clock.t,
                text=payload.get("text", ""),
                mood=payload.get("mood"),
            )


class RecordingWriter:
    """Stands in for `events.SupabaseWriter`. Records every §4 event and every
    decision; never touches Supabase or the offline queue."""

    def __init__(self, log: ReplayLog, wheel: MovementWheel, clock: FakeClock) -> None:
        self._log = log
        self._wheel = wheel
        self._clock = clock
        self._next_event_id = 0

    def record_event(
        self, type_: str, payload: dict[str, Any], screenshot_url: str | None = None
    ) -> None:
        self._log.add(
            "event", self._wheel.tick, self._clock.t, type=type_, payload=dict(payload)
        )

    def insert_event_now(
        self, type_: str, payload: dict[str, Any], screenshot_url: str | None = None
    ) -> int:
        self.record_event(type_, payload, screenshot_url)
        self._next_event_id += 1
        return self._next_event_id

    def insert_mission(self, row: dict[str, Any]) -> None:
        self._log.add("mission_row", self._wheel.tick, self._clock.t, row=dict(row))

    def record_decision(self, row: dict[str, Any]) -> None:
        self._log.add("decision", self._wheel.tick, self._clock.t, **row)

    # unused by the tick this file drives, kept so a caller that happens to
    # reach them (e.g. a future extension) fails loudly rather than with
    # AttributeError deep in production code.
    def upload_screenshot(self, _jpeg: bytes, _hint: str) -> None:
        return None

    def flush(self) -> None:
        return None


class _Breaks:
    """`Harness._vehicle_hold` reads `self.breaks.on_break`; humanizer breaks
    are not driven by this replayer (see the module docstring)."""

    on_break = False


# --------------------------------------------------------------------------- #
#  the driver                                                                 #
# --------------------------------------------------------------------------- #


def _settings(*, missions_enabled: bool, hourly_cap_usd: float) -> Settings:
    """A real `Settings`, none of whose credential fields are ever read by the
    tick this file drives — constructed directly rather than via
    `Settings.load()` so nothing here needs an `.env` file or an API key."""
    return Settings(
        anthropic_api_key=None,
        bridge_url="http://127.0.0.1:0",  # unused: RecordingBridge never dials out
        supabase_url=None,
        supabase_secret_key=None,
        obs_ws_host="127.0.0.1",
        obs_ws_port=0,
        obs_ws_password=None,
        overlay_host="127.0.0.1",
        overlay_port=0,
        hourly_cap_usd=hourly_cap_usd,
        state_dir=_throwaway_dir("state"),
        pricing_file=Path(__file__).resolve().parents[2] / "config" / "pricing.yaml",
        missions_enabled=missions_enabled,
    )


class ReplayHarness:
    """A `Harness`-like driver: real selection code, recording collaborators.

    Built the way `_ReflexStub`/`_LoopStub` are (class-level assignment of the
    real, unmodified `Harness` methods), extended with the brain layer
    (`_think`/`_dynamic_context`/`_apply_decision`) and the day-plan / mission-
    objective drivers, because replaying the whole tick — not just the reflex
    half those two stubs cover — is this file's whole job.
    """

    # -- the real production methods, bound verbatim -----------------------
    _reflex = Harness._reflex
    _phone_reflex = Harness._phone_reflex
    _game_owns_controls = Harness._game_owns_controls
    _game_control_reason = Harness._game_control_reason
    _reflex_act = Harness._reflex_act
    _break_the_idle = Harness._break_the_idle
    _roam_preempted = Harness._roam_preempted
    _mission_preempted = Harness._mission_preempted
    _day_plan_preempted = Harness._day_plan_preempted
    _governor_preempted = Harness._governor_preempted
    _interior_preempted = Harness._interior_preempted
    _run_interior_escape = Harness._run_interior_escape
    _release_interior_wheel = Harness._release_interior_wheel
    _vehicle_hold = Harness._vehicle_hold
    _handle_mission_events = Harness._handle_mission_events
    _write_mission_row = Harness._write_mission_row
    _record_big_event = Harness._record_big_event
    _end_activity_if_running = Harness._end_activity_if_running
    _drive_activities = Harness._drive_activities
    _judge_roam_goal = Harness._judge_roam_goal
    _advance_roam_goal = Harness._advance_roam_goal
    _restart_locked_plan = Harness._restart_locked_plan
    _goal_prompt_line = Harness._goal_prompt_line
    _free_roam_owns_movement = Harness._free_roam_owns_movement
    _wait_has_a_reason = Harness._wait_has_a_reason
    _begin_roam_goal = Harness._begin_roam_goal
    _issue_activity_step = Harness._issue_activity_step
    _drive_day_plan = Harness._drive_day_plan
    _drive_mission_objective = Harness._drive_mission_objective
    _dynamic_context = Harness._dynamic_context
    _validation_context = Harness._validation_context
    _think = Harness._think
    _apply_decision = Harness._apply_decision
    _execute_action = Harness._execute_action
    _say = Harness._say
    goal_text = Harness.goal_text  # a property; class-level assignment keeps it one

    def __init__(
        self,
        *,
        seed: int = 1,
        missions_enabled: bool = True,
        hourly_cap_usd: float = 1.50,
        brain_mode: str = "policy",
        governor_level: int = 0,
    ) -> None:
        self.clock = FakeClock()
        self.rng = random.Random(seed)
        self.settings = _settings(missions_enabled=missions_enabled, hourly_cap_usd=hourly_cap_usd)

        self.log = ReplayLog()
        self.wheel = MovementWheel(clock=self.clock)
        self.bridge = RecordingBridge(self.wheel, self.log, self.clock)
        self.primitives = RecordingPrimitives(self.wheel, self.log, self.clock, self.rng)
        self.bus = RecordingBus(self.log, self.wheel, self.clock)
        self.writer = RecordingWriter(self.log, self.wheel, self.clock)

        state_dir = _throwaway_dir("harness-state")
        self.totals = LifetimeTotals(path=state_dir / TOTALS_FILENAME)
        self.counters = self.totals.counters()
        self.memory = Memory(state_dir)
        self.commentary = Commentary(state_dir, self.rng)
        self.learned_scripts = LearnedScripts(state_dir / "learned_mission_scripts.json")

        self.mood = MoodModel(rng=self.rng)
        self.idle = IdlePicker(self.rng, clock=self.clock)
        self.breaks = _Breaks()
        self.activities = ActivityPicker(self.rng)
        self.activity_runner = ActivityRunner(self.activities, self.rng)
        self.roam = RoamEngine(self.rng, clock=self.clock, missions_enabled=missions_enabled)
        self.house_escape = HouseEscape(clock=self.clock)
        self.interior_escape = InteriorEscape(clock=self.clock)
        self.missions = MissionTracker()
        self.mission_follower = MissionFollower()
        self.planner = DayPlanner(self.rng, self.clock, missions_enabled=missions_enabled)
        self.stuck = StuckDetector(clock=self.clock)
        self.task_stall = TaskStallDetector(clock=self.clock)
        self.cleared_backoff = ClearedByGameBackoff(clock=self.clock)
        # Seeded and on the fake clock: the breaker picks a bearing at random,
        # and a replay has to be reproducible from (seed, states) alone.
        self.idle_breaker = IdleBreaker(clock=self.clock, rng=random.Random(seed))
        self.stranded = StrandedEscalator(clock=self.clock)
        self.threat_latch = ThreatLatch(clock=self.clock)
        self.damage = DamageTracker(clock=self.clock)
        self.vehicle = VehicleController(clock=self.clock)
        self.control_regained = ControlRegained(clock=self.clock)
        self.water = WaterEscalator(clock=self.clock)
        self.road_dodge = RoadDodge(clock=self.clock)
        self.jack_handoff = JackHandoffGate(clock=self.clock)

        self.wheel.on_preempt("roam", self._roam_preempted)
        self.wheel.on_preempt("mission", self._mission_preempted)
        self.wheel.on_preempt("day_plan", self._day_plan_preempted)
        self.wheel.on_preempt("governor", self._governor_preempted)
        self.wheel.on_preempt("exit_interior", self._interior_preempted)
        self._roam_token = None
        self._mission_token = None
        self._day_plan_token = None
        self._governor_token = None
        self._interior_token = None
        self._park_task_id = None
        self._park_deadline = 0.0

        self.governor = BudgetGovernor(hourly_cap_usd=hourly_cap_usd, clock=self.clock)
        self.governor._level = governor_level  # test rig: start at a given level
        self.tactical_cadence = _TacticalCadence(self.rng)
        self.director_cadence = _DirectorCadence(self.rng)
        self.api_backoff = ApiBackoff(clock=self.clock, rng=self.rng)
        self.death_recovery = DeathArrestRecovery(clock=self.clock)
        self.perceptor = Perceptor()
        self.grabber = None  # no screenshots in a replay; see the module docstring

        # -- the brain: real TacticalBrain/DirectorBrain, fake network seam --
        import anthropic

        from wasted_harness.budget import Pricing

        pricing_path = Path(__file__).resolve().parents[2] / "config" / "pricing.yaml"
        pricing = Pricing.load(pricing_path)
        client = anthropic.Anthropic(api_key="sk-fake-not-used")  # never called; see fakebrain.py
        self.tactical = TacticalBrain(client, pricing, on_cost=self.governor.record)
        self.director = DirectorBrain(client, pricing, on_cost=self.governor.record)
        self.fake_tactical, self.fake_director = install_fake_brain(
            self.tactical, self.director, mode=brain_mode, on_cost=self.governor.record
        )

        self.current_goal = INITIAL_GOAL
        self.current_mission = None
        self._mission_started_iso = None
        self._mission_tokens = 0
        self._pending_big_event: str | None = None
        self._pending_screenshot_trigger: str | None = None
        self._quiet_until = 0.0
        self._cutscene_active = False
        self._switch_in_progress = False
        self._retry_in_flight = False
        self._player_down = False
        self._mission_active = False
        self._wanted_now = 0
        self._phone_task_reissued_for = None
        self._live_last_task = None
        self._threat_has_the_wheel = False
        self._under_attack = False
        self._phone_answered_this_ring = False
        self._phone_hung_up_this_call = False
        self._phone_call_connected_at = None
        self.phone_hangup_after_s = main_mod.PHONE_HANGUP_AFTER_S
        self._screen_blocked = False
        self.clips = None
        self._tick_n = 0
        self._last_wheel_owner: str | None = None
        self._last_wheel_reason: str = ""

    # -- capture seams the real methods call into ---------------------------

    def _capture_screenshot(self, _hint: str) -> tuple[bytes | None, str | None]:
        # No dxcam in a replay (CLAUDE.md: nothing game-adjacent runs off the
        # server). Every caller already tolerates `(None, None)` honestly.
        return None, None

    def _capture_clip_async(
        self, _event_type: str, _caption: str, _event_id: int | None = None
    ) -> None:
        return None

    # -- the one tick, in production order -----------------------------------

    def feed(self, state: GameState, *, wall_ts: str | None = None) -> None:
        """One tick: `main.run()`'s body from a fresh `/state` snapshot onward.

        Advancing the clock is the CALLER's job (`replay()` below derives it
        from the recorded `ts` deltas); this method only runs the selection
        code against `state` at whatever `self.clock.t` already is.
        """
        self._tick_n += 1
        self.bridge.tick = self._tick_n
        self.log.add_state(self._tick_n, self.clock.t, wall_ts or "", state)

        self._cutscene_active = state.mission.cutscene_active
        self._switch_in_progress = state.player.switch_in_progress
        self._retry_in_flight = state.mission.retry_in_flight
        self._player_down = state.player.dead or state.player.arrested
        self._mission_active = state.mission.active
        self._wanted_now = state.player.wanted
        self._live_last_task = state.last_task

        delta: Delta = self.perceptor.observe(state, None)
        self.mood.observe("quiet")

        self._reflex(state, delta)

        now = self.clock.t
        level = self.governor.level

        if not (self._player_down or self._retry_in_flight):
            big_event, self._pending_big_event = self._pending_big_event, None
            director_trigger = self.director_cadence.should_fire(now, big_event, level)
            if director_trigger is not None:
                result = self._think("director", state, delta, director_trigger)
                self.director_cadence.fired(now)
                if result is not None:
                    self._apply_decision("director", result, state, delta, big_event)
            else:
                tactical_trigger = self.tactical_cadence.should_fire(now, delta, level)
                if (
                    tactical_trigger is not None
                    and not state.mission.cutscene_active
                    and not self._switch_in_progress
                ):
                    result = self._think("tactical", state, delta, tactical_trigger)
                    self.tactical_cadence.fired(now, level, self.mood.mood)
                    if result is not None:
                        self._apply_decision("tactical", result, state, delta, big_event)
                elif (
                    level < 3
                    and self.activity_runner.current is None
                    and not self.planner.in_mission_block
                    and now >= self._quiet_until
                    and state.last_task.status in ("idle", "done")
                ):
                    idle = self.idle.pick(self.mood.mood)
                    if idle is not None:
                        self.wheel.note("idle", idle.action["type"])
                        self._execute_action(idle.action["type"], idle.action["params"])

        self._drive_day_plan(state)
        self._drive_activities(state)
        self._drive_mission_objective(state)
        self.wheel.end_tick()

        owner, reason = self.wheel.owner, self.wheel.reason
        if owner != self._last_wheel_owner:
            self.log.add(
                "wheel", self._tick_n, self.clock.t, owner=owner, reason=reason,
                previous_owner=self._last_wheel_owner,
            )
            self._last_wheel_owner, self._last_wheel_reason = owner, reason


# --------------------------------------------------------------------------- #
#  the public entry point                                                     #
# --------------------------------------------------------------------------- #


def _epoch_seconds(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def replay(
    states: Sequence[GameState] | Iterable[GameState],
    *,
    ts: Sequence[str] | None = None,
    default_tick_s: float = 1.0 / 3.0,
    seed: int = 1,
    missions_enabled: bool = True,
    hourly_cap_usd: float = 1.50,
    brain_mode: str = "policy",
    governor_level: int = 0,
) -> ReplayLog:
    """Feed `states` through the real selection code; return the structured log.

    `ts`, when given, is a wall-clock ISO-8601 timestamp per state (the
    recorder's own envelope `ts` — see `tools/record_state.py`); consecutive
    deltas advance :class:`FakeClock` by exactly that many seconds, so a
    recorded 4-second gap is a 4-second gap in every `clock=`-driven component
    too. Without `ts` (a hand-built synthetic stream), the clock advances by
    `default_tick_s` per state — CONTRACTS' own 2-4 Hz poll band, so a
    synthetic stream still produces cadence-realistic tactical-timer firing.
    """
    states = list(states)
    if ts is not None and len(ts) != len(states):
        raise ValueError(f"ts has {len(ts)} entries but states has {len(states)}")

    harness = ReplayHarness(
        seed=seed,
        missions_enabled=missions_enabled,
        hourly_cap_usd=hourly_cap_usd,
        brain_mode=brain_mode,
        governor_level=governor_level,
    )

    with mock.patch.object(main_mod, "time", _FakeTimeModule(harness.clock)):
        prev_epoch: float | None = None
        for i, state in enumerate(states):
            if ts is not None:
                epoch = _epoch_seconds(ts[i])
                delta_s = 0.0 if prev_epoch is None else max(0.0, epoch - prev_epoch)
                prev_epoch = epoch
            else:
                delta_s = 0.0 if i == 0 else default_tick_s
            harness.clock.advance(delta_s)
            harness.feed(state, wall_ts=(ts[i] if ts is not None else ""))

    return harness.log


def replay_from_jsonl(path: Path, **kwargs: Any) -> ReplayLog:
    """`replay()` over a recorder-format `.jsonl` file (`tools/record_state.py`'s
    own output: one `{"ts": ..., "state": {...}}` object per line)."""
    states: list[GameState] = []
    stamps: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            states.append(GameState.model_validate(row["state"]))
            stamps.append(row["ts"])
    return replay(states, ts=stamps, **kwargs)


__all__ = [
    "FakeClock",
    "ReplayHarness",
    "ReplayLog",
    "make_state",
    "replay",
    "replay_from_jsonl",
]
