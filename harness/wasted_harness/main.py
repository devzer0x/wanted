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
from .behavior.missions import MissionFollower, MissionTracker
from .behavior.planner import DayPlanner
from .behavior.recovery import (
    ApiBackoff,
    BlockingScreenWatchdog,
    BridgeDownTracker,
    BridgeStallTracker,
    ClearedByGameBackoff,
    DamageTracker,
    DeathArrestRecovery,
    GameRestartDetector,
    JackHandoffGate,
    OffLoopGrab,
    RoadDodge,
    StrandedEscalator,
    StuckDetector,
    TaskStallDetector,
    ThreatLatch,
    WaterEscalator,
    classify_api_failure,
    flipped_action,
    threat_action,
)
from .behavior.roam import (
    PREEMPTED_OUTCOME as ROAM_PREEMPTED,
)
from .behavior.roam import HouseEscape, InteriorEscape, RoamEngine, as_activity
from .behavior.vehicle import (
    ControlRegained,
    MovementToken,
    MovementWheel,
    VehicleController,
    VehiclePhase,
    resume_action,
)
from .brain.characters import (
    CHECKED_NAMES,
    absent_names_mentioned,
    has_unidentified_friendly,
    present_names,
)
from .brain.director import VISION_TRIGGERS, DirectorBrain, DirectorCadence
from .brain.knowledge_base import mission_state_hint, render, select
from .brain.memory import Memory
from .brain.mission_knowledge import (
    LearnedScripts,
    identify_mission_with_source,
    load_missions,
    mission_card,
)
from .brain.prompts import banned_phrases, director_static_prefix, tactical_static_prefix
from .brain.schemas import (
    ACTION_TYPES,
    BRIDGE_TASKS,
    DEDUPE_RECENT_WINDOW,
    MOVEMENT_TASKS,
    PHONE_TASKS,
    DecisionModel,
    DecisionValidationContext,
)
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
from .brain.vision import MissionOutcome, read_mission_outcome, read_mission_title
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
from .budget import LEVEL_NOTES, BudgetGovernor, Pricing, cost_of_usage, cost_usd
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
from .totals import LifetimeTotals

log = get_logger("wasted.main")

#: Timescale held while the brain thinks. **1.0 = no dip at all**, and at 1.0
#: `_think` never touches POST /timescale (no dip, no restore, no 503 path).
#:
#: It used to be 0.15. The idea was to buy the model time; what it actually
#: bought was a show that drops to 15% speed for the two-to-four seconds of
#: every tactical call and then snaps back — "goes slow for 3-4 seconds then
#: normal speed", every clip looking broken — and a failure mode where a
#: restore that 503s leaves the world in slow motion indefinitely (four
#: occurrences in one session's logs). Normal speed during a think is exactly
#: what a human player experiences: perception still runs at 3-4 Hz and the
#: reflex layer, not the model, is what keeps him alive inside those seconds.
THINKING_TIMESCALE = 1.0
#: Anything at or above this counts as normal time; below it the world is in
#: slow motion and, unless the GAME put it there, wants restoring.
NORMAL_TIMESCALE = 1.0
TIMESCALE_OK_ABOVE = 0.99
#: POST /timescale is clamped to 0.1-1.0 bridge-side (CONTRACTS §1), so a value
#: strictly below 0.1 cannot be one the harness set — it is the game's own
#: effect (the death slow-motion sits around 0.075). Never fight that.
TIMESCALE_GAME_FLOOR = 0.1
#: Re-assert normal time at most this often, so a game that keeps overriding it
#: gets one POST every few seconds instead of one per tick.
TIMESCALE_REASSERT_INTERVAL_S = 5.0
#: How old the last captured frame may be and still be an honest screenshot of
#: "now" for an event. Beyond this the harness reports no screenshot rather
#: than uploading a stale frame with a fresh event's name on it.
FRAME_MAX_AGE_S = 2.0
STATS_INTERVAL_S = 5.0
FLUSH_INTERVAL_S = 2.0
#: The goal shown to the brain fresh at startup, and again the moment he
#: respawns from a death/arrest — a stale pre-death goal must not survive a
#: respawn (WP-.../death recovery item 2: "clear stale task/goal state").
#: Character budget for the retrieved-knowledge block in the dynamic context. The tactical
#: prefix is ~15k cached tokens and a warm call costs ~$0.0026 at ~240 calls/hour; this block
#: is NOT cached (it changes every tick), so every character is paid for at the full input
#: rate on every decision. 1800 chars is roughly 450 tokens, about a dozen retrieved items -
#: enough to answer "what should I know right now" without pushing the state snapshot and the
#: mission card out of the model's attention, which is what a bigger budget actually costs.
KNOWLEDGE_BUDGET_CHARS = 1800

INITIAL_GOAL = "wake up, find wheels, see what the day wants"

#: T5 (findings.md R3): the dashboard CURRENT GOAL when neither a roam goal is
#: locked nor a mission is tracked — the plugin's own fixed, honest fallback.
#: Never the model's free-text `goal`; see `Harness.goal_text`.
NEUTRAL_GOAL_TEXT = "seeing what Los Santos throws at him next"

#: T3 (findings.md R2): action types that mean "he is fighting" for the
#: purposes of gating `say` on an event — the decision itself choosing one of
#: these is the event, independent of whatever `nearby.peds`/`threat` said a
#: tick ago (this module has no cross-tick memory of THAT beyond `delta`).
FIGHT_ACTION_TYPES: frozenset[str] = frozenset({"fight_ped", "combat_hated_targets_around"})

#: T8 (findings.md R6): with missions off, how long an auto-answered call is
#: left connected before `_phone_reflex` hangs it up on its own — long enough
#: to actually be entertaining (the operator's own word), short enough that a
#: call cannot silently own the wheel for the rest of the session the way a
#: connected story call did before T8 (R1's own evidence: "a story call was
#: CONNECTED the whole time; the game takes the ped's task for the phone
#: UI"). Overridden to 0 s effectively by the fight/chase check in
#: `_phone_reflex`, which hangs up immediately regardless of this budget.
PHONE_HANGUP_AFTER_S = 25.0

#: T4 (findings.md R4): the catalogued mission-name vocabulary MINUS anything
#: that collides with a character name (`CHECKED_NAMES`) — computed once, not
#: per decision. A mission called "Chop" would otherwise fight the ped named
#: Chop for the same word, and the two checks would contradict each other.
_KNOWN_MISSION_NAMES: frozenset[str] = frozenset(
    m["name"]
    for m in load_missions()
    if m.get("name") and m["name"].strip().lower() not in {n.lower() for n in CHECKED_NAMES}
)


def _tick_event_reason(
    delta: Delta,
    big_event: str | None,
    roam_transition: bool,
    action_type: str,
) -> str | None:
    """T3: why this tick's `say` may be published, or None when nothing did.

    findings.md R2 — tactical decisions fire on every poll (8-25 s), and every
    one of them used to publish `d.say` unconditionally, so a stationary the agent
    narrated a fresh line each cycle with nothing behind it. The fix is in
    code: `say` only goes out when THIS tick carries one of the events T3
    names — a CONTRACTS §4 event (`big_event`, popped once per tick from
    `_pending_big_event`), a roam goal pick/done/fail (the transition line
    `RoamEngine.note()` just drained, sniffed in `_dynamic_context`), a
    vehicle change, a wanted change, a fight (the decision's own action is one
    of `FIGHT_ACTION_TYPES`), a death, or control regained (a respawn or a
    cutscene ending). Deliberately NOT here: `task_finished`/`danger`/
    `objective_change` — the tactical cadence's own trigger reasons — because
    `danger` alone is true on every poll for the whole length of a chase
    (brain.tactical's own docs), which is exactly the "narrating nothing"
    failure this exists to stop being possible in code.
    """
    if big_event is not None:
        return f"event:{big_event}"
    if roam_transition:
        return "roam_goal_transition"
    if delta.entered_vehicle or delta.exited_vehicle:
        return "vehicle_change"
    if delta.wanted_changed:
        return "wanted_change"
    if delta.died:
        return "death"
    if delta.respawned or delta.cutscene_ended:
        return "control_regained"
    if action_type in FIGHT_ACTION_TYPES:
        return "fight"
    return None

# `ROAM_PREEMPTED` (= `behavior.roam.PREEMPTED_OUTCOME`) is the
# `activity_end.outcome` that means "a higher owner took the movement wheel off
# free roam". It is the harness's `roam_goal_failed{reason: "preempted", by:
# <owner>}`: `roam_goal_failed` is NOT one of the CONTRACTS §4 event types, §4
# is a closed enum, and its Supabase CHECK constraint is already live on the
# production project — an insert of an unlisted type would be rejected and take
# the whole batch with it. So the signal rides on `activity_end` with the goal
# id, the outcome and the preempting owner in the payload, exactly as
# `RoamEngine.close()` has documented since it was written.
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
    if pricing.tactical_mission is not None:
        rep.note(f"tactical_mission={pricing.tactical_mission.id} (mid-mission tactical model)")
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
    if pricing.tactical_mission is not None and "tactical_mission" in counts:
        rep.note(
            f"tactical_mission prefix {counts['tactical_mission']} tok "
            f">= {pricing.tactical_mission.min_cacheable_prefix_tokens} minimum"
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
        frame, _captured_at = grabber.grab()
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
            tiers = [("tactical", pricing.tactical), ("director", pricing.director)]
            if pricing.tactical_mission is not None:
                tiers.append(("tactical_mission", pricing.tactical_mission))
            for tier, mp in tiers:
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


def _threat_and_blips_line(state: GameState) -> str:
    """One short line for `threat.*` and named `mission.entity_blips[]`.

    The full `STATE:` json further down `_dynamic_context` already carries
    both, but the model should not have to hunt a 2-3 kB blob for "who is
    hitting me" and "where is the crewmate whose dot I lost" — those are
    exactly the two v1.11 facts this function exists to surface. Kept SHORT
    and deliberately not the whole array: this rides in the uncached dynamic
    half and costs tokens every single call. A free function (not a method):
    it needs nothing from `self`, and every test constructing a stand-in
    harness for `_dynamic_context` gets it for free.
    """
    bits: list[str] = []
    t = state.threat
    if t.attacker_handle is not None:
        weapon = ""
        for ped in state.nearby.peds:
            if ped.handle == t.attacker_handle:
                weapon = f", {ped.weapon_class}" if ped.weapon_class else ""
                break
        bits.append(f"ATTACKER: handle {t.attacker_handle}{weapon} — use fight_ped")
    if t.being_jacked_by is not None:
        bits.append(f"BEING JACKED BY: handle {t.being_jacked_by} — use fight_ped")
    named = [b for b in state.mission.entity_blips if b.name]
    if named:
        top = ", ".join(f"{b.name} {b.distance:.0f}m" for b in named[:3])
        bits.append(f"NAMED BLIPS: {top}")
    return " | ".join(bits)


def _phone_line(state: GameState, missions_enabled: bool) -> str:
    """CONTRACTS v1.13 `phone`, as one line — empty when the phone is quiet.

    T8 (findings.md R6): "it cant cut the call, check or accept whatever" plus
    "calls are entertaining and can start story" — the reflex layer
    (`_phone_reflex`) now ANSWERS every ring itself, missions on or off, so
    this is no longer a live choice offered to the model the way it was
    pre-T8. It only ever reports the FACT of what the reflex is doing or has
    done, which is good material and cannot contradict what the viewer sees
    on screen:

    * **Jobs on.** Answering may start a mission — that is the point. Nothing
      here ever hangs up on him.
    * **Jobs off.** He is not taking jobs, but the call is still answered (it
      is entertaining); the reflex hangs it up on its own after
      `PHONE_HANGUP_AFTER_S`, sooner if a fight or a chase is on.

    A free function, not a method, for the same reason
    :func:`_threat_and_blips_line` is one: it needs nothing from `self`, and
    every test standing in a harness for `_dynamic_context` gets it free.
    """
    phone = state.phone
    if phone.in_call:
        if missions_enabled:
            return (
                "PHONE: a call is connected. You cannot see who it is and you did not pick "
                "the words. Play whatever it leads to."
            )
        return (
            "PHONE: a call is connected. Jobs are switched off, so the harness will hang up "
            "on its own soon (sooner if a fight or a chase is on) — you did not pick the "
            "words and there is nothing for you to do about the call itself."
        )
    if not phone.ringing:
        return ""
    if missions_enabled:
        return (
            "PHONE: ringing — the harness is answering it for you. Answering a story call "
            "STARTS THAT JOB. You cannot see who is calling, so do not name them."
        )
    return (
        "PHONE: ringing — jobs are switched off, but the harness answers anyway (calls are "
        "entertaining and can start story); it will hang up on its own soon. You cannot see "
        "who it was."
    )


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
        #: CONTRACTS v1.10 `mission.script` -> mission name, learned live
        #: (never shipped as guesses — see LearnedScripts' own docstring).
        self.learned_scripts = LearnedScripts(state_dir / "learned_mission_scripts.json")

        #: Counters, played time and the budget ledger that must survive a
        #: restart (see totals.py for why they are lifetime, not per-session).
        self.totals = LifetimeTotals.load(state_dir)
        self._seed_totals_once()

        # `on_change` is wired only AFTER seeding: adopting the previous
        # process's spend is not a level TRANSITION and must not announce one on
        # the feed. Everything after this point does announce, as §7 requires.
        self.governor = BudgetGovernor(hourly_cap_usd=settings.hourly_cap_usd)
        self.governor.seed(self.totals.budget_seed())
        self.governor.on_change = self._on_governor_change
        # The brain is mandatory: fail loudly here, never a silent degraded mode.
        self.anthropic = make_client(settings)
        verify_model_ids(self.anthropic, self.pricing, self.governor.record)
        verify_prefix_cacheable(self.anthropic, self.pricing)
        # Every billed call reports its own cost, whether or not the caller can
        # use the answer — a schema-rejected decision is still a decision
        # Anthropic charged for.
        self.tactical = TacticalBrain(self.anthropic, self.pricing, self.governor.record)
        self.director = DirectorBrain(self.anthropic, self.pricing, self.governor.record)
        #: The identified story mission (a missions.json entry) while one is active, else None.
        #: Set at mission_start from the screen-read title (fallback: zone), cleared at end/fail.
        self.current_mission: dict | None = None
        self.tactical_cadence = TacticalCadence(self.rng)
        self.director_cadence = DirectorCadence(self.rng)

        self.bridge = BridgeClient(settings.bridge_url)
        self.perceptor = Perceptor()
        self.mood = MoodModel(rng=self.rng)
        self.idle = IdlePicker(self.rng)
        self.breaks = BreakScheduler(rng=self.rng)
        self.activities = ActivityPicker(self.rng)
        self.activity_runner = ActivityRunner(self.activities, self.rng)
        #: Free roam's single owner: a catalog of goals with `needs` gates and
        #: `done_when` predicates the code evaluates against /state, one of which
        #: is LOCKED at a time. It replaces the ActivityPicker's weighted draw
        #: (which is why `self.activities` above is now only the L3 scenic-park
        #: helper and the death-spot memory); the ActivityRunner is kept as the
        #: step machine it always was, driven from here instead of from a pick.
        self.roam = RoamEngine(self.rng, missions_enabled=settings.missions_enabled)
        #: The one thing StrandedEscalator cannot do: get him out of a building.
        #: Runs BEFORE it, because widening a vehicle search from inside a house
        #: just picks a car that is further away and behind more walls.
        self.house_escape = HouseEscape()
        #: CONTRACTS v1.12: the same job, off the bridge's own `player.interior`
        #: instead of `house_escape`'s guess. This is the one that actually gets
        #: reached in the live loop — `house_escape` needed 20 s of stillness AND
        #: a failed `enter_nearest_vehicle` before it would even look.
        self.interior_escape = InteriorEscape()
        self.missions = MissionTracker()
        self.mission_follower = MissionFollower()
        #: Decides the SHAPE of the day: roam blocks and mission blocks, and
        #: when to go and stand in a `mission.starts[]` marker. No model call.
        self.planner = DayPlanner(self.rng, missions_enabled=settings.missions_enabled)
        self.stuck = StuckDetector()
        #: The on-foot half of "stuck": `StuckDetector` needs a vehicle and a
        #: driving task, so a task that runs forever and pins him on foot was
        #: invisible to every observer here (measured: 20 s of RUNNING combat,
        #: 0.2 m of movement).
        self.task_stall = TaskStallDetector()
        self.cleared_backoff = ClearedByGameBackoff()
        self.stranded = StrandedEscalator()
        #: T9 (findings.md R1/R5): `vehicle.in_water` past 10 s → exit, then
        #: one walk toward known land; an NPC vehicle closing fast while he is
        #: on foot → step off; recently jacked → hold `stranded` off so
        #: roam's own `take_my_car_back` gets first refusal at the same car.
        self.water = WaterEscalator()
        self.road_dodge = RoadDodge()
        self.jack_handoff = JackHandoffGate()
        self.threat_latch = ThreatLatch()
        #: Health across ticks. The relationship field in `nearby.peds` says
        #: what the engine's relationship GROUPS are, not who is currently
        #: swinging; losing HP is the signal that cannot lie (behavior.recovery).
        self.damage = DamageTracker()
        #: Getting into a car is the START of something. Nothing here used to
        #: watch the window between `enter_nearest_vehicle -> done` and the car
        #: actually moving, which is how he sat in a stolen convertible and let
        #: the owner beat him to death through the open door (2026-09-02).
        self.vehicle = VehicleController()
        #: F6. Every moment the game hands the controls back — a respawn, the
        #: end of a cutscene or a mission, a protagonist switch finishing, a
        #: door out of an interior, `control_enabled` coming back — starts a
        #: three-second stopwatch. If no layer in the whole ladder posts a
        #: movement task before it runs out, the `resume` rung does. Measured,
        #: not assumed: the whole point is that "somebody will handle it" is
        #: exactly what the 2 h of logs behind docs/findings.md disproved.
        self.control_regained = ControlRegained()
        #: One owner of bridge-task movement at a time, ENFORCED. Every
        #: `_execute_action` that posts a CONTRACTS §1 task carries the
        #: holder's token and is refused without it, which is where the
        #: "two owners issued movement in the same tick" deadlock class dies.
        self.wheel = MovementWheel()
        #: What losing the wheel MEANS for each owner. The wheel cannot reach
        #: the roam engine, the mission follower or the day planner from
        #: `behavior/vehicle.py` without an import cycle, and "what do I cancel
        #: when I am preempted" is each layer's own business anyway.
        self.wheel.on_preempt("roam", self._roam_preempted)
        self.wheel.on_preempt("mission", self._mission_preempted)
        self.wheel.on_preempt("day_plan", self._day_plan_preempted)
        self.wheel.on_preempt("governor", self._governor_preempted)
        self.wheel.on_preempt("exit_interior", self._interior_preempted)
        #: Open-lease tokens: the owners that hold the wheel across many ticks
        #: and release explicitly (a locked roam goal, an active mission, the
        #: day plan's trip to a marker, the governor's scenic park). The reflex
        #: layer takes one-tick leases instead and simply asks again.
        self._roam_token: MovementToken | None = None
        self._mission_token: MovementToken | None = None
        self._day_plan_token: MovementToken | None = None
        self._governor_token: MovementToken | None = None
        #: The escape holds an OPEN lease too: its ladder runs across many ticks
        #: with its own per-rung timeouts, and holding it is what keeps free roam
        #: from picking a goal he could not possibly walk to from a living room.
        self._interior_token: MovementToken | None = None
        self.bridge_down = BridgeDownTracker()
        self.bridge_stall = BridgeStallTracker()
        self.restart = GameRestartDetector()
        self.api_backoff = ApiBackoff(rng=self.rng)
        self.death_recovery = DeathArrestRecovery()
        self.blocking_screen_watchdog = BlockingScreenWatchdog()

        try:
            self.grabber: ScreenGrabber | None = ScreenGrabber()
        except ScreenshotUnavailableError as exc:
            log.warning("screenshots disabled", extra={"kv": {"reason": str(exc)[:140]}})
            self.grabber = None
        #: Every dxcam touch in the running harness goes through this one
        #: worker (see `_grab_frame_and_hash`): off the loop thread, deadlined,
        #: and single-flight, because the capture device is not re-entrant.
        self.grab_pump = OffLoopGrab(self._grab_frame_and_hash, name="screen-grab")
        #: Latest real frame + when it was taken. Event screenshots read this
        #: instead of grabbing again, so no code path outside the worker can
        #: block the loop on a wedged capture device.
        self._frame: Any = None
        self._frame_at = 0.0
        #: Set by `_on_game_restart`; consumed by the grab worker, so the dxcam
        #: device rebuild also happens off the loop thread and never races a
        #: grab that is already running.
        self._grab_reset_wanted = False
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

        #: LIFETIME totals, not this process's. The site renders these three
        #: with no session qualifier and the character page calls the death
        #: count "the counter on the front page", so a per-process counter
        #: published 0/0/0 on every watchdog restart — an unlabelled number
        #: that silently under-reports. `self.counters` is a live mirror of
        #: `self.totals`; the totals file is the source of truth.
        self.counters = self.totals.counters()
        self.current_goal = INITIAL_GOAL
        self._pending_big_event: str | None = None
        self._pending_screenshot_trigger: str | None = None
        self._pending_park = False
        #: T3: did THIS tick's dynamic context carry a roam goal pick/done/fail
        #: transition line? Set in `_dynamic_context` (from `RoamEngine.note()`,
        #: which drains it), read once by the paired `_apply_decision` call —
        #: both happen back to back on the same tick, single-threaded, so there
        #: is no staleness window between the two.
        self._tick_roam_transition = False
        #: Governor L3 park-somewhere-scenic state: the task id of the drive to
        #: the scenic spot, and its deadline. None when not parking.
        self._park_task_id: str | None = None
        self._park_deadline = 0.0
        #: Deliberate stillness (the `wait` action) as a deadline rather than a
        #: blocking sleep, so perception keeps running through it.
        self._quiet_until = 0.0
        #: Per-tick `/state` facts a `wait` may be waiting for (`_wait_has_a_reason`).
        self._mission_active = False
        self._wanted_now = 0
        #: Set from the latest /state each tick (`run()`); the single choke
        #: point `_execute_action` reads before posting ANY bridge task, from
        #: whichever source (reflex, activity, mission-follow, or the brain's
        #: own decision) — the game ignores tasks during a cutscene, so none
        #: get sent, rather than relying on every caller to remember to check.
        self._cutscene_active = False
        #: v1.11 siblings of `_cutscene_active`, same idiom, same choke point:
        #: a protagonist switch (the aerial fly-over) and a mission retry /
        #: checkpoint reload are both "the game owns the world right now",
        #: exactly like a cutscene — `_execute_action` refuses bridge tasks
        #: while either is true, and the tactical cadence skips the call
        #: entirely so no commentary is generated about a world that is about
        #: to be replaced or a body that is not really his yet.
        self._switch_in_progress = False
        self._retry_in_flight = False
        #: Set from the latest /state each tick, same idiom as
        #: `_cutscene_active`: `_execute_action` refuses any bridge task while
        #: this is true (dead/arrested — nothing productive to do until the
        #: game's own respawn), rather than relying on every caller to
        #: remember the check.
        self._player_down = False
        #: Set by `_reflex` from THIS tick's snapshot: the survival ladder
        #: (`threat_action`) wants the wheel right now. `_drive_day_plan`,
        #: `_drive_activities` and `_drive_mission_objective` run later in the
        #: same tick and read it before posting navigation - their own
        #: "someone else has the wheel" check reads `state.last_task`, which
        #: predates the reflex's POST and therefore cannot see it. Without
        #: this, a mission trip posts `walk_to` straight over the combat task
        #: issued milliseconds earlier, and ThreatLatch's hold-down then
        #: suppresses the re-post: the reflex is silently defeated.
        #: `DayPlanner` has its own copy of this gate for the hostile case
        #: (`_threat_close`), which by construction cannot see the
        #: damage-driven path - `relationship` is exactly what that path
        #: exists to stop depending on.
        self._threat_has_the_wheel = False
        self._under_attack = False
        #: T8 (findings.md R6): has THIS ring already been answered? Latched
        #: by `_phone_reflex` on an `answer_call` that actually reached the
        #: game, cleared the moment `phone.ringing` goes false. One attempt
        #: per ring: every POST /task preempts the running task, so
        #: re-posting at the 2-4 Hz poll rate would cancel whatever he was
        #: doing several times a second for the length of the ring.
        self._phone_answered_this_ring = False
        #: T8: has the CURRENT connected call already been hung up (missions
        #: off only)? Latched on a `reject_call` that actually reached the
        #: game while `phone.in_call`, cleared the moment the call ends.
        self._phone_hung_up_this_call = False
        #: T8: wall-clock time (`time.monotonic()`) the current call
        #: connected, or None while nothing is connected. Used to grade the
        #: `PHONE_HANGUP_AFTER_S` budget; reset every time `phone.in_call`
        #: goes false so a NEW call gets its own fresh budget.
        self._phone_call_connected_at: float | None = None
        #: T8: the hang-up budget while missions are off, and a plain
        #: attribute (not a bare module constant) so a test can shrink it
        #: without waiting out the real 25 s.
        self.phone_hangup_after_s = PHONE_HANGUP_AFTER_S
        #: Set each tick from `BlockingScreenWatchdog.blocked`: the game is on
        #: a modal screen (MISSION FAILED / a menu), the SHVDN script thread is
        #: not ticking, and therefore every field in `state` is a frozen lie.
        #: Read by `_execute_action` (nothing will run a task) and by
        #: `_dynamic_context` (so the brain stops narrating a world it cannot
        #: see - measured on the broadcast: "Alpha's right there. Staying on
        #: his six." over a MISSION FAILED banner, for over a minute).
        self._screen_blocked = False
        #: True only while `_think` is holding a deliberate timescale dip, so
        #: the tick-level guard below never fights the harness's own dip. At
        #: THINKING_TIMESCALE == 1.0 there is no dip and this stays False.
        self._thinking_dip_active = False
        self._last_timescale_reassert = 0.0
        self._last_stats = 0.0
        self._last_flush = 0.0
        self._started = time.monotonic()
        #: Monotonic time of the last tick whose /state we believed. Drives BOTH
        #: the played-time accumulator and the heartbeat: `heartbeat_at` is the
        #: site's only ON AIR signal (CONTRACTS §5), so it may only be refreshed
        #: while there is recent evidence the agent is actually in the world.
        self._last_live_state_at = 0.0
        #: ISO timestamp of the current mission's start, and the decision tokens
        #: spent inside it — the `missions` row's own fields, which nothing used
        #: to write at all.
        self._mission_started_iso: str | None = None
        self._mission_tokens = 0
        self._stop = threading.Event()

    # -- infrastructure --------------------------------------------------------

    def _seed_totals_once(self) -> None:
        """Adopt previously published totals the first time lifetime.json exists.

        One read, one time. If it fails the totals stay UNSEEDED (and start from
        whatever is on disk, usually zero) and the next start tries again — a
        seed that silently substituted zeros would freeze an under-reported
        history onto the front page, which is the bug this whole file exists to
        fix.
        """
        if self.totals.seeded:
            return
        seed = self.writer.fetch_lifetime_seed()
        if seed is None:
            log.warning(
                "lifetime totals not seeded: previously published rows could not be "
                "read. Counters run from what is on disk and the seed is retried "
                "next start",
                extra={"kv": {"path": str(self.totals.path)}},
            )
            return
        self.totals.apply_seed(seed)
        log.info(
            "lifetime totals seeded from previously published rows",
            extra={"kv": {**self.totals.counters(), "played_hours": round(self.totals.played_hours, 3)}},
        )

    def _record_api_usage(self, model_id: str, usage: object) -> None:
        """Cost sink for the raw (non-decision) calls: the two vision reads.

        They send an image and are billed like anything else; their `usage` used
        to be read nowhere at all, so the governor throttled on a figure that
        did not include them.
        """
        self.governor.record(cost_of_usage(self.pricing, model_id, usage))

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

    def _record_big_event(
        self, type_: str, payload: dict[str, Any], screenshot_url: str | None = None
    ) -> int | None:
        """Record a §4 event and return its row id when one can be known.

        A clip has to point back at the event that triggered it
        (`clips.event_id`, schema.sql:64), and a batched insert cannot report an
        id — `returning="minimal"` is what makes the batch cheap. So the two
        clip-worthy events get a single insert that returns their id, and ONLY
        when the clip pipeline is actually connected: with OBS absent there is
        no clip to link and no reason to pay for the extra round trip. `None`
        means "not known" (offline, or clips off) and the clip row honestly
        carries a null event_id rather than a made-up one.
        """
        if self.clips is not None and type_ in CLIP_EVENTS:
            return self.writer.insert_event_now(type_, payload, screenshot_url)
        self.writer.record_event(type_, payload, screenshot_url=screenshot_url)
        return None

    def _capture_clip_async(
        self, event_type: str, caption: str, event_id: int | None = None
    ) -> None:
        """Save + upload a replay off the loop thread. One clip at a time."""
        if self.clips is None or event_type not in CLIP_EVENTS:
            return
        if self._clip_thread is not None and self._clip_thread.is_alive():
            log.info("clip skipped: previous clip still uploading")
            return

        def worker() -> None:
            try:
                self.clips.capture_clip(
                    event_id=event_id, event_type=event_type, caption=caption
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

    def _clear_current_session(self) -> None:
        """Remove state/current_session.json on the way out.

        The watchdog's `tools/post_event` reads it to attribute out-of-process
        events. It was never deleted, so after the harness died every
        watchdog-posted `bridge_down` kept being stamped with the dead session's
        id and appeared as a fresh feed item under an OFF AIR banner.
        """
        try:
            (self.settings.state_dir / "current_session.json").unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "could not clear current_session.json; post-mortem events may be "
                "attributed to this session",
                extra={"kv": {"error": f"{type(exc).__name__}: {exc}"}},
            )

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
                # Explicit, not the column's `default now()`: a session row that
                # has to wait in the offline queue is inserted whenever Supabase
                # comes back, and the database would then stamp it with the
                # RECONNECT time. That is exactly the 2-hour gap between
                # sessions.started_at and this session's own session_start event
                # visible in the published data.
                "started_at": datetime.now(UTC).isoformat(),
                "game_edition": edition,
                "harness_version": __version__,
            }
        )
        self.writer.record_event(
            "session_start", {"harness_version": __version__, "game_edition": edition}
        )
        self.writer.flush()

    # -- reflex layer ----------------------------------------------------------

    def _grab_frame_and_hash(self) -> tuple[Any, int, float] | None:
        """The grab worker's whole body — the ONLY code that touches `grabber`.

        Runs on `grab_pump`'s daemon thread. Keeping every dxcam call (grab AND
        device rebuild) on that one thread is what makes the off-loop grab
        safe: Desktop Duplication is not re-entrant, so a second concurrent
        grab, or a rebuild racing a grab, is undefined behaviour. The objective
        hash is computed here too, so no image work lands on the loop thread
        either.
        """
        grabber = self.grabber
        if grabber is None:
            return None
        if self._grab_reset_wanted:
            self._grab_reset_wanted = False
            grabber.reset()
        img, captured_at = grabber.grab()
        return img, objective_region_hash(img), captured_at

    def _capture_screenshot(self, name_hint: str) -> tuple[bytes | None, str | None]:
        """Encode + upload the most recent real frame. Never grabs, never blocks.

        The frame comes from `grab_pump`, which took it earlier in this same
        tick. Grabbing again here would be a second, unbounded dxcam call on
        the loop thread — the exact 172 s stall this file now exists to
        prevent — and a second concurrent caller of a device that does not
        allow one. If the newest frame is older than FRAME_MAX_AGE_S the event
        gets no screenshot: a minutes-old picture filed as "this death" would
        be a lie, and CONTRACTS §4 allows a screenshot to be absent.
        """
        if self.grabber is None or self._frame is None:
            return None, None
        age = time.monotonic() - self._frame_at
        if age > FRAME_MAX_AGE_S:
            log.warning(
                "screenshot skipped: no fresh frame",
                extra={"kv": {"hint": name_hint, "age_s": round(age, 1)}},
            )
            return None, None
        try:
            jpeg = encode_jpeg(self._frame)
        except (ScreenshotUnavailableError, OSError) as exc:
            # PIL reports an unencodable frame as OSError; either way the event
            # goes out without a picture rather than taking the loop with it.
            log.warning("screenshot encode failed", extra={"kv": {"reason": str(exc)[:120]}})
            return None, None
        url = self.writer.upload_screenshot(jpeg, name_hint)
        return jpeg, url

    def _reflex(self, state: GameState, delta: Delta) -> None:
        # Re-decided from scratch every tick: "the survival ladder wants the
        # wheel right now". Read by `_drive_day_plan`, `_drive_activities` and
        # `_drive_mission_objective`, which run later in the same tick and
        # would otherwise post navigation straight over the combat/cover task
        # issued a few milliseconds ago from this same snapshot (their own
        # `last_task` check cannot see it yet - `state` predates the POST).
        self._threat_has_the_wheel = False
        self._under_attack = False
        # Open the movement tick: one-tick reflex leases expire here, so the
        # ladder is re-decided from scratch, and the "one bridge task per tick"
        # latch resets. Everything below asks the wheel for permission; a
        # refusal means post nothing, and every acquire/refusal/preemption is
        # logged with owner, reason and tick.
        self.wheel.begin_tick()
        if delta.died:
            self.counters["deaths"] = self.totals.bump("deaths")
            _, url = self._capture_screenshot("death")
            event_id = self._record_big_event(
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
            self._capture_clip_async("death", line, event_id)
            self._end_activity_if_running("interrupted_by_death")
        if delta.busted:
            self.counters["busted"] = self.totals.bump("busted")
            _, url = self._capture_screenshot("busted")
            event_id = self._record_big_event(
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
            self._capture_clip_async("busted", line, event_id)
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

        self._handle_mission_events(state, delta)

        # Dead/arrested: notice the game's own respawn and clear stale
        # mission/goal state before normal behaviour resumes, and log loudly
        # if "down" runs past a sane ceiling. Never simulates or hurries the
        # respawn itself (CLAUDE.md rule 5) — only the game's own
        # dead/arrested flag clearing counts. When a mission failure froze
        # the script thread on a blocking screen, that respawn only happens
        # once `_blocking_screen_watchdog` (run() wiring) has pressed it
        # clear; this class does not know or care which — it only reacts
        # once `dead`/`arrested` has actually gone back to false.
        recovery = self.death_recovery.feed(state)
        if recovery["respawned"]:
            self.mission_follower.reset()
            self.threat_latch.reset()
            # A death is a 200 HP drop and the respawn is a 200 HP jump; either
            # one left in the window would have him come back swinging at
            # nobody.
            self.damage.reset()
            # He respawns on foot, and the car he died in is gone. Leaving the
            # machine seated would have the drive-away reflex grade a vehicle
            # that no longer exists.
            self.vehicle.reset()
            self.current_goal = INITIAL_GOAL
            self._quiet_until = 0.0
            log.info(
                "respawn detected; stale mission/goal state cleared",
                extra={"kv": {"cause": recovery["respawn_cause"]}},
            )

        # THE TWO WHEEL OVERRIDES, before any layer may ask for it.
        #
        # 1. The game owns the controls (death, arrest, a cutscene, a
        #    protagonist switch, a checkpoint reload, `control_enabled` false).
        #    Nothing posted now reaches the player — `_execute_action` refuses
        #    every bridge task in exactly these states — so the wheel goes to
        #    `idle` and whatever was running is cancelled through its preempt
        #    hook. The ladder then starts from scratch when control comes back.
        # 2. Otherwise the mission flag drives the wheel: `mission.active`
        #    false->true takes it unconditionally for `mission`, true->false
        #    releases it. That is what makes "roam posts nothing during a
        #    mission" structural instead of a gate somebody can forget.
        if self._game_owns_controls(state):
            self.wheel.force_idle(self._game_control_reason(state))
            self._mission_token = None
        else:
            self._mission_token = self.wheel.mission_active(state.mission.active)

        # CONTRACTS v1.12, and it runs EVERY tick ahead of every gate: "he came
        # out" has to be seen on the tick it happens whatever else is going on,
        # and the position on that tick is the only honest source for a learned
        # way out of that interior. Dead, arrested, mid-cutscene, mid-mission —
        # all of them still leave doors behind them.
        left_interior = self.interior_escape.observe(state)
        if left_interior is not None and state.player.interior is None:
            # `interior -> null`. The wheel goes back and normal selection
            # resumes on this same tick.
            #
            # NOT written to Supabase: `left_interior` is not one of the
            # CONTRACTS §4 event types and §4 is a closed enum per contract
            # version, so `events.py` is not this package's to widen — the same
            # rule `roam.close()` documents for `roam_goal_picked`. It is a log
            # line, which is also what the acceptance check for this fix reads.
            log.info(
                "left_interior",
                extra={
                    "kv": {
                        "interior_id": left_interior,
                        "was_escaping": self._interior_token is not None,
                        "pos": [round(v, 1) for v in (state.player.pos.x, state.player.pos.y)],
                    }
                },
            )
            self._release_interior_wheel("left_interior")
            self.interior_escape.reset()

        # F6, and it is a MEASUREMENT, not a rung: every edge where the game
        # gives the controls back arms a three-second stopwatch, and the only
        # thing that stops it is a movement task actually reaching the bridge
        # (`_execute_action` calls `moved()` on the one line that posts). The
        # `resume` rung at the bottom of the ladder below is what answers a
        # stopwatch that ran out. `respawned` is passed in rather than derived
        # because `DeathArrestRecovery` can tell a respawn from an arrest
        # release and a raw `dead` false-edge cannot.
        self.control_regained.feed(state, respawned=bool(recovery["respawned"]))

        if state.player.dead or state.player.arrested:
            # Nothing physical to do but wait: every reflex below would
            # either be a no-op on a corpse (StuckDetector's reverse_out) or
            # actively wrong (exit_vehicle while dead, widening a vehicle
            # search for cuffed hands). `_drive_activities` already returns
            # early on this same condition; this is its `_reflex` counterpart.
            pass
        else:
            # Survival ladder, highest priority: the threat reflex answers a
            # firefight without a model call (CLAUDE.md rule 1's "survival
            # outranks the mission and free-roam" at reflex speed).
            #
            # What it does NOT do any more is short-circuit the rest of this
            # block. It used to: while any hostile stood within 40 m, the
            # `else:` below never ran, so `self.stuck.check()` was never
            # called — the unstick ladder could not fire at all during a
            # firefight, which is exactly when a car ends up wedged on a kerb.
            # A combat task and a stuck-car check are independent and both
            # cheap, and the physical-recovery half posts primitives or
            # /unstick, not tasks, so it cannot preempt the combat task — which
            # is also why `physical` calls `wheel.note()` rather than
            # `wheel.acquire()`: it takes nothing and refuses nobody, it only
            # keeps `taken_by_reflex()` honest for the planners below.
            #
            # `flipped_action` is the one exception: it posts `exit_vehicle`,
            # a real task, so it is computed FIRST and it now sits at the TOP
            # of the ladder (owner `flip`) rather than being posted at the
            # bottom of the block behind a `flip is None` guard on the threat
            # post. Getting out of an upside-down car outranks shooting from
            # inside one; the old shape let a task-stall `stop` AND an
            # `exit_vehicle` both go out on the same tick.
            flip = flipped_action(state)
            # T9 (findings.md R1/R5): fed every tick regardless of `flip`
            # (its own timers need to stay warm even on a tick `flip`
            # preempts), graded just below it in the ladder — a car sitting
            # in the water is a physical emergency, same tier as one upside
            # down, but "get out and swim" is strictly less urgent than "get
            # out of the thing that is currently upside down".
            water_action = self.water.check(state)
            # T9: fed every tick too, for the same reason — the two-tick
            # closing-speed derivative needs last tick's vehicle positions
            # cached even on a tick this does not fire.
            road_dodge_action = self.road_dodge.check(state)
            # T9: fed every tick, whether or not `threat.being_jacked_by` is
            # live this tick — its own grace window is measured from the LAST
            # tick it saw the handle, so a gap in feeding it would understate
            # how recently the jacking actually happened.
            recently_jacked = self.jack_handoff.feed(state)
            # One call per tick, here and nowhere else: the tracker's window is
            # keyed on wall-clock time, and feeding it twice in a tick would
            # put two samples of the same HP reading in it.
            under_attack = self.damage.feed(state)
            # Kept for `_dynamic_context`: the knowledge retrieval wants to know he is
            # being hit, and the tracker above must not be fed a second time to find out.
            self._under_attack = under_attack

            # A RUNNING task that has not moved him for 20 s has him pinned
            # (measured live: `combat_hated_targets_around` against a cat that
            # can never stop being a hated target). Checked BEFORE the threat
            # post so a stall and a threat landing on the same tick produce
            # one post, not two - and so the type that just deadlocked him is
            # already refused when `threat_action` proposes it again.
            self.cleared_backoff.feed(state)
            stalled_type = self.task_stall.feed(
                state,
                under_attack=under_attack,
                suspended=(
                    self.governor.level >= 3  # L3 is asleep in a parked car (§7)
                    or time.monotonic() < self._quiet_until  # a deliberate `wait`
                    # CONTRACTS v1.12: the interior escape has its OWN 30 s
                    # per-rung timeouts, and its whole job is to post a
                    # `walk_to` that will look like "a running task that is not
                    # moving him" for as long as the ped is picking its way
                    # through a hallway. Left running, this detector would
                    # `stop` the escape's walk from a HIGHER wheel owner and the
                    # ladder would never finish a rung.
                    or self.interior_escape.active()
                    # A blocked screen freezes `state`, so "he has not moved"
                    # is trivially true and means nothing. Exactly one of the
                    # two interventions may be live at a time, and the
                    # blocking-screen watchdog owns this one.
                    or self._screen_blocked
                ),
            )

            # The vehicle state machine. Fed EVERY tick, whoever ends up with
            # the wheel, because its phase is what the threat ladder and the
            # planners read: `vehicle_blocked` below is the only evidence there
            # is that a car with healthy bodywork will not actually move
            # (/state has no engine health, no `driveable`, no obstruction
            # flag). Fed before `threat_action` for exactly that reason.
            vehicle_intent = self.vehicle.feed(
                state,
                hold=self._vehicle_hold(state),
                style=self.mood.driving_style(),
            )
            vehicle_blocked = self.vehicle.phase in (
                VehiclePhase.BLOCKED,
                VehiclePhase.STUCK,
            )

            threat = threat_action(
                state, delta, under_attack, vehicle_blocked=vehicle_blocked,
                heat_wanted=self.roam.heat_is_the_goal(),
            )
            if threat is not None and self.task_stall.blocked(threat["type"]):
                # This exact task type just pinned him. Dropping it here (and
                # not merely at the POST) matters: `_threat_has_the_wheel`
                # below must stay honest, or the day plan and the activity
                # runner would stand down for a survival action that is never
                # going to be issued.
                threat = None
            # --- the reflex ladder, highest owner first -------------------
            # Exactly one BRIDGE TASK may leave this block, and the order of
            # these branches is the priority order (`vehicle.MOVEMENT_OWNER_
            # TABLE` is the same order written down). The wheel enforces it
            # twice over: a lower owner is refused while a higher one holds,
            # and no owner may acquire at all once a task has been posted this
            # tick.
            if flip is not None:
                # Upside down or in the water. Above survival: shooting from
                # inside an inverted car is not a plan.
                token = self.wheel.acquire("flip", f"upside down / in water: {flip['type']}")
                if token is not None:
                    self._execute_action(flip["type"], flip["params"], token)
            elif water_action is not None:
                # T9: stuck in the water past the timeout (right-way-up, or
                # `flip` would already have claimed the tick). Still a
                # physical-emergency tier, still above survival — trading
                # punches or aiming from a car going nowhere in the water is
                # not a plan either.
                token = self.wheel.acquire("water", f"in the water: {water_action['type']}")
                if token is not None:
                    self._execute_action(water_action["type"], water_action["params"], token)
            elif road_dodge_action is not None:
                # T9: on foot with a vehicle closing fast. Above the ordinary
                # threat ladder — a car is seconds from a hit, and stepping
                # off does not cost him whatever fight is also happening;
                # `threat_action` gets another look next tick with the same
                # snapshot's worth of urgency it had before.
                token = self.wheel.acquire(
                    "road_dodge", f"vehicle closing on foot: {road_dodge_action['type']}"
                )
                if token is not None:
                    self._execute_action(
                        road_dodge_action["type"], road_dodge_action["params"], token
                    )
            elif stalled_type is not None:
                # `stop` only: CONTRACTS §1's own "clear current task -> idle".
                # What to do INSTEAD is the day plan's / the activity runner's
                # / the brain's call, and they all get this same tick.
                token = self.wheel.acquire(
                    "threat", f"{stalled_type} pinned him; clearing it"
                )
                if token is not None:
                    self._execute_action("stop", {}, token)
            elif threat is not None:
                # Acquired even when ThreatLatch suppresses the POST: the latch
                # only ever suppresses because the engine is ALREADY running
                # exactly this task, so survival really does have the wheel and
                # the planners below must stand down for it.
                token = self.wheel.acquire("threat", f"survival: {threat['type']}")
                if token is not None and self.threat_latch.should_issue(threat, state):
                    # ThreatLatch, not a fresh post per tick: every POST /task
                    # preempts the running task (CONTRACTS §1), so re-issuing
                    # the same combat order at 3 Hz restarted the engine's aim
                    # cycle three times a second — observed on stream as
                    # stuttering movement and shots that never landed.
                    task_id = self._execute_action(threat["type"], threat["params"], token)
                    if task_id is not None:
                        # Only a post that actually reached the game starts the
                        # hold-down. A task suppressed mid-cutscene or lost to a
                        # bridge blip never happened, and he must be free to ask
                        # again on the next tick.
                        self.threat_latch.issued(threat)

            # The vehicle reflex: drive away the moment he is seated with
            # nobody steering, and grade whether a drive order actually moved
            # the world. Strictly below survival — the wheel refuses it if the
            # threat ladder already took this tick — and strictly above every
            # planner, which is what stops "he got in and sat there" from
            # depending on a model call that costs seconds and money. Its
            # recovery ladder mixes tasks and keypresses, so it goes through
            # the same helper the whole harness uses.
            if vehicle_intent is not None:
                task_id, attempted = self._reflex_act(
                    "vehicle",
                    vehicle_intent.action,
                    f"{vehicle_intent.kind}: {vehicle_intent.reason}",
                )
                if attempted:
                    self.vehicle.bind_task(task_id)

            stuck_action = self.stuck.check(state)
            if stuck_action == "reverse_out" and self.primitives is not None:
                # Through the single choke point rather than straight at
                # SendInput, because `_execute_action` is where every other
                # refusal (cutscene, dead, blocking screen) already lives. It
                # takes NO wheel token: a keypress posts no task and preempts
                # nothing, which is precisely why the stuck ladder starts with
                # one — gating it here would re-open the starvation bug where
                # a firefight stopped the unstick ladder from ever running.
                self.wheel.note("physical", "stuck: reverse_out")
                self._execute_action("reverse_out", {"ms": 1400})
            elif stuck_action == "unstick":
                self.wheel.note("physical", "stuck: unstick nudge")
                moved = self.stuck.try_unstick(self.bridge)
                if moved is not None:
                    self.writer.record_event(
                        "unstick",
                        {
                            "distance_m": moved,
                            "stuck_for_s": state.vehicle.stopped_for_s if state.vehicle else 0.0,
                        },
                    )
                    self._say("That was a legal nudge. Three meters. Judges allow it.")

            if threat is None and vehicle_intent is None:
                # Free-roam reflexes only when nothing is shooting at him:
                # these DO post tasks, so running them under fire would
                # preempt the survival action that was just issued.
                #
                # Stranded on foot with no task: widen the vehicle search,
                # then give up and let the brain be creative about it. NOT
                # while a mission is active (cutscene or objective phase):
                # this is what used to widen a "find a vehicle" search (50m ->
                # 90m -> 140m) while the agent stood inside a scripted cutscene —
                # free-roam reflexes must not compete with
                # `_drive_mission_objective`, which owns getting him a car for
                # a mission on its own terms.
                # CONTRACTS v1.12 — THE GROUND-TRUTH INTERIOR ESCAPE, and the
                # reason this whole fix exists. It sits ABOVE the mission-block
                # gate below on purpose: while he is inside a building the day
                # plan's walk to a mission-start marker cannot possibly complete
                # either (the nav mesh is disconnected by doors), so standing
                # down for it would reproduce the original failure with extra
                # steps. `InteriorEscape.applies()` does its own gating on
                # `mission.active`, cutscenes, switches and control — missions
                # happen indoors on purpose and are never escaped from.
                escaping = self.interior_escape.applies(state) and self._run_interior_escape(
                    state
                )
                if escaping:
                    # He is being walked out of a building. Nothing below this
                    # can help and every one of them would be refused anyway.
                    self.stranded.reset()
                elif self.missions.in_mission or self.planner.in_mission_block:
                    # Same rule extended to the day plan's own mission block:
                    # while the planner is walking him into a start marker it
                    # owns getting him a car, and a second `enter_nearest_vehicle`
                    # from here would preempt the first one every tick.
                    self.stranded.reset()
                elif self.roam.current is not None:
                    # ...and extended again to a LIVE ROAM GOAL, for the same
                    # reason and after watching it happen on stream: `stranded`
                    # is reflex-class, so it outranked roam and preempted every
                    # goal within two seconds of it being picked —
                    #     roam goal picked   goal=roam_the_block
                    #     wheel preempted    owner=roam by=stranded
                    #     roam goal ended    outcome=preempted duration_s=0.3
                    # — including `roam_the_block`, whose own plan IS
                    # `enter_nearest_vehicle`. It was preempting a goal in order
                    # to do the thing that goal was already doing, forever.
                    # A man walking to a fight is not stranded. The goal owns
                    # getting him there, and it has its own stuck watchdog
                    # (GOAL_STUCK_S, two strikes) for when it genuinely cannot.
                    self.stranded.reset()
                elif recently_jacked:
                    # T9 (findings.md R1/R5): he was just pulled out of his
                    # own car (`threat.being_jacked_by`, fought off a few
                    # ticks ago by the threat ladder above). `_drive_activities`
                    # runs right after this method on this SAME snapshot and
                    # is where roam's own `take_my_car_back` actually gets
                    # picked (`RoamView.stolen_from` / the `take_my_car_back`
                    # trigger in behavior/roam.py) — standing `stranded` down
                    # for `JACK_HANDOFF_GRACE_S` is what stops it from
                    # widening a vehicle search for whatever "any" car is
                    # nearest and grabbing a stranger's car instead of his own
                    # in that gap.
                    self.stranded.reset()
                else:
                    # BEFORE the stranded ladder, and it resets it: from inside
                    # a building, widening a vehicle search (50 -> 90 -> 140 m)
                    # picks a car that is further away and behind MORE walls,
                    # which is worse than doing nothing. `walk_to` is the only
                    # action that paths through a door (it is
                    # TASK_FOLLOW_NAV_MESH_TO_COORD and interiors are
                    # nav-meshed), so getting out is a walk, in breadcrumbs.
                    escape = self.house_escape.check(state, self.roam.still_for_s())
                    if escape is not None:
                        self.stranded.reset()
                        self._reflex_act("house_escape", escape, "apparently indoors")
                    else:
                        strand = self.stranded.check(state)
                        if strand is not None:
                            self._reflex_act("stranded", strand, "on foot with no car")
                # Governor L2: reflex drives — keep a wander task alive with
                # mood style. The activity runner is also reflex-layer
                # behaviour and outranks this; posting a wander on top of a
                # running activity step would preempt it. L2 ONLY: at L3 he
                # is asleep in the car (§7), and a wander posted here would
                # drive straight out of the scenic parking spot L3 just took
                # him to — which is how L3 ended up meaning nothing at all.
                # Also never while a mission is active: mission intent
                # outranks free-roam intent (`_drive_mission_objective` is
                # what should be steering).
                if (
                    2 <= self.governor.level < 3
                    and self.activity_runner.current is None
                    and not self.missions.in_mission
                    and not self.planner.in_mission_block
                    and state.player.in_vehicle
                    and state.last_task.status in ("idle", "done", "failed")
                ):
                    self._reflex_act(
                        "governor",
                        {"type": "wander_drive", "params": {"style": self.mood.driving_style()}},
                        "L2: reflex drives",
                    )

            # F6, the LAST rung: control came back, three seconds went by, and
            # not one layer above posted a movement task. Outside the
            # `threat is None and vehicle_intent is None` block above on
            # purpose — the wheel is the gate, not an `if`: `resume` sits at the
            # bottom of the reflex class, so a live survival or vehicle rung
            # refuses it, and a task already posted this tick refuses it too.
            # Asking and being refused costs one log line and is the honest
            # shape; deciding for the wheel here is how ladders drift apart.
            overdue_edge = self.control_regained.overdue()
            if overdue_edge is not None:
                fallback = resume_action(state, self.mood.driving_style())
                if fallback is None:
                    # Indoors, or in a car that cannot go anywhere. Both have a
                    # higher owner whose whole job this is (`exit_interior`,
                    # `flip`); inventing a second answer here is what put a
                    # widening vehicle search inside a living room.
                    log.info(
                        "f6: nothing moved him and no fallback applies here",
                        extra={
                            "kv": {
                                "edge": overdue_edge,
                                "interior": state.player.interior,
                                "in_vehicle": state.player.in_vehicle,
                            }
                        },
                    )
                else:
                    self._reflex_act(
                        "resume",
                        fallback,
                        f"F6: {overdue_edge} and nobody moved him in "
                        f"{self.control_regained.deadline_s:.0f}s",
                    )

        # CONTRACTS v1.13 — the phone, LAST in the tick on purpose (see
        # `_phone_reflex`). It runs even while he is dead or arrested: the only
        # thing it does in that state is notice the ring ending and re-arm,
        # because `_execute_action` refuses the post anyway.
        phone_acted = self._phone_reflex(state)

        # The arbiter's answer, published under the name the three planners
        # already read. It means "a REFLEX acted this tick" — either it holds
        # the wheel or it fired a keypress recovery that `state.last_task`
        # cannot show yet — and a day-plan or mission trip posted over the top
        # would put him straight back in the parked convertible.
        #
        # `phone_acted` is ORed in rather than routed through `wheel.note()`:
        # the phone is not a movement owner and must not become one, but a
        # `reject_call` posted milliseconds ago is a real task occupying the
        # bridge's single slot, and a planner posting navigation over it this
        # tick would cancel the refusal before it landed.
        self._threat_has_the_wheel = self.wheel.taken_by_reflex() or phone_acted

    def _phone_reflex(self, state: GameState) -> bool:
        """T8 (findings.md R6): answer every ring; hang up on a budget while missions are off.

        THE POLICY, and it is the operator's: "it cant cut the call, check or
        accept whatever" plus "calls are entertaining and can start story".
        So the phone is always ANSWERED — `answer_call` (`Control.PhoneSelect`)
        — whether or not `Settings.missions_enabled` is true:

        * **Jobs on.** Answering may start a mission; that is the point, and
          nothing here ever hangs up on him once connected.
        * **Jobs off.** He is not taking jobs, but the call is still answered
          (it is entertaining) — then hung up on its own after
          :data:`PHONE_HANGUP_AFTER_S`, or IMMEDIATELY if a fight or a police
          chase is live (`threat.attacker_handle`, `player.wanted > 0`, or the
          wheel already running one of :data:`FIGHT_ACTION_TYPES`) — a phone
          conversation is not the bit while he is being shot at.

        RATE LIMIT: one attempt to ANSWER per ring, and (jobs off only) one
        attempt to HANG UP per connected call — the same shape `ThreatLatch`
        uses for combat and the pre-T8 reflex used for `reject_call`. `POST
        /task` preempts the running task (CONTRACTS §1), so re-posting either
        verb at the poll rate would tear down and restart the engine's own
        control-injection loop several times a second. The bridge's own
        `answer_call`/`reject_call` already re-inject the control every frame
        for up to ~6 s on their own, so one post per ring or per call IS the
        whole attempt — CONTRACTS's own "unanswered"/"unrejectable" after that
        bound is the honest end state, never a loop.

        Each latch is only spent on a post that actually reached the game. A
        task suppressed mid-cutscene or lost to a bridge blip never happened,
        and the next tick must be free to try again.

        Returns True when it posted, which is what makes the planners running
        later in this tick stand down for it.
        """
        phone = state.phone
        if phone.in_call:
            # Connected — re-arm the ANSWER latch for the next ring regardless
            # of the missions switch, so a second call right after this one
            # gets its own attempt.
            self._phone_answered_this_ring = False
            if self.settings.missions_enabled:
                # Jobs on: the story owns this call. Nothing here ends it.
                self._phone_call_connected_at = None
                self._phone_hung_up_this_call = False
                return False
            if self._phone_call_connected_at is None:
                self._phone_call_connected_at = time.monotonic()
            if self._phone_hung_up_this_call:
                return False
            fighting = (
                state.threat.attacker_handle is not None
                or state.player.wanted > 0
                or (
                    state.last_task.status == "running"
                    and state.last_task.type in FIGHT_ACTION_TYPES
                )
            )
            elapsed = time.monotonic() - self._phone_call_connected_at
            if not fighting and elapsed < self.phone_hangup_after_s:
                return False
            if self.wheel.posted_this_tick:
                # Bridge-side it is still one task at a time; the call stays
                # connected next tick and this latch is untouched, so the
                # hang-up is simply retried on the next tick that is free.
                log.debug(
                    "phone: connected call due for a hang-up, deferring — a task "
                    "is already posted this tick"
                )
                return False
            task_id = self._execute_action("reject_call", {})
            if task_id is None:
                return False
            self._phone_hung_up_this_call = True
            log.info(
                "phone: hanging up the connected call",
                extra={
                    "kv": {
                        "task_id": task_id,
                        "reason": "fight_or_chase" if fighting else "budget",
                        "elapsed_s": round(elapsed, 1),
                    }
                },
            )
            return True

        # Not connected — forget the hang-up bookkeeping for the next call.
        self._phone_call_connected_at = None
        self._phone_hung_up_this_call = False

        if not phone.ringing:
            # The ring is over — answered, or the caller gave up. Re-arm.
            self._phone_answered_this_ring = False
            return False
        if self._phone_answered_this_ring:
            return False
        if self.wheel.posted_this_tick:
            # The survival ladder (or the vehicle reflex) already put a task
            # on the wire this tick. `answer_call` takes no wheel, so nothing
            # would refuse it — but bridge-side it is still one task at a
            # time, and posting now would preempt the action that was chosen
            # over it. The phone is still ringing next tick; the latch is
            # untouched.
            log.debug(
                "phone: ringing, deferring the answer — a task is already posted this tick"
            )
            return False
        task_id = self._execute_action("answer_call", {})
        if task_id is None:
            return False
        self._phone_answered_this_ring = True
        log.info(
            "phone: ringing — answering (calls are entertaining and can start story)",
            extra={"kv": {"task_id": task_id, "tick": self.wheel.tick}},
        )
        # No canned line here on purpose. `_phone_line` puts the FACT in the
        # brain's next context and he narrates it in his own words on his own
        # cadence; a fixed string fired from the reflex would be the same
        # sentence every single call.
        return True

    # -- movement arbitration --------------------------------------------------

    def _game_owns_controls(self, state: GameState) -> bool:
        """True when nothing this harness posts can reach the player.

        Exactly the set `_execute_action` refuses bridge tasks for, read off
        THIS tick's snapshot rather than the cached per-tick flags, because
        `_reflex` runs before some of them are set on the very first tick.
        """
        return (
            state.mission.cutscene_active
            or state.player.dead
            or state.player.arrested
            or state.player.switch_in_progress
            or state.mission.retry_in_flight
            or not state.player.control_enabled
        )

    def _game_control_reason(self, state: GameState) -> str:
        if state.mission.cutscene_active:
            return "cutscene playing"
        if state.player.dead or state.player.arrested:
            return "player down (dead/arrested)"
        if state.player.switch_in_progress:
            return "protagonist switch in progress"
        if state.mission.retry_in_flight:
            return "mission retry/checkpoint reload in progress"
        return "control_enabled is false"

    def _reflex_act(
        self, owner: str, action: dict[str, Any], reason: str
    ) -> tuple[str | None, bool]:
        """Run one reflex-layer action under `owner`. Returns `(task_id, attempted)`.

        The reflex ladder mixes MOVEMENT tasks (which preempt whatever is
        running, and therefore need the wheel) with §2 primitives and v1.13's
        two phone verbs (which move nobody, and therefore do not).
        `attempted` is False only when the wheel REFUSED — the caller must then
        do nothing at all with the result, not even bind a null task id.
        """
        action_type = action["type"]
        if action_type not in MOVEMENT_TASKS:
            self.wheel.note(owner, f"{action_type}: {reason}")
            return self._execute_action(action_type, action["params"]), True
        token = self.wheel.acquire(owner, f"{action_type}: {reason}")
        if token is None:
            return None, False
        return self._execute_action(action_type, action["params"], token), True

    def _roam_preempted(self, by: str, reason: str) -> None:
        """Free roam lost the wheel. The goal it was running is over, for real.

        This is the operator's rule: "when reflex or mission acquires over
        roam, the wheel cancels roam's active task, clears `roam.current`, and
        emits roam_goal_failed{reason: preempted, by: <owner>}". Cancelling the
        bookkeeping is what makes it real — leaving `roam.current` locked while
        somebody else drives is precisely the state that produced "follow Lamar
        while standing next to the objective car".
        """
        self._roam_token = None
        if self.roam.current is None and self.activity_runner.current is None:
            return
        log.info(
            "roam goal preempted",
            extra={
                "kv": {
                    "goal": None if self.roam.current is None else self.roam.current.goal.id,
                    "by": by,
                    "reason": reason,
                }
            },
        )
        self._end_activity_if_running(ROAM_PREEMPTED, by=by)

    def _mission_preempted(self, by: str, reason: str) -> None:
        self._mission_token = None
        # The follower must forget the task it was watching: it is no longer
        # the task the engine is running, and its own "someone else has the
        # wheel" check reads `state.last_task`, which is a tick behind.
        self.mission_follower.bind_task(None)

    def _day_plan_preempted(self, by: str, reason: str) -> None:
        self._day_plan_token = None
        self.planner.bind_task(None)

    def _governor_preempted(self, by: str, reason: str) -> None:
        self._governor_token = None
        self._park_task_id = None
        self._park_deadline = 0.0

    def _interior_preempted(self, by: str, reason: str) -> None:
        """Something above the escape took the wheel (a firefight, a mission).

        The ladder is NOT abandoned — he is still in the building and
        `player.interior` will still say so on the next tick — but the rung it
        had running has been preempted by definition (CONTRACTS §1: a new task
        preempts the old one), so its 30 s clock is meaningless and it asks for
        the wheel again from the same rung once the higher owner is done.
        """
        self._interior_token = None
        self.interior_escape.retry_step()
        log.info(
            "exit_interior preempted",
            extra={
                "kv": {
                    "interior_id": self.interior_escape.interior_id,
                    "by": by,
                    "reason": reason,
                }
            },
        )

    def _run_interior_escape(self, state: GameState) -> bool:
        """CONTRACTS v1.12: walk him out of the interior he is standing in.

        Returns True while the escape owns this tick (it holds the wheel and
        the layers below must stand down), False once it has given up or was
        refused the wheel by a higher owner.

        THE POINT OF THIS METHOD is that it is REACHED. The previous version of
        this behaviour (`HouseEscape`) was real code with real tests that the
        live loop essentially never called, because the only way in was a
        heuristic needing 20 s of stillness AND a failed `enter_nearest_vehicle`
        on the same snapshot. `interior_detected` below is logged at the moment
        of detection in the LIVE tick precisely so that its presence in a
        session log is proof of reachability rather than an argument about it.
        """
        interior = state.player.interior
        assert interior is not None  # applies() is the caller's guard

        # True the first tick the ladder engages for THIS interior id — including
        # walking straight from one interior into another, which is a new problem
        # with a new way out.
        first = self.interior_escape.interior_id != interior.id
        action = self.interior_escape.check(state)
        if not self.interior_escape.active():
            # It gave up (or stood itself down). Hand the wheel back rather than
            # sitting on it: the rest of the loop still has a show to run.
            self._release_interior_wheel("escape exhausted")
            return False
        if first:
            log.warning(
                "interior_detected",
                extra={
                    "kv": {
                        "id": interior.id,
                        "since_s": round(interior.since_s, 1),
                        "has_last_outdoor": state.player.last_outdoor is not None,
                        "protagonist": state.player.protagonist,
                    }
                },
            )

        # An OPEN lease, renewed every tick: the ladder spans many ticks and its
        # rungs have their own timeouts, and holding it is what makes free roam
        # stand down (`_drive_activities` returns on `wheel.taken_by_reflex()`)
        # instead of locking a goal it cannot walk to from a living room.
        token = self.wheel.acquire(
            "exit_interior", f"exit_interior: interior {interior.id}", lease_ticks=None
        )
        if token is None:
            # Outranked — a firefight, or the game took the controls. The rung
            # keeps its place; it asks again next tick.
            self._interior_token = None
            return False
        self._interior_token = token
        if action is None:
            return True  # a rung is running; let it have its 30 s
        log.info(
            "exit_interior step",
            extra={
                "kv": {
                    "step": self.interior_escape.step,
                    "interior_id": interior.id,
                    "target": [round(action["params"][k], 1) for k in ("x", "y", "z")],
                }
            },
        )
        task_id = self._execute_action(action["type"], action["params"], token)
        if task_id is None:
            # Refused downstream (a modal screen, a blocked task type). The rung
            # never happened, so it does not burn its timeout.
            self.interior_escape.retry_step()
        else:
            self.interior_escape.posted(task_id)
        return True

    def _release_interior_wheel(self, why: str) -> None:
        """Hand the wheel back, so normal selection resumes on this same tick."""
        if self._interior_token is None:
            return
        next_up = self.wheel.release(self._interior_token)
        self._interior_token = None
        log.info(
            "exit_interior released the wheel",
            extra={"kv": {"why": why, "next_in_line": next_up}},
        )

    def _vehicle_hold(self, state: GameState) -> str | None:
        """Why sitting still in a car is CORRECT right now — or None.

        The vehicle reflex is deliberately aggressive: it starts him moving
        within a couple of ticks of reaching a driver's seat. These are the
        cases where that would be wrong, and every one of them is a real
        condition rather than a preference. The value is the reason, and it is
        logged on the phase transition so "why did he sit there" has an answer
        in the log.

        Not in this list, and deliberately: `mission.active`. During a mission
        the drive-away half already stands down inside
        :class:`behavior.vehicle.VehicleController` (mission navigation belongs
        to `MissionFollower`), but the motion WATCHDOG must keep running — it
        only ever grades a drive task that is already running, which is exactly
        the "the mission told him to drive and the car never moved" case.
        """
        if state.mission.cutscene_active:
            # The game owns control through a cutscene and discards ped tasks;
            # `_execute_action` refuses them anyway. Posting here would also
            # burn the drive-away hold-down on something that never happened.
            return "cutscene"
        if state.player.switch_in_progress:
            # v1.11: a protagonist switch (the aerial fly-over) is playing.
            # Same story as a cutscene — the game owns the camera and the
            # controls, and this is the real signal behind narrating "wrong
            # body" instead of just sitting through it quietly.
            return "switch_in_progress"
        if state.mission.retry_in_flight:
            # v1.11: the game's own retry/checkpoint-reload script thread is
            # running. Posting here would burn the drive-away hold-down on a
            # world that is about to be replaced by the checkpoint anyway.
            return "retry_in_flight"
        if not state.player.control_enabled:
            # The game has taken the controls for a scripted beat. Nothing
            # posted now reaches the player, and pretending otherwise is how a
            # reflex ends up "working" against a screen nobody is driving.
            return "control_disabled"
        if state.player.dead or state.player.arrested:
            # Nothing productive a corpse or a cuffed man can do with a car
            # (CLAUDE.md rule 5: recovery means waiting for the game's own
            # respawn).
            return "player_down"
        if self._screen_blocked:
            # The script thread is not ticking, so "stationary" is a frozen lie
            # and no command would be applied.
            return "blocking_screen"
        if time.monotonic() < self._quiet_until:
            # A deliberate `wait` from the brain. Sitting still IS the action;
            # overriding it would make the brain's own choice meaningless.
            return "deliberate_wait"
        if self.governor.level >= 3:
            # CONTRACTS §7 L3 is "asleep in the car, parked somewhere scenic".
            # Driving out of the parking spot is precisely how L3 came to mean
            # nothing before.
            return "governor_l3"
        if self.breaks.on_break:
            # The humanizer's break: he is away from the wheel on purpose.
            return "on_break"
        if self.activity_runner.current is not None and not self.activity_runner.current.finished:
            # An activity is mid-plan and `_drive_activities` posts its next
            # step later in THIS tick. Several plans are deliberately still for
            # a beat — `park_and_watch` is drive_to → stop → look_around →
            # wait 25 — and the gap between `stop` completing and the `wait`
            # that follows it is exactly the shape the drive-away fires on. An
            # activity that has finished is NOT a hold: `steal_nicer_car`'s
            # whole plan is one `enter_nearest_vehicle`, and driving away from
            # the moment it declares victory is the entire point of this work.
            return "activity_step"
        return None

    # -- decisions -------------------------------------------------------------

    def _read_mission_title(self, jpeg: bytes) -> str | None:
        """Seam for tests; production reads the title with one Haiku vision call."""
        return read_mission_title(
            self.anthropic, self.pricing.tactical.id, jpeg, self._record_api_usage
        )

    def _read_mission_outcome(self, jpeg: bytes) -> MissionOutcome:
        """Seam for tests; production reads PASSED/FAILED/UNKNOWN (+ the screen's own reason
        line) with one Haiku vision call. Called at most once per mission END — as rare as a
        mission start — never on a tick timer; see brain/vision.py's own cost note."""
        return read_mission_outcome(
            self.anthropic, self.pricing.tactical.id, jpeg, self._record_api_usage
        )

    def _handle_mission_events(self, state: GameState, delta: Delta) -> None:
        """Turn MissionTracker phase transitions into §4 events, and arm the director's
        vision trigger for the ones the contract says carry a screenshot. Extracted from
        `_reflex` so the wiring is unit-testable with a bare Harness."""
        for ev in self.missions.feed(
            state.mission.active,
            state.mission.cutscene_active,
            state.mission.random_event_active,
            delta,
            state.player.dead,
            state.player.arrested,
        ):
            url = None
            if ev.type == "mission_start":
                # CONTRACTS v1.3: mission_start carries a screenshot. This is the ONE moment the
                # game draws the objective on screen; /state has no mission name and no objective
                # text (the field does not exist), so the screen is the only honest source of
                # "what does this job want". Captured now for the event row AND armed as the
                # director's vision trigger so its next reaction call can actually read it.
                # Before this, VISION_TRIGGERS listed mission_start but nothing ever set it, so
                # v1.3 bought nothing (found by the dossier.txt dossier, 13.8).
                jpeg, url = self._capture_screenshot("mission_start")
                self._pending_screenshot_trigger = "mission_start"
                # `missions.started_at` (§5). Stamped here rather than left to
                # the database's now(), so a row written or replayed later still
                # says when the job actually began.
                self._mission_started_iso = datetime.now(UTC).isoformat()
                self._mission_tokens = 0
                # Mission knowledge: /state has no mission name, so read the title the game
                # prints on screen (one cheap vision call), fall back to the zone, and hand the
                # brain that mission's walkthrough card via _dynamic_context. None = no card.
                title = self._read_mission_title(jpeg) if jpeg else None
                self.current_mission, source = identify_mission_with_source(
                    title, state.location.zone,
                    script=state.mission.script, learned=self.learned_scripts.mapping,
                )
                if source == "title" and state.mission.script and self.current_mission:
                    # Learn from the screen-read title ONLY — a zone fallback can
                    # in general name more than one mission, and a "script" hit is
                    # already-learned data, not new evidence (LearnedScripts.learn
                    # itself refuses to overwrite an existing pairing either way).
                    self.learned_scripts.learn(state.mission.script, self.current_mission["name"])
                log.info(
                    "mission identified" if self.current_mission else "mission not identified",
                    extra={"kv": {"title_read": title, "zone": state.location.zone,
                                  "script": state.mission.script, "source": source,
                                  "mission": (self.current_mission or {}).get("name")}},
                )
            if ev.type == "mission_fail":
                # CONTRACTS §4: mission_fail carries a screenshot, same as
                # death/busted — captured now (the moment of failure), not
                # confused with the SEPARATE vision-trigger screenshot the
                # director's own reaction call takes later, after he is back
                # up (`_pending_screenshot_trigger` below). Clearing the
                # actual blocking screen this usually coincides with is
                # `_blocking_screen_watchdog`'s job (wired in `run()`), not
                # this loop's — see DeathArrestRecovery's docstring for why.
                _, url = self._capture_screenshot("mission_fail")
                self._pending_screenshot_trigger = "mission_fail"
            if ev.type in ("mission_end", "mission_fail"):
                self._write_mission_row(ev.type, ev.payload)
                self.current_mission = None
            # The day planner counts attempts and failures off these same
            # events (back off after MAX_CONSECUTIVE_MISSION_FAILS, owe a roam
            # block after any mission ends) — fed here so there is exactly one
            # place mission events are interpreted.
            self.planner.observe_mission_event(ev.type)
            # The roam engine's "the story has to move" clock is the same fact
            # from the other side: a started mission resets both the completed-
            # goal counter and the fifteen-minute deadline, and a finished one
            # restarts the roam clock so the next deadline is measured from when
            # free roam actually resumed rather than from the last job.
            if ev.type == "mission_start":
                self.roam.note_mission_started()
            elif ev.type in ("mission_end", "mission_fail"):
                self.roam.note_roam_resumed()
            self.writer.record_event(ev.type, ev.payload, screenshot_url=url)
            self._pending_big_event = ev.type

        if getattr(self.missions, "pending_outcome_read", False):
            # The one ending `MissionTracker.feed()` cannot call from flags
            # alone: the mission ended with the player neither dead nor
            # arrested — the most common failure in the whole game (a follow
            # target driving away: "MISSION FAILED / Franklin lost Lamar")
            # looks exactly like this, and used to go entirely unreported.
            # Reuse the SAME screenshot path mission_start already uses (one
            # dxcam grab already happened this tick — see `_capture_screenshot`'s
            # own "never grabs, never blocks" rule) and read PASSED/FAILED/
            # UNKNOWN off it. This is one vision call per mission END, exactly
            # as rare as mission_start's own call — bounded the same way v1.3
            # bounded that one; it must never run on a tick timer.
            jpeg, url = self._capture_screenshot("mission_end")
            result = self._read_mission_outcome(jpeg) if jpeg else MissionOutcome("unknown", None)
            log.info(
                "mission-end screen read",
                extra={"kv": {"outcome": result.outcome, "reason_text": result.reason_text}},
            )
            resolved = self.missions.resolve_outcome(result.outcome, result.reason_text)
            if resolved is not None:
                if resolved.type == "mission_fail":
                    self._pending_screenshot_trigger = "mission_fail"
                else:
                    # A CONFIRMED pass, from the screen itself — the only place
                    # `missions_passed` may move (CLAUDE.md rule 1: never
                    # guessed, never incremented on anything but this read).
                    # The site's live counters and its opengraph image both
                    # read this same counter.
                    self.counters["missions_passed"] = self.totals.bump("missions_passed")
                    self.bus.publish("counters", dict(self.counters))
                    self._pending_screenshot_trigger = "mission_end"
                # Same clearing the fast dead/arrested path already does for
                # mission_end/mission_fail: the walkthrough card no longer
                # applies once the job is over. `mission_follower` needs no
                # explicit reset here — `MissionFollower.plan()` already
                # resets itself the instant `state.mission.active` reads
                # False, which it already does on THIS tick's state (mission_
                # ended IS that transition). `current_goal` is deliberately
                # left alone, matching the existing dead/arrested path below —
                # only the respawn path resets it, because only a respawn
                # leaves a genuinely stale pre-death goal behind.
                self._write_mission_row(resolved.type, resolved.payload)
                self.current_mission = None
                self.planner.observe_mission_event(resolved.type)
                self.writer.record_event(resolved.type, resolved.payload, screenshot_url=url)
                self._pending_big_event = resolved.type

    def _write_mission_row(self, event_type: str, payload: dict[str, Any]) -> None:
        """One `missions` row per mission that ended with a KNOWN outcome (§5).

        Nothing in this package ever wrote this table, so /missions could only
        ever be empty while promising "every story mission the agent has attempted,
        passed, or fumbled". Every field here is an observation, not an
        inference: the outcome is the one `_handle_mission_events` already
        reported to the events feed (a MISSION PASSED/FAILED banner read off the
        screen, or the game's own dead/arrested flags), `tokens` is the real
        decision-token spend inside the mission, and `summary` is the game's own
        reason line when it printed one. A mission that ended with an
        unreadable screen gets NO row — the same rule the counter follows,
        because "ended somehow" is not an outcome anyone can publish.
        """
        if self._mission_started_iso is None:
            # No observed start (the harness or the game restarted mid-mission):
            # a row with a guessed started_at would put a false duration on the
            # page, so nothing is written.
            log.info("mission row skipped: this mission's start was never observed")
            return
        name = (self.current_mission or {}).get("name") or str(payload.get("name") or "unknown")
        row: dict[str, Any] = {
            "name": name,
            "started_at": self._mission_started_iso,
            "ended_at": datetime.now(UTC).isoformat(),
            "outcome": "passed" if event_type == "mission_end" else "failed",
            "attempts": int(payload.get("attempts") or payload.get("attempt") or 1),
            "deaths": int(payload.get("deaths", self.missions.deaths_this_mission) or 0),
            "tokens": self._mission_tokens,
        }
        reason = payload.get("reason_text")
        if reason:
            row["summary"] = str(reason)
        self.writer.insert_mission(row)
        self._mission_started_iso = None
        self._mission_tokens = 0

    def _dynamic_context(self, state: GameState, delta: Delta, trigger: str, layer: str) -> str:
        # T3: `note()` DRAINS the pending transition line (RoamEngine.note's own
        # docstring), so it is read exactly once here and the result reused
        # below rather than calling `note()` twice. The prefix is the only
        # signal `RoamEngine.pick`/`close` leave behind for "a goal was just
        # decided this tick" — see `_tick_event_reason`, which `_apply_decision`
        # (called right after this, same tick, single-threaded) consults via
        # `self._tick_roam_transition`.
        roam_note = self.roam.note()
        self._tick_roam_transition = roam_note.startswith(
            ("ROAM GOAL PICKED:", "ROAM GOAL DONE:", "ROAM GOAL FAILED:")
        )
        parts = [
            f"TRIGGER: {trigger}",
            f"GOAL: {self.goal_text}",
            f"MOOD (tracker): {self.mood.mood}, held {self.mood.held_for_s():.0f}s",
            f"GOVERNOR: L{self.governor.level} ({LEVEL_NOTES[self.governor.level]})",
            (
                "BLOCKED: the game is on a modal screen - a mission has probably "
                "failed and the retry prompt is up. The world snapshot below is "
                "FROZEN and no longer true: do not narrate the world, you cannot "
                "see it. Say something about being stuck on a menu, or say "
                "nothing much at all."
                if self._screen_blocked
                else ""
            ),
            _threat_and_blips_line(state),
            # CONTRACTS v1.13. Empty (and therefore dropped by the join below)
            # whenever the phone is quiet, which is nearly always — this rides
            # in the UNCACHED dynamic half and costs tokens on every call.
            _phone_line(state, self.settings.missions_enabled),
            self.missions.brain_note(),
            self.mission_follower.note(),
            self.planner.note(),
            # `roam.note()` replaces `activity_runner.note()` in this slot: the
            # runner knows which STEP is running, the roam engine knows which
            # GOAL is locked, how long it has, and — when nothing is locked — the
            # menu the model must pick from. Deliberately in the DYNAMIC half:
            # the menu changes every tick, so caching it would invalidate the
            # prefix cache on every call (the same reasoning as the knowledge
            # block below).
            roam_note,
            (mission_card(self.current_mission) if self.current_mission else ""),
            # Retrieved GTA V knowledge for THIS situation, not the whole encyclopedia:
            # 651 researched items live on disk and `select` returns the handful that match
            # the current state (wanted level, hostiles, vehicle type, mission phase). Kept
            # in the DYNAMIC half deliberately - it changes every tick, so putting it in the
            # cached prefix would invalidate the cache on every call.
            render(
                select(
                    state,
                    mission=self.current_mission,
                    damage_taken=self._under_attack,
                    budget_chars=KNOWLEDGE_BUDGET_CHARS,
                ),
                budget_chars=KNOWLEDGE_BUDGET_CHARS,
            ),
            (
                mission_state_hint((self.current_mission or {}).get("name"), state) or ""
                if self.current_mission
                else ""
            ),
            (
                # Missions are switched off (Settings.missions_enabled). The planner and the
                # roam engine already refuse to start one — but the BRAIN could still set
                # "walk to Franklin's marker and start the job" as its own goal, because it
                # saw `mission.starts[]` and the director prompt says "roam, then work".
                # Watched on the dashboard 2026-09-03. So: say it plainly, and do not show
                # him the markers at all.
                "MISSIONS ARE OFF (operator setting): do not walk to a job marker, do not set "
                "a goal about starting a job, do not answer story calls. Free roam is the "
                "whole show right now. Pick from ROAM AVAILABLE."
                if not self.settings.missions_enabled
                else ""
            ),
            "STATE: " + (
                state.model_dump_json(by_alias=True)
                if self.settings.missions_enabled
                else state.model_copy(update={"mission": state.mission.model_copy(update={"starts": []})}).model_dump_json(by_alias=True)
            ),
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
        return "\n\n".join(p for p in parts if p)

    def _validation_context(self, state: GameState) -> DecisionValidationContext:
        """T4 (findings.md R4): the plain-data snapshot `validate_decision_content`
        checks a decision against, gathered fresh every call — state, the roam
        menu and the commentary's recent-line window all change every tick.

        `roam_offered_ids`/`roam_model_choice` are populated only while
        `self.roam.current is None`: exactly the condition under which
        `RoamEngine.note()` shows a "ROAM AVAILABLE" menu rather than "ROAM
        CURRENT" (a locked goal is echoed, not chosen, and validating an echo
        against the menu that was on offer BEFORE it locked would be checking
        the wrong thing).
        """
        mission_name = (self.current_mission or {}).get("name") if self.current_mission else None
        roam_offered = () if self.roam.current is not None else self.roam.offered_ids()
        return DecisionValidationContext(
            present_names=frozenset(present_names(state)),
            names_check_suspended=has_unidentified_friendly(state),
            mission_name=mission_name,
            known_mission_names=_KNOWN_MISSION_NAMES,
            recent_lines=tuple(self.commentary.recent.recent(DEDUPE_RECENT_WINDOW) or ()),
            banned_phrases=banned_phrases(),
            roam_offered_ids=tuple(roam_offered),
            roam_model_choice=self.roam.model_choice if roam_offered else None,
        )

    def _think(self, layer: str, state: GameState, delta: Delta, trigger: str) -> DecisionResult | None:
        """One decision with the timescale dip; None when the call failed."""
        if not self.api_backoff.ready():
            return None
        dipped = False
        try:
            if THINKING_TIMESCALE < NORMAL_TIMESCALE:
                # Only ever entered if someone deliberately re-enables the dip.
                # At the shipped value (1.0) the world simply keeps running at
                # normal speed while he thinks, exactly as it does for a human
                # player, and POST /timescale is never called — which also
                # removes the 503-on-restore path that left the game at 0.15x.
                try:
                    self.bridge.set_timescale(THINKING_TIMESCALE)
                    dipped = True
                    self._thinking_dip_active = True
                except BridgeError:
                    # Down, or up and not ready (v1.2 503): thinking is still
                    # allowed, the world just won't slow down for it.
                    pass
            context = self._dynamic_context(state, delta, trigger, layer)
            # T4: the same snapshot `roam.note()` just described to the model,
            # turned into plain data the validator can check the RESPONSE
            # against once it comes back.
            validation_ctx = self._validation_context(state)
            if layer == "tactical":
                result = self.tactical.decide(
                    context, mission_active=state.mission.active, validation_ctx=validation_ctx
                )
            else:
                shot, shot_trigger = None, None
                if (
                    self._pending_screenshot_trigger in VISION_TRIGGERS
                    and self.grabber is not None
                ):
                    shot, _ = self._capture_screenshot(f"director-{self._pending_screenshot_trigger}")
                    shot_trigger = self._pending_screenshot_trigger
                self._pending_screenshot_trigger = None
                result = self.director.decide(context, shot, shot_trigger, validation_ctx)
            self.api_backoff.record_success()
            return result
        except DecisionFailedError as exc:
            cause = classify_api_failure(exc.__cause__ or exc)
            log.error(
                "decision failed; reflex keeps control",
                extra={"kv": {"layer": layer, "cause": cause, "error": str(exc)[:160]}},
            )
            if cause != "invalid_output":
                # H2: a decision OUR schema rejected is not an API outage, and
                # charging it to the outage backoff is what turned a run of
                # pydantic rejections into an escalating 1.8 -> 4.1 -> 6.8 ->
                # 16.5 s silence while the API was perfectly healthy — the
                # "he just did nothing" stretches in the session logs. The
                # brain already retried it once with the violation fed back;
                # beyond that the reflex layer keeps control and the next
                # cadence tick simply tries again at full speed.
                self.api_backoff.record_failure(cause, exc.__cause__ or exc)
            return None
        finally:
            if dipped:
                try:
                    self.bridge.set_timescale(NORMAL_TIMESCALE)
                except BridgeError as exc:
                    # Leaving the world in slow motion would be a visibly broken
                    # show, so this is loud — but it must not raise out of
                    # `finally` and take the loop with it. `_guard_timescale`
                    # picks it up on a later tick; nothing used to.
                    log.warning(
                        "could not restore timescale; the tick guard will re-assert it",
                        extra={"kv": {"error": str(exc)[:160]}},
                    )
                finally:
                    self._thinking_dip_active = False

    @property
    def goal_text(self) -> str:
        """CURRENT GOAL, for the site and for the prompt — written by the
        PLUGIN, never by the model (T5, findings.md R3).

        Three fixed sources, in priority order, and nothing else:
          1. a locked roam goal's own description (`roam.dashboard_goal()`);
          2. the tracked mission's name (+ its first documented objective,
             when the walkthrough card has one) while a mission is active —
             `self.current_mission` is set at `mission_start` and cleared at
             `mission_end`/`mission_fail`, so its presence already tracks
             `state.mission.active` without needing `state` here;
          3. :data:`NEUTRAL_GOAL_TEXT`.
        `self.current_goal` (the director's free-text `goal` field) is
        deliberately NOT read here any more: a model narrating a different
        intention while the engine drives a locked goal — or while no goal is
        locked at all — is exactly the disagreement that made "CURRENT GOAL"
        on the site untrustworthy. It is still recorded to the `decisions`
        table (`_apply_decision`) and still read back into the NEXT prompt as
        context; it just never reaches this box or `stats.current_goal`.
        """
        locked = self.roam.dashboard_goal()
        if locked is not None:
            return locked
        if self.current_mission is not None:
            name = str(self.current_mission.get("name") or "").strip() or "the job"
            objectives = self.current_mission.get("objectives") or []
            first = str(objectives[0]).strip() if objectives else ""
            return f"{name} — {first}" if first else name
        return NEUTRAL_GOAL_TEXT

    def _apply_decision(
        self,
        layer: str,
        result: DecisionResult,
        state: GameState,
        delta: Delta,
        big_event: str | None,
    ) -> None:
        d: DecisionModel = result.decision
        if layer == "director":
            # Still recorded — but a locked roam goal OUTRANKS it everywhere it
            # is read (see `goal_text`), so the director's idea is held rather
            # than obeyed while a goal is in flight, and is back in force the
            # instant the lock clears.
            self.current_goal = d.goal
        # NOT governor.record() here any more: the cost is recorded by the call
        # that incurred it (brain.tactical.BilledCall), so a rejected or
        # retried decision is billed to the governor exactly like a used one.
        # Tokens spent inside a mission are that mission's `tokens` column.
        if self.missions.in_mission:
            self._mission_tokens += result.input_tokens + result.output_tokens
        self.writer.record_decision(
            {
                "layer": layer,
                "thought": d.thought,
                "say": d.say,
                "mood": d.mood,
                "goal": d.goal,
                "action": {"type": d.action.type, "params": d.action.wire_params()},
                "confidence": d.confidence,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cached_tokens": result.cached_tokens,
                "cost_usd": round(result.cost_usd, 6),
            }
        )
        # T3 (findings.md R2): a line only on an EVENT. Tactical decisions
        # fire on every poll (8-25 s); without this a stationary the agent with
        # nothing happening narrated a fresh line every cycle. `say` is forced
        # to "" in CODE — before publish AND before the no-repeat memory sees
        # it — whenever this tick carries none of `_tick_event_reason`'s
        # event set, even if the model wrote one. The `decisions` table row
        # above already has the model's raw, unforced `say`; only the
        # published/remembered copy is touched here.
        event_reason = _tick_event_reason(
            delta, big_event, self._tick_roam_transition, d.action.type
        )
        say = d.say
        if event_reason is None:
            log.debug(
                "commentary suppressed: no event this tick",
                extra={"kv": {"layer": layer, "would_have_said": d.say[:120]}},
            )
            say = ""
        # GROUNDING (live 2026-09-02): he narrated "keep Dave alive" and
        # "Trevor's got the rifle" through a whole mission in which neither man
        # existed. Naming somebody who is not there is the most damaging thing
        # he can say on a live stream, so a line that does it is not published.
        # Only names the game's own ped models can produce are ever challenged,
        # so streets, zones and car names pass untouched. T4's output
        # validator (brain.tactical/brain.director) already runs this same
        # check before the decision comes back here — this stays as a second,
        # cheap backstop, not the only line of defense any more.
        # An unnamed FRIENDLY nearby means we cannot prove anybody is absent —
        # a mission crewmate whose model we have no entry for could be exactly
        # who the line is about. Staying quiet then is the honest default:
        # dropping a TRUE line is worse than letting one through.
        absent = (
            []
            if has_unidentified_friendly(state)
            else absent_names_mentioned(say, present_names(state))
        )
        if absent:
            log.warning(
                "commentary names somebody who is not here; dropping the line",
                extra={"kv": {"say": say[:120], "absent": ",".join(absent)}},
            )
        elif say and self.commentary.gate_say(say):
            # `say` truthy first: T3's forced "" (no event) or an
            # honestly empty model line must never reach the bus just
            # because `gate_say` has nothing recent to compare an empty
            # string against.
            # T5 (findings.md R3): the published mood is the TRACKED mood
            # (`humanizer.MoodModel`, computed from health/wanted/recent
            # events — see `_reflex`'s `self.mood.observe(...)` calls), never
            # `d.mood`. The model's own mood is advisory at most: it is still
            # recorded verbatim on the `decisions` row above for audit, but it
            # does not drive what the overlay/feed shows.
            self.bus.publish("say", {"text": say, "mood": self.mood.mood})
        # Feeds the no-repeat list the prompt promises him back into the prompt
        # — always, even a gated-out line, so the model's own memory of what it
        # said stays accurate (see Commentary.gate_say's docstring).
        self.commentary.record_say(say)
        self.memory.log_day("decision", f"[{layer}] {say} -> {d.action.type}")
        # THE LOCK. This conditional is the whole "the model may not choose to
        # stand still" mechanism; the prompt text is decoration around it.
        #
        # The brain used to outrank free roam unconditionally: any bridge task
        # from a decision preempted the running step. With a goal LOCKED that is
        # exactly the observed failure — the model narrates a new plan, posts a
        # drive somewhere else or a `wait`, the goal dies, a new one is picked,
        # and at a 3 Hz poll that cycle repeats several times a second while he
        # stands in the road. So while a goal is locked and the decision does not
        # name it, the movement task is DROPPED rather than obeyed.
        #
        # Primitives are never dropped: radio, horn, look_around and a short wait
        # move nothing and keep the commentary alive, which is the difference
        # between committing to a goal and going mute for two minutes.
        if self.roam.blocks_foreign_action(d.action.type, d.goal):
            log.info(
                "decision action dropped: a roam goal is locked and this is not it",
                extra={
                    "kv": {
                        "action": d.action.type,
                        "locked_goal": self.roam.current.goal.id if self.roam.current else None,
                        "decision_goal": d.goal[:60],
                    }
                },
            )
            time.sleep(reaction_delay(self.rng))
            return
        if d.action.type not in MOVEMENT_TASKS:
            # A primitive posts no task and preempts nothing, so it never asks
            # the wheel: radio, horn, look_around and a short wait keep the
            # commentary alive even on a tick survival owns. v1.13's
            # `answer_call`/`reject_call` come through here too: they are POST
            # /task, but they move nobody, and a decision to hang up on a
            # ringing phone must not be dropped because free roam happens to
            # hold the wheel.
            time.sleep(reaction_delay(self.rng))  # humanizer: 300-900 ms reaction
            self._execute_action(d.action.type, d.action.wire_params())
            return
        # ARBITRATED, not merely registered. The brain used to `force()` its way
        # onto the wheel: it had no stand-down rule against the reflex layer, so
        # a decision computed from a snapshot 1-2 s old could post navigation
        # straight over a combat task issued milliseconds earlier and
        # ThreatLatch's hold-down would then suppress the re-post — the reflex
        # silently defeated. `brain` sits in the MISSION class: it outranks the
        # mission follower, the day plan and free roam (a deliberate decision
        # beats a structural one), and it yields to the reflex ladder.
        token = self.wheel.acquire("brain", f"{layer} decision: {d.action.type}")
        if token is None:
            log.info(
                "decision action dropped: the wheel is held by a higher owner",
                extra={
                    "kv": {
                        "action": d.action.type,
                        "held_by": self.wheel.owner,
                        "held_reason": self.wheel.reason,
                    }
                },
            )
            time.sleep(reaction_delay(self.rng))
            return
        # A bridge task from the brain ends whatever free roam was doing. The
        # wheel's preempt hook has usually already done it (roam is below the
        # brain, so acquiring took the wheel off it); this covers a handoff goal
        # that holds no token because it posts nothing of its own.
        self._end_activity_if_running("preempted_by_decision")
        time.sleep(reaction_delay(self.rng))  # humanizer: 300-900 ms reaction
        self._execute_action(d.action.type, d.action.wire_params(), token)

    def _wait_has_a_reason(self) -> bool:
        """Is there anything a `wait` could be waiting FOR this tick?

        Read from the per-tick flags `run()` caches from `/state`: the game
        owns the moment (cutscene, switch, retry, dead/arrested), a mission is
        running (the objective may be a wait), or the police are interested
        (hiding is a plan). Free roam with control in hand is none of these.
        """
        return bool(
            self._cutscene_active
            or self._switch_in_progress
            or self._retry_in_flight
            or self._player_down
            or self._mission_active
            or self._wanted_now > 0
        )

    def _execute_action(
        self,
        action_type: str,
        params: dict[str, Any],
        token: MovementToken | None = None,
    ) -> str | None:
        """Run one decision-schema action. Returns the bridge task id when the
        action posted a task (POST /task's `task_id` is authoritative — the
        caller must match on it, never on whatever id the next snapshot shows),
        and None for primitives, quiet periods and failures.

        **THE MOVEMENT GATE.** Every CONTRACTS §1 bridge task must carry the
        movement wheel's current token or it is refused here. The wheel's own
        spec said to put this assertion in the bridge; it is here instead, on
        purpose and with no wire change:

        * the harness is the bridge's ONLY client, and this method is the ONLY
          funnel — every reflex, every planner, every roam step, every brain
          decision and every primitive already passes through it, because the
          cutscene / dead / blocking-screen / stalled-type refusals all live
          here and each of them was moved here for the same reason;
        * so a token check here gives exactly the guarantee "no bridge task
          reaches the game without the wheel", with no `POST /task` body
          change, no CONTRACTS version bump and no bridge redeploy on a
          production box we are not allowed to touch.

        §2 primitives take no token: `look_around` is a mouse sweep, `wait`
        posts nothing at all, `radio`/`horn` are their own endpoints and
        `brake_tap`/`swerve`/`reverse_out`/`press_prompt_key` are short raw key
        holds. None of them is a `POST /task`, so none of them preempts a
        running task, so none of them belongs to the wheel.

        CONTRACTS v1.13's `answer_call`/`reject_call` sit on that same
        primitive-like side even though they ARE `POST /task`
        (:data:`PHONE_TASKS`): they inject one phone control per frame and move
        nobody, and a ringing phone has to be answerable or refusable whatever
        owns the wheel. Making the phone ask `roam` or `mission` for permission
        to hang up on Simeon would mean the missions-off switch quietly stops
        working exactly while a goal is locked, which is most of the time. Every
        OTHER refusal below still applies to them, because a task posted during
        a cutscene or on a blocking screen reaches nobody whatever it does.
        """
        if action_type in MOVEMENT_TASKS and (wait := self.cleared_backoff.refuses(action_type)) > 0:
            # The game itself cleared this task type a moment ago. Re-posting it now
            # is the storm measured live: started, cleared_by_game, re-posted within
            # 300 ms, for minutes. Other types still go through, so a stuck ped can
            # try something DIFFERENT — which is the whole point.
            log.info(
                "task refused: the game just cleared this type; backing off",
                extra={"kv": {"action": action_type, "wait_s": round(wait, 1)}},
            )
            return None
        if action_type in MOVEMENT_TASKS and not self.wheel.holds(token):
            # The caller was refused (or never asked) and posted anyway. That is
            # a bug in the caller, not a condition of the world, so it is loud.
            log.error(
                "task refused: the caller does not hold the movement wheel",
                extra={
                    "kv": {
                        "type": action_type,
                        "token_owner": None if token is None else token.owner,
                        "token": None if token is None else token.id,
                        "held_by": self.wheel.owner,
                        "held_reason": self.wheel.reason,
                        "tick": self.wheel.tick,
                    }
                },
            )
            return None
        if action_type in BRIDGE_TASKS and (
            self._cutscene_active
            or self._player_down
            or self._switch_in_progress
            or self._retry_in_flight
        ):
            # CONTRACTS §1 bridge tasks map to the engine's own ped-task
            # natives; the engine ignores them while a cutscene owns control,
            # so posting one would just be a wasted HTTP round trip (and, for
            # a brain decision, wasted API spend on an action nobody applies).
            # Dead/arrested is the same story: there is nothing productive a
            # corpse or a cuffed man can do with a task, and posting one is a
            # wasted call for no effect (CLAUDE.md rule 5 — recovery means
            # waiting for the game's own respawn, never cheating death, so
            # this never substitutes a fake action for that wait). One choke
            # point catches every source — reflex, activity runner,
            # mission-follow, and the brain's own decision — rather than
            # relying on each caller to remember the check.
            if self._cutscene_active:
                reason = "cutscene playing"
            elif self._switch_in_progress:
                reason = "protagonist switch in progress"
            elif self._retry_in_flight:
                reason = "mission retry/checkpoint reload in progress"
            else:
                reason = "player down (dead/arrested)"
            log.info(f"task suppressed: {reason}", extra={"kv": {"type": action_type}})
            return None
        if action_type in BRIDGE_TASKS and self._screen_blocked:
            # The script thread is not ticking, so the game will not apply a
            # queued command at all (that is the same `game_thread_stalled`
            # condition CONTRACTS v1.2 names). Posting is pure noise, and the
            # binding callers do would latch onto a task that never starts.
            log.info(
                "task suppressed: the game is on a modal/blocking screen",
                extra={"kv": {"type": action_type}},
            )
            return None
        if action_type in BRIDGE_TASKS and self.task_stall.blocked(action_type):
            # The same choke point, for the same reason: a task type that has
            # just been measured pinning him in place must not be re-posted by
            # ANY layer for a short while, or he walks straight back into the
            # identical deadlock on the next tick. `TaskStallDetector.blocked`
            # logs this once per episode rather than at the poll rate.
            return None
        try:
            if action_type == "wait":
                # A long `wait` must NOT block the loop: perception has to keep
                # running at 2-4 Hz or a death mid-wait goes unseen and the
                # site's heartbeat goes stale (>60 s reads as offline, §5).
                # The loop honours the quiet period instead.
                seconds = max(0.0, min(30.0, float(params.get("seconds", 5))))
                if not self._wait_has_a_reason():
                    # Free roam, control in hand, nothing running: a `wait` here
                    # is the "he's just thinking, not playing" loop seen on
                    # stream — the quiet period held the drive-away AND the next
                    # goal pick, and the next think said `wait` again. Standing
                    # still is not a plan; the roam engine gets the tick.
                    log.info(
                        "wait ignored: free roam with control, nothing to wait for",
                        extra={"kv": {"seconds": round(seconds, 1)}},
                    )
                    return None
                self._quiet_until = time.monotonic() + seconds
                log.info("quiet period", extra={"kv": {"seconds": round(seconds, 1)}})
                return None
            # Any other action ends a quiet period: he decided to do something.
            self._quiet_until = 0.0
            if action_type in BRIDGE_TASKS:
                if action_type == "exit_vehicle":
                    # Tell the roam engine this dismount was OURS. Without it
                    # every deliberate exit looks exactly like being dragged out
                    # of the driver's seat, and the "that's my car" trigger
                    # fires on his own decision to get out.
                    self.roam.note_self_exit()
                # Latch the tick BEFORE the round trip: from here on the wheel
                # refuses every other owner until the next `begin_tick`, so "no
                # two movement tasks from different owners in the same tick" is
                # structural rather than a property of the call order. Marked on
                # the ATTEMPT, because a task lost to a bridge blip still used
                # this tick's one shot and the reflexes will ask again next tick.
                # None only for a PHONE_TASKS post, which takes no token by
                # design (see the docstring) and therefore does not spend the
                # tick's one movement slot — it moves nobody, so there is
                # nothing for a later owner to trip over. For everything else
                # the gate at the top guaranteed a live token.
                if token is not None:
                    self.wheel.mark_posted(token, action_type)
                task_id = self.bridge.post_task(action_type, params)
                if token is not None:
                    # F6's stopwatch stops HERE and nowhere else: this is the one
                    # line in the harness where a movement task actually reaches
                    # the game, and it is reached only after a 202. "The wheel
                    # was acquired" is not movement and neither is "the post was
                    # attempted" — a task lost to a bridge blip left him exactly
                    # as still as no task at all, which is the whole lesson of
                    # `VehicleController`'s motion verification.
                    self.control_regained.moved(action_type)
                return task_id
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

    def _end_activity_if_running(self, outcome: str, *, by: str | None = None) -> None:
        # Free roam's goal is over, so free roam's claim on the wheel is over
        # too. Released FIRST and unconditionally: a token left behind by a
        # goal that has ended is a hold nothing will ever give back, and the
        # owners below `roam` would wait on it forever.
        self._roam_token = None
        self.wheel.release_owner("roam")
        # One slot, one owner: closing the step machine also closes the roam
        # goal that owned it, so the two can never disagree about whether
        # something is running. `close` returns the goal id, the quotable `why`
        # and — the field that matters — whether `done_when` actually fired, so
        # the feed can tell a verified completion from a timeout.
        extra = self.roam.close(outcome)
        ended = self.activity_runner.finish(outcome)
        # Always told, even when nothing was running: the planner's "is an
        # activity still going" flag is what keeps a roam block from being cut
        # short mid-set-piece, and a flag that only clears on the happy path is
        # a flag that eventually sticks on.
        self.planner.roam_activity_ended()
        if ended is None:
            return
        _, payload = ended
        payload.update(extra)
        if by is not None:
            # The harness's `roam_goal_failed{reason: "preempted", by: <owner>}`.
            # See ROAM_PREEMPTED at the top of this file for why it rides on
            # `activity_end` instead of being a §4 type of its own.
            payload["by"] = by
        self.writer.record_event("activity_end", payload)

    def _drive_activities(self, state: GameState) -> None:
        """The single free-roam slot: one goal engine, one owner, one task source.

        Kept under its old name deliberately — this is the ONE place in the tick
        where free roam may post movement (`run()` calls it between the day plan
        and the mission objective, and the ordering comment there exists because
        two systems reading each other's stale state caused a bug once). What
        changed is what it drives: `behavior.roam` chooses by live-state `needs`
        and grades by a `done_when` predicate, instead of an activity being
        declared finished the moment its last bridge task returned `done`.

        Order inside the tick matters and is the specification:

        1. Observe first, always. The cross-tick derivations (is that bus
           stopped, was he pulled out of his car, how long has he been inside a
           2 m circle) have to see EVERY snapshot, including the ones on which
           free roam stands down — a bus that stopped during a mission is still
           a stopped bus when the mission ends.
        2. Judge the locked goal SECOND, before any stand-down gate. A goal the
           world has already completed must be closed as `completed`, not
           reported as "preempted by a mission" one line later, or the completed
           counter that forces the story forward never advances.
        3. Only then the gates.
        """
        # 1. Perception. Free even when he is mid-mission and this returns early.
        self.roam.observe(state, mood=self.mood.mood, mood_style=self.mood.driving_style())

        # 2. Grade the locked goal against the world.
        if self.roam.current is not None:
            # A locked goal is not up for renegotiation. If the model named a
            # DIFFERENT id this tick it is ignored on purpose — switching
            # mid-goal is exactly the boring loop free roam exists to prevent —
            # but it is logged, because a run of these means the prompt is not
            # telling him to repeat the locked id and the menu keeps tempting him.
            said = self.roam.model_choice(self.current_goal)
            if said is not None and said != self.roam.current.goal.id:
                log.info(
                    "goal_switch_ignored: a goal is locked; the model named another",
                    extra={"kv": {"locked": self.roam.current.goal.id, "named": said}},
                )
            if self._judge_roam_goal(state):
                return

        if self.governor.level >= 3 or self.breaks.on_break:
            return
        if self._threat_has_the_wheel:
            # Survival owns the tick; a goal step posted now would preempt the
            # combat/cover task `_reflex` just issued. The goal is NOT ended — a
            # firefight is seconds, and the step machine already tolerates its
            # step being preempted — it simply does not advance this tick.
            return
        if state.player.dead or state.player.arrested:
            return
        if self.missions.in_mission or (state.player.wanted > 0 and not self.roam.heat_is_the_goal()):
            # Stars end a goal — unless stars were the goal. `judge()` already
            # exempts `wants_heat` goals from its wanted override; this gate
            # did not, and closed `earn_two_stars` as "wanted" one poll after
            # its drive-by earned the star (soak trace, 2026-09-03).
            if self.activity_runner.current is not None:
                self._end_activity_if_running(
                    "mission" if self.missions.in_mission else "wanted"
                )
            return
        if self.planner.in_mission_block:
            # The day plan is walking him into a mission-start marker; a
            # free-roam task here would post a drive straight over the top of
            # that navigation. `start_nearest_mission` reaches this branch by
            # design: it is a HANDOFF goal that posts nothing of its own and was
            # already closed in step 2 the moment `mission.active` went true.
            if self.activity_runner.current is not None:
                self._end_activity_if_running("day_plan_mission_block")
            return

        if time.monotonic() < self._quiet_until:
            return  # a `wait` step is still running its course

        if self.roam.current is not None:
            # Routed on the LOCK, not on the step machine: the runner can be
            # abandoned under a goal that is still locked (see
            # `_advance_roam_goal`), and reaching `_begin_roam_goal` in that
            # state made it release roam's own lease on a `pick()` that had
            # nothing to pick.
            self._advance_roam_goal(state)
            return

        if not self.roam.due():
            # Not idle, just between goals — but "between goals" is capped by
            # `RoamEngine.due`, which ignores its own jittered beat the moment he
            # is actually standing still. That is the operator's one hard rule.
            return
        self._begin_roam_goal(state)

    def _judge_roam_goal(self, state: GameState) -> bool:
        """Grade the locked goal. True when this tick is finished with free roam.

        Returns True when the goal ended (so the caller stops) or when an
        escalation posted an action; False when the goal is simply still running.
        """
        verdict = self.roam.judge(state)
        if verdict is None:
            return False
        if verdict == "done":
            self._end_activity_if_running("completed")
            return True
        if verdict == "escalate":
            # The in-goal stuck watchdog's first strike. The plan was built from
            # a snapshot that has gone stale (the car drove off, the door did not
            # open); rebuild it from where he ACTUALLY is rather than re-posting
            # a task at a coordinate that is no longer right.
            fresh = self.roam.replan(state)
            if fresh is None:
                self._end_activity_if_running("stuck")
                return True
            step = self.activity_runner.replace_plan(self.roam.current.plan)
            if step is None:
                step = self._restart_locked_plan()
            if step is not None:
                self._issue_activity_step(step)
            return True
        # timeout / stuck / wanted_override / player_down: the goal is over. A
        # new one is picked on the next tick, which at 2-4 Hz is a few hundred
        # milliseconds, not a gap anybody can see.
        self._end_activity_if_running(verdict)
        return True

    def _advance_roam_goal(self, state: GameState) -> None:
        """One step of the locked goal's plan, or a re-plan when it ran out.

        A plan running out is NOT completion any more. `steal_nice_car`'s whole
        plan is a walk and an `enter_nearest_vehicle`, and the bridge falls back
        to the nearest usable car when nothing outranks — so the old code
        declared victory in a WORSE car. Now the plan running out with
        `done_when` still false buys one fresh plan, and after that the goal
        fails honestly.
        """
        step = self.activity_runner.next_step(state.last_task.status, state.last_task.id)
        if step is not None:
            self._issue_activity_step(step)
            return
        running = self.activity_runner.current
        if running is None:
            # The step machine was abandoned under a goal that is still locked.
            # `ActivityRunner.next_step` treats any foreign RUNNING task as
            # preemption, but only the wheel's preempt hook closes the goal —
            # and a task that never touches the wheel can still replace the
            # step's bridge task: `answer_call`/`reject_call` (CONTRACTS v1.13
            # phone tasks are not movement tasks). Observed as the goal sitting
            # locked with nothing running until the stuck watchdog failed it
            # ~20 s later. The goal is not over: rebuild its plan from where he
            # is and carry on, exactly as the escalate arm does.
            if state.last_task.type in PHONE_TASKS and state.last_task.status == "running":
                # The phone task is still pressing its key (it waits up to a
                # few seconds for `in_call` to flip). A movement task posted now
                # would preempt it and the call would never be answered or hung
                # up — the goal waits one more poll, locked, and restarts on the
                # tick the phone task is done.
                return
            if self.roam.replan(state) is None:
                self._end_activity_if_running("preempted", by="bridge_task")
                return
            log.info(
                "roam step machine was abandoned under a locked goal; restarting its plan",
                extra={"kv": {"goal": self.roam.current.goal.id,
                              "last_task": state.last_task.type}},
            )
            restarted = self._restart_locked_plan()
            if restarted is not None:
                self._issue_activity_step(restarted)
            return
        if not running.finished:
            return
        fresh = self.roam.replan(state)
        if fresh is None:
            self._end_activity_if_running("actions_done_goal_unmet")
            return
        replaced = self.activity_runner.replace_plan(self.roam.current.plan)
        if replaced is not None:
            self._issue_activity_step(replaced)

    def _restart_locked_plan(self) -> dict[str, Any] | None:
        """Start the locked goal's (re-planned) steps on an EMPTY step machine.

        `replace_plan` refuses when nothing is running, which is exactly the
        abandoned case; `start_plan` opens a fresh record. The goal's own timer
        lives in `roam.current`, so a restart cannot buy it more time.
        """
        locked = self.roam.current
        if locked is None:
            return None
        return self.activity_runner.start_plan(as_activity(locked.goal), locked.plan)

    def _begin_roam_goal(self, state: GameState) -> None:
        """Pick one goal off the offered menu and post its first action.

        Two things may influence the choice and neither can override the menu:
        the model's own preference (read out of its free-text `goal` field by an
        exact id match against what was actually offered) and the day plan's idea
        for this roam block. A name that is not on the menu is ignored, which is
        what "the model may only pick from that list" means in code.
        """
        # THE WHEEL FIRST, before anything is chosen. A refused acquire means
        # free roam does NOTHING this tick: it posts no task, it starts no goal
        # timer, and it does not even pick — a goal locked while somebody else
        # is driving is a goal that runs its whole timeout without ever moving
        # him, which is the "he picked something and then stood still" failure.
        # An OPEN lease: the goal owns the wheel until it completes, fails,
        # times out or is preempted, not just for this tick.
        token = self.wheel.acquire("roam", "picking a goal", lease_ticks=None)
        if token is None:
            return
        # `observe` refreshed the menu at the top of this tick, so the id the
        # model named is validated against what is on offer NOW.
        chosen = self.roam.model_choice(self.current_goal)
        if chosen is None and self.roam.offered_ids():
            # The one decision in free roam that is genuinely the model's, and it
            # did not make it: the `goal` field named no offered id (or named
            # two). The engine falls back to the head of the menu, which is the
            # triggered offer when there is one, so the show carries on — but
            # this is logged because a RUN of these means the prompt and the menu
            # have drifted apart, and that is invisible from the stream.
            log.info(
                "goal_fallback: the model named no offered goal; taking the top of the menu",
                extra={"kv": {"said": (self.current_goal or "")[:80],
                              "offered": list(self.roam.offered_ids())}},
            )
        picked = self.roam.pick(
            state, goal_id=chosen, prefer=self.planner.roam_preference
        )
        if picked is None:
            self.wheel.release(token)
            return
        self._roam_token = token
        locked, first = picked
        if locked.goal.handoff:
            # It posts nothing itself: the day planner owns the trip to a marker
            # end to end, and this goal exists to ASK for it and then watch
            # `mission.active` for the answer. So it gives the wheel straight
            # back — holding it would mean the day plan had to PREEMPT the goal
            # that just asked it for a favour, and the preemption would cancel
            # that goal one tick after it started.
            self._roam_token = None
            self.wheel.release(token)
            self.planner.request_mission_block(locked.why)
            self.planner.roam_activity_started(locked.goal.id)
            self.writer.record_event(
                "activity_start",
                {"activity": locked.goal.id, "why": locked.why, "params": {}},
            )
            self.memory.log_day("activity", f"goal: {locked.goal.description}")
            return
        activity = as_activity(locked.goal)
        step = self.activity_runner.start_plan(activity, locked.plan)
        if step is None:
            self._roam_token = None
            self.wheel.release(token)
            self.roam.close("no_plan")
            return
        self.planner.roam_activity_started(locked.goal.id)
        self.writer.record_event(
            "activity_start",
            {
                "activity": locked.goal.id,
                "why": locked.why,
                "params": dict(first.get("params", {})),
            },
        )
        self.memory.log_day(
            "activity", f"goal {locked.goal.id}: {locked.goal.description} ({locked.why})"
        )
        self._issue_activity_step(step)

    def _issue_activity_step(self, step: dict[str, Any]) -> None:
        """Run one plan step and bind the runner to the task id POST /task returned.

        Binding to the returned id rather than to the next snapshot's
        `last_task.id` is what stops the runner from latching the PREVIOUS task
        (3 Hz poll vs a 60 Hz game thread) and skipping the step.
        """
        if step["type"] not in BRIDGE_TASKS:
            # `look_around`, `press_prompt_key`, a short `wait`: no task, no
            # preemption, no wheel. A goal is allowed to breathe on a tick
            # survival owns.
            self.activity_runner.bind_step_task(self._execute_action(step["type"], step["params"]))
            return
        # The goal already holds the wheel (`_begin_roam_goal` took it before it
        # picked). Re-acquiring rather than trusting the cached token is what
        # makes a preemption STICK: if something above free roam has taken it,
        # this is refused and the step is not posted over the top of them.
        token = self.wheel.acquire("roam", step["type"], lease_ticks=None)
        if token is None:
            # Refused: the goal is already over (the preempt hook ended it the
            # moment the wheel changed hands), so there is nothing left to bind.
            return
        self._roam_token = token
        task_id = self._execute_action(step["type"], step["params"], token)
        if task_id is None:
            # The step never reached the game (bridge down / not ready). Nothing
            # will complete it; end the activity now rather than sit through the
            # step timeout pretending it is running.
            self._end_activity_if_running("bridge_task_lost")
            return
        self.activity_runner.bind_step_task(task_id)

    # -- the day plan ------------------------------------------------------------

    def _drive_day_plan(self, state: GameState) -> None:
        """Advance the day plan: roam blocks, and the trip to a mission-start marker.

        Runs BEFORE `_drive_activities` so the block state the activity gate
        reads is this tick's, not last tick's — otherwise the tick the planner
        switches to a mission block is also a tick on which an activity can
        start, only to be ended one tick later.

        `update()` itself is called unconditionally (it is the state machine: a
        death mid-trip has to abort the block and say so even though nothing
        will be posted). Only the POST is gated, and only on the one condition
        the planner cannot see: a deliberate `wait` the brain is still holding.
        Everything else — dead, arrested, wanted, cutscene, someone else's task
        already running — the planner refuses on its own.
        """
        if self.governor.level >= 3:
            # L3 is "asleep in the car" (CONTRACTS §7). A day plan that drove
            # off to a mission marker while he is meant to be parked and silent
            # is the same bug the L2 wander reflex had: it makes L3 mean
            # nothing. The plan wakes up when the hourly window resets.
            return
        outcome = self.planner.update(state, self.mood.mood, self.missions.in_mission)
        for event_type, payload in outcome.events:
            self.writer.record_event(event_type, payload)
        if outcome.task is None:
            return
        if self._threat_has_the_wheel:
            # The survival ladder claimed this tick a few milliseconds ago.
            # `update()` above still ran (it is the state machine - a death
            # mid-trip has to end the block honestly); only the POST is held.
            return
        if time.monotonic() < self._quiet_until:
            return  # honour a deliberate `wait` from the brain
        # An OPEN lease: the trip to a mission-start marker spans many ticks and
        # the planner's own re-post latch depends on nothing else stealing it in
        # between. Above `roam` and below `mission`/`brain`, so the trip
        # preempts a free-roam goal (that is what a mission block MEANS) and
        # yields to the mission itself the moment it starts.
        token = self.wheel.acquire(
            "day_plan", outcome.task["type"], lease_ticks=None
        )
        if token is None:
            return
        self._day_plan_token = token
        task_id = self._execute_action(outcome.task["type"], outcome.task["params"], token)
        self.planner.bind_task(task_id)

    # -- mission following -------------------------------------------------------

    def _drive_mission_objective(self, state: GameState) -> None:
        """Structural pursuit of `mission.objective_blip` (WP-H item 1).

        Called after the brain has had first refusal on the tick (same
        ordering rule as `_drive_activities`), so a fresh decision is never
        posted and then immediately preempted by this reflex a moment later.
        `MissionFollower.plan` already returns None during a cutscene, when
        there is no objective, when it has backed off a stuck objective, or
        when something else already has the wheel — this method only has to
        act on what comes back, and to respect a brain-issued `wait` the same
        way `_drive_activities` does.
        """
        if state.player.dead or state.player.arrested:
            return  # nothing to steer toward on a corpse or in a cell
        if self._threat_has_the_wheel:
            return  # survival outranks the objective; see `_threat_has_the_wheel`
        if time.monotonic() < self._quiet_until:
            return  # honour a deliberate `wait` from the brain
        step = self.mission_follower.plan(state)
        if step is None:
            return
        # `mission.active` already put the wheel in `mission`'s hands (see
        # `_reflex`); this renews that hold and returns the live token. It is
        # refused only when the reflex ladder or a brain decision owns the tick,
        # and in both of those cases posting objective navigation over the top
        # is exactly the double-post this arbitration exists to stop.
        token = self.wheel.acquire("mission", step["type"], lease_ticks=None)
        if token is None:
            return
        self._mission_token = token
        task_id = self._execute_action(step["type"], step["params"], token)
        self.mission_follower.bind_task(task_id)

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
        # The budget stop is a REFLEX-class owner on an open lease: it holds the
        # wheel from here until the `stop` at the end of the drive, so nothing
        # below it can wander him back out of the parking spot — which is how
        # L3 came to mean nothing at all the first time round.
        token = self.wheel.acquire("governor", "L3: park somewhere scenic", lease_ticks=None)
        if token is None:
            log.info(
                "governor L3 park deferred: the wheel is held",
                extra={"kv": {"held_by": self.wheel.owner, "held_reason": self.wheel.reason}},
            )
            self._pending_park = True  # ask again next tick; the cap has not moved
            return
        self._governor_token = token
        if not state.player.in_vehicle or state.player.dead or state.player.arrested:
            # On foot / dead / in a cell: there is no car to be asleep in. Stop
            # and say so plainly rather than walking him across the map.
            self._execute_action("stop", {}, token)
            self._governor_token = None
            self.wheel.release(token)
            self._say(
                "Out of budget for the hour. Standing right here until the meter resets."
            )
            return
        pos = (state.player.pos.x, state.player.pos.y, state.player.pos.z)
        spot, plan = scenic_park_plan(pos, self.mood.driving_style())
        task_id = self._execute_action(plan[0]["type"], plan[0]["params"], token)
        if task_id is None:
            # Could not post the drive (bridge down / not ready): stop where he
            # is. Better parked badly than driving with nobody watching.
            self._execute_action("stop", {}, token)
            self._governor_token = None
            self.wheel.release(token)
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
            self._governor_token = None
            self.wheel.release_owner("governor")
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
            # Something else is driving; do not fight it. The wheel says who,
            # and the park gives up its claim rather than sitting on it.
            self._governor_token = None
            self.wheel.release_owner("governor")
            return
        token = self.wheel.acquire("governor", "L3: parked, engine off", lease_ticks=None)
        if token is not None:
            self._execute_action("stop", {}, token)
            self._governor_token = None
            self.wheel.release(token)
        self._say(
            "Parked. Engine off, meter running down. Back when the hour resets.", "chill"
        )

    # -- screen capture --------------------------------------------------------

    def _pump_screen_grab(self) -> int | None:
        """One tick's worth of screen capture. Bounded; never blocks the loop.

        Returns the HUD objective-region hash when a frame arrived, else None
        (no frame this tick simply means no HUD-change signal this tick — the
        perceptor keeps the previous hash and compares across the gap). The
        newest frame is also kept for `_capture_screenshot`.

        The grab used to be called straight from the loop. On the real server
        the display flipped modes, dxcam entered its own 90-attempt recovery
        INSIDE `grab()`, and the loop produced nothing at all for 172 s
        (16:53:53 -> 16:56:45) and 76 s earlier the same session: no
        perception, no reflexes, no decisions, no heartbeat. Screenshots are a
        nice-to-have; the loop is the show.
        """
        if self.grabber is None:
            return None
        result = self.grab_pump.poll()
        if self.grab_pump.dead:
            # Ten failures inside a minute: this display is not coming back
            # during this session. Say so once, loudly, and carry on without
            # screenshots rather than paying for a dead device every tick.
            log.error(
                "screen capture disabled for the rest of this session; the show "
                "continues without screenshots, and MISSIONS can no longer move "
                "(a pass is only ever counted off a real MISSION PASSED banner)",
                extra={"kv": {"reason": self.grab_pump.last_reason[:200]}},
            )
            # Not said out loud: there is no CONTRACTS §4 event for "capture
            # device died", and a line without an event is exactly the noise
            # T3 removed. The overlay's mission counter simply stops moving,
            # and the log line above is the record of why.
            self.grabber = None
            self._frame = None
            return None
        if result is None:
            return None
        frame, objective_hash, captured_at = result
        self._frame = frame
        # The grabber's own capture time, NOT now: `grab()` hands back the
        # cached frame when dxcam has nothing new, and stamping that "now" made
        # FRAME_MAX_AGE_S measure "time since the pump last returned something"
        # instead of "time since a new frame was captured". On this box the
        # display stops producing frames whenever the game loses focus, so the
        # difference is the everyday case, not a corner one: an unchanged
        # screen now correctly reads as no fresh screenshot.
        self._frame_at = captured_at
        return objective_hash

    # -- timescale guard -------------------------------------------------------

    def _guard_timescale(self, state: GameState) -> None:
        """Put the world back to normal speed if something left it slowed.

        The observed failure: `_think` dipped the world to 0.15x, the restore
        POST came back `503 game_thread_stalled`, and the code logged "watchdog
        will catch it" — but there was no watchdog. Nothing anywhere re-read
        `world.timescale`, so the game stayed at 15% speed for minutes at a
        time (four such restores in one session's logs: 16:45:56, 17:14:10,
        17:19:30, 17:26:23). That is the "slow motion" the stream showed.

        `/state.world.timescale` is in every snapshot, so this is a free check
        every tick. It is deliberately narrow:

        * never while the harness is holding its own dip;
        * never below TIMESCALE_GAME_FLOOR — POST /timescale is clamped to
          0.1-1.0 bridge-side, so anything under 0.1 (the death slow-motion
          sits near 0.075) is the GAME's effect and not ours to overrule;
        * never while dead or in a cutscene, the two states the game slows
          time for on purpose;
        * at most one POST every TIMESCALE_REASSERT_INTERVAL_S.

        Restoring 1.0 is putting the game back to the speed it ships at — it
        is not a cheat and cannot be one: the bridge clamps at 1.0, so there
        is no "faster than normal" to ask for (CLAUDE.md rule 5).
        """
        if self._thinking_dip_active:
            return
        timescale = state.world.timescale
        if timescale >= TIMESCALE_OK_ABOVE or timescale < TIMESCALE_GAME_FLOOR:
            return
        if state.player.dead or state.mission.cutscene_active:
            return
        now = time.monotonic()
        if now - self._last_timescale_reassert < TIMESCALE_REASSERT_INTERVAL_S:
            return
        self._last_timescale_reassert = now
        try:
            self.bridge.set_timescale(NORMAL_TIMESCALE)
        except BridgeError as exc:
            log.warning(
                "world is in slow motion and the re-assert did not land",
                extra={"kv": {"timescale": round(timescale, 3), "error": str(exc)[:160]}},
            )
            return
        log.warning(
            "world was left in slow motion; re-asserted normal time",
            extra={"kv": {"timescale": round(timescale, 3)}},
        )

    def _on_game_restart(self) -> None:
        """Wipe every observer that compares against a pre-crash snapshot.

        Without this the first post-relaunch tick invents events: a death
        because the player was dead when the game died, a wanted_change from a
        stale star count, a finished task that no longer exists.
        """
        # Closed FIRST, while the roam engine still knows which goal was
        # locked, so the `activity_end` row carries the goal id instead of a
        # bare "game_restarted" against nothing.
        self._end_activity_if_running("game_restarted")
        self.perceptor = Perceptor()
        self.stuck = StuckDetector()
        self._phone_answered_this_ring = False
        self._phone_hung_up_this_call = False
        self._phone_call_connected_at = None
        self.task_stall = TaskStallDetector()
        self.cleared_backoff = ClearedByGameBackoff()
        self.stranded = StrandedEscalator()
        # T9: every timer here is keyed on wall-clock time or an ephemeral
        # vehicle handle, void behind a new game process — same rule as
        # every other stateful observer on this list.
        self.water.reset()
        self.road_dodge.reset()
        self.jack_handoff.reset()
        # Same rule as every other stateful observer here: a fresh instance.
        # The roam engine's memory is all cross-tick derivations about a world
        # that no longer exists — vehicle handles are ephemeral by contract, so
        # "that bus has been stopped for 3 s" is void behind a new game process.
        self.roam = RoamEngine(self.rng, missions_enabled=self.settings.missions_enabled)
        self.house_escape = HouseEscape()
        # Interior-proxy handles belong to the dead process, and so does every
        # exit this run had learned for them.
        self.interior_escape = InteriorEscape()
        self._interior_token = None
        self.missions = MissionTracker()
        self.mission_follower = MissionFollower()
        self.planner = DayPlanner(self.rng, missions_enabled=self.settings.missions_enabled)
        self.death_recovery = DeathArrestRecovery()
        self.blocking_screen_watchdog = BlockingScreenWatchdog()
        self._screen_blocked = False
        self.threat_latch = ThreatLatch()
        self.damage = DamageTracker()
        # Vehicle handles are ephemeral by contract: the car he was in does not
        # exist behind a new game process, so every timer keyed on its handle
        # is void. Same reason the trackers above are rebuilt.
        self.vehicle.reset()
        self.wheel.reset()
        # Same rule: every flag F6 diffs against belongs to the dead process, so
        # a relaunch must not manufacture a "cutscene ended" edge out of one
        # snapshot from before the crash and one from after it.
        self.control_regained.reset()
        self.current_mission = None
        self.current_goal = INITIAL_GOAL
        # A mission that was in flight when the game died has no honest ending
        # to report, so its half-built row is dropped rather than closed with a
        # guessed outcome. The lifetime counters are deliberately NOT reset:
        # a relaunch does not un-die a death.
        self._mission_started_iso = None
        self._mission_tokens = 0
        self._pending_screenshot_trigger = None
        # The dxcam device has to be rebuilt after a relaunch, but that rebuild
        # blocks exactly like a grab does — so it is REQUESTED here and
        # performed by the grab worker, which is the one thread allowed to
        # touch the capture device. A rebuild that fails surfaces as a counted
        # grab failure and, if it keeps failing, disables capture below.
        self._grab_reset_wanted = self.grabber is not None
        self._frame = None
        self._frame_at = 0.0
        # No line here either: a restart detected between two good polls has
        # no §4 event of its own (`bridge_down`/`bridge_up` only fire when a
        # poll actually failed), and `_end_activity_if_running` above already
        # recorded the `activity_end` a locked goal earns — which is the one
        # event the next decision can speak to.
        log.info("game restarted; perception and stuck detectors reset")

    # -- housekeeping ----------------------------------------------------------

    def _believe_state(self, state: GameState | None) -> bool:
        """Is THIS tick's snapshot one we are willing to publish as current?

        No snapshot at all (the bridge is up but has none — a loading screen or
        a stalled game) is not one. Neither is a snapshot taken while
        `BlockingScreenWatchdog` says the script thread has stopped: the bridge
        keeps answering 200 with the same frozen `tick`, so health, cash, street
        and zone are last-known values, not current ones, and republishing them
        under a fresh timestamp is publishing a number the game is not producing.
        """
        return state is not None and not self._screen_blocked

    def _accrue_play_time(self, live: bool) -> None:
        """Add this tick's elapsed time to `hours_alive` — only if it was played.

        `hours_alive` used to be `now - process start`, which counted the entire
        duration of a game crash (the gap reappeared as a jump the moment the
        bridge answered again) and every second before the bridge had ever
        answered at all. Accumulating per believed tick, capped at
        PLAY_GAP_MAX_S, means the number can only ever be time this loop watched
        the game be up.
        """
        now = time.monotonic()
        if not live:
            self._last_live_state_at = 0.0  # the streak is broken; start fresh
            return
        if self._last_live_state_at:
            self.totals.add_play_seconds(now - self._last_live_state_at)
        self._last_live_state_at = now

    def _heartbeat(self, state: GameState | None) -> None:
        live = self._believe_state(state)
        self._accrue_play_time(live)
        now = time.monotonic()
        if now - self._last_stats < STATS_INTERVAL_S:
            return
        if not live:
            # `heartbeat_at` is the site's ONLY liveness signal (CONTRACTS §5),
            # so it may only be refreshed while we can vouch for the game being
            # up. It used to be written on the no-snapshot path too, which put a
            # fresh "ON THE AIR" next to an empty HUD for as long as the game
            # was stuck. Nothing is written now: the last honest row simply ages
            # out, and the site says OFF AIR after its own 60 s threshold — long
            # enough that a normal loading screen never trips it.
            log.info(
                "heartbeat withheld: no current snapshot to publish",
                extra={"kv": {"blocked_screen": self._screen_blocked, "state": state is not None}},
            )
            return
        assert state is not None  # `live` implies it; keeps the type checker honest
        self._last_stats = now
        hud: dict[str, Any] = {
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
        self.totals.adopt_budget(self.governor.snapshot())
        self.totals.save()
        self.writer.upsert_stats(
            {
                "deaths": self.counters["deaths"],
                "busted": self.counters["busted"],
                "missions_passed": self.counters["missions_passed"],
                # Time the game was observed up and answering — not process
                # uptime, and lifetime rather than per-process, like the
                # counters beside it on the page.
                "hours_alive": round(self.totals.played_hours, 3),
                # Really today (UTC), and really the trailing hour: see
                # BudgetGovernor.day_usd / cost_per_hour_usd.
                "cost_today_usd": round(self.governor.day_usd(), 4),
                "cost_per_hour_usd": round(self.governor.cost_per_hour_usd(), 4),
                "governor_level": self.governor.level,
                # drives the site's offline banner: stale > 60 s => offline (§5)
                "heartbeat_at": datetime.now(UTC).isoformat(),
                "current_goal": self.goal_text,
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
            # T2. This used to be a bare `self.bridge.post_task("stop", {})` —
            # the ONE path in the harness that put a CONTRACTS §1 MOVEMENT task
            # on the wire without asking the movement wheel, and therefore the
            # one hole in "the wheel is the only path to movement". `stop` is a
            # movement task by every definition that matters: it clears whatever
            # the current holder had running, so a break starting mid-goal
            # cancelled a roam step out from under an owner that never found
            # out.
            #
            # `force_idle` is the honest expression of what a break IS — nobody
            # drives, and whatever was running is cancelled through its own
            # preempt hook, exactly as for a cutscene or a death — and it leaves
            # `idle` holding an open lease, whose token is what `_execute_action`
            # then accepts. Down or not-ready: he takes the break either way;
            # the game is not going anywhere without a task.
            # No `contextlib.suppress(BridgeError)` any more: `_execute_action`
            # already swallows every bridge failure into a log line, which is
            # exactly what the old suppress was there for.
            # This runs BEFORE `_reflex` opens the movement tick and the loop
            # `continue`s straight after, so open and close one here: without
            # it the `stop` below is logged under the previous iteration's
            # tick number, beside whichever owner posted then (verify audit).
            self.wheel.begin_tick()
            self.wheel.force_idle(f"break started ({planned}s)")
            self._execute_action("stop", {}, self.wheel.token_for("idle"))
            self.wheel.end_tick()
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

                # Runs unconditionally, every tick, ahead of everything else:
                # a frozen script thread (pause menu / MISSION FAILED / retry
                # prompt) means `state` itself is stale, so this cannot wait
                # its turn behind dead/arrested/cutscene gating built for a
                # ticking game. Bridge tasks cannot help here — only a real
                # SendInput keypress can (confirmed live: SendKeys does
                # nothing, GTA V discards synthetic window messages).
                stall_key = self.blocking_screen_watchdog.feed(state)
                was_blocked, self._screen_blocked = (
                    self._screen_blocked,
                    self.blocking_screen_watchdog.blocked,
                )
                if self._screen_blocked and not was_blocked:
                    # Entering the blocked state, once. Whatever mission he was
                    # in is over or about to be retried, and the objective he
                    # was chasing is stale - the measured failure was a tail of
                    # a companion who was already gone. Drop both so nothing
                    # resumes chasing a dead objective after the retry.
                    self.current_mission = None
                    self.mission_follower.reset()
                    log.warning(
                        "the game appears to be on a modal screen (a mission has "
                        "probably failed); /state is frozen, so tasks are "
                        "suppressed and stale mission context is cleared"
                    )
                if stall_key is not None:
                    if self.primitives is not None:
                        self.primitives.press_key(stall_key)
                    else:
                        log.error(
                            "script thread appears stalled but SendInput is "
                            "unavailable on this platform; cannot clear the "
                            "blocking screen",
                            extra={"kv": {"key": stall_key}},
                        )

                # Normal speed is the show's default; put it back if anything
                # left the world slowed and the game is not the one doing it.
                self._guard_timescale(state)

                objective_hash = self._pump_screen_grab()
                delta = self.perceptor.observe(state, objective_hash)
                self.mood.observe("quiet")

                if self._handle_breaks(state):
                    self._heartbeat(state)
                    self._maybe_flush()
                    self._stop.wait(5.0)
                    continue

                # Read once per tick; `_execute_action` is the single choke
                # point that refuses any bridge task while this is true.
                self._cutscene_active = state.mission.cutscene_active
                self._switch_in_progress = state.player.switch_in_progress
                self._retry_in_flight = state.mission.retry_in_flight
                self._player_down = state.player.dead or state.player.arrested
                self._mission_active = state.mission.active
                self._wanted_now = state.player.wanted

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

                if self._player_down or self._retry_in_flight:
                    # Dead/arrested: there is nothing to decide (he cannot
                    # move, fight or drive), so a decision now would just pay
                    # for an action `_execute_action` suppresses anyway.
                    # `_pending_big_event` is deliberately left set (not
                    # popped here) — the first decision once he is back up
                    # will still see it (e.g. the `death` itself) and react,
                    # rather than losing the beat entirely.
                    #
                    # v1.11 `mission.retry_in_flight`: a checkpoint reload is
                    # in progress, i.e. the world this tick's snapshot
                    # describes is about to be discarded and replaced. CONTRACTS
                    # says "suppress tasks and commentary while true" — unlike
                    # `cutscene_active` (which still lets the director react to
                    # what it is watching), a retry has nothing worth reacting
                    # to, so BOTH tiers are skipped here, not just tactical.
                    pass
                else:
                    big_event, self._pending_big_event = self._pending_big_event, None

                    director_trigger = self.director_cadence.should_fire(now, big_event, level)
                    if director_trigger is not None:
                        result = self._think("director", state, delta, director_trigger)
                        self.director_cadence.fired(now)
                        if result is not None:
                            self._apply_decision("director", result, state, delta, big_event)
                    else:
                        tactical_trigger = self.tactical_cadence.should_fire(now, delta, level)
                        # v1.11 `player.switch_in_progress` gates this call
                        # exactly like `mission.cutscene_active` already did:
                        # the game owns the camera and the body, and this is
                        # the real signal behind narrating "wrong body /
                        # waiting for the switch" instead of just sitting
                        # through it quietly.
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
                                # `idle` is the bottom rung of the ladder and it
                                # never needs the wheel: every one of
                                # IdlePicker's behaviours is a §2 primitive
                                # (`look_around`, `radio`, `wait`, `horn`), so
                                # it posts no task and preempts nothing. Noted
                                # rather than acquired so the log still says
                                # who filled the tick.
                                self.wheel.note("idle", idle.action["type"])
                                self._execute_action(idle.action["type"], idle.action["params"])

                # All three run AFTER the brain, so a decision always gets first
                # refusal on the tick and nothing posts a task just to have it
                # preempted a few milliseconds later. Among themselves the day
                # plan goes first: it decides whether this is a roam block or a
                # mission block, and `_drive_activities` gates on that answer —
                # reading last tick's answer would let an activity start on the
                # very tick the plan switched, only to be ended one tick later.
                self._drive_day_plan(state)
                self._drive_activities(state)
                self._drive_mission_objective(state)
                # Close the movement tick: a tick nobody claimed is the shape
                # of the observed failure, so it is recorded rather than
                # leaving the previous owner apparently still driving.
                self.wheel.end_tick()

                self._heartbeat(state)
                self._maybe_flush()
                elapsed = time.monotonic() - loop_started
                self._stop.wait(max(0.05, poll_interval - elapsed))
        finally:
            log.info("harness stopping")
            self._end_activity_if_running("shutdown")
            self.writer.record_event("session_end", {"reason": "shutdown"})
            # `sessions.ended_at` (§5) was never written by anything, so every
            # session that has ever run reads as still live and a crash was
            # indistinguishable from a clean stop. Buffered before the single
            # flush below, so a clean stop taken while Supabase is unreachable
            # still closes the session out when the queue is replayed.
            self.writer.update_session_end(self.session_id, datetime.now(UTC).isoformat())
            self.totals.save()
            self._clear_current_session()
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
