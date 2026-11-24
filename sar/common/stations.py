"""Which ground station to talk to: follow the leader with the highest epoch.

Drones and operators hear every station's heartbeat (epoch, am-I-leader). They
send to the leader with the highest epoch heard recently; orders from an older
epoch are ignored (fencing). If no leader is heard, any reachable station is
used: it accepts sightings by itself and forwards the rest to the leader once
there is one.
"""
from __future__ import annotations

import time
from typing import Callable


class StationTracker:
    def __init__(self, stations: list[str], timeout_s: float, clock: Callable[[], float] = time.monotonic):
        self.stations = sorted(stations)
        self.timeout_s = timeout_s
        self.clock = clock
        self.info: dict[str, dict] = {}
        self.max_epoch = 0

    def heard(self, node: str, epoch: int, leader: bool, leader_id: str | None) -> None:
        self.info[node] = {"epoch": epoch, "leader": leader, "leader_id": leader_id, "t": self.clock()}
        self.max_epoch = max(self.max_epoch, epoch)

    def alive(self) -> list[str]:
        now = self.clock()
        return [s for s, i in self.info.items() if now - i["t"] <= self.timeout_s]

    def leader(self) -> str | None:
        leaders = [(self.info[s]["epoch"], s) for s in self.alive() if self.info[s]["leader"]]
        return max(leaders)[1] if leaders else None

    def target(self) -> str:
        leader = self.leader()
        if leader:
            return leader
        alive = sorted(self.alive())
        return alive[0] if alive else self.stations[0]
