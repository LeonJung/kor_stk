"""Normalize all tick timestamps in data/ticks.sqlite to UTC.

분봉 fix (fix_minute_timestamps.py) 와 같은 카테고리 — tick capture 가
publisher 에 따라 +09:00 (KST) 또는 +00:00 (UTC) 두 종류로 섞여 들어감.
KIS WS H0STCNT0 frame (parse_trade_record → _parse_kis_time) 은 정확히
UTC 로 변환되지만, WARM poll snapshot 또는 이전 capture script 는
KST tzinfo 그대로 publish 했음.

이 스크립트:
  - +09:00 라벨 record 의 시각은 KST wall-time → UTC 로 -9h shift + tzinfo UTC
  - +00:00 record 는 그대로 (이미 UTC)
  - naive timestamp 는 KST 로 가정 후 UTC 변환

idempotent: 다시 돌려도 +09:00 record 가 없으니 no-op.

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.fix_tick_timestamps
    PYTHONPATH=src .venv/bin/python -m scripts.fix_tick_timestamps --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/ticks.sqlite")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No DB at {db_path}")
        return 0

    con = sqlite3.connect(str(db_path))
    total = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    print(f"Total ticks: {total:,}")

    # Categorize
    n_kst = con.execute("SELECT COUNT(*) FROM ticks WHERE ts_iso LIKE '%+09:00'").fetchone()[0]
    n_utc = con.execute("SELECT COUNT(*) FROM ticks WHERE ts_iso LIKE '%+00:00'").fetchone()[0]
    n_naive = con.execute(
        "SELECT COUNT(*) FROM ticks WHERE ts_iso NOT LIKE '%+%' AND ts_iso NOT LIKE '%Z'"
    ).fetchone()[0]
    print(f"  +09:00 (KST):       {n_kst:,}")
    print(f"  +00:00 (UTC):       {n_utc:,}  (already correct, leave alone)")
    print(f"  naive (no tz):      {n_naive:,}  (assume KST → convert to UTC)")

    if args.dry_run:
        print("--dry-run: no changes")
        con.close()
        return 0

    fixes: list[tuple[str, int]] = []

    # Fix +09:00 → -9h shift to UTC
    rows = con.execute("SELECT id, ts_iso FROM ticks WHERE ts_iso LIKE '%+09:00'").fetchall()
    for rid, ts in rows:
        dt = datetime.fromisoformat(ts)
        new = dt.astimezone(timezone.utc).isoformat()
        fixes.append((new, rid))

    # Fix naive → assume KST → convert to UTC
    rows = con.execute(
        "SELECT id, ts_iso FROM ticks WHERE ts_iso NOT LIKE '%+%' AND ts_iso NOT LIKE '%Z'"
    ).fetchall()
    for rid, ts in rows:
        dt = datetime.fromisoformat(ts).replace(tzinfo=_KST)
        new = dt.astimezone(timezone.utc).isoformat()
        fixes.append((new, rid))

    if not fixes:
        print("Nothing to fix.")
        con.close()
        return 0

    print(f"Updating {len(fixes):,} rows...")
    con.execute("BEGIN")
    con.executemany("UPDATE ticks SET ts_iso = ? WHERE id = ?", fixes)
    con.commit()

    # Verify
    n_kst_after = con.execute("SELECT COUNT(*) FROM ticks WHERE ts_iso LIKE '%+09:00'").fetchone()[0]
    n_utc_after = con.execute("SELECT COUNT(*) FROM ticks WHERE ts_iso LIKE '%+00:00'").fetchone()[0]
    print(f"After fix: +09:00 {n_kst_after:,}, +00:00 {n_utc_after:,}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
