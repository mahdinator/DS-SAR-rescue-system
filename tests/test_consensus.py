"""Consensus on a simulated network: deterministic time, delays and partitions."""
import random

from sar.consensus.core import PrimaryBackup, Raft

CFG = {"heartbeat_real_s": 0.15, "election_timeout_real_s": [1.0, 2.0], "pb_takeover_real_s": 1.5}
DT = 0.05


class Net:
    def __init__(self, kind, ids, seed=1):
        self.t = 0.0
        self.rng = random.Random(seed)
        self.queue = []  # (deliver_at, src, dst, msg)
        self.blocked: set[frozenset] = set()
        self.down: set[str] = set()
        self.applied = {i: [] for i in ids}
        self.orphans = {i: [] for i in ids}
        self.lost = {i: 0 for i in ids}
        cls = Raft if kind == "raft" else PrimaryBackup
        self.nodes = {}
        for i in ids:
            self.nodes[i] = cls(
                i, ids, send=lambda dst, m, src=i: self._send(src, dst, m), now=lambda: self.t,
                apply=lambda idx, cmd, me=i: self.applied[me].append((idx, cmd.get("cid"))), cfg=CFG,
                rng=random.Random(f"{seed}-{i}"), on_orphans=lambda cmds, me=i: self.orphans[me].extend(cmds),
                on_event=lambda ev, me=i, **kw: self._event(me, ev, kw),
                on_reset=lambda me=i: self.applied[me].clear(),
            )

    def _event(self, me, ev, kw):
        if ev == "committed_lost":
            self.lost[me] += kw["count"]

    def _send(self, src, dst, m):
        self.queue.append((self.t + self.rng.uniform(0.002, 0.02), src, dst, m))

    def cut(self, a, b):
        self.blocked.add(frozenset((a, b)))

    def isolate(self, a):
        for b in self.nodes:
            if b != a:
                self.cut(a, b)

    def heal(self):
        self.blocked.clear()

    def run(self, seconds):
        end = self.t + seconds
        while self.t < end:
            self.t += DT
            due = [q for q in self.queue if q[0] <= self.t]
            self.queue = [q for q in self.queue if q[0] > self.t]
            for _, src, dst, m in due:
                if dst in self.down or src in self.down or frozenset((src, dst)) in self.blocked:
                    continue
                self.nodes[dst].handle(src, m)
            for i, n in self.nodes.items():
                if i not in self.down:
                    n.tick()

    def leaders(self):
        return sorted(i for i, n in self.nodes.items() if n.is_leader() and i not in self.down)

    def submit(self, node, cid):
        return self.nodes[node].submit({"op": "x", "cid": cid})


def committed_cids(n):
    return [e.cmd.get("cid") for e in n.log[:n.commit] if e.cmd.get("op") != "noop"]


def assert_prefix_consistent(net):
    logs = [committed_cids(n) for i, n in net.nodes.items()]
    for a in logs:
        for b in logs:
            k = min(len(a), len(b))
            assert a[:k] == b[:k]


# -- Raft ----------------------------------------------------------------------------------

def test_raft_elects_one_leader_and_replicates():
    net = Net("raft", ["gs-1", "gs-2", "gs-3"])
    net.run(4)
    assert len(net.leaders()) == 1
    leader = net.leaders()[0]
    for k in range(5):
        assert net.submit(leader, f"c{k}")
    net.run(1)
    for n in net.nodes.values():
        assert committed_cids(n) == [f"c{k}" for k in range(5)]


def test_raft_leader_isolated_majority_elects_new_and_minority_cannot_commit():
    net = Net("raft", ["gs-1", "gs-2", "gs-3"], seed=2)
    net.run(4)
    old = net.leaders()[0]
    net.isolate(old)
    idx = net.submit(old, "lonely")
    net.run(5)
    assert old not in net.leaders()  # stepped down: no majority
    assert len(net.leaders()) == 1
    assert net.nodes[old].commit < idx  # never committed
    new = net.leaders()[0]
    net.submit(new, "majority-side")
    net.run(1)
    net.heal()
    net.run(3)
    assert_prefix_consistent(net)
    assert "lonely" in [c.get("cid") for c in net.orphans[old]]  # handed back for re-submission
    assert all(lost == 0 for lost in net.lost.values())


def test_raft_survives_leader_crash():
    net = Net("raft", ["gs-1", "gs-2", "gs-3"], seed=3)
    net.run(4)
    old = net.leaders()[0]
    net.submit(old, "before")
    net.run(1)
    net.down.add(old)
    net.run(5)
    assert len(net.leaders()) == 1 and net.leaders()[0] != old
    survivors = [n for i, n in net.nodes.items() if i != old]
    assert all("before" in committed_cids(n) for n in survivors)


def test_raft_safety_under_random_partitions():
    ids = ["gs-1", "gs-2", "gs-3", "gs-4", "gs-5"]
    for seed in range(4):
        net = Net("raft", ids, seed=10 + seed)
        rng = random.Random(seed)
        k = 0
        for step in range(30):
            net.heal()
            for _ in range(rng.randint(0, 3)):
                a, b = rng.sample(ids, 2)
                net.cut(a, b)
            for leader in net.leaders():
                k += 1
                net.submit(leader, f"s{seed}-{k}")
            net.run(rng.uniform(0.3, 2.5))
            assert_prefix_consistent(net)
        net.heal()
        net.run(6)
        assert_prefix_consistent(net)
        assert all(lost == 0 for lost in net.lost.values())


# -- primary-backup -------------------------------------------------------------------------

def test_pb_backup_takes_over_from_dead_primary():
    net = Net("pb", ["gs-1", "gs-2"])
    net.run(1)
    assert net.leaders() == ["gs-1"]
    net.submit("gs-1", "a")
    net.run(0.5)
    net.down.add("gs-1")
    net.run(3)
    assert net.leaders() == ["gs-2"]
    assert "a" in committed_cids(net.nodes["gs-2"])


def test_pb_split_brain_then_repair_loses_lone_commits():
    net = Net("pb", ["gs-1", "gs-2"])
    net.run(1)
    net.cut("gs-1", "gs-2")
    net.run(3)
    assert net.leaders() == ["gs-1", "gs-2"]  # split brain: both act as primary
    net.submit("gs-1", "old-side")
    net.submit("gs-2", "new-side")
    net.run(0.5)
    assert "old-side" in committed_cids(net.nodes["gs-1"])  # committed alone
    net.heal()
    net.run(2)
    assert net.leaders() == ["gs-2"]  # higher epoch wins
    assert_prefix_consistent(net)
    assert net.lost["gs-1"] >= 1  # what gs-1 committed alone was overwritten
    assert "old-side" in [c.get("cid") for c in net.orphans["gs-1"]]  # and handed back to merge
