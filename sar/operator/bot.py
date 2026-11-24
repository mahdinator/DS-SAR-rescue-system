"""Bot operator: stands in for a human analyst in headless experiments.

Works like a person at the review screen: claims one cluster at a time from the
station's queue, takes `delay_s` mission seconds to look at it, decides, then
asks for the next. Its judgement uses ground truth: it asks the world server
whether a real victim is near the cluster's estimated position, and answers
correctly with probability `accuracy`.

"Near" scales with the terrain's position error: radius = match_sigmas x sigma.
For 2-D Gaussian error, 2.5 sigma accepts about 96% of real sightings on any
terrain. Setting `match_radius_m` forces one fixed radius everywhere.

Which station it works at: `station: gs-2` pins it to that station's terminal (the
station forwards to the leader, or keeps decisions provisional when there is
none). Without it, the bot follows the leader with the highest epoch.
"""
from __future__ import annotations

import asyncio
import random

from ..common.grid import stable_int
from ..common.messages import ClaimRequest, Envelope, Heartbeat, NoReview, ReviewItem, Verdict, VerdictRecorded
from ..common.node import Node, run_node
from ..common.stations import StationTracker
from ..world.client import WorldClient


class BotOperator(Node):
    role = "operator"

    def __init__(self, rt, node_id, incarnation=0):
        super().__init__(rt, node_id, incarnation)
        cfg = next(o for o in self.scenario["operators"] if o["id"] == node_id)
        self.accuracy = float(cfg.get("accuracy", 0.95))
        self.delay_s = float(cfg.get("delay_s", 30))
        self.fixed_radius = cfg.get("match_radius_m")
        self.match_sigmas = float(cfg.get("match_sigmas", 2.5))
        self.poll_s = float(self.scenario["review"]["poll_real_s"])
        self.rng = random.Random(stable_int(rt.seed, node_id))
        self.world = WorldClient(self.ctx, rt.world_addr)
        self.pinned = cfg.get("station")
        self.tracker = StationTracker(rt.ids("groundstation"), float(self.scenario["failure_detector"]["timeout_real_s"]))
        self._reply: asyncio.Future | None = None
        self.on(ReviewItem, self.on_reply)
        self.on(NoReview, self.on_reply)
        self.on(VerdictRecorded, self.on_recorded)
        self.on(Heartbeat, self.on_heartbeat)

    @property
    def gs(self) -> str:
        return self.pinned or self.tracker.target()

    def on_heartbeat(self, env: Envelope, msg: Heartbeat) -> None:
        if msg.role == "groundstation":
            self.tracker.heard(msg.node, msg.epoch, msg.leader, msg.leader_id)

    def on_reply(self, env: Envelope, msg) -> None:
        if msg.operator == self.id and self._reply is not None and not self._reply.done():
            self._reply.set_result(msg)

    def on_recorded(self, env: Envelope, msg: VerdictRecorded) -> None:
        if msg.operator == self.id and not msg.ok:
            self.events.emit("my_verdict_refused", cluster=msg.cluster_id, reason=msg.reason)
        elif msg.operator == self.id and msg.provisional:
            self.events.emit("my_verdict_provisional", cluster=msg.cluster_id)

    async def main(self) -> None:
        self.spawn(self.work())

    async def work(self) -> None:
        while True:
            self._reply = asyncio.get_running_loop().create_future()
            await self.send(ClaimRequest(operator=self.id), dst=self.gs)
            try:
                reply = await asyncio.wait_for(self._reply, 2.0)
            except asyncio.TimeoutError:
                continue
            if isinstance(reply, NoReview):
                await asyncio.sleep(self.poll_s)
                continue
            await self.review(reply.cluster)

    def radius_for(self, c: dict) -> float:
        if self.fixed_radius is not None:
            return float(self.fixed_radius)
        return self.match_sigmas * float(c["sigma"])

    async def review(self, c: dict) -> None:
        await self.clock.sleep(self.delay_s * self.rng.uniform(0.5, 1.5))
        truth = await self.world.call("truth", x=c["x"], y=c["y"], radius=self.radius_for(c))
        is_real = truth["victim"] is not None
        correct = self.rng.random() < self.accuracy
        says_real = is_real if correct else not is_real
        decision = "confirmed" if says_real else "rejected"
        self.events.emit("review_done", cluster=c["id"], decision=decision, correct=correct, size=c["count"])
        await self.send(Verdict(cluster_id=c["id"], decision=decision, operator=self.id, seen=c["status"]), dst=self.gs)


if __name__ == "__main__":
    run_node(BotOperator)
