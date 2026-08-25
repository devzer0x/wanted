"""Harness entrypoint: supervised reflex→tactical→director loop, and --check.

``python -m wasted_harness.main --check`` probes every prerequisite for real
(bridge, API key, Supabase config, OBS, pricing file) and exits nonzero listing
what is missing. There is no demo mode: without the game bridge the loop
reports bridge-down and retries; without the API key the brain refuses to start.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .bridge_client import BridgeClient, BridgeDownError, GameState, OnlineSessionActiveError
from .brain.director import VISION_TRIGGERS, DirectorBrain, DirectorCadence
from .brain.memory import Memory
from .brain.schemas import BRIDGE_TASKS, DecisionModel
from .brain.tactical import (
    BrainUnavailableError,
    DecisionFailedError,
    DecisionResult,
    TacticalBrain,
    TacticalCadence,
    make_client,
    verify_model_ids,
    verify_prefix_cacheable,
)
from .budget import LEVEL_NOTES, BudgetGovernor, Pricing
from .behavior.activities import ActivityPicker
from .behavior.humanizer import BreakScheduler, IdlePicker, MoodModel, reaction_delay
from .behavior.missions import MissionTracker
from .behavior.recovery import (
    ApiBackoff,
    BridgeDownTracker,
    StuckDetector,
    flipped_action,
    stranded_action,
)
from .commentary import Commentary
from .events import SupabaseWriter
from .logsetup import get_logger, setup_logging
from .overlay import OverlayBus, create_app
from .perception import (
    Delta,
    Perceptor,
    ScreenshotUnavailableError,
    ScreenGrabber,
    encode_jpeg,
    objective_region_hash,
)
from .settings import ConfigError, Settings

log = get_logger("wasted.main")

THINKING_TIMESCALE = 0.15
STATS_INTERVAL_S = 5.0
FLUSH_INTERVAL_S = 2.0


# --------------------------------------------------------------------------- #
#  --check                                                                    #
# --------------------------------------------------------------------------- #


def run_check(settings: Settings) -> int:
    """Probe every prerequisite for real; print a report; exit nonzero if any missing."""
    missing: list[str] = []
    print(f"WANTED harness {__version__} — prerequisite check\n")

    # pricing file
    try:
        pricing = Pricing.load(settings.pricing_file)
        print(f"[ok]      pricing: {settings.pricing_file}")
        print(f"          tactical={pricing.tactical.id} director={pricing.director.id}")
        print(f"          source={pricing.source_url} (fetched {pricing.fetched})")
    except ConfigError as exc:
        pricing = None
        print(f"[MISSING] pricing: {exc}")
        missing.append("pricing.yaml")

    # state dir
    try:
        settings.ensure_state_dir()
        probe = settings.state_dir / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
        print(f"[ok]      state dir writable: {settings.state_dir}")
    except OSError as exc:
        print(f"[MISSING] state dir not writable: {settings.state_dir} ({exc})")
        missing.append("state dir")

    # bridge
    try:
        bridge = BridgeClient(settings.bridge_url, timeout_s=1.5, connect_retries=0)
        health = bridge.get_health()
        print(
            f"[ok]      bridge: {settings.bridge_url} "
            f"(v{health.version}, {health.edition}, {health.game_fps:.0f} fps)"
        )
        bridge.close()
    except OnlineSessionActiveError as exc:
        print(f"[MISSING] bridge: {exc}")
        missing.append("bridge (online session active)")
    except BridgeDownError as exc:
        print(f"[MISSING] bridge: {exc}")
        missing.append("bridge")

    # Claude API key + static-prefix cache check
    if not settings.anthropic_api_key:
        print("[MISSING] ANTHROPIC_API_KEY is not set — the brain cannot run.")
        missing.append("ANTHROPIC_API_KEY")
    elif pricing is not None:
        try:
            client = make_client(settings)
            counts = verify_prefix_cacheable(client, pricing)
            print(
                f"[ok]      claude api key valid; static prefixes cacheable "
                f"(tactical={counts['tactical']} tok >= 4096, "
                f"director={counts['director']} tok >= 1024)"
            )
        except BrainUnavailableError as exc:
            print(f"[MISSING] claude api: {exc}")
            missing.append("claude api (key or prefix)")

    # Supabase
    if not settings.supabase_configured:
        print(
            "[MISSING] supabase: SUPABASE_URL / SUPABASE_SECRET_KEY unset — "
            "events will queue offline at "
            f"{settings.state_dir / 'queue.jsonl'}"
        )
        missing.append("supabase config")
    else:
        try:
            from supabase import create_client

            client = create_client(settings.supabase_url, settings.supabase_secret_key)
            client.table("sessions").select("id").limit(1).execute()
            print(f"[ok]      supabase reachable: {settings.supabase_url}")
        except Exception as exc:
            print(
                f"[MISSING] supabase configured but unreachable: "
                f"{type(exc).__name__}: {str(exc)[:160]}"
            )
            missing.append("supabase (unreachable)")

    # OBS
    try:
        import obsws_python as obs

        req = obs.ReqClient(
            host=settings.obs_ws_host,
            port=settings.obs_ws_port,
            password=settings.obs_ws_password or "",
            timeout=2,
        )
        version = req.get_version()
        print(f"[ok]      obs: {version.obs_version} (ws {version.obs_web_socket_version})")
        req.disconnect()
    except Exception as exc:
        print(
            f"[MISSING] obs websocket at {settings.obs_ws_host}:{settings.obs_ws_port} "
            f"({type(exc).__name__}: {str(exc)[:120]}) — clips unavailable"
        )
        missing.append("obs")

    # screenshots
    if sys.platform != "win32":
        print(
            f"[MISSING] screenshots: platform is {sys.platform!r}; dxcam capture "
            f"requires the Windows game server ([windows] extra)"
        )
        missing.append("screenshots (not windows)")

    print()
    if missing:
        print(f"NOT READY — missing: {', '.join(missing)}")
        return 1
    print("READY — all prerequisites present.")
    return 0


# --------------------------------------------------------------------------- #
#  supervised loop                                                            #
# --------------------------------------------------------------------------- #


class Harness:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rng = random.Random()
        self.pricing = Pricing.load(settings.pricing_file)
        state_dir = settings.ensure_state_dir()

        self.session_id = str(uuid.uuid4())
        self.writer = SupabaseWriter(settings, session_id=self.session_id)
        self.memory = Memory(state_dir)
        self.commentary = Commentary(state_dir, self.rng)
        self.bus = OverlayBus()

        self.governor = BudgetGovernor(
            hourly_cap_usd=settings.hourly_cap_usd, on_change=self._on_governor_change
        )
        # The brain is mandatory: fail loudly here, never a silent degraded mode.
        self.anthropic = make_client(settings)
        verify_model_ids(self.anthropic, self.pricing)
        verify_prefix_cacheable(self.anthropic, self.pricing)
        self.tactical = TacticalBrain(self.anthropic, self.pricing)
        self.director = DirectorBrain(self.anthropic, self.pricing)
        self.tactical_cadence = TacticalCadence(self.rng)
        self.director_cadence = DirectorCadence(self.rng)

        self.bridge = BridgeClient(settings.bridge_url)
        self.perceptor = Perceptor()
        self.mood = MoodModel(rng=self.rng)
        self.idle = IdlePicker(self.rng)
        self.breaks = BreakScheduler(rng=self.rng)
        self.activities = ActivityPicker(self.rng)
        self.missions = MissionTracker()
        self.stuck = StuckDetector()
        self.bridge_down = BridgeDownTracker()
        self.api_backoff = ApiBackoff(rng=self.rng)

        try:
            self.grabber: ScreenGrabber | None = ScreenGrabber()
        except ScreenshotUnavailableError as exc:
            log.warning("screenshots disabled", extra={"kv": {"reason": str(exc)[:140]}})
            self.grabber = None
        try:
            from .primitives import Primitives

            self.primitives: Any = Primitives(self.rng)
        except Exception as exc:
            log.warning(
                "SendInput primitives unavailable on this platform",
                extra={"kv": {"reason": str(exc)[:140]}},
            )
            self.primitives = None

        self.counters = {"deaths": 0, "busted": 0, "missions_passed": 0}
        self.current_goal = "wake up, find wheels, see what the day wants"
        self._pending_big_event: str | None = None
        self._pending_screenshot_trigger: str | None = None
        self._last_stats = 0.0
        self._last_flush = 0.0
        self._started = time.monotonic()
        self._stop = threading.Event()

    # -- infrastructure --------------------------------------------------------

    def _on_governor_change(self, old: int, new: int, reason: str) -> None:
        self.writer.record_event("governor_level", {"from": old, "to": new, "reason": reason})
        self.bus.publish("governor", {"level": new, "note": LEVEL_NOTES[new]})
        self._pending_big_event = "governor_level"

    def _start_overlay(self) -> None:
        import uvicorn

        app = create_app(self.bus)
        config = uvicorn.Config(
            app,
            host=self.settings.overlay_host,
            port=self.settings.overlay_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="overlay", daemon=True)
        thread.start()
        log.info(
            "overlay serving",
            extra={"kv": {"url": f"http://{self.settings.overlay_host}:{self.settings.overlay_port}/overlay"}},
        )

    def _write_session_start(self) -> None:
        (self.settings.state_dir / "current_session.json").write_text(
            json.dumps({"session_id": self.session_id}), encoding="utf-8"
        )
        edition = "legacy"
        try:
            edition = self.bridge.get_health().edition
        except BridgeDownError:
            pass  # session starts anyway; edition confirmed when the bridge is up
        self.writer.insert_session(
            {
                "id": self.session_id,
                "game_edition": edition,
                "harness_version": __version__,
            }
        )
        self.writer.record_event(
            "session_start", {"harness_version": __version__, "game_edition": edition}
        )
        self.writer.flush()

    # -- reflex layer ----------------------------------------------------------

    def _capture_screenshot(self, name_hint: str) -> tuple[bytes | None, str | None]:
        if self.grabber is None:
            return None, None
        try:
            jpeg = encode_jpeg(self.grabber.grab())
            url = self.writer.upload_screenshot(jpeg, name_hint)
            return jpeg, url
        except ScreenshotUnavailableError as exc:
            log.warning("screenshot capture failed", extra={"kv": {"reason": str(exc)[:120]}})
            return None, None

    def _reflex(self, state: GameState, delta: Delta) -> None:
        if delta.died:
            self.counters["deaths"] += 1
            _, url = self._capture_screenshot("death")
            self.writer.record_event(
                "death",
                {
                    "cause": "?",
                    "street": state.location.street,
                    "deaths_total": self.counters["deaths"],
                },
                screenshot_url=url,
            )
            self.activities.last_death_pos = (
                state.player.pos.x,
                state.player.pos.y,
                state.player.pos.z,
            )
            line = self.commentary.death_line()
            self.bus.publish("banner", {"kind": "wasted"})
            self.bus.publish("say", {"text": line, "mood": "scared"})
            self.bus.publish("counters", dict(self.counters))
            self.mood.observe("death")
            self.memory.log_day("death", f"died on {state.location.street}: {line}")
            self._pending_big_event = "death"
            self._pending_screenshot_trigger = "death"
        if delta.busted:
            self.counters["busted"] += 1
            _, url = self._capture_screenshot("busted")
            self.writer.record_event(
                "busted",
                {
                    "wanted_at_arrest": delta.wanted_from,
                    "street": state.location.street,
                    "busted_total": self.counters["busted"],
                },
                screenshot_url=url,
            )
            line = self.commentary.busted_line()
            self.bus.publish("banner", {"kind": "busted"})
            self.bus.publish("say", {"text": line, "mood": "bored"})
            self.bus.publish("counters", dict(self.counters))
            self.mood.observe("busted")
            self.memory.log_day("busted", f"busted on {state.location.street}: {line}")
            self._pending_big_event = "busted"
            self._pending_screenshot_trigger = "busted"
        if delta.wanted_changed:
            url = None
            if delta.wanted_to >= 3:
                _, url = self._capture_screenshot("wanted")
                self.mood.observe("wanted_high")
                self._pending_screenshot_trigger = "wanted_change"
            elif delta.wanted_to == 0:
                self.mood.observe("wanted_clear")
            self.writer.record_event(
                "wanted_change",
                {"from": delta.wanted_from, "to": delta.wanted_to},
                screenshot_url=url,
            )

        for ev in self.missions.feed(
            state.mission.active,
            state.mission.cutscene_active,
            state.mission.random_event_active,
            delta,
        ):
            self.writer.record_event(ev.type, ev.payload)
            self._pending_big_event = ev.type

        # physical recovery
        stuck_action = self.stuck.check(state)
        if stuck_action == "reverse_out" and self.primitives is not None:
            self.primitives.execute("reverse_out", {"ms": 1400})
        elif stuck_action == "unstick":
            moved = self.stuck.try_unstick(self.bridge)
            if moved is not None:
                self.writer.record_event(
                    "unstick",
                    {"distance_m": moved, "stuck_for_s": state.vehicle.stopped_for_s if state.vehicle else 0.0},
                )
                self.bus.publish(
                    "say",
                    {"text": "That was a legal nudge. Three meters. Judges allow it.", "mood": self.mood.mood},
                )
        flip = flipped_action(state)
        if flip is not None:
            self._execute_action(flip["type"], flip["params"])
        # Governor L2: reflex drives — keep a wander task alive with mood style.
        if self.governor.level >= 2 and state.last_task.status in ("idle", "done", "failed"):
            if state.player.in_vehicle:
                self.bridge.post_task("wander_drive", {"style": self.mood.driving_style()})
            else:
                strand = stranded_action(state)
                if strand is not None:
                    self._execute_action(strand["type"], strand["params"])

    # -- decisions -------------------------------------------------------------

    def _dynamic_context(self, state: GameState, delta: Delta, trigger: str, layer: str) -> str:
        parts = [
            f"TRIGGER: {trigger}",
            f"GOAL: {self.current_goal}",
            f"MOOD (tracker): {self.mood.mood}",
            f"GOVERNOR: L{self.governor.level} ({LEVEL_NOTES[self.governor.level]})",
            self.missions.brain_note(),
            "STATE: " + state.model_dump_json(by_alias=True),
            "CHANGES: "
            + (
                ", ".join(k for k, v in vars(delta).items() if isinstance(v, bool) and v)
                or "none"
            ),
            self.memory.context_block(),
        ]
        if layer == "director":
            counters = ", ".join(f"{k}={v}" for k, v in self.counters.items())
            parts.append(f"RECORD: {counters}")
            parts.append(
                f"COST: ${self.governor.hourly_spend_usd():.2f} this hour of "
                f"${self.settings.hourly_cap_usd:.2f} cap"
            )
        return "\n\n".join(parts)

    def _think(self, layer: str, state: GameState, delta: Delta, trigger: str) -> DecisionResult | None:
        """One decision with the timescale dip; None when the call failed."""
        if not self.api_backoff.ready():
            return None
        dipped = False
        try:
            try:
                self.bridge.set_timescale(THINKING_TIMESCALE)
                dipped = True
            except BridgeDownError:
                pass  # thinking still allowed; the world just won't slow down
            context = self._dynamic_context(state, delta, trigger, layer)
            if layer == "tactical":
                result = self.tactical.decide(context)
            else:
                shot, shot_trigger = None, None
                if (
                    self._pending_screenshot_trigger in VISION_TRIGGERS
                    and self.grabber is not None
                ):
                    shot, _ = self._capture_screenshot(f"director-{self._pending_screenshot_trigger}")
                    shot_trigger = self._pending_screenshot_trigger
                self._pending_screenshot_trigger = None
                result = self.director.decide(context, shot, shot_trigger)
            self.api_backoff.record_success()
            return result
        except DecisionFailedError as exc:
            log.error("decision failed; reflex keeps control", extra={"kv": {"layer": layer, "error": str(exc)[:160]}})
            self.api_backoff.record_failure()
            return None
        finally:
            if dipped:
                try:
                    self.bridge.set_timescale(1.0)
                except BridgeDownError:
                    log.warning("could not restore timescale (bridge down); watchdog will catch it")

    def _apply_decision(self, layer: str, result: DecisionResult) -> None:
        d: DecisionModel = result.decision
        if layer == "director":
            self.current_goal = d.goal
        self.governor.record(result.cost_usd)
        self.writer.record_decision(
            {
                "layer": layer,
                "thought": d.thought,
                "say": d.say,
                "mood": d.mood,
                "goal": d.goal,
                "action": d.action.model_dump(),
                "confidence": d.confidence,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cached_tokens": result.cached_tokens,
                "cost_usd": round(result.cost_usd, 6),
            }
        )
        self.bus.publish("say", {"text": d.say, "mood": d.mood})
        self.memory.log_day("decision", f"[{layer}] {d.say} -> {d.action.type}")
        time.sleep(reaction_delay(self.rng))  # humanizer: 300-900 ms reaction
        self._execute_action(d.action.type, d.action.params)

    def _execute_action(self, action_type: str, params: dict[str, Any]) -> None:
        try:
            if action_type in BRIDGE_TASKS:
                self.bridge.post_task(action_type, params)
            elif action_type == "radio":
                self.bridge.set_radio(str(params.get("station", "off")))
            elif action_type == "horn":
                self.bridge.horn(int(params.get("ms", 200)))
            elif self.primitives is not None:
                self.primitives.execute(action_type, params)
            else:
                log.error(
                    "cannot execute primitive on this platform (SendInput unavailable)",
                    extra={"kv": {"type": action_type}},
                )
        except BridgeDownError as exc:
            log.warning("action lost: bridge down", extra={"kv": {"type": action_type, "error": str(exc)[:100]}})
        except Exception as exc:
            log.error(
                "action execution failed",
                extra={"kv": {"type": action_type, "error": f"{type(exc).__name__}: {exc}"[:160]}},
            )

    # -- housekeeping ----------------------------------------------------------

    def _heartbeat(self, state: GameState | None) -> None:
        now = time.monotonic()
        if now - self._last_stats < STATS_INTERVAL_S:
            return
        self._last_stats = now
        hud: dict[str, Any] = {}
        if state is not None:
            hud = {
                "health": state.player.health,
                "armor": state.player.armor,
                "wanted": state.player.wanted,
                "cash": state.player.cash,
                "vehicle": state.vehicle.display_name if state.vehicle else None,
                "street": state.location.street,
                "zone": state.location.zone,
                "clock": state.world.clock,
                "weather": state.world.weather,
            }
        self.writer.upsert_stats(
            {
                "deaths": self.counters["deaths"],
                "busted": self.counters["busted"],
                "missions_passed": self.counters["missions_passed"],
                "hours_alive": round((now - self._started) / 3600.0, 3),
                "cost_today_usd": round(self.governor.total_usd(), 4),
                "cost_per_hour_usd": round(self.governor.cost_per_hour_usd(), 4),
                "governor_level": self.governor.level,
                # drives the site's offline banner: stale > 60 s => offline (§5)
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "current_goal": self.current_goal,
                "hud": hud,
            }
        )

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_flush >= FLUSH_INTERVAL_S:
            self._last_flush = now
            self.writer.flush()

    def _handle_breaks(self, state: GameState) -> bool:
        """Returns True while on a break (loop should idle)."""
        planned = self.breaks.finish_if_over()
        if planned is not None:
            self.writer.record_event("break", {"phase": "end", "planned_s": planned})
            self.bus.publish("say", {"text": "Back. Did anyone move my car.", "mood": "chill"})
            return False
        if self.breaks.on_break:
            return True
        if self.breaks.due() and not self.missions.in_mission and state.player.wanted == 0:
            planned = self.breaks.start()
            try:
                self.bridge.post_task("stop", {})
            except BridgeDownError:
                pass
            self.writer.record_event("break", {"phase": "start", "planned_s": planned})
            self.bus.publish("say", {"text": "Even I stop for gas. Back in a few.", "mood": "chill"})
            return True
        return False

    # -- main loop -------------------------------------------------------------

    def run(self) -> int:
        setup_logging()
        log.info(
            "harness starting",
            extra={"kv": {"version": __version__, "session": self.session_id}},
        )
        self._start_overlay()
        self._write_session_start()
        self.bus.publish("counters", dict(self.counters))

        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())

        poll_interval = 1.0 / self.settings.poll_hz
        try:
            while not self._stop.is_set():
                loop_started = time.monotonic()
                try:
                    state = self.bridge.get_state()
                except BridgeDownError as exc:
                    payload = self.bridge_down.record_failure(exc)
                    if payload is not None:
                        self.writer.record_event("bridge_down", payload)
                    self._maybe_flush()
                    self._stop.wait(self.bridge_down.backoff_s())
                    continue
                except OnlineSessionActiveError as exc:
                    log.critical(str(exc))
                    self._stop.wait(30.0)
                    continue

                up_payload = self.bridge_down.record_success()
                if up_payload is not None:
                    self.writer.record_event("bridge_up", up_payload)

                objective_hash = None
                if self.grabber is not None:
                    try:
                        objective_hash = objective_region_hash(self.grabber.grab())
                    except ScreenshotUnavailableError:
                        pass
                delta = self.perceptor.observe(state, objective_hash)
                self.mood.observe("quiet")

                if self._handle_breaks(state):
                    self._heartbeat(state)
                    self._maybe_flush()
                    self._stop.wait(5.0)
                    continue

                self._reflex(state, delta)

                now = time.monotonic()
                level = self.governor.level
                big_event, self._pending_big_event = self._pending_big_event, None

                director_trigger = self.director_cadence.should_fire(now, big_event, level)
                if director_trigger is not None:
                    result = self._think("director", state, delta, director_trigger)
                    self.director_cadence.fired(now)
                    if result is not None:
                        self._apply_decision("director", result)
                else:
                    tactical_trigger = self.tactical_cadence.should_fire(now, delta, level)
                    if tactical_trigger is not None and not state.mission.cutscene_active:
                        result = self._think("tactical", state, delta, tactical_trigger)
                        self.tactical_cadence.fired(now, level)
                        if result is not None:
                            self._apply_decision("tactical", result)
                    elif level < 3 and state.last_task.status in ("idle", "done"):
                        idle = self.idle.pick(self.mood.mood)
                        if idle is not None:
                            self._execute_action(idle.action["type"], idle.action["params"])

                self._heartbeat(state)
                self._maybe_flush()
                elapsed = time.monotonic() - loop_started
                self._stop.wait(max(0.05, poll_interval - elapsed))
        finally:
            log.info("harness stopping")
            self.writer.record_event("session_end", {"reason": "shutdown"})
            self.writer.flush()
            self.bridge.close()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wasted_harness.main")
    parser.add_argument(
        "--check",
        action="store_true",
        help="probe prerequisites (bridge, key, supabase, obs) and exit nonzero if missing",
    )
    args = parser.parse_args(argv)
    setup_logging()
    settings = Settings.load()
    if args.check:
        return run_check(settings)
    try:
        return Harness(settings).run()
    except ConfigError as exc:
        log.critical(str(exc))
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
