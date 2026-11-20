"""``--learn-presets``: learn the signature of each preset (Designer protocol).

Recalls every preset of config.yaml in turn, reads the config downloaded at the next connection, keeps the
parameters that differ between presets, saves them next to config.yaml, then recalls the preset that was
active before (if it could be recognised). Must be run again after the design is changed.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

from . import fingerprint
from . import protocol as p
from .config import Config
from .designer_link import DesignerLink


async def _session(cfg: Config, recall: Optional[int]) -> fingerprint.Values:
    """Open a session, return the parameter values read at connection, then optionally recall a preset."""
    link = DesignerLink(cfg.serial_port, baudrate=cfg.baudrate, rtscts=cfg.rtscts, reconnect_delay=1.0)
    await link.start()
    try:
        for _ in range(150):
            if link.connected:
                break
            await asyncio.sleep(0.1)
        if not link.connected:
            raise SystemExit(f"Impossible d'ouvrir une session sur {cfg.serial_port} (Designer fermé ? câble ?)")
        values = dict(link.parameter_values)
        if recall is not None:
            await link.send(p.build_preset(recall))
            print(f"  {time.strftime('%H:%M:%S')}  preset {recall} rappelé")
            await asyncio.sleep(1.5)  # let the 9088 apply it before the next read
        return values
    finally:
        await link.stop()
        await asyncio.sleep(0.5)


async def learn_presets(cfg: Config) -> None:
    if cfg.protocol != "designer":
        raise SystemExit("--learn-presets ne concerne que le protocole designer (serial.protocol: designer)")
    names = {pid: name for name, pid in cfg.presets.items()}
    ids = sorted(names)
    if len(ids) < 2:
        raise SystemExit("Il faut au moins 2 presets dans la section 'presets' de config.yaml")
    print(f"Apprentissage des presets {', '.join(f'{i} ({names[i]})' for i in ids)} sur {cfg.serial_port}")
    print("Chaque preset va être rappelé (le son peut changer), puis le preset actif sera restauré.")

    configs: Dict[int, fingerprint.Values] = {}
    initial = await _session(cfg, ids[0])
    for prev, nxt in zip(ids, ids[1:] + [None]):
        configs[prev] = await _session(cfg, nxt)

    exclude = [(v.handle, v.parameter) for v in cfg.volumes.values()]
    signatures = fingerprint.build(configs, exclude)
    same = [(a, b) for a in ids for b in ids if a < b and signatures[a] == signatures[b]]
    for a, b in same:
        print(f"  ATTENTION : presets {a} et {b} donnent le même état, ils ne pourront pas être distingués")
    fingerprint.save(cfg.signatures_path, signatures)
    n = len(next(iter(signatures.values()), {}))
    print(f"Signatures enregistrées ({n} paramètres par preset) : {cfg.signatures_path}")

    before = fingerprint.match(signatures, initial)
    last = ids[-1]
    if before is None:
        print(f"Preset actif avant l'apprentissage non reconnu : le preset {last} ({names[last]}) reste actif.")
    elif before != last:
        await _session(cfg, before)
        print(f"Preset actif avant l'apprentissage restauré : {before} ({names[before]}).")
    else:
        print(f"Preset actif avant l'apprentissage : {before} ({names[before]}), déjà actif.")
