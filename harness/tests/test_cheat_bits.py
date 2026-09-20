"""Cheat bits (CLAUDE.md rule 5, rewritten 2026-09-04): `burn_the_city` and the ARSENAL.

What is pinned here, in order:

* the CADENCE POLICY for any `Goal.cheat` goal — L3, chaos 2.0, cooldown >= 20 min,
  never two in a row, withheld when hurt/wanted/in a mission, and behind the
  operator's `WASTED_CHEATS` switch;
* that the bit is only ever offered on a bridge that REPORTS `effects` (a cheat
  the state cannot see is a cheat `done_when` cannot grade);
* that `done_when` is an honest reading of `/state`: kit rounds actually gone,
  folded by the engine from `player.weapon.owned` while `effects.arsenal` is on;
* that the announcement rides in the note and in both event payloads;
* the harness half of the restore guarantee: `Harness._end_activity_if_running`
  posts `/arsenal off` for a cheat goal on EVERY outcome, and
  `Harness._begin_roam_goal` refuses to run the bit when the bridge refuses the
  cheat. Real `Harness` methods bound on a bare object, the idiom
  `test_main_loop_resilience.py::_harness` established.

States are explicit builders (`armed_state` + `_effects`), never fixtures.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from test_roam import (
    ARSENAL_KIT,
    FakeClock,
    _effects,
    _weapon,
    armed_state,
    make_state,
    ped,
    veh,
)
from wasted_harness.behavior.activities import ActivityRunner
from wasted_harness.behavior.recovery import ARSENAL_GUNS, loaded_gun_for
from wasted_harness.behavior.roam import (
    ARSENAL_TTL_S,
    BURN_MIN_ROUNDS,
    BURN_S,
    BURN_THROWS,
    CATALOG,
    GOALS_BY_ID,
    ROAM_CATEGORIES,
    RoamEngine,
    arsenal_active,
    arsenal_weapons,
    effects_reported,
)
from wasted_harness.behavior.vehicle import MovementWheel
from wasted_harness.brain.schemas import ACTION_TYPES, ActionModel
from wasted_harness.bridge_client import (
    BridgeApiError,
    BridgeClient,
    BridgeDownError,
    GameState,
)
from wasted_harness.main import Harness

FULL_LOADOUT = {"Pistol": 60, "MicroSMG": 90, "PumpShotgun": 24}


def _burn_state(**over: Any) -> GameState:
    """On foot, healthy, no stars, a 1.9.0 bridge with the arsenal OFF, a parked
    car and a bystander in range: the state `burn_the_city` is offered in."""
    base: dict[str, Any] = dict(
        in_vehicle=False,
        health=200,
        weapon=_weapon(owned=dict(FULL_LOADOUT)),
        effects=_effects(active=False),
        nearby_vehicles=[veh(12, "sultan", "Sedans", 18.0, pos=(18.0, 0.0, 0.0))],
        nearby_peds=[ped(handle=44, model="a_m_y_hipster_01", distance=9.0)],
    )
    base.update(over)
    return armed_state(**base)


def _arsenal_on_state(spent: dict[str, int] | None = None, **over: Any) -> GameState:
    """The tick AFTER the grant: `effects.arsenal.active` and the kit in `owned`,
    minus whatever `spent` says has gone."""
    owned = dict(FULL_LOADOUT)
    for name, count in ARSENAL_KIT.items():
        owned[name] = count - (spent or {}).get(name, 0)
    base: dict[str, Any] = dict(
        in_vehicle=False,
        health=200,
        weapon=_weapon(owned=owned),
        effects=_effects(active=True, expires_in_s=170.0),
        nearby_vehicles=[veh(12, "sultan", "Sedans", 18.0, pos=(18.0, 0.0, 0.0))],
        nearby_peds=[ped(handle=44, model="a_m_y_hipster_01", distance=9.0)],
    )
    base.update(over)
    return armed_state(**base)


def _l3(seed: int = 7) -> tuple[RoamEngine, FakeClock]:
    clock = FakeClock()
    return RoamEngine(random.Random(seed), clock=clock, level=3), clock


def _offered(e: RoamEngine, state: GameState) -> set[str]:
    e.observe(state)
    return {o.id for o in e.available(state)}


# --- the cadence policy, as catalog invariants ---------------------------------


def test_every_cheat_goal_follows_the_settled_cadence_policy() -> None:
    cheats = [g for g in CATALOG if g.cheat]
    assert cheats, "burn_the_city is the first cheat bit and must be in the catalog"
    for goal in cheats:
        assert goal.level == 3, goal.id
        assert goal.chaos_cost == 2.0, goal.id
        assert goal.cooldown_s >= 20 * 60.0, goal.id
        assert goal.calm is False, f"{goal.id}: a cheat bit is withheld when hurt"
        assert goal.wants_heat, f"{goal.id}: rockets are stars; the override must not kill it"
        assert not goal.override_only and not goal.fallback and not goal.handoff, goal.id
        assert goal.category in ROAM_CATEGORIES, goal.id
    assert GOALS_BY_ID["burn_the_city"].cheat


def test_the_bit_is_never_offered_on_a_bridge_that_cannot_report_the_cheat() -> None:
    """No `effects` on the wire -> no cheat. A pre-1.9.0 bridge cannot say the
    arsenal is on, so `done_when` could never read whether it was."""
    e, _ = _l3()
    old = _burn_state()
    body = old.model_dump(by_alias=True)
    body.pop("effects")
    old = GameState.model_validate(body)
    assert not effects_reported(old)
    assert "burn_the_city" not in _offered(e, old)
    assert "burn_the_city" in _offered(_l3()[0], _burn_state())


@pytest.mark.parametrize(
    "why, over",
    [
        ("arsenal already on", {"effects": _effects(active=True)}),
        ("wanted", {"wanted": 1}),
        ("in a mission", {"mission_active": True}),
        ("hurt", {"health": 80}),
        ("in a car", {"in_vehicle": True, "vehicle": {"class": "Sedans", "model": "sultan"}}),
        ("nothing in range", {"nearby_vehicles": [], "nearby_peds": []}),
    ],
)
def test_the_gates(why: str, over: dict[str, Any]) -> None:
    e, _ = _l3()
    assert "burn_the_city" not in _offered(e, _burn_state(**over)), why


def test_the_chaos_ladder_withdraws_it_below_l3() -> None:
    for level in (1, 2):
        e = RoamEngine(random.Random(3), clock=FakeClock(), level=level)
        assert "burn_the_city" not in _offered(e, _burn_state()), level
    assert "burn_the_city" in _offered(_l3()[0], _burn_state())


def test_the_operator_switch_pulls_every_cheat_off_the_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WASTED_CHEATS", "off")
    e, _ = _l3()
    assert not e.cheats_enabled
    assert "burn_the_city" not in _offered(e, _burn_state())
    monkeypatch.setenv("WASTED_CHEATS", "on")
    e2, _ = _l3()
    assert e2.cheats_enabled
    assert "burn_the_city" in _offered(e2, _burn_state())


def test_never_two_cheat_bits_in_a_row() -> None:
    """The last goal that ran being a cheat blocks the next cheat, on top of
    (and independently of) the cooldown."""
    e, clock = _l3()
    state = _burn_state()
    e.observe(state)
    picked = e.pick(state, goal_id="burn_the_city")
    assert picked is not None and picked[0].goal.id == "burn_the_city"
    e.close("completed")
    clock.tick(31 * 60.0)  # past the 30-minute cooldown
    assert "burn_the_city" not in _offered(e, state), "a cheat straight after a cheat"
    # Something else ran in between: the cheat is legal again.
    e._recent_ids.append("steal_nice_car")
    e._last_category = "acquisition"
    assert "burn_the_city" in _offered(e, state)


# --- the plan ---------------------------------------------------------------------


def test_the_plan_throws_first_then_fights_with_the_launchers_then_runs() -> None:
    e, _ = _l3()
    state = _burn_state()
    e.observe(state)
    plan, snap = GOALS_BY_ID["burn_the_city"].plan(state, e.view)
    assert [s["type"] for s in plan] == ["throw_at", "fight_ped", "walk_to"]
    assert plan[0]["params"] == {"handle": 12, "count": BURN_THROWS}, "the parked car first"
    assert plan[1]["params"] == {"handle": 44, "weapon": "armed"}
    assert plan[2]["params"]["run"] is True
    for step in plan:
        assert step["type"] in ACTION_TYPES
        ActionModel.model_validate(step)  # the schema accepts every step as-is
    assert snap["target"] == 12 and "start" in snap


def test_with_nobody_in_range_the_plan_is_the_car_and_the_run() -> None:
    e, _ = _l3()
    state = _burn_state(nearby_peds=[])
    e.observe(state)
    plan, _ = GOALS_BY_ID["burn_the_city"].plan(state, e.view)
    assert [s["type"] for s in plan] == ["throw_at", "walk_to"]


# --- done_when: kit rounds gone, read from /state -----------------------------


def _pick_burn() -> tuple[RoamEngine, FakeClock]:
    e, clock = _l3()
    state = _burn_state()
    e.observe(state)
    assert e.pick(state, goal_id="burn_the_city") is not None
    return e, clock


def test_not_done_until_kit_rounds_are_actually_gone_and_the_scene_has_run() -> None:
    e, clock = _pick_burn()
    clock.tick(1.0)
    assert e.judge(_arsenal_on_state()) is None
    assert e.current.snapshot["_arsenal_spent"] == 0
    # One rocket is not a barrage.
    clock.tick(1.0)
    assert e.judge(_arsenal_on_state(spent={"RPG": 1})) is None
    # Three rounds gone, but too early for a scene.
    clock.tick(1.0)
    assert e.judge(_arsenal_on_state(spent={"RPG": 1, "Grenade": 2})) is None
    assert e.current.snapshot["_arsenal_spent"] == BURN_MIN_ROUNDS
    clock.tick(BURN_S)
    assert e.judge(_arsenal_on_state(spent={"RPG": 1, "Grenade": 2})) == "done"


def test_the_bridge_taking_the_kit_back_ends_a_bit_whose_rounds_were_spent() -> None:
    """TTL / cutscene / edge: `effects.arsenal.active` goes false and the kit
    leaves `owned`. The spent total is kept, and the goal completes without
    waiting for the clock."""
    e, clock = _pick_burn()
    clock.tick(1.0)
    assert e.judge(_arsenal_on_state(spent={"GrenadeLauncher": 3})) is None
    clock.tick(1.0)
    after = _burn_state(effects=_effects(active=False, last_cleared="ttl_expired"))
    assert not arsenal_active(after) and arsenal_weapons(after) == ()
    assert e.judge(after) == "done"
    assert e.current is None or True  # judge does not close; main does
    assert e.current.snapshot["_arsenal_spent"] == 3


def test_a_bit_in_which_nothing_was_fired_never_reads_as_done() -> None:
    e, clock = _pick_burn()
    clock.tick(1.0)
    e.judge(_arsenal_on_state())
    clock.tick(BURN_S + 1.0)
    # Arsenal expired with the kit untouched: honest answer is anything BUT
    # "done" (the stillness watchdog may well say "escalate" here; fine).
    assert e.judge(_burn_state(effects=_effects(active=False, last_cleared="ttl_expired"))) != "done"
    clock.tick(ARSENAL_TTL_S + 60.0)
    assert e.judge(_burn_state(effects=_effects(active=False))) == "timeout"


def test_picking_up_ammo_mid_bit_cannot_mask_a_shot_or_go_negative() -> None:
    e, clock = _pick_burn()
    clock.tick(1.0)
    e.judge(_arsenal_on_state(spent={"AssaultRifle": 30}))
    assert e.current.snapshot["_arsenal_spent"] == 30
    clock.tick(1.0)
    more = _arsenal_on_state(spent={"AssaultRifle": -20})  # a magazine picked up
    e.judge(more)
    assert e.current.snapshot["_arsenal_spent"] == 30


# --- the announcement -------------------------------------------------------------


def test_the_note_and_both_event_payloads_say_it_is_a_cheat() -> None:
    e, _ = _pick_burn()
    note = e.note()
    assert "CHEAT ON" in note and "ARSENAL" in note and "cheat" in note.lower()
    extra = e.close("completed")
    assert extra["cheat"] == "arsenal" and extra["goal_id"] == "burn_the_city"
    # And an ordinary goal does not carry the key.
    e2, _ = _l3()
    state = _burn_state()
    e2.observe(state)
    assert e2.pick(state, goal_id="armed_rampage_block") is not None
    assert "cheat" not in e2.close("completed")


# --- the recovery predictor mirrors the bridge's arsenal-first rule -----------


def test_loaded_gun_for_answers_armed_for_any_kit_gun_with_rounds() -> None:
    dry = {"Pistol": 0, "MicroSMG": 0, "PumpShotgun": 0}
    assert not loaded_gun_for(armed_state(weapon=_weapon(ammo=0, owned=dict(dry))), 7.0)
    for gun in ARSENAL_GUNS:
        owned = dict(dry, **{gun: 1})
        state = armed_state(
            weapon=_weapon(ammo=0, owned=owned), effects=_effects(active=True)
        )
        assert loaded_gun_for(state, 7.0), gun
    # The kit in `owned` but the effect OFF (the bridge never reports that
    # combination, but the predictor must follow the effect, not the names).
    off = armed_state(weapon=_weapon(ammo=0, owned=dict(dry, RPG=4)), effects=_effects(active=False))
    assert not loaded_gun_for(off, 40.0)
    # Throwables are not guns for this purpose.
    grenades = armed_state(
        weapon=_weapon(ammo=0, owned=dict(dry, Grenade=8)), effects=_effects(active=True)
    )
    assert not loaded_gun_for(grenades, 7.0)


# --- the wire ---------------------------------------------------------------------


def test_effects_parse_and_default() -> None:
    absent = make_state()
    assert not effects_reported(absent)
    assert absent.effects.arsenal.active is False and absent.effects.arsenal.weapons == []
    on = armed_state(effects=_effects(active=True, expires_in_s=42.5, last_cleared=""))
    assert effects_reported(on) and arsenal_active(on)
    assert on.effects.arsenal.expires_in_s == 42.5
    assert set(arsenal_weapons(on)) == set(ARSENAL_KIT)


def test_set_arsenal_posts_the_documented_body_and_clamps_the_ttl() -> None:
    client = BridgeClient("http://127.0.0.1:1", timeout_s=0.2, connect_retries=0)
    sent: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        sent.append((method, path, json_body))
        if json_body and json_body.get("on"):
            return {"active": True, "expires_in_s": 180.0, "weapons": list(ARSENAL_KIT)}
        return {"active": False, "cleared": True}

    client._request = fake_request  # type: ignore[method-assign]
    try:
        on = client.set_arsenal(True, ttl_s=9999.0)
        assert on.active and on.weapons == list(ARSENAL_KIT)
        assert sent[-1] == ("POST", "/arsenal", {"on": True, "ttl_s": 300.0})
        off = client.set_arsenal(False)
        assert not off.active and off.cleared
        assert sent[-1] == ("POST", "/arsenal", {"on": False})
        assert "throw_at" in __import__("wasted_harness.bridge_client", fromlist=["x"]).BRIDGE_TASK_TYPES
    finally:
        client.close()


def test_set_arsenal_against_a_dead_bridge_raises_bridge_down() -> None:
    client = BridgeClient("http://127.0.0.1:1", timeout_s=0.2, connect_retries=0)
    try:
        with pytest.raises(BridgeDownError):
            client.set_arsenal(True)
    finally:
        client.close()


# --- the harness half of the restore guarantee -------------------------------


class _Bridge:
    """Records /arsenal calls; `refuse` makes the grant a bridge 409."""

    def __init__(self, refuse: str | None = None) -> None:
        self.calls: list[bool] = []
        self.refuse = refuse

    def set_arsenal(self, on: bool, ttl_s: float = 180.0) -> Any:
        self.calls.append(on)
        if on and self.refuse:
            raise BridgeApiError(409, self.refuse, "refused by the bridge")
        from wasted_harness.bridge_client import ArsenalResult

        return ArsenalResult(
            active=on, expires_in_s=ttl_s if on else 0.0,
            weapons=list(ARSENAL_KIT) if on else [], cleared=not on,
        )


class _Recorder:
    #: Stands in for `SupabaseWriter`, which carries this flag; `Harness._heartbeat`
    #: reads it to refuse publishing a heartbeat over an unflushed backlog.
    unflushed = False

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record_event(self, type_: str, payload: dict[str, Any], **_kw: Any) -> None:
        self.events.append((type_, payload))


class _Planner:
    roam_preference = None

    def __init__(self) -> None:
        self.started: list[str] = []
        self.ended = 0

    def roam_activity_started(self, goal_id: str) -> None:
        self.started.append(goal_id)

    def roam_activity_ended(self) -> None:
        self.ended += 1

    def request_mission_block(self, why: str) -> None:  # pragma: no cover - not a handoff
        raise AssertionError("burn_the_city is not a handoff goal")


class _Memory:
    def log_day(self, *_a: Any, **_k: Any) -> None:
        pass


def _harness(bridge: _Bridge) -> tuple[Harness, RoamEngine, FakeClock]:
    """A bare Harness with the REAL `_begin_roam_goal`, `_end_activity_if_running`
    and `_switch_arsenal` bound, and only the collaborators they touch."""
    h = Harness.__new__(Harness)
    h.bridge = bridge
    h.writer = _Recorder()
    h.planner = _Planner()
    h.memory = _Memory()
    h.wheel = MovementWheel()
    h._roam_token = None
    h.current_goal = "burn_the_city"
    h._operator_choice = lambda chosen: chosen  # type: ignore[method-assign]
    h.activity_runner = ActivityRunner(None, random.Random(1))  # type: ignore[arg-type]
    e, clock = _l3()
    h.roam = e
    issued: list[dict[str, Any]] = []
    h._issue_activity_step = issued.append  # type: ignore[method-assign]
    h._issued = issued  # type: ignore[attr-defined]
    return h, e, clock


def test_begin_switches_the_arsenal_on_and_announces_it() -> None:
    bridge = _Bridge()
    h, e, _ = _harness(bridge)
    state = _burn_state()
    e.observe(state)
    h._begin_roam_goal(state)
    assert e.current is not None and e.current.goal.id == "burn_the_city"
    assert bridge.calls == [True]
    (type_, payload), = h.writer.events
    assert type_ == "activity_start"
    assert payload["cheat"] == "arsenal" and payload["cheat_ttl_s"] == ARSENAL_TTL_S
    assert payload["activity"] == "burn_the_city"
    assert h._issued and h._issued[0]["type"] == "throw_at"


@pytest.mark.parametrize("refusal", ["player_down", "cutscene_active", "mission_active", "arsenal_active"])
def test_a_refused_grant_ends_the_bit_before_it_starts(refusal: str) -> None:
    bridge = _Bridge(refuse=refusal)
    h, e, _ = _harness(bridge)
    state = _burn_state()
    e.observe(state)
    h._begin_roam_goal(state)
    assert bridge.calls == [True]
    assert e.current is None, "a cheat goal must not run without its cheat"
    assert h.writer.events == [], "nothing was announced because nothing started"
    assert h._issued == []
    assert h.wheel.acquire("roam", "again", lease_ticks=None) is not None, "the wheel was released"


@pytest.mark.parametrize("outcome", ["completed", "timeout", "stuck", "player_down", "preempted", "game_restarted"])
def test_every_way_the_goal_ends_restores_the_arsenal(outcome: str) -> None:
    bridge = _Bridge()
    h, e, _ = _harness(bridge)
    state = _burn_state()
    e.observe(state)
    h._begin_roam_goal(state)
    assert bridge.calls == [True]
    h._end_activity_if_running(outcome)
    assert bridge.calls == [True, False], outcome
    assert e.current is None
    end = [p for t, p in h.writer.events if t == "activity_end"]
    assert end and end[0]["cheat"] == "arsenal" and end[0]["outcome"] == outcome


def test_an_ordinary_goal_never_touches_the_arsenal() -> None:
    bridge = _Bridge()
    h, e, _ = _harness(bridge)
    h.current_goal = "armed_rampage_block"
    state = _burn_state()
    e.observe(state)
    h._begin_roam_goal(state)
    assert e.current is not None and e.current.goal.id == "armed_rampage_block"
    h._end_activity_if_running("completed")
    assert bridge.calls == []
