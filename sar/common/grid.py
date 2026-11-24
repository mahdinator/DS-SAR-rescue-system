"""Grid geometry shared by the world, drones and ground stations.

Cell (i, j): i is the column (x axis), j is the row (y axis).
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Grid:
    width: int
    height: int
    cell_m: float

    def cell_of(self, x: float, y: float) -> tuple[int, int] | None:
        i = int(math.floor(x / self.cell_m))
        j = int(math.floor(y / self.cell_m))
        if 0 <= i < self.width and 0 <= j < self.height:
            return (i, j)
        return None

    def center(self, i: int, j: int) -> tuple[float, float]:
        return ((i + 0.5) * self.cell_m, (j + 0.5) * self.cell_m)

    @property
    def cells(self) -> int:
        return self.width * self.height


def stable_int(*parts) -> int:
    """Deterministic 64-bit integer from the parts (Python's hash() is salted per process)."""
    text = "|".join(str(p) for p in parts)
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")
