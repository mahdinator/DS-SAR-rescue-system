"""Drone node.

Behaviour: ask the ground station for a job, fly there at transit altitude,
search it in a lawnmower pattern (or take a closer look at one spot, for an
inspect job), report sightings and coverage, repeat.

Link-loss failsafe (step 2): a drone that loses its station keeps searching its
job (sightings wait in the outbox). If it needs new work and every station is
suspected, it flies toward base until one answers.

Replicated stations (step 4): the drone talks to the leader with the highest
epoch it has heard (any station accepts sightings), re-addresses queued messages
when the leader changes, and ignores assignments from an older epoch.

Jobs and battery (step 5):
  - every job comes with a lease token; every report about the job carries it.
  - battery: flight time drains it. Before each cell the drone checks that it can
    still get home with its reserve; if not ("bingo") it gives the rest of the job
    back (JobRelease, with the next cell to search) and flies home.
  - at base it swaps its battery, then asks for work again.
  - if the station says the lease is gone (Revoke), it stops that job at once.
"""
from __future__ import annotations

import asyncio
import math

from ..common.grid import Grid
from ..common.messages import (
    Assignment, Coverage, Envelope, Heartbeat, JobRelease, JobRequest, KnownVictims, NoJob, Revoke, SectorDone,
    Sighting, SwapDone, SwapGrant, SwapRequest, Telemetry,
)
from ..common.node import Node, run_node
from ..common.stations import StationTracker
from ..groundstation.dedup import cosine
from ..groundstation.planner import lawnmower
from ..world.client import WorldClient

TICK_REAL_S = 0.05  # real seconds between movement updates


class Bingo(Exception):
    """Battery is down to the way home plus the reserve."""


class Drone(Node):
    role = "drone"
    watch_roles = {"groundstation"}

    def __init__(self, rt, node_id, incarnation=0):
        super().__init__(rt, node_id, incarnation)
        cfg = self.scenario["drones"]
        self.cruise = float(cfg["cruise_mps"])
        self.search_speed = float(cfg["search_mps"])
        self.telemetry_period = float(cfg["telemetry_period_real_s"])
        self.coverage_period = float(cfg["coverage_period_real_s"])
        self.job_retry = float(cfg["job_retry_real_s"])
        bat = cfg["battery"]
        self.battery_on = bool(bat["enabled"])
        self.endurance = float(bat["endurance_s"])
        self.reserve = float(bat["reserve_s"])
        self.swap_s = float(bat["swap_s"])
        self.battery = self.endurance
        self.hover_s = float(self.scenario["planner"]["inspect"]["hover_s"])
        self.lease_margin = float(self.scenario["planner"]["lease_grace_s"])
        w = self.scenario["world"]
        self.grid = Grid(int(w["width_cells"]), int(w["height_cells"]), float(w["cell_m"]))
        self.base = (float(self.scenario["base"]["x"]), float(self.scenario["base"]["y"]))
        self.x, self.y = self.base
        self.pos = self.base
        self.state = "idle"
        self.down = False
        self.job: Assignment | None = None
        self.sector: int | None = None
        self.sector_rect: tuple[int, int, int, int] | None = None
        self.progress = 0
        self.done_sectors: list[list[int]] = []  # [job, token]
        self.released: list[list[int]] = []  # [job, token, next cell]
        self._job_task: asyncio.Task | None = None
        self._revoked = False
        self.last_cell: tuple[int, int] | None = None
        self.sense_seq = self.id_base  # restart-safe, like message ids
        self.sighting_seq = self.id_base
        self.coverage_buffer: list[list] = []
        self.world = WorldClient(self.ctx, rt.world_addr)
        self.tracker = StationTracker(rt.ids("groundstation"), float(self.scenario["failure_detector"]["timeout_real_s"]))
        self._job_future: asyncio.Future | None = None
        self._swap_future: asyncio.Future | None = None
        self.launch = cfg.get("launch", "naive")
        self.index = sorted(rt.ids("drone")).index(node_id) if node_id in rt.ids("drone") else 0

        self.known: list[dict] = []
        self.suppress = bool(self.scenario["known_victims"]["suppress"])
        self.signature_threshold = float(self.scenario["dedup"]["signature_threshold"])

        self.on(Assignment, self.on_assignment)
        self.on(NoJob, self.on_no_job)
        self.on(Revoke, self.on_revoke)
        self.on(SwapGrant, self.on_swap_grant)
        self.on(KnownVictims, self.on_known_victims)
        self.on(Heartbeat, self.on_heartbeat)

    # -- which station to talk to -------------------------------------------------

    @property
    def gs(self) -> str:
        return self.tracker.target()

    def on_heartbeat(self, env: Envelope, msg: Heartbeat) -> None:
        if msg.role == "groundstation":
            self.tracker.heard(msg.node, msg.epoch, msg.leader, msg.leader_id)

    def retarget(self, env: Envelope) -> None:
        if env.dst in self.stations and env.dst != self.gs:
            env.dst = self.gs  # same msg_id: whichever station gets it first acks it

    def station_lost(self) -> bool:
        return all(self.fd.is_suspected(s) for s in self.stations)

    def on_known_victims(self, env: Envelope, msg: KnownVictims) -> None:
        self.known = msg.victims

    def matches_known(self, d: dict) -> str | None:
        """Same object as a confirmed victim? Close in position AND similar signature,
        so a second person lying next to a known victim is still reported."""
        for k in self.known:
            if (math.hypot(d["x"] - k["x"], d["y"] - k["y"]) <= k["radius"]
                    and cosine(d["signature"], k["signature"]) >= self.signature_threshold):
                return k["cluster"]
        return None

    # -- messages from the ground station --------------------------------------

    def on_assignment(self, env: Envelope, msg: Assignment) -> None:
        if msg.epoch < self.tracker.max_epoch:
            self.events.emit("stale_assignment_ignored", epoch=msg.epoch, current=self.tracker.max_epoch)
            return  # fencing: an order from a deposed leader
        if msg.drone == self.id and self._job_future and not self._job_future.done():
            self._job_future.set_result(msg)

    def on_no_job(self, env: Envelope, msg: NoJob) -> None:
        if msg.drone == self.id and self._job_future and not self._job_future.done():
            self._job_future.set_result(msg)

    def on_revoke(self, env: Envelope, msg: Revoke) -> None:
        job = self.job
        if msg.drone != self.id or job is None or job.sector != msg.sector or job.token != msg.token:
            return
        if self._job_task and not self._job_task.done():
            self._revoked = True
            self.events.emit("job_revoked", sector=msg.sector, token=msg.token, why=msg.reason)
            self._job_task.cancel()

    def battery_left(self) -> float:
        return round(self.battery, 1) if self.battery_on else -1.0

    async def request_job(self) -> Assignment | NoJob:
        """Ask until the station answers. The request is idempotent, so retries are safe.

        Failsafe: if the station is suspected (out of range or down), fly toward
        base while asking, and stop as soon as an answer arrives."""
        seeking: asyncio.Task | None = None
        try:
            while True:
                self._job_future = asyncio.get_running_loop().create_future()
                await self.send(JobRequest(drone=self.id, x=round(self.x, 1), y=round(self.y, 1),
                                           done=list(self.done_sectors), released=list(self.released),
                                           battery_s=self.battery_left()), dst=self.gs)
                try:
                    return await asyncio.wait_for(self._job_future, self.job_retry)
                except asyncio.TimeoutError:
                    self.events.emit("job_request_retry")
                if seeking is None and not self.at_base():
                    # No answer while in the air: don't hover burning battery, head home
                    # (toward the stations) and keep asking on the way.
                    self.state = "seeking_link"
                    self.events.emit("failsafe_seek_link", x=round(self.x), y=round(self.y),
                                     station_lost=self.station_lost())
                    seeking = self.spawn(self.fly_to(*self.base, mode="transit", bingo=False))
        finally:
            if seeking is not None and not seeking.done():
                seeking.cancel()

    def on_membership(self, node: str, state: str) -> None:
        self.events.emit("station_link", station=node, state=state, drone_state=self.state, outbox=len(self.outbox))

    # -- flight and battery ----------------------------------------------------

    def at_base(self) -> bool:
        return math.hypot(self.x - self.base[0], self.y - self.base[1]) < 1.0

    def time_home(self, x: float | None = None, y: float | None = None) -> float:
        x = self.x if x is None else x
        y = self.y if y is None else y
        return math.hypot(x - self.base[0], y - self.base[1]) / self.cruise

    def drain(self, seconds: float) -> None:
        if not self.battery_on:
            return
        self.battery -= seconds
        if self.battery <= 0 and not self.down:
            self.down = True
            self.state = "down"
            self.events.emit("battery_depleted", x=round(self.x), y=round(self.y), sector=self.sector)

    async def fly_to(self, tx: float, ty: float, mode: str, bingo: bool = True) -> None:
        speed = self.cruise if mode == "transit" else self.search_speed
        step = speed * TICK_REAL_S * self.clock.speedup
        while True:
            if self.down:
                await asyncio.Event().wait()  # fell out of the sky: silent from now on
            if bingo and self.must_turn_back():
                raise Bingo()
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            # Move in sub-steps of at most half a cell, so no cell is skipped at high speed-ups.
            move = min(dist, step)
            n = max(1, math.ceil(move / (self.grid.cell_m / 2)))
            for _ in range(n):
                self.x += dx / dist * move / n if dist else 0.0
                self.y += dy / dist * move / n if dist else 0.0
                self.check_cell(mode)
            if dist <= step:
                self.x, self.y = tx, ty
            self.pos = (self.x, self.y)
            if dist <= step:
                return
            await asyncio.sleep(TICK_REAL_S)

    async def hover(self, seconds: float) -> None:
        await self.clock.sleep(seconds)

    async def battery_loop(self) -> None:
        """Flight time drains the battery whenever the drone is in the air (flying,
        searching or hovering while it waits for an answer), not only while moving."""
        last = self.clock.now()
        while True:
            await asyncio.sleep(TICK_REAL_S)
            now = self.clock.now()
            if not self.at_base():
                self.drain(now - last)
            last = now

    def must_turn_back(self) -> bool:
        """Bingo: the battery, or the job's lease, only leaves time to get home.
        A lease holder stops before its lease runs out (with a margin for clock error),
        so the station can safely give the work away at the deadline."""
        home = self.time_home() + self.reserve
        if self.battery_on and self.battery <= home:
            return True
        job = self.job
        return bool(job and job.deadline) and self.clock.now() + home >= job.deadline - self.lease_margin

    def on_swap_grant(self, env: Envelope, msg: SwapGrant) -> None:
        if msg.drone == self.id and self._swap_future and not self._swap_future.done():
            self._swap_future.set_result(msg)

    async def swap_battery(self) -> None:
        """Queue for the swap point (one drone at a time, first come first served),
        swap, report done. If no station answers, the crew at base swaps anyway."""
        self.state = "queued"
        t0 = self.clock.now()
        self.events.emit("swap_queued", battery=self.battery_left())
        while True:
            self._swap_future = asyncio.get_running_loop().create_future()
            await self.send(SwapRequest(drone=self.id), dst=self.gs)
            try:
                await asyncio.wait_for(self._swap_future, 1.0)
                break
            except asyncio.TimeoutError:
                if self.station_lost():
                    self.events.emit("swap_without_station")
                    break
        self.events.emit("swap_started", waited_s=round(self.clock.now() - t0, 1))
        self.state = "swap"
        await self.clock.sleep(self.swap_s)
        self.events.emit("battery_swapped", left=round(self.battery, 1), waited_s=round(self.clock.now() - t0 - self.swap_s, 1))
        self.battery = self.endurance
        self.state = "idle"
        await self.send(SwapDone(drone=self.id), dst=self.gs)

    async def go_home(self) -> None:
        if self.at_base():
            return
        self.state = "return"
        self.sector_rect = None
        await self.fly_to(*self.base, mode="transit", bingo=False)
        self.state = "idle"
        self.events.emit("landed", battery=self.battery_left())

    def check_cell(self, mode: str) -> None:
        cell = self.grid.cell_of(self.x, self.y)
        if cell is None or cell == self.last_cell:
            return
        if mode == "transit" and self.in_sector(cell):
            return  # will be searched properly; don't count a weak transit pass
        self.last_cell = cell
        self.spawn(self.sense(cell, mode))

    def in_sector(self, cell: tuple[int, int]) -> bool:
        if self.sector_rect is None:
            return False
        i0, j0, i1, j1 = self.sector_rect
        return i0 <= cell[0] < i1 and j0 <= cell[1] < j1

    async def sense(self, cell: tuple[int, int], mode: str) -> None:
        self.sense_seq += 1
        req = self.sense_seq
        res = await self.world.call("sense", drone=self.id, cell=list(cell), mode=mode, req=req)
        self.coverage_buffer.append([cell[0], cell[1], mode])
        for det_index, d in enumerate(res["detections"]):
            known = self.matches_known(d) if self.suppress else None
            if known is not None:
                self.events.emit("sighting_suppressed", req=req, det=det_index, cluster=known, i=cell[0], j=cell[1])
                continue
            self.sighting_seq += 1
            sid = f"{self.id}#{self.sighting_seq}"
            self.events.emit("sighting_sent", sighting_id=sid, req=req, det=det_index, i=cell[0], j=cell[1], mode=mode)
            await self.send(
                Sighting(sighting_id=sid, drone=self.id, t=round(self.clock.now(), 2), x=d["x"], y=d["y"],
                         i=cell[0], j=cell[1], confidence=d["confidence"], signature=d["signature"],
                         evidence=d["evidence"], mode=mode, thumb=d.get("thumb")),
                dst=self.gs,
            )

    # -- periodic reports ------------------------------------------------------

    async def telemetry_loop(self) -> None:
        while True:
            if not self.down:
                await self.send(Telemetry(drone=self.id, x=round(self.x, 2), y=round(self.y, 2), state=self.state,
                                          sector=self.sector, token=self.job.token if self.job else 0,
                                          progress=self.progress, battery_s=self.battery_left()))
            # For the observer only (true position and internal state); not part of the protocol.
            self.events.emit("drone_state", x=round(self.x, 1), y=round(self.y, 1), state=self.state,
                             sector=self.sector, outbox=len(self.outbox), station_link=not self.station_lost(),
                             target=self.gs, known_victims=len(self.known), battery=self.battery_left(),
                             battery_pct=round(100 * self.battery / self.endurance) if self.battery_on else None,
                             job_kind=self.job.kind if self.job else None)
            await asyncio.sleep(self.telemetry_period)

    async def coverage_loop(self) -> None:
        while True:
            await asyncio.sleep(self.coverage_period)
            if self.coverage_buffer and not self.down:
                passes, self.coverage_buffer = self.coverage_buffer, []
                await self.send(Coverage(drone=self.id, passes=passes), dst=self.gs)

    # -- mission ---------------------------------------------------------------

    async def main(self) -> None:
        self.spawn(self.telemetry_loop())
        self.spawn(self.coverage_loop())
        self.spawn(self.battery_loop())
        self.spawn(self.mission())

    async def fly_job(self, job: Assignment) -> str:
        """Fly one job. Returns "done" or "bingo" (self.progress = next cell to search)."""
        path = lawnmower(job.i0, job.j0, job.i1, job.j1)
        self.progress = min(job.start, len(path))
        cells = path[self.progress:]
        if not cells:
            return "done"
        try:
            if self.must_turn_back():
                raise Bingo()
            self.state = "transit"
            await self.fly_to(*self.grid.center(*cells[0]), mode="transit")
            self.last_cell = None  # so the first cell is sensed in search mode
            if job.kind == "inspect":
                self.state = "inspect"
                self.last_cell = cells[0]
                self.spawn(self.sense(cells[0], "inspect"))
                await self.hover(self.hover_s)
                self.progress = len(path)
                return "done"
            self.state = "search"
            for cell in cells:
                await self.fly_to(*self.grid.center(*cell), mode="search")
                self.progress += 1
            return "done"
        except Bingo:
            return "bingo"

    async def mission(self) -> None:
        idle_logged = False
        if self.launch == "staggered" and self.index and self.incarnation == 0:
            # Spread take-offs by one swap time, so the drones also come back one by one.
            self.state = "standby"
            await self.clock.sleep(self.index * self.swap_s)
        while True:
            if self.battery_on and self.at_base() and self.battery < self.endurance - 1:
                await self.swap_battery()
            reply = await self.request_job()
            if isinstance(reply, NoJob):
                # rtb: nothing fits what is left in the battery; wait/done: no work right now.
                if not self.at_base():
                    self.events.emit("no_job_return", reason=reply.reason, battery=self.battery_left())
                    await self.go_home()
                elif not idle_logged:
                    self.events.emit("idle_at_base", reason=reply.reason)
                idle_logged = True
                self.state = "idle"
                await asyncio.sleep(self.job_retry * 2)
                continue
            idle_logged = False
            job = reply
            self.job = job
            self.sector = job.sector
            self.sector_rect = (job.i0, job.j0, job.i1, job.j1)
            self._revoked = False
            self.events.emit("sector_started", sector=job.sector, token=job.token, start=job.start, kind=job.kind,
                             battery=self.battery_left())
            self._job_task = asyncio.create_task(self.fly_job(job))
            await asyncio.wait({self._job_task})
            result = "revoked" if self._job_task.cancelled() else self._job_task.result()
            if result == "done":
                self.done_sectors.append([job.sector, job.token])
                self.events.emit("sector_finished", sector=job.sector, token=job.token)
                await self.send(SectorDone(drone=self.id, sector=job.sector, token=job.token), dst=self.gs)
            elif result == "bingo":
                self.released.append([job.sector, job.token, self.progress])
                self.events.emit("bingo", sector=job.sector, token=job.token, next_cell=self.progress,
                                 battery=self.battery_left(), home_s=round(self.time_home(), 1))
                await self.send(JobRelease(drone=self.id, sector=job.sector, token=job.token, start=self.progress),
                                dst=self.gs)
            self.job = None
            self.sector = None
            self.sector_rect = None
            self.progress = 0
            if result == "bingo":
                await self.go_home()


if __name__ == "__main__":
    run_node(Drone)
