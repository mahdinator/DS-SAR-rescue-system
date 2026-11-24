"""Seeded terrain and victim generation. The same seed always gives the same world."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ..common.grid import Grid


def generate_terrain(grid: Grid, seed: int, regions: int, mix: dict[str, float]) -> list[list[str]]:
    """Voronoi regions: each cell takes the terrain of its nearest region seed.

    Returns rows: terrain[j][i].
    """
    rng = random.Random(seed)
    names = list(mix.keys())
    weights = [mix[n] for n in names]
    seeds = [
        (rng.uniform(0, grid.width), rng.uniform(0, grid.height), rng.choices(names, weights)[0])
        for _ in range(regions)
    ]
    rows = []
    for j in range(grid.height):
        row = []
        for i in range(grid.width):
            cx, cy = i + 0.5, j + 0.5
            nearest = min(seeds, key=lambda s: (s[0] - cx) ** 2 + (s[1] - cy) ** 2)
            row.append(nearest[2])
        rows.append(row)
    return rows


@dataclass
class Victim:
    id: str
    x: float
    y: float
    i: int
    j: int
    terrain: str
    signature: list[float]


def place_victims(
    grid: Grid, terrain: list[list[str]], table: dict, seed: int, count: int, dims: int,
    prefix: str = "v", weighted: bool = True,
) -> list[Victim]:
    """Victims land in cells with probability proportional to the terrain's POA weight.
    Several victims may share a cell (victims cluster in real disasters).

    The same function places decoys (prefix "d", spread uniformly): fixed objects
    such as debris or clothing that the detector mistakes for a person on every pass.
    """
    rng = random.Random(seed + (1 if prefix == "v" else 2))
    cells = [(i, j) for j in range(grid.height) for i in range(grid.width)]
    weights = [table[terrain[j][i]]["poa"] if weighted else 1.0 for (i, j) in cells]
    chosen = rng.choices(cells, weights, k=count) if count else []
    victims = []
    for n, (i, j) in enumerate(chosen):
        x = (i + rng.random()) * grid.cell_m
        y = (j + rng.random()) * grid.cell_m
        sig = [rng.gauss(0, 1) for _ in range(dims)]
        norm = math.sqrt(sum(v * v for v in sig)) or 1.0
        victims.append(Victim(f"{prefix}{n}", x, y, i, j, terrain[j][i], [v / norm for v in sig]))
    return victims
