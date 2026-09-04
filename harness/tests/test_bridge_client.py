"""Bridge client against a REAL dead port: clear BridgeDownError, no fake data."""

import socket

import pytest

from wasted_harness.bridge_client import (
    BRIDGE_TASK_TYPES,
    BridgeClient,
    BridgeDownError,
)


def _closed_port(preferred: int = 7777) -> int:
    """The contract port if nothing listens there; otherwise another closed port."""
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket() as s:  # something IS on 7777 here; grab a fresh closed port
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def dead_client():
    port = _closed_port()
    client = BridgeClient(f"http://127.0.0.1:{port}", timeout_s=0.5, connect_retries=0)
    yield client
    client.close()


def test_get_state_raises_bridge_down_with_clear_message(dead_client: BridgeClient) -> None:
    with pytest.raises(BridgeDownError) as exc_info:
        dead_client.get_state()
    msg = str(exc_info.value)
    assert "bridge unreachable" in msg
    assert "/state" in msg
    assert "WastedBridge" in msg  # tells the operator what to check


def test_health_and_task_also_raise(dead_client: BridgeClient) -> None:
    with pytest.raises(BridgeDownError):
        dead_client.get_health()
    with pytest.raises(BridgeDownError):
        dead_client.post_task("drive_to", {"x": 0, "y": 0, "z": 0, "speed_mps": 12, "style": "normal"})
    with pytest.raises(BridgeDownError):
        dead_client.set_timescale(0.15)


def test_unknown_task_type_rejected_client_side(dead_client: BridgeClient) -> None:
    # Rejected before any network I/O — teleport etc. simply do not exist.
    with pytest.raises(ValueError, match="not a bridge task"):
        dead_client.post_task("teleport", {"x": 0, "y": 0, "z": 0})
    with pytest.raises(ValueError, match="not a bridge task"):
        dead_client.post_task("look_around", {})  # primitive, not a bridge task


def test_bridge_task_types_match_contract() -> None:
    assert BRIDGE_TASK_TYPES == (
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
        # Bridge 1.7.0 (fix-opus-b, T6). `attack_ped` is deliberately NOT here:
        # it would be the same native and the same param as `fight_ped`'s ranged
        # arm, so the weapon choice ships as `fight_ped {weapon}` instead.
        "shoot_at",
        "drive_by",
        "enter_vehicle_seat",
        # T8 (findings.md R6), CONTRACTS v1.13. FIXED here: these two were in
        # `brain.schemas.BRIDGE_TASKS` and issued by `main._phone_reflex` all
        # along but were never added to THIS tuple, so every phone-reflex post
        # raised ValueError("not a bridge task") client-side before any I/O —
        # the literal bug behind the operator's "it cant cut the call".
        "answer_call",
        "reject_call",
        # bridge 1.7.0 (fix-opus-a, T1), CONTRACTS v1.14.
        "flee_ped",
        # bridge 1.8.0 (CONTRACTS §1 proposal): TASK_PLANE_MISSION /
        # TASK_HELI_MISSION behind one verb, the flight step of `go_flying`.
        "fly_to",
        # bridge 1.9.0 (CONTRACTS §1 proposal): raw TASK_THROW_PROJECTILE, graded on
        # the throwable's ammo dropping; the throw step of `burn_the_city`.
        "throw_at",
    )


def test_phone_tasks_are_real_bridge_tasks_not_primitives() -> None:
    """The regression test for the bug above: both verbs must actually reach
    `post_task` without `BridgeClient` rejecting them client-side first."""
    assert "answer_call" in BRIDGE_TASK_TYPES
    assert "reject_call" in BRIDGE_TASK_TYPES
