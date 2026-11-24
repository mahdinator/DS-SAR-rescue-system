"""Step 5: sortie-sized planning, leases with fencing tokens, POA x POD tasking."""
import copy

from sar.common.config import DEFAULTS
from sar.common.grid import Grid
from sar.groundstation.planner import SortiePlanner
from sar.groundstation.pool import SectorPool
from sar.groundstation.tasking import Tasker

TABLE = {"rubble": {"poa": 1.0, "pod": 0.4}, "field": {"poa": 0.3, "pod": 0.9}}


def world(width=20, height=10, split=10):
    grid = Grid(width, height, 20.0)
    terrain = [["rubble" if i < split else "field" for i in range(width)] for _ in range(height)]
    return grid, terrain


def scenario(**planner):
    sc = copy.deepcopy(DEFAULTS)
    sc["planner"].update(planner)
    return sc


def test_sortie_pieces_cover_every_cell_once_and_fit_one_battery():
    grid = Grid(50, 50, 20.0)
    terrain = [["field"] * 50 for _ in range(50)]
    base = (500.0, 10.0)
    planner = SortiePlanner(budget_s=900, cruise_mps=12, search_mps=6, max_cells=10_000)
    pieces = planner.plan(grid, terrain, TABLE, base)
    cells = [c for p in pieces for c in p.cells()]
    assert len(cells) == len(set(cells)) == grid.cells
    assert all(planner.sortie_s(grid, base, p.i0, p.j0, p.i1, p.j1) <= 900 for p in pieces)


def test_pieces_near_base_are_larger_than_far_ones():
    grid = Grid(50, 200, 20.0)  # 1 km x 4 km, base at one end: transit eats the far sorties
    terrain = [["field"] * 50 for _ in range(200)]
    pieces = SortiePlanner(1080, 12, 6, 10_000).plan(grid, terrain, TABLE, (500.0, 10.0))
    size = lambda p: (p.i1 - p.i0) * (p.j1 - p.j0)
    near = [size(p) for p in pieces if p.j1 <= 50]
    far = [size(p) for p in pieces if p.j0 >= 170]
    assert min(near) > max(far)


def pool_of(sectors, grid):
    return SectorPool(sectors, grid)


def make_pool():
    grid, terrain = world()
    sectors = SortiePlanner(10_000, 12, 6, 50).plan(grid, terrain, TABLE, (0.0, 0.0))
    return grid, terrain, SectorPool(sectors, grid)


def test_every_assignment_gets_a_new_token_and_old_tokens_are_fenced():
    grid, terrain, pool = make_pool()
    t1 = pool.apply_assign(0, "d1", deadline=100)
    assert t1 == 1 and pool.apply_assign(0, "d1") == 1  # same assignment again: same token
    assert pool.apply_requeue(0, t1, start=7) == "requeued"  # d1's lease expired after 7 cells
    assert pool.sectors[0].start == 7 and 0 in pool.waiting
    t2 = pool.apply_assign(0, "d2", deadline=200)
    assert t2 == 2
    assert pool.apply_complete(0, "d1", token=t1) == "stale"  # d1 comes back late: refused
    assert pool.apply_requeue(0, t1, start=30) == "stale"
    assert pool.apply_complete(0, "d2", token=t2) == "done"
    assert pool.apply_complete(0, "d2", token=t2) == "duplicate"


def test_late_completion_is_accepted_while_nobody_else_holds_the_job():
    grid, terrain, pool = make_pool()
    t1 = pool.apply_assign(0, "d1", deadline=100)
    pool.apply_requeue(0, t1, start=0)  # expired: drone was out of contact, not dead
    assert pool.apply_complete(0, "d1", token=t1) == "done"  # the work was done
    assert 0 not in pool.waiting


def test_release_with_nothing_left_counts_as_done():
    grid, terrain, pool = make_pool()
    t = pool.apply_assign(0, "d1")
    n = len(pool.sectors[0].path())
    assert pool.apply_requeue(0, t, start=n) == "done"


def test_replicas_applying_the_same_commands_agree():
    grid, terrain, _ = make_pool()
    sectors = SortiePlanner(10_000, 12, 6, 50).plan(grid, terrain, TABLE, (0.0, 0.0))
    a, b = SectorPool(sectors, grid), SectorPool(sectors, grid)
    log = [("assign", 0, "d1"), ("assign", 1, "d2"), ("requeue", 0, 1, 5), ("assign", 0, "d3"), ("complete", 0, "d1", 1)]
    for p in (a, b):
        for c in log:
            if c[0] == "assign":
                p.apply_assign(c[1], c[2])
            elif c[0] == "requeue":
                p.apply_requeue(c[1], c[2], c[3])
            else:
                p.apply_complete(c[1], c[2], c[3])
    assert (a.leases, a.waiting, a.done, a.token) == (b.leases, b.waiting, b.done, b.token)


def tasker_for(grid, terrain, **planner):
    return Tasker(grid, terrain, TABLE, (0.0, 0.0), scenario(**planner))


def test_greedy_prefers_rubble_and_researches_it_before_an_open_field():
    grid, terrain = world()
    sectors = SortiePlanner(10_000, 12, 6, 100).plan(grid, terrain, TABLE, (0.0, 0.0))
    pool = SectorPool(sectors, grid)
    tasker = tasker_for(grid, terrain, strategy="greedy")
    poa = {(i, j): TABLE[terrain[j][i]]["poa"] for j in range(10) for i in range(20)}
    job, new, _ = tasker.choose(pool, "d1", 0, 0, None, poa, [])
    assert terrain[job.j0][job.i0] == "rubble" and not new
    pool.apply_complete(job.id, "d1")
    for c in job.cells():  # one pass at POD 0.4 leaves 60% of the rubble POA
        poa[c] *= 0.6
    job2, new2, _ = tasker.choose(pool, "d1", 0, 0, None, poa, [])
    # 0.6 x 0.4 = 0.24 per cell (rubble again) vs 0.3 x 0.9 = 0.27 (field): field first, but close
    assert terrain[job2.j0][job2.i0] == "field"
    pool.apply_assign(job2.id, "d1")
    pool.apply_complete(job2.id, "d1")
    for c in job2.cells():
        poa[c] *= 0.1
    job3, new3, _ = tasker.choose(pool, "d1", 0, 0, None, poa, [])
    assert new3 and job3.tile == job.tile and job3.pass_ == 1  # re-search the rubble


def test_sweep_first_covers_everything_before_any_research():
    grid, terrain = world()
    sectors = SortiePlanner(10_000, 12, 6, 100).plan(grid, terrain, TABLE, (0.0, 0.0))
    pool = SectorPool(sectors, grid)
    tasker = tasker_for(grid, terrain, strategy="sweep_first")
    poa = {(i, j): TABLE[terrain[j][i]]["poa"] for j in range(10) for i in range(20)}
    job, _, _ = tasker.choose(pool, "d1", 0, 0, None, poa, [])
    pool.apply_assign(job.id, "d1")
    pool.apply_complete(job.id, "d1")
    nxt, new, _ = tasker.choose(pool, "d1", 0, 0, None, poa, [])  # rubble still worth more, but unsearched first
    assert not new and nxt.pass_ == 0


def test_jobs_that_do_not_fit_the_battery_send_the_drone_home():
    grid, terrain = world()
    sectors = SortiePlanner(10_000, 12, 6, 100).plan(grid, terrain, TABLE, (0.0, 0.0))
    pool = SectorPool(sectors, grid)
    tasker = tasker_for(grid, terrain, strategy="greedy")
    poa = {(i, j): 1.0 for j in range(10) for i in range(20)}
    job, _, reason = tasker.choose(pool, "d1", 0, 0, 30.0, poa, [])
    assert job is None and reason == "rtb"
    job, _, _ = tasker.choose(pool, "d1", 0, 0, 1200.0, poa, [])
    assert job is not None


def test_no_work_left_is_wait_while_jobs_are_out_and_done_after():
    grid, terrain = world(4, 4, 4)
    sectors = SortiePlanner(10_000, 12, 6, 100).plan(grid, terrain, TABLE, (0.0, 0.0))
    pool = SectorPool(sectors, grid)
    tasker = tasker_for(grid, terrain, strategy="greedy", research={"max_passes": 1, "stop_frac": 0.1})
    poa = {(i, j): 1.0 for j in range(4) for i in range(4)}
    job, _, _ = tasker.choose(pool, "d1", 0, 0, None, poa, [])
    pool.apply_assign(job.id, "d1")
    assert tasker.choose(pool, "d2", 0, 0, None, poa, [])[2] == "wait"
    pool.apply_complete(job.id, "d1")
    assert tasker.choose(pool, "d2", 0, 0, None, poa, [])[2] == "done"


def test_cells_near_a_confirmed_victim_are_worth_more():
    grid, terrain = world()
    tasker = tasker_for(grid, terrain)
    near = tasker.boosted([(100.0, 100.0)])
    assert grid.cell_of(100.0, 100.0) in near and grid.cell_of(380.0, 180.0) not in near


def test_inspect_job_is_made_once_per_cluster():
    grid, terrain, pool = make_pool()
    tasker = tasker_for(grid, terrain)
    job = tasker.inspect_job(pool, "drone-1#5", 55.0, 65.0)
    assert job.kind == "inspect" and (job.i1 - job.i0, job.j1 - job.j0) == (1, 1)
    assert pool.apply_new(job.to_dict())
    assert tasker.inspect_job(pool, "drone-1#5", 55.0, 65.0) is None


def test_work_given_back_early_goes_before_untouched_work_of_equal_value():
    grid, terrain = world(20, 10, 20)  # all rubble: every piece is worth the same per cell
    sectors = SortiePlanner(10_000, 12, 6, 100).plan(grid, terrain, TABLE, (0.0, 0.0))
    pool = SectorPool(sectors, grid)
    tasker = tasker_for(grid, terrain, strategy="greedy", leftover_boost=0.5)
    poa = {(i, j): 1.0 for j in range(10) for i in range(20)}
    far = max(pool.sectors.values(), key=lambda s: s.i0 + s.j0)
    t = pool.apply_assign(far.id, "d1")
    pool.apply_requeue(far.id, t, start=len(far.path()) // 2)  # d1 went home halfway
    job, _, _ = tasker.choose(pool, "d2", 0, 0, None, poa, [])
    assert job.id == far.id
