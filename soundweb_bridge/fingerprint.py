"""Recognise the active preset from the parameter values downloaded at connection (Designer protocol).

The 9088 does not store an "active preset" number, but each preset leaves a signature: the values of the
parameters it sets. Signatures are learned once (``--learn-presets``: recall each preset, read the config) and
saved next to config.yaml; at start-up the current config is compared to them.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Iterable, Optional, Set, Tuple

log = logging.getLogger(__name__)

Key = Tuple[int, int]  # (handle, method)
Values = Dict[Key, int]


def build(configs: Dict[int, Values], exclude: Iterable[Key] = ()) -> Dict[int, Values]:
    """Keep, for each preset, only the parameters whose value differs between presets."""
    skip: Set[Key] = set(exclude)
    keys = set().union(*configs.values()) - skip if configs else set()
    varying = {k for k in keys if len({cfg.get(k) for cfg in configs.values()}) > 1}
    return {pid: {k: cfg[k] for k in sorted(varying) if k in cfg} for pid, cfg in configs.items()}


def match(fingerprints: Dict[int, Values], values: Values) -> Optional[int]:
    """Preset whose signature matches ``values`` exactly (None if no or several presets match)."""
    hits = [pid for pid, fp in fingerprints.items() if fp and all(values.get(k) == v for k, v in fp.items())]
    return hits[0] if len(hits) == 1 else None


def save(path: str, fingerprints: Dict[int, Values]) -> None:
    data = {str(pid): {f"{h:#x}:{m:#010x}": v for (h, m), v in fp.items()} for pid, fp in fingerprints.items()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "presets": data}, f, indent=1)
    os.replace(tmp, path)


def load(path: str) -> Dict[int, Values]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Cannot read preset signatures %s: %s", path, e)
        return {}
    out: Dict[int, Values] = {}
    for pid, fp in (data.get("presets") or {}).items():
        out[int(pid)] = {(int(k.split(":")[0], 16), int(k.split(":")[1], 16)): int(v) for k, v in fp.items()}
    return out
