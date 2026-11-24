import itertools
import random

from sar.groundstation.dedup import NoDedup, RadiusDedup, RadiusSignatureDedup, cluster
from sar.groundstation.review import ReviewBoard

SIG_A = [1.0, 0.0, 0.0, 0.0]
SIG_B = [0.0, 1.0, 0.0, 0.0]


def s(sid, x, y, sig=SIG_A, conf=0.7, sigma=5.0):
    return {"id": sid, "x": x, "y": y, "sigma": sigma, "signature": sig, "confidence": conf,
            "drone": sid.split("#")[0], "i": 0, "j": 0}


def as_dict(items):
    return {i["id"]: i for i in items}


def test_clusters_do_not_depend_on_arrival_order():
    rng = random.Random(1)
    items = [s(f"d{k % 3}#{k}", rng.uniform(0, 60), rng.uniform(0, 60)) for k in range(12)]
    dedup = RadiusDedup(2.5)
    expected = cluster(as_dict(items), dedup)
    for _ in range(5):
        rng.shuffle(items)
        assert cluster(as_dict(items), dedup) == expected


def test_cluster_id_is_smallest_member_in_natural_order():
    items = [s("drone-1#10", 0, 0), s("drone-1#9", 1, 1)]
    assert list(cluster(as_dict(items), RadiusDedup(2.5))) == ["drone-1#9"]


def test_signature_keeps_two_nearby_people_apart():
    items = [s("a#1", 0, 0, SIG_A), s("b#1", 3, 0, SIG_B)]
    assert len(cluster(as_dict(items), RadiusDedup(2.5))) == 1  # position alone merges them
    assert len(cluster(as_dict(items), RadiusSignatureDedup(2.5, 0.7))) == 2
    assert len(cluster(as_dict(items), NoDedup())) == 2


def board(lease=30.0):
    now = [0.0]
    b = ReviewBoard(RadiusSignatureDedup(2.5, 0.7), lease, clock=lambda: now[0])
    return b, now


def test_claim_is_exclusive_and_expires():
    b, now = board(lease=10)
    b.add(s("a#1", 0, 0))
    assert b.claim("ali") == "a#1"
    assert b.claim("sara") is None  # nothing else to offer
    assert b.claim("ali") == "a#1"  # asking again gives the same claim
    now[0] = 11
    assert b.expire() == [("a#1", "ali")]
    assert b.claim("sara") == "a#1"


def test_compare_and_set_refuses_stale_or_foreign_verdicts():
    b, _ = board()
    b.add(s("a#1", 0, 0))
    b.claim("ali")
    ok, reason, _ = b.decide("sara", "a#1", "rejected", "pending")
    assert not ok and "claimed by ali" in reason
    assert b.decide("ali", "a#1", "confirmed", "pending")[0]
    ok, reason, _ = b.decide("sara", "a#1", "rejected", "pending")
    assert not ok and "status is confirmed" in reason


def test_new_sighting_of_confirmed_victim_needs_no_review():
    b, _ = board()
    b.add(s("a#1", 0, 0))
    b.claim("ali")
    b.decide("ali", "a#1", "confirmed", "pending")
    cid = b.add(s("b#1", 2, 1))
    assert cid == "a#1" and b.status(cid) == "confirmed"
    assert b.queue() == []


def test_sighting_near_rejected_object_is_rechecked_and_can_conflict():
    b, _ = board()
    b.add(s("a#1", 0, 0))
    b.claim("ali")
    b.decide("ali", "a#1", "rejected", "pending")
    b.add(s("b#1", 1, 0))
    assert b.status("a#1") == "recheck"
    b.add(s("c#1", 500, 500))
    assert b.queue() == ["c#1", "a#1"]  # rechecks come after fresh sightings
    b.claim("ali")  # takes c#1
    assert b.claim("sara") == "a#1"
    ok, _, applied = b.decide("sara", "a#1", "confirmed", "recheck")
    assert ok and applied == ["b#1"]  # the earlier rejection is not overwritten
    assert b.status("a#1") == "conflict"


def test_claim_follows_cluster_when_clusters_merge():
    b, _ = board()
    b.add(s("b#1", 0, 0))
    assert b.claim("ali") == "b#1"
    b.add(s("a#1", 1, 0))  # smaller id joins: cluster is now "a#1"
    assert b.claimed_by("a#1") == "ali"
    assert b.decide("ali", "b#1", "confirmed", "pending")[0]  # old id still resolves


def test_known_victims_only_lists_confirmed():
    b, _ = board()
    b.add(s("a#1", 0, 0))
    b.add(s("b#1", 300, 300))
    b.claim("ali")
    b.decide("ali", "a#1", "confirmed", "pending")
    known = b.known_victims()
    assert [k["cluster"] for k in known] == ["a#1"]
    assert known[0]["radius"] > 0


def test_two_operators_never_hold_the_same_cluster():
    b, _ = board()
    for k in range(6):
        b.add(s(f"a#{k}", k * 100, 0))
    held = [b.claim(op) for op in ("ali", "sara", "omar")]
    assert len(set(held)) == 3
    assert all(b.claimed_by(c) == op for c, op in zip(held, ("ali", "sara", "omar")))


def test_every_order_of_two_verdicts_gives_one_winner():
    for first, second in itertools.permutations(["ali", "sara"]):
        b, _ = board()
        b.add(s("a#1", 0, 0))
        results = [b.decide(op, "a#1", d, "pending")[0] for op, d in ((first, "confirmed"), (second, "rejected"))]
        assert results == [True, False]


def test_merged_verdict_fills_gaps_and_records_conflicts_confirm_wins():
    b, _ = board()
    b.add(s("a#1", 0, 0))
    b.add(s("a#2", 1, 0))
    assert b.apply_verdict(["a#1"], "rejected", "ali")["ok"]
    res = b.apply_verdict(["a#1", "a#2"], "confirmed", "sara", merged=True)  # provisional, made while cut off
    assert res == {"ok": True, "reason": "", "conflicts": 1}
    assert b.state_of("a#1") == "confirmed" and b.state_of("a#2") == "confirmed"
    assert b.status("a#1") == "conflict"


def test_normal_verdict_refused_at_apply_if_already_decided():
    b, _ = board()
    b.add(s("a#1", 0, 0))
    assert b.apply_verdict(["a#1"], "confirmed", "ali")["ok"]
    assert not b.apply_verdict(["a#1"], "rejected", "sara")["ok"]  # same order on every replica


def test_decision_can_arrive_before_its_sighting():
    b, _ = board()
    b.apply_verdict(["a#1"], "confirmed", "ali")  # replicated log got here first
    cid = b.add(s("a#1", 0, 0))  # anti-entropy delivers the sighting later
    assert b.status(cid) == "confirmed"
