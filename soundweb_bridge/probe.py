"""Serial diagnostic: find the settings at which the Soundweb answers the third-party protocol.

Sends a frame with a deliberately WRONG checksum: the Soundweb executes nothing and, if its port is in
third-party ("+s") mode at the right speed, replies NAK (0x15). Harmless for the running design.
"""
from __future__ import annotations

import time
from typing import List, Optional

import serial

from . import protocol as p

BAUDRATES = [38400, 115200, 57600, 19200, 9600]


def _bad_frame() -> bytes:
    body = p.build_source_select(0x00000001, 1)
    bad = p.checksum(body) ^ 0x5A  # wrong on purpose
    return bytes((p.STX,)) + b"".join(p._escape(b) for b in body) + p._escape(bad) + bytes((p.ETX,))


def _describe(data: bytes) -> str:
    if not data:
        return "aucune réponse"
    if p.NAK in data:
        return f"NAK reçu -> protocole Soundweb OK à ces réglages  [{data.hex(' ')}]"
    if p.ACK in data:
        return f"ACK reçu (inattendu pour une trame corrompue) -> le Soundweb répond  [{data.hex(' ')}]"
    return f"octets reçus mais pas de NAK (autre protocole / mauvaise vitesse ?)  [{data.hex(' ')}]"


def probe(port: str, baudrates: Optional[List[int]] = None, wait: float = 1.0) -> bool:
    frame = _bad_frame()
    print(f"Port {port} - trame de test (checksum volontairement faux) : {frame.hex(' ')}")
    found = False
    for baud in baudrates or BAUDRATES:
        for rtscts in (False, True):
            label = f"{baud:>6} bauds 8N1, rtscts={'oui' if rtscts else 'non'}"
            try:
                with serial.serial_for_url(port, baudrate=baud, rtscts=rtscts, timeout=0.1, write_timeout=1) as ser:
                    time.sleep(0.2)
                    ser.reset_input_buffer()
                    try:
                        ser.write(frame)
                        ser.flush()
                    except serial.SerialTimeoutException:
                        print(f"  {label} : écriture bloquée (CTS jamais activé : câble sans RTS/CTS)")
                        continue
                    data = b""
                    end = time.monotonic() + wait
                    while time.monotonic() < end:
                        data += ser.read(64)
                    modem = ""
                    try:
                        modem = f"  (CTS={int(ser.cts)} DSR={int(ser.dsr)})"
                    except (OSError, serial.SerialException, NotImplementedError):
                        pass
                    print(f"  {label} : {_describe(data)}{modem}")
                    found = found or p.NAK in data or p.ACK in data
            except (serial.SerialException, OSError, ValueError) as e:
                print(f"  {label} : impossible d'ouvrir le port ({e})")
                return False
    print()
    if found:
        print("=> Mets dans config.yaml la vitesse / rtscts de la ligne 'NAK reçu'.")
    else:
        print("=> Aucune réponse au protocole tiers. Causes probables :")
        print("   - le device n'est PAS en mode '+s' (nom du device se terminant par '+s', design chargé, 9088 redémarré) ;")
        print("     tant que Soundweb Designer peut se connecter sur ce port, il est en mode Designer et ignore ces trames ;")
        print("   - câble branché sur le mauvais port, ou TX/RX non croisés ;")
        print("   - Soundweb Designer (ou un autre logiciel) encore connecté au port.")
    return found
