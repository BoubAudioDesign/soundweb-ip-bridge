"""Bridge logic shared by the TCP and HTTP front-ends."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Set

from . import fingerprint
from . import protocol as p
from .config import Config, Selector, Volume, group_number
from .serial_link import SendError, SerialLink

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, config: Config, link: Optional[SerialLink] = None) -> None:
        self.config = config
        if link is None:
            if config.protocol == "designer":
                from .designer_link import DesignerLink

                link = DesignerLink(
                    config.serial_port,
                    baudrate=config.baudrate,
                    rtscts=config.rtscts,
                    ack_timeout=config.ack_timeout,
                    retries=config.retries,
                )
                for sel in config.selectors.values():
                    missing = [n for n in sel.sources if n not in sel.source_presets]
                    if missing:
                        log.warning(
                            "Designer protocol: selector %r sources %s have no 'preset' and cannot be selected",
                            sel.name, missing,
                        )
            else:
                link = SerialLink(
                    config.serial_port,
                    baudrate=config.baudrate,
                    rtscts=config.rtscts,
                    ack_timeout=config.ack_timeout,
                    retries=config.retries,
                )
        self.link = link
        self.link.on_message = self._on_message
        self.link.on_link = self._on_link
        self.sources: Dict[str, Optional[int]] = {name: None for name in config.selectors}
        self.volumes: Dict[str, Optional[float]] = {name: None for name in config.volumes}
        self.active_preset: Optional[int] = None  # last preset recalled / recognised (None = unknown)
        self._volume_hold: Dict[str, float] = {}  # volume name -> monotonic deadline (keep_on_preset)
        self._subscribers: Set[asyncio.Queue] = set()

    async def start(self) -> None:
        await self.link.start()

    async def stop(self) -> None:
        await self.link.stop()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _publish(self, event: Dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def selector(self, name: str) -> Selector:
        sel = self.config.selectors.get(name)
        if sel is None:
            for s in self.config.selectors.values():
                if s.name.lower() == name.lower():
                    return s
            raise KeyError(f"unknown selector {name!r}")
        return sel

    def source_event(self, sel: Selector, origin: str) -> Dict[str, Any]:
        n = self.sources.get(sel.name)
        return {
            "type": "source",
            "selector": sel.name,
            "source": n,
            "label": sel.sources.get(n) if n is not None else None,
            "origin": origin,
        }

    def state(self) -> Dict[str, Any]:
        return {
            "connected": self.link.connected,
            "selectors": [
                {
                    "name": s.name,
                    "label": s.label,
                    "source": self.sources.get(s.name),
                    "sources": [{"number": n, "label": lbl} for n, lbl in s.sources.items()],
                }
                for s in self.config.selectors.values()
            ],
            "presets": [{"name": k, "id": v} for k, v in self.config.presets.items()],
            "active_preset": self.active_preset,
            "volumes": [
                {
                    "name": v.name, "label": v.label, "db": self.volumes.get(v.name),
                    "min": v.min_db, "max": v.max_db, "law": v.law,
                }
                for v in self.config.volumes.values()
            ],
        }

    # ---- volumes
    def volume(self, name: str) -> Volume:
        vol = self.config.volumes.get(name)
        if vol is None:
            for v in self.config.volumes.values():
                if v.name.lower() == name.lower():
                    return v
            raise KeyError(f"unknown volume {name!r}")
        return vol

    def volume_event(self, vol: Volume, origin: str) -> Dict[str, Any]:
        return {"type": "volume", "name": vol.name, "db": self.volumes.get(vol.name), "origin": origin}

    async def set_volume(self, name: str, db: Any) -> Dict[str, Any]:
        vol = self.volume(name)
        text = str(db).strip().lower()
        if text in ("-inf", "min", "off", "-∞"):
            value = vol.min_db
        elif text == "max":
            value = vol.max_db
        else:
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                raise ValueError(f"invalid volume {db!r} (dB, 'min', 'max' or '-inf')") from None
        raw = vol.encode(value)
        await self.link.send(p.build_raw(vol.handle, vol.parameter, raw & 0xFFFF, 2))
        self.volumes[vol.name] = vol.decode(raw & 0xFFFF)
        event = self.volume_event(vol, "remote")
        self._publish(event)
        log.info("Volume %s -> %s dB", vol.name, self.volumes[vol.name])
        return event

    PRESET_HOLD = 2.0  # s: window after a preset recall during which the Soundweb's volume re-read is overridden

    def _hold_volumes(self) -> None:
        """Called on every preset recall: the 9088 then re-applies the physical pot position to the gain."""
        deadline = time.monotonic() + self.PRESET_HOLD
        for vol in self.config.volumes.values():
            if vol.keep_on_preset and self.volumes.get(vol.name) is not None:
                self._volume_hold[vol.name] = deadline

    async def _restore_volume(self, vol: Volume, db: float) -> None:
        try:
            await self.link.send(p.build_raw(vol.handle, vol.parameter, vol.encode(db) & 0xFFFF, 2))
            log.info("Volume %s restored to %s dB after preset recall", vol.name, db)
        except SendError as e:
            log.warning("Cannot restore volume %s after preset: %s", vol.name, e)

    def _apply_parameter(self, handle: int, method: int, value: int, origin: str) -> bool:
        """Reflect a parameter value reported by the Soundweb on the configured volumes."""
        hit = False
        for vol in self.config.volumes.values():
            if vol.handle == handle and vol.parameter == method:
                hit = True
                db = vol.decode(value & 0xFFFF)
                held = self.volumes.get(vol.name)
                if time.monotonic() < self._volume_hold.get(vol.name, 0.0) and held is not None:
                    # value re-read by the 9088 because of a preset recall: keep ours
                    if vol.encode(db) != vol.encode(held):
                        asyncio.ensure_future(self._restore_volume(vol, held))
                    continue
                self.volumes[vol.name] = db
                self._publish(self.volume_event(vol, origin))
        return hit

    async def select_source(self, selector: str, source: Any) -> Dict[str, Any]:
        sel = self.selector(selector)
        n = sel.resolve(source)
        preset_value = sel.source_presets.get(n)
        if preset_value is None and self.config.protocol == "designer":
            raise ValueError(
                f"source {n} of selector {sel.name!r} has no 'preset' (Designer protocol: sources by preset only)"
            )
        if preset_value is not None:
            body = p.build_preset(preset_value)
            self._hold_volumes()
        elif sel.handle is not None:
            body = p.build_source_select(sel.handle, n)
        elif sel.set_value_id is not None:
            body = p.build_set_value(p.GROUPS["source"], sel.set_value_id, n)
        else:
            raise ValueError(f"source {n} of selector {sel.name!r} has no preset and the selector has no handle")
        await self.link.send(body)
        # Soundweb does not echo changes it receives, so reflect it ourselves.
        if preset_value is not None:
            # a preset may also switch other zones: update every selector bound to it
            self._preset_event(preset_value, "remote")
            self._apply_preset(preset_value, "remote", primary=sel.name)
        self.sources[sel.name] = n
        event = self.source_event(sel, "remote")
        self._publish(event)
        log.info("Source %s -> %s%s", sel.name, n, f" (preset {preset_value:#x})" if preset_value is not None else "")
        return event

    def _apply_preset(self, preset_value: int, origin: str, primary: Optional[str] = None) -> None:
        """Reflect a preset recall on the selectors whose sources are bound to that preset."""
        for sel in self.config.selectors.values():
            if sel.name == primary:
                continue
            for n, pv in sel.source_presets.items():
                if pv == preset_value:
                    self.sources[sel.name] = n
                    self._publish(self.source_event(sel, origin))
                    log.info("Source %s -> %s (preset %#x)", sel.name, n, preset_value)
                    break

    async def activate_preset(self, preset: Any) -> int:
        text = str(preset).strip()
        if text in self.config.presets:
            pid = self.config.presets[text]
        else:
            try:
                pid = int(text, 0)
            except ValueError:
                raise KeyError(f"unknown preset {text!r}") from None
        self._hold_volumes()
        await self.link.send(p.build_preset(pid))
        self._preset_event(pid, "remote")
        self._apply_preset(pid, "remote")
        return pid

    async def activate_parameter_preset(self, parameter_preset_id: int, state_index: int) -> None:
        self._hold_volumes()
        await self.link.send(p.build_parameter_preset(parameter_preset_id, state_index))
        self._apply_preset(((parameter_preset_id & 0xFFFF) << 16) | (state_index & 0xFFFF), "remote")

    async def send_raw(self, handle: int, method: int, value: int, value_size: Optional[int] = None) -> None:
        await self.link.send(p.build_raw(handle, method, value, value_size))

    async def send_set_value(self, group: Any, control_id: int, value: int) -> None:
        await self.link.send(p.build_set_value(group_number(group), control_id, value))

    async def send_body(self, body: bytes) -> None:
        await self.link.send(body)

    def _preset_event(self, pid: int, origin: str) -> None:
        self.active_preset = pid
        self._publish({"type": "preset", "id": pid, "origin": origin})

    def _on_link(self, up: bool) -> None:
        self._publish({"type": "link", "connected": up})
        # Designer protocol: parameter values read from the downloaded config give the initial state
        values = getattr(self.link, "parameter_values", None) if up else None
        if values and self.config.signatures_path:
            signatures = fingerprint.load(self.config.signatures_path)
            pid = fingerprint.match(signatures, values) if signatures else None
            if pid is not None:
                log.info("Active preset recognised: %d", pid)
                self._preset_event(pid, "soundweb")
            elif signatures:
                log.info("Active preset not recognised (no matching signature)")
        if values:
            for vol in self.config.volumes.values():
                if (vol.handle, vol.parameter) in values:
                    self._apply_parameter(vol.handle, vol.parameter, values[(vol.handle, vol.parameter)], "soundweb")

    def _on_message(self, msg: p.Message) -> None:
        log.info("From Soundweb: %s", msg)
        if isinstance(msg, p.RawMessage) and self._apply_parameter(msg.handle, msg.method, msg.value, "soundweb"):
            return
        for sel in self.config.selectors.values():
            if (
                isinstance(msg, p.RawMessage)
                and sel.handle is not None
                and msg.handle == sel.handle
                and msg.method == p.METHOD_SOURCE_SELECT
            ) or (
                isinstance(msg, p.SetValue)
                and sel.set_value_id is not None
                and msg.group == p.GROUPS["source"]
                and msg.control_id == sel.set_value_id
            ):
                self.sources[sel.name] = msg.value
                self._publish(self.source_event(sel, "soundweb"))
                return
        if (
            isinstance(msg, p.RawMessage)
            and msg.handle == p.BROADCAST_HANDLE
            and msg.method == p.METHOD_PRESET_ACTIVATE
        ):
            self._hold_volumes()
            self._preset_event(msg.value, "soundweb")
            self._apply_preset(msg.value, "soundweb")
            return
        if isinstance(msg, p.RawMessage):
            self._publish({"type": "raw", "handle": msg.handle, "method": msg.method, "value": msg.value})
        elif isinstance(msg, p.SetValue):
            self._publish({"type": "set_value", "group": msg.group, "id": msg.control_id, "value": msg.value})
        else:
            self._publish({"type": "unknown", "hex": msg.body.hex(" ")})
