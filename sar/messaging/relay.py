"""Multi-hop relay rule (geographic forwarding).

Flooding every packet through every drone would cost N x N transmissions. A drone
instead re-broadcasts a packet only if doing so moves it in the right direction:

  - to a ground station:      only if I am closer to that station than the transmitter
  - from a ground station:    only if I am farther from it than the transmitter (spread outward)
  - other drone traffic:      only if I am closer to my nearest station than the transmitter

The TTL caps the number of hops and the seen-set stops loops. A packet is
relayed at most once per drone.
"""
from __future__ import annotations

import math

from ..common.messages import Envelope

Point = tuple[float, float]


def _d(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def should_relay(env: Envelope, me_id: str, me_pos: Point | None, stations: dict[str, Point], margin: float) -> bool:
    if env.ttl <= 0 or me_pos is None or env.pos is None or env.dst == me_id:
        return False
    hop_pos = env.pos
    if env.dst in stations:
        target = stations[env.dst]
        return _d(me_pos, target) < _d(hop_pos, target) - margin
    if env.src in stations:
        origin = stations[env.src]
        return _d(me_pos, origin) > _d(hop_pos, origin) + margin
    if stations:
        target = min(stations.values(), key=lambda p: _d(me_pos, p))
        return _d(me_pos, target) < _d(hop_pos, target) - margin
    return False
