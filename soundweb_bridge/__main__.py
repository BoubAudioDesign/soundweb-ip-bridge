from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import signal
import sys

from .config import load_config
from .core import Bridge
from .http_server import start_http_server
from .tcp_server import start_tcp_server


async def run(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    if args.simulate:
        if cfg.protocol == "designer":
            from .simulator import start_designer_simulator

            cfg.serial_port, _ = start_designer_simulator()
        else:
            from .simulator import start_simulator  # pty on Linux, local TCP on Windows

            cfg.serial_port, _ = start_simulator()
        logging.getLogger(__name__).warning("SIMULATION: using simulated Soundweb on %s", cfg.serial_port)
    bridge = Bridge(cfg)
    await bridge.start()
    tcp = await start_tcp_server(bridge, cfg.tcp_host, cfg.tcp_port)
    http = await start_http_server(bridge, cfg.http_host, cfg.http_port, cfg.token)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    try:
        await stop.wait()
    finally:
        tcp.close()
        await http.cleanup()
        await bridge.stop()


def list_ports() -> None:
    from serial.tools import list_ports as lp

    ports = sorted(lp.comports(), key=lambda p: p.device)
    if not ports:
        print("No serial port found.")
    for p in ports:
        print(f"{p.device:<16} {p.description}  [{p.hwid}]")


def main() -> None:
    ap = argparse.ArgumentParser(prog="soundweb-bridge", description="BSS Soundweb 9088 RS-232 <-> IP bridge")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every serial frame (hex)")
    ap.add_argument("--simulate", action="store_true", help="use a built-in simulated Soundweb (no hardware)")
    ap.add_argument("--log-file", help="also write logs to this file (rotated, 5 x 1 MB); useful when run as a service")
    ap.add_argument("--list-ports", action="store_true", help="list the serial ports of this machine (COMx, /dev/tty...) and exit")
    ap.add_argument(
        "--probe", nargs="?", const="", metavar="PORT",
        help="serial diagnostic: test speeds/flow control with a harmless corrupted frame (port from config if omitted)",
    )
    ap.add_argument("--learn-presets", action="store_true",
                    help="Designer protocol: recall each preset once to learn its signature (active preset at start-up)")
    args = ap.parse_args()
    if args.learn_presets:
        from .learn import learn_presets

        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        asyncio.run(learn_presets(load_config(args.config)))
        return
    if args.list_ports:
        list_ports()
        return
    if args.probe is not None:
        from .probe import probe

        probe(args.probe or load_config(args.config).serial_port)
        return
    handlers: list = [logging.StreamHandler(sys.stderr)]
    if args.log_file:
        handlers.append(
            logging.handlers.RotatingFileHandler(args.log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        )
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
