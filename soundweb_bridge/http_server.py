"""HTTP API, WebSocket feedback and web remote page."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .config import _int
from .core import Bridge
from .serial_link import SendError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def create_app(bridge: Bridge, token: str = "") -> web.Application:
    @web.middleware
    async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
        if token and request.path.startswith(("/api", "/ws")):
            supplied = request.query.get("token", "")
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[7:]
            if not hmac.compare_digest(supplied, token):
                return _error(401, "unauthorized")
        try:
            return await handler(request)
        except KeyError as e:
            return _error(404, str(e.args[0] if e.args else e))
        except ValueError as e:
            return _error(400, str(e))
        except SendError as e:
            return _error(502, str(e))

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    async def state(_: web.Request) -> web.Response:
        return web.json_response(bridge.state())

    async def body(request: web.Request) -> dict:
        if request.can_read_body:
            try:
                return await request.json()
            except json.JSONDecodeError:
                raise ValueError("invalid JSON body") from None
        return {}

    async def set_source(request: web.Request) -> web.Response:
        source = request.match_info.get("source")
        if source is None:
            source = (await body(request)).get("source")
        if source is None:
            raise ValueError("missing 'source'")
        ev = await bridge.select_source(request.match_info["selector"], source)
        return web.json_response({"ok": True, **ev})

    async def get_source(request: web.Request) -> web.Response:
        sel = bridge.selector(request.match_info["selector"])
        return web.json_response(bridge.source_event(sel, "query"))

    async def preset(request: web.Request) -> web.Response:
        pid = await bridge.activate_preset(request.match_info["preset"])
        return web.json_response({"ok": True, "id": pid})

    async def set_volume(request: web.Request) -> web.Response:
        db = request.match_info.get("db")
        if db is None:
            db = (await body(request)).get("db")
        if db is None:
            raise ValueError("missing 'db'")
        ev = await bridge.set_volume(request.match_info["volume"], db)
        return web.json_response({"ok": True, **ev})

    async def get_volume(request: web.Request) -> web.Response:
        return web.json_response(bridge.volume_event(bridge.volume(request.match_info["volume"]), "query"))

    async def raw(request: web.Request) -> web.Response:
        data = await body(request)
        size = data.get("value_size")
        await bridge.send_raw(
            _int(data["handle"], "handle"),
            _int(data["method"], "method"),
            _int(data["value"], "value"),
            _int(size, "value_size") if size is not None else None,
        )
        return web.json_response({"ok": True})

    async def ws(request: web.Request) -> web.WebSocketResponse:
        sock = web.WebSocketResponse(heartbeat=30)
        await sock.prepare(request)
        queue = bridge.subscribe()
        await sock.send_json({"type": "state", **bridge.state()})

        async def pump() -> None:
            while True:
                await sock.send_json(await queue.get())

        task = asyncio.create_task(pump())
        try:
            async for msg in sock:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("action") == "source":
                            await bridge.select_source(data["selector"], data["source"])
                        elif data.get("action") == "preset":
                            await bridge.activate_preset(data["preset"])
                        elif data.get("action") == "volume":
                            await bridge.set_volume(data["volume"], data["db"])
                    except (KeyError, ValueError, SendError, json.JSONDecodeError) as e:
                        await sock.send_json({"type": "error", "error": str(e)})
        finally:
            task.cancel()
            bridge.unsubscribe(queue)
        return sock

    app = web.Application(middlewares=[guard])
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/selectors/{selector}/source", get_source)
    app.router.add_post("/api/selectors/{selector}/source", set_source)
    app.router.add_route("*", "/api/selectors/{selector}/source/{source}", set_source)
    app.router.add_route("*", "/api/presets/{preset}", preset)
    app.router.add_get("/api/volumes/{volume}", get_volume)
    app.router.add_post("/api/volumes/{volume}", set_volume)
    app.router.add_route("*", "/api/volumes/{volume}/{db}", set_volume)
    app.router.add_post("/api/raw", raw)
    app.router.add_get("/ws", ws)
    return app


async def start_http_server(bridge: Bridge, host: str, port: int, token: str = "") -> web.AppRunner:
    runner = web.AppRunner(create_app(bridge, token), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("HTTP on http://%s:%d", host, port)
    return runner
