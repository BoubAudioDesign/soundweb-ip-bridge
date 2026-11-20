"""Soundweb (original 9088/3088) RS-232 protocol, per BSS "Soundweb Serial Interface Kit".

<message> = STX <body> <checksum> ETX, checksum = XOR of body bytes,
special bytes in body/checksum are escaped as ESC (byte + 0x80).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, Optional, Union

STX = 0x02
ETX = 0x03
ACK = 0x06
NAK = 0x15
ESC = 0x1B
SPECIAL = frozenset({STX, ETX, ACK, NAK, ESC})

SET_VALUE = 0x80
SET_STRING = 0x81
REQUEST_VALUE = 0x82
REQUEST_STRING = 0x83
RAW_MSG = 0x84

GROUPS = {
    "button": 0,
    "toggle": 1,
    "led": 2,
    "preset": 3,
    "spin": 4,
    "level": 5,
    "source": 6,
    "text": 7,
}

BROADCAST_HANDLE = 0xFFFFFFFF
METHOD_PRESET_ACTIVATE = 0x0000000B
METHOD_SOURCE_SELECT = 0x01000800


def value_size_for_method(method: int) -> int:
    """Byte width of a RAW_MSG value: 32-bit for 0x02xxxxxx methods and preset activation, else 16-bit."""
    if method == METHOD_PRESET_ACTIVATE or (method >> 24) == 0x02:
        return 4
    return 2


def _escape(byte: int) -> bytes:
    return bytes((ESC, byte + 0x80)) if byte in SPECIAL else bytes((byte,))


def checksum(body: bytes) -> int:
    c = 0
    for b in body:
        c ^= b
    return c


def frame(body: bytes) -> bytes:
    out = bytearray((STX,))
    for b in body:
        out += _escape(b)
    out += _escape(checksum(body))
    out.append(ETX)
    return bytes(out)


def build_set_value(group: int, control_id: int, value: int) -> bytes:
    return bytes((SET_VALUE, group & 0xFF, control_id & 0xFF)) + struct.pack(">H", value & 0xFFFF)


def build_raw(handle: int, method: int, value: int, value_size: Optional[int] = None) -> bytes:
    size = value_size or value_size_for_method(method)
    fmt = ">IIH" if size == 2 else ">III"
    mask = 0xFFFF if size == 2 else 0xFFFFFFFF
    return bytes((RAW_MSG,)) + struct.pack(fmt, handle & 0xFFFFFFFF, method & 0xFFFFFFFF, value & mask)


def build_source_select(handle: int, source: int) -> bytes:
    if not 0 <= source <= 16:
        raise ValueError("source must be 0 (none) to 16")
    return build_raw(handle, METHOD_SOURCE_SELECT, source)


def build_preset(preset_id: int) -> bytes:
    return build_raw(BROADCAST_HANDLE, METHOD_PRESET_ACTIVATE, preset_id, 4)


def build_parameter_preset(parameter_preset_id: int, state_index: int) -> bytes:
    return build_preset(((parameter_preset_id & 0xFFFF) << 16) | (state_index & 0xFFFF))


@dataclass(frozen=True)
class SetValue:
    group: int
    control_id: int
    value: int


@dataclass(frozen=True)
class RawMessage:
    handle: int
    method: int
    value: int


@dataclass(frozen=True)
class Unknown:
    body: bytes


Message = Union[SetValue, RawMessage, Unknown]


def parse_body(body: bytes) -> Message:
    if len(body) == 5 and body[0] == SET_VALUE:
        return SetValue(body[1], body[2], struct.unpack(">H", body[3:5])[0])
    if body and body[0] == RAW_MSG and len(body) in (11, 13):
        handle, method = struct.unpack(">II", body[1:9])
        fmt = ">H" if len(body) == 11 else ">I"
        return RawMessage(handle, method, struct.unpack(fmt, body[9:])[0])
    return Unknown(bytes(body))


@dataclass(frozen=True)
class Frame:
    """A decoded frame; ``body`` is None for a bad checksum or malformed frame."""

    body: Optional[bytes]


class Decoder:
    """Incremental decoder yielding ACK/NAK control bytes (as ints) and Frame objects."""

    def __init__(self) -> None:
        self._buf: Optional[bytearray] = None
        self._escape = False

    def feed(self, data: bytes) -> Iterator[Union[int, Frame]]:
        for b in data:
            if b == STX:
                self._buf = bytearray()
                self._escape = False
            elif b == ETX:
                buf, self._buf = self._buf, None
                self._escape = False
                if buf is None or len(buf) < 2:
                    yield Frame(None)
                    continue
                body, csum = bytes(buf[:-1]), buf[-1]
                yield Frame(body if checksum(body) == csum else None)
            elif self._buf is None:
                if b in (ACK, NAK):
                    yield b
            elif b == ESC:
                self._escape = True
            else:
                self._buf.append((b - 0x80) & 0xFF if self._escape else b)
                self._escape = False
