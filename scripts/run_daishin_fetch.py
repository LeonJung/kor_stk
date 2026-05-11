"""Driver: orchestrate Daishin CYBOS Plus fetch on Windows + sync to Linux + delete on Windows.

배경: Windows PC 의 디스크 16GB 만 남음 (사용자 알림 2026-05-10) → batch
단위로 fetch 후 즉시 Linux 로 SCP get + Windows 측 parquet 삭제.

흐름 (per batch):
1. SSH execute: ``py -3.10 fetcher.py --offset N --limit BATCH ...``
2. SFTP get: ``C:/ks_ws_export/bars/**/*.parquet`` → ``data/bars/{tf}/{sym}/{year}.parquet``
   (BarStore 와 같은 layout — 즉시 ks_ws 가 read 가능)
3. SSH execute: ``del /S /Q C:\\ks_ws_export\\bars\\*.parquet``
4. 다음 batch

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.run_daishin_fetch \
        --batch 10 --days 90 --timeframes 1m 1d --total 200
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import paramiko

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_daishin_fetch")

import os as _os
WIN_IP = _os.environ.get("DAISHIN_WIN_IP", "172.30.1.38")
WIN_USER = _os.environ.get("DAISHIN_WIN_USER", "owner")
WIN_PASSWORD = _os.environ.get("DAISHIN_WIN_PWD")
if not WIN_PASSWORD:
    raise SystemExit("ERROR: set DAISHIN_WIN_PWD environment variable")
WIN_REMOTE_BASE = "C:/ks_ws_export"
WIN_BARS_DIR = "C:/ks_ws_export/bars"


def _connect_ssh() -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(WIN_IP, username=WIN_USER, password=WIN_PASSWORD, timeout=10)
    return ssh


def _exec(ssh: paramiko.SSHClient, cmd: str, *, timeout: int = 600) -> tuple[str, str]:
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return out, err


def _sftp_walk_parquet(sftp: paramiko.SFTPClient, remote_root: str):
    """Yield (remote_path, rel_path_under_root) for all *.parquet under remote_root."""
    stack = [remote_root]
    while stack:
        d = stack.pop()
        try:
            entries = sftp.listdir_attr(d)
        except IOError:
            continue
        for entry in entries:
            full = f"{d}/{entry.filename}"
            mode = entry.st_mode
            from stat import S_ISDIR
            if S_ISDIR(mode):
                stack.append(full)
            elif full.lower().endswith(".parquet"):
                yield full, full[len(remote_root) + 1:]


def sync_and_clean_batch(ssh: paramiko.SSHClient, local_data_dir: Path) -> int:
    """SFTP get all parquet under WIN_BARS_DIR → local_data_dir, then delete on Windows."""
    sftp = ssh.open_sftp()
    fetched = 0
    try:
        for remote_path, rel in _sftp_walk_parquet(sftp, WIN_BARS_DIR):
            local_path = local_data_dir / "bars" / rel
            local_path.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_path, str(local_path))
            fetched += 1
    finally:
        sftp.close()
    # Delete on Windows side (recursive parquet delete)
    out, err = _exec(ssh, f'cmd /c del /S /Q "{WIN_BARS_DIR.replace("/", chr(92))}\\*.parquet"', timeout=60)
    return fetched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=10, help="symbols per fetch batch")
    parser.add_argument("--total", type=int, default=200, help="total symbols (top N market cap)")
    parser.add_argument("--offset", type=int, default=0, help="start offset")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--timeframes", nargs="+", default=["1m", "1d"])
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    log.info("Connecting Windows %s ...", WIN_IP)
    ssh = _connect_ssh()
    log.info("Connected.")

    # CYBOS connection check via fetcher in dry mode? Try a 1-symbol probe.
    out, err = _exec(
        ssh,
        f'cd /d C:\\ks_ws_export && py -3.10 -c "import win32com.client; cp=win32com.client.Dispatch(\'CpUtil.CpCybos\'); print(\'connected=\', cp.IsConnect)"',
        timeout=30,
    )
    if "connected= 1" not in out:
        log.error("CYBOS Plus not connected. Open CYBOS 5 GUI and log in first.")
        log.error("  ssh probe stdout: %s", out.strip()[:300])
        ssh.close()
        return 2
    log.info("CYBOS Plus connected.")

    local_dir = Path(args.data_dir)
    n_done = 0
    started = time.monotonic()
    offset = args.offset
    while offset < args.total:
        batch_size = min(args.batch, args.total - offset)
        log.info("=== Batch offset=%d size=%d (total %d/%d) ===",
                 offset, batch_size, n_done, args.total)
        # Run fetcher
        tf_args = " ".join(args.timeframes)
        cmd = (
            f'cd /d C:\\ks_ws_export && py -3.10 fetcher.py '
            f'--universe universe_top200.json --timeframes {tf_args} '
            f'--days {args.days} --offset {offset} --limit {batch_size}'
        )
        out, err = _exec(ssh, cmd, timeout=1200)
        # Strip non-essential progress; keep tail
        for line in out.splitlines()[-5:]:
            log.info("  fetcher: %s", line.strip())
        if err.strip():
            log.warning("  fetcher stderr: %s", err.strip()[:200])
        # Sync + clean
        synced = sync_and_clean_batch(ssh, local_dir)
        log.info("  synced %d parquet files; Windows side cleaned", synced)
        n_done += batch_size
        offset += batch_size
        elapsed = time.monotonic() - started
        rate = n_done / max(elapsed, 0.01)
        eta = (args.total - n_done) / max(rate, 0.001)
        log.info("  progress: %d/%d  rate=%.2f sym/s  eta=%.0fmin",
                 n_done, args.total, rate, eta / 60)

    ssh.close()
    log.info("All batches done. Total %d symbols.", n_done)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
