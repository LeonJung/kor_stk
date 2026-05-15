"""update_symbol_weights — walk-forward 기반 strategy × symbol weight 갱신.

사용자 룰 (2026-05-15) Tier 5: 매주/매월 cron 으로 실행. 최근 N개월 backtest
의 trade CSV 에서 종목별 승률 + 평균 PnL% 계산 → SymbolWeightMatrix sqlite
저장. paper_trade_breakout 가 시작 시 load → Allocator 에 주입.

Input:
- trade CSV (backtest_all_strategies_minute / _daily 가 emit)
- 또는 trade_review.sqlite (paper trade 결과 — 향후 확장)

Output:
- data/symbol_weights.sqlite (SymbolWeightMatrix DDL)

Usage:
    PYTHONPATH=src .venv/bin/python -m scripts.update_symbol_weights \\
        --trades data/reports/full_backtest_2026_05_15/minute_concat_trades.csv \\
        --trades data/reports/full_backtest_2026_05_15/daily_trades.csv \\
        --train-months 6 \\
        --db data/symbol_weights.sqlite
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.strategies.symbol_weights import (
    SymbolWeightMatrix,
    WeightRule,
    compute_weight,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("update_symbol_weights")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--trades", action="append", required=True,
                   help="trade CSV 파일 (여러 개 OK)")
    p.add_argument("--train-months", type=int, default=6,
                   help="과거 N개월 trade 만 사용 (default 6)")
    p.add_argument("--db", type=str, default="data/symbol_weights.sqlite")
    p.add_argument("--min-n", type=int, default=3, help="최소 회차 (default 3)")
    p.add_argument("--min-avg-pct", type=float, default=0.0,
                   help="최소 평균 PnL%% (default 0)")
    args = p.parse_args()

    # 1. cutoff (train period)
    cutoff = datetime.now(UTC) - timedelta(days=args.train_months * 30)
    cutoff_iso = cutoff.isoformat()
    log.info("train window cutoff: %s", cutoff_iso)

    # 2. Aggregate trades per (strategy, symbol)
    agg: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl_pct_sum": 0.0}
    )
    total_rows = 0
    for path in args.trades:
        if not Path(path).exists():
            log.warning("not found: %s", path)
            continue
        with open(path) as f:
            for r in csv.DictReader(f):
                total_rows += 1
                if r.get("entry_ts", "") < cutoff_iso:
                    continue
                key = (r["strategy"], r["symbol"])
                a = agg[key]
                a["n"] += 1
                try:
                    pnl_pct = float(r["pnl_pct"])
                    pnl = int(r["pnl_krw"])
                except (ValueError, KeyError):
                    continue
                if pnl > 0:
                    a["wins"] += 1
                a["pnl_pct_sum"] += pnl_pct
    log.info("processed %d total rows → %d (strategy, symbol) pairs",
             total_rows, len(agg))

    # 3. Compute weights
    rule = WeightRule(min_n=args.min_n, min_avg_pct=args.min_avg_pct)
    entries = []
    weight_counts: dict[float, int] = defaultdict(int)
    for (strat, sym), stats in agg.items():
        w = compute_weight(stats["n"], stats["wins"],
                           stats["pnl_pct_sum"], rule=rule)
        weight_counts[w] += 1
        entries.append((strat, sym, w, stats["n"], stats["wins"],
                        stats["pnl_pct_sum"]))

    # 4. Bulk upsert
    matrix = SymbolWeightMatrix(db_path=args.db)
    matrix.bulk_upsert(entries)
    log.info("upserted %d entries to %s", len(entries), args.db)
    log.info("weight distribution:")
    for w in sorted(weight_counts, reverse=True):
        log.info("  weight=%.1f: %d entries", w, weight_counts[w])

    # 5. Per-strategy stats
    log.info("\nPer-strategy stats:")
    stats = matrix.stats()
    for strat in sorted(stats):
        s = stats[strat]
        log.info("  %-22s total=%d blocked=%d x1=%d x2=%d x3+=%d",
                 strat, s["total"], s["blocked"], s["x1"], s["x2"], s["x3+"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
