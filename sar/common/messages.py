"""Message envelope and message types.

Every message travels inside an Envelope. The envelope carries the fields the
distributed-systems layers need (unique id, epoch, Lamport time, sender
position, relay TTL); the body carries the domain message.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import msgpack

_REGISTRY: dict[str, type] = {}


def message(kind: str):
    """Class decorator: make a dataclass and register it under `kind`."""

    def wrap(cls):
        cls = dataclass(cls)
        cls.KIND = kind
        _REGISTRY[kind] = cls
        return cls

    return wrap


def to_body(msg: Any) -> dict:
    return asdict(msg)


def from_body(kind: str, body: dict) -> Any:
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"unknown message kind: {kind}")
    return cls(**body)


@dataclass
class Envelope:
    src: str
    seq: int
    kind: str
    body: dict
    dst: str | None = None  # None = broadcast
    epoch: int = 0
    lamport: int = 0
    pos: tuple[float, float] | None = None  # position of the transmitter (src or relay)
    ttl: int = 0  # relay hops left
    hop: str | None = None  # relay that transmitted this copy (None = sent by src)
    attempt: int = 0  # retransmission number (0 = first send)

    @property
    def msg_id(self) -> str:
        """Globally unique message id: (sender, sequence number). Same for every
        retry and every relayed copy, which is what makes duplicates detectable."""
        return f"{self.src}:{self.seq}"

    def pack(self) -> bytes:
        return msgpack.packb(
            [
                self.src,
                self.seq,
                self.kind,
                self.body,
                self.dst,
                self.epoch,
                self.lamport,
                list(self.pos) if self.pos is not None else None,
                self.ttl,
                self.hop,
                self.attempt,
            ],
            use_bin_type=True,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "Envelope":
        src, seq, kind, body, dst, epoch, lamport, pos, ttl, hop, attempt = msgpack.unpackb(data, raw=False)
        return cls(src, seq, kind, body, dst, epoch, lamport, tuple(pos) if pos else None, ttl, hop, attempt)

    def message(self) -> Any:
        return from_body(self.kind, self.body)


# Delivery classes (architecture doc, section 7.4).
# Reliable: acknowledged and retried from the sender's outbox until acked.
RELIABLE_KINDS = {"sighting", "coverage", "sector_done", "job_release"}
# Ground-only: never sent over radio (ground station <-> operator traffic).
GROUND_ONLY_KINDS = {"claim_request", "review_item", "no_review", "verdict", "release_claim", "verdict_recorded",
                     "consensus", "forward", "forward_reply", "sync_digest", "sync_items",
                     "command_request", "command_reply", "presence"}


# --------------------------------------------------------------------------
# Membership and messaging
# --------------------------------------------------------------------------


@message("heartbeat")
class Heartbeat:
    node: str
    role: str
    epoch: int = 0  # station's current term/epoch
    leader: bool = False  # this station is the leader
    leader_id: str | None = None  # who this station thinks leads


@message("ack")
class Ack:
    msg_id: str


# --------------------------------------------------------------------------
# Drone <-> ground station
# --------------------------------------------------------------------------


@message("telemetry")
class Telemetry:
    drone: str
    x: float
    y: float
    state: str
    sector: int | None = None
    token: int = 0  # lease token of the job being flown
    progress: int = 0  # cells of the job's path done so far
    battery_s: float = -1.0  # flight time left (mission s); -1 = no battery model


@message("job_request")
class JobRequest:
    """A drone asks for work. `done` lists every [job, token] this drone finished
    and `released` every [job, token, next_cell] it gave back early, so a lost
    SectorDone or JobRelease can never leave a stale lease behind (the request
    is idempotent). `battery_s` lets the station offer only jobs that fit."""

    drone: str
    x: float
    y: float
    done: list
    released: list | None = None
    battery_s: float = -1.0


@message("assignment")
class Assignment:
    """Sector given to a drone. Cells i0 <= i < i1, j0 <= j < j1."""

    drone: str
    sector: int
    i0: int
    j0: int
    i1: int
    j1: int
    epoch: int = 0  # leader epoch that made it; drones ignore orders from an older epoch
    token: int = 0  # lease (fencing) token: every report about this job carries it
    start: int = 0  # first cell of the lawnmower path still to search (leftover jobs)
    kind: str = "search"  # search | inspect
    deadline: float = 0.0  # lease deadline (mission s): the job is given away after it


@message("no_job")
class NoJob:
    drone: str
    reason: str = "done"  # rtb (nothing fits this battery) | wait (work is out) | done


@message("sector_done")
class SectorDone:
    drone: str
    sector: int
    token: int = 0


@message("job_release")
class JobRelease:
    """The drone stops a job early (battery): cells from `start` on are left."""

    drone: str
    sector: int
    token: int
    start: int


@message("swap_request")
class SwapRequest:
    """A drone at base wants a fresh battery. Repeated until granted (idempotent)."""

    drone: str


@message("swap_grant")
class SwapGrant:
    """Your turn at the swap point."""

    drone: str
    waited_s: float = 0.0


@message("swap_done")
class SwapDone:
    drone: str


@message("presence")
class Presence:
    """A person at a terminal is there (the station leader notices when the IC is not)."""

    person: str
    role: str


@message("command_request")
class CommandRequest:
    """An action by a person in the chain of command. action "status" only reads."""

    person: str
    req: str
    action: str
    args: dict


@message("command_reply")
class CommandReply:
    person: str
    req: str
    ok: bool
    reason: str = ""
    state: dict | None = None


@message("revoke")
class Revoke:
    """The station tells a drone its lease is gone (expired and given away, or
    the job is done): stop flying it."""

    drone: str
    sector: int
    token: int
    reason: str = ""


@message("coverage")
class Coverage:
    """Cells a drone has passed over: [[i, j, mode], ...]."""

    drone: str
    passes: list


@message("sighting")
class Sighting:
    sighting_id: str  # "<drone>#<n>", unique
    drone: str
    t: float  # mission time of the observation
    x: float  # estimated victim position (noisy)
    y: float
    i: int
    j: int
    confidence: float
    signature: list
    evidence: float  # 0..1 quality of the evidence image
    mode: str  # "search" or "transit"
    thumb: list | None = None  # 16 x 16 grey evidence image, row-major


@message("known_victims")
class KnownVictims:
    """Confirmed victims, re-broadcast periodically (soft state). Drones stop
    reporting an object that matches one of these in position and signature."""

    victims: list  # [{"cluster", "x", "y", "radius", "signature"}]


# --------------------------------------------------------------------------
# Ground station <-> operators (pull-based review queue)
# --------------------------------------------------------------------------


@message("claim_request")
class ClaimRequest:
    """An operator asks for the next cluster to review."""

    operator: str


@message("review_item")
class ReviewItem:
    """A cluster claimed for this operator until `lease_s` real seconds from now."""

    operator: str
    cluster: dict


@message("no_review")
class NoReview:
    operator: str


@message("verdict")
class Verdict:
    cluster_id: str
    decision: str  # "confirmed" or "rejected"
    operator: str
    seen: str = "pending"  # cluster status the operator saw (compare-and-set)


@message("release_claim")
class ReleaseClaim:
    cluster_id: str
    operator: str


@message("verdict_recorded")
class VerdictRecorded:
    cluster_id: str
    decision: str
    operator: str
    ok: bool
    reason: str = ""
    provisional: bool = False  # stored locally, waiting for a reachable leader


# --------------------------------------------------------------------------
# Station <-> station (ground network)
# --------------------------------------------------------------------------


@message("consensus")
class ConsensusMsg:
    """Raft / primary-backup protocol message (append, vote, ...)."""

    data: dict


@message("forward")
class Forward:
    """A station passes an operation to the leader (operators and drones may talk to any station)."""

    req: str
    op: str
    args: dict


@message("forward_reply")
class ForwardReply:
    req: str
    result: dict


@message("sync_digest")
class SyncDigest:
    """Anti-entropy: the ids this station holds. The peer replies with what is missing."""

    sightings: list
    coverage: list


@message("sync_items")
class SyncItems:
    sightings: list
    coverage: list
