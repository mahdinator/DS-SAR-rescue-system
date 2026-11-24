"""Fault script: what breaks, when, for how long (mission seconds).

Every node reads the same script and evaluates it against its own mission
clock, so no controller is needed to inject network faults. Process kills are
carried out by the launcher.

Scenario syntax (`duration` omitted = until the end of the mission):
    faults:
      - {t: 900,  duration: 300, isolate: [drone-2]}          # cut off from everyone else
      - {t: 900,  duration: 300, blackout: drone-2}           # same as isolate [drone-2]
      - {t: 1200, duration: 120, cut: [drone-1, gs-1]}        # one link only
      - {t: 1500, duration: 300, jam: {x: 500, y: 500, radius_m: 200}}  # radio dead zone
      - {t: 3000, kill: drone-3}                              # process crash (launcher)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Fault:
    kind: str
    start: float
    end: float
    nodes: list[str] = field(default_factory=list)
    zone: dict | None = None
    id: str | None = None  # set for faults injected live from the control center

    def active(self, t: float) -> bool:
        return self.start <= t < self.end

    def describe(self) -> dict:
        d = {"kind": self.kind, "start": self.start, "end": None if math.isinf(self.end) else self.end}
        if self.id:
            d["id"] = self.id
        if self.nodes:
            d["nodes"] = self.nodes
        if self.zone:
            d["zone"] = self.zone
        return d


def parse_faults(items: list[dict]) -> list[Fault]:
    faults = []
    for item in items or []:
        start = float(item["t"])
        end = start + float(item["duration"]) if "duration" in item else math.inf
        if "isolate" in item:
            faults.append(Fault("isolate", start, end, nodes=list(item["isolate"])))
        elif "blackout" in item:
            faults.append(Fault("isolate", start, end, nodes=[item["blackout"]]))
        elif "cut" in item:
            faults.append(Fault("cut", start, end, nodes=list(item["cut"])))
        elif "jam" in item:
            faults.append(Fault("jam", start, end, zone=dict(item["jam"])))
        elif "kill" in item:
            faults.append(Fault("kill", start, math.inf, nodes=[item["kill"]]))
        elif "restart" in item:
            faults.append(Fault("restart", start, math.inf, nodes=[item["restart"]]))
        else:
            raise ValueError(f"unknown fault: {item}")
    return faults


def _inside(zone: dict, pos) -> bool:
    if pos is None:
        return False
    return math.hypot(pos[0] - float(zone["x"]), pos[1] - float(zone["y"])) <= float(zone["radius_m"])


def fault_from_dict(d: dict) -> Fault:
    """Inverse of Fault.describe() (used for live faults sent by the control center)."""
    end = d.get("end")
    return Fault(d["kind"], float(d["start"]), math.inf if end is None else float(end),
                 nodes=list(d.get("nodes") or []), zone=d.get("zone"), id=d.get("id"))


class FaultSchedule:
    """Scripted faults from the scenario plus live faults from the control center.

    Live faults arrive as a full list that replaces the previous one (soft state):
    a node that missed an update, or restarted, is correct after the next one.
    """

    def __init__(self, items: list[dict]):
        self.static = parse_faults(items)
        self.dynamic: list[Fault] = []

    @property
    def faults(self) -> list[Fault]:
        return self.static + self.dynamic

    def set_dynamic(self, items: list[dict]) -> None:
        self.dynamic = [fault_from_dict(d) for d in items]

    def blocks(self, a: str, b: str, t: float, channel: str, pos_a, pos_b) -> str | None:
        """Reason the link a -> b is blocked at mission time t, or None."""
        for f in self.faults:
            if not f.active(t):
                continue
            if f.kind == "isolate" and ((a in f.nodes) != (b in f.nodes)):
                return "isolate"
            if f.kind == "cut" and {a, b} == set(f.nodes):
                return "cut"
            if f.kind == "jam" and channel == "radio" and (_inside(f.zone, pos_a) or _inside(f.zone, pos_b)):
                return "jam"
        return None

    def kills(self) -> list[tuple[float, str]]:
        return [(f.start, f.nodes[0]) for f in self.static if f.kind == "kill"]
