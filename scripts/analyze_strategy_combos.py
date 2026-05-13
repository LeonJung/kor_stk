"""analyze_strategy_combos — multi-strategy 동시 BUY 패턴 분석.

memory `feedback_strategy_decisions` + ensemble 본질: 한 종목에 여러 strategy
가 동시에 BUY signal 내면 더 강한 confidence → 더 큰 비중.

FundamentalAllocator.combine() 이 이미 buy_score 누적함. 본 스크립트는:
- review_log + paper_breakout_ledger 의 OrderIntent.sources 를 분해
- 같은 종목 / 같은 시간대 (5분 window) 안에서 여러 strategy 가 동시 BUY 한 trade 찾음
- 단일 strategy entry vs 조합 entry 의 win_rate / mean_pnl 비교

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.analyze_strategy_combos
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _load_orders(db: Path, *, since_iso: str) -> list[dict]:
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = list(conn.execute(
            "SELECT order_id, symbol, side, quantity, submitted_at, sources "
            "FROM orders WHERE submitted_at >= ?",
            (since_iso,),
        ))
        cols = ["order_id", "symbol", "side", "quantity", "submitted_at", "sources"]
        return [dict(zip(cols, row, strict=True)) for row in rows]
    finally:
        conn.close()


def _parse_sources(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return [raw]
    if isinstance(raw, list):
        return raw
    return [str(raw)]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30, help="lookback days")
    p.add_argument(
        "--window-min", type=int, default=5,
        help="multi-strategy 동시성 판단 시간 window (분)",
    )
    args = p.parse_args()

    since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
    orders = _load_orders(Path("data/paper_breakout_ledger.sqlite"), since_iso=since)
    if not orders:
        print(f"\n(no orders in last {args.days} days — paper_trade 누적 부족)")
        return 0

    # Group BUY orders by (symbol, time_bucket) where bucket = window-min window
    win_sec = args.window_min * 60
    by_bucket: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for o in orders:
        if o["side"] != "buy":
            continue
        try:
            ts = datetime.fromisoformat(o["submitted_at"]).timestamp()
        except (ValueError, TypeError):
            continue
        bucket = int(ts // win_sec)
        by_bucket[(o["symbol"], bucket)].append(o)

    # Classify: single-strategy entry vs multi-strategy
    single_entries: list[tuple[str, str]] = []  # (sym, strategy)
    multi_entries: list[tuple[str, tuple[str, ...]]] = []  # (sym, strategies)
    for (sym, _bucket), group in by_bucket.items():
        all_sources = set()
        for o in group:
            all_sources.update(_parse_sources(o.get("sources")))
        if not all_sources:
            continue
        if len(all_sources) == 1:
            single_entries.append((sym, next(iter(all_sources))))
        else:
            multi_entries.append((sym, tuple(sorted(all_sources))))

    print(f"\n=== Multi-strategy combo analysis (last {args.days} days, "
          f"{args.window_min}분 window) ===\n")
    print(f"  Single-strategy entries: {len(single_entries)}")
    print(f"  Multi-strategy entries: {len(multi_entries)}")

    # Top combo frequencies
    combo_counts: dict[tuple[str, ...], int] = defaultdict(int)
    for _sym, combo in multi_entries:
        combo_counts[combo] += 1
    if combo_counts:
        print("\n  [Top 10 combos]")
        for combo, count in sorted(combo_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"    x {count:>3}: {' + '.join(combo)}")

    # PnL comparison would need to match SELL exits with these BUYs.
    # For now, just frequency analysis. Full PnL combo requires review_log
    # cross-reference (which strategy actually closed the position).
    return 0


if __name__ == "__main__":
    sys.exit(main())
