#!/usr/bin/env python3
"""End-to-end demo: synthesise a state stream, replay it, run funcheck.

**This is a SYNTHETIC DEMONSTRATION of the offline pipeline, not a fixture and
not a claim about real gameplay.** CLAUDE.md rule 1 restricts *recordings*
(`harness/tests/fixtures/*.json`) to real sessions; nothing here is written
there, and nothing here is presented as a recording. What this generates is
exactly the same kind of thing `tests/support/states.py::make_state` already
produces for unit tests — pure-function `GameState` objects — just strung
into a longer timeline and pushed through the real recorder file format so
the whole pipeline (`record_state.py`'s `.jsonl` shape -> `replayer.
replay_from_jsonl` -> `funcheck.py`) is exercised exactly the way a real
recording would be, once one exists.

**The "world" is a minimal, honest reactive model, not a physics engine.**
After every tick this script looks at what the replay just posted and
reflects only what CONTRACTS says the bridge would report: `set_waypoint` is
`done` on the very next poll (CONTRACTS §1 says so explicitly); a `drive_to`/
`walk_to` is graded as arrived-on-the-next-tick (an honest simplification —
travel time is not simulated, arrival is); `enter_nearest_vehicle` seats him.
It never invents an outcome CONTRACTS does not promise (no teleport, no
free money, no scripted "he found it interesting").

Usage::

    python harness/tools/soak.py --minutes 12 --out-dir /tmp/wasted-soak
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

_HARNESS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HARNESS_ROOT))
sys.path.insert(0, str(_HARNESS_ROOT / "tests"))

from support.replayer import ReplayHarness, replay_from_jsonl  # noqa: E402
from support.states import make_state  # noqa: E402

from tools import funcheck as fc  # noqa: E402
from wasted_harness.bridge_client import GameState  # noqa: E402

POLL_S = 1.0 / 3.0  # CONTRACTS' own 2-4 Hz poll band, mid-point


class World:
    """Tracks the ground truth the next synthetic `/state` reflects.

    Reacts only to what the LAST tick actually posted (`h.log.records` since
    the previous call) — never invents a beat of its own. Every reaction
    mirrors CONTRACTS.md exactly; see the module docstring.
    """

    def __init__(self) -> None:
        self.pos = (-200.0, -800.0, 30.0)
        self.in_vehicle = True
        self.vehicle: dict[str, Any] | None = {
            "handle": 1, "model": "blista", "display_name": "Blista",
            "class": "Compacts", "speed": 0.0, "health": 1000.0,
            "upside_down": False, "in_water": False, "stopped_for_s": 30.0,
        }
        self.health = 200
        self.wanted = 0
        self.dead = False
        #: Honest `vehicle.speed`/`stopped_for_s`, CONTRACTS-shaped: moving
        #: while a never-completing drive task is `running`, stationary
        #: otherwise. Without this the vehicle sat at speed=0/stopped_for_s=30
        #: THE WHOLE RUN regardless of what was posted, and `VehicleController`
        #: (fed real `/state` every tick) correctly read that as a car that
        #: will not move and ran its own stuck-recovery ladder over it.
        self._stopped_for_s = 30.0
        self.interior: tuple[int, float] | None = None
        self.last_outdoor: tuple[float, float, float] | None = None
        self.mission_active = False
        self.task_id: str | None = None
        self.task_type: str | None = None
        self.task_status = "idle"
        self._last_seen_record_index = 0
        self._heat_for_s = 0.0
        self._cold_for_s = 0.0
        self._flee_for_s = 0.0
        self._runup_armed = False
        self._runup_for_s = 0.0
        self._airborne_polls = 0
        self._travel: tuple[tuple[float, float, float], int] | None = None
        self._rounds_spent = 0

    def _vehicle_for_state(self) -> dict[str, Any] | None:
        if self.vehicle is None:
            return None
        moving = self.task_status == "running" and (
            self.task_type in self._NEVER_COMPLETES or self._travel is not None
        )
        v = dict(self.vehicle)
        v["speed"] = 15.0 if moving else 0.0
        v["in_air"] = self._airborne_polls > 0
        v["stopped_for_s"] = 0.0 if moving else self._stopped_for_s
        return v

    #: CONTRACTS v1.14 `player.weapon` as the bridge reports it after its default
    #: `ammunation` loadout: the three tracked weapons, owned. Without it the
    #: world is unarmed and every heat goal falls to its unarmed branch, which is
    #: not the stream the operator will see.
    _WEAPON: ClassVar[dict[str, Any]] = {
        "name": "Pistol", "class": "gun", "ammo": 60,
        "owned": {"Pistol": 60, "MicroSMG": 90, "PumpShotgun": 24}, "loadout": "ammunation",
    }

    def state(self, tick: int) -> Any:
        body = self._state_body(tick).model_dump(by_alias=True)
        weapon = dict(self._WEAPON)
        weapon["owned"] = {k: max(0, v - self._rounds_spent) for k, v in self._WEAPON["owned"].items()}
        weapon["ammo"] = weapon["owned"][weapon["name"]]
        body["player"]["weapon"] = weapon
        return GameState.model_validate(body)

    #: Two bystanders on the pavement, the way any Los Santos street has them.
    #: Without a single ped in `nearby.peds` no heat goal can ever open with
    #: its violence verb (they all target a NAMED ped), so the soak was
    #: measuring their unarmed branches only.
    _BYSTANDERS: ClassVar[list[dict[str, Any]]] = [
        {"handle": 901, "model": "a_m_y_hipster_01", "distance": 9.0,
         "relationship": "neutral", "pos": {"x": 0.0, "y": 0.0, "z": 0.0}},
        {"handle": 902, "model": "a_f_y_tourist_01", "distance": 14.0,
         "relationship": "neutral", "pos": {"x": 0.0, "y": 0.0, "z": 0.0}},
    ]

    def _state_body(self, tick: int) -> Any:
        return make_state(
            nearby_peds=[
                {**b, "pos": {"x": self.pos[0] + b["distance"], "y": self.pos[1], "z": self.pos[2]}}
                for b in self._BYSTANDERS
            ],
            tick=tick,
            pos=self.pos,
            in_vehicle=self.in_vehicle,
            vehicle=self._vehicle_for_state(),
            health=self.health,
            wanted=self.wanted,
            dead=self.dead,
            interior=self.interior,
            last_outdoor=self.last_outdoor,
            mission_active=self.mission_active,
            task_id=self.task_id,
            task_type=self.task_type,
            task_status=self.task_status,
        )

    #: CONTRACTS §1: `wander_drive`/`flee_police`/`combat_hated_targets_around`/
    #: `seek_cover`/`follow_entity` "never complete[s]; runs until preempted" —
    #: grading them `done` the tick they are posted would be inventing an
    #: outcome the contract does not promise. They stay `running` and this
    #: model instead advances distance under them each tick, the same honest
    #: substitute `drive_to`/`walk_to` arrival already is for real travel time.
    _NEVER_COMPLETES = frozenset(
        {"wander_drive", "flee_police", "combat_hated_targets_around", "seek_cover", "follow_entity"}
    )
    #: m/tick at the ~3 Hz poll rate — a plausible, unremarkable cruising speed
    #: (~15 m/s / 54 km/h), used only to give `wander_drive` honest distance
    #: coverage for goals like `roam_the_block` that grade on it.
    _WANDER_STEP_M = 5.0
    #: Verbs the police answer. One star the moment he fires (the game's own
    #: rule for gunfire near witnesses), a second after :data:`_HEAT_2_S` if he
    #: keeps going; `flee_police` held for :data:`_LOSE_S` clears them, and so
    #: does :data:`_COLD_S` of nothing. Deliberately coarse — it exists so a
    #: heat goal can COMPLETE in this world, not to model the wanted system.
    _VIOLENCE = frozenset({"shoot_at", "drive_by", "fight_ped", "combat_hated_targets_around"})
    #: A ramp. `big_jump` drives to an approach `rushed` at 28 m/s and then
    #: keeps the pedal down (`wander_drive rushed`); after :data:`_RUNUP_S` of
    #: that, this world lets the car leave the ground for two polls — the same
    #: class of grant as instant `drive_to` arrival: the outcome the action
    #: physically produces, never an outcome with no action behind it. Once
    #: per approach, so a missed jump is a timeout, not a second free one.
    _RUNUP_S = 5.0
    #: Longest a single `drive_to`/`walk_to` may take in this world.
    _TRAVEL_CAP_S = 45.0
    #: Rounds a violence verb costs. The goals graded on "he actually fired"
    #: (`drive_by_run`, `armed_rampage_block`) read `player.weapon.owned`
    #: deltas, and a world where the magazine never empties can only ever
    #: time them out.
    _ROUNDS_PER_BURST = 10
    _AIRBORNE_POLLS = 2
    _HEAT_2_S = 8.0
    _LOSE_S = 30.0
    _COLD_S = 90.0

    def react(self, h: ReplayHarness) -> None:
        """Apply the effect of whatever was posted since the last call, THEN
        advance any still-`running` never-completing task by one tick of travel."""
        new_records = h.log.records[self._last_seen_record_index :]
        self._last_seen_record_index = len(h.log.records)
        posted = [r for r in new_records if r["kind"] == "posted_task" and r.get("movement")]
        if posted:
            last = posted[-1]
            ttype, params = last["type"], last["params"]
            self.task_id, self.task_type = last.get("task_id"), ttype
            self._travel = None
            if ttype == "set_waypoint":
                self.task_status = "done"  # CONTRACTS §1: immediate, done same tick
            elif ttype in ("drive_to", "walk_to"):
                # Travel takes time: at 15 m/s (3 m/s on foot), capped so a
                # cross-map landmark does not eat the soak. Instant arrival
                # stacked every goal completion into the same minute and
                # measured an idle ratio no real drive produces.
                target = (params["x"], params["y"], params.get("z", self.pos[2]))
                dist = ((target[0] - self.pos[0]) ** 2 + (target[1] - self.pos[1]) ** 2) ** 0.5
                speed = 15.0 if ttype == "drive_to" else 3.0
                self._travel = (target, max(1, min(int(self._TRAVEL_CAP_S / POLL_S), int(dist / speed / POLL_S))))
                self.task_status = "running"
                # A rushed, fast drive_to is `big_jump`'s approach: arm the ramp.
                self._runup_armed = (
                    ttype == "drive_to" and params.get("style") == "rushed"
                    and float(params.get("speed_mps", 0.0)) >= 20.0
                )
                self._runup_for_s = 0.0
            elif ttype == "enter_nearest_vehicle":
                self.in_vehicle = True
                if self.vehicle is None:
                    # Boarded whatever the reflex found nearby — a plain,
                    # driveable car, honestly generic (CONTRACTS names no
                    # specific vehicle a "nearest" search would find).
                    self.vehicle = {
                        "handle": 2, "model": "blista", "display_name": "Blista",
                        "class": "Compacts", "speed": 0.0, "health": 1000.0,
                        "upside_down": False, "in_water": False, "stopped_for_s": 0.0,
                    }
                self.task_status = "done"
            elif ttype == "exit_vehicle":
                self.in_vehicle = False
                self.task_status = "done"
            elif ttype in self._NEVER_COMPLETES:
                self.task_status = "running"
            else:
                self.task_status = "done"
        elif self._travel is not None and self.task_status == "running":
            target, polls_left = self._travel
            x, y, z = self.pos
            step = 1.0 / polls_left
            self.pos = (x + (target[0] - x) * step, y + (target[1] - y) * step, z + (target[2] - z) * step)
            if polls_left <= 1:
                self.pos = target
                self.task_status = "done"
                self._travel = None
            else:
                self._travel = (target, polls_left - 1)
        elif self.task_type in self._NEVER_COMPLETES and self.task_status == "running":
            x, y, z = self.pos
            self.pos = (x + self._WANDER_STEP_M, y, z)
        moving = self.task_status == "running" and (
            self.task_type in self._NEVER_COMPLETES or self._travel is not None
        )
        self._stopped_for_s = 0.0 if moving else self._stopped_for_s + POLL_S
        self._react_heat(posted[-1]["type"] if posted else None)
        self._react_ramp(moving)

    def _react_ramp(self, moving: bool) -> None:
        if self._airborne_polls > 0:
            self._airborne_polls -= 1
            return
        if not (self._runup_armed and moving and self.task_type == "wander_drive"):
            return
        self._runup_for_s += POLL_S
        if self._runup_for_s >= self._RUNUP_S:
            self._runup_armed = False
            self._airborne_polls = self._AIRBORNE_POLLS

    def _react_heat(self, posted_type: str | None) -> None:
        if posted_type in self._VIOLENCE:
            self._rounds_spent += self._ROUNDS_PER_BURST
            self.wanted = max(self.wanted, 1)
            self._heat_for_s = 0.0
            self._cold_for_s = 0.0
        if self.wanted <= 0:
            return
        self._heat_for_s += POLL_S
        self._cold_for_s += POLL_S
        fleeing = self.task_type == "flee_police" and self.task_status == "running"
        self._flee_for_s = self._flee_for_s + POLL_S if fleeing else 0.0
        if self.wanted == 1 and self._heat_for_s >= self._HEAT_2_S:
            self.wanted = 2
        if self._flee_for_s >= self._LOSE_S or self._cold_for_s >= self._COLD_S:
            self.wanted = 0
            self._heat_for_s = self._cold_for_s = self._flee_for_s = 0.0


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_stream(
    minutes: float, seed: int = 3
) -> tuple[list[Any], list[str], ReplayHarness]:
    """idle car -> goal pick(s) -> death -> respawn (indoors) -> interior exit.

    Returns the harness alongside the stream, ALREADY fed — see `run()`'s own
    docstring for why its log, not a second `replay_from_jsonl` pass, is what
    funcheck actually reads."""
    total_s = minutes * 60.0
    h = ReplayHarness(seed=seed, missions_enabled=False, brain_mode="policy")
    world = World()
    states: list[Any] = []
    stamps: list[str] = []
    wall = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    tick = 0

    def step(**overrides: Any) -> None:
        nonlocal tick, wall
        tick += 1
        for k, v in overrides.items():
            setattr(world, k, v)
        st = world.state(tick)
        states.append(st)
        stamps.append(_iso(wall))
        h.clock.advance(POLL_S)
        h.feed(st, wall_ts=stamps[-1])
        world.react(h)
        wall += timedelta(seconds=POLL_S)

    # Phase 1: idle car, ~15s — the starting condition the demo asks for.
    # Short on purpose: F1 (idle ratio) allows only 5% of any 10-minute
    # window idle, i.e. 30s/600s, and this phase is the only place in the
    # whole run nothing is posted, so it has to fit inside that budget for
    # the rest of the run to demonstrate a genuinely "fun to watch" show.
    for _ in range(int(15.0 / POLL_S)):
        step()

    # Phase 2: let free roam run — goals get picked, the world reacts to
    # whatever gets posted (set_waypoint/drive_to -> arrival), goals complete
    # and new ones get picked — for about a third of the budget.
    phase2_end = 15.0 + total_s * 0.40
    while tick * POLL_S < phase2_end:
        step()

    # Phase 3: death. A real drop to zero HP, `dead=True` — `_reflex` reacts
    # to the true edge (`delta.died`), same as it would to a real one.
    step(dead=True, health=0)
    for _ in range(6):
        step(dead=True, health=0)

    # Phase 4: respawn, indoors, with `last_outdoor` set — the exact CONTRACTS
    # v1.12 shape `InteriorEscape` rung 1 answers (see roam.py's own
    # docstring): a fresh interior id, no prior transition recorded THIS run.
    respawn_pos = (215.0, -805.0, 30.0)
    outdoor_pos = (230.0, -805.0, 30.0)
    step(
        dead=False, health=200, in_vehicle=False, vehicle=None,
        pos=respawn_pos, interior=(11, 0.5), last_outdoor=outdoor_pos,
        task_id=None, task_type=None, task_status="idle",
    )
    # A few ticks of the walk still in flight (interior unchanged, running).
    for _ in range(3):
        step(task_status="running")
    # He is through the door: `interior` -> null, position at the doorway.
    step(interior=None, pos=outdoor_pos, task_status="done")

    # Phase 5: back to ordinary free roam for the remaining budget.
    while tick * POLL_S < total_s:
        step(in_vehicle=world.in_vehicle)

    return states, stamps, h


def run(minutes: float, out_dir: Path, seed: int = 3) -> int:
    """Build the stream, replay it, run funcheck.

    **Why the SCRIPTING harness's own log is what funcheck reads, not a
    second `replay_from_jsonl` pass over the saved file.** Task ids
    (`last_task.id`) are only meaningful relative to whichever bridge issued
    them — for a REAL recording there is exactly one bridge, so
    `replay_from_jsonl` faithfully reproduces "is this the task I am
    waiting on" for real. A synthetic stream generated by having ONE
    `ReplayHarness` react to its own postings and THEN handed to a SECOND,
    independent `ReplayHarness` breaks that: the second harness issues its
    OWN fresh id sequence ("t-1", "t-2", ...) that has no relationship to the
    ids baked into the recorded states ("t-6", "t-7", ...), so its
    `ActivityRunner`/`MissionFollower` never recognise a `done` confirmation
    as belonging to the step they just issued, and a goal that would clearly
    finish stalls for its full timeout instead. This is a property of using
    TWO simulated bridges for one demo, not a bug in the replayer's fidelity
    to production code — flagged here, and in the NOT VERIFIED list, rather
    than worked around by fudging the demo.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    states, stamps, h = build_stream(minutes, seed=seed)

    states_path = out_dir / "soak.states.jsonl"
    with states_path.open("w", encoding="utf-8") as fh:
        for st, ts in zip(states, stamps, strict=True):
            fh.write(
                json.dumps({"ts": ts, "state": json.loads(st.model_dump_json(by_alias=True))})
                + "\n"
            )
    print(f"[soak] synthesised {len(states)} states ({minutes:.1f} min) -> {states_path}")

    log_path = out_dir / "soak.replay.jsonl"
    states_out_path = out_dir / "soak.replay_states.jsonl"
    h.log.write_jsonl(log_path)
    h.log.write_states_jsonl(states_out_path)
    print(f"[soak] replayed -> {log_path} ({len(h.log.records)} records)")

    # A SEPARATE, honest structural check: `replay_from_jsonl` (the real
    # entry point a real recording would go through) loads the recorder-
    # format file and runs to completion with no crash. Its own log is NOT
    # used for the table above, for the reason in this function's docstring.
    reloaded = replay_from_jsonl(states_path, seed=seed, missions_enabled=False, brain_mode="policy")
    print(
        f"[soak] recorder-format round trip: {len(reloaded.states)} states reloaded and "
        f"replayed without error ({len(reloaded.records)} records produced by the fresh harness)"
    )

    data = fc.ReplayData.load(log_path, states_out_path)
    print(
        f"\nfuncheck: {len(data.states)} states, {len(data.records)} records, "
        f"{data.duration_s:.0f}s ({data.duration_hours:.2f}h) covered\n"
    )
    results = fc.run_all(data)
    print(fc.format_table(results))
    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed} passed, {len(results) - passed} failed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=float, default=12.0)
    parser.add_argument(
        "--out-dir", type=Path,
        default=Path(tempfile.gettempdir()) / "wasted-soak",
        help="default: a fresh dir under the OS temp dir",
    )
    parser.add_argument("--seed", type=int, default=3)
    args = parser.parse_args(argv)
    return run(args.minutes, args.out_dir, seed=args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
