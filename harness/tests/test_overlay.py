"""Overlay via httpx ASGITransport against the REAL app (no server socket needed)."""

import asyncio
import json

import httpx
import pytest

from wasted_harness.overlay import OverlayBus, create_app


def test_overlay_page_is_self_contained_and_transparent() -> None:
    bus = OverlayBus()
    app = create_app(bus)

    async def fetch() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://overlay") as client:
            return await client.get("/overlay")

    resp = asyncio.run(fetch())
    assert resp.status_code == 200
    html = resp.text
    assert "background: transparent" in html
    assert "EventSource('/overlay/stream')" in html
    # Self-contained: no external fetches (CONTRACTS §6 "no external assets").
    assert "http://" not in html.replace("http://overlay", "")
    assert "https://" not in html
    # Game-offline default: counters render as em-dashes until events arrive.
    assert "&mdash;" in html
    for kind in ("Wasted", "Busted"):
        assert kind in html


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event_type, data = None, None
    for line in body.splitlines():
        if line.startswith("event: "):
            event_type = line[len("event: "):]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
        elif line == "" and event_type is not None:
            events.append((event_type, data))
            event_type, data = None, None
    return events


def test_sse_stream_delivers_all_four_contract_event_types() -> None:
    # httpx ASGITransport runs the app to completion (it buffers the body), so
    # the request bounds the stream with max_events while a concurrent task
    # publishes through the real bus exactly as the sync harness loop would.
    bus = OverlayBus()
    app = create_app(bus)

    async def scenario() -> httpx.Response:
        async def publisher() -> None:
            # By the time this runs, the app generator is parked on queue.get().
            await asyncio.sleep(0.1)
            bus.publish("banner", {"kind": "wasted"})
            bus.publish("say", {"text": "Put it on the tab.", "mood": "bored"})
            bus.publish("counters", {"deaths": 1, "busted": 0, "missions_passed": 0})
            bus.publish("governor", {"level": 1, "note": "slower tactical timers"})

        pub = asyncio.create_task(publisher())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://overlay") as client:
            resp = await client.get("/overlay/stream", params={"max_events": 6})
        await pub
        return resp

    resp = asyncio.run(asyncio.wait_for(scenario(), timeout=20))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    kinds = [k for k, _ in events]
    assert kinds[:2] == ["counters", "governor"]  # retained snapshot for late joiners
    assert kinds[2:] == ["banner", "say", "counters", "governor"]
    payloads = dict(events[2:])
    assert payloads["banner"] == {"kind": "wasted"}
    assert payloads["say"]["text"] == "Put it on the tab."
    assert payloads["counters"]["deaths"] == 1
    assert payloads["governor"]["level"] == 1


def test_bus_rejects_non_contract_event_types() -> None:
    bus = OverlayBus()
    with pytest.raises(ValueError, match="overlay event type"):
        bus.publish("confetti", {})
