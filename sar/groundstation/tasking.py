"""Which job a drone should do next (leader only; the result goes into the log).

Value of searching a cell = POA x POD: the chance a victim is there times the
chance this pass would see it (search theory, Koopman). After every pass the
station lowers the cell's POA to POA x (1 - POD), so a searched cell is worth
less, but rubble (high POA, low POD) stays worth more than an unsearched open
field for a while. That is what replaces the fixed "second pass" of step 1:
re-search happens where it pays.

Score of a job = value of its remaining cells / time to do it (fly there + search).
A job is only offered if the drone can fly there, search it and get home with
its reserve left (`fits`). Strategies (config `planner.strategy`):
  - greedy:      best score, re-search jobs compete with first-pass jobs
  - sweep_first: first-pass jobs first (by score), re-search after everything is covered once
  - static:      steps 1-4 behaviour: planner priority then distance, no new jobs

Near a confirmed victim, cells count (1 + boost) times: people are often found
together (a collapsed building, a vehicle).
"""
from __future__ import annotations

import math

from ..common.grid import Grid
from .planner import Sector, distance
from .pool import INSPECT_BASE, SectorPool


class Tasker:
    def __init__(self, grid: Grid, terrain, table: dict, base: tuple[float, float], scenario: dict):
        self.grid, self.terrain, self.table, self.base = grid, terrain, table, base
        p = scenario["planner"]
        d = scenario["drones"]
        default = "static" if p.get("kind", "grid") == "grid" else "greedy"
        self.strategy = p.get("strategy") or default
        self.cruise = float(d["cruise_mps"])
        self.search = float(d["search_mps"])
        self.reserve = float(d["battery"]["reserve_s"]) if d["battery"]["enabled"] else 0.0
        self.min_search = float(p.get("min_search_s", 180.0))  # worth flying out for at least this much searching
        r = p.get("research", {})
        self.max_passes = int(r.get("max_passes", 4))
        self.stop_frac = float(r.get("stop_frac", 0.1))
        b = p.get("boost", {})
        self.boost = float(b.get("factor", 0.5))
        self.boost_r = float(b.get("radius_m", 80.0))
        self.leftover_boost = float(p.get("leftover_boost", 0.0))
        i = p.get("inspect", {})
        self.inspect_value = float(i.get("value", 5.0))
        self.inspect_hover = float(i.get("hover_s", 20.0))
        self.pod = {(x, y): float(table[terrain[y][x]]["pod"]) for y in range(grid.height) for x in range(grid.width)}
        # Stop threshold for re-search: a fraction of the best tile's value before any search.
        self.poa0 = {c: float(table[terrain[c[1]][c[0]]]["poa"]) for c in self.pod}
        self.initial_best: float | None = None
        self.zone_w: dict[tuple[int, int], float] = {}  # commander's priority zones

    # -- time and value -----------------------------------------------------------------

    def search_s(self, job: Sector) -> float:
        if job.kind == "inspect":
            return self.inspect_hover
        return len(job.cells()) * self.grid.cell_m / self.search

    def sortie_s(self, job: Sector, x: float, y: float) -> tuple[float, float]:
        """(time to finish the job from here, time to then get home)."""
        cells = job.cells() or job.path()[-1:]
        first, last = self.grid.center(*cells[0]), self.grid.center(*cells[-1])
        work = distance((x, y), first) / self.cruise + self.search_s(job)
        return work, distance(last, self.base) / self.cruise

    def fits(self, job: Sector, x: float, y: float, battery_s: float | None) -> bool:
        """Can the drone do a useful part of this job and still get home with its reserve?
        It need not finish: it searches until its battery says go home ("bingo") and
        gives the rest back as a leftover job. Inspect jobs must be done whole."""
        if battery_s is None:
            return True
        cells = job.cells() or job.path()[-1:]
        first = self.grid.center(*cells[0])
        far_home = max(distance(self.grid.center(*c), self.base) for c in (cells[0], cells[-1])) / self.cruise
        avail = battery_s - self.reserve - distance((x, y), first) / self.cruise - far_home
        need = self.search_s(job) if job.kind == "inspect" else min(self.search_s(job), self.min_search)
        return avail >= need

    def boosted(self, victims: list[tuple[float, float]]) -> set[tuple[int, int]]:
        cells = set()
        r = int(math.ceil(self.boost_r / self.grid.cell_m))
        for vx, vy in victims:
            c = self.grid.cell_of(vx, vy)
            if c is None:
                continue
            for j in range(c[1] - r, c[1] + r + 1):
                for i in range(c[0] - r, c[0] + r + 1):
                    if 0 <= i < self.grid.width and 0 <= j < self.grid.height and \
                            distance(self.grid.center(i, j), (vx, vy)) <= self.boost_r:
                        cells.add((i, j))
        return cells

    def value(self, job: Sector, poa: dict, boosted: set) -> float:
        if job.kind == "inspect":
            return self.inspect_value
        # Work a drone had to give back (battery) goes first: it closes the gap the
        # leaving drone opened instead of leaving a half-searched area for later.
        lb = 1 + self.leftover_boost if job.start > 0 else 1
        zw = self.zone_w
        return lb * sum(poa.get(c, 0.0) * self.pod[c] * (1 + self.boost if c in boosted else 1) * zw.get(c, 1.0)
                        for c in job.cells())

    def set_zones(self, weight_at) -> None:
        """weight_at(x, y) -> multiplier; recomputed when the commander changes zones."""
        self.zone_w = {}
        for c in self.pod:
            w = weight_at(*self.grid.center(*c))
            if w != 1.0:
                self.zone_w[c] = w

    # -- choice ---------------------------------------------------------------------------

    def research_candidates(self, pool: SectorPool, poa: dict, boosted: set) -> list[Sector]:
        """One new job per tile that is not queued or out, while it is still worth it."""
        out = []
        for t, tile in pool.tiles.items():
            if pool.tile_busy(t):
                continue
            p = pool.passes_done(t)
            if p == 0 or p >= self.max_passes:
                continue
            s = Sector(pool.research_id(t, p), tile.i0, tile.j0, tile.i1, tile.j1, tile.priority, tile=t, pass_=p)
            if s.id in pool.sectors:
                continue
            if self.value(s, poa, boosted) >= self.stop_frac * (self.initial_best or 0):
                out.append(s)
        return out

    def choose(self, pool: SectorPool, drone: str, x: float, y: float, battery_s: float | None,
               poa: dict, victims: list, exclude=frozenset()) -> tuple[Sector | None, bool, str]:
        """Returns (job, is_new, reason). reason when no job: "rtb" (work exists but
        does not fit this battery: go home), "wait" (all work is out with other
        drones) or "done" (nothing left worth searching)."""
        boosted = self.boosted(victims) if self.boost and victims else set()
        if self.initial_best is None:
            vals = [self.value(t, self.poa0, set()) for t in pool.tiles.values()]
            self.initial_best = max(vals) if vals else 0.0
        waiting = [pool.sectors[j] for j in sorted(pool.waiting - set(exclude))]
        if self.strategy == "static":
            cands = [(s, False) for s in waiting if s.kind == "search"]
            fit = [(s, n) for s, n in cands if self.fits(s, x, y, battery_s)]
            if fit:
                s = min(fit, key=lambda c: (-c[0].priority, distance((x, y), c[0].center(self.grid)), c[0].id))[0]
                return s, False, ""
            return None, False, "rtb" if cands else ("wait" if pool.assigned else "done")
        cands = [(s, False) for s in waiting]
        first_pass_left = any(s.pass_ == 0 for s in waiting if s.kind == "search") or any(
            pool.sectors[j].pass_ == 0 and pool.sectors[j].kind == "search" for j in pool.assigned)
        if self.strategy == "greedy" or not first_pass_left:
            cands += [(s, True) for s in self.research_candidates(pool, poa, boosted) if s.id not in exclude]
        if not cands:
            return None, False, "wait" if pool.assigned else "done"
        fit = [(s, n) for s, n in cands if self.fits(s, x, y, battery_s)]
        if not fit:
            return None, False, "rtb"

        def score(c):
            s = c[0]
            work, _ = self.sortie_s(s, x, y)
            return self.value(s, poa, boosted) / max(work, 1.0)

        if self.strategy == "sweep_first" and first_pass_left:
            firsts = [c for c in fit if c[0].pass_ == 0 or c[0].kind == "inspect"]
            fit = firsts or fit
        best = max(fit, key=lambda c: (score(c), -c[0].id))
        return best[0], best[1], ""

    def inspect_job(self, pool: SectorPool, cluster: str, x: float, y: float) -> Sector | None:
        c = self.grid.cell_of(x, y)
        if c is None or cluster in pool.inspected:
            return None
        return Sector(INSPECT_BASE + pool.n_inspect, c[0], c[1], c[0] + 1, c[1] + 1, self.inspect_value,
                      tile=-2, kind="inspect", cluster=cluster)
