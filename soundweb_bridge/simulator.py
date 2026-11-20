"""Minimal Soundweb 9088 simulator for testing without hardware.

ACKs every valid frame, NAKs bad ones, keeps source-selector state and can push
front-panel changes back to the bridge.

Transport:
- Linux / macOS: a pseudo-terminal (the bridge opens it like a real serial port);
- Windows (no pty): a local TCP port, opened by the bridge as ``socket://127.0.0.1:<port>``.
``start_simulator()`` picks the right one automatically.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from . import protocol as p

log = logging.getLogger("simulator")

HAS_PTY = os.name == "posix"


class SoundwebSimulator:
    def __init__(self, write: Callable[[bytes], None], drop_acks: int = 0) -> None:
        self._write = write
        self._decoder = p.Decoder()
        self.received: list = []
        self.sources: Dict[int, int] = {}
        self.drop_acks = drop_acks
        self.connected = threading.Event()  # set once the bridge side is attached

    def feed(self, data: bytes) -> None:
        for item in self._decoder.feed(data):
            if isinstance(item, int):
                continue
            if item.body is None:
                self._write(bytes((p.NAK,)))
                continue
            msg = p.parse_body(item.body)
            self.received.append(msg)
            if self.drop_acks > 0:
                self.drop_acks -= 1
                log.info("(dropping ACK) %s", msg)
                continue
            self._write(bytes((p.ACK,)))
            if isinstance(msg, p.RawMessage) and msg.method == p.METHOD_SOURCE_SELECT:
                self.sources[msg.handle] = msg.value
            log.info("RX %s", msg)

    def front_panel_source(self, handle: int, source: int) -> None:
        """Simulate a change made on the Soundweb side (sent to the bridge)."""
        self.sources[handle] = source
        self._write(p.frame(p.build_source_select(handle, source)))


def start_pty_simulator(drop_acks: int = 0) -> Tuple[str, SoundwebSimulator]:
    """POSIX only: simulator behind a pseudo-terminal, returns its device path."""
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(slave)
    path = os.ttyname(slave)
    sim = SoundwebSimulator(lambda b: os.write(master, b), drop_acks)

    def loop() -> None:
        while True:
            try:
                data = os.read(master, 1024)
            except OSError:
                return
            if data:
                sim.feed(data)

    threading.Thread(target=loop, daemon=True).start()
    sim._slave_fd = slave  # keep the slave open so the pty stays alive
    sim.connected.set()
    return path, sim


def start_tcp_simulator(
    drop_acks: int = 0, host: str = "127.0.0.1", port: int = 0
) -> Tuple[str, SoundwebSimulator]:
    """Cross-platform: simulator on a TCP port (one bridge at a time), returns a pyserial ``socket://`` URL."""
    srv = socket.create_server((host, port))
    port = srv.getsockname()[1]
    sim = SoundwebSimulator(lambda b: None, drop_acks)

    def loop() -> None:
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                return
            log.info("Bridge connected from %s", addr)
            sim._write = conn.sendall
            sim._decoder = p.Decoder()
            sim.connected.set()
            try:
                while True:
                    data = conn.recv(1024)
                    if not data:
                        break
                    sim.feed(data)
            except OSError:
                pass
            finally:
                sim.connected.clear()
                sim._write = lambda b: None
                conn.close()
                log.info("Bridge disconnected")

    threading.Thread(target=loop, daemon=True).start()
    sim._server = srv
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"socket://{connect_host}:{port}", sim


class DesignerSimulator:
    """Soundweb 9088 rear port in *Designer* mode (protocol of :mod:`designer_link`), over TCP.

    Answers keep-alives, identity, connect, config download, online/offline; records preset recalls
    (``presets``) and sends the parameter-change notification observed on the real device.
    """

    DEVICE_ID = bytes.fromhex("06 31 79 70")

    def __init__(self, config_size: int = 300) -> None:
        self.presets: list = []
        self.online = False
        self.sessions = 0
        # parameter values {(handle, method): 16-bit value}; object 5 = volume pot (-8.27 dB as on the real unit)
        self.params = {(1, 0x01000819): 0, (5, 0x01000800): 0, (5, 0x01000830): 0xF7BC}
        self.preset_volumes = {0: 0xEC00, 1: 0xD800}  # volume applied by a preset recall (simulation)
        # parameters set by each preset on object 2 (router), like the real design: gives each preset a signature
        self.preset_states = {
            0: {(2, 0x01001000): 1, (2, 0x01001064): 1},
            1: {(2, 0x01001060): 1, (2, 0x01001150): 1},
            2: {(2, 0x01001002): 1, (2, 0x01000802): 0xF3CD},
        }
        self.apply_preset_state(0)
        self.connected = threading.Event()
        self.ignore_connect = False  # True: never answer the connect message (failure tests)
        self._conn: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._seq = 0          # Soundweb -> PC numbering (kept across connections, like the real unit)
        self._expect_pc = 0    # next PC sequence number expected (kept across connections)

    @property
    def config(self) -> bytes:
        """Config in the 9088 format: 'DF' header + records handle(4) count(2) [method(4) value(2) pad(2)]..."""
        import struct

        by_handle: dict = {}
        for (h, m), v in sorted(self.params.items()):
            by_handle.setdefault(h, []).append((m, v))
        recs = b"".join(
            struct.pack(">IH", h, len(ents)) + b"".join(struct.pack(">IHH", m, v, 0) for m, v in ents)
            for h, ents in by_handle.items()
        ) + b"**\x00\x00\x00\x00"
        return b"DF" + struct.pack(">I", len(recs) + 6) + b"\xff\xff\xff\xff\x00\x0c" + recs

    def apply_preset_state(self, preset: int) -> None:
        for state in self.preset_states.values():
            for key in state:
                self.params[key] = 0
        self.params.update(self.preset_states.get(preset, {}))

    def notify(self, handle: int, method: int) -> None:
        """Parameter-change notification (as sent by the 9088, e.g. after a preset or a front-panel move)."""
        import struct

        self._data(0x01, struct.pack(">IIH", handle, method, self.params[(handle, method)]) + b"\x00\x80")

    # --- helpers
    def _send(self, body: bytes) -> None:
        from .designer_link import encode

        conn = self._conn
        if conn is not None:
            with self._lock:
                try:
                    conn.sendall(encode(body))
                except OSError:
                    pass

    def _data(self, msg_id: int, args: bytes = b"", addr: bytes = bytes.fromhex("00 00 00 00 00 00 00 01")) -> None:
        import struct

        from .designer_link import SW_HDR

        body = SW_HDR + bytes((0x00, self._seq)) + addr + struct.pack(">I", msg_id) + args
        self._seq = (self._seq + 1) % 5
        self._send(body)

    def drop(self) -> None:
        """Simulate a cable unplug: close the current connection."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    # --- protocol
    def handle(self, body: bytes) -> None:
        import struct

        from . import designer_link as d

        if body == d.KEEPALIVE or body == d.HELLO:
            return
        if body == d.SESSION:
            self._send(bytes.fromhex("00 02 03 ff 00 00 00 00 00 00 00 01 00 10 00 00"))
            return
        if body.startswith(d.BCAST) and len(body) > len(d.BCAST):
            msg, payload = body[len(d.BCAST)], body[len(d.BCAST) + 1:]
            if msg == 0x00:  # discover -> identity
                ident = (b"\x00" * 12 + bytes.fromhex("00 a6 23 80 55 31 00 6e 14 77 6e 00 45 00 54") + b"\x00" * 15
                         + self.DEVICE_ID + bytes.fromhex("05 b5 cc ba 00 02 00 02 00 01 00 82 00 00")
                         + b"Apr 08 2004\x0012:58:31\x00" + bytes.fromhex("05 01 d0 00 00"))
                self._data(0x00, ident[12:], addr=bytes.fromhex("00 00 00 00 ff ff ff ff"))
            elif msg == 0x01 and payload[4:8] == self.DEVICE_ID and not self.ignore_connect:
                self._data(0x02, b"\x00\x00")
            elif msg == p.METHOD_PRESET_ACTIVATE and self.online:
                self.presets.append(payload[3])
                log.info("Designer sim: preset %d recalled", payload[3])
                self.apply_preset_state(payload[3])
                if payload[3] in self.preset_volumes:
                    self.params[(5, 0x01000830)] = self.preset_volumes[payload[3]]
                self.notify(5, 0x01000830)
            return
        if body[:8] == d.PC_HDR and len(body) >= 10 and body[8] == 0x00:
            seq = body[9]
            if seq != self._expect_pc:  # out of sequence: tell which number is expected, ignore the message
                self._send(d.SW_HDR + bytes((0x03, self._expect_pc)))
                return
            self._expect_pc = (seq + 1) % 5
            self._send(d.SW_HDR + bytes((0x02, self._expect_pc)))  # ACK
            payload = body[10:]
            msg = struct.unpack(">I", payload[8:12])[0]
            args = payload[12:]
            handle = struct.unpack(">I", payload[0:4])[0]
            if (msg >> 24) == 0x01 and handle != 0 and len(args) >= 2:  # set parameter on object 'handle'
                self.params[(handle, msg)] = struct.unpack(">H", args[:2])[0]
                log.info("Designer sim: parameter %#x/%#010x = %#06x", handle, msg, self.params[(handle, msg)])
            elif msg == d.MSG_CFG_START:
                self._data(d.MSG_CFG_START_REPLY, b"\x00\x00\x00\x00")
            elif msg == d.MSG_CFG_BLOCK:
                off, ln = struct.unpack(">HH", args[4:8])
                chunk = self.config[off:off + ln]
                self._data(d.MSG_CFG_BLOCK_REPLY, b"\x00\x00\x00\x00" + struct.pack(">IH", off, len(chunk)) + chunk)
            elif msg == d.MSG_CFG_END:
                self._data(d.MSG_CFG_END_REPLY)
            elif msg == d.MSG_ONLINE:
                self.online = True
                self.sessions += 1
            elif msg == d.MSG_OFFLINE:
                self.online = False

    def serve(self, srv: socket.socket) -> None:
        from .designer_link import KEEPALIVE, Splitter

        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                return
            log.info("Designer sim: bridge connected from %s", addr)
            self._conn, self.online = conn, False
            self.connected.set()
            stop = threading.Event()

            def keepalive(c: socket.socket = conn, ev: threading.Event = stop) -> None:
                while not ev.wait(0.2) and self._conn is c:
                    self._send(KEEPALIVE)

            threading.Thread(target=keepalive, daemon=True).start()
            splitter = Splitter()
            try:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    for body in splitter.feed(data):
                        if body is not None:
                            self.handle(body)
            except OSError:
                pass
            finally:
                stop.set()
                self.online = False
                self.connected.clear()
                if self._conn is conn:
                    self._conn = None
                conn.close()


def start_designer_simulator(host: str = "127.0.0.1", port: int = 0) -> Tuple[str, DesignerSimulator]:
    srv = socket.create_server((host, port))
    sim = DesignerSimulator()
    threading.Thread(target=sim.serve, args=(srv,), daemon=True).start()
    sim._server = srv
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"socket://{connect_host}:{srv.getsockname()[1]}", sim


def start_simulator(drop_acks: int = 0) -> Tuple[str, SoundwebSimulator]:
    """Start a simulator with the best transport for this OS; returns (serial URL/path, simulator)."""
    if HAS_PTY:
        return start_pty_simulator(drop_acks)
    return start_tcp_simulator(drop_acks)


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description="Soundweb 9088 simulator")
    ap.add_argument(
        "--tcp", type=int, metavar="PORT",
        help="listen on this TCP port (all interfaces) instead of a pty; default on Windows: 5000",
    )
    ap.add_argument("--designer", action="store_true",
                    help="simulate the rear port in Soundweb Designer mode (TCP; for serial.protocol: designer)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.designer:
        url, dsim = start_designer_simulator(host="0.0.0.0", port=args.tcp or 5000)
        print(f"Simulated Soundweb (Designer mode). In config.yaml use  protocol: designer  and  port: {url}")
        print("Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
    if args.tcp is None and not HAS_PTY:
        args.tcp = 5000
    if args.tcp is not None:
        url, sim = start_tcp_simulator(host="0.0.0.0", port=args.tcp)
        print(f"Simulated Soundweb listening on TCP port {args.tcp}. In config.yaml use  serial.port: {url}")
    else:
        url, sim = start_pty_simulator()
        print(f"Simulated Soundweb on {url}. In config.yaml use  serial.port: {url}")
    print("Type '<handle> <source>' to simulate a front-panel change (Ctrl+C to quit).")
    while True:
        try:
            line = input().lstrip("﻿")  # BOM sent by some Windows tools when piping stdin
        except (EOFError, KeyboardInterrupt):
            return
        try:
            h, s = line.split()
            sim.front_panel_source(int(h, 0), int(s, 0))
        except ValueError:
            print("format: <handle> <source>")


if __name__ == "__main__":
    main()
