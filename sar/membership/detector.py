"""Timeout failure detector.

Any message from a node (direct or relayed) counts as a sign of life. A node not
heard from for `timeout_s` real seconds is suspected; hearing it again clears the
suspicion. Silence cannot tell a crash from a lost link, so this only ever
says "suspected", never "dead".
"""
from __future__ import annotations

import time
from typing import Callable


class TimeoutFailureDetector:
    def __init__(self, timeout_s: float, clock: Callable[[], float] = time.monotonic):
        self.timeout_s = timeout_s
        self.clock = clock
        self.last: dict[str, float] = {}
        self.suspected: set[str] = set()

    def watch(self, node: str) -> None:
        """Start tracking a node before hearing from it (grace period = timeout)."""
        self.last.setdefault(node, self.clock())

    def heard(self, node: str) -> None:
        self.last[node] = self.clock()

    def check(self) -> list[tuple[str, str]]:
        """Return state changes since the last check: (node, "suspected" | "recovered")."""
        now = self.clock()
        changes = []
        for node, t in self.last.items():
            late = now - t > self.timeout_s
            if late and node not in self.suspected:
                self.suspected.add(node)
                changes.append((node, "suspected"))
            elif not late and node in self.suspected:
                self.suspected.discard(node)
                changes.append((node, "recovered"))
        return changes

    def is_suspected(self, node: str) -> bool:
        return node in self.suspected
