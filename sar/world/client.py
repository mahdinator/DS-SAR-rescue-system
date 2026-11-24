"""Asynchronous client for the world server (one DEALER socket, many requests in flight)."""
from __future__ import annotations

import asyncio
import itertools

import msgpack
import zmq
import zmq.asyncio


class WorldError(RuntimeError):
    pass


class WorldClient:
    def __init__(self, ctx: zmq.asyncio.Context, addr: str, timeout_s: float = 5.0):
        self.sock = ctx.socket(zmq.DEALER)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.connect(addr)
        self.timeout_s = timeout_s
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        while True:
            frames = await self.sock.recv_multipart()
            res = msgpack.unpackb(frames[-1], raw=False)
            fut = self._pending.pop(res.get("rid"), None)
            if fut is not None and not fut.done():
                fut.set_result(res)

    async def call(self, op: str, **kwargs) -> dict:
        self.start()
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self.sock.send(msgpack.packb({"rid": rid, "op": op, **kwargs}, use_bin_type=True))
        try:
            res = await asyncio.wait_for(fut, self.timeout_s)
        finally:
            self._pending.pop(rid, None)
        if "error" in res:
            raise WorldError(res["error"])
        return res
