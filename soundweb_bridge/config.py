from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import yaml

from .protocol import GROUPS

DEFAULT_SERIAL_PORT = "COM3" if os.name == "nt" else "/dev/ttyUSB0"

# Serial defaults per protocol (each can be overridden in the 'serial' section)
PROTOCOL_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "thirdparty": {"baudrate": 38400, "rtscts": False, "ack_timeout": 1.0},  # rear port in "+s" mode (BSS SDK)
    "designer": {"baudrate": 115200, "rtscts": True, "ack_timeout": 0.5},    # rear port in Soundweb Designer mode
}


def _int(v: Any, what: str) -> int:
    if isinstance(v, bool):
        raise ValueError(f"{what}: invalid number {v!r}")
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip(), 0)
    except ValueError:
        raise ValueError(f"{what}: invalid number {v!r}") from None


@dataclass
class Selector:
    name: str
    label: str
    sources: Dict[int, str]
    handle: Optional[int] = None
    set_value_id: Optional[int] = None
    # source number -> 32-bit preset activation value (global preset id, or (param_preset_id << 16) | state)
    source_presets: Dict[int, int] = field(default_factory=dict)

    def resolve(self, source: Any) -> int:
        """Accept a source number or (case-insensitive) label."""
        text = str(source).strip()
        try:
            n = int(text, 0)
        except ValueError:
            for num, lbl in self.sources.items():
                if lbl.lower() == text.lower():
                    return num
            raise ValueError(f"unknown source {text!r} for selector {self.name!r}") from None
        if not 0 <= n <= 16:
            raise ValueError("source must be 0 (none) to 16")
        return n


@dataclass
class Volume:
    """A 16-bit gain parameter (fader / pot object) driven as a slider, in dB."""

    name: str
    label: str
    handle: int
    parameter: int = 0x01000830  # RAW_MSG method of the gain parameter
    min_db: float = -100.0       # bottom of the slider (shown as "-inf" when <= -80 dB)
    max_db: float = 0.0
    scale: float = 256.0         # transmitted value = round(dB * scale), signed 16-bit
    law: str = "log"             # slider law on the remote: "log" (fader, dB = 40*log10(position)) or "linear"
    keep_on_preset: bool = True  # after a preset recall, re-send our volume if the 9088 re-applies another value

    def encode(self, db: float) -> int:
        db = min(max(db, self.min_db), self.max_db)
        return max(-32768, min(32767, round(db * self.scale)))

    def decode(self, value: int) -> float:
        if value >= 0x8000:
            value -= 0x10000
        return round(value / self.scale, 2)


@dataclass
class Config:
    serial_port: str = DEFAULT_SERIAL_PORT
    protocol: str = "thirdparty"  # "thirdparty" (device name "+s", SDK protocol) or "designer" (Designer mode)
    baudrate: int = 38400
    rtscts: bool = False
    ack_timeout: float = 1.0
    retries: int = 3
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    token: str = ""
    tcp_host: str = "0.0.0.0"
    tcp_port: int = 4999
    selectors: Dict[str, Selector] = field(default_factory=dict)
    presets: Dict[str, int] = field(default_factory=dict)
    volumes: Dict[str, Volume] = field(default_factory=dict)
    signatures_path: str = ""  # preset signatures file (Designer protocol), next to config.yaml


def parse_config(data: Dict[str, Any]) -> Config:
    data = data or {}
    s = data.get("serial") or {}
    h = data.get("http") or {}
    t = data.get("tcp") or {}
    protocol = str(s.get("protocol", "thirdparty")).strip().lower()
    if protocol not in PROTOCOL_DEFAULTS:
        raise ValueError(f"serial.protocol: {protocol!r} (expected 'thirdparty' or 'designer')")
    d = PROTOCOL_DEFAULTS[protocol]
    cfg = Config(
        serial_port=str(s.get("port", DEFAULT_SERIAL_PORT)),
        protocol=protocol,
        baudrate=_int(s.get("baudrate", d["baudrate"]), "serial.baudrate"),
        rtscts=bool(s.get("rtscts", d["rtscts"])),
        ack_timeout=float(s.get("ack_timeout", d["ack_timeout"])),
        retries=_int(s.get("retries", 3), "serial.retries"),
        http_host=str(h.get("host", "0.0.0.0")),
        http_port=_int(h.get("port", 8080), "http.port"),
        token=str(h.get("token", "") or ""),
        tcp_host=str(t.get("host", "0.0.0.0")),
        tcp_port=_int(t.get("port", 4999), "tcp.port"),
    )
    for name, pid in (data.get("presets") or {}).items():
        cfg.presets[str(name)] = _int(pid, f"presets.{name}")
    for name, sel in (data.get("selectors") or {}).items():
        name = str(name)
        if " " in name:
            raise ValueError(f"selector name {name!r} must not contain spaces")
        sel = sel or {}
        handle = sel.get("handle")
        sv_id = sel.get("set_value_id")
        sources: Dict[int, str] = {}
        source_presets: Dict[int, int] = {}
        for k, v in (sel.get("sources") or {}).items():
            n = _int(k, f"selectors.{name}.sources")
            if not 0 <= n <= 16:
                raise ValueError(f"selector {name!r}: source {n} out of range 0-16")
            what = f"selectors.{name}.sources.{n}"
            if isinstance(v, dict):
                sources[n] = str(v.get("label", f"Source {n}"))
                pv = _source_preset(v, cfg.presets, what)
                if pv is not None:
                    source_presets[n] = pv
            else:
                sources[n] = str(v)
        if handle is None and sv_id is None:
            missing = [n for n in sources if n not in source_presets]
            if not sources or missing:
                raise ValueError(
                    f"selector {name!r}: set 'handle' (RAW_MSG) or 'set_value_id' (SET_VALUE), "
                    f"or a 'preset' / 'parameter_preset' on every source (missing: {missing or 'no sources'})"
                )
        cfg.selectors[name] = Selector(
            name=name,
            label=str(sel.get("label", name)),
            sources=dict(sorted(sources.items())),
            handle=_int(handle, f"selectors.{name}.handle") if handle is not None else None,
            set_value_id=_int(sv_id, f"selectors.{name}.set_value_id") if sv_id is not None else None,
            source_presets=source_presets,
        )
    for name, v in (data.get("volumes") or {}).items():
        name = str(name)
        if " " in name:
            raise ValueError(f"volume name {name!r} must not contain spaces")
        v = v or {}
        if "handle" not in v:
            raise ValueError(f"volumes.{name}: 'handle' is required (handle of the gain / pot object)")
        param = _int(v.get("parameter", 0x01000830), f"volumes.{name}.parameter")
        if param < 0x100:  # short form: parameter index -> 16-bit "set parameter" method 0x010008xx
            param |= 0x01000800
        vol = Volume(
            name=name,
            label=str(v.get("label", name)),
            handle=_int(v["handle"], f"volumes.{name}.handle"),
            parameter=param,
            min_db=float(v.get("min_db", -100.0)),
            max_db=float(v.get("max_db", 0.0)),
            scale=float(v.get("scale", 256.0)),
            law=str(v.get("law", "log")).strip().lower(),
            keep_on_preset=bool(v.get("keep_on_preset", True)),
        )
        if vol.law not in ("log", "linear"):
            raise ValueError(f"volumes.{name}.law: {vol.law!r} (expected 'log' or 'linear')")
        if not vol.min_db < vol.max_db:
            raise ValueError(f"volumes.{name}: min_db must be lower than max_db")
        cfg.volumes[name] = vol
    return cfg


def _source_preset(src: Dict[str, Any], presets: Dict[str, int], what: str) -> Optional[int]:
    """Preset activation value for a source entry, or None if it uses the selector's handle/set_value_id.

    preset: <name from the 'presets' section | index>
    parameter_preset: [<parameter preset id>, <state index>]  (or {id: .., state: ..})
    """
    if "preset" in src and "parameter_preset" in src:
        raise ValueError(f"{what}: use either 'preset' or 'parameter_preset', not both")
    if "preset" in src:
        ref = src["preset"]
        if isinstance(ref, str) and ref.strip() in presets:
            return presets[ref.strip()]
        try:
            pid = _int(ref, f"{what}.preset")
        except ValueError:
            raise ValueError(f"{what}.preset: unknown preset {ref!r} (not in 'presets' and not a number)") from None
        if not 0 <= pid <= 0xFFFFFFFF:
            raise ValueError(f"{what}.preset: out of range")
        return pid
    if "parameter_preset" in src:
        pp = src["parameter_preset"]
        if isinstance(pp, dict):
            pp = [pp.get("id"), pp.get("state")]
        if not isinstance(pp, (list, tuple)) or len(pp) != 2 or None in pp:
            raise ValueError(f"{what}.parameter_preset: expected [<id>, <state>]")
        pp_id = _int(pp[0], f"{what}.parameter_preset id")
        state = _int(pp[1], f"{what}.parameter_preset state")
        if not (0 <= pp_id <= 0xFFFF and 0 <= state <= 0xFFFF):
            raise ValueError(f"{what}.parameter_preset: id and state must be 0-65535")
        return (pp_id << 16) | state
    return None


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as f:
        cfg = parse_config(yaml.safe_load(f))
    cfg.signatures_path = os.path.join(os.path.dirname(os.path.abspath(path)), "preset-signatures.json")
    return cfg


def group_number(group: Any) -> int:
    text = str(group).strip().lower()
    if text in GROUPS:
        return GROUPS[text]
    return _int(text, "group")
