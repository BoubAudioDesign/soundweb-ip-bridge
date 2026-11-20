import asyncio

import aiohttp
import pytest

from soundweb_bridge import protocol as p
from soundweb_bridge.config import parse_config
from soundweb_bridge.core import Bridge
from soundweb_bridge.http_server import start_http_server
from soundweb_bridge.serial_link import SendError, SerialLink
from soundweb_bridge.simulator import start_simulator
from soundweb_bridge.tcp_server import start_tcp_server

CONFIG = {
    "selectors": {
        "salle": {"label": "Salle", "handle": 5, "sources": {1: "PC", 2: "Blu-ray"}},
        "hall": {"set_value_id": 3, "sources": {1: "Ambiance"}},
    },
    "presets": {"Show": 0},
}


async def make_bridge(drop_acks=0, retries=3):
    path, sim = start_simulator(drop_acks)
    cfg = parse_config(CONFIG)
    link = SerialLink(path, ack_timeout=0.3, retries=retries, reconnect_delay=0.1)
    bridge = Bridge(cfg, link)
    await bridge.start()
    for _ in range(50):
        if link.connected:
            break
        await asyncio.sleep(0.05)
    assert link.connected
    assert sim.connected.wait(2)  # TCP simulator (Windows): wait until it has accepted the bridge
    return bridge, sim


async def wait_for(cond, timeout=2.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


def test_source_by_number_and_label():
    async def run():
        bridge, sim = await make_bridge()
        await bridge.select_source("salle", 2)
        await bridge.select_source("salle", "pc")
        await bridge.select_source("hall", 1)
        assert sim.received == [
            p.RawMessage(5, p.METHOD_SOURCE_SELECT, 2),
            p.RawMessage(5, p.METHOD_SOURCE_SELECT, 1),
            p.SetValue(6, 3, 1),
        ]
        assert bridge.sources == {"salle": 1, "hall": 1}
        with pytest.raises(ValueError):
            await bridge.select_source("salle", "Inconnue")
        await bridge.stop()

    asyncio.run(run())


def test_resend_when_ack_missing():
    async def run():
        bridge, sim = await make_bridge(drop_acks=1)
        await bridge.select_source("salle", 2)
        assert len(sim.received) == 2  # first attempt not ACKed, resent
        await bridge.stop()

    asyncio.run(run())


def test_error_after_retries():
    async def run():
        bridge, sim = await make_bridge(drop_acks=10, retries=2)
        with pytest.raises(SendError):
            await bridge.select_source("salle", 2)
        assert bridge.sources["salle"] is None
        await bridge.stop()

    asyncio.run(run())


def test_feedback_from_soundweb():
    async def run():
        bridge, sim = await make_bridge()
        q = bridge.subscribe()
        sim.front_panel_source(5, 2)
        ev = await asyncio.wait_for(q.get(), 2)
        assert ev == {"type": "source", "selector": "salle", "source": 2, "label": "Blu-ray", "origin": "soundweb"}
        await bridge.stop()

    asyncio.run(run())


def test_tcp_and_http_front_ends():
    async def run():
        bridge, sim = await make_bridge()
        tcp = await start_tcp_server(bridge, "127.0.0.1", 0)
        tcp_port = tcp.sockets[0].getsockname()[1]
        runner = await start_http_server(bridge, "127.0.0.1", 18765, token="secret")

        r, w = await asyncio.open_connection("127.0.0.1", tcp_port)
        w.write(b"SOURCE salle Blu-ray\r\n")
        assert (await r.readline()).strip() == b"OK SOURCE salle 2"
        w.write(b"SOURCE? salle\r\n")
        lines = {(await r.readline()).strip() for _ in range(2)}
        assert b"SOURCE salle 2" in lines
        w.write(b"SOURCE nope 1\r\n")
        line = (await r.readline()).strip()
        assert line.startswith(b"ERR")

        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:18765/api/state") as resp:
                assert resp.status == 401
            async with s.get("http://127.0.0.1:18765/api/selectors/salle/source/1?token=secret") as resp:
                data = await resp.json()
                assert resp.status == 200 and data["source"] == 1
            hdr = {"Authorization": "Bearer secret"}
            async with s.post("http://127.0.0.1:18765/api/selectors/salle/source", json={"source": "Blu-ray"}, headers=hdr) as resp:
                assert (await resp.json())["source"] == 2
            async with s.post("http://127.0.0.1:18765/api/presets/Show", headers=hdr) as resp:
                assert (await resp.json())["id"] == 0
            async with s.get("http://127.0.0.1:18765/api/state", headers=hdr) as resp:
                st = await resp.json()
                assert st["connected"] and st["selectors"][0]["source"] == 2

        await wait_for(lambda: p.RawMessage(0xFFFFFFFF, 0x0B, 0) in sim.received)
        # TCP client also receives unsolicited feedback for changes made via HTTP
        seen = []
        while b"SOURCE salle 2" not in seen or b"PRESET 0" not in seen:
            seen.append((await asyncio.wait_for(r.readline(), 2)).strip())
        w.close()
        tcp.close()
        await runner.cleanup()
        await bridge.stop()

    asyncio.run(run())


PRESET_CONFIG = {
    "presets": {"Conf": 4, "Concert": 5},
    "selectors": {
        # sources by preset only (no handle)
        "salle": {
            "sources": {
                1: {"label": "Conférence", "preset": "Conf"},
                2: {"label": "Concert", "preset": 5},
                3: {"label": "Visio", "parameter_preset": [2, 1]},
            }
        },
        # mixed: one source via preset, the others via the Source Selector handle
        "hall": {"handle": 9, "sources": {1: "Ambiance", 2: {"label": "Concert", "preset": "Concert"}}},
    },
}


async def make_preset_bridge():
    path, sim = start_simulator()
    link = SerialLink(path, ack_timeout=0.3, reconnect_delay=0.1)
    bridge = Bridge(parse_config(PRESET_CONFIG), link)
    await bridge.start()
    await wait_for(lambda: link.connected)
    assert sim.connected.wait(2)
    return bridge, sim


def test_source_via_preset():
    async def run():
        bridge, sim = await make_preset_bridge()
        q = bridge.subscribe()
        await bridge.select_source("salle", "conférence")
        await bridge.select_source("salle", 3)
        await bridge.select_source("hall", 1)
        assert sim.received == [
            p.RawMessage(p.BROADCAST_HANDLE, p.METHOD_PRESET_ACTIVATE, 4),
            p.RawMessage(p.BROADCAST_HANDLE, p.METHOD_PRESET_ACTIVATE, (2 << 16) | 1),
            p.RawMessage(9, p.METHOD_SOURCE_SELECT, 1),
        ]
        assert bridge.sources == {"salle": 3, "hall": 1}
        # a preset shared by two zones updates both
        await bridge.select_source("salle", "Concert")
        assert sim.received[-1] == p.RawMessage(p.BROADCAST_HANDLE, p.METHOD_PRESET_ACTIVATE, 5)
        assert bridge.sources == {"salle": 2, "hall": 2}
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert {"type": "source", "selector": "hall", "source": 2, "label": "Concert", "origin": "remote"} in events
        with pytest.raises(ValueError):  # source 4 has no preset and the selector has no handle
            await bridge.select_source("salle", 4)
        await bridge.stop()

    asyncio.run(run())


def test_preset_command_and_feedback_update_sources():
    async def run():
        bridge, sim = await make_preset_bridge()
        await bridge.activate_preset("Conf")
        assert bridge.sources["salle"] == 1
        await bridge.activate_parameter_preset(2, 1)
        assert bridge.sources["salle"] == 3
        # preset recalled on the Soundweb side (e.g. control input) and sent back to the bridge
        sim._write(p.frame(p.build_preset(5)))
        await wait_for(lambda: bridge.sources == {"salle": 2, "hall": 2})
        await bridge.stop()

    asyncio.run(run())


def test_preset_config_errors():
    with pytest.raises(ValueError, match="missing"):
        parse_config({"selectors": {"z": {"sources": {1: {"label": "A", "preset": 1}, 2: "B"}}}})
    with pytest.raises(ValueError, match="unknown preset"):
        parse_config({"selectors": {"z": {"sources": {1: {"preset": "Nope"}}}}})
    with pytest.raises(ValueError, match="parameter_preset"):
        parse_config({"selectors": {"z": {"sources": {1: {"parameter_preset": [1]}}}}})
    cfg = parse_config({"selectors": {"z": {"sources": {1: {"parameter_preset": {"id": 3, "state": 2}}}}}})
    assert cfg.selectors["z"].source_presets == {1: (3 << 16) | 2}
    assert cfg.selectors["z"].sources == {1: "Source 1"}


def test_volume_thirdparty_raw():
    async def run():
        path, sim = start_simulator()
        link = SerialLink(path, ack_timeout=0.3, reconnect_delay=0.1)
        bridge = Bridge(parse_config({"volumes": {"master": {"handle": 5}}}), link)
        await bridge.start()
        await wait_for(lambda: link.connected)
        assert sim.connected.wait(2)
        await bridge.set_volume("master", -20)
        assert sim.received[-1] == p.RawMessage(5, 0x01000830, 0xEC00)
        sim._write(p.frame(p.build_raw(5, 0x01000830, 0xD800, 2)))  # change reported by the Soundweb
        await wait_for(lambda: bridge.volumes["master"] == -40.0)
        await bridge.stop()

    asyncio.run(run())
