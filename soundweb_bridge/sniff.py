"""Serial capture proxy: Soundweb Designer <-> (this tool) <-> Soundweb 9088.

Used to observe the (undocumented) protocol that Soundweb Designer speaks on the rear port, so the bridge can
reproduce it. Designer connects to one end of a virtual COM pair (com0com), this tool relays every byte
between the other end of the pair and the real port, and logs everything with timestamps.

    python -m soundweb_bridge.sniff --pc COM11 --device COM4 [--baud 115200] [--rtscts] [-o capture.log]

Type a note + Enter while capturing (e.g. "source salle 2") to mark what you just did in Designer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading
import time
from typing import IO

import serial

from . import protocol as p


class Capture:
    def __init__(self, out: IO[str]) -> None:
        self.out = out
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.decoders = {"PC>SW": p.Decoder(), "SW>PC": p.Decoder()}
        self.bytes = {"PC>SW": 0, "SW>PC": 0}

    def line(self, text: str) -> None:
        with self.lock:
            stamp = f"{time.monotonic() - self.t0:10.3f}"
            for row in text.splitlines() or [""]:
                msg = f"{stamp} {row}"
                print(msg, file=self.out, flush=True)
                print(msg, flush=True)

    def data(self, direction: str, data: bytes) -> None:
        self.bytes[direction] += len(data)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
        self.line(f"{direction} [{len(data):4}] {data.hex(' ')}   |{ascii_}|")
        # Try the third-party framing (STX ... checksum ETX): tells us if Designer uses the same format.
        for item in self.decoders[direction].feed(data):
            if isinstance(item, int):
                name = {p.ACK: "ACK", p.NAK: "NAK"}.get(item, hex(item))
                self.line(f"      {direction} control byte {name}")
            elif item.body is None:
                self.line(f"      {direction} STX/ETX frame, bad checksum")
            else:
                self.line(f"      {direction} STX/ETX frame OK: {p.parse_body(item.body)}")


def relay(src: serial.Serial, dst: serial.Serial, direction: str, cap: Capture, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            # forward every byte at once, but log a burst as one line (ends after ~10 ms of silence)
            burst = bytearray()
            while not stop.is_set():
                data = src.read(src.in_waiting or 1)
                if not data:
                    break
                try:
                    dst.write(data)
                except serial.SerialTimeoutException:  # other side not reading (e.g. Designer closed its port)
                    cap.line(f"!! {direction} write timeout, {len(data)} bytes dropped")
                burst += data
            if burst:
                cap.data(direction, bytes(burst))
        except (serial.SerialException, OSError) as e:
            cap.line(f"!! {direction} error: {e}")
            stop.set()


def main(argv: "list[str] | None" = None) -> None:
    ap = argparse.ArgumentParser(description="Capture Soundweb Designer <-> Soundweb serial traffic")
    ap.add_argument("--pc", required=True, help="virtual COM port Designer is NOT using (other end of the com0com pair)")
    ap.add_argument("--device", required=True, help="real COM port wired to the Soundweb rear port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--rtscts", action="store_true", help="hardware flow control on the real port")
    ap.add_argument("-o", "--output", default=f"capture-{dt.datetime.now():%Y%m%d-%H%M%S}.log")
    args = ap.parse_args(argv)

    dev = serial.serial_for_url(args.device, baudrate=args.baud, rtscts=args.rtscts, timeout=0.01, write_timeout=2)
    pc = serial.serial_for_url(args.pc, baudrate=args.baud, rtscts=args.rtscts, timeout=0.01, write_timeout=2)
    with open(args.output, "w", encoding="utf-8") as out:
        cap = Capture(out)
        cap.line(f"# capture {dt.datetime.now():%Y-%m-%d %H:%M:%S}  pc={args.pc} device={args.device} "
                 f"{args.baud} 8N1 rtscts={args.rtscts}")
        stop = threading.Event()
        for a, b, d in ((pc, dev, "PC>SW"), (dev, pc, "SW>PC")):
            threading.Thread(target=relay, args=(a, b, d, cap, stop), daemon=True).start()
        print(f"Capture -> {args.output}. Connect Soundweb Designer to the other end of the virtual pair.")
        print("Type a note + Enter to mark an action (e.g. 'source salle 2'), Ctrl+C to stop.")
        try:
            while not stop.is_set():
                note = sys.stdin.readline()
                if not note:
                    stop.wait()
                    break
                cap.line(f"### NOTE: {note.strip()}")
        except KeyboardInterrupt:
            pass
        stop.set()
        cap.line(f"# end - PC>SW {cap.bytes['PC>SW']} bytes, SW>PC {cap.bytes['SW>PC']} bytes")
    pc.close()
    dev.close()


if __name__ == "__main__":
    main()
