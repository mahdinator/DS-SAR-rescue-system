"""Client side of the logging service: every node pushes structured events.

The logging service only observes. If it is down, events are dropped and the
mission is unaffected (sends never block).
"""
from __future__ import annotations

import time

import msgpack
import zmq


class EventClient:
    def __init__(self, addr: str, node_id: str, clock, lamport=None):
        self.node_id = node_id
        self.clock = clock
        self.lamport = lamport
        self.ctx = zmq.Context()
        self.sock = self.ctx.socket(zmq.PUSH)
        self.sock.setsockopt(zmq.SNDHWM, 100000)
        self.sock.setsockopt(zmq.LINGER, 2000)
        self.sock.connect(addr)

    def emit(self, ev: str, **fields) -> None:
        rec = {"t": round(self.clock.now(), 3), "wall": time.time(), "node": self.node_id, "ev": ev}
        if self.lamport is not None:
            rec["lamport"] = self.lamport.value
        rec.update(fields)
        try:
            self.sock.send(msgpack.packb(rec, use_bin_type=True, default=str), flags=zmq.NOBLOCK)
        except zmq.Again:
            pass
