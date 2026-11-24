"""Ground station node, replicated (architecture doc, sections 8 and 9).

State and how it is kept consistent across stations:
  - sightings and coverage: add-only sets. Any station accepts them from drones
    and acknowledges; stations exchange what the others are missing every
    second (anti-entropy). Available even when stations are cut off.
  - sector assignments, completions and verdicts: commands in the replicated log
    (Raft or primary-backup). Applied in the same order on every station.
  - review claims: held by the leader only (short-lived; lost on failover, which
    costs an operator one re-claim).

Operators and drones may talk to any station. A station that is not the leader
forwards the operation to the leader. If no leader is reachable, verdicts are
stored provisionally and forwarded as merged commands once one is; sector
operations wait (drones keep their current work).

The log and the add-only sets are written to runs/<run>/state/<station>.jsonl,
so a restarted station comes back with its state.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import random
import time
from dataclasses import asdict
from pathlib import Path

from ..common.grid import Grid, stable_int
from ..common.messages import (
    Assignment, ClaimRequest, CommandReply, CommandRequest, ConsensusMsg, Coverage, Envelope, Forward, ForwardReply, Heartbeat, JobRelease,
    JobRequest, KnownVictims, NoJob, NoReview, Presence, ReleaseClaim, ReviewItem, Revoke, SectorDone, Sighting, SyncDigest,
    SwapDone, SwapGrant, SwapRequest, SyncItems, Telemetry, Verdict, VerdictRecorded,
)
from ..common.node import Node, run_node
from ..consensus.core import Store, make_replica
from ..world.client import WorldClient
from .command import CommandState
from .dedup import make_dedup
from .planner import Sector, make_planner
from .pool import SectorPool
from .review import ReviewBoard
from .tasking import Tasker

SYNC_BATCH = 150


class GroundStation(Node):
    role = "groundstation"
    watch_roles = {"drone"}

    def __init__(self, rt, node_id, incarnation=0):
        super().__init__(rt, node_id, incarnation)
        self.world = WorldClient(self.ctx, rt.world_addr)
        self.grid: Grid | None = None
        self.terrain = None
        self.table = self.scenario["terrain"]
        self.transit_factor = float(self.scenario["world"]["transit_pod_factor"])
        self.pool: SectorPool | None = None
        self.tasker: Tasker | None = None
        self.pcfg = self.scenario["planner"]
        self.expiring: set[tuple[int, int]] = set()
        self.revoked: dict[tuple[str, int, int], float] = {}
        self.progress_seen: dict[tuple[int, int], int] = {}
        # Chain of command: replicated state (log) plus who is present (soft state).
        self.ccmd = self.scenario["command"]
        self.command = CommandState(self.ccmd)
        self.presence: dict[str, float] = {}
        self.peer_beats: dict[str, dict] = {}  # other stations as this one hears them
        self.trails: dict[str, collections.deque] = {}
        self.ready_at = time.monotonic()
        self.closed_leases: set[tuple[int, int]] = set()  # (job, token) the drone itself finished or gave back
        # Battery swap queue at base (leader only, soft state like review claims: after
        # a failover the drones' repeated requests rebuild it).
        self.swap_queue: list[str] = []
        self.swap_since: dict[str, float] = {}  # drone -> mission time it joined the queue
        self.swapping: dict[str, float] = {}  # drone -> mission-time deadline of its turn
        self.swap_s = float(self.scenario["drones"]["battery"]["swap_s"])
        self.sectors: list[Sector] = []
        self.drones: dict[str, dict] = {}
        self.poa: dict[tuple[int, int], float] = {}
        self.coverage_batches: dict[str, list] = {}
        self.cell_passes: dict[tuple[int, int], int] = {}
        dcfg = self.scenario["dedup"]
        self.board = ReviewBoard(
            make_dedup(dcfg), float(self.scenario["review"]["claim_lease_real_s"]), float(dcfg["link_sigmas"])
        )
        self.received_t: dict[str, float] = {}
        self.ready = False

        # -- replication ------------------------------------------------------------
        self.ccfg = self.scenario["consensus"]
        self.station_ids = rt.ids("groundstation")
        self.peers = [s for s in self.station_ids if s != self.id]
        self.commit_timeout = float(self.ccfg["commit_timeout_real_s"])
        self.leader_timeout = float(self.ccfg["leader_timeout_real_s"])
        path = Path(rt.run_dir) / "state" / f"{self.id}.jsonl"
        loaded = Store.load(path)
        self.store = Store(path)
        self.loaded_items = loaded["items"]
        self.rep = make_replica(
            self.ccfg["kind"], self.id, self.station_ids, send=self._send_consensus, now=time.monotonic,
            apply=self._apply, cfg=self.ccfg, store=self.store, loaded=loaded,
            rng=random.Random(stable_int(rt.seed, self.id, incarnation)),
            on_event=self._on_consensus_event, on_orphans=self._on_orphans, on_reset=self._reset_replicated,
        )
        self.waiters: dict[str, asyncio.Future] = {}
        self.fwd_waiters: dict[str, asyncio.Future] = {}
        self.inflight: set[int] = set()
        self.provisional: list[tuple[str, dict]] = []
        self.emitted: set[tuple[str, str]] = set()
        self.chain = "0"
        self.checkpoints: dict[str, str] = {}
        self._n = 0

        self.on(Telemetry, self.on_telemetry)
        self.on(JobRequest, self.on_job_request)
        self.on(SectorDone, self.on_sector_done)
        self.on(JobRelease, self.on_job_release)
        self.on(SwapRequest, self.on_swap_request)
        self.on(CommandRequest, self.on_command_request)
        self.on(Presence, self.on_presence)
        self.on(Heartbeat, self.on_station_heartbeat)
        self.on(SwapDone, self.on_swap_done)
        self.on(Coverage, self.on_coverage)
        self.on(Sighting, self.on_sighting)
        self.on(ClaimRequest, self.on_claim_request)
        self.on(Verdict, self.on_verdict)
        self.on(ReleaseClaim, self.on_release)
        self.on(ConsensusMsg, self.on_consensus)
        self.on(Forward, self.on_forward)
        self.on(ForwardReply, self.on_forward_reply)
        self.on(SyncDigest, self.on_sync_digest)
        self.on(SyncItems, self.on_sync_items)

    # -- start-up -------------------------------------------------------------------

    async def main(self) -> None:
        m = await self.world.call("map")
        self.grid = Grid(m["width"], m["height"], m["cell_m"])
        self.terrain = m["terrain"]
        self.poa = {
            (i, j): self.table[self.terrain[j][i]]["poa"]
            for j in range(self.grid.height) for i in range(self.grid.width)
        }
        base = (float(self.scenario["base"]["x"]), float(self.scenario["base"]["y"]))
        self.sectors = make_planner(self.pcfg, self.scenario).plan(self.grid, self.terrain, self.table, base)
        self.pool = SectorPool(self.sectors, self.grid)
        self.tasker = Tasker(self.grid, self.terrain, self.table, base, self.scenario)
        self.tasker.set_zones(self.command.zone_weight)
        # Recover what this station knew before a restart, then replay its committed log.
        for kind, data in self.loaded_items:
            if kind == "sighting":
                self._add_sighting(data, persist=False)
            elif kind == "coverage":
                self._add_coverage(data["id"], data["passes"], persist=False)
        self.rep.replay()
        # Election and takeover timers start now, not at process start (so stations
        # that finished loading at the same moment do not all time out together).
        self.rep.leader_heard = time.monotonic()
        if hasattr(self.rep, "_reset_deadline"):
            self.rep._reset_deadline()
            if not self.rep.peers:
                self.rep.deadline = time.monotonic()
        self.ready = True
        self.ready_at = time.monotonic()
        self.events.emit("plan_ready", sectors=len(self.sectors), consensus=self.rep.kind,
                         planner=self.pcfg["kind"], strategy=self.tasker.strategy,
                         tiles=[[s.id, s.i0, s.j0, s.i1, s.j1] for s in self.sectors],
                         recovered_sightings=len(self.board.sightings), recovered_commit=self.rep.commit,
                         priorities=[s.priority for s in sorted(self.sectors, key=lambda s: -s.priority)])
        for loop in (self.consensus_loop, self.heartbeat_loop, self.status_loop, self.review_loop,
                     self.known_victims_loop, self.sync_loop, self.provisional_loop, self.lease_loop,
                     self.swap_loop, self.succession_loop):
            self.spawn(loop())
        port = self.rt.nodes[self.id].get("http_port")
        if port:
            from ..ui.app import serve  # imported only when the UI is enabled

            self.spawn(serve(self, int(port)))
            self.events.emit("ui_ready", url=f"http://127.0.0.1:{port}/")

    # -- consensus plumbing -----------------------------------------------------------

    def _send_consensus(self, dst: str, data: dict) -> None:
        self.spawn(self.send(ConsensusMsg(data=data), dst=dst))

    def on_consensus(self, env: Envelope, msg: ConsensusMsg) -> None:
        if self.ready:
            self.rep.handle(env.src, msg.data)

    async def consensus_loop(self) -> None:
        while True:
            self.rep.tick()
            await asyncio.sleep(0.05)

    def _on_consensus_event(self, ev: str, **kw) -> None:
        if ev == "role":
            self.events.emit("consensus_role", **kw, kind=self.ccfg["kind"])
            if kw["role"] != "leader":
                self.board.claims.clear()  # claims live on the leader only
        else:
            self.events.emit(f"consensus_{ev}", **kw)

    def _on_orphans(self, cmds: list[dict]) -> None:
        """Entries this station must discard (another leader's log won). Verdicts and
        completed sectors are re-submitted as merged commands; assignments are dropped
        (drones follow the newer epoch)."""
        n = 0
        for c in cmds:
            if c.get("op") == "verdict":
                self.provisional.append(("decide", {"operator": c["operator"], "cluster": c.get("cluster"),
                                                    "decision": c["decision"], "seen": c.get("seen", "pending"),
                                                    "applied": c["sids"], "merged": True}))
                n += 1
            elif c.get("op") == "complete":
                self.provisional.append(("complete_sector", {"drone": c["drone"], "sector": c["sector"]}))
                n += 1
        if n:
            self.events.emit("orphans_resubmitted", count=n)

    def _reset_replicated(self) -> None:
        if self.pool:
            self.pool.reset()
        self.board.reset_decisions()
        self.command.reset()
        if self.tasker:
            self.tasker.set_zones(self.command.zone_weight)
        self.chain = "0"
        self.checkpoints = {}
        self.events.emit("replicated_state_rebuilt")

    def _emit_once(self, cmd: dict, ev: str, **kw) -> None:
        key = (cmd.get("cid", ""), ev)
        if cmd.get("origin") == self.id and key not in self.emitted:
            self.emitted.add(key)
            self.events.emit(ev, **kw)

    def _apply(self, index: int, cmd: dict) -> None:
        """Apply one committed command. Runs on every station, in log order."""
        op = cmd.get("op")
        res: dict = {"ok": True}
        if op == "assign" and self.pool:
            new = cmd.get("job")
            if new:
                self.pool.apply_new(new)
            token = self.pool.apply_assign(cmd["sector"], cmd["drone"], cmd.get("deadline"))
            res = {"ok": True, "assigned": bool(token), "token": token}
            if token:
                s = self.pool.sectors[cmd["sector"]]
                self._emit_once(cmd, "sector_assigned", drone=cmd["drone"], sector=cmd["sector"],
                                priority=s.priority, epoch=cmd.get("epoch"), token=token, deadline=cmd.get("deadline"),
                                kind=s.kind, pass_=s.pass_, start=s.start, cells=len(s.cells()), new=bool(new),
                                rect=[s.i0, s.j0, s.i1, s.j1], tile=s.tile, value=cmd.get("value"))
        elif op == "complete" and self.pool:
            r = self.pool.apply_complete(cmd["sector"], cmd["drone"], cmd.get("token"))
            self.closed_leases.add((cmd["sector"], cmd.get("token")))
            res = {"ok": True, "done": r == "done", "result": r}
            if r == "done":
                self._emit_once(cmd, "sector_done", drone=cmd["drone"], sector=cmd["sector"],
                                kind=self.pool.sectors[cmd["sector"]].kind)
            elif r == "stale":
                self._emit_once(cmd, "completion_stale", drone=cmd["drone"], sector=cmd["sector"],
                                token=cmd.get("token"))
        elif op in ("release", "expire") and self.pool:
            r = self.pool.apply_requeue(cmd["sector"], cmd["token"], cmd["start"])
            if op == "release":
                self.closed_leases.add((cmd["sector"], cmd["token"]))
            res = {"ok": True, "result": r}
            if r != "stale":
                s = self.pool.sectors[cmd["sector"]]
                ev = "sector_released" if op == "release" else "lease_expired"
                self._emit_once(cmd, ev, drone=cmd["drone"], sector=cmd["sector"], token=cmd["token"],
                                start=s.start, left=len(s.cells()) if r == "requeued" else 0, lost=cmd.get("lost"))
                if op == "expire" and cmd.get("lost"):
                    alert = f"lost:{cmd['drone']}:{cmd['token']}"
                    self.command.raise_alert(alert, "lost_aircraft", drone=cmd["drone"], sector=cmd["sector"])
                    self._emit_once(cmd, "lost_aircraft", drone=cmd["drone"], sector=cmd["sector"], alert=alert)
        elif op == "new_job" and self.pool:
            ok = self.pool.apply_new(cmd["job"])
            res = {"ok": True, "created": ok}
            if ok:
                self._emit_once(cmd, "inspect_job_created", sector=cmd["job"]["id"], cluster=cmd["job"]["cluster"])
        elif op == "cancel" and self.pool:
            ok = self.pool.apply_cancel(cmd["sector"])
            res = {"ok": True, "cancelled": ok}
            if ok:
                self._emit_once(cmd, "job_cancelled", sector=cmd["sector"], why=cmd.get("why"))
        elif op == "verdict" and not self.command.may_review(cmd["operator"]):
            res = {"ok": False, "reason": f"{cmd['operator']} is not an analyst", "conflicts": 0}
            self._emit_once(cmd, "verdict_refused", cluster=cmd.get("cluster"), operator=cmd["operator"],
                            decision=cmd["decision"], reason=res["reason"], at="apply")
        elif op == "command":
            ok, reason = self.command.apply(index, cmd)
            res = {"ok": ok, "reason": reason}
            a = cmd.get("args", {})
            if ok and cmd["action"] == "resolve":
                res["applied"] = self.board.resolve_conflict(a["cluster"], a["decision"], cmd["by"])
            if ok and cmd["action"] in ("zone", "clear_zone") and self.tasker:
                self.tasker.set_zones(self.command.zone_weight)
            self._emit_once(cmd, "command_applied" if ok else "command_refused", by=cmd["by"], action=cmd["action"],
                            args=a, reason=reason, index=index, commander=self.command.commander,
                            acting=self.command.acting)
        elif op == "verdict":
            res = self.board.apply_verdict(cmd["sids"], cmd["decision"], cmd["operator"], cmd.get("merged", False))
            members = sorted({m for s in cmd["sids"] if (c := self.board.cluster_of.get(s))
                              for m in self.board.clusters[c]}) or list(cmd["sids"])
            if res["ok"]:
                self._emit_once(cmd, "verdict_committed", cluster=cmd.get("cluster"), decision=cmd["decision"],
                                operator=cmd["operator"], applied=cmd["sids"], members=members,
                                seen=cmd.get("seen"), merged=cmd.get("merged", False),
                                conflicts=res["conflicts"], index=index)
            else:
                self._emit_once(cmd, "verdict_refused", cluster=cmd.get("cluster"), operator=cmd["operator"],
                                decision=cmd["decision"], reason=res["reason"], at="apply")
            if res.get("conflicts"):
                self._emit_once(cmd, "verdict_conflict", cluster=cmd.get("cluster"), count=res["conflicts"],
                                decision=cmd["decision"], operator=cmd["operator"])
        self.chain = hashlib.blake2b((self.chain + str(cmd.get("cid"))).encode(), digest_size=8).hexdigest()
        if index % 20 == 0:
            self.checkpoints[str(index)] = self.chain
        fut = self.waiters.pop(cmd.get("cid"), None)
        if fut is not None and not fut.done():
            fut.set_result(res)

    async def replicate(self, cmd: dict) -> dict:
        """Submit a command as leader and wait until it is committed and applied."""
        self._n += 1
        cid = f"{self.id}:{self.incarnation}:{self._n}"
        cmd = {**cmd, "cid": cid, "origin": self.id, "epoch": self.rep.term}
        fut = asyncio.get_running_loop().create_future()
        self.waiters[cid] = fut
        t0 = time.monotonic()
        if self.rep.submit(cmd) is None:
            self.waiters.pop(cid, None)
            return {"ok": False, "reason": "not leader"}
        try:
            res = await asyncio.wait_for(fut, self.commit_timeout)
        except asyncio.TimeoutError:
            self.waiters.pop(cid, None)
            self.events.emit("commit_timeout", op=cmd.get("op"))
            return {"ok": False, "reason": "not committed in time", "timeout": True}
        self.events.emit("cmd_committed", op=cmd.get("op"), latency_ms=round(1000 * (time.monotonic() - t0), 1))
        return res

    # -- routing operations to the leader ---------------------------------------------

    def leader_reachable(self) -> str | None:
        if self.rep.is_leader():
            return self.id
        if self.rep.leader_alive(self.leader_timeout):
            return self.rep.leader
        return None

    async def call(self, op: str, args: dict, allow_provisional: bool = False) -> dict:
        if not self.ready:
            return {"ok": False, "reason": "station starting"}
        if self.rep.is_leader():
            return await self.local_op(op, args)
        leader = self.leader_reachable()
        if leader:
            self._n += 1
            req = f"{self.id}:{self.incarnation}:f{self._n}"
            fut = asyncio.get_running_loop().create_future()
            self.fwd_waiters[req] = fut
            await self.send(Forward(req=req, op=op, args=args), dst=leader)
            try:
                return await asyncio.wait_for(fut, self.commit_timeout + 1.0)
            except asyncio.TimeoutError:
                self.fwd_waiters.pop(req, None)
        if allow_provisional and op == "decide":
            return self._provisional_decide(args)
        return {"ok": False, "reason": "no leader reachable"}

    def _provisional_decide(self, a: dict) -> dict:
        if a.get("merged"):
            applied = a.get("applied") or []
        else:
            ok, reason, applied = self.board.check(a["operator"], a["cluster"], a["decision"], a["seen"])
            if not ok and not reason.startswith("claimed by"):
                return {"ok": False, "reason": reason}
            if not ok:
                cid = self.board.resolve(a["cluster"])
                applied = [s for s in self.board.clusters.get(cid, []) if self.board.state_of(s) == "pending"]
        self.provisional.append(("decide", {**a, "applied": applied, "merged": True}))
        self.events.emit("decision_provisional", cluster=a["cluster"], operator=a["operator"], decision=a["decision"])
        return {"ok": True, "provisional": True, "reason": "no leader reachable: saved provisionally"}

    async def local_op(self, op: str, a: dict) -> dict:
        if op == "job":
            return await self.leader_job(a)
        if op == "claim":
            return {"ok": True, "item": self._claim(a["operator"])}
        if op == "release":
            ok = self.board.release(a["operator"], a["cluster"])
            if ok:
                self.events.emit("claim_released", cluster=a["cluster"], operator=a["operator"])
            return {"ok": ok}
        if op == "decide":
            return await self.leader_decide(a)
        if op == "complete_sector":
            if self.pool and a["sector"] in self.pool.done:
                return {"ok": True}
            return await self.replicate({"op": "complete", "sector": a["sector"], "drone": a["drone"],
                                         "token": a.get("token")})
        if op == "inspect":
            cid = self.board.resolve(a["cluster"])
            if cid is None:
                return {"ok": False, "reason": "unknown cluster"}
            x, y, _ = self.board.position(cid)
            job = self.tasker.inspect_job(self.pool, cid, x, y)
            if job is None:
                return {"ok": False, "reason": "a closer look was already requested"}
            return await self.replicate({"op": "new_job", "job": job.to_dict(), "by": a.get("by")})
        if op == "command":
            return await self.replicate({"op": "command", "by": a["by"], "action": a["action"],
                                         "args": a.get("args", {}), "t": round(self.clock.now(), 1)})
        if op == "release_job":
            if self.pool and not self.pool.lease_valid(a["sector"], a["drone"], a["token"]):
                return {"ok": True, "result": "stale"}
            return await self.replicate({"op": "release", "sector": a["sector"], "drone": a["drone"],
                                         "token": a["token"], "start": a["start"]})
        return {"ok": False, "reason": f"unknown op {op}"}

    def on_forward(self, env: Envelope, msg: Forward) -> None:
        async def run():
            if not self.rep.is_leader():
                res = {"ok": False, "reason": "not leader"}
            else:
                res = await self.local_op(msg.op, msg.args)
            await self.send(ForwardReply(req=msg.req, result=res), dst=env.src)
        self.spawn(run())

    def on_forward_reply(self, env: Envelope, msg: ForwardReply) -> None:
        fut = self.fwd_waiters.pop(msg.req, None)
        if fut is not None and not fut.done():
            fut.set_result(msg.result)

    async def provisional_loop(self) -> None:
        """Hand provisional and orphaned decisions to the leader once one is reachable."""
        while True:
            await asyncio.sleep(1.0)
            while self.provisional and self.leader_reachable():
                op, args = self.provisional[0]
                res = await self.call(op, args)
                if not res.get("ok") and res.get("reason") in ("no leader reachable", "not leader",
                                                               "not committed in time", "station starting"):
                    break
                self.provisional.pop(0)
                self.events.emit("provisional_merged", op=op, ok=res.get("ok"), conflicts=res.get("conflicts", 0))

    # -- leader operations ------------------------------------------------------------

    def _sector_view(self, s: Sector) -> dict:
        lease = self.pool.lease_of(s.id) or {}
        return {"id": s.id, "i0": s.i0, "j0": s.j0, "i1": s.i1, "j1": s.j1, "epoch": self.rep.term,
                "token": lease.get("token", 0), "start": s.start, "kind": s.kind,
                "deadline": lease.get("deadline") or 0.0}

    def confirmed_positions(self) -> list[tuple[float, float]]:
        return [(k["x"], k["y"]) for k in self.board.known_victims()]

    async def leader_job(self, a: dict) -> dict:
        drone = a["drone"]
        pool = self.pool
        # Settle what the drone reports first (idempotent: lost reports are repeated here).
        for item in a.get("done", []):
            sid, tok = (int(item), None) if isinstance(item, int) else (int(item[0]), int(item[1]))
            if sid not in pool.done and (sid in pool.waiting or tok is None or pool.lease_valid(sid, drone, tok)):
                await self.replicate({"op": "complete", "sector": sid, "drone": drone, "token": tok})
        for sid, tok, start in a.get("released") or []:
            if pool.lease_valid(int(sid), drone, int(tok)):
                await self.replicate({"op": "release", "sector": int(sid), "drone": drone, "token": int(tok),
                                      "start": int(start)})
        cur = pool.current(drone)
        if cur is not None:
            return {"ok": True, "sector": self._sector_view(cur)}
        battery = a.get("battery_s")
        battery = None if battery is None or battery < 0 else float(battery)
        grace = float(self.pcfg["lease_grace_s"])
        for _ in range(3):
            s, new, reason = self.tasker.choose(pool, drone, a["x"], a["y"], battery, self.poa,
                                                self.confirmed_positions(), exclude=self.inflight)
            if s is None:
                return {"ok": True, "sector": None, "reason": reason}
            work, home = self.tasker.sortie_s(s, a["x"], a["y"])
            # The lease lasts as long as the drone can possibly stay up: its battery.
            life = battery if battery is not None else 3 * (work + home)
            deadline = round(self.clock.now() + life + grace, 1)
            self.inflight.add(s.id)
            try:
                res = await self.replicate({"op": "assign", "sector": s.id, "drone": drone, "deadline": deadline,
                                            "job": s.to_dict() if new else None,
                                            "value": round(self.tasker.value(s, self.poa, set()), 3)})
            finally:
                self.inflight.discard(s.id)
            if not res.get("ok"):
                return res
            if res.get("assigned"):
                return {"ok": True, "sector": self._sector_view(pool.sectors[s.id])}
            cur = pool.current(drone)
            if cur is not None:
                return {"ok": True, "sector": self._sector_view(cur)}
        return {"ok": False, "reason": "could not assign"}

    async def lease_loop(self) -> None:
        """Leader: give back jobs whose lease ran out (the drone must be down by now),
        and create closer-look jobs for doubtful clusters."""
        icfg = self.pcfg["inspect"]
        while True:
            await asyncio.sleep(1.0)
            if not self.rep.is_leader() or self.pool is None:
                continue
            now = self.clock.now()
            for sid, lease in list(self.pool.leases.items()):
                key = (sid, lease["token"])
                if lease.get("deadline") and now > lease["deadline"] and key not in self.expiring:
                    self.expiring.add(key)
                    start = self.progress_seen.get(key, 0)
                    lost = self.fd.is_suspected(lease["drone"]) or lease["drone"] not in self.drones
                    self.spawn(self._expire(key, {"op": "expire", "sector": sid, "drone": lease["drone"],
                                                  "token": lease["token"], "start": int(start), "lost": lost}))
            if icfg.get("enabled") and self.tasker.strategy != "static":
                await self._inspect_jobs(icfg)

    async def _expire(self, key: tuple, cmd: dict) -> None:
        res = await self.replicate(cmd)
        if not res.get("ok"):
            self.expiring.discard(key)  # not committed (lost leadership): try again later

    async def _inspect_jobs(self, icfg: dict) -> None:
        statuses = set(icfg.get("statuses", []))
        for sid in sorted(self.pool.waiting):
            s = self.pool.sectors[sid]
            if s.kind == "inspect" and s.cluster and \
                    self.board.status(self.board.resolve(s.cluster)) not in statuses | {"pending"}:
                await self.replicate({"op": "cancel", "sector": sid, "why": "decided"})
        for cid in list(self.board.clusters):
            if self.board.status(cid) in statuses and cid not in self.pool.inspected:
                x, y, _ = self.board.position(cid)
                job = self.tasker.inspect_job(self.pool, cid, x, y)
                if job is not None:
                    await self.replicate({"op": "new_job", "job": job.to_dict()})

    def _claim(self, operator: str) -> dict | None:
        cid = self.board.claim(operator)
        if cid is None:
            return None
        members = self.board.clusters[cid]
        first = min(self.received_t.get(s, self.clock.now()) for s in members)
        self.events.emit("review_claimed", cluster=cid, operator=operator, size=len(members),
                         status=self.board.status(cid), waited_s=round(self.clock.now() - first, 1))
        return self.board.summary(cid)

    async def leader_decide(self, a: dict) -> dict:
        merged = bool(a.get("merged"))
        if merged:
            applied = a.get("applied") or []
            cluster = a.get("cluster")
        else:
            ok, reason, applied = self.board.check(a["operator"], a["cluster"], a["decision"], a["seen"])
            if not ok:
                self.events.emit("verdict_refused", cluster=a["cluster"], operator=a["operator"],
                                 decision=a["decision"], reason=reason, at="check")
                return {"ok": False, "reason": reason}
            cluster = self.board.resolve(a["cluster"])
        res = await self.replicate({"op": "verdict", "sids": applied, "decision": a["decision"],
                                    "operator": a["operator"], "merged": merged, "cluster": cluster,
                                    "seen": a.get("seen")})
        if res.get("timeout") and not merged:
            # Lost leadership (or a majority) while committing: keep it, merge later.
            return self._provisional_decide({**a, "applied": applied, "merged": True})
        return res

    # -- drones -----------------------------------------------------------------------

    def on_telemetry(self, env: Envelope, msg: Telemetry) -> None:
        self.drones[msg.drone] = {"x": msg.x, "y": msg.y, "state": msg.state, "sector": msg.sector,
                                  "token": msg.token, "progress": msg.progress, "battery_s": msg.battery_s,
                                  "t": self.clock.now()}
        trail = self.trails.setdefault(msg.drone, collections.deque(maxlen=80))
        if not trail or abs(trail[-1][0] - msg.x) + abs(trail[-1][1] - msg.y) > 15:
            trail.append((round(msg.x), round(msg.y)))
        if msg.sector is not None:
            key = (msg.sector, msg.token)  # last progress heard for this lease (used if it expires)
            self.progress_seen[key] = max(self.progress_seen.get(key, 0), msg.progress)
        if msg.sector is not None and self.pool and self.rep.is_leader():
            self._check_lease(msg)

    def _check_lease(self, msg: Telemetry) -> None:
        """Fencing on the drone side: tell a drone flying a job it no longer holds.
        Only on positive evidence (job done, or leased under a newer token), never just
        because this leader has not applied the assignment yet."""
        pool, sid = self.pool, msg.sector
        if sid not in pool.sectors or msg.token > pool.token or (sid, msg.token) in self.closed_leases:
            return  # unknown yet, or a report sent just before the drone closed the job itself
        lease = pool.lease_of(sid)
        if sid in pool.done:
            why = "job already done"
        elif lease is not None and (lease["token"] != msg.token or lease["drone"] != msg.drone):
            why = f"job leased to {lease['drone']} (token {lease['token']})"
        elif lease is None and sid in pool.waiting and msg.token:
            why = "lease expired"
        else:
            return
        key = (msg.drone, sid, msg.token)
        now = time.monotonic()
        if now - self.revoked.get(key, -1e9) < 2.0:
            return
        self.revoked[key] = now
        self.events.emit("lease_revoked", drone=msg.drone, sector=sid, token=msg.token, why=why)
        self.spawn(self.send(Revoke(drone=msg.drone, sector=sid, token=msg.token, reason=why), dst=msg.drone))

    def on_job_request(self, env: Envelope, msg: JobRequest) -> None:
        async def run():
            res = await self.call("job", {"drone": msg.drone, "x": msg.x, "y": msg.y, "done": list(msg.done),
                                          "released": list(msg.released or []), "battery_s": msg.battery_s})
            if not res.get("ok"):
                return  # no leader reachable: the drone retries or goes looking for a station
            s = res.get("sector")
            if s is None:
                await self.send(NoJob(drone=msg.drone, reason=res.get("reason") or "done"), dst=msg.drone)
            else:
                await self.send(Assignment(drone=msg.drone, sector=s["id"], i0=s["i0"], j0=s["j0"], i1=s["i1"],
                                           j1=s["j1"], epoch=s["epoch"], token=s["token"], start=s["start"],
                                           kind=s["kind"], deadline=s["deadline"]), dst=msg.drone)
        self.spawn(run())

    def on_sector_done(self, env: Envelope, msg: SectorDone) -> None:
        self.spawn(self.call("complete_sector", {"drone": msg.drone, "sector": msg.sector,
                                                 "token": msg.token or None}))

    # -- chain of command ---------------------------------------------------------------

    def on_presence(self, env: Envelope, msg: Presence) -> None:
        self.presence[msg.person] = time.monotonic()

    def on_station_heartbeat(self, env: Envelope, msg: Heartbeat) -> None:
        if msg.role == "groundstation":
            self.peer_beats[msg.node] = {"epoch": msg.epoch, "leader": msg.leader, "leader_id": msg.leader_id,
                                         "at": time.monotonic()}

    async def ui_presence(self, person: str) -> None:
        """A person has the UI open at this station: tell every station (succession needs it)."""
        self.presence[person] = time.monotonic()
        await self.send(Presence(person=person, role=self.command.role_of(person) or "operator"))

    async def ui_command(self, person: str, action: str, args: dict) -> dict:
        return await self.call("command", {"by": person, "action": action, "args": args})

    async def ui_inspect(self, person: str, cid: str) -> dict:
        return await self.call("inspect", {"by": person, "cluster": cid})

    def present(self, person: str) -> bool:
        t = self.presence.get(person)
        return t is not None and time.monotonic() - t <= float(self.ccmd["absence_timeout_real_s"])

    def command_view(self) -> dict:
        v = self.command.view()
        v["present"] = sorted(p for p in self.command.roster if self.present(p))
        v["conflicts"] = sorted(c for c in self.board.clusters if self.board.status(c) == "conflict")
        return v

    def on_command_request(self, env: Envelope, msg: CommandRequest) -> None:
        async def run():
            if msg.action == "status":
                res = {"ok": True}
            else:
                res = await self.call("command", {"by": msg.person, "action": msg.action, "args": msg.args})
            await self.send(CommandReply(person=msg.person, req=msg.req, ok=bool(res.get("ok")),
                                         reason=res.get("reason", ""), state=self.command_view()), dst=env.src)
        self.spawn(run())

    async def succession_loop(self) -> None:
        """Leader: if the IC has been silent too long, offer command to the next
        present person in the succession list (they must accept). Absence is judged
        from presence messages, which every station hears, so a new leader can judge too."""
        timeout = float(self.ccmd["absence_timeout_real_s"])
        while True:
            await asyncio.sleep(1.0)
            c = self.command
            if not (c.enabled and self.ready and self.rep.is_leader()):
                continue
            if time.monotonic() - self.ready_at < 2 * timeout:
                continue  # give everyone time to be heard after start-up or a restart
            ic = c.commander
            if ic is not None and self.present(ic):
                continue
            if c.offer and c.offer["kind"] == "succession" and self.present(c.offer["to"]):
                continue  # already offered to someone who is there
            nxt = next((p for p in c.succession if p != ic and self.present(p)), None)
            if nxt is None or (c.offer and c.offer["to"] == nxt):
                continue
            self.events.emit("commander_absent", commander=ic, offered_to=nxt)
            await self.replicate({"op": "command", "by": "system", "action": "succession",
                                  "args": {"to": nxt, "absent": ic}, "t": round(self.clock.now(), 1)})

    # -- battery swap queue -------------------------------------------------------------

    def on_swap_request(self, env: Envelope, msg: SwapRequest) -> None:
        if not self.rep.is_leader():
            return  # the drone repeats its request to whoever leads
        d = msg.drone
        if d in self.swapping:
            self.spawn(self.send(SwapGrant(drone=d), dst=d))  # grant lost: send it again
        elif d not in self.swap_queue:
            self.swap_queue.append(d)
            self.swap_since[d] = self.clock.now()
            self.events.emit("swap_queue", drone=d, position=len(self.swap_queue), queue=list(self.swap_queue))

    def on_swap_done(self, env: Envelope, msg: SwapDone) -> None:
        self.swapping.pop(msg.drone, None)

    async def swap_loop(self) -> None:
        """One swap at a time. A turn lasts at most the swap time plus a margin, so a
        drone that dies at the swap point does not block the queue."""
        while True:
            await asyncio.sleep(0.1)
            if not self.rep.is_leader():
                self.swap_queue.clear()
                self.swapping.clear()
                continue
            now = self.clock.now()  # mission time: a paused mission does not end anyone's turn
            for d, until in list(self.swapping.items()):
                if now > until:
                    del self.swapping[d]
            if not self.swapping and self.swap_queue:
                d = self.swap_queue.pop(0)
                waited = round(self.clock.now() - self.swap_since.pop(d, self.clock.now()), 1)
                self.swapping[d] = now + self.swap_s + 60.0
                self.events.emit("swap_granted", drone=d, waited_s=waited, queue=len(self.swap_queue))
                await self.send(SwapGrant(drone=d, waited_s=waited), dst=d)

    def on_job_release(self, env: Envelope, msg: JobRelease) -> None:
        self.spawn(self.call("release_job", {"drone": msg.drone, "sector": msg.sector, "token": msg.token,
                                         "start": msg.start}))

    # -- add-only sets: coverage and sightings ------------------------------------------

    def _add_coverage(self, batch_id: str, passes: list, persist: bool = True) -> bool:
        if batch_id in self.coverage_batches or self.grid is None:
            return False
        self.coverage_batches[batch_id] = passes
        if persist:
            self.store.item("coverage", {"id": batch_id, "passes": passes})
        for i, j, mode in passes:
            cell = (int(i), int(j))
            pod = self.table[self.terrain[cell[1]][cell[0]]]["pod"]
            if mode == "transit":
                pod *= self.transit_factor
            # POA x (1 - POD): order-independent, so replicas converge without coordination.
            self.poa[cell] = self.poa.get(cell, 0.0) * (1.0 - pod)
            self.cell_passes[cell] = self.cell_passes.get(cell, 0) + 1
        return True

    def on_coverage(self, env: Envelope, msg: Coverage) -> None:
        self._add_coverage(env.msg_id, msg.passes)

    def sigma_at(self, i: int, j: int) -> float:
        if self.terrain is None:
            return 10.0
        return float(self.table[self.terrain[j][i]]["sigma"])

    def _add_sighting(self, rec: dict, persist: bool = True) -> str | None:
        if rec["id"] in self.board.sightings:
            return None
        cid = self.board.add(rec)
        self.received_t.setdefault(rec["id"], self.clock.now())
        if persist:
            self.store.item("sighting", rec)
        return cid

    def on_sighting(self, env: Envelope, msg: Sighting) -> None:
        rec = asdict(msg)
        rec["id"] = msg.sighting_id
        rec["sigma"] = self.sigma_at(msg.i, msg.j)
        cid = self._add_sighting(rec)
        if cid is None:
            self.events.emit("sighting_duplicate", sighting_id=msg.sighting_id)
            return
        status = self.board.status(cid)
        auto = status in ("confirmed", "conflict") and len(self.board.clusters[cid]) > 1
        self.events.emit("sighting_received", sighting_id=msg.sighting_id, drone=msg.drone,
                         x=msg.x, y=msg.y, confidence=msg.confidence, mode=msg.mode,
                         obs_t=msg.t, relayed=env.hop is not None, attempt=env.attempt,
                         cluster=cid, cluster_status=status, cluster_size=len(self.board.clusters[cid]),
                         auto_attached=auto)

    async def sync_loop(self) -> None:
        """Anti-entropy: tell each peer what we hold; it sends back what we lack."""
        while True:
            await asyncio.sleep(1.0)
            for p in self.peers:
                await self.send(SyncDigest(sightings=list(self.board.sightings),
                                           coverage=list(self.coverage_batches)), dst=p)

    async def on_sync_digest(self, env: Envelope, msg: SyncDigest) -> None:
        theirs_s, theirs_c = set(msg.sightings), set(msg.coverage)
        s = [self.board.sightings[k] for k in self.board.sightings if k not in theirs_s][:SYNC_BATCH]
        c = [{"id": k, "passes": v} for k, v in self.coverage_batches.items() if k not in theirs_c][:SYNC_BATCH]
        if s or c:
            await self.send(SyncItems(sightings=s, coverage=c), dst=env.src)

    def on_sync_items(self, env: Envelope, msg: SyncItems) -> None:
        added = sum(1 for rec in msg.sightings if self._add_sighting(rec) is not None)
        added += sum(1 for b in msg.coverage if self._add_coverage(b["id"], b["passes"]))
        if added:
            self.counters["synced_items"] += added

    # -- review queue (operators: bots over the ground network, people via the UI) -----------

    async def ui_claim(self, operator: str) -> dict:
        return await self.call("claim", {"operator": operator})

    async def ui_decide(self, operator: str, cid: str, decision: str, seen: str) -> dict:
        return await self.call("decide", {"operator": operator, "cluster": cid, "decision": decision, "seen": seen},
                               allow_provisional=True)

    async def ui_release(self, operator: str, cid: str) -> dict:
        return await self.call("release", {"operator": operator, "cluster": cid})

    def on_claim_request(self, env: Envelope, msg: ClaimRequest) -> None:
        async def run():
            res = await self.ui_claim(msg.operator)
            if res.get("ok") and res.get("item"):
                await self.send(ReviewItem(operator=msg.operator, cluster=res["item"]), dst=env.src)
            else:
                await self.send(NoReview(operator=msg.operator), dst=env.src)
        self.spawn(run())

    def on_verdict(self, env: Envelope, msg: Verdict) -> None:
        async def run():
            res = await self.ui_decide(msg.operator, msg.cluster_id, msg.decision, msg.seen)
            await self.send(VerdictRecorded(cluster_id=msg.cluster_id, decision=msg.decision, operator=msg.operator,
                                            ok=bool(res.get("ok")), reason=res.get("reason", ""),
                                            provisional=bool(res.get("provisional"))), dst=env.src)
        self.spawn(run())

    def on_release(self, env: Envelope, msg: ReleaseClaim) -> None:
        self.spawn(self.ui_release(msg.operator, msg.cluster_id))

    # -- periodic loops -------------------------------------------------------------------

    async def heartbeat_loop(self) -> None:
        period = float(self.scenario["heartbeat_real_s"])
        while True:
            await self.send(Heartbeat(node=self.id, role=self.role, epoch=self.rep.term,
                                      leader=self.rep.is_leader(), leader_id=self.rep.leader))
            await asyncio.sleep(period)

    async def review_loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(1.0)
            for cid, op in self.board.expire():
                self.events.emit("claim_expired", cluster=cid, operator=op)
            n += 1
            if n % 2 == 0 and self.rep.is_leader():
                snapshot = {}
                for cid, members in self.board.clusters.items():
                    x, y, _ = self.board.position(cid)
                    snapshot[cid] = {"members": members, "status": self.board.status(cid),
                                     "x": round(x, 1), "y": round(y, 1)}
                self.events.emit("cluster_snapshot", clusters=snapshot)

    async def known_victims_loop(self) -> None:
        """Re-broadcast confirmed victims (soft state: a lost copy is replaced by the next)."""
        period = float(self.scenario["known_victims"]["broadcast_real_s"])
        while True:
            await asyncio.sleep(period)
            if self.rep.is_leader():
                known = self.board.known_victims()
                if known:
                    await self.send(KnownVictims(victims=known))

    async def status_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            self.events.emit(
                "gs_status",
                pool=self.pool.stats() if self.pool else None,
                sightings=len(self.board.sightings),
                clusters=self.board.counts(),
                queue=len(self.board.queue()),
                covered_cells=len(self.cell_passes),
                total_cells=self.grid.cells if self.grid else 0,
                drones={d: v["state"] for d, v in self.drones.items()},
                suspected=sorted(self.fd.suspected),
                consensus=self.rep.describe(),
                provisional=len(self.provisional),
                checkpoints=self.checkpoints,
                chain=self.chain,
            )

    # -- views for the UI ----------------------------------------------------------------

    def ui_map(self) -> dict:
        return {
            "width": self.grid.width if self.grid else 0,
            "height": self.grid.height if self.grid else 0,
            "cell_m": self.grid.cell_m if self.grid else 0,
            "terrain": self.terrain or [],
            "base": [self.scenario["base"]["x"], self.scenario["base"]["y"]],
        }

    def ui_poa(self) -> dict:
        """Remaining probability per cell as this station sees it (POA x zone weight), 0-255."""
        if self.grid is None:
            return {"rows": []}
        zw = self.tasker.zone_w if self.tasker else {}
        vals = {c: p * zw.get(c, 1.0) for c, p in self.poa.items()}
        top = max(vals.values(), default=1.0) or 1.0
        rows = [[int(255 * vals.get((i, j), 0.0) / top) for i in range(self.grid.width)] for j in range(self.grid.height)]
        return {"rows": rows, "max": round(top, 4)}

    def ui_state(self) -> dict:
        pool = self.pool
        sectors = []
        if pool:
            for s in pool.sectors.values():
                state = "done" if s.id in pool.done else ("assigned" if s.id in pool.assigned else "waiting")
                if state == "done" and s.pass_ > 0:
                    continue  # finished re-search jobs: the first pass already draws that rectangle
                sectors.append({"id": s.id, "i0": s.i0, "j0": s.j0, "i1": s.i1, "j1": s.j1, "kind": s.kind,
                                "pass": s.pass_, "start": s.start, "cells": len(s.path()), "state": state,
                                "drone": pool.assigned.get(s.id)})
        clusters = []
        for cid in self.board.clusters:
            x, y, _ = self.board.position(cid)
            clusters.append({"id": cid, "x": round(x, 1), "y": round(y, 1), "status": self.board.status(cid),
                             "count": len(self.board.clusters[cid]), "claimed_by": self.board.claimed_by(cid)})
        endurance = float(self.scenario["drones"]["battery"]["endurance_s"])
        now_real = time.monotonic()
        drones = []
        for d, v in sorted(self.drones.items()):
            job = pool.sectors.get(v.get("sector")) if pool and v.get("sector") is not None else None
            lease = pool.lease_of(job.id) if job else None
            drones.append({"id": d, **v, "suspected": self.fd.is_suspected(d),
                           "age_s": round(self.clock.now() - v["t"], 1),
                           "battery_pct": round(100 * v["battery_s"] / endurance) if v.get("battery_s", -1) >= 0 else None,
                           "job": {"kind": job.kind, "pass": job.pass_, "cells": len(job.path()),
                                   "progress": v.get("progress", 0),
                                   "deadline": lease.get("deadline") if lease else None,
                                   "lease_ok": bool(lease and lease["drone"] == d and lease["token"] == v.get("token"))}
                           if job else None,
                           "trail": list(self.trails.get(d, []))})
        peers = {p: {**b, "age_s": round(now_real - b["at"], 1)} for p, b in self.peer_beats.items() if p != self.id}
        return {
            "t": round(self.clock.now(), 1),
            "station": self.id,
            "consensus": self.rep.describe(),
            "leader_reachable": self.leader_reachable(),
            "provisional": len(self.provisional),
            "peers": peers,
            "swap": {"queue": list(self.swap_queue), "swapping": list(self.swapping)} if self.rep.is_leader() else None,
            "command": self.command_view(),
            "audit": self.command.audit[-40:],
            "pool": pool.stats() if pool else None,
            "drones": drones,
            "sectors": sectors,
            "clusters": clusters,
            "queue": len(self.board.queue()),
            "counts": self.board.counts(),
            "covered_cells": len(self.cell_passes),
            "total_cells": self.grid.cells if self.grid else 0,
        }


if __name__ == "__main__":
    run_node(GroundStation)
