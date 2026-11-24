"""Review board: sightings, derived clusters, the review queue and claim leases.

Two kinds of state, replicated differently (architecture doc, section 8):
  - sightings: an add-only set. Any station accepts them, stations exchange what
    they are missing (anti-entropy), so all replicas converge.
  - decisions (sighting id -> confirmed / rejected): changed only by verdict
    commands applied from the replicated log, in the same order everywhere.
    A decision may arrive before its sighting; it applies as soon as it does.

Clusters are recomputed from the sightings (dedup.py). A cluster's status is
derived from its members' decisions:

    all pending                          -> pending    (in the queue)
    some confirmed, none rejected        -> confirmed  (new sightings join without a prompt)
    some rejected, some still pending    -> recheck    (back in the queue, low priority)
    all decided, all rejected            -> rejected
    confirmed and rejected members, or a
    sighting decided both ways           -> conflict   (flagged for the commander)

Concurrency control between operators:
  - claim: an operator takes the best free cluster for `lease_s` real seconds;
    nobody else is offered it. Claims live on the leader only (soft state).
  - compare-and-set: a verdict carries the status the operator saw, and is
    checked again when it is applied from the log. If anything was decided in
    between, it is refused. Merged verdicts (provisional, or re-submitted after a
    leader change) never refuse: they fill in what is undecided and record
    conflicts where the decisions differ, confirm winning.
"""
from __future__ import annotations

import math
import time
from collections import Counter
from typing import Callable

from .dedup import Deduplicator, cluster, sort_key

REVIEWABLE = ("pending", "recheck")


class ReviewBoard:
    def __init__(self, dedup: Deduplicator, lease_s: float, link_sigmas: float = 2.5,
                 clock: Callable[[], float] = time.monotonic):
        self.dedup = dedup
        self.lease_s = lease_s
        self.link_sigmas = link_sigmas
        self.clock = clock
        self.sightings: dict[str, dict] = {}
        self.decisions: dict[str, str] = {}
        self.decided_by: dict[str, str] = {}
        self.conflicts: set[str] = set()
        self.clusters: dict[str, list[str]] = {}
        self.cluster_of: dict[str, str] = {}
        self.claims: dict[str, tuple[str, float]] = {}  # cluster id -> (operator, expires)

    # -- sightings and clusters ----------------------------------------------------------

    def add(self, rec: dict) -> str:
        """Add a sighting (needs id, x, y, sigma, signature, confidence). Returns its cluster id."""
        rec = {k: v for k, v in rec.items() if k != "state"}
        self.sightings[rec["id"]] = rec
        self._recompute()
        return self.cluster_of[rec["id"]]

    def _recompute(self) -> None:
        self.clusters = cluster(self.sightings, self.dedup)
        self.cluster_of = {s: cid for cid, members in self.clusters.items() for s in members}
        # Cluster ids change when clusters merge; claims follow their cluster.
        claims: dict[str, tuple[str, float]] = {}
        for cid, (op, exp) in self.claims.items():
            new = self.cluster_of.get(cid, cid)
            if new not in claims or claims[new][1] < exp:
                claims[new] = (op, exp)
        self.claims = claims

    def state_of(self, sid: str) -> str:
        return self.decisions.get(sid, "pending")

    def resolve(self, cid: str) -> str | None:
        """Current cluster id for a (possibly outdated) cluster or sighting id."""
        return self.cluster_of.get(cid)

    def status(self, cid: str) -> str:
        members = self.clusters[cid]
        states = {self.state_of(s) for s in members}
        if any(s in self.conflicts for s in members) or ("confirmed" in states and "rejected" in states):
            return "conflict"
        if "confirmed" in states:
            return "confirmed"
        if "rejected" in states:
            return "recheck" if "pending" in states else "rejected"
        return "pending"

    def counts(self) -> dict:
        return dict(Counter(self.status(cid) for cid in self.clusters))

    def reset_decisions(self) -> None:
        """Forget everything that came from the log (it is about to be replayed)."""
        self.decisions.clear()
        self.decided_by.clear()
        self.conflicts.clear()

    # -- queue and claims ------------------------------------------------------------------

    def claimed_by(self, cid: str) -> str | None:
        claim = self.claims.get(cid)
        if claim and claim[1] > self.clock():
            return claim[0]
        return None

    def _score(self, cid: str) -> float:
        pending = [self.sightings[s] for s in self.clusters[cid] if self.state_of(s) == "pending"]
        best = max((s["confidence"] for s in pending), default=0.0)
        return best + 0.05 * (len(self.clusters[cid]) - 1)

    def queue(self) -> list[str]:
        """Reviewable, unclaimed clusters: pending first (most confident first), then rechecks."""
        free = [cid for cid in self.clusters if self.status(cid) in REVIEWABLE and self.claimed_by(cid) is None]
        return sorted(free, key=lambda c: (self.status(c) == "recheck", -self._score(c), sort_key(c)))

    def claim(self, operator: str) -> str | None:
        now = self.clock()
        for cid, (op, exp) in self.claims.items():
            if op == operator and exp > now and self.status(cid) in REVIEWABLE:
                return cid  # already holding one: same answer (idempotent)
        q = self.queue()
        if not q:
            return None
        self.claims[q[0]] = (operator, now + self.lease_s)
        return q[0]

    def release(self, operator: str, cid: str) -> bool:
        cid = self.resolve(cid) or cid
        if self.claimed_by(cid) == operator:
            del self.claims[cid]
            return True
        return False

    def expire(self) -> list[tuple[str, str]]:
        now = self.clock()
        gone = [(cid, op) for cid, (op, exp) in self.claims.items() if exp <= now]
        for cid, _ in gone:
            del self.claims[cid]
        return gone

    # -- verdicts ----------------------------------------------------------------------------

    def check(self, operator: str, cid: str, decision: str, seen: str) -> tuple[bool, str, list[str]]:
        """Can this verdict go into the log? Returns (ok, reason, sightings it decides)."""
        if decision not in ("confirmed", "rejected"):
            return False, f"bad decision {decision!r}", []
        current = self.resolve(cid)
        if current is None:
            return False, "unknown cluster", []
        holder = self.claimed_by(current)
        if holder is not None and holder != operator:
            return False, f"claimed by {holder}", []
        status = self.status(current)
        if status != seen:
            return False, f"status is {status}, you saw {seen}", []
        if status not in REVIEWABLE:
            return False, f"status is {status}", []
        return True, "", [s for s in self.clusters[current] if self.state_of(s) == "pending"]

    def apply_verdict(self, sids: list[str], decision: str, operator: str, merged: bool = False) -> dict:
        """Apply a committed verdict (same result on every replica: depends only on log order)."""
        if not merged and any(s in self.decisions for s in sids):
            return {"ok": False, "reason": "already decided (compare-and-set)", "conflicts": 0}
        conflicts = 0
        for s in sids:
            cur = self.decisions.get(s)
            if cur is None:
                self.decisions[s] = decision
                self.decided_by[s] = operator
            elif cur != decision:
                self.conflicts.add(s)
                conflicts += 1
                if decision == "confirmed":
                    self.decisions[s] = "confirmed"  # confirm wins: a missed person costs more
        for s in sids:
            cid = self.cluster_of.get(s)
            if cid and self.claims.get(cid, ("", 0))[0] == operator:
                del self.claims[cid]
        return {"ok": True, "reason": "", "conflicts": conflicts}

    def resolve_conflict(self, cid: str, decision: str, by: str) -> list[str]:
        """The commander settles a cluster decided both ways: one decision for every
        member, conflict flags cleared. Returns the sightings it applied to."""
        current = self.resolve(cid)
        members = list(self.clusters.get(current, [])) if current else []
        for s in members:
            self.decisions[s] = decision
            self.decided_by[s] = by
            self.conflicts.discard(s)
        return members

    def decide(self, operator: str, cid: str, decision: str, seen: str) -> tuple[bool, str, list[str]]:
        """Single-station shortcut: check, then apply at once."""
        ok, reason, applied = self.check(operator, cid, decision, seen)
        if not ok:
            return ok, reason, []
        self.apply_verdict(applied, decision, operator)
        self.claims.pop(self.resolve(cid), None)
        return True, "", applied

    # -- views -------------------------------------------------------------------------------

    def position(self, cid: str) -> tuple[float, float, float]:
        members = [self.sightings[s] for s in self.clusters[cid]]
        x = sum(m["x"] for m in members) / len(members)
        y = sum(m["y"] for m in members) / len(members)
        return x, y, max(m["sigma"] for m in members)

    def summary(self, cid: str, max_members: int = 6) -> dict:
        members = [self.sightings[s] for s in self.clusters[cid]]
        x, y, sigma = self.position(cid)
        claim = self.claims.get(cid)
        shown = sorted(members, key=lambda m: -m["confidence"])[:max_members]
        return {
            "id": cid,
            "status": self.status(cid),
            "count": len(members),
            "x": round(x, 1),
            "y": round(y, 1),
            "sigma": sigma,
            "i": members[0]["i"],
            "j": members[0]["j"],
            "max_confidence": max(m["confidence"] for m in members),
            "drones": sorted({m["drone"] for m in members}),
            "claimed_by": self.claimed_by(cid),
            "lease_left_s": round(claim[1] - self.clock(), 1) if claim and self.claimed_by(cid) else None,
            "members": [
                {**{k: m.get(k) for k in ("id", "drone", "t", "confidence", "evidence", "mode", "thumb")},
                 "state": self.state_of(m["id"])}
                for m in shown
            ],
        }

    def known_victims(self) -> list[dict]:
        out = []
        for cid in self.clusters:
            if self.status(cid) != "confirmed":
                continue
            x, y, sigma = self.position(cid)
            sigs = [self.sightings[s]["signature"] for s in self.clusters[cid]]
            mean = [sum(col) / len(sigs) for col in zip(*sigs)]
            norm = math.sqrt(sum(v * v for v in mean)) or 1.0
            out.append({
                "cluster": cid, "x": round(x, 1), "y": round(y, 1),
                "radius": round(self.link_sigmas * math.sqrt(2) * sigma, 1),
                "signature": [round(v / norm, 4) for v in mean],
            })
        return out
