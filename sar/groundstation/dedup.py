"""Deduplication layer 3: which sightings show the same object?

Clusters are a *derived view*: they are recomputed from the set of sightings by a
deterministic algorithm, so any replica holding the same sightings computes the
same clusters without exchanging cluster data. A cluster's id is its smallest
sighting id.

Linking rule (single linkage, then connected components):
  - "none":             nothing is linked; every sighting is its own cluster
  - "radius":           close in position
  - "radius_signature": close in position AND similar appearance signature

"Close" scales with position error: two estimates of the same point differ by
noise with standard deviation sqrt(sa^2 + sb^2), so they are linked if they are
within link_sigmas times that.
"""
from __future__ import annotations

import math
from typing import Protocol


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def sort_key(sighting_id: str) -> tuple[str, int]:
    """Order "drone-2#10" after "drone-2#9" (natural order, deterministic)."""
    head, _, num = sighting_id.rpartition("#")
    return (head, int(num) if num.isdigit() else 0)


class Deduplicator(Protocol):
    def linked(self, a: dict, b: dict) -> bool: ...


class NoDedup:
    def linked(self, a: dict, b: dict) -> bool:
        return False


class RadiusDedup:
    def __init__(self, link_sigmas: float):
        self.link_sigmas = link_sigmas

    def close(self, a: dict, b: dict) -> bool:
        limit = self.link_sigmas * math.sqrt(a["sigma"] ** 2 + b["sigma"] ** 2)
        return math.hypot(a["x"] - b["x"], a["y"] - b["y"]) <= limit

    def linked(self, a: dict, b: dict) -> bool:
        return self.close(a, b)


class RadiusSignatureDedup(RadiusDedup):
    def __init__(self, link_sigmas: float, signature_threshold: float):
        super().__init__(link_sigmas)
        self.threshold = signature_threshold

    def linked(self, a: dict, b: dict) -> bool:
        return self.close(a, b) and cosine(a["signature"], b["signature"]) >= self.threshold


def make_dedup(cfg: dict) -> Deduplicator:
    kind = cfg.get("kind", "radius_signature")
    if kind == "none":
        return NoDedup()
    if kind == "radius":
        return RadiusDedup(float(cfg["link_sigmas"]))
    if kind == "radius_signature":
        return RadiusSignatureDedup(float(cfg["link_sigmas"]), float(cfg["signature_threshold"]))
    raise ValueError(f"unknown dedup {kind!r}")


def cluster(sightings: dict[str, dict], dedup: Deduplicator) -> dict[str, list[str]]:
    """Group sightings into clusters. Returns {cluster_id: [member ids sorted]}.

    Deterministic: the result depends only on the set of sightings, not on the
    order they arrived in.
    """
    ids = sorted(sightings, key=sort_key)
    parent = {s: s for s in ids}

    def find(s: str) -> str:
        while parent[s] != s:
            parent[s] = parent[parent[s]]
            s = parent[s]
        return s

    for n, a in enumerate(ids):
        for b in ids[n + 1:]:
            if dedup.linked(sightings[a], sightings[b]):
                ra, rb = find(a), find(b)
                if ra != rb:
                    # The root is always the smaller id, so the cluster id is its smallest member.
                    if sort_key(ra) < sort_key(rb):
                        parent[rb] = ra
                    else:
                        parent[ra] = rb
    groups: dict[str, list[str]] = {}
    for s in ids:
        groups.setdefault(find(s), []).append(s)
    return groups
