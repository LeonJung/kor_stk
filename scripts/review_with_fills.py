"""review_with_fills — review_log + ledger fills 결합 보고서.

review_log 는 strategy 가 자체 기록한 entry/exit (in-memory price).
ledger fills 는 MockFillSimulator (cycle 23) 가 cost 반영 실제 fill price 기록.

본 스크립트는 두 source 를 (symbol, entry_ts/exit_ts) 로 매칭 → slippage
(strategy 가 예상한 price vs 실제 fill price 차이) 분석.

cron::

    0 21 * * 1-5 cd /home/bpearson/ks_ws && \\
      PYTHONPATH=src .venv/bin/python -m scripts.review_with_fills \\
      > data/reports/slippage_$(date +\\%Y\\%m\\%d).txt

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.review_with_fills
    PYTHONPATH=src .venv/bin/python -m scripts.review_with_fills --days 7
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

_STRATEGY_KR = {
    "breakout": "신고가매매", "closing_bet": "종가베팅",
    "double_bottom": "쌍바닥매매", "box_breakout": "박스권돌파매매",
    "inverse_head_shoulders": "역헤드앤숄더매매", "flag_pennant": "깃발페넌트매매",
    "cup_handle": "컵앤핸들매매", "triangle": "삼각수렴매매", "wedge": "웨지매매",
    "volatility_breakout": "변동성돌파", "vwap_reversion": "VWAP평균회귀",
    "nr7_breakout": "NR7돌파", "bnf_disparity": "BNF이격도",
    "dual_thrust": "듀얼트러스트", "opening_momentum": "시초모멘텀",
    "foreign_flow": "외국인수급", "color_streak": "양봉연속",
    "pivot_half_pullback": "피벗절반눌림", "tape_burst": "체결폭주",
}


def _kr(s: str) -> str:
    return _STRATEGY_KR.get(s, s)


def _load_reviews(db: Path, since_iso: str) -> list[dict]:
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = list(conn.execute(
            "SELECT strategy, symbol, entry_ts, entry_price, "
            "exit_ts, exit_price, pnl_krw, exit_reason "
            "FROM trade_reviews WHERE exit_ts >= ?",
            (since_iso,),
        ))
    finally:
        conn.close()
    cols = ["strategy", "symbol", "entry_ts", "entry_price",
            "exit_ts", "exit_price", "pnl_krw", "exit_reason"]
    return [dict(zip(cols, row, strict=True)) for row in rows]


def _load_fills(db: Path, since_iso: str) -> list[dict]:
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = list(conn.execute(
            "SELECT symbol, side, quantity, price, filled_at "
            "FROM fills WHERE filled_at >= ? ORDER BY filled_at",
            (since_iso,),
        ))
    finally:
        conn.close()
    cols = ["symbol", "side", "quantity", "price", "filled_at"]
    return [dict(zip(cols, row, strict=True)) for row in rows]


def _match_review_with_fill(
    review: dict, fills: list[dict], side: str, ts_key: str, price_key: str,
) -> dict | None:
    """Find the fill closest in time to review[ts_key] with matching symbol+side."""
    ts = datetime.fromisoformat(review[ts_key])
    best = None
    best_diff = None
    for f in fills:
        if f["symbol"] != review["symbol"] or f["side"] != side:
            continue
        f_ts = datetime.fromisoformat(f["filled_at"])
        diff = abs((f_ts - ts).total_seconds())
        if best is None or diff < best_diff:
            best = f
            best_diff = diff
    if best is None or best_diff > 60:  # > 60s 차이 = 매칭 X
        return None
    return best


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()

    since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
    reviews = _load_reviews(Path("data/trade_review.sqlite"), since)
    fills = _load_fills(Path("data/paper_breakout_ledger.sqlite"), since)

    now_kst = datetime.now(UTC).astimezone()
    print(f"\n=== Review x Ledger fills @ {now_kst.strftime('%Y-%m-%d %H:%M KST')} "
          f"(last {args.days} days) ===\n")
    print(f"  reviews: {len(reviews)}, fills: {len(fills)}\n")

    if not reviews or not fills:
        print("  (insufficient data)")
        return 0

    # Match each review with BUY entry + SELL exit fill
    slippage_buy: list[int] = []  # actual_fill - strategy_expected
    slippage_sell: list[int] = []
    matched = 0
    by_strat_slip: dict[str, list[int]] = defaultdict(list)

    for r in reviews:
        buy = _match_review_with_fill(r, fills, "buy", "entry_ts", "entry_price")
        sell = _match_review_with_fill(r, fills, "sell", "exit_ts", "exit_price")
        if buy is None and sell is None:
            continue
        matched += 1
        if buy is not None:
            slip = buy["price"] - r["entry_price"]
            slippage_buy.append(slip)
            by_strat_slip[r["strategy"]].append(slip)
        if sell is not None:
            slip = r["exit_price"] - sell["price"]  # SELL slippage = expected - actual
            slippage_sell.append(slip)
            by_strat_slip[r["strategy"]].append(slip)

    print(f"  matched: {matched}/{len(reviews)} reviews (BUY or SELL fill found within 60s)\n")

    if slippage_buy:
        print(f"  BUY slippage (actual_fill - expected): "
              f"mean={statistics.mean(slippage_buy):+.0f} "
              f"max={max(slippage_buy)} min={min(slippage_buy)}")
    if slippage_sell:
        print(f"  SELL slippage (expected - actual_fill): "
              f"mean={statistics.mean(slippage_sell):+.0f} "
              f"max={max(slippage_sell)} min={min(slippage_sell)}")

    if by_strat_slip:
        print("\n  [전략 x 평균 slippage]")
        sorted_slips = sorted(
            by_strat_slip.items(),
            key=lambda x: -abs(statistics.mean(x[1])),
        )
        for strat, slips in sorted_slips:
            print(f"  {_kr(strat):<18} n={len(slips):>4} "
                  f"mean_slip={statistics.mean(slips):+.0f} KRW")

    return 0


if __name__ == "__main__":
    sys.exit(main())
