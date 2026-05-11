"""One-shot: shift existing 1m bars in BarStore by -9h to fix KST→UTC bug.

Bug: KIS API gives KST wall-clock time; we wrote it as UTC. So 09:01 KST
got stored as 09:01 UTC (= 18:01 KST). Fix: shift all 1m parquet timestamps
by -9 hours.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def main() -> int:
    root = Path("data/bars/1m")
    if not root.exists():
        print("No 1m bars to fix")
        return 0
    files = list(root.rglob("*.parquet"))
    print(f"Fixing {len(files)} 1m parquet files (shift -9h)...")
    for i, path in enumerate(files):
        con = duckdb.connect(":memory:")
        try:
            tmp = path.with_suffix(".parquet.tmp")
            con.execute(
                f"""
                COPY (
                    SELECT
                        (timestamp - INTERVAL '9 hours') AS timestamp,
                        open, high, low, close, volume, value
                    FROM read_parquet('{path}')
                    ORDER BY timestamp
                ) TO '{tmp}' (FORMAT PARQUET)
                """
            )
            tmp.replace(path)
        finally:
            con.close()
        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{len(files)}] fixed", flush=True)
    print(f"  ✓ Fixed {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
