"""Base class for nodes that join the message bus (drones, ground stations, operators).

Receive pipeline for every packet on the wire:
  1. link model   - did I hear it (range, loss, faults), and how late?   (transport)
  2. liveness     - any packet from a node is a sign of life             (membership)
  3. seen-set     - first copy of this msg_id?                            (messaging)
  4. relay        - drones forward it if that moves it closer             (messaging)
  5. ack          - reliable messages addressed to me are acknowledged    (messaging)
  6. handler      - first copy only, if addressed to me or broadcast      (domain)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from collections import Counter, defaultdict
from typing import Any, Callable

import zmq
import zmq.asyncio

from ..membership.detector import TimeoutFailureDetector
from ..messaging.outbox import Outbox
from ..messaging.relay import should_relay
from ..messaging.seen import SeenSet
from ..transport.faults import FaultSchedule
from ..transport.linkmodel import RADIO_ROLES, LinkModel
from .bus import ZmqBus
from .clock import LamportClock, MissionClock
from .config import Runtime
from .events import EventClient
from .messages import GROUND_ONLY_KINDS, RELIABLE_KINDS, Ack, Envelope, Heartbeat, to_body

STATS_PERIOD_REAL_S = 2.0


class Node:
    role = "node"
    watch_roles: set[str] = set()  # roles whose liveness this node tracks

    def __init__(self, rt: Runtime, node_id: str, incarnation: int = 0):
        self.rt = rt
        self.id = node_id
        # A restarted node must never reuse an old message id, or receivers would
        # drop its new messages as duplicates. Each restart gets a new incarnation
        # number, and every sequence counter starts from incarnation x 10^9.
        self.incarnation = incarnation
        self.id_base = incarnation * 10**9
        self.scenario = rt.scenario
        self.clock = MissionClock(rt.mission_start, rt.speedup)
        self.lamport = LamportClock()
        self.events = EventClient(rt.logger_addr, node_id, self.clock, self.lamport)
        self.ctx = zmq.asyncio.Context()
        me = rt.nodes[node_id]
        peers = {
            nid: f"tcp://127.0.0.1:{n['port']}"
            for nid, n in rt.nodes.items()
            if nid != node_id and n.get("bus", True)
        }
        self.bus = ZmqBus(self.ctx, me["port"], peers)
        self.handlers: dict[str, Callable] = {}
        self.pos: tuple[float, float] | None = tuple(me["pos"]) if me.get("pos") else None
        self._seq = self.id_base
        self._tasks: set[asyncio.Task] = set()

        self.roles = {nid: n["role"] for nid, n in rt.nodes.items()}
        self.stations = {
            nid: tuple(n["pos"]) for nid, n in rt.nodes.items() if n["role"] == "groundstation" and n.get("pos")
        }
        self.faults = FaultSchedule(self.scenario.get("faults", []))
        self.link = LinkModel(self.scenario, self.roles, self.faults)
        radio = self.scenario["radio"]
        self.on_radio = self.role in RADIO_ROLES
        self.relay_enabled = bool(radio["relay"]) and self.role == "drone"
        self.relay_ttl = int(radio["relay_ttl"])
        self.relay_margin = float(radio["relay_margin_m"])
        msg_cfg = self.scenario["messaging"]
        self.reliable = bool(msg_cfg["reliable"])
        self.outbox = Outbox(float(msg_cfg["retry_real_s"]), float(msg_cfg["retry_max_real_s"]), int(msg_cfg["outbox_limit"]))
        self.fd = TimeoutFailureDetector(float(self.scenario["failure_detector"]["timeout_real_s"]))
        self.seen = SeenSet()  # subclasses must not reuse this name
        self.link_counts: dict[str, Counter] = defaultdict(Counter)
        self.counters = Counter()
        self.dup_kinds = Counter()

    # -- sending ---------------------------------------------------------------

    def on(self, msg_cls, handler: Callable) -> None:
        self.handlers[msg_cls.KIND] = handler

    async def send(self, msg: Any, dst: str | None = None) -> Envelope:
        self._seq += 1
        ttl = self.relay_ttl if (self.on_radio and msg.KIND not in GROUND_ONLY_KINDS) else 0
        env = Envelope(
            src=self.id,
            seq=self._seq,
            kind=msg.KIND,
            body=to_body(msg),
            dst=dst,
            lamport=self.lamport.tick(),
            ttl=ttl,
        )
        self.seen.add(env.msg_id)  # ignore echoes of my own packets
        if self.reliable and dst is not None and msg.KIND in RELIABLE_KINDS:
            self.outbox.add(env, time.monotonic())
        await self._transmit(env)
        return env

    async def _transmit(self, env: Envelope) -> None:
        env.pos = self.pos  # the link model needs the transmitter's position
        await self.bus.send(env.pack())

    async def _relay(self, env: Envelope) -> None:
        copy = Envelope(env.src, env.seq, env.kind, env.body, env.dst, env.epoch, env.lamport,
                        self.pos, env.ttl - 1, self.id, env.attempt)
        self.counters["relayed"] += 1
        await self.bus.send(copy.pack())

    # -- receiving -------------------------------------------------------------

    async def _recv_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            data = await self.bus.recv()
            try:
                env = Envelope.unpack(data)
            except Exception as exc:
                self.events.emit("bad_message", error=str(exc))
                continue
            decision = self.link.decide(env, len(data), self.id, self.pos, self.clock.now())
            if decision.reason != "no_channel":
                self.link_counts[env.hop or env.src][decision.reason] += 1
            if not decision.ok:
                continue
            if decision.delay_s > 0:
                loop.call_later(decision.delay_s, lambda e=env: self.spawn(self._deliver(e)))
            else:
                await self._deliver(env)

    async def _deliver(self, env: Envelope) -> None:
        try:
            msg = env.message()
        except Exception as exc:
            self.events.emit("bad_message", error=str(exc))
            return
        if self.roles.get(env.src) in self.watch_roles:
            self.fd.heard(env.src)
        first = self.seen.add(env.msg_id)
        if not first:
            self.counters["duplicates"] += 1
            self.dup_kinds[env.kind] += 1
        elif self.relay_enabled and should_relay(env, self.id, self.pos, self.stations, self.relay_margin):
            await self._relay(env)

        if env.kind == "ack":
            if env.dst == self.id:
                self.outbox.ack(msg.msg_id)
            return
        if env.dst is not None and env.dst != self.id:
            return
        if env.dst == self.id and env.kind in RELIABLE_KINDS:
            # Ack every copy, even duplicates: the earlier ack may have been lost.
            self.counters["acks_sent"] += 1
            await self.send(Ack(msg_id=env.msg_id), dst=env.src)
        if not first:
            return
        self.lamport.observe(env.lamport)
        handler = self.handlers.get(env.kind)
        if handler is None:
            return
        try:
            result = handler(env, msg)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            err = traceback.format_exc()
            print(err, file=sys.stderr, flush=True)
            self.events.emit("handler_error", kind=env.kind, error=err)

    # -- background loops --------------------------------------------------------

    def spawn(self, coro) -> asyncio.Task:
        """Run a coroutine in the background; errors are logged, not lost."""
        task = asyncio.create_task(self._guard(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _guard(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            err = traceback.format_exc()
            print(err, file=sys.stderr, flush=True)
            self.events.emit("task_error", error=err)

    async def _outbox_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            for env in self.outbox.due(time.monotonic()):
                self.retarget(env)
                await self._transmit(env)

    def retarget(self, env: Envelope) -> None:
        """Hook: change a retried message's destination (e.g. a new leader). Same msg_id."""

    async def _fd_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            for node, state in self.fd.check():
                self.events.emit(f"node_{state}", peer=node)
                self.on_membership(node, state)

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_PERIOD_REAL_S)
            links = {src: dict(c) for src, c in self.link_counts.items()}
            self.link_counts.clear()
            self.events.emit(
                "node_stats",
                links=links,
                outbox=len(self.outbox),
                outbox_max=self.outbox.max_size,
                retries=self.outbox.retries,
                acked=self.outbox.acked,
                outbox_dropped=self.outbox.dropped,
                suspected=sorted(self.fd.suspected),
                duplicates_by_kind=dict(self.dup_kinds),
                **dict(self.counters),
            )

    def on_membership(self, node: str, state: str) -> None:
        """Hook: a watched node became suspected or recovered."""

    async def _control_loop(self) -> None:
        """Live faults from the control center: a full snapshot replaces the last one."""
        sock = self.ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        sock.connect(self.rt.control_addr)
        version = None
        while True:
            state = json.loads(await sock.recv())
            if state.get("clock"):
                self.clock.apply(state["clock"])  # pause / speed changes from the control center
            self.faults.set_dynamic(state.get("faults", []))
            if state.get("loss") is not None:
                self.link.loss = float(state["loss"])
            if state.get("version") != version:
                version = state.get("version")
                self.events.emit("control_applied", version=version, faults=len(self.faults.dynamic),
                                 loss=self.link.loss)

    # -- lifecycle ---------------------------------------------------------------

    async def main(self) -> None:
        raise NotImplementedError

    async def run(self) -> None:
        if not isinstance(self.seen, SeenSet):
            raise TypeError(f"{type(self).__name__} overrides Node.seen; use another attribute name")
        self.bus.start()
        self.events.emit("node_started", role=self.role, incarnation=self.incarnation)
        self.spawn(self._recv_loop())
        if self.rt.control_addr:
            self.spawn(self._control_loop())
        await self.clock.wait_start()
        for nid, role in self.roles.items():
            if role in self.watch_roles and nid != self.id:
                self.fd.watch(nid)
        self.spawn(self._outbox_loop())
        self.spawn(self._fd_loop())
        self.spawn(self._stats_loop())
        await self.main()
        # main() returning does not stop the node: keep serving messages.
        await asyncio.Event().wait()


def run_async(coro) -> None:
    """asyncio.run with the selector loop on Windows (pyzmq needs it)."""
    if sys.platform == "win32":
        asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(coro)


def node_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, help="path to the run's runtime.json")
    parser.add_argument("--id", required=True, help="this node's id")
    parser.add_argument("--incarnation", type=int, default=0, help="restart count (0 = first start)")
    return parser.parse_args()


def run_node(node_cls) -> None:
    args = node_args()
    rt = Runtime.load(args.runtime)
    node = node_cls(rt, args.id, args.incarnation)
    try:
        run_async(node.run())
    except KeyboardInterrupt:
        pass


__all__ = ["Node", "run_node", "run_async", "node_args", "Heartbeat"]
