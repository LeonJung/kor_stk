import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ks_ws.scheduler import Scheduler, _next_daily


def test_register_every_validates_interval():
    sched = Scheduler()
    with pytest.raises(ValueError):
        sched.every(seconds=0, name="bad", job=lambda: None)


def test_register_daily_validates_hours():
    sched = Scheduler()
    with pytest.raises(ValueError):
        sched.daily_at(24, 0, name="bad", job=lambda: None)
    with pytest.raises(ValueError):
        sched.daily_at(12, 60, name="bad", job=lambda: None)


def test_next_daily_today_when_future():
    kst = timezone(timedelta(hours=9))
    now = datetime(2026, 5, 8, 9, 0, tzinfo=kst)
    target = _next_daily(now, 18, 0)
    assert target == datetime(2026, 5, 8, 18, 0, tzinfo=kst)


def test_next_daily_tomorrow_when_past():
    kst = timezone(timedelta(hours=9))
    now = datetime(2026, 5, 8, 19, 0, tzinfo=kst)  # 18:00 already past
    target = _next_daily(now, 18, 0)
    assert target == datetime(2026, 5, 9, 18, 0, tzinfo=kst)


def test_every_fires_repeatedly():
    fired = []

    def job():
        fired.append(1)

    async def run():
        sched = Scheduler()
        sched.every(seconds=0.05, name="tick", job=job)
        await sched.start()
        await asyncio.sleep(0.18)  # ~3 fires expected
        await sched.stop()

    asyncio.run(run())
    assert len(fired) >= 2  # conservative — scheduling jitter


def test_every_run_immediately_fires_at_start():
    fired = []

    def job():
        fired.append(datetime.now())

    async def run():
        sched = Scheduler()
        sched.every(seconds=10, name="immediate", job=job, run_immediately=True)
        await sched.start()
        await asyncio.sleep(0.05)
        await sched.stop()

    asyncio.run(run())
    assert len(fired) == 1


def test_async_job_supported():
    fired = []

    async def job():
        fired.append(1)

    async def run():
        sched = Scheduler()
        sched.every(seconds=0.05, name="async", job=job, run_immediately=True)
        await sched.start()
        await asyncio.sleep(0.02)
        await sched.stop()

    asyncio.run(run())
    assert len(fired) >= 1


def test_job_exception_does_not_kill_loop():
    fired = []

    def bad():
        raise RuntimeError("boom")

    def good():
        fired.append(1)

    async def run():
        sched = Scheduler()
        sched.every(seconds=0.04, name="bad", job=bad, run_immediately=True)
        sched.every(seconds=0.04, name="good", job=good, run_immediately=True)
        await sched.start()
        await asyncio.sleep(0.15)
        await sched.stop()
        return sched.schedules

    schedules = asyncio.run(run())
    assert len(fired) >= 1
    bad_sched = next(s for s in schedules if s.name == "bad")
    assert bad_sched.error_count >= 1
    assert bad_sched.run_count >= 1


def test_stop_cancels_pending_jobs():
    """daily_at far in the future — stop() should cancel without hanging."""

    async def run():
        sched = Scheduler()
        sched.daily_at(3, 0, name="late", job=lambda: None)  # likely tomorrow 03:00
        await sched.start()
        await asyncio.sleep(0.01)
        await sched.stop()
        return sched.running

    assert asyncio.run(run()) is False


def test_run_count_tracks_executions():
    fired = []

    def job():
        fired.append(1)

    async def run():
        sched = Scheduler()
        sched.every(seconds=0.05, name="counted", job=job, run_immediately=True)
        await sched.start()
        await asyncio.sleep(0.18)
        await sched.stop()
        return sched.schedules[0].run_count

    count = asyncio.run(run())
    assert count >= 2


def test_start_idempotent_and_stop_idempotent():
    async def run():
        sched = Scheduler()
        sched.every(seconds=10, name="x", job=lambda: None)
        await sched.start()
        await sched.start()  # no-op
        running_after_double = sched.running
        await sched.stop()
        await sched.stop()
        return running_after_double, sched.running

    after_double, after_stop = asyncio.run(run())
    assert after_double is True
    assert after_stop is False
