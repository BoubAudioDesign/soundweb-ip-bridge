"""Reliable Soundweb serial link: framing, ACK/NAK handling, resend and auto-reconnect."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional

import serial

from . import protocol as p

log = logging.getLogger(__name__)


class SendError(Exception):
    pass


class SerialLink:
    def __init__(
        self,
        url: str,
        baudrate: int = 38400,
        rtscts: bool = False,
        ack_timeout: float = 1.0,
        retries: int = 3,
        reconnect_delay: float = 3.0,
        on_message: Optional[Callable[[p.Message], None]] = None,
        on_link: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.url = url
        self.baudrate = baudrate
        self.rtscts = rtscts
        self.ack_timeout = ack_timeout
        self.retries = retries
        self.reconnect_delay = reconnect_delay
        self.on_message = on_message
        self.on_link = on_link
        self._serial: Optional[serial.SerialBase] = None
        self._decoder = p.Decoder()
        self._queue: "asyncio.Queue[tuple[bytes, asyncio.Future]]" = asyncio.Queue()
        self._reply: Optional[asyncio.Future] = None
        self._tasks: list[asyncio.Task] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._serial is not None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._tasks = [asyncio.create_task(self._connect_loop()), asyncio.create_task(self._writer())]

    async def stop(self) -> None:
        self._closed = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._close_port()

    async def send(self, body: bytes) -> None:
        """Queue a message body and wait until the Soundweb ACKs it (raises SendError otherwise)."""
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put((body, fut))
        await fut

    async def _connect_loop(self) -> None:
        while not self._closed:
            if self._serial is None:
                try:
                    ser = serial.serial_for_url(
                        self.url, baudrate=self.baudrate, rtscts=self.rtscts, timeout=0.2, write_timeout=2
                    )
                except (serial.SerialException, OSError, ValueError) as e:
                    log.warning("Cannot open %s: %s (retrying in %.0fs)", self.url, e, self.reconnect_delay)
                else:
                    self._serial = ser
                    self._decoder = p.Decoder()
                    log.info("Serial link up on %s @ %d", self.url, self.baudrate)
                    threading.Thread(target=self._reader, args=(ser,), daemon=True).start()
                    self._notify_link(True)
            await asyncio.sleep(self.reconnect_delay)

    def _reader(self, ser: serial.SerialBase) -> None:
        while not self._closed and self._serial is ser:
            try:
                data = ser.read(ser.in_waiting or 1)
            # AttributeError: pyserial's socket:// backend when the port is closed from another thread
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if self._closed or self._serial is not ser:
                    return
                log.warning("Serial read error: %s", e)
                self._call(self._on_error, ser)
                return
            if data:
                self._call(self._on_bytes, data)

    def _call(self, fn: Callable, *args: object) -> None:
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # event loop already closed during shutdown
            pass

    def _on_error(self, ser: serial.SerialBase) -> None:
        if self._serial is ser:
            self._close_port()

    def _close_port(self) -> None:
        ser, self._serial = self._serial, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
            log.warning("Serial link down")
            self._notify_link(False)

    def _notify_link(self, up: bool) -> None:
        if self.on_link:
            self.on_link(up)

    def _write(self, data: bytes) -> None:
        if self._serial is None:
            raise SendError("serial port not connected")
        try:
            self._serial.write(data)
        except (serial.SerialException, OSError) as e:
            self._close_port()
            raise SendError(f"serial write failed: {e}") from e

    def _on_bytes(self, data: bytes) -> None:
        log.debug("RX %s", data.hex(" "))
        for item in self._decoder.feed(data):
            if isinstance(item, int):
                if self._reply is not None and not self._reply.done():
                    self._reply.set_result(item)
                continue
            try:
                self._write(bytes((p.ACK if item.body is not None else p.NAK,)))
            except SendError as e:
                log.warning("Cannot reply ACK/NAK: %s", e)
            if item.body is None:
                log.warning("Received corrupt frame, NAK sent")
            elif self.on_message:
                try:
                    self.on_message(p.parse_body(item.body))
                except Exception:  # noqa: BLE001
                    log.exception("on_message handler failed")

    async def _writer(self) -> None:
        while True:
            body, fut = await self._queue.get()
            if fut.done():
                continue
            try:
                await self._send_with_retry(body)
            except SendError as e:
                if not fut.done():
                    fut.set_exception(e)
            else:
                if not fut.done():
                    fut.set_result(None)

    async def _send_with_retry(self, body: bytes) -> None:
        data = p.frame(body)
        last = "no reply"
        for attempt in range(1, self.retries + 1):
            self._reply = self._loop.create_future()
            log.debug("TX %s (attempt %d)", data.hex(" "), attempt)
            self._write(data)
            try:
                reply = await asyncio.wait_for(self._reply, self.ack_timeout)
            except asyncio.TimeoutError:
                last = "no ACK (timeout)"
                continue
            finally:
                self._reply = None
            if reply == p.ACK:
                return
            last = "NAK"
        raise SendError(f"message not acknowledged after {self.retries} attempts: {last}")
