"""Pausable mission clock: every process applies the same timeline."""
import asyncio
import time

from sar.common.clock import MissionClock


def test_pause_freezes_mission_time_and_resume_continues_from_there():
    c = MissionClock(time.time() - 10, 20)  # 10 real s in at x20: about 200 mission s
    tl = c.set_speed(0)
    frozen = c.now()
    time.sleep(0.05)
    assert c.now() == frozen and c.paused
    c.set_speed(20)
    assert abs(c.now() - frozen) < 2


def test_another_process_applying_the_timeline_agrees():
    a = MissionClock(time.time() - 5, 25)
    b = MissionClock(a.mission_start, 25)
    b.apply(a.set_speed(100))
    assert abs(a.now() - b.now()) < 0.5 and b.speedup == 100


def test_mission_sleep_waits_out_a_pause():
    c = MissionClock(time.time(), 100)

    async def run():
        t0 = time.monotonic()
        c.set_speed(0)
        asyncio.get_running_loop().call_later(0.3, c.set_speed, 100)
        await c.sleep(10)  # 10 mission s = 0.1 real s at x100, but paused for 0.3 s first
        return time.monotonic() - t0

    assert asyncio.run(run()) >= 0.3
