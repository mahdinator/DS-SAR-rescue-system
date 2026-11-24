from sar.common.config import DEFAULTS, deep_merge
from sar.world.server import World


def scenario(seed=42, **world):
    return deep_merge(DEFAULTS, {"seed": seed, "world": world})


def test_same_seed_same_world():
    a, b = World(scenario(7)), World(scenario(7))
    assert a.terrain == b.terrain
    assert [(v.x, v.y) for v in a.victims] == [(v.x, v.y) for v in b.victims]


def test_different_seed_different_world():
    assert World(scenario(1)).terrain != World(scenario(2)).terrain


def test_sense_depends_on_cell_pass_not_on_drone():
    a, b = World(scenario(3)), World(scenario(3))
    v = a.victims[0]
    cell = (v.i, v.j)
    # Same n-th pass over the same cell gives the same result, whichever drone flies it.
    assert [a.sense("drone-1", cell, "search", 1) for _ in range(5)] == \
           [b.sense("drone-7", cell, "search", 99) for _ in range(5)]


def test_false_alarm_rate_does_not_change_victim_detections():
    low = World(scenario(9, victims=80))
    high = World(deep_merge(DEFAULTS, {"seed": 9, "world": {"victims": 80},
                                       "terrain": {t: {"pfa": 0.5} for t in DEFAULTS["terrain"]}}))
    cells = sorted({(v.i, v.j) for v in low.victims})

    def real(w):
        return [[d for d in w.sense("d", c, "search", 0) if d["truth"]] for c in cells]

    assert real(low) == real(high)


def test_transit_detects_less_than_search():
    w = World(scenario(4, victims=60))
    w.sensor.make_thumbs = False  # counts only
    cells = {(v.i, v.j) for v in w.victims}

    def real_hits(mode):
        hits = 0
        for req in range(200):
            for cell in cells:
                hits += sum(1 for d in w.sense("d", cell, mode, req * 1000 + hash(cell) % 997) if d["truth"])
        return hits

    assert real_hits("transit") < real_hits("search")


def test_transit_gives_fewer_false_alarms():
    w = World(scenario(8, victims=0))

    def false_alarms(mode):
        return sum(len(w.sense("d", (i, 0), mode, req)) for req in range(400) for i in range(w.grid.width))

    search, transit = false_alarms("search"), false_alarms("transit")
    assert search > 0
    assert transit < 0.75 * search  # factor 0.5, with room for randomness


def test_decoys_are_fixed_and_come_back():
    a = World(scenario(11, victims=0, decoys=20))
    b = World(scenario(11, victims=0, decoys=20))
    assert [(d.x, d.y) for d in a.decoys] == [(d.x, d.y) for d in b.decoys]
    d = a.decoys[0]
    hits = sum(any(x["truth"] == d.id for x in a.sense("x", (d.i, d.j), "search", 0)) for _ in range(200))
    assert hits > 10  # the same decoy is detected again on later passes


def test_thumbnails_are_32_by_32():
    w = World(scenario(12, victims=50))
    v = w.victims[0]
    for _ in range(50):
        for d in w.sense("x", (v.i, v.j), "search", 0):
            assert len(d["thumb"]) == 1024 and all(0 <= p <= 255 for p in d["thumb"])


def test_truth_finds_nearest_victim():
    w = World(scenario(5))
    v = w.victims[0]
    assert w.truth(v.x + 1, v.y, radius=5)["victim"] is not None
    assert w.truth(-1000, -1000, radius=5)["victim"] is None


def test_victims_prefer_high_poa_terrain():
    w = World(scenario(6, victims=400))
    counts = {}
    for v in w.victims:
        counts[v.terrain] = counts.get(v.terrain, 0) + 1
    cells = {t: sum(row.count(t) for row in w.terrain) for t in w.table}
    density = {t: counts.get(t, 0) / cells[t] for t in cells if cells[t] > 50}
    if "rubble" in density and "open_field" in density:
        assert density["rubble"] > density["open_field"]
