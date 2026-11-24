from sar.common.grid import Grid
from sar.drone.drone import lawnmower
from sar.groundstation.planner import GridSectorPlanner
from sar.groundstation.pool import SectorPool

TABLE = {"a": {"poa": 1.0}, "b": {"poa": 0.2}}


def make(width=25, height=15, n=10):
    grid = Grid(width, height, 20.0)
    terrain = [["a" if i < 10 else "b" for i in range(width)] for _ in range(height)]
    sectors = GridSectorPlanner(n).plan(grid, terrain, TABLE, (0.0, 0.0))
    return grid, sectors


def test_sectors_cover_every_cell_once():
    grid, sectors = make()
    cells = [c for s in sectors for c in s.cells()]
    assert len(cells) == grid.cells
    assert len(set(cells)) == grid.cells


def test_pool_gives_highest_priority_first_and_is_idempotent():
    grid, sectors = make()
    pool = SectorPool(sectors, grid)
    first = pool.request("d1", 0, 0)
    assert first.priority == max(s.priority for s in sectors)
    assert pool.request("d1", 0, 0).id == first.id  # retry returns the same sector
    second = pool.request("d2", 0, 0)
    assert second.id != first.id


def test_pool_ignores_stale_completion():
    grid, sectors = make()
    pool = SectorPool(sectors, grid)
    s = pool.request("d1", 0, 0)
    assert not pool.complete("d2", s.id)
    assert pool.complete("d1", s.id)
    assert not pool.complete("d1", s.id)  # duplicate report
    assert pool.stats()["done"] == 1


def test_pool_runs_out():
    grid, sectors = make()
    pool = SectorPool(sectors, grid)
    for _ in sectors:
        s = pool.request("d1", 0, 0)
        pool.complete("d1", s.id)
    assert pool.request("d1", 0, 0) is None


def test_second_pass_comes_after_the_first():
    grid = Grid(20, 20, 20.0)
    terrain = [["a"] * 20 for _ in range(20)]
    sectors = GridSectorPlanner(10, passes=2).plan(grid, terrain, TABLE, (0.0, 0.0))
    assert len(sectors) == 8
    pool = SectorPool(sectors, grid)
    first_four = []
    for _ in range(4):
        s = pool.request("d1", 0, 0)
        first_four.append(s.id)
        pool.complete("d1", s.id)
    assert sorted(first_four) == [0, 1, 2, 3]


def test_lawnmower_visits_each_cell_once_adjacent_steps():
    path = lawnmower(2, 3, 6, 6)
    assert len(path) == len(set(path)) == 12
    for (a, b) in zip(path, path[1:]):
        assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
