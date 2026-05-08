"""Run the Scheduler with three sample tasks for a short window so the
interval and one-shot semantics are visible.

Run:
    uv run examples/scheduler_demo.py [DURATION_SEC]
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from ks_ws.scheduler import Scheduler

_KST = timezone(timedelta(hours=9))


async def run(duration_sec: int) -> None:
    fired_immediate = []
    fired_interval = []
    fired_async = []

    def immediate_job() -> None:
        fired_immediate.append(datetime.now(_KST).strftime("%H:%M:%S"))
        print(f"  [immediate] fire at {fired_immediate[-1]}")

    def interval_job() -> None:
        fired_interval.append(datetime.now(_KST).strftime("%H:%M:%S"))
        print(f"  [every 1s]  fire at {fired_interval[-1]}")

    async def async_job() -> None:
        fired_async.append(datetime.now(_KST).strftime("%H:%M:%S"))
        print(f"  [async]     fire at {fired_async[-1]}")

    sched = Scheduler()
    sched.every(seconds=10, name="immediate", job=immediate_job, run_immediately=True)
    sched.every(seconds=1, name="interval", job=interval_job)
    sched.every(seconds=2, name="async", job=async_job)
    # daily_at: schedule a far-future task to demonstrate registration without
    # actually firing during the demo.
    sched.daily_at(
        3,
        30,
        name="far_future",
        job=lambda: print("  [daily]     would fire at 03:30 KST"),
    )

    print(f"=== Scheduler demo ({duration_sec}s) ===\n")
    await sched.start()
    try:
        await asyncio.sleep(duration_sec)
    finally:
        await sched.stop()

    print("\n=== Summary ===")
    for s in sched.schedules:
        spec = (
            f"every {s.interval_sec}s"
            if s.interval_sec
            else f"daily at {s.daily_hour:02d}:{s.daily_minute:02d}"
        )
        print(f"  {s.name:<14} ({spec:<22}) ran {s.run_count} times, errors {s.error_count}")


def main() -> None:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    asyncio.run(run(duration))


if __name__ == "__main__":
    main()
