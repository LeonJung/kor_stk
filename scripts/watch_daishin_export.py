"""SFTP watcher: Windows fetcher 가 만든 parquet 을 즉시 Linux 로 sync + Windows 삭제.

배경: SSH non-interactive session 에서 CYBOS COM 접근 불가 (Windows session
격리). 사용자가 Windows GUI cmd 에서 fetcher 직접 실행, 우리는 SFTP 로
결과 watching + sync + 삭제로 16GB 디스크 안전 보장.

동작:
- 매 INTERVAL 초마다 SFTP 로 C:/ks_ws_export/bars/**/*.parquet 검색
- 새 파일 있으면 Linux data/bars/{tf}/{sym}/{year}.parquet 으로 download
- download 성공 시 Windows 측 즉시 삭제
- IDLE_LIMIT 초 동안 새 파일 없으면 종료

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.watch_daishin_export \
        --interval 5 --idle-limit 120
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from stat import S_ISDIR

import paramiko

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watch_daishin")

import os as _os
WIN_IP = _os.environ.get("DAISHIN_WIN_IP", "172.30.1.38")
WIN_USER = _os.environ.get("DAISHIN_WIN_USER", "owner")
WIN_PASSWORD = _os.environ.get("DAISHIN_WIN_PWD")
if not WIN_PASSWORD:
    raise SystemExit("ERROR: set DAISHIN_WIN_PWD environment variable")
WIN_BARS_DIR = "C:/ks_ws_export/bars"


def _connect() -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(WIN_IP, username=WIN_USER, password=WIN_PASSWORD, timeout=10)
    return ssh


def _sftp_walk_parquet(sftp: paramiko.SFTPClient, root: str):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sftp.listdir_attr(d)
        except IOError:
            continue
        for entry in entries:
            full = f"{d}/{entry.filename}"
            if S_ISDIR(entry.st_mode):
                stack.append(full)
            elif full.lower().endswith(".parquet"):
                yield full, full[len(root) + 1:], entry.st_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=5,
                        help="poll seconds between sftp sweeps")
    parser.add_argument("--idle-limit", type=int, default=300,
                        help="exit after N seconds of no new files")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--keep-windows", action="store_true",
                        help="do NOT delete on Windows after sync (debug)")
    args = parser.parse_args()

    log.info("Connecting to %s ...", WIN_IP)
    ssh = _connect()
    sftp = ssh.open_sftp()
    log.info("Watching %s every %ds (idle-limit=%ds)", WIN_BARS_DIR, args.interval, args.idle_limit)
    local_dir = Path(args.data_dir)
    last_seen = time.monotonic()
    total_synced = 0
    total_bytes = 0

    try:
        while True:
            new_files = list(_sftp_walk_parquet(sftp, WIN_BARS_DIR))
            if not new_files:
                if time.monotonic() - last_seen > args.idle_limit:
                    log.info("Idle for %ds — exiting", args.idle_limit)
                    break
                time.sleep(args.interval)
                continue
            last_seen = time.monotonic()
            for remote_path, rel, size in new_files:
                local_path = local_dir / "bars" / rel
                local_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    sftp.get(remote_path, str(local_path))
                except Exception as e:
                    log.warning("get %s failed: %s", remote_path, e)
                    continue
                total_synced += 1
                total_bytes += size
                if not args.keep_windows:
                    try:
                        sftp.remove(remote_path)
                    except Exception as e:
                        log.warning("remove %s failed: %s", remote_path, e)
            log.info("synced %d new files (cumulative %d, %.1f MB)",
                     len(new_files), total_synced, total_bytes / 1024 / 1024)
    finally:
        sftp.close()
        ssh.close()
    log.info("Watcher stopped. total %d files, %.1f MB", total_synced, total_bytes / 1024 / 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
