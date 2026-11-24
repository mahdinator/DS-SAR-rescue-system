"""Bounded set of message ids already processed (duplicate suppression)."""
from __future__ import annotations

from collections import deque


class SeenSet:
    def __init__(self, capacity: int = 50000):
        self.capacity = capacity
        self._set: set[str] = set()
        self._order: deque[str] = deque()

    def add(self, msg_id: str) -> bool:
        """Record msg_id. Returns True if it was new."""
        if msg_id in self._set:
            return False
        self._set.add(msg_id)
        self._order.append(msg_id)
        if len(self._order) > self.capacity:
            self._set.discard(self._order.popleft())
        return True

    def __contains__(self, msg_id: str) -> bool:
        return msg_id in self._set
