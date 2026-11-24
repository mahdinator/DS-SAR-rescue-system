"""Mission time and logical time.

Mission time is simulated seconds since the mission started. It runs on a
timeline: from wall-clock moment `base_real` on, mission time is `base_mission`
plus elapsed wall time times `speed`. The control center can pause (speed 0) or
change the speed: the launcher publishes a new timeline on the control channel
and every process applies it (MissionClock.apply), so all agree on mission time.

Nothing correct depends on mission time; it drives movement, battery, review
delays, leases and scripted faults. The network protocols (heartbeats, failure
detection, elections, retries) run in real time, as they would on real hardware,
so they keep working while the mission is paused. Ordering of events uses
Lamport clocks and the consensus log.
"""
from __future__ import annotations

import asyncio
import time


class MissionClock:
    def __init__(self, mission_start: float, speedup: float):
        self.mission_start = mission_start
        self.nominal = float(speedup)  # the scenario's speed, used to convert when paused
        self.base_real = mission_start
        self.base_mission = 0.0
        self.speed = float(speedup)

    @property
    def speedup(self) -> float:
        """Current mission seconds per real second (0 while paused)."""
        return self.speed

    @property
    def paused(self) -> bool:
        return self.speed == 0

    def now(self) -> float:
        """Mission seconds (simulated). Negative before the mission starts."""
        return self.base_mission + (time.time() - self.base_real) * self.speed

    def timeline(self) -> dict:
        return {"base_real": self.base_real, "base_mission": self.base_mission, "speed": self.speed}

    def set_speed(self, speed: float) -> dict:
        """Change speed from now on (0 = pause). Returns the new timeline to publish."""
        now_real = time.time()
        if now_real > self.base_real:  # before the mission starts, keep counting down to it
            self.base_mission = self.now()
            self.base_real = now_real
        self.speed = max(0.0, float(speed))
        return self.timeline()

    def apply(self, tl: dict) -> None:
        self.base_real = float(tl["base_real"])
        self.base_mission = float(tl["base_mission"])
        self.speed = float(tl["speed"])

    def to_real(self, mission_seconds: float) -> float:
        return mission_seconds / (self.speed or self.nominal)

    async def sleep(self, mission_seconds: float) -> None:
        """Sleep for mission time: waits longer while paused, less after a speed-up."""
        target = self.now() + mission_seconds
        while True:
            left = target - self.now()
            if left <= 0:
                return
            await asyncio.sleep(min(0.25, left / self.speed) if self.speed else 0.1)

    async def wait_start(self) -> None:
        delay = self.mission_start - time.time()
        if delay > 0:
            await asyncio.sleep(delay)


class LamportClock:
    def __init__(self) -> None:
        self.value = 0

    def tick(self) -> int:
        """Advance for a local event or a send."""
        self.value += 1
        return self.value

    def observe(self, received: int) -> int:
        """Merge a received timestamp."""
        self.value = max(self.value, received) + 1
        return self.value
