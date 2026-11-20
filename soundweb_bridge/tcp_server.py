"""Line-based TCP control protocol (for Crestron, AMX, Extron, Companion, netcat...)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from .config import _int
from .core import Bridge
from .serial_link import SendError

log = logging.getLogger(__name__)

HELP = (
    "Commands: SOURCE <selector> <n|label> | SOURCE? [selector] | PRESET <name|id> | "
    "VOLUME <name> <dB|min|max|-inf> | VOLUME? [name] | PRESET? | "
    "PPRESET <param_preset_id> <state> | RAW <handle> <method> <value> | "
    "SETVALUE <group> <id> <value> | HEX <body bytes> | STATUS | PING | HELP"
)


def format_event(event: Dict[str, Any]) -> Optional[str]:
    t = event["type"]
    if t == "source":
        n = event["source"]
        return f"SOURCE {event['selector']} {n if n is not None else '?'}"
    if t == "link":
        return "LINK UP" if event["connected"] else "LINK DOWN"
    if t == "preset":
        return f"PRESET {event['id']}"
    if t == "volume":
        db = event["db"]
        return f"VOLUME {event['name']} {db:g}" if db is not None else f"VOLUME {event['name']} ?"
    if t == "raw":
        return f"RAW 0x{event['handle']:08X} 0x{event['method']:08X} {event['value']}"
    if t == "set_value":
        return f"SETVALUE {event['group']} {event['id']} {event['value']}"
    return None


async def handle_command(bridge: Bridge, line: str) -> str:
    parts = line.split()
    if not parts:
        return ""
    cmd, args = parts[0].upper(), parts[1:]
    try:
        if cmd == "PING":
            return "PONG"
        if cmd == "HELP":
            return HELP
        if cmd == "STATUS":
            return "STATUS " + json.dumps(bridge.state(), ensure_ascii=False)
        if cmd == "SOURCE?":
            names = [bridge.selector(args[0]).name] if args else list(bridge.config.selectors)
            return "\r\n".join(format_event(bridge.source_event(bridge.selector(n), "query")) for n in names)
        if cmd == "SOURCE":
            if len(args) < 2:
                return "ERR usage: SOURCE <selector> <n|label>"
            ev = await bridge.select_source(args[0], " ".join(args[1:]))
            return f"OK SOURCE {ev['selector']} {ev['source']}"
        if cmd == "VOLUME?":
            names = [bridge.volume(args[0]).name] if args else list(bridge.config.volumes)
            return "\r\n".join(format_event(bridge.volume_event(bridge.volume(n), "query")) for n in names)
        if cmd == "VOLUME":
            if len(args) != 2:
                return "ERR usage: VOLUME <name> <dB|min|max|-inf>"
            ev = await bridge.set_volume(args[0], args[1])
            return f"OK {format_event(ev)}"
        if cmd == "PRESET?":
            pid = bridge.active_preset
            return f"PRESET {pid}" if pid is not None else "PRESET ?"
        if cmd == "PRESET":
            if not args:
                return "ERR usage: PRESET <name|id>"
            pid = await bridge.activate_preset(" ".join(args))
            return f"OK PRESET {pid}"
        if cmd == "PPRESET":
            await bridge.activate_parameter_preset(_int(args[0], "id"), _int(args[1], "state"))
            return "OK PPRESET"
        if cmd == "RAW":
            await bridge.send_raw(_int(args[0], "handle"), _int(args[1], "method"), _int(args[2], "value"))
            return "OK RAW"
        if cmd == "SETVALUE":
            await bridge.send_set_value(args[0], _int(args[1], "id"), _int(args[2], "value"))
            return "OK SETVALUE"
        if cmd == "HEX":
            await bridge.send_body(bytes.fromhex("".join(args)))
            return "OK HEX"
        return f"ERR unknown command {parts[0]!r} (try HELP)"
    except (IndexError,):
        return "ERR missing argument (try HELP)"
    except (ValueError, KeyError) as e:
        return f"ERR {e.args[0] if e.args else e}"
    except SendError as e:
        return f"ERR {e}"


async def _client(bridge: Bridge, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    log.info("TCP client connected: %s", peer)
    queue = bridge.subscribe()
    lock = asyncio.Lock()

    async def send(text: str) -> None:
        async with lock:
            writer.write((text + "\r\n").encode("utf-8"))
            await writer.drain()

    async def pump() -> None:
        while True:
            ev = await queue.get()
            line = format_event(ev)
            if line:
                await send(line)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            reply = await handle_command(bridge, raw.decode("utf-8", "replace").strip())
            if reply:
                await send(reply)
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        pump_task.cancel()
        bridge.unsubscribe(queue)
        writer.close()
        log.info("TCP client disconnected: %s", peer)


async def start_tcp_server(bridge: Bridge, host: str, port: int) -> asyncio.base_events.Server:
    server = await asyncio.start_server(lambda r, w: _client(bridge, r, w), host, port)
    log.info("TCP control on %s:%d", host, port)
    return server
