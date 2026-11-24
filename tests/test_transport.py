from sar.common.config import DEFAULTS, deep_merge
from sar.common.messages import Envelope
from sar.transport.faults import FaultSchedule
from sar.transport.linkmodel import LinkModel, channel_for

ROLES = {"gs-1": "groundstation", "gs-2": "groundstation", "bot-1": "operator",
         "drone-1": "drone", "drone-2": "drone"}


def model(faults=None, **radio):
    scenario = deep_merge(DEFAULTS, {"radio": radio, "faults": faults or []})
    return LinkModel(scenario, ROLES, FaultSchedule(scenario["faults"]))


def env(src="drone-1", pos=(0.0, 0.0), seq=1, kind="sighting", dst="gs-1", attempt=0):
    return Envelope(src=src, seq=seq, kind=kind, body={}, dst=dst, pos=pos, ttl=3, attempt=attempt)


def test_channel_matrix():
    assert channel_for("drone", "drone", "telemetry") == "radio"
    assert channel_for("drone", "groundstation", "sighting") == "radio"
    assert channel_for("groundstation", "groundstation", "heartbeat") == "ground"
    assert channel_for("groundstation", "operator", "claim_request") == "ground"
    assert channel_for("drone", "operator", "telemetry") is None
    assert channel_for("groundstation", "drone", "claim_request") is None  # ground-only kind


def test_close_links_deliver_far_links_do_not():
    m = model(loss=0.0)
    assert m.decide(env(pos=(0, 0)), 100, "gs-1", (100, 0), 0).ok
    far = m.decide(env(pos=(0, 0)), 100, "gs-1", (2000, 0), 0)
    assert not far.ok and far.reason == "out_of_range"


def test_delivery_falls_off_between_r0_and_rmax():
    m = model(loss=0.0)  # drone-drone: r0 300, rmax 600

    def rate(d):
        return sum(m.decide(env(seq=s, dst="drone-2"), 100, "drone-2", (d, 0), 0).ok for s in range(2000)) / 2000

    assert rate(200) == 1.0
    assert 0.35 < rate(450) < 0.65  # halfway between r0 and rmax
    assert rate(650) == 0.0


def test_decision_is_deterministic_but_retry_rerolls():
    m = model(loss=0.5)
    first = [m.decide(env(seq=s), 100, "gs-1", (10, 0), 0).ok for s in range(200)]
    assert first == [m.decide(env(seq=s), 100, "gs-1", (10, 0), 0).ok for s in range(200)]
    retry = [m.decide(env(seq=s, attempt=1), 100, "gs-1", (10, 0), 0).ok for s in range(200)]
    assert retry != first


def test_isolate_fault_only_during_its_window():
    m = model(loss=0.0, faults=[{"t": 100, "duration": 50, "isolate": ["drone-1"]}])
    e = env(pos=(0, 0))
    assert m.decide(e, 100, "gs-1", (10, 0), 99).ok
    blocked = m.decide(e, 100, "gs-1", (10, 0), 120)
    assert not blocked.ok and blocked.reason == "isolate"
    assert m.decide(e, 100, "gs-1", (10, 0), 150).ok


def test_jam_zone_blocks_radio_but_not_ground():
    m = model(loss=0.0, faults=[{"t": 0, "jam": {"x": 0, "y": 0, "radius_m": 50}}])
    assert m.decide(env(pos=(10, 10)), 100, "gs-1", (100, 0), 5).reason == "jam"
    ground = Envelope(src="gs-1", seq=1, kind="claim_request", body={}, dst=None, pos=(10, 10))
    assert m.decide(ground, 100, "bot-1", None, 5).ok


def test_operators_never_hear_drones():
    m = model()
    assert m.decide(env(dst=None, kind="telemetry"), 100, "bot-1", None, 0).reason == "no_channel"
