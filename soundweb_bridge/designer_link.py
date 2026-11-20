"""Soundweb Designer protocol link (rear port in Designer mode, no "+s" needed).

The 9088 rear port, when NOT in third-party mode, speaks the protocol used by Soundweb Designer
(undocumented by BSS). It was reverse-engineered from captures of Designer <-> 9088 traffic:

- 115200 bauds 8N1, RTS/CTS flow control;
- SLIP framing: frames delimited by 0xC0, 0xC0/0xDB escaped as DB DC / DB DD;
- last byte of every frame = XOR of the other bytes;
- keep-alive body ``02 02`` about every 100 ms in both directions;
- "numbered" messages (sequence 0..4) acknowledged by the other side with ``02 <next seq>``;
- broadcast messages (``ff 02 04 ...``) not acknowledged; preset recall is one of them.

A session is: sync -> hello + discover -> identity -> PC time + connect(device id) -> config download
-> online -> (commands...) -> offline. Commands: preset recall, and setting a 16-bit parameter
(numbered message addressed to ``handle``, e.g. a gain). Parameter-change notifications are forwarded.

Exposes the same interface as :class:`SerialLink` (start/stop/send/connected/on_link/on_message),
``send()`` accepting the third-party message bodies built by :mod:`protocol` (preset activations).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import logging
import queue
import re
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import serial

from . import protocol as p
from .serial_link import SendError

log = logging.getLogger(__name__)

END, ESC, ESC_END, ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD
KEEPALIVE = b"\x02\x02"
SYNC_FRAME = b"\xc0\xc0"
HELLO = bytes.fromhex("ff 02 02 ff 00 00 ff ff ff ff ff ff 00 00 00 00 00 00 00 00")
SESSION = bytes.fromhex("ff 02 02 ff 00 00 ff ff 00 00 00 01 00 00 00 00 00 00 00 00")
BCAST = bytes.fromhex("ff 02 04 ff 00 00 ff ff 01 ff ff ff ff 00 00 00 00 00 00 00")  # + msg id byte + payload
PC_HDR = bytes.fromhex("01 02 01 ff 00 00 00 00")
SW_HDR = bytes.fromhex("00 02 01 ff 00 00 00 00")
PC_ADDR = bytes.fromhex("00 00 00 01 00 00 00 00")
PRESET_TAIL = bytes.fromhex("00 00 00 80 db 59 00 00 00 00 00")  # constant observed in every Designer recall

MSG_IDENTITY = 0x00
MSG_NOTIFY = 0x01  # Soundweb -> PC: parameter changed (handle, method, value)
MSG_CONNECT_REPLY = 0x02
MSG_ONLINE = 0x05
MSG_OFFLINE = 0x06
MSG_CFG_END = 0x27
MSG_CFG_BLOCK_REPLY = 0x29
MSG_CFG_END_REPLY = 0x2A
MSG_CFG_START = 0x2E
MSG_CFG_START_REPLY = 0x2F
MSG_CFG_BLOCK = 0x30
SEQ_MOD = 5
CFG_BLOCK = 0x78


class SessionError(Exception):
    pass


# --------------------------------------------------------------------------- framing helpers
def checksum(body: bytes) -> int:
    c = 0
    for b in body:
        c ^= b
    return c


def encode(body: bytes) -> bytes:
    """SLIP-frame a body (checksum appended)."""
    out = bytearray((END,))
    for b in body + bytes((checksum(body),)):
        if b == END:
            out += bytes((ESC, ESC_END))
        elif b == ESC:
            out += bytes((ESC, ESC_ESC))
        else:
            out.append(b)
    out.append(END)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == ESC:
            n = next(it, None)
            out.append(END if n == ESC_END else ESC if n == ESC_ESC else (n or 0))
        else:
            out.append(b)
    return bytes(out)


class Splitter:
    """Splits a byte stream into checked frame bodies (checksum removed); bad frames yield None."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[Optional[bytes]]:
        self._buf += data
        frames: List[Optional[bytes]] = []
        while END in self._buf:
            i = self._buf.index(END)
            raw, self._buf = bytes(self._buf[:i]), self._buf[i + 1:]
            if not raw:
                continue
            body = unescape(raw)
            if len(body) < 2 or checksum(body[:-1]) != body[-1]:
                frames.append(None)
            else:
                frames.append(body[:-1])
        return frames


def numbered(seq: int, msg_id: int, args: bytes = b"", addr: bytes = PC_ADDR) -> bytes:
    """Numbered PC message. ``addr`` = 8 bytes: session address, or ``handle(4) + 0(4)`` for a parameter."""
    return PC_HDR + bytes((0x00, seq)) + addr + struct.pack(">I", msg_id) + args


def parse_config(data: bytes) -> Dict[Tuple[int, int], int]:
    """Parameter values from the downloaded config: {(handle, method): 16-bit value}.

    Layout: 'DF' + u32 length + 6 header bytes, then records ``handle(u32) count(u16)`` followed by
    ``count`` entries ``method(u32) value(u16) pad(u16)``.
    """
    values: Dict[Tuple[int, int], int] = {}
    if data[:2] != b"DF":
        return values
    pos = 12
    while pos + 6 <= len(data):
        handle, count = struct.unpack(">IH", data[pos:pos + 6])
        end = pos + 6 + 8 * count
        if end > len(data):
            break
        for i in range(count):
            method, value = struct.unpack(">IH", data[pos + 6 + 8 * i:pos + 12 + 8 * i])
            values[(handle, method)] = value
        pos = end
    return values


def ack_body(next_seq: int) -> bytes:
    return PC_HDR + bytes((0x02, next_seq))


def preset_body(preset: int) -> bytes:
    return BCAST + bytes((p.METHOD_PRESET_ACTIVATE,)) + b"\x00\x00\x00" + bytes((preset, preset)) + PRESET_TAIL


def msg_id_of(payload: bytes) -> Optional[int]:
    return struct.unpack(">I", payload[8:12])[0] if len(payload) >= 12 else None


def ole_now() -> float:
    return (dt.datetime.now() - dt.datetime(1899, 12, 30)).total_seconds() / 86400.0


# --------------------------------------------------------------------------- link
class DesignerLink:
    def __init__(
        self,
        url: str,
        baudrate: int = 115200,
        rtscts: bool = True,
        ack_timeout: float = 0.5,
        retries: int = 3,
        reconnect_delay: float = 3.0,
        link_timeout: float = 3.0,
        on_message: Optional[Callable[[p.Message], None]] = None,
        on_link: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.url = url
        self.baudrate = baudrate
        self.rtscts = rtscts
        self.ack_timeout = ack_timeout
        self.retries = retries
        self.reconnect_delay = reconnect_delay
        self.link_timeout = link_timeout
        self.on_message = on_message
        self.on_link = on_link
        self.device_id: Optional[bytes] = None
        self.firmware = ""
        self.config_data = b""
        self._online = False
        self._shutting_down = False
        self._closed = threading.Event()
        self._commands: "queue.Queue[tuple[tuple, concurrent.futures.Future]]" = queue.Queue()
        self.parameter_values: Dict[Tuple[int, int], int] = {}
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ser: Optional[serial.SerialBase] = None
        self._reset_session()

    # ---- public interface (same as SerialLink)
    @property
    def connected(self) -> bool:
        return self._online

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, name="designer-link", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._closed.set()
        if self._thread:
            await asyncio.get_running_loop().run_in_executor(None, self._thread.join, 5)

    async def send(self, body: bytes) -> None:
        """Accepts preset activations and 16-bit 'set parameter' RAW messages (methods 0x01xxxxxx)."""
        msg = p.parse_body(body)
        if not isinstance(msg, p.RawMessage):
            raise SendError("Designer protocol: only preset recalls and RAW parameter messages are supported")
        if msg.handle == p.BROADCAST_HANDLE and msg.method == p.METHOD_PRESET_ACTIVATE:
            if not 0 <= msg.value <= 0xFF:
                raise SendError("Designer protocol: preset index must be 0-255 (parameter presets not supported)")
            cmd: tuple = ("preset", msg.value)
        elif msg.handle != p.BROADCAST_HANDLE and (msg.method >> 24) == 0x01 and 0 <= msg.value <= 0xFFFF:
            cmd = ("param", msg.handle, msg.method, msg.value)
        else:
            raise SendError("Designer protocol: unsupported message (preset recall or 16-bit RAW parameter only)")
        if not self._online:
            raise SendError("Designer session not established")
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._commands.put((cmd, fut))
        await asyncio.wrap_future(fut)

    # ---- thread side
    def _reset_session(self) -> None:
        self._splitter = Splitter()
        self._pc_seq = 0
        self._acks: List[int] = []
        self._resync: Optional[int] = None
        self._data: List[bytes] = []
        self._last_ka_tx = 0.0
        self._last_rx = time.monotonic()

    def _notify(self, up: bool) -> None:
        if self._online == up:
            return
        self._online = up
        if self.on_link and self._loop:
            try:
                self._loop.call_soon_threadsafe(self.on_link, up)
            except RuntimeError:
                pass

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                self._ser = serial.serial_for_url(
                    self.url, baudrate=self.baudrate, rtscts=self.rtscts, timeout=0.01, write_timeout=2
                )
            except (serial.SerialException, OSError, ValueError) as e:
                log.warning("Cannot open %s: %s (retrying in %.0fs)", self.url, e, self.reconnect_delay)
                self._closed.wait(self.reconnect_delay)
                continue
            try:
                self._reset_session()
                self._open_session()
                self._notify(True)
                self._online_loop()
            except (SessionError, serial.SerialException, OSError) as e:
                if not self._closed.is_set():
                    log.warning("Designer session lost: %s", e)
            finally:
                self._notify(False)
                self._fail_pending("Designer session lost")
                try:
                    self._ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ser = None
            if not self._closed.is_set():
                self._closed.wait(self.reconnect_delay)

    def _fail_pending(self, reason: str) -> None:
        while True:
            try:
                _, fut = self._commands.get_nowait()
            except queue.Empty:
                return
            if not fut.done():
                fut.set_exception(SendError(reason))

    def _write(self, body: bytes) -> None:
        if log.isEnabledFor(logging.DEBUG) and body != KEEPALIVE:
            log.debug("TX %s", body.hex(" "))
        self._ser.write(encode(body))

    def _pump(self, seconds: float, until: Optional[Callable[[], bool]] = None, keepalive: bytes = KEEPALIVE) -> bool:
        """Read/dispatch frames and keep the link alive for up to ``seconds``; stops early when ``until()``."""
        end = time.monotonic() + seconds
        while True:
            now = time.monotonic()
            if now - self._last_ka_tx >= 0.1:
                if keepalive == SYNC_FRAME:
                    self._ser.write(SYNC_FRAME)
                else:
                    self._write(keepalive)
                self._last_ka_tx = now
            data = self._ser.read(self._ser.in_waiting or 1)
            if data:
                self._last_rx = time.monotonic()
                for body in self._splitter.feed(data):
                    self._dispatch(body)
            if until and until():
                return True
            if time.monotonic() >= end or (self._closed.is_set() and not self._shutting_down):
                return False

    def _dispatch(self, body: Optional[bytes]) -> None:
        if body is None:
            log.debug("RX bad frame (checksum)")
            return
        if body == KEEPALIVE:
            return
        log.debug("RX %s", body.hex(" "))
        if body[:8] == SW_HDR and len(body) >= 10:
            if body[8] == 0x02:
                self._acks.append(body[9])
            elif body[8] == 0x03:
                # out of sequence: the 9088 keeps its numbering across sessions and tells which one it expects
                self._resync = body[9]
            elif body[8] == 0x00:
                self._write(ack_body((body[9] + 1) % SEQ_MOD))
                self._data.append(body[10:])

    def _take(self, msg_id: int) -> Optional[bytes]:
        for i, payload in enumerate(self._data):
            if msg_id_of(payload) == msg_id:
                return self._data.pop(i)
        return None

    def _wait_msg(self, msg_id: int, timeout: float, what: str) -> bytes:
        found: List[bytes] = []

        def check() -> bool:
            payload = self._take(msg_id)
            if payload is not None:
                found.append(payload)
            return bool(found)

        if not self._pump(timeout, check):
            raise SessionError(f"no reply from the Soundweb ({what})")
        return found[0]

    def _deliver_notifications(self) -> None:
        """Forward parameter-change notifications (msg 0x01: handle, method, value) to ``on_message``."""
        data, self._data = self._data, []
        for payload in data:
            args = payload[12:]
            if msg_id_of(payload) != MSG_NOTIFY or len(args) < 10:
                continue
            handle, method, value = struct.unpack(">IIH", args[:10])
            self.parameter_values[(handle, method)] = value
            if self.on_message and self._loop:
                try:
                    self._loop.call_soon_threadsafe(self.on_message, p.RawMessage(handle, method, value))
                except RuntimeError:
                    pass

    def _send_numbered(self, msg_id: int, args: bytes = b"", addr: bytes = PC_ADDR) -> None:
        attempts = 0
        resyncs = 0
        while attempts < self.retries:
            seq = self._pc_seq
            expected = (seq + 1) % SEQ_MOD
            self._acks.clear()
            self._resync = None
            self._write(numbered(seq, msg_id, args, addr))
            if self._pump(self.ack_timeout, lambda: expected in self._acks or self._resync is not None):
                if expected in self._acks:
                    self._pc_seq = expected
                    return
                if resyncs < SEQ_MOD:  # "expecting n": renumber and resend at once
                    log.debug("Soundweb expects sequence %d (was %d): resync", self._resync, seq)
                    self._pc_seq = self._resync % SEQ_MOD
                    resyncs += 1
                    continue
            attempts += 1
        raise SessionError(f"message {msg_id:#04x} not acknowledged")

    def _open_session(self) -> None:
        # 1. sync: empty frames until the Soundweb answers with keep-alives
        rx_before = self._last_rx
        if not self._pump(3.0, lambda: self._last_rx != rx_before, keepalive=SYNC_FRAME):
            raise SessionError("no answer on the serial port (Designer mode, 115200, RTS/CTS?)")
        self._pump(0.3, keepalive=SYNC_FRAME)
        # 2. hello + discover -> identity
        self._write(HELLO)
        self._write(BCAST + bytes((0x00,)))
        identity = self._wait_msg(MSG_IDENTITY, 3.0, "identity")
        if len(identity) < 46:
            raise SessionError("unexpected identity frame")
        self.device_id = identity[42:46]
        m = re.search(rb"[A-Z][a-z]{2} [ \d]\d \d{4}\x00\d\d:\d\d:\d\d", identity)
        self.firmware = m.group().replace(b"\x00", b" ").decode("ascii") if m else ""
        self._pump(0.3)
        # 3. PC time + connect to the device
        self._write(BCAST + bytes((0x64,)) + struct.pack("<d", ole_now()))
        self._write(BCAST + bytes((0x01,)) + b"\x00" * 4 + self.device_id + b"\x00\x00\x00\x01")
        self._wait_msg(MSG_CONNECT_REPLY, 3.0, "connect")
        self._pump(0.3)
        self._write(SESSION)
        self._pump(0.3)
        # 4. config download (what Designer does on "receive config")
        self._send_numbered(MSG_CFG_START, b"\x00\x02")
        self._wait_msg(MSG_CFG_START_REPLY, 2.0, "config start")
        config = bytearray()
        offset = 0
        for _ in range(512):
            self._send_numbered(MSG_CFG_BLOCK, b"\x00\x00\x00\x00" + struct.pack(">HH", offset, CFG_BLOCK))
            block = self._wait_msg(MSG_CFG_BLOCK_REPLY, 2.0, "config block")
            n = struct.unpack(">H", block[20:22])[0] if len(block) >= 22 else 0
            if n == 0:
                break
            config += block[22:22 + n]
            offset += n
        self.config_data = bytes(config)
        self.parameter_values = parse_config(self.config_data)
        self._send_numbered(MSG_CFG_END, b"\x00\x00")
        self._wait_msg(MSG_CFG_END_REPLY, 2.0, "config end")
        # 5. online
        self._send_numbered(MSG_ONLINE, b"\x00\x00\x00\x01" + b"\xff" * 8)
        self._pump(0.3)
        log.info(
            "Designer session open on %s (device %s, firmware %s, config %d bytes)",
            self.url, self.device_id.hex(" "), self.firmware or "?", len(self.config_data),
        )

    def _online_loop(self) -> None:
        while not self._closed.is_set():
            self._pump(0.05)
            if time.monotonic() - self._last_rx > self.link_timeout:
                raise SessionError(f"no keep-alive from the Soundweb for {self.link_timeout:.0f}s")
            self._deliver_notifications()
            try:
                cmd, fut = self._commands.get_nowait()
            except queue.Empty:
                continue
            if fut.done():
                continue
            try:
                if cmd[0] == "preset":
                    self._write(preset_body(cmd[1]))
                    log.info("Preset %d recalled (Designer protocol)", cmd[1])
                else:
                    _, handle, method, value = cmd
                    self._send_numbered(method, struct.pack(">H", value), addr=struct.pack(">II", handle, 0))
                    log.debug("Parameter %#x/%#010x = %#06x", handle, method, value)
                fut.set_result(None)
            except SessionError as e:
                fut.set_exception(SendError(str(e)))
                raise
            except (serial.SerialException, OSError) as e:
                fut.set_exception(SendError(f"serial write failed: {e}"))
                raise
        # clean shutdown: go offline
        self._shutting_down = True
        try:
            self._send_numbered(MSG_OFFLINE, b"\x00\x00\x00\x01" + b"\xff" * 8)
        except (SessionError, serial.SerialException, OSError):
            pass
