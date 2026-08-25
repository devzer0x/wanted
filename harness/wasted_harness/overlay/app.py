"""Overlay FastAPI app (CONTRACTS §6).

- ``GET /overlay`` — transparent, fully self-contained page OBS embeds (no
  external assets; renders with the game offline: blank counters, no banner).
- ``GET /overlay/stream`` — SSE with exactly the four contract event types:
  ``banner``, ``counters``, ``say``, ``governor``.

The bus is written from the (threaded, sync) main loop and read by async SSE
subscribers; publish() marshals into each subscriber's event loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from ..logsetup import get_logger

log = get_logger("wasted.overlay")

CONTRACT_EVENT_TYPES = ("banner", "counters", "say", "governor")
KEEPALIVE_S = 15.0


class OverlayBus:
    """Thread-safe pub/sub with a retained snapshot for late joiners."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self.counters: dict[str, int] = {"deaths": 0, "busted": 0, "missions_passed": 0}
        self.last_say: dict[str, str] | None = None
        self.governor: dict[str, Any] = {"level": 0, "note": ""}

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type not in CONTRACT_EVENT_TYPES:
            raise ValueError(
                f"{event_type!r} is not a §6 overlay event type {CONTRACT_EVENT_TYPES}"
            )
        with self._lock:
            if event_type == "counters":
                self.counters = dict(payload)
            elif event_type == "say":
                self.last_say = dict(payload)
            elif event_type == "governor":
                self.governor = dict(payload)
            targets = list(self._subscribers)
        item = (event_type, payload)
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # subscriber's loop already closed; unsubscribe cleans it up

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [(l, q) for (l, q) in self._subscribers if q is not queue]

    def snapshot(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            events: list[tuple[str, dict[str, Any]]] = [
                ("counters", dict(self.counters)),
                ("governor", dict(self.governor)),
            ]
            if self.last_say is not None:
                events.append(("say", dict(self.last_say)))
        return events


def _sse(event_type: str, payload: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(bus: OverlayBus) -> FastAPI:
    app = FastAPI(title="WASTED overlay", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/overlay", response_class=HTMLResponse)
    async def overlay_page() -> str:
        return OVERLAY_HTML

    @app.get("/overlay/stream")
    async def overlay_stream(max_events: int | None = None) -> StreamingResponse:
        # max_events bounds the stream (debugging with curl, and tests via
        # httpx ASGITransport, which buffers the app to completion). OBS omits
        # it and streams forever.
        async def gen():
            queue = bus.subscribe()
            sent = 0
            try:
                for event_type, payload in bus.snapshot():
                    yield _sse(event_type, payload)
                    sent += 1
                    if max_events is not None and sent >= max_events:
                        return
                while True:
                    try:
                        event_type, payload = await asyncio.wait_for(
                            queue.get(), timeout=KEEPALIVE_S
                        )
                        yield _sse(event_type, payload)
                        sent += 1
                        if max_events is not None and sent >= max_events:
                            return
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


# Self-contained page: system fonts only, transparent background, no requests
# other than the SSE stream. Renders blank (no banner, em-dash counters) until
# events arrive, which is the contracted game-offline behavior.
OVERLAY_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>WASTED overlay</title>
<style>
  html, body {
    margin: 0; padding: 0; width: 100vw; height: 100vh;
    background: transparent; overflow: hidden;
    font-family: "Arial Narrow", Arial, Helvetica, sans-serif;
  }
  #counters {
    position: absolute; top: 18px; right: 22px;
    display: flex; gap: 18px;
    color: #fff; font-size: 20px; font-weight: 700;
    text-shadow: 0 1px 3px rgba(0,0,0,.9);
    letter-spacing: .04em;
  }
  #counters .label { color: #ff3b30; font-size: 12px; display: block; letter-spacing: .18em; }
  #say {
    position: absolute; left: 50%; bottom: 64px; transform: translateX(-50%);
    max-width: 72vw; padding: 8px 18px;
    color: #fff; font-size: 24px; font-weight: 700; text-align: center;
    background: rgba(0,0,0,.55); border-left: 4px solid #ff3b30;
    text-shadow: 0 1px 2px rgba(0,0,0,.8);
    opacity: 0; transition: opacity .3s ease;
  }
  #say.visible { opacity: 1; }
  #say .mood { color: #ff3b30; font-size: 13px; letter-spacing: .2em; text-transform: uppercase; }
  #banner {
    position: absolute; inset: 0; display: none;
    align-items: center; justify-content: center;
    background: radial-gradient(ellipse at center, rgba(0,0,0,.45) 0%, rgba(0,0,0,.75) 100%);
  }
  #banner.show { display: flex; }
  #banner span {
    font-family: Georgia, "Times New Roman", serif;
    font-weight: 900; font-size: 15vw; color: #b00d18;
    letter-spacing: .06em; text-transform: uppercase;
    text-shadow: 0 0 24px rgba(176,13,24,.7), 0 2px 6px rgba(0,0,0,.9);
    animation: slam .45s cubic-bezier(.2,1.6,.35,1) both;
  }
  @keyframes slam {
    from { transform: scale(2.4); opacity: 0; }
    to   { transform: scale(1);   opacity: 1; }
  }
  #governor {
    position: absolute; left: 22px; bottom: 18px;
    color: #ddd; font-size: 14px; letter-spacing: .06em;
    background: rgba(0,0,0,.55); padding: 5px 12px;
    border-left: 3px solid #ff3b30;
    display: none;
  }
  #governor.show { display: block; }
</style>
</head>
<body>
  <div id="counters">
    <div><span class="label">DEATHS</span><span id="c-deaths">&mdash;</span></div>
    <div><span class="label">BUSTED</span><span id="c-busted">&mdash;</span></div>
    <div><span class="label">MISSIONS</span><span id="c-missions">&mdash;</span></div>
  </div>
  <div id="say"><span class="mood" id="say-mood"></span><div id="say-text"></div></div>
  <div id="banner"><span id="banner-text"></span></div>
  <div id="governor"><span id="governor-text"></span></div>
<script>
  var es = new EventSource('/overlay/stream');
  var sayTimer = null, bannerTimer = null;
  es.addEventListener('counters', function (e) {
    var d = JSON.parse(e.data);
    document.getElementById('c-deaths').textContent = d.deaths;
    document.getElementById('c-busted').textContent = d.busted;
    document.getElementById('c-missions').textContent = d.missions_passed;
  });
  es.addEventListener('say', function (e) {
    var d = JSON.parse(e.data);
    document.getElementById('say-text').textContent = d.text;
    document.getElementById('say-mood').textContent = d.mood || '';
    var el = document.getElementById('say');
    el.classList.add('visible');
    if (sayTimer) clearTimeout(sayTimer);
    sayTimer = setTimeout(function () { el.classList.remove('visible'); }, 12000);
  });
  es.addEventListener('banner', function (e) {
    var d = JSON.parse(e.data);
    var banner = document.getElementById('banner');
    document.getElementById('banner-text').textContent =
      d.kind === 'busted' ? 'Busted' : 'Wasted';
    banner.classList.remove('show');
    void banner.offsetWidth; /* restart the slam animation */
    banner.classList.add('show');
    if (bannerTimer) clearTimeout(bannerTimer);
    bannerTimer = setTimeout(function () { banner.classList.remove('show'); }, 4500);
  });
  es.addEventListener('governor', function (e) {
    var d = JSON.parse(e.data);
    var el = document.getElementById('governor');
    if (d.level > 0) {
      document.getElementById('governor-text').textContent =
        'brain governor L' + d.level + (d.note ? ' — ' + d.note : '');
      el.classList.add('show');
    } else {
      el.classList.remove('show');
    }
  });
</script>
</body>
</html>
"""
