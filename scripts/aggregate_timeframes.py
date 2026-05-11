"""Aggregate higher timeframes from base bars.

년/월/주 봉 = 일봉 aggregate (open=first, high=max, low=min, close=last,
volume=sum, value=sum).
시봉 = 분봉 aggregate (60-bar windows aligned to 09:00).

KIS API 가 시봉 endpoint 따로 안 줄 수 있고 (일/분/주/월 만), 우리가 자체
aggregate 하는 게 가장 확실. 일봉 fetch 가 끝난 후 호출.

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.aggregate_timeframes
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def aggregate_bars(bars, target: str):
    """Group bars by target timeframe boundary, return aggregated bars."""
    from ks_ws.domain import Bar

    if target not in {"1w", "1mo", "1y", "1h"}:
        raise ValueError(f"unsupported target {target}")

    groups: dict = defaultdict(list)
    for b in bars:
        ts = b.timestamp
        if target == "1w":
            iso = ts.isocalendar()
            key = (iso[0], iso[1])  # year, week
        elif target == "1mo":
            key = (ts.year, ts.month)
        elif target == "1y":
            key = (ts.year,)
        elif target == "1h":
            key = (ts.year, ts.month, ts.day, ts.hour)
        groups[key].append(b)

    out = []
    for key, group in sorted(groups.items()):
        group_sorted = sorted(group, key=lambda x: x.timestamp)
        first = group_sorted[0]
        out.append(
            Bar(
                symbol=first.symbol,
                timestamp=first.timestamp,
                timeframe=target,
                open=first.open,
                high=max(b.high for b in group_sorted),
                low=min(b.low for b in group_sorted),
                close=group_sorted[-1].close,
                volume=sum(b.volume for b in group_sorted),
                value=sum(b.value for b in group_sorted),
            )
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--universe-db", default="data/universe.sqlite")
    parser.add_argument("--source", choices=["1d", "1m"], default="1d",
                        help="source timeframe (1d → aggregate to 1w/1mo/1y; 1m → 1h)")
    args = parser.parse_args()

    from ks_ws.storage.bars import BarStore
    from ks_ws.storage.universe import UniverseRegistry

    bar_store = BarStore(args.data_dir)
    reg = UniverseRegistry(args.universe_db)
    codes = reg.codes(exclude_spac=True)
    print(f"=== Aggregating from {args.source} for {len(codes)} symbols ===")

    if args.source == "1d":
        targets = ["1w", "1mo", "1y"]
    else:
        targets = ["1h"]

    for target in targets:
        print(f"\n--- Target: {target} ---")
        total = 0
        for i, code in enumerate(codes):
            src_bars = list(bar_store.read(code, args.source))
            if not src_bars:
                continue
            agg = aggregate_bars(src_bars, target)
            if agg:
                bar_store.write(agg)
                total += len(agg)
            if (i + 1) % 200 == 0:
                print(f"  [{i+1:>5d}/{len(codes)}] {target} aggregated rows={total}", flush=True)
        print(f"  ✓ {target} complete: {total} rows")

    reg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
