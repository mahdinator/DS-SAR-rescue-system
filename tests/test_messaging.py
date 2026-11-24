from sar.common.messages import Envelope
from sar.membership.detector import TimeoutFailureDetector
from sar.messaging.outbox import Outbox
from sar.messaging.relay import should_relay
from sar.messaging.seen import SeenSet

STATIONS = {"gs-1": (0.0, 0.0)}


def env(src="drone-1", dst="gs-1", pos=(1000.0, 0.0), ttl=3, seq=1):
    return Envelope(src=src, seq=seq, kind="sighting", body={}, dst=dst, pos=pos, ttl=ttl)


def test_seen_set_reports_new_once_and_forgets_oldest():
    s = SeenSet(capacity=2)
    assert s.add("a") and not s.add("a")
    s.add("b")
    s.add("c")  # evicts "a"
    assert "a" not in s and "c" in s


def test_outbox_retries_with_backoff_until_acked():
    box = Outbox(retry_s=1.0, max_s=4.0, limit=10)
    e = env()
    box.add(e, now=0.0)
    assert box.due(0.5) == []
    assert box.due(1.0) == [e] and e.attempt == 1  # next try at 1 + 2
    assert box.due(2.9) == []
    assert box.due(3.0) == [e]  # next at 3 + 4 (capped)
    assert box.due(6.9) == []
    assert box.ack(e.msg_id) and not box.ack(e.msg_id)
    assert len(box) == 0 and box.retries == 2


def test_outbox_limit_drops_oldest():
    box = Outbox(1.0, 4.0, limit=2)
    for s in range(3):
        box.add(env(seq=s), 0.0)
    assert len(box) == 2 and box.dropped == 1


def test_relay_toward_station_only_if_closer():
    assert should_relay(env(pos=(1000, 0)), "drone-2", (600, 0), STATIONS, 10)
    assert not should_relay(env(pos=(1000, 0)), "drone-2", (1200, 0), STATIONS, 10)
    assert not should_relay(env(pos=(1000, 0), ttl=0), "drone-2", (600, 0), STATIONS, 10)


def test_relay_from_station_spreads_outward():
    hb = env(src="gs-1", dst=None, pos=(0, 0))
    assert should_relay(hb, "drone-2", (500, 0), STATIONS, 10)
    back = env(src="gs-1", dst="drone-1", pos=(500, 0))
    assert not should_relay(back, "drone-2", (100, 0), STATIONS, 10)


def test_never_relay_own_destination():
    assert not should_relay(env(dst="drone-2"), "drone-2", (600, 0), STATIONS, 10)


def test_failure_detector_suspects_and_recovers():
    now = [0.0]
    fd = TimeoutFailureDetector(3.0, clock=lambda: now[0])
    fd.watch("gs-1")
    now[0] = 2.0
    assert fd.check() == []
    now[0] = 3.5
    assert fd.check() == [("gs-1", "suspected")] and fd.is_suspected("gs-1")
    fd.heard("gs-1")
    assert fd.check() == [("gs-1", "recovered")]
