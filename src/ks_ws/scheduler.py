"""Lightweight async scheduler for recurring operational tasks.

Two flavors of schedule:

- ``every(seconds=N)`` — interval-based, fires every N seconds while the
  scheduler is running. The first fire happens after the first interval
  unless ``run_immediately=True``.
- ``daily_at(hour, minute, tz='Asia/Seoul')`` — wall-clock cron-like,
  fires once per calendar day at the given local time.

Each registered task is wrapped in its own asyncio.Task so a slow or
failing job doesn't block the others. Exceptions inside a job are
caught and logged — the loop continues.

Usage::

    sched = Scheduler()
    sched.every(seconds=60, name='token_refresh', job=refresh_token)
    sched.daily_at(18, 0, name='cold_batch', job=run_cold_batch)
    await sched.start()
    ...
    await sched.stop()

Jobs may be sync or async callables that take no arguments.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger("ks_ws.scheduler")

_DEFAULT_TZ = timezone(timedelta(hours=9))  # KST

JobFn = Callable[[], Awaitable[None] | None]


@dataclass
class _Schedule:
    name: str
    job: JobFn
    interval_sec: float | None = None
    daily_hour: int | None = None
    daily_minute: int | None = None
    tz: timezone = field(default=_DEFAULT_TZ)
    run_immediately: bool = False
    run_count: int = 0
    error_count: int = 0


def _next_daily(now: datetime, hour: int, minute: int) -> datetime:
    """Return the next datetime in `now`'s tz that matches hour:minute.
    If today's slot has passed, returns tomorrow's."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class Scheduler:
    def __init__(self) -> None:
        self._schedules: list[_Schedule] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def schedules(self) -> list[_Schedule]:
        return list(self._schedules)

    def every(
        self,
        seconds: float,
        *,
        name: str,
        job: JobFn,
        run_immediately: bool = False,
    ) -> None:
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        self._schedules.append(
            _Schedule(
                name=name,
                job=job,
                interval_sec=seconds,
                run_immediately=run_immediately,
            )
        )

    def daily_at(
        self,
        hour: int,
        minute: int = 0,
        *,
        name: str,
        job: JobFn,
        tz: timezone | None = None,
    ) -> None:
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("hour ∈ [0, 24) and minute ∈ [0, 60)")
        self._schedules.append(
            _Schedule(
                name=name,
                job=job,
                daily_hour=hour,
                daily_minute=minute,
                tz=tz or _DEFAULT_TZ,
            )
        )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [asyncio.create_task(self._run_schedule(s)) for s in self._schedules]

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _run_schedule(self, schedule: _Schedule) -> None:
        try:
            if schedule.interval_sec is not None:
                if schedule.run_immediately:
                    await self._fire(schedule)
                while True:
                    await asyncio.sleep(schedule.interval_sec)
                    await self._fire(schedule)
            else:
                assert schedule.daily_hour is not None and schedule.daily_minute is not None
                while True:
                    now = datetime.now(schedule.tz)
                    next_run = _next_daily(now, schedule.daily_hour, schedule.daily_minute)
                    delta = (next_run - now).total_seconds()
                    await asyncio.sleep(max(0.0, delta))
                    await self._fire(schedule)
        except asyncio.CancelledError:
            pass

    async def _fire(self, schedule: _Schedule) -> None:
        schedule.run_count += 1
        try:
            result = schedule.job()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            schedule.error_count += 1
            log.exception("scheduled job %s raised", schedule.name)
