"""Receiver-side link model: did this node "hear" a packet, and how late?

Every node receives every packet on the wire and runs the same deterministic
model before passing it up. No central process routes traffic.

Channels:
  radio  - drone <-> drone, drone <-> ground station: range, loss, latency, faults
  ground - ground station <-> ground station / operator: low latency, faults only
  none   - drone <-> operator (operators have no radio): never delivered
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..common.grid import stable_int
from ..common.messages import GROUND_ONLY_KINDS, Envelope
from .faults import FaultSchedule

RADIO_ROLES = {"drone", "groundstation"}
GROUND_ROLES = {"groundstation", "operator"}


def channel_for(a_role: str | None, b_role: str | None, kind: str) -> str | None:
    if a_role in GROUND_ROLES and b_role in GROUND_ROLES:
        return "ground"
    if a_role in RADIO_ROLES and b_role in RADIO_ROLES and kind not in GROUND_ONLY_KINDS:
        return "radio"
    return None


def unit(*parts) -> float:
    """Deterministic number in [0, 1) from the parts."""
    return stable_int(*parts) / 2**64


@dataclass
class LinkDecision:
    ok: bool
    reason: str  # "ok", "out_of_range", "loss", "isolate", "cut", "jam", "no_channel", "no_position"
    delay_s: float = 0.0
    channel: str | None = None


class LinkModel:
    def __init__(self, scenario: dict, roles: dict[str, str], faults: FaultSchedule):
        r = scenario["radio"]
        self.seed = int(scenario["seed"])
        self.roles = roles
        self.faults = faults
        self.enabled = bool(r["enabled"])
        self.loss = float(r["loss"])
        self.ranges = {role: (float(r[role]["r0_m"]), float(r[role]["rmax_m"])) for role in RADIO_ROLES}
        self.latency_s = float(r["latency_ms"]) / 1000
        self.jitter_s = float(r["jitter_ms"]) / 1000
        self.bytes_per_s = float(r["bytes_per_ms"]) * 1000
        self.ground_latency_s = float(scenario["ground"]["latency_ms"]) / 1000

    def range_for(self, a_role: str, b_role: str) -> tuple[float, float]:
        """A ground station's bigger antenna helps both directions: use the larger range."""
        ra, rb = self.ranges[a_role], self.ranges[b_role]
        return (max(ra[0], rb[0]), max(ra[1], rb[1]))

    def delivery_probability(self, d: float, r0: float, rmax: float) -> float:
        if d >= rmax:
            return 0.0
        p = 1.0 if d <= r0 else (rmax - d) / (rmax - r0)
        return p * (1.0 - self.loss)

    def decide(self, env: Envelope, size: int, rx_id: str, rx_pos, t: float) -> LinkDecision:
        hop = env.hop or env.src
        hop_role, rx_role = self.roles.get(hop), self.roles.get(rx_id)
        channel = channel_for(hop_role, rx_role, env.kind)
        if channel is None:
            return LinkDecision(False, "no_channel")
        blocked = self.faults.blocks(hop, rx_id, t, channel, env.pos, rx_pos)
        if blocked:
            return LinkDecision(False, blocked, channel=channel)
        if channel == "ground" or not self.enabled:
            return LinkDecision(True, "ok", self.ground_latency_s, channel)
        if env.pos is None or rx_pos is None:
            return LinkDecision(False, "no_position", channel=channel)
        d = math.hypot(env.pos[0] - rx_pos[0], env.pos[1] - rx_pos[1])
        r0, rmax = self.range_for(hop_role, rx_role)
        if d >= rmax:
            return LinkDecision(False, "out_of_range", channel=channel)
        # Each (transmitter, receiver, message, hop count) gets its own fixed roll,
        # so a retry or a relayed copy is an independent attempt.
        if unit(self.seed, "link", hop, rx_id, env.msg_id, env.ttl, env.attempt) >= self.delivery_probability(d, r0, rmax):
            return LinkDecision(False, "loss", channel=channel)
        jitter = unit(self.seed, "jitter", hop, rx_id, env.msg_id, env.ttl, env.attempt) * self.jitter_s
        return LinkDecision(True, "ok", self.latency_s + jitter + size / self.bytes_per_s, channel)
