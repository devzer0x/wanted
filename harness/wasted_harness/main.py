"""Harness entrypoint: supervised reflex→tactical→director loop, --check, --prompt-audit.

``python -m wasted_harness.main --check`` is the single source of truth for
readiness on the game server. It probes every prerequisite for real — pricing
file, state directory, bridge, Claude key + static-prefix cache minimums,
Supabase, OBS websocket, screenshot capture, SendInput/session, overlay port —
prints a report, and exits nonzero listing what is missing. There is no demo
mode: without the game bridge the loop reports bridge-down and retries; without
the API key the brain refuses to start.

``--prompt-audit`` validates the brain's static half (prompt files, prefix token
counts against the real API, decision schema, catalog/schema agreement, cost
projection) without a game. It deliberately does NOT generate a decision: a
decision needs a game-state snapshot, and inventing one to feed the model would
be fabricating game data, which this project does not do.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .behavior.activities import (
    L3_PARK_TIMEOUT_S,
    ActivityPicker,
    ActivityRunner,
    scenic_park_plan,
)
from .behavior.humanizer import BreakScheduler, IdlePicker, MoodModel, reaction_delay
from .behavior.missions import MissionTracker
from .behavior.recovery import (
    ApiBackoff,
    BridgeDownTracker,
    BridgeStallTracker,
    GameRestartDetector,
    StrandedEscalator,
    StuckDetector,
    classify_api_failure,
    flipped_action,
)
from .brain.director import VISION_TRIGGERS, DirectorBrain, DirectorCadence
from .brain.memory import Memory
from .brain.prompts import director_static_prefix, tactical_static_prefix
from .brain.schemas import ACTION_TYPES, BRIDGE_TASKS, DecisionModel
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
from .bridge_client import (
    UNKNOWN_EDITION,
    BridgeApiError,
    BridgeClient,
    BridgeDownError,
    BridgeError,
    BridgeTransientError,
    GameState,
    OnlineSessionActiveError,
)
from .budget import LEVEL_NOTES, BudgetGovernor, Pricing, cost_usd
from .commentary import Commentary
from .events import SupabaseWriter
from .logsetup import force_utf8_console, get_logger, setup_logging
from .overlay import OverlayBus, create_app
from .perception import (
    Delta,
    Perceptor,
    ScreenGrabber,
    ScreenshotUnavailableError,
    encode_jpeg,
    objective_region_hash,
)
from .primitives import gamepad_status, session_diagnostics
from .settings import ConfigError, Settings

log = get_logger("wasted.main")

THINKING_TIMESCALE = 0.15
STATS_INTERVAL_S = 5.0
FLUSH_INTERVAL_S = 2.0
#: Events worth a replay-buffer clip. Kept to the banner moments so a clip is
#: always something a viewer would actually want to see again.
CLIP_EVENTS: frozenset[str] = frozenset({"death", "busted"})


# --------------------------------------------------------------------------- #
#  --check                                                                    #
# --------------------------------------------------------------------------- #


class _Report:
    """Collects check results so the report is one shape, not eight print styles."""

    def __init__(self) -> None:
        self.missing: list[str] = []
        self.warnings: list[str] = []

    def ok(self, label: str, detail: str = "") -> None:
        print(f"[ok]      {label}{': ' + detail if detail else ''}")

    def note(self, detail: str) -> None:
        print(f"          {detail}")

    def warn(self, label: str, detail: str) -> None:
        print(f"[warn]    {label}: {detail}")
        self.warnings.append(label)

    def fail(self, key: str, label: str, detail: str) -> None:
        print(f"[MISSING] {label}: {detail}")
        self.missing.append(key)


def _check_pricing(settings: Settings, rep: _Report) -> Pricing | None:
    try:
        pricing = Pricing.load(settings.pricing_file)
    except ConfigError as exc:
        rep.fail("pricing.yaml", "pricing", str(exc))
        return None
    rep.ok("pricing", str(settings.pricing_file))
    rep.note(f"tactical={pricing.tactical.id} director={pricing.director.id}")
    rep.note(f"source={pricing.source_url} (fetched {pricing.fetched})")
    return pricing


def _check_state_dir(settings: Settings, rep: _Report) -> None:
    try:
        settings.ensure_state_dir()
        probe = settings.state_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        rep.fail(
            "state dir",
            "state dir not writable",
            f"{settings.state_dir} ({type(exc).__name__}: {exc})",
        )
        return
    rep.ok("state dir writable", str(settings.state_dir))
    queue = settings.state_dir / "queue.jsonl"
    if queue.exists():
        writer = SupabaseWriter(settings)
        depth = writer.queue_depth()
        if depth > 0:
            rep.warn(
                "offline queue backlog",
                f"{depth} rows waiting in {queue} (flushed automatically once "
                f"Supabase is reachable)",
            )


def _check_bridge(settings: Settings, rep: _Report) -> None:
    bridge = BridgeClient(settings.bridge_url, timeout_s=1.5, connect_retries=0)
    try:
        health = bridge.get_health()
        rep.ok(
            "bridge",
            f"{settings.bridge_url} (v{health.version}, {health.edition}, "
            f"{health.game_fps:.0f} fps, queue {health.queue_depth})",
        )
        if health.online_blocked:
            rep.fail(
                "bridge (online session active)",
                "bridge",
                "reports online_blocked — the safety latch is engaged",
            )
        if health.edition == UNKNOWN_EDITION:
            rep.warn(
                "bridge edition",
                "still 'unknown' — edition detection has not completed yet "
                "(CONTRACTS v1.2 (b)); it retries, and the session row records "
                "whatever is true at start",
            )
    except OnlineSessionActiveError as exc:
        rep.fail("bridge (online session active)", "bridge", str(exc))
    except BridgeDownError as exc:
        rep.fail("bridge", "bridge", str(exc))
    except BridgeApiError as exc:
        # Includes the v1.2 transient 503s: the bridge is up but has nothing to
        # report yet. Not ready is not the same as not there — say which.
        rep.fail("bridge (not ready)", "bridge", str(exc))
    finally:
        bridge.close()


def _check_brain(settings: Settings, pricing: Pricing | None, rep: _Report) -> None:
    if not settings.anthropic_api_key:
        rep.fail(
            "ANTHROPIC_API_KEY",
            "claude api",
            "ANTHROPIC_API_KEY is not set — the brain cannot run",
        )
        return
    if pricing is None:
        rep.fail(
            "claude api (no pricing)",
            "claude api",
            "cannot verify prefixes without a readable pricing.yaml",
        )
        return
    try:
        client = make_client(settings)
        counts = verify_prefix_cacheable(client, pricing)
    except BrainUnavailableError as exc:
        rep.fail("claude api (key or prefix)", "claude api", str(exc))
        return
    rep.ok("claude api key valid; static prefixes cacheable")
    rep.note(
        f"tactical prefix {counts['tactical']} tok "
        f">= {pricing.tactical.min_cacheable_prefix_tokens} minimum"
    )
    rep.note(
        f"director prefix {counts['director']} tok "
        f">= {pricing.director.min_cacheable_prefix_tokens} minimum"
    )
    warm = cost_usd(pricing.tactical, 400, 150, cache_read_tokens=counts["tactical"])
    rep.note(f"projected warm tactical call: ${warm:.6f} (prefix served from cache)")


def _check_supabase(settings: Settings, rep: _Report) -> None:
    if not settings.supabase_configured:
        rep.fail(
            "supabase config",
            "supabase",
            f"SUPABASE_URL / SUPABASE_SECRET_KEY unset — events would queue "
            f"offline at {settings.state_dir / 'queue.jsonl'}",
        )
        return
    try:
        from supabase import create_client

        client = create_client(settings.supabase_url, settings.supabase_secret_key)
        client.table("sessions").select("id").limit(1).execute()
    except Exception as exc:  # any failure here is "not reachable", loudly
        rep.fail(
            "supabase (unreachable)",
            "supabase configured but unreachable",
            f"{type(exc).__name__}: {str(exc)[:160]}",
        )
        return
    rep.ok("supabase reachable", str(settings.supabase_url))


def _check_obs(settings: Settings, rep: _Report) -> None:
    try:
        import obsws_python as obs

        req = obs.ReqClient(
            host=settings.obs_ws_host,
            port=settings.obs_ws_port,
            password=settings.obs_ws_password or "",
            timeout=2,
        )
    except Exception as exc:  # obsws raises several unrelated types
        rep.fail(
            "obs",
            f"obs websocket at {settings.obs_ws_host}:{settings.obs_ws_port}",
            f"{type(exc).__name__}: {str(exc)[:140]} — stream overlay and clips "
            f"unavailable",
        )
        return
    try:
        version = req.get_version()
        rep.ok("obs", f"{version.obs_version} (ws {version.obs_web_socket_version})")
        try:
            status = req.get_replay_buffer_status()
            if status.output_active:
                rep.ok("obs replay buffer", "active — clips available")
            else:
                rep.warn(
                    "obs replay buffer",
                    "inactive; the harness starts it on the first clip. It is "
                    "unavailable entirely with the Custom Output (FFmpeg) "
                    "recording type",
                )
        except Exception as exc:
            rep.warn("obs replay buffer", f"status unreadable ({type(exc).__name__}: {exc})")
    finally:
        with_disconnect = getattr(req, "disconnect", None)
        if with_disconnect is not None:
            # Teardown failure has nothing useful to add to the result above.
            with contextlib.suppress(Exception):
                with_disconnect()


def _check_screenshots(rep: _Report) -> None:
    try:
        grabber = ScreenGrabber()
    except ScreenshotUnavailableError as exc:
        rep.fail("screenshots", "screenshots", str(exc))
        return
    try:
        frame = grabber.grab()
        rep.ok("screenshots", f"dxcam captured {frame.width}x{frame.height}")
        jpeg = encode_jpeg(frame)
        rep.note(f"768px JPEG encode ok ({len(jpeg)} bytes)")
    except ScreenshotUnavailableError as exc:
        rep.fail("screenshots (no frame)", "screenshots", str(exc))
    except Exception as exc:  # PIL/dxcam surprises are still "no screenshots"
        rep.fail(
            "screenshots (error)", "screenshots", f"{type(exc).__name__}: {exc}"
        )


def _check_input(rep: _Report) -> None:
    diag = session_diagnostics()
    rep.note(f"gamepad: {gamepad_status()}")
    if not diag["supported"]:
        rep.fail(
            "sendinput (not windows)",
            "manual-control input",
            f"SendInput needs Windows; platform is {diag['platform']!r}",
        )
        return
    try:
        from .primitives import Primitives

        Primitives()
    except Exception as exc:
        rep.fail(
            "sendinput", "manual-control input", f"{type(exc).__name__}: {exc}"
        )
        return
    rep.ok("manual-control input", "SendInput available (keyboard/mouse)")
    rep.note(
        f"session={diag['session_id']} console={diag['console_session_id']} "
        f"foreground={diag['foreground_window']!r}"
    )
    if diag["in_console_session"] is False:
        rep.warn(
            "input session",
            "this process is NOT in the console session — SendInput will not "
            "reach the game and Desktop Duplication will not see it. Reconnect "
            "the console with scripts/detach-rdp.ps1 (tscon /dest:console)",
        )


def _check_overlay_port(settings: Settings, rep: _Report) -> None:
    sock = socket.socket()
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((settings.overlay_host, settings.overlay_port))
    except OSError as exc:
        rep.fail(
            "overlay port",
            "overlay port",
            f"cannot bind {settings.overlay_host}:{settings.overlay_port} "
            f"({exc}) — another harness is probably already running",
        )
        return
    finally:
        sock.close()
    rep.ok(
        "overlay port free",
        f"http://{settings.overlay_host}:{settings.overlay_port}/overlay",
    )


def run_check(settings: Settings) -> int:
    """Probe every prerequisite for real; print a report; exit nonzero if any missing."""
    force_utf8_console()
    rep = _Report()
    print(f"WANTED harness {__version__} — prerequisite check")
    print(f"python {sys.version.split()[0]} on {sys.platform}\n")

    pricing = _check_pricing(settings, rep)
    _check_state_dir(settings, rep)
    _check_bridge(settings, rep)
    _check_brain(settings, pricing, rep)
    _check_supabase(settings, rep)
    _check_obs(settings, rep)
    _check_screenshots(rep)
    _check_input(rep)
    _check_overlay_port(settings, rep)

    print()
    if rep.warnings:
        print(f"warnings: {', '.join(rep.warnings)}")
    if rep.missing:
        print(f"NOT READY — missing: {', '.join(rep.missing)}")
        return 1
    print("READY — all prerequisites present.")
    return 0


# --------------------------------------------------------------------------- #
#  --prompt-audit                                                             #
# --------------------------------------------------------------------------- #


# Chars-per-token ratios calibrated against the real count_tokens measurement
# recorded in docs/STATUS.md on 2026-08-25 (tactical 7617 tok / 27168 chars on
# Haiku 4.5; director 8532 tok / 22862 chars on Sonnet 5). Used ONLY to print an
# estimate when the live endpoint is unavailable — never as a verification.
_HAIKU_CHARS_PER_TOKEN = 3.567
_SONNET_CHARS_PER_TOKEN = 2.680


def _print_offline_token_estimate(tactical: str, director: str) -> None:
    t_est = round(len(tactical) / _HAIKU_CHARS_PER_TOKEN)
    d_est = round(len(director) / _SONNET_CHARS_PER_TOKEN)
    print(
        f"[estimate] tactical ~{t_est} tok (min 4096), director ~{d_est} tok "
        f"(min 1024) — extrapolated from the 2026-08-25 measurement, NOT verified"
    )


def run_prompt_audit(settings: Settings) -> int:
    """Validate the brain's static half without a game.

    What this checks, all for real: the prompt files load and assemble to a
    byte-stable prefix; the action catalog in the prompt names exactly the
    action types the schema accepts; the decision schema is well-formed; and —
    when an API key is present — the true token count of each prefix from the
    live count_tokens endpoint, plus the resulting per-call cost from
    pricing.yaml.

    What this deliberately does NOT do: run a decision. A decision needs a game
    state snapshot; there is no game here, and inventing one would be
    fabricating game data. The live decision path is exercised on the server
    with the game running (PLAN.md Phase 2), not here.
    """
    force_utf8_console()
    print(f"WANTED harness {__version__} — prompt audit (no game, no fabricated state)\n")
    failures: list[str] = []

    tactical = tactical_static_prefix()
    director = director_static_prefix()
    if tactical_static_prefix() is not tactical:
        failures.append("tactical prefix is not byte-stable across calls (cache would miss)")
    print(f"[ok]      tactical prefix assembled: {len(tactical)} chars")
    print(f"[ok]      director prefix assembled: {len(director)} chars")

    # Catalog / schema agreement — a drift here means the model is told about
    # actions the harness will reject, or never told about ones it accepts.
    import re
    from importlib import resources

    catalog_md = (
        resources.files("wasted_harness.brain.prompts") / "action_catalog.md"
    ).read_text(encoding="utf-8")
    documented = re.findall(r"^### (\w+)$", catalog_md, re.MULTILINE)
    undocumented = [t for t in ACTION_TYPES if t not in documented]
    invented = [t for t in documented if t not in ACTION_TYPES]
    if undocumented or invented:
        failures.append(
            f"action catalog drift: undocumented={undocumented} invented={invented}"
        )
        print(f"[FAIL]    action catalog drift: undocumented={undocumented} invented={invented}")
    else:
        print(f"[ok]      action catalog matches schema exactly ({len(documented)} actions)")

    schema = DecisionModel.model_json_schema()
    required = set(schema.get("required", []))
    expected = {"thought", "say", "mood", "action", "goal", "confidence"}
    if required != expected:
        failures.append(f"decision schema required fields {sorted(required)} != {sorted(expected)}")
        print(f"[FAIL]    decision schema required fields: {sorted(required)}")
    else:
        print(f"[ok]      decision schema fields: {sorted(required)}")

    try:
        pricing = Pricing.load(settings.pricing_file)
    except ConfigError as exc:
        print(f"[FAIL]    pricing: {exc}")
        return 1

    if not settings.anthropic_api_key:
        print(
            "[skip]    token counts: ANTHROPIC_API_KEY not set. Real counts come "
            "from the live count_tokens endpoint; there is no offline tokenizer "
            "for these models, so what follows is an estimate, not a check."
        )
        _print_offline_token_estimate(tactical, director)
    else:
        try:
            client = make_client(settings)
            counts = verify_prefix_cacheable(client, pricing)
        except BrainUnavailableError as exc:
            failures.append(f"prefix token check failed: {exc}")
            print(f"[FAIL]    token counts: {exc}")
            _print_offline_token_estimate(tactical, director)
        else:
            for tier, mp in (("tactical", pricing.tactical), ("director", pricing.director)):
                n = counts[tier]
                warm = cost_usd(mp, 400, 150, cache_read_tokens=n)
                cold = cost_usd(mp, 400, 150, cache_creation_tokens=n)
                print(
                    f"[ok]      {tier}: {n} tok "
                    f"(min {mp.min_cacheable_prefix_tokens}) — "
                    f"cold ${cold:.6f} / warm ${warm:.6f} per call"
                )

    print()
    if failures:
        for f in failures:
            print(f"FAILED: {f}")
        return 1
    print("PROMPT AUDIT PASSED.")
    print(
        "Not covered here (needs the game server): live decisions, cache-hit "
        "telemetry, and measured $/hour — PLAN.md Phase 2."
    )
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
        self.activity_runner = ActivityRunner(self.activities, self.rng)
        self.missions = MissionTracker()
        self.stuck = StuckDetector()
        self.stranded = StrandedEscalator()
        self.bridge_down = BridgeDownTracker()
        self.bridge_stall = BridgeStallTracker()
        self.restart = GameRestartDetector()
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

        # Clips are best-effort: OBS may not be up yet when the harness starts
        # (the startup chain launches them in parallel). A missing OBS costs
        # clips, never the show.
        self.clips: Any = None
        self._clip_thread: threading.Thread | None = None

        self.counters = {"deaths": 0, "busted": 0, "missions_passed": 0}
        self.current_goal = "wake up, find wheels, see what the day wants"
        self._pending_big_event: str | None = None
        self._pending_screenshot_trigger: str | None = None
        self._pending_park = False
        #: Governor L3 park-somewhere-scenic state: the task id of the drive to
        #: the scenic spot, and its deadline. None when not parking.
        self._park_task_id: str | None = None
        self._park_deadline = 0.0
        #: Deliberate stillness (the `wait` action) as a deadline rather than a
        #: blocking sleep, so perception keeps running through it.
        self._quiet_until = 0.0
        self._last_stats = 0.0
        self._last_flush = 0.0
        self._started = time.monotonic()
        self._stop = threading.Event()

    # -- infrastructure --------------------------------------------------------

    def _on_governor_change(self, old: int, new: int, reason: str) -> None:
        self.writer.record_event("governor_level", {"from": old, "to": new, "reason": reason})
        self.bus.publish("governor", {"level": new, "note": LEVEL_NOTES[new]})
        self._pending_big_event = "governor_level"
        if new >= 3:
            # L3 is "asleep in the car" (CONTRACTS §7): park, stop thinking, and
            # say so honestly on screen. The bridge call happens on the loop
            # thread, not here — this callback can fire from a property read.
            self._pending_park = True

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

    def _say(self, text: str, mood: str | None = None) -> None:
        """A harness-authored line: to the overlay AND into the no-repeat memory.

        Everything the agent is heard saying goes through here or through a
        decision, so the brain is never shown a clean slate it does not have.
        """
        self.bus.publish("say", {"text": text, "mood": mood or self.mood.mood})
        self.commentary.record_say(text)

    def _start_clips(self) -> None:
        """Connect to OBS for replay clips. Never fatal — clips are a bonus."""
        from .obs import ObsClips, ObsError

        clips = ObsClips(self.settings, self.writer)
        try:
            clips.connect()
        except ObsError as exc:
            log.warning(
                "clips disabled: obs unreachable",
                extra={"kv": {"reason": str(exc)[:200]}},
            )
            return
        except Exception as exc:  # obsws raises assorted unrelated types
            log.warning(
                "clips disabled: obs connect raised",
                extra={"kv": {"error": f"{type(exc).__name__}: {exc}"[:200]}},
            )
            return
        self.clips = clips

    def _capture_clip_async(self, event_type: str, caption: str) -> None:
        """Save + upload a replay off the loop thread. One clip at a time."""
        if self.clips is None or event_type not in CLIP_EVENTS:
            return
        if self._clip_thread is not None and self._clip_thread.is_alive():
            log.info("clip skipped: previous clip still uploading")
            return

        def worker() -> None:
            try:
                self.clips.capture_clip(
                    event_id=None, event_type=event_type, caption=caption
                )
            except Exception as exc:  # the clip pipeline never kills the show
                log.warning(
                    "clip capture failed",
                    extra={
                        "kv": {
                            "event_type": event_type,
                            "error": f"{type(exc).__name__}: {exc}"[:200],
                        }
                    },
                )

        self._clip_thread = threading.Thread(target=worker, name="clip", daemon=True)
        self._clip_thread.start()

    def _write_session_start(self) -> None:
        (self.settings.state_dir / "current_session.json").write_text(
            json.dumps({"session_id": self.session_id}), encoding="utf-8"
        )
        # v1.2 (b): `unknown` is a legal edition until the bridge finishes
        # detecting, and it is the only honest default — writing "legacy" for a
        # bridge that has not said so yet puts a guess in the sessions table.
        edition = UNKNOWN_EDITION
        # The session starts anyway; the edition is confirmed once the bridge is up.
        with contextlib.suppress(BridgeError):
            edition = self.bridge.get_health().edition
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
            self._say(line, "scared")
            self.bus.publish("counters", dict(self.counters))
            self.mood.observe("death")
            self.memory.log_day("death", f"died on {state.location.street}: {line}")
            self._pending_big_event = "death"
            self._pending_screenshot_trigger = "death"
            self._capture_clip_async("death", line)
            self._end_activity_if_running("interrupted_by_death")
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
            self._say(line, "bored")
            self.bus.publish("counters", dict(self.counters))
            self.mood.observe("busted")
            self.memory.log_day("busted", f"busted on {state.location.street}: {line}")
            self._pending_big_event = "busted"
            self._pending_screenshot_trigger = "busted"
            self._capture_clip_async("busted", line)
            self._end_activity_if_running("interrupted_by_arrest")
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
                self._say("That was a legal nudge. Three meters. Judges allow it.")
        flip = flipped_action(state)
        if flip is not None:
            self._execute_action(flip["type"], flip["params"])
        # Stranded on foot with no task: widen the vehicle search, then give up
        # and let the brain be creative about it.
        strand = self.stranded.check(state)
        if strand is not None:
            self._execute_action(strand["type"], strand["params"])
        # Governor L2: reflex drives — keep a wander task alive with mood style.
        # The activity runner is also reflex-layer behaviour and outranks this;
        # posting a wander on top of a running activity step would preempt it.
        # L2 ONLY: at L3 he is asleep in the car (§7), and a wander posted here
        # would drive straight out of the scenic parking spot L3 just took him
        # to — which is how L3 ended up meaning nothing at all.
        if (
            2 <= self.governor.level < 3
            and self.activity_runner.current is None
            and state.player.in_vehicle
            and state.last_task.status in ("idle", "done", "failed")
        ):
            self._execute_action("wander_drive", {"style": self.mood.driving_style()})

    # -- decisions -------------------------------------------------------------

    def _dynamic_context(self, state: GameState, delta: Delta, trigger: str, layer: str) -> str:
        parts = [
            f"TRIGGER: {trigger}",
            f"GOAL: {self.current_goal}",
            f"MOOD (tracker): {self.mood.mood}, held {self.mood.held_for_s():.0f}s",
            f"GOVERNOR: L{self.governor.level} ({LEVEL_NOTES[self.governor.level]})",
            self.missions.brain_note(),
            self.activity_runner.note(),
            "STATE: " + state.model_dump_json(by_alias=True),
            "CHANGES: "
            + (
                ", ".join(k for k, v in vars(delta).items() if isinstance(v, bool) and v)
                or "none"
            ),
            self.memory.context_block(),
        ]
        recent = self.commentary.recent.context_block()
        if recent:
            parts.append(recent)
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
            except BridgeError:
                # Down, or up and not ready (v1.2 503): thinking is still
                # allowed, the world just won't slow down for it.
                pass
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
            cause = classify_api_failure(exc.__cause__ or exc)
            log.error(
                "decision failed; reflex keeps control",
                extra={"kv": {"layer": layer, "cause": cause, "error": str(exc)[:160]}},
            )
            self.api_backoff.record_failure(cause, exc.__cause__ or exc)
            return None
        finally:
            if dipped:
                try:
                    self.bridge.set_timescale(1.0)
                except BridgeError as exc:
                    # Leaving the world at 0.15x would be a visibly broken show,
                    # so this is loud — but it must not raise out of `finally`
                    # and take the loop with it.
                    log.warning(
                        "could not restore timescale; watchdog will catch it",
                        extra={"kv": {"error": str(exc)[:160]}},
                    )

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
        # Feeds the no-repeat list the prompt promises him back into the prompt.
        self.commentary.record_say(d.say)
        self.memory.log_day("decision", f"[{layer}] {d.say} -> {d.action.type}")
        # The brain outranks the activity runner: a bridge task from a decision
        # preempts whatever step was running, so end the activity honestly
        # rather than letting the runner report progress it no longer has.
        if d.action.type in BRIDGE_TASKS:
            self._end_activity_if_running("preempted_by_decision")
        time.sleep(reaction_delay(self.rng))  # humanizer: 300-900 ms reaction
        self._execute_action(d.action.type, d.action.params)

    def _execute_action(self, action_type: str, params: dict[str, Any]) -> str | None:
        """Run one decision-schema action. Returns the bridge task id when the
        action posted a task (POST /task's `task_id` is authoritative — the
        caller must match on it, never on whatever id the next snapshot shows),
        and None for primitives, quiet periods and failures."""
        try:
            if action_type == "wait":
                # A long `wait` must NOT block the loop: perception has to keep
                # running at 2-4 Hz or a death mid-wait goes unseen and the
                # site's heartbeat goes stale (>60 s reads as offline, §5).
                # The loop honours the quiet period instead.
                seconds = max(0.0, min(60.0, float(params.get("seconds", 5))))
                self._quiet_until = time.monotonic() + seconds
                log.info("quiet period", extra={"kv": {"seconds": round(seconds, 1)}})
                return None
            # Any other action ends a quiet period: he decided to do something.
            self._quiet_until = 0.0
            if action_type in BRIDGE_TASKS:
                return self.bridge.post_task(action_type, params)
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
        except BridgeTransientError as exc:
            # not_ready / game_thread_stalled / queue_full (or an unknown code):
            # the command did not land. Normal during a loading screen; the
            # caller sees None and the reflexes will ask again.
            log.info(
                "action deferred: bridge not ready",
                extra={"kv": {"type": action_type, "error": exc.error}},
            )
        except BridgeApiError as exc:
            # v1.2 409s (`not_in_vehicle`, `unstick_conditions_not_met`) are the
            # bridge refusing something that does not apply right now — honking
            # on foot, for one. That is a normal answer, not an error; the 400s
            # (bad params / unknown type) are a real bug and stay loud.
            if exc.status == 409:
                log.info(
                    "action refused: does not apply right now",
                    extra={"kv": {"type": action_type, "error": exc.error}},
                )
            else:
                log.error(
                    "bridge rejected action",
                    extra={
                        "kv": {
                            "type": action_type,
                            "status": exc.status,
                            "error": exc.error,
                            "detail": exc.detail[:160],
                        }
                    },
                )
        except Exception as exc:
            log.error(
                "action execution failed",
                extra={"kv": {"type": action_type, "error": f"{type(exc).__name__}: {exc}"[:160]}},
            )
        return None

    # -- activities ------------------------------------------------------------

    def _end_activity_if_running(self, outcome: str) -> None:
        ended = self.activity_runner.finish(outcome)
        if ended is None:
            return
        _, payload = ended
        self.writer.record_event("activity_end", payload)

    def _drive_activities(self, state: GameState) -> None:
        """Give the show rhythm when nobody is steering.

        Only runs in ordinary time: no mission, no stars, not on a break, not
        dead/arrested, and the governor still allowing behaviour (L3 is asleep).
        The brain preempts freely; that is handled in `_apply_decision`.
        """
        if self.governor.level >= 3 or self.breaks.on_break:
            return
        if state.player.dead or state.player.arrested:
            return
        if self.missions.in_mission or state.player.wanted > 0:
            if self.activity_runner.current is not None:
                self._end_activity_if_running(
                    "mission" if self.missions.in_mission else "wanted"
                )
            return

        if time.monotonic() < self._quiet_until:
            return  # a `wait` step is still running its course

        if self.activity_runner.current is not None:
            step = self.activity_runner.next_step(
                state.last_task.status, state.last_task.id
            )
            if step is not None:
                self._issue_activity_step(step)
            elif self.activity_runner.current is not None and self.activity_runner.current.finished:
                self._end_activity_if_running("completed")
            return

        if not self.activity_runner.due():
            return
        started = self.activity_runner.start(self.mood.mood, self.mood.driving_style())
        if started is None:
            return
        activity, first = started
        self.writer.record_event(
            "activity_start", {"activity": activity.name, "params": dict(first["params"])}
        )
        self.memory.log_day("activity", f"started {activity.name}: {activity.brief}")
        self._issue_activity_step(first)

    def _issue_activity_step(self, step: dict[str, Any]) -> None:
        """Run one plan step and bind the runner to the task id POST /task returned.

        Binding to the returned id rather than to the next snapshot's
        `last_task.id` is what stops the runner from latching the PREVIOUS task
        (3 Hz poll vs a 60 Hz game thread) and skipping the step.
        """
        task_id = self._execute_action(step["type"], step["params"])
        if step["type"] in BRIDGE_TASKS and task_id is None:
            # The step never reached the game (bridge down / not ready). Nothing
            # will complete it; end the activity now rather than sit through the
            # step timeout pretending it is running.
            self._end_activity_if_running("bridge_task_lost")
            return
        self.activity_runner.bind_step_task(task_id)

    # -- governor L3 -----------------------------------------------------------

    def _begin_scenic_park(self, state: GameState) -> None:
        """Governor L3: drive to the nearest scenic spot, then sleep there (§7).

        §7 L3 is "asleep in the car: parks somewhere scenic". A bare `stop`
        satisfies "asleep" and not "somewhere scenic" — and where he happens to
        be when the hourly cap trips can be lane three of a freeway, which is
        both a bad picture and a good way to be rear-ended while nobody is
        thinking. The drive is an ordinary bridge task, so the engine does it
        for free; L3 still makes no model calls.
        """
        self._end_activity_if_running("governor_l3")
        self._park_task_id = None
        self._park_deadline = 0.0
        if not state.player.in_vehicle or state.player.dead or state.player.arrested:
            # On foot / dead / in a cell: there is no car to be asleep in. Stop
            # and say so plainly rather than walking him across the map.
            self._execute_action("stop", {})
            self._say(
                "Out of budget for the hour. Standing right here until the meter resets."
            )
            return
        pos = (state.player.pos.x, state.player.pos.y, state.player.pos.z)
        spot, plan = scenic_park_plan(pos, self.mood.driving_style())
        task_id = self._execute_action(plan[0]["type"], plan[0]["params"])
        if task_id is None:
            # Could not post the drive (bridge down / not ready): stop where he
            # is. Better parked badly than driving with nobody watching.
            self._execute_action("stop", {})
            self._say("Out of budget for the hour. Parking. Back when the meter resets.")
            return
        self._park_task_id = task_id
        self._park_deadline = time.monotonic() + L3_PARK_TIMEOUT_S
        log.info(
            "governor L3: parking somewhere scenic",
            extra={"kv": {"spot": spot, "task_id": task_id}},
        )
        self._say(
            "Out of budget for the hour. Finding a view to sleep in front of."
        )

    def _finish_scenic_park_if_arrived(self, state: GameState) -> None:
        """Second half of the L3 park: stop once the scenic drive ends."""
        if self._park_task_id is None:
            return
        if self.governor.level < 3:
            # The hour reset mid-drive; he is awake and the drive is his again.
            self._park_task_id = None
            self._park_deadline = 0.0
            return
        lt = state.last_task
        arrived = lt.id == self._park_task_id and lt.status in ("done", "failed")
        hijacked = lt.status == "running" and lt.id != self._park_task_id
        expired = time.monotonic() >= self._park_deadline
        if not (arrived or hijacked or expired):
            return
        self._park_task_id = None
        self._park_deadline = 0.0
        if hijacked:
            return  # something else is driving; do not fight it
        self._execute_action("stop", {})
        self._say(
            "Parked. Engine off, meter running down. Back when the hour resets.", "chill"
        )

    def _on_game_restart(self) -> None:
        """Wipe every observer that compares against a pre-crash snapshot.

        Without this the first post-relaunch tick invents events: a death
        because the player was dead when the game died, a wanted_change from a
        stale star count, a finished task that no longer exists.
        """
        self.perceptor = Perceptor()
        self.stuck = StuckDetector()
        self.stranded = StrandedEscalator()
        self.missions = MissionTracker()
        self._end_activity_if_running("game_restarted")
        self._pending_screenshot_trigger = None
        if self.grabber is not None:
            try:
                self.grabber.reset()
            except ScreenshotUnavailableError as exc:
                log.warning(
                    "screen capture lost across the restart",
                    extra={"kv": {"reason": str(exc)[:160]}},
                )
                self.grabber = None
        self._say("Something rebooted. It wasn't me. Where's my car.")

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
                "heartbeat_at": datetime.now(UTC).isoformat(),
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
            self._say("Back. Did anyone move my car.", "chill")
            # `break` is in the director's BIG_EVENTS; nothing used to set it, so
            # that entry never fired. Coming back is the half worth a line — the
            # start of a break returns early and never reaches the brain.
            self._pending_big_event = "break"
            return False
        if self.breaks.on_break:
            return True
        if self.breaks.due() and not self.missions.in_mission and state.player.wanted == 0:
            planned = self.breaks.start()
            with contextlib.suppress(BridgeError):
                # Down or not-ready: he takes the break either way; the game is
                # not going anywhere without a task.
                self.bridge.post_task("stop", {})
            self.writer.record_event("break", {"phase": "start", "planned_s": planned})
            self._say("Even I stop for gas. Back in a few.", "chill")
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
        self._start_clips()
        self._write_session_start()
        self.bus.publish("counters", dict(self.counters))

        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            # SIGBREAK is Windows-only (Ctrl+Break, and what a scheduled task's
            # "End task" delivers); absent everywhere else.
            sig = getattr(signal, name, None)
            if sig is not None:
                signal.signal(sig, lambda *_: self._stop.set())

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
                except BridgeTransientError as exc:
                    # v1.2: not_ready / game_thread_stalled / queue_full — and
                    # any code this version does not know. The bridge is up and
                    # answering; the game just has no snapshot yet. Normal for
                    # the whole of a launch/loading screen, so wait, do NOT
                    # claim a bridge_down, and above all do not exit the loop.
                    wait_s = self.bridge_stall.record_failure(exc)
                    self._heartbeat(None)
                    self._maybe_flush()
                    self._stop.wait(wait_s)
                    continue
                except BridgeApiError as exc:
                    # A non-transient bridge error on /state means one side's
                    # contract copy is wrong. Loud, but still not fatal: the show
                    # keeps polling rather than dying on a bad status line.
                    log.error(
                        "bridge rejected /state; still polling",
                        extra={"kv": {"status": exc.status, "error": exc.error, "detail": exc.detail[:160]}},
                    )
                    self._maybe_flush()
                    self._stop.wait(2.0)
                    continue

                self.bridge_stall.record_success()
                up_payload = self.bridge_down.record_success()
                if up_payload is not None:
                    self.writer.record_event("bridge_up", up_payload)

                # A relaunched game means every "what changed since last tick"
                # comparison is against a dead world. Reset before perceiving.
                if self.restart.check(state):
                    self._on_game_restart()
                    self._heartbeat(state)
                    self._maybe_flush()
                    continue

                objective_hash = None
                if self.grabber is not None:
                    # No frame this tick just means no HUD-hash signal this tick.
                    with contextlib.suppress(ScreenshotUnavailableError):
                        objective_hash = objective_region_hash(self.grabber.grab())
                delta = self.perceptor.observe(state, objective_hash)
                self.mood.observe("quiet")

                if self._handle_breaks(state):
                    self._heartbeat(state)
                    self._maybe_flush()
                    self._stop.wait(5.0)
                    continue

                self._reflex(state, delta)

                if self._pending_park:
                    # Governor L3: park somewhere scenic and stop thinking. The
                    # overlay already carries the honest note via the governor
                    # event; this is the "asleep in the car" half.
                    self._pending_park = False
                    self._begin_scenic_park(state)
                else:
                    # Never on the same tick as the post: `state` predates it.
                    self._finish_scenic_park_if_arrived(state)

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
                        self.tactical_cadence.fired(now, level, self.mood.mood)
                        if result is not None:
                            self._apply_decision("tactical", result)
                    elif (
                        level < 3
                        and self.activity_runner.current is None
                        and now >= self._quiet_until
                        and state.last_task.status in ("idle", "done")
                    ):
                        idle = self.idle.pick(self.mood.mood)
                        if idle is not None:
                            self._execute_action(idle.action["type"], idle.action["params"])

                # After the brain, so a decision always gets first refusal on the
                # tick and the runner never posts a task just to have it
                # preempted a few milliseconds later.
                self._drive_activities(state)

                self._heartbeat(state)
                self._maybe_flush()
                elapsed = time.monotonic() - loop_started
                self._stop.wait(max(0.05, poll_interval - elapsed))
        finally:
            log.info("harness stopping")
            self._end_activity_if_running("shutdown")
            self.writer.record_event("session_end", {"reason": "shutdown"})
            self.writer.flush()
            if self.clips is not None:
                self.clips.close()
            self.bridge.close()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wasted_harness.main",
        description="WANTED harness: the agent's brain, hands and feed.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="probe every prerequisite for real (pricing, state dir, bridge, "
        "claude key + prefix cache minimums, supabase, obs, screenshots, "
        "SendInput/session, overlay port) and exit nonzero if any is missing",
    )
    mode.add_argument(
        "--prompt-audit",
        action="store_true",
        help="validate prompts, decision schema, catalog/schema agreement and "
        "real prefix token counts without a game (no fabricated game state, "
        "and therefore no live decision)",
    )
    args = parser.parse_args(argv)
    setup_logging()
    try:
        settings = Settings.load()
    except ConfigError as exc:
        log.critical(str(exc))
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    if args.check:
        return run_check(settings)
    if args.prompt_audit:
        return run_prompt_audit(settings)
    try:
        return Harness(settings).run()
    except ConfigError as exc:
        log.critical(str(exc))
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
