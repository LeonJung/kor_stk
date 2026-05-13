"""coordinator — Phase 2.1 다중 worker 관리.

paper_trade_breakout 을 multiple worker process 로 띄워서 universe 분배 실행.
각 worker 가 죽으면 자동 재시작. 20:00 stop 시간 도달 시 모든 worker 일괄 종료.

cron 등록 시 install_cron.sh 의 paper_trade entry 대신 사용 가능:

    50 7 * * 1-5 cd /home/bpearson/ks_ws && \\
      PYTHONPATH=src .venv/bin/python -m scripts.coordinator --workers 2 \\
      >> data/reports/coord_$(date +\\%Y\\%m\\%d).log 2>&1

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.coordinator --workers 1  # 단일 (default)
    PYTHONPATH=src .venv/bin/python -m scripts.coordinator --workers 2  # 다중
    PYTHONPATH=src .venv/bin/python -m scripts.coordinator --workers 3 --stop-kst 20:00
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path

_REPO = Path(__file__).parent.parent
_KST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s coordinator %(message)s",
)
log = logging.getLogger("coordinator")


def _spawn_worker(worker_id: int, total: int, log_dir: Path) -> subprocess.Popen:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"worker_{worker_id}_{datetime.now(_KST).strftime('%Y%m%d')}.log"
    env = {"PYTHONPATH": str(_REPO / "src"), **os.environ}
    cmd = [
        sys.executable, "-m", "scripts.paper_trade_breakout",
        "--worker-id", str(worker_id), "--total-workers", str(total),
    ]
    log.info("spawning worker %d/%d (log=%s)", worker_id, total, log_path.name)
    return subprocess.Popen(
        cmd, cwd=_REPO, env=env,
        stdout=open(log_path, "a", buffering=1),
        stderr=subprocess.STDOUT,
    )


def _parse_stop_kst(s: str) -> dt_time:
    h, m = s.split(":")
    return dt_time(int(h), int(m))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=1,
                   help="동시 worker 개수 (default 1 = single)")
    p.add_argument("--stop-kst", default="20:00",
                   help="자동 종료 시각 KST (HH:MM, default 20:00)")
    p.add_argument("--check-interval", type=int, default=30,
                   help="worker 상태 polling 주기 sec (default 30)")
    p.add_argument("--log-dir", default="data/reports/workers")
    args = p.parse_args()

    if args.workers < 1:
        log.error("workers must be >= 1")
        return 1

    stop_kst = _parse_stop_kst(args.stop_kst)
    log_dir = Path(args.log_dir)

    log.info("coordinator start: workers=%d stop_kst=%s check_interval=%ds",
             args.workers, stop_kst, args.check_interval)

    workers: list[subprocess.Popen | None] = [None] * args.workers
    for i in range(args.workers):
        workers[i] = _spawn_worker(i, args.workers, log_dir)

    shutdown = False

    def _stop_all(*_):
        nonlocal shutdown
        log.warning("signal received — stopping all workers")
        shutdown = True
        for i, w in enumerate(workers):
            if w is not None and w.poll() is None:
                log.info("terminating worker %d (pid=%d)", i, w.pid)
                w.terminate()

    signal.signal(signal.SIGINT, _stop_all)
    signal.signal(signal.SIGTERM, _stop_all)

    try:
        while not shutdown:
            now_kst = datetime.now(UTC).astimezone(_KST)
            if now_kst.time() >= stop_kst:
                log.info("stop_kst (%s) reached, terminating workers", stop_kst)
                _stop_all()
                break

            # poll each worker
            for i, w in enumerate(workers):
                if w is None:
                    continue
                rc = w.poll()
                if rc is None:
                    continue
                log.warning("worker %d exited rc=%s — restarting", i, rc)
                workers[i] = _spawn_worker(i, args.workers, log_dir)

            time.sleep(args.check_interval)
    finally:
        # final wait for graceful shutdown
        for i, w in enumerate(workers):
            if w is not None and w.poll() is None:
                try:
                    w.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    log.warning("worker %d kill -9", i)
                    w.kill()
        log.info("coordinator exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
