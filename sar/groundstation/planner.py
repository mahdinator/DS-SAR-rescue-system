"""Sector planning: cutting the area into jobs a drone can do on one battery.

Two planners (config `planner.kind`):
  - grid:   fixed square tiles (steps 1-4). `passes` > 1 queues every tile again
            at half priority per extra pass.
  - sortie: tiles sized to one sortie. Start from the whole area and halve the
            largest piece until each piece can be flown from base, searched and
            flown back on one battery with the reserve left over (and is no larger
            than `max_cells`). Pieces near base can be larger than far ones.

A plan is the list of first-pass jobs ("tiles"). Re-search jobs, leftover jobs
and inspect jobs are created later from these (see pool.py and tasking.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..common.grid import Grid


def lawnmower(i0: int, j0: int, i1: int, j1: int) -> list[tuple[int, int]]:
    """Cells of a rectangle, row by row, alternating direction."""
    path = []
    for row, j in enumerate(range(j0, j1)):
        cols = range(i0, i1) if row % 2 == 0 else range(i1 - 1, i0 - 1, -1)
        path.extend((i, j) for i in cols)
    return path


@dataclass
class Sector:
    """A job: a rectangle searched in lawnmower order from cell index `start`.

    kind "search" covers the rectangle; kind "inspect" is a closer look at one
    cell (for a doubtful sighting). `tile` is the plan tile it came from and
    `pass_` how many times that tile had been searched before."""

    id: int
    i0: int
    j0: int
    i1: int  # exclusive
    j1: int  # exclusive
    priority: float
    tile: int = -1
    pass_: int = 0
    start: int = 0
    kind: str = "search"
    cluster: str | None = None

    def __post_init__(self):
        if self.tile < 0:
            self.tile = self.id

    def path(self) -> list[tuple[int, int]]:
        """All cells in flying order (the full rectangle)."""
        return lawnmower(self.i0, self.j0, self.i1, self.j1)

    def cells(self) -> list[tuple[int, int]]:
        """Cells still to search (from `start`)."""
        return self.path()[self.start:]

    def center(self, grid: Grid) -> tuple[float, float]:
        return ((self.i0 + self.i1) / 2 * grid.cell_m, (self.j0 + self.j1) / 2 * grid.cell_m)

    def to_dict(self) -> dict:
        return {"id": self.id, "i0": self.i0, "j0": self.j0, "i1": self.i1, "j1": self.j1,
                "priority": self.priority, "tile": self.tile, "pass_": self.pass_, "start": self.start,
                "kind": self.kind, "cluster": self.cluster}


def _mean_poa(table: dict, terrain, i0, j0, i1, j1) -> float:
    cells = [(i, j) for j in range(j0, j1) for i in range(i0, i1)]
    return sum(table[terrain[j][i]]["poa"] for i, j in cells) / len(cells)


class GridSectorPlanner:
    """`passes` > 1 queues every sector again after the first sweep, at half the
    priority per extra pass (the step-1 stand-in for re-search)."""

    def __init__(self, sector_cells: int, passes: int = 1):
        self.n = sector_cells
        self.passes = passes

    def plan(self, grid: Grid, terrain: list[list[str]], table: dict, base: tuple[float, float]) -> list[Sector]:
        tiles = []
        for j0 in range(0, grid.height, self.n):
            for i0 in range(0, grid.width, self.n):
                i1, j1 = min(i0 + self.n, grid.width), min(j0 + self.n, grid.height)
                tiles.append((i0, j0, i1, j1, _mean_poa(table, terrain, i0, j0, i1, j1)))
        sectors = []
        for p in range(self.passes):
            for t, (i0, j0, i1, j1, poa) in enumerate(tiles):
                sectors.append(Sector(len(sectors), i0, j0, i1, j1, round(poa * 0.5 ** p, 4), tile=t, pass_=p))
        return sectors


class SortiePlanner:
    """Halve pieces until each fits one sortie (architecture doc, section 11)."""

    def __init__(self, budget_s: float, cruise_mps: float, search_mps: float, max_cells: int):
        self.budget = budget_s
        self.cruise = cruise_mps
        self.search = search_mps
        self.max_cells = max_cells

    def sortie_s(self, grid: Grid, base, i0, j0, i1, j1) -> float:
        """Fly out to the farthest corner, search every cell, fly back (conservative)."""
        far = max(math.hypot(i * grid.cell_m - base[0], j * grid.cell_m - base[1])
                  for i in (i0, i1) for j in (j0, j1))
        return 2 * far / self.cruise + (i1 - i0) * (j1 - j0) * grid.cell_m / self.search

    def plan(self, grid: Grid, terrain: list[list[str]], table: dict, base: tuple[float, float]) -> list[Sector]:
        todo = [(0, 0, grid.width, grid.height)]
        pieces = []
        while todo:
            i0, j0, i1, j1 = todo.pop()
            w, h = i1 - i0, j1 - j0
            fits = self.sortie_s(grid, base, i0, j0, i1, j1) <= self.budget and w * h <= self.max_cells
            if fits or (w == 1 and h == 1):
                pieces.append((i0, j0, i1, j1))
            elif w >= h:
                m = i0 + w // 2
                todo += [(m, j0, i1, j1), (i0, j0, m, j1)]
            else:
                m = j0 + h // 2
                todo += [(i0, m, i1, j1), (i0, j0, i1, m)]
        pieces.sort(key=lambda p: (p[1], p[0]))  # stable ids: row-major by corner
        return [Sector(k, *p, round(_mean_poa(table, terrain, *p), 4)) for k, p in enumerate(pieces)]


def sortie_budget(scenario: dict) -> float:
    """Mission seconds one battery gives for a sortie, minus the reserve."""
    b = scenario["drones"]["battery"]
    return float(b["endurance_s"]) - float(b["reserve_s"])


def make_planner(cfg: dict, scenario: dict | None = None):
    kind = cfg.get("kind", "grid")
    if kind == "grid":
        return GridSectorPlanner(int(cfg["sector_cells"]), int(cfg.get("passes", 1)))
    if kind == "sortie":
        d = scenario["drones"]
        return SortiePlanner(sortie_budget(scenario) * float(cfg.get("fill", 0.9)), float(d["cruise_mps"]),
                             float(d["search_mps"]), int(cfg.get("max_cells", 200)))
    raise ValueError(f"unknown planner {kind!r}")


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
