"""Stable, process-local RNG helpers.

Python's `hash()` is seeded per process, and `random.seed()` mutates the
global RNG used by fuel ticks, events, and the AI. Use these instead.
"""

from __future__ import annotations

import random
import zlib


def stable_hash(value: str) -> int:
    """Deterministic 32-bit hash (crc32), stable across process restarts."""
    return zlib.crc32(str(value).encode("utf-8")) & 0xFFFFFFFF


def seeded_rng(*parts) -> random.Random:
    """Local Random whose seed is built from ints and stable_hash of strings."""
    seed = 0
    for part in parts:
        if isinstance(part, bool):
            n = int(part)
        elif isinstance(part, int):
            n = part
        else:
            n = stable_hash(str(part))
        seed = (seed * 1_000_003 + (n % 1_000_000_007)) % (2**32)
    return random.Random(seed)
