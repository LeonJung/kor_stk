"""run_daishin_worker — worker.cmd 기반 batch fetch orchestrator.

worker.cmd (C:\\ks_ws_export\\worker.cmd) 가 Console session 안에서 polling 중일 때,
이 script 가 Linux 에서:

1. universe 를 batch 로 분할 (Windows disk 16GB 제약 → 종목 batch 단위 fetch)
2. batch 마다:
    a. universe_batch.json + trigger.req SFTP put
    b. worker.status 폴링 (RUNNING → DONE)
    c. SFTP get C:/ks_ws_export/bars/<tf>/* → data/bars/<tf>/
    d. SSH del C:\\ks_ws_export\\bars\\*\\*\\*.parquet (Windows disk 정리)
    e. 결과 verify (BarStore read + 거래일/분봉 수 체크)
    f. 실패 종목은 retry queue 로
3. 진행 상황 markdown 로 저장 (resume 가능, progress.json 누적)

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.run_daishin_worker \\
        --universe /home/bpearson/ks_ws/data/daishin_fetch_plan/universe_all.json \\
        --timeframes 1d \\
        --days 2000 \\
        --batch-size 200 \\
        --progress /home/bpearson/ks_ws/data/daishin_fetch_plan/progress_1d.json

run_in_background 권고 — 일봉 ~30분-1시간, 분봉 ~3-4일.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import paramiko

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("run_daishin_worker")

WIN_IP = os.environ.get("DAISHIN_WIN_IP", "172.30.1.38")
WIN_USER = os.environ.get("DAISHIN_WIN_USER", "owner")
WIN_PWD = os.environ.get("DAISHIN_WIN_PWD", "123123")
WIN_BASE = "C:/ks_ws_export"
WIN_BARS = f"{WIN_BASE}/bars"


def _ssh() -> paramiko.SSHClient:
    s = paramiko.SSHClient()
    s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    s.connect(WIN_IP, username=WIN_USER, password=WIN_PWD, timeout=15)
    return s


def _exec(ssh: paramiko.SSHClient, cmd: str, timeout: int = 60) -> tuple[str, int]:
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("cp949", errors="replace")
    err = stderr.read().decode("cp949", errors="replace")
    rc = stdout.channel.recv_exit_status()
    return out + err, rc


def _put_trigger(
    sftp: paramiko.SFTPClient, universe_file: str, args_extra: str,
) -> None:
    args = f"--universe {universe_file} {args_extra}".strip()
    tmp = "/tmp/trigger.req.tmp"
    with open(tmp, "w") as f:
        f.write(args + "\n")
    sftp.put(tmp, f"{WIN_BASE}/trigger.req")
    log.info("trigger.req sent: %s", args)


def _wait_done(
    ssh: paramiko.SSHClient, *, poll_sec: int = 30, max_wait_sec: int = 14400,
    log_path: str = f"{WIN_BASE}/fetch.log",
) -> tuple[str, int]:
    """Poll worker.status. Returns (status_text, rc)."""
    started = time.time()
    last_log_line = ""
    while time.time() - started < max_wait_sec:
        time.sleep(poll_sec)
        out, _ = _exec(ssh, f'type "{WIN_BASE.replace("/", chr(92))}\\worker.status"')
        status = out.strip()
        log_out, _ = _exec(
            ssh,
            f'powershell -c "Get-Content {log_path} -Tail 1 2>$null"',
        )
        log_line = log_out.strip()
        elapsed = int(time.time() - started)
        if log_line != last_log_line:
            log.info("[%ds] %s | %s", elapsed, status[:60], log_line[:140])
            last_log_line = log_line
        if "DONE" in status:
            rc = 0
            if "rc=" in status:
                try:
                    rc = int(status.split("rc=")[1].split()[0])
                except (ValueError, IndexError):
                    rc = 99
            return status, rc
        if "FAILED" in status:
            return status, 1
    return "TIMEOUT", -1


def _sftp_get_recursive(
    sftp: paramiko.SFTPClient, remote_dir: str, local_dir: Path,
) -> int:
    """Recursive SFTP get. Returns file count."""
    from stat import S_ISDIR
    local_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        entries = sftp.listdir_attr(remote_dir)
    except IOError:
        return 0
    for e in entries:
        rpath = f"{remote_dir}/{e.filename}"
        lpath = local_dir / e.filename
        if S_ISDIR(e.st_mode):
            count += _sftp_get_recursive(sftp, rpath, lpath)
        else:
            sftp.get(rpath, str(lpath))
            count += 1
    return count


def _verify_batch(
    timeframes: list[str], local_data_dir: Path, codes: list[str], days: int,
) -> dict[str, dict]:
    """Per-symbol verify after fetch — bar count vs expected. Returns
    {symbol: {tf: {'count': N, 'enough': bool}}}."""
    import duckdb
    con = duckdb.connect(":memory:")
    out: dict[str, dict] = {}
    expected_1d = days
    expected_1m = days * 390  # rough
    for sym in codes:
        out[sym] = {}
        for tf in timeframes:
            pattern = f"{local_data_dir}/bars/{tf}/{sym}/*.parquet"
            try:
                r = con.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{pattern}')"
                ).fetchone()
                n = r[0]
            except Exception:
                n = 0
            expected = expected_1d if tf == "1d" else expected_1m
            out[sym][tf] = {
                "count": n,
                "expected_min": int(expected * 0.5),  # 거래일 / 분봉 결손 일부 허용
                "enough": n >= expected * 0.5,
            }
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--universe", required=True,
                   help="JSON list of symbols (e.g. universe_all.json)")
    p.add_argument("--timeframes", nargs="+", default=["1d"],
                   help="1d / 1m")
    p.add_argument("--days", type=int, default=2000)
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--progress", required=True,
                   help="JSON progress file (resume 용)")
    p.add_argument("--log-file", default=None,
                   help="Windows fetch.log path (default fetch.log)")
    p.add_argument("--max-wait-sec", type=int, default=14400,
                   help="Per-batch worker wait max (default 4h)")
    p.add_argument("--data-dir", default="/home/bpearson/ks_ws/data")
    args = p.parse_args()

    universe_path = Path(args.universe)
    codes = json.loads(universe_path.read_text())
    log.info("universe loaded: %d symbols from %s", len(codes), universe_path)

    progress_path = Path(args.progress)
    progress: dict = {"done_codes": [], "failed_codes": []}
    if progress_path.exists():
        progress = json.loads(progress_path.read_text())
        log.info(
            "resume: %d done, %d failed",
            len(progress.get("done_codes", [])),
            len(progress.get("failed_codes", [])),
        )

    done_set = set(progress.get("done_codes", []))
    remaining = [c for c in codes if c not in done_set]
    log.info("remaining: %d / %d", len(remaining), len(codes))

    if not remaining:
        log.info("all done.")
        return 0

    ssh = _ssh()
    sftp = ssh.open_sftp()
    log_file_arg = (
        f"--log {args.log_file}" if args.log_file
        else f"--log {WIN_BASE}/fetch_{int(time.time())}.log"
    )

    n_batches = (len(remaining) + args.batch_size - 1) // args.batch_size
    total_files = 0
    for b_idx in range(n_batches):
        start = b_idx * args.batch_size
        batch_codes = remaining[start : start + args.batch_size]
        log.info(
            "=== batch %d/%d — %d symbols ===",
            b_idx + 1, n_batches, len(batch_codes),
        )

        # 1) universe batch json upload
        batch_json_local = "/tmp/universe_batch.json"
        with open(batch_json_local, "w") as f:
            json.dump(batch_codes, f)
        batch_json_remote_name = "universe_batch.json"
        sftp.put(batch_json_local, f"{WIN_BASE}/{batch_json_remote_name}")

        # 2) trigger.req
        tf_arg = " ".join(args.timeframes)
        extra = (
            f"--timeframes {tf_arg} --days {args.days} "
            f"--out {WIN_BARS} {log_file_arg}"
        )
        _put_trigger(sftp, batch_json_remote_name, extra)

        # 3) wait done
        log_path = args.log_file or log_file_arg.split()[-1]
        status, rc = _wait_done(
            ssh, max_wait_sec=args.max_wait_sec, log_path=log_path,
        )
        log.info("batch %d status=%s rc=%d", b_idx + 1, status[:80], rc)

        if rc != 0:
            log.warning("batch %d failed (rc=%d). 종목 retry queue 로", b_idx + 1, rc)
            progress.setdefault("failed_codes", []).extend(batch_codes)
            progress_path.write_text(json.dumps(progress, indent=2))
            continue

        # 4) SFTP get parquet → Linux
        local_data = Path(args.data_dir)
        for tf in args.timeframes:
            count = _sftp_get_recursive(
                sftp, f"{WIN_BARS}/{tf}", local_data / "bars" / tf,
            )
            log.info("  SFTP get %s: %d files", tf, count)
            total_files += count

        # 5) Windows del — 다음 batch 디스크 확보
        for tf in args.timeframes:
            _exec(ssh, f'powershell -c "Remove-Item -Path \\"{WIN_BARS}/{tf}/*\\" -Recurse -Force -ErrorAction SilentlyContinue"')
        log.info("  Windows bars/* deleted")

        # 6) verify
        v = _verify_batch(args.timeframes, local_data, batch_codes, args.days)
        ok = sum(
            1 for s, tfs in v.items()
            if all(d["enough"] for d in tfs.values())
        )
        log.info("  verify: %d/%d ok", ok, len(batch_codes))

        # 7) progress update
        done_now = [s for s, tfs in v.items()
                    if all(d["enough"] for d in tfs.values())]
        failed_now = [s for s, tfs in v.items()
                      if not all(d["enough"] for d in tfs.values())]
        progress.setdefault("done_codes", []).extend(done_now)
        progress.setdefault("failed_codes", []).extend(failed_now)
        progress_path.write_text(json.dumps(progress, indent=2))

    ssh.close()
    log.info(
        "=== complete: %d files synced. done=%d, failed=%d ===",
        total_files,
        len(progress.get("done_codes", [])),
        len(progress.get("failed_codes", [])),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
