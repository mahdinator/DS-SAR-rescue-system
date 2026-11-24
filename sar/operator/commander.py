"""Commander bot: stands in for a person in the chain of command.

  - says it is present (Presence, twice a second), like a person logged in at a terminal
  - checks the command state every second at its station
  - accepts command when it is offered (after `accept_delay_s` mission seconds,
    the time to read the handover briefing)
  - while in command: acknowledges lost-aircraft alerts and settles conflicting
    verdicts. Policy: when two analysts disagree, the possible person stays
    confirmed (a ground team checks; a missed person costs more than a wasted trip)
  - performs its own lines of the scenario's command script at their mission time

Kill it (control center or a scripted `kill`) to see succession: the leader
station offers command to the next person present.
"""
from __future__ import annotations

import asyncio

from ..common.messages import CommandReply, CommandRequest, Envelope, Heartbeat, Presence
from ..common.node import Node, run_node
from ..common.stations import StationTracker


class CommanderBot(Node):
    role = "operator"

    def __init__(self, rt, node_id, incarnation=0):
        super().__init__(rt, node_id, incarnation)
        cfg = next(o for o in self.scenario["operators"] if o["id"] == node_id)
        self.accept_delay = float(cfg.get("accept_delay_s", 30))
        self.ack_delay = float(cfg.get("ack_delay_s", 20))
        self.pinned = cfg.get("station")
        roster = {p["id"]: p["role"] for p in self.scenario["command"].get("roster", [])}
        self.my_role = roster.get(node_id, "commander")
        self.script = [s for s in self.scenario["command"].get("script", []) if s.get("by") == node_id]
        self.tracker = StationTracker(rt.ids("groundstation"), float(self.scenario["failure_detector"]["timeout_real_s"]))
        self.state: dict = {}
        self._n = 0
        self._waiting: dict[str, asyncio.Future] = {}
        self.busy: set[str] = set()
        self.on(CommandReply, self.on_reply)
        self.on(Heartbeat, self.on_heartbeat)

    @property
    def gs(self) -> str:
        return self.pinned or self.tracker.target()

    def on_heartbeat(self, env: Envelope, msg: Heartbeat) -> None:
        if msg.role == "groundstation":
            self.tracker.heard(msg.node, msg.epoch, msg.leader, msg.leader_id)

    def on_reply(self, env: Envelope, msg: CommandReply) -> None:
        if msg.person != self.id:
            return
        if msg.state:
            self.state = msg.state
        fut = self._waiting.pop(msg.req, None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    async def act(self, action: str, args: dict | None = None, tries: int = 5) -> CommandReply | None:
        for _ in range(tries):
            self._n += 1
            req = f"{self.id}:{self.incarnation}:{self._n}"
            fut = asyncio.get_running_loop().create_future()
            self._waiting[req] = fut
            await self.send(CommandRequest(person=self.id, req=req, action=action, args=args or {}), dst=self.gs)
            try:
                reply = await asyncio.wait_for(fut, 4.0)
            except asyncio.TimeoutError:
                self._waiting.pop(req, None)
                continue
            if action != "status":
                self.events.emit("my_command", action=action, args=args or {}, ok=reply.ok, reason=reply.reason)
            if reply.ok or "not committed" not in reply.reason and "no leader" not in reply.reason:
                return reply
            await asyncio.sleep(1.0)
        return None

    async def main(self) -> None:
        self.spawn(self.presence_loop())
        self.spawn(self.watch_loop())
        for item in self.script:
            self.spawn(self.scripted(item))

    async def presence_loop(self) -> None:
        while True:
            await self.send(Presence(person=self.id, role=self.my_role))
            await asyncio.sleep(0.5)

    async def scripted(self, item: dict) -> None:
        await self.clock.sleep(max(0.0, float(item["t"]) - self.clock.now()))
        await self.act(item["action"], item.get("args", {}))

    def later(self, key: str, delay_s: float, action: str, args: dict) -> None:
        """Do something after a human-like delay, once."""
        if key in self.busy:
            return
        self.busy.add(key)

        async def run():
            try:
                await self.clock.sleep(delay_s)
                await self.act(action, args)
            finally:
                await asyncio.sleep(3.0)
                self.busy.discard(key)
        self.spawn(run())

    async def watch_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self.act("status", tries=1)
            s = self.state
            offer = s.get("offer")
            if offer and offer.get("to") == self.id:
                self.later(f"accept:{offer.get('by')}:{offer.get('kind')}", self.accept_delay, "accept", {})
            if s.get("commander") != self.id:
                continue
            for alert in s.get("alerts", []):
                if not alert.get("acked_by"):
                    self.later(f"ack:{alert['id']}", self.ack_delay, "ack", {"alert": alert["id"]})
            for cid in s.get("conflicts", []):
                if f"resolve:{cid}" not in self.busy:
                    self.spawn(self.resolve(cid))

    async def resolve(self, cid: str) -> None:
        self.busy.add(f"resolve:{cid}")
        await self.clock.sleep(self.ack_delay)
        await self.act("resolve", {"cluster": cid, "decision": "confirmed"})


if __name__ == "__main__":
    run_node(CommanderBot)
