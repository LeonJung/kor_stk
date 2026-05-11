"""Fetch KOSPI / KOSDAQ index daily bars via FinanceDataReader,
저장은 BarStore '/1d/' 의 별도 symbol code (KOSPI, KOSDAQ) 로.

CrashRecovery / RegimeDetector / TrendShift 가 사용하는 시장 baseline.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    import FinanceDataReader as fdr

    from ks_ws.domain import Bar
    from ks_ws.storage.bars import BarStore

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=args.days)
    bar_store = BarStore(args.data_dir)

    written_total = 0
    for symbol_code, fdr_code in [("KOSPI", "KS11"), ("KOSDAQ", "KQ11")]:
        df = fdr.DataReader(fdr_code, start, end)
        if df is None or len(df) == 0:
            print(f"  ! {symbol_code}: empty")
            continue
        bars = []
        for ts, row in df.iterrows():
            try:
                ts_utc = datetime(ts.year, ts.month, ts.day, tzinfo=UTC)
                bars.append(
                    Bar(
                        symbol=symbol_code,
                        timestamp=ts_utc,
                        timeframe="1d",
                        open=int(row["Open"] * 100),  # KOSPI 지수 *100 → integer
                        high=int(row["High"] * 100),
                        low=int(row["Low"] * 100),
                        close=int(row["Close"] * 100),
                        volume=int(row.get("Volume", 0) or 0),
                        value=int(row.get("Amount", 0) or 0),
                    )
                )
            except Exception:
                continue
        if bars:
            n = bar_store.write(bars)
            written_total += n
            print(f"  ✓ {symbol_code}: {n} rows")
    print(f"\nTotal: {written_total} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
