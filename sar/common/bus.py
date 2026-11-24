"""Step-1 transport: every node publishes on its own ZeroMQ PUB socket and
subscribes to every other node in the roster. Nothing routes traffic through
a central process. Step 2 adds the emulated radio link model on receive."""
from __future__ import annotations

import zmq
import zmq.asyncio


class ZmqBus:
    def __init__(self, ctx: zmq.asyncio.Context, bind_port: int, peers: dict[str, str]):
        self.ctx = ctx
        self.bind_port = bind_port
        self.peers = peers
        self.pub = None
        self.sub = None

    def start(self) -> None:
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.setsockopt(zmq.SNDHWM, 10000)
        self.pub.setsockopt(zmq.LINGER, 0)
        self.pub.bind(f"tcp://127.0.0.1:{self.bind_port}")
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.RCVHWM, 10000)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"")
        for addr in self.peers.values():
            self.sub.connect(addr)

    async def send(self, data: bytes) -> None:
        await self.pub.send(data)

    async def recv(self) -> bytes:
        return await self.sub.recv()
