import asyncio

import pytest

from soundweb_bridge import designer_link as d
from soundweb_bridge.config import parse_config
from soundweb_bridge.core import Bridge
from soundweb_bridge.designer_link import DesignerLink
from soundweb_bridge.serial_link import SendError
from soundweb_bridge.simulator import start_designer_simulator

CONFIG = {
    "serial": {"protocol": "designer"},
    "presets": {"Show": 0, "Conf": 1},
    "volumes": {"master": {"label": "Volume", "handle": 5, "parameter": 0x30}},
    "selectors": {
        "salle": {"sources": {1: {"label": "Show", "preset": "Show"}, 2: {"label": "Conf", "preset": 1}}},
        "hall": {"handle": 9, "sources": {1: "Ambiance"}},
    },
}

# frames captured between Soundweb Designer and a real 9088 (2026-11-18)
CAPTURED = {
    "preset1": "c0 ff 02 04 ff 00 00 ff ff 01 ff ff ff ff 00 00 00 00 00 00 00 0b 00 00 00 01 01 00 00 00 80 db dd 59 00 00 00 00 00 0e c0",
    "online2": "c0 01 02 01 ff 00 00 00 00 00 02 00 00 00 01 00 00 00 00 00 00 00 05 00 00 00 01 ff ff ff ff ff ff ff ff fa c0",
    "block3c0": "c0 01 02 01 ff 00 00 00 00 00 04 00 00 00 01 00 00 00 00 00 00 00 30 00 00 00 00 03 db dc 00 78 73 c0",
    "ack3": "c0 01 02 01 ff 00 00 00 00 02 03 fc c0",
}


def test_frames_match_capture():
    import struct

    assert d.encode(d.preset_body(1)) == bytes.fromhex(CAPTURED["preset1"])
    assert d.encode(d.numbered(2, d.MSG_ONLINE, b"\0\0\0\x01" + b"\xff" * 8)) == bytes.fromhex(CAPTURED["online2"])
    assert d.encode(d.numbered(4, d.MSG_CFG_BLOCK, b"\0" * 4 + struct.pack(">HH", 0x3C0, 0x78))) == bytes.fromhex(CAPTURED["block3c0"])
    assert d.encode(d.ack_body(3)) == bytes.fromhex(CAPTURED["ack3"])
    sp = d.Splitter()
    assert sp.feed(bytes.fromhex(CAPTURED["preset1"])[:10]) == []
    assert sp.feed(bytes.fromhex(CAPTURED["preset1"])[10:]) == [d.preset_body(1)]
    assert d.Splitter().feed(b"\xc0\x01\x02\xc0") == [None]  # bad checksum


def test_config_defaults():
    cfg = parse_config(CONFIG)
    assert (cfg.protocol, cfg.baudrate, cfg.rtscts) == ("designer", 115200, True)
    assert parse_config({}).baudrate == 38400
    with pytest.raises(ValueError):
        parse_config({"serial": {"protocol": "nope"}})


async def wait_for(cond, timeout=8.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def make(url):
    return DesignerLink(url, reconnect_delay=0.2, link_timeout=1.0)


def test_designer_session_and_presets():
    async def run():
        url, sim = start_designer_simulator()
        link = make(url)
        bridge = Bridge(parse_config(CONFIG), link)
        await bridge.start()
        await wait_for(lambda: link.connected)
        assert sim.online and link.device_id == sim.DEVICE_ID
        assert link.config_data == sim.config and link.firmware == "Apr 08 2004 12:58:31"
        await bridge.select_source("salle", "Conf")
        await bridge.activate_preset("Show")
        await wait_for(lambda: sim.presets == [1, 0])
        assert bridge.sources["salle"] == 1
        with pytest.raises(ValueError, match="no 'preset'"):
            await bridge.select_source("hall", 1)  # Source Selector handle: not available in Designer mode
        with pytest.raises(SendError, match="0-255"):
            await bridge.activate_parameter_preset(2, 1)
        await bridge.stop()
        await wait_for(lambda: not sim.online, 3)  # clean offline

    asyncio.run(run())


def test_designer_reconnects_after_drop():
    async def run():
        url, sim = start_designer_simulator()
        link = make(url)
        events = []
        link.on_link = events.append
        await link.start()
        await wait_for(lambda: link.connected)
        sim.drop()
        await wait_for(lambda: not link.connected)
        with pytest.raises(SendError, match="not established"):
            await link.send(d_preset(0))
        await wait_for(lambda: link.connected and sim.sessions == 2)
        await link.send(d_preset(2))
        await wait_for(lambda: sim.presets == [2])
        assert events[:3] == [True, False, True]
        await link.stop()

    asyncio.run(run())


def test_designer_no_session_when_device_silent():
    async def run():
        url, sim = start_designer_simulator()
        sim.ignore_connect = True
        link = make(url)
        await link.start()
        await asyncio.sleep(4)
        assert not link.connected
        await link.stop()

    asyncio.run(run())


def d_preset(n):
    from soundweb_bridge import protocol as p

    return p.build_preset(n)


def test_volume_config_and_scale():
    cfg = parse_config(CONFIG)
    vol = cfg.volumes["master"]
    assert (vol.handle, vol.parameter, vol.min_db, vol.max_db) == (5, 0x01000830, -100.0, 0.0)
    # values captured from Designer: -40, -20, 0, -100 dB
    assert [vol.encode(x) & 0xFFFF for x in (-40, -20, 0, -100)] == [0xD800, 0xEC00, 0x0000, 0x9C00]
    assert vol.decode(0xF7BC) == -8.27
    assert vol.encode(+6) == 0 and vol.encode(-200) == -25600  # clamped to the slider range
    with pytest.raises(ValueError):
        parse_config({"volumes": {"v": {"label": "no handle"}}})
    assert vol.law == "log"
    assert parse_config({"volumes": {"v": {"handle": 1, "law": "linear"}}}).volumes["v"].law == "linear"
    with pytest.raises(ValueError):
        parse_config({"volumes": {"v": {"handle": 1, "law": "expo"}}})


def test_designer_volume_set_feedback_and_initial_value():
    async def run():
        url, sim = start_designer_simulator()
        link = make(url)
        bridge = Bridge(parse_config(CONFIG), link)
        q = bridge.subscribe()
        await bridge.start()
        await wait_for(lambda: link.connected)
        await wait_for(lambda: bridge.volumes["master"] == -8.27)  # read from the downloaded config
        ev = await bridge.set_volume("master", "-20")
        assert ev["db"] == -20.0 and sim.params[(5, 0x01000830)] == 0xEC00
        await bridge.set_volume("master", "-inf")
        assert sim.params[(5, 0x01000830)] == 0x9C00
        # preset recall: the 9088 re-applies another gain -> the bridge keeps (and re-sends) its volume
        await bridge.activate_preset("Conf")
        await wait_for(lambda: sim.params[(5, 0x01000830)] == 0x9C00 and sim.presets[-1] == 1)
        await asyncio.sleep(0.5)
        assert sim.params[(5, 0x01000830)] == 0x9C00 and bridge.volumes["master"] == -100.0
        # physical pot moved (no preset): the slider follows
        await asyncio.sleep(bridge.PRESET_HOLD)
        sim.params[(5, 0x01000830)] = 0xD800
        sim.notify(5, 0x01000830)
        await wait_for(lambda: bridge.volumes["master"] == -40.0)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert {"type": "volume", "name": "master", "db": -40.0, "origin": "soundweb"} in events
        with pytest.raises(ValueError):
            await bridge.set_volume("master", "fort")
        with pytest.raises(KeyError):
            await bridge.set_volume("nope", 0)
        await bridge.stop()

    asyncio.run(run())


def test_volume_frame_matches_capture():
    import struct

    frame = d.encode(d.numbered(2, 0x01000830, bytes.fromhex("d8 00"), struct.pack(">II", 5, 0)))
    assert frame == bytes.fromhex("c0 01 02 01 ff 00 00 00 00 00 02 00 00 00 05 00 00 00 00 01 00 08 30 d8 00 1b c0")


def test_volume_follows_preset_when_keep_disabled():
    async def run():
        cfg = dict(CONFIG, volumes={"master": {"handle": 5, "keep_on_preset": False}})
        url, sim = start_designer_simulator()
        link = make(url)
        bridge = Bridge(parse_config(cfg), link)
        await bridge.start()
        await wait_for(lambda: link.connected and bridge.volumes["master"] is not None)
        await bridge.set_volume("master", -20)
        await bridge.activate_preset("Conf")  # simulator: preset 1 sets the pot to -40 dB
        await wait_for(lambda: bridge.volumes["master"] == -40.0)
        assert sim.params[(5, 0x01000830)] == 0xD800
        await bridge.stop()

    asyncio.run(run())


def test_designer_back_to_back_sessions_resync():
    """The 9088 keeps its numbering across sessions and answers '03 <expected>' to a wrong number."""
    async def run():
        url, sim = start_designer_simulator()
        for i in range(3):
            link = make(url)
            await link.start()
            await wait_for(lambda: link.connected, 5)
            await link.send(d_preset(i))
            await link.stop()
        await wait_for(lambda: sim.presets == [0, 1, 2])
        assert sim.sessions == 3 and sim._expect_pc != 0

    asyncio.run(run())


def test_fingerprint_build_match_roundtrip(tmp_path):
    from soundweb_bridge import fingerprint as fp

    configs = {
        0: {(2, 1): 1, (2, 2): 0, (5, 0x01000830): 0x1111, (1, 9): 7},
        1: {(2, 1): 0, (2, 2): 1, (5, 0x01000830): 0x2222, (1, 9): 7},
    }
    sig = fp.build(configs, exclude=[(5, 0x01000830)])
    assert sig == {0: {(2, 1): 1, (2, 2): 0}, 1: {(2, 1): 0, (2, 2): 1}}  # volume and constant params ignored
    assert fp.match(sig, {(2, 1): 0, (2, 2): 1, (5, 0x01000830): 0x9999}) == 1
    assert fp.match(sig, {(2, 1): 1, (2, 2): 1}) is None
    path = str(tmp_path / "sig.json")
    fp.save(path, sig)
    assert fp.load(path) == sig
    assert fp.load(str(tmp_path / "absent.json")) == {}


def test_learn_presets_and_recognise_at_startup(tmp_path):
    from soundweb_bridge.learn import learn_presets

    async def run():
        url, sim = start_designer_simulator()
        sim.apply_preset_state(1)  # preset 1 active before learning
        cfg = parse_config(dict(CONFIG, serial={"protocol": "designer", "port": url}, presets={"Show": 0, "Conf": 1, "C": 2}))
        cfg.signatures_path = str(tmp_path / "preset-signatures.json")
        await learn_presets(cfg)
        assert sim.presets == [0, 1, 2, 1]  # learned 0, 1, 2 then restored 1
        # start-up: the bridge recognises the active preset from the downloaded config
        link = make(url)
        bridge = Bridge(cfg, link)
        await bridge.start()
        await wait_for(lambda: bridge.active_preset == 1)
        assert bridge.state()["active_preset"] == 1
        await bridge.activate_preset("C")
        assert bridge.active_preset == 2
        await bridge.stop()

    asyncio.run(run())
