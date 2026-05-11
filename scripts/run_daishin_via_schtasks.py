"""SSH-trigger Daishin CYBOS fetcher via schtasks /it (interactive desktop session).

문제: SSH session 의 owner ≠ desktop session 의 owner (Windows session isolation).
CYBOS Plus 는 desktop session 에 logged in 이라 SSH 직접 호출 시 IsConnect=0.

해결: schtasks /create /it /ru owner 로 desktop logon session 안에서 task 실행 →
fetcher.py 가 desktop session 의 Plus 인스턴스 사용 가능.

흐름:
  1. fetcher.py + fetch.cmd 가 Win PC 에 있음 (없으면 upload).
  2. fetch_args.txt 작성 (이번 호출의 argv).
  3. schtasks /create /F /tn ks_fetcher /tr "C:\\ks_ws_export\\fetch.cmd" /sc once /st 23:59 /it /ru owner /rl HIGHEST
  4. schtasks /run /tn ks_fetcher
  5. fetch.status 폴링 → "FINISHED" 또는 "FAILED" 까지 (timeout 시 abort).
  6. fetch.log fetch + tail print.
  7. SFTP get bars/* + delete remote bars (already-fetched data 제거).

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.run_daishin_via_schtasks \\
        --timeframes 1m --days 30 --limit 5 --universe universe_top200.json
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path
from stat import S_ISDIR

import paramiko

import os as _os
WIN_IP = _os.environ.get("DAISHIN_WIN_IP", "172.30.1.38")
WIN_USER = _os.environ.get("DAISHIN_WIN_USER", "owner")
WIN_PWD = _os.environ.get("DAISHIN_WIN_PWD")  # required — read from .env or shell
if not WIN_PWD:
    raise SystemExit("ERROR: set DAISHIN_WIN_PWD environment variable")
WIN_BASE = "C:/ks_ws_export"
WIN_BARS = f"{WIN_BASE}/bars"
LOCAL_BARS = Path("data/bars")
LOCAL_LOG = Path("/tmp/daishin_fetch.log")

TASK_NAME = "ks_fetcher"


def _ssh() -> paramiko.SSHClient:
    s = paramiko.SSHClient()
    s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    s.connect(WIN_IP, username=WIN_USER, password=WIN_PWD, timeout=10)
    return s


def _exec(ssh: paramiko.SSHClient, cmd: str, timeout: int = 60) -> tuple[str, str, int]:
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("cp949", errors="replace")
    err = stderr.read().decode("cp949", errors="replace")
    rc = stdout.channel.recv_exit_status()
    return out, err, rc


def _sftp_put(ssh: paramiko.SSHClient, local: Path, remote: str) -> None:
    sftp = ssh.open_sftp()
    try:
        sftp.put(str(local), remote)
    finally:
        sftp.close()


def _sftp_get_text(ssh: paramiko.SSHClient, remote: str, max_bytes: int = 1_000_000) -> str:
    sftp = ssh.open_sftp()
    try:
        with sftp.open(remote, "rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except IOError:
        return ""
    finally:
        sftp.close()


def _walk_parquet(sftp: paramiko.SFTPClient, root: str):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sftp.listdir_attr(d)
        except IOError:
            continue
        for e in entries:
            full = f"{d}/{e.filename}"
            if S_ISDIR(e.st_mode):
                stack.append(full)
            elif e.filename.endswith(".parquet"):
                yield full, full[len(root) + 1:]


def _ensure_helpers(ssh: paramiko.SSHClient, *, force_upload: bool) -> None:
    """Make sure fetcher.py / worker.cmd are present on Windows."""
    sftp = ssh.open_sftp()
    try:
        targets = [
            ("/tmp/win_fetcher.py", f"{WIN_BASE}/fetcher.py"),
            ("/tmp/worker.cmd", f"{WIN_BASE}/worker.cmd"),
        ]
        for local, remote in targets:
            if not Path(local).exists():
                print(f"  WARN: local {local} missing — skipping upload of {remote}")
                continue
            if force_upload:
                sftp.put(local, remote)
                print(f"  uploaded {local} → {remote}")
            else:
                try:
                    sftp.stat(remote)
                except IOError:
                    sftp.put(local, remote)
                    print(f"  uploaded {local} → {remote} (was missing)")
    finally:
        sftp.close()


def _check_worker_running(ssh: paramiko.SSHClient) -> tuple[bool, str]:
    """Check worker.status — must show READY or recent DONE in desktop session."""
    status = _sftp_get_text(ssh, f"{WIN_BASE}/worker.status")
    if not status:
        return False, "worker.status missing — worker.cmd not started yet"
    if "session=" in status and "session=Console" in status:
        return True, status.strip()
    if "session=" in status:
        return True, status.strip()  # any session > no worker, but warn
    return False, status.strip()


def _write_args_file(ssh: paramiko.SSHClient, args_line: str) -> None:
    sftp = ssh.open_sftp()
    try:
        with sftp.open(f"{WIN_BASE}/fetch_args.txt", "w") as f:
            # Single-line args. Newline-terminated for set /p compatibility.
            f.write(args_line.rstrip() + "\r\n")
    finally:
        sftp.close()


def _file_exists(ssh: paramiko.SSHClient, remote: str) -> bool:
    sftp = ssh.open_sftp()
    try:
        sftp.stat(remote)
        return True
    except IOError:
        return False
    finally:
        sftp.close()


def _trigger_via_worker(ssh: paramiko.SSHClient, args_line: str, *, timeout_sec: int) -> tuple[bool, str]:
    """Lifecycle: write trigger.req → worker moves to active.req → worker
    deletes active.req when done. Phase-tracked polling (ignores stale DONE
    status from prior runs)."""
    sftp = ssh.open_sftp()
    try:
        try:
            sftp.remove(f"{WIN_BASE}/active.req")
        except IOError:
            pass
        try:
            sftp.remove(f"{WIN_BASE}/trigger.req")
        except IOError:
            pass
        # Binary write — ASCII bytes + LF only. CRLF caused `set /p` to leak \r.
        with sftp.open(f"{WIN_BASE}/trigger.req", "wb") as f:
            f.write((args_line.rstrip() + "\n").encode("ascii", errors="strict"))
    finally:
        sftp.close()
    print(f"  wrote trigger.req — waiting for worker pickup (~3s)")

    started = time.monotonic()
    phase = "pickup"  # pickup → running → done
    last_log_size = 0
    last_status = ""
    while True:
        elapsed = time.monotonic() - started
        if elapsed > timeout_sec:
            return False, f"timeout after {elapsed:.0f}s in phase={phase}, last status={last_status}"
        trigger_exists = _file_exists(ssh, f"{WIN_BASE}/trigger.req")
        active_exists = _file_exists(ssh, f"{WIN_BASE}/active.req")
        status = _sftp_get_text(ssh, f"{WIN_BASE}/worker.status").strip()
        if status != last_status:
            print(f"  [{elapsed:5.0f}s] phase={phase} status: {status[:120]}")
            last_status = status
        # Tail log
        log_text = _sftp_get_text(ssh, f"{WIN_BASE}/fetch.log", max_bytes=5_000_000)
        if len(log_text) > last_log_size:
            LOCAL_LOG.write_text(log_text, encoding="utf-8")
            last_log_size = len(log_text)
        if phase == "pickup":
            if not trigger_exists and active_exists:
                phase = "running"
                print(f"  [{elapsed:5.0f}s] worker picked up trigger.req → running")
            elif not trigger_exists and not active_exists:
                # Very fast — worker already finished
                phase = "done"
        if phase == "running" and not active_exists:
            phase = "done"
        if phase == "done":
            return True, status
        time.sleep(2)


def _sync_bars(ssh: paramiko.SSHClient) -> int:
    sftp = ssh.open_sftp()
    fetched = 0
    try:
        for remote, rel in _walk_parquet(sftp, WIN_BARS):
            local = LOCAL_BARS / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote, str(local))
            fetched += 1
    finally:
        sftp.close()
    # delete on Windows
    _exec(ssh, f'cmd /c del /S /Q "{WIN_BARS.replace("/", chr(92))}\\*.parquet"', timeout=60)
    return fetched


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--universe", default="universe_top200.json",
                   help="path on Windows side (relative to C:\\ks_ws_export)")
    p.add_argument("--timeframes", nargs="+", default=["1m"])
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--timeout", type=int, default=600,
                   help="max seconds to wait for task to finish")
    p.add_argument("--no-upload", action="store_true",
                   help="skip uploading fetcher.py / fetch.cmd (use existing on Win)")
    p.add_argument("--force-upload", action="store_true",
                   help="force re-upload of fetcher.py / fetch.cmd")
    args = p.parse_args()

    print(f"=== Daishin schtasks orchestrator ===")
    print(f"  universe={args.universe} timeframes={args.timeframes} days={args.days}")
    print(f"  limit={args.limit} offset={args.offset} timeout={args.timeout}s")

    # --log 안 줌: worker.cmd 가 stdout/stderr 를 fetch.log 로 redirect.
    # fetcher.py 의 --log 와 worker 의 redirect 가 동시에 같은 file 을 lock 하면
    # PermissionError 발생.
    args_line = (
        f"--universe {args.universe} "
        f"--timeframes {' '.join(args.timeframes)} "
        f"--days {args.days} "
        f"--out C:/ks_ws_export/bars"
    )
    if args.limit:
        args_line += f" --limit {args.limit}"
    if args.offset:
        args_line += f" --offset {args.offset}"

    ssh = _ssh()
    try:
        # 1) Upload helpers if requested
        if not args.no_upload:
            _ensure_helpers(ssh, force_upload=args.force_upload)
        # 2) Verify worker is running (poll daemon in desktop session)
        ok, status = _check_worker_running(ssh)
        if not ok:
            print(f"  ERROR: worker not running. status={status}")
            print("  → On Windows desktop, run: C:\\ks_ws_export\\worker.cmd")
            return 3
        print(f"  worker ready: {status[:120]}")
        # 3) Truncate previous log
        _exec(ssh, 'cmd /c del C:\\ks_ws_export\\fetch.log 2>nul')
        # 4) Trigger + wait
        ok, status = _trigger_via_worker(ssh, args_line, timeout_sec=args.timeout)
        print(f"  final status: {status}")
        # 5) Print log tail
        log_text = _sftp_get_text(ssh, f"{WIN_BASE}/fetch.log")
        if log_text:
            tail = log_text.strip().splitlines()[-60:]
            print("--- fetch.log tail ---")
            print("\n".join(tail))
            print("--- end log ---")
        if not ok:
            return 2
        # 6) Sync bars
        fetched = _sync_bars(ssh)
        print(f"  synced {fetched} parquet files → {LOCAL_BARS}")
    finally:
        ssh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
