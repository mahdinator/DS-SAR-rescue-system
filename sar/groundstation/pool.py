"""The job pool: jobs wait here, drones hold them under a lease.

Replicated state. Only the apply_* methods change it, and they run on every
station in log order, so every replica holds the same pool. The leader decides
*which* job to hand out (tasking.py) and puts everything the replicas need into
the command (job definition, lease deadline), so applying needs no local
information that could differ between stations.

Leases (architecture doc, section 11):
  - assigning a job makes a lease (drone, token, deadline). The token is a
    counter that grows with every assignment: a fencing token.
  - the deadline is when the drone's battery would be empty (plus a margin): by
    then the drone is certainly on the ground, so no renewal messages are needed.
  - reports carry the token. A completion, release or progress report with an
    old token is refused (the job was given to someone else meanwhile).
  - release (drone came home early) and expire (deadline passed) put the job
    back with `start` set to how far it got: a leftover job.
"""
from __future__ import annotations

from ..common.grid import Grid
from .planner import Sector, distance

INSPECT_BASE = 1_000_000  # inspect job ids start here


class SectorPool:
    def __init__(self, sectors: list[Sector], grid: Grid):
        self.grid = grid
        self.plan = [Sector(**s.to_dict()) for s in sectors]
        self.tiles = {s.tile: s for s in sectors if s.pass_ == 0}
        self.reset()

    def reset(self) -> None:
        self.sectors: dict[int, Sector] = {s.id: Sector(**s.to_dict()) for s in self.plan}
        self.waiting: set[int] = set(self.sectors)
        self.assigned: dict[int, str] = {}  # job -> drone
        self.by_drone: dict[str, int] = {}  # drone -> job
        self.leases: dict[int, dict] = {}  # job -> {"drone", "token", "deadline"}
        self.done: set[int] = set()
        self.token = 0
        self.n_inspect = 0
        self.inspected: set[str] = set()  # clusters that already got an inspect job
        self.ntiles = max(self.tiles) + 1 if self.tiles else 0

    # -- reads -----------------------------------------------------------------------

    def current(self, drone: str) -> Sector | None:
        sid = self.by_drone.get(drone)
        return self.sectors[sid] if sid is not None else None

    def lease_of(self, job: int) -> dict | None:
        return self.leases.get(job)

    def lease_valid(self, job: int, drone: str, token: int) -> bool:
        lease = self.leases.get(job)
        return lease is not None and lease["drone"] == drone and lease["token"] == token

    def tile_busy(self, tile: int) -> bool:
        """A job of this tile is waiting or out: no re-search job for it yet."""
        return any(self.sectors[j].tile == tile and self.sectors[j].kind == "search"
                   for j in self.waiting | set(self.assigned))

    def passes_done(self, tile: int) -> int:
        return sum(1 for j in self.done if self.sectors[j].tile == tile and self.sectors[j].kind == "search")

    def research_id(self, tile: int, pass_: int) -> int:
        return pass_ * self.ntiles + tile

    def choose(self, drone: str, x: float, y: float, exclude: set[int] = frozenset()) -> Sector | None:
        """Static choice (steps 1-4): highest planner priority, then nearest."""
        free = self.waiting - set(exclude)
        if not free:
            return None
        best = min(free, key=lambda sid: (-self.sectors[sid].priority,
                                          distance((x, y), self.sectors[sid].center(self.grid)), sid))
        return self.sectors[best]

    # -- state changes (applied from the log on every replica) ----------------------------

    def apply_new(self, job: dict) -> bool:
        """Create a job (re-search or inspect) from its full definition in the command."""
        if job["id"] in self.sectors:
            return False
        s = Sector(**job)
        self.sectors[s.id] = s
        self.waiting.add(s.id)
        if s.kind == "inspect":
            self.n_inspect += 1
            if s.cluster:
                self.inspected.add(s.cluster)
        return True

    def apply_assign(self, sector_id: int, drone: str, deadline: float | None = None) -> int:
        """Lease a waiting job to a drone. Returns the lease token (0 = refused)."""
        lease = self.leases.get(sector_id)
        if lease and lease["drone"] == drone:
            return lease["token"]  # the same assignment again (idempotent)
        if sector_id not in self.waiting or drone in self.by_drone:
            return 0
        self.token += 1
        self.waiting.discard(sector_id)
        self.assigned[sector_id] = drone
        self.by_drone[drone] = sector_id
        self.leases[sector_id] = {"drone": drone, "token": self.token, "deadline": deadline}
        return self.token

    def _unassign(self, sector_id: int) -> None:
        owner = self.assigned.pop(sector_id, None)
        self.leases.pop(sector_id, None)
        if owner is not None and self.by_drone.get(owner) == sector_id:
            del self.by_drone[owner]

    def apply_complete(self, sector_id: int, drone: str, token: int | None = None, strict: bool = False) -> str:
        """Mark a job searched. Returns "done", "stale" (old token: the job is leased to
        someone else now) or "duplicate". token None = no fencing (orphan re-submits)."""
        if sector_id in self.done or sector_id not in self.sectors:
            return "duplicate"
        lease = self.leases.get(sector_id)
        if strict and (lease is None or lease["drone"] != drone):
            return "stale"
        if token is not None and lease is not None and lease["token"] != token:
            return "stale"
        # No lease (it expired and the job waits again): the work was done, accept it.
        self._unassign(sector_id)
        self.waiting.discard(sector_id)
        self.done.add(sector_id)
        return "done"

    def apply_requeue(self, sector_id: int, token: int, start: int) -> str:
        """Release or expiry: the job goes back, continuing from cell `start`.
        Returns "requeued", "done" (nothing left), or "stale"."""
        lease = self.leases.get(sector_id)
        if lease is None or lease["token"] != token:
            return "stale"
        self._unassign(sector_id)
        s = self.sectors[sector_id]
        s.start = max(s.start, min(int(start), len(s.path())))
        if s.start >= len(s.path()):
            self.done.add(sector_id)
            return "done"
        self.waiting.add(sector_id)
        return "requeued"

    def apply_cancel(self, sector_id: int) -> bool:
        """Drop a waiting job that is no longer worth doing (an inspect job whose
        cluster was decided meanwhile)."""
        if sector_id not in self.waiting:
            return False
        self.waiting.discard(sector_id)
        self.done.add(sector_id)
        return True

    # -- single-station shortcuts (tests) -------------------------------------------------

    def request(self, drone: str, x: float, y: float) -> Sector | None:
        cur = self.current(drone)
        if cur is not None:
            return cur
        s = self.choose(drone, x, y)
        if s is not None:
            self.apply_assign(s.id, drone)
        return s

    def complete(self, drone: str, sector_id: int) -> bool:
        return self.apply_complete(sector_id, drone, strict=True) == "done"

    def stats(self) -> dict:
        searches = [s for s in self.sectors.values() if s.kind == "search"]
        planned = {p.id for p in self.plan}
        return {"waiting": len(self.waiting), "assigned": len(self.assigned), "done": len(self.done),
                "total": len(self.sectors), "tiles": len(self.tiles),
                "research_jobs": sum(1 for s in searches if s.id not in planned),
                "inspect_jobs": self.n_inspect,
                "leftovers": sum(1 for s in searches if s.start > 0 and s.id in self.waiting)}
