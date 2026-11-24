"""Outbox: reliable messages wait here until acknowledged.

Retries use exponential backoff (retry_s, 2 x retry_s, ... up to max_s). While a
node is out of contact the messages simply stay queued and keep being retried
at the slow rate; when any link returns they get through (store-and-forward).
Every retry carries the same msg_id, so receivers process it once.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from ..common.messages import Envelope


@dataclass
class _Entry:
    env: Envelope
    next_try: float
    backoff: float
    first_sent: float


class Outbox:
    def __init__(self, retry_s: float, max_s: float, limit: int):
        self.retry_s = retry_s
        self.max_s = max_s
        self.limit = limit
        self.entries: OrderedDict[str, _Entry] = OrderedDict()
        self.retries = 0
        self.acked = 0
        self.dropped = 0
        self.max_size = 0

    def add(self, env: Envelope, now: float) -> None:
        self.entries[env.msg_id] = _Entry(env, now + self.retry_s, self.retry_s, now)
        if len(self.entries) > self.limit:
            self.entries.popitem(last=False)
            self.dropped += 1
        self.max_size = max(self.max_size, len(self.entries))

    def ack(self, msg_id: str) -> bool:
        if self.entries.pop(msg_id, None) is None:
            return False  # duplicate ack
        self.acked += 1
        return True

    def due(self, now: float) -> list[Envelope]:
        out = []
        for e in self.entries.values():
            if e.next_try <= now:
                e.backoff = min(e.backoff * 2, self.max_s)
                e.next_try = now + e.backoff
                e.env.attempt += 1
                self.retries += 1
                out.append(e.env)
        return out

    def __len__(self) -> int:
        return len(self.entries)
