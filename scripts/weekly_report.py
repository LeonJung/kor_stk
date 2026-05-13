r"""weekly_report — 매주 토요일 09:00 cron 자동 실행.

종합:
1. 이번 주 매매 결과 (review_log 7일 분해)
2. universe_candidates 누적 (이번 주 폭증 종목 top)
3. StrategyWeightManager 현재 weight 상태
4. 다음 주 paper_trade 권고 (자동 비활성 strategy / boost 종목)

Cron entry::

    0 9 * * 6 cd /home/bpearson/ks_ws && \
      PYTHONPATH=src .venv/bin/python -m scripts.weekly_report \
      > data/reports/weekly_$(date +\%Y\%m\%d).txt

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.weekly_report
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
    "breakout": "신고가매매", "closing_bet": "종가베팅", "pair_follow": "짝꿍매매",
    "scalping": "스캘핑", "limit_up": "상따",
    "double_bottom": "쌍바닥매매", "box_breakout": "박스권돌파매매",
    "inverse_head_shoulders": "역헤드앤숄더매매", "flag_pennant": "깃발페넌트매매",
    "cup_handle": "컵앤핸들매매", "triangle": "삼각수렴매매", "wedge": "웨지매매",
    "volatility_breakout": "변동성돌파", "vwap_reversion": "VWAP평균회귀",
    "nr7_breakout": "NR7돌파", "bnf_disparity": "BNF이격도",
    "dual_thrust": "듀얼트러스트", "opening_momentum": "시초모멘텀",
    "foreign_flow": "외국인수급", "color_streak": "양봉연속",
    "pivot_half_pullback": "피벗절반눌림", "tape_burst": "체결폭주",
}

_NAMES = {
    "005930": "삼성전자", "000660": "SK하이닉스", "402340": "SK스퀘어",
    "005380": "현대차", "373220": "LG엔솔", "034020": "두산에너빌",
    "329180": "HD현대중", "028260": "삼성물산", "009150": "삼성전기",
    "207940": "삼성바이오", "012450": "한화에어로", "000270": "기아",
    "105560": "KB금융", "032830": "삼성생명", "006400": "삼성SDI",
    "267260": "HD현대일렉", "010120": "LS ELEC", "055550": "신한지주",
    "012330": "현대모비스", "006800": "미래에셋증권",
}


def _kr(s: str) -> str:
    return _STRATEGY_KR.get(s, s)


def _name(s: str) -> str:
    return _NAMES.get(s, "?")


def _trade_review_summary(db: Path, days: int = 7) -> str:
    if not db.exists():
        return "(trade_review.sqlite missing)"
    conn = sqlite3.connect(str(db))
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        rows = list(conn.execute(
            "SELECT strategy, symbol, exit_reason, pnl_krw, exit_ts "
            "FROM trade_reviews WHERE exit_ts >= ? ORDER BY exit_ts",
            (since,),
        ))
    finally:
        conn.close()
    if not rows:
        return f"  (no closed trades in last {days} days)"

    by_strat: dict[str, list[int]] = defaultdict(list)
    by_sym: dict[str, list[int]] = defaultdict(list)
    by_strat_sym: dict[tuple[str, str], list[int]] = defaultdict(list)
    for strategy, symbol, _reason, pnl, _ts in rows:
        by_strat[strategy].append(pnl)
        by_sym[symbol].append(pnl)
        by_strat_sym[(strategy, symbol)].append(pnl)

    out = [f"  Total: {len(rows)} trades / "
           f"{sum(p for ps in by_strat.values() for p in ps):+,} KRW"]
    out.append("")
    out.append("  [Strategy 별]")
    out.append(f"  {'전략':<18} {'n':>5} {'win%':>6} {'mean':>10} {'total':>14}")
    out.append("  " + "-" * 60)
    for strat in sorted(by_strat, key=lambda s: -sum(by_strat[s])):
        pnls = by_strat[strat]
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100
        out.append(
            f"  {_kr(strat):<18} {len(pnls):>5} {win_rate:>5.1f}% "
            f"{int(statistics.mean(pnls)):>+10,} {sum(pnls):>+14,}",
        )

    out.append("")
    out.append("  [종목 별 top 5 wins / bottom 5]")
    syms_sorted = sorted(by_sym, key=lambda s: -sum(by_sym[s]))
    for sym in syms_sorted[:5]:
        out.append(
            f"  + {sym} {_name(sym):<10} pnl={sum(by_sym[sym]):>+12,} "
            f"n={len(by_sym[sym])}",
        )
    if len(syms_sorted) > 5:
        out.append("  ...")
        for sym in syms_sorted[-5:]:
            out.append(
                f"  - {sym} {_name(sym):<10} pnl={sum(by_sym[sym]):>+12,} "
                f"n={len(by_sym[sym])}",
            )
    return "\n".join(out)


def _universe_candidates_summary(db: Path, days: int = 7) -> str:
    if not db.exists():
        return "  (universe_candidates.sqlite missing)"
    conn = sqlite3.connect(str(db))
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        rows = conn.execute(
            "SELECT symbol, MAX(surge_ratio), COUNT(*) "
            "FROM candidates WHERE detected_at >= ? GROUP BY symbol "
            "ORDER BY 2 DESC",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "  (no candidates this week)"
    out = [f"  {'symbol':<10} {'name':<12} {'max_surge':>10} {'count':>5}"]
    out.append("  " + "-" * 50)
    for sym, max_surge, count in rows[:10]:
        out.append(
            f"  {sym:<10} {_name(sym):<12} {float(max_surge):>9.2f}x {count:>5}",
        )
    return "\n".join(out)


def _strategy_weights_summary(db: Path) -> str:
    """Compute current strategy weights using the WeightManager."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ks_ws.sources.strategy_weight_manager import compute_strategy_weights

    weights = compute_strategy_weights(db, days=14, n_min=5)
    if not weights:
        return "  (no live data — using backtest baseline)"
    out = [f"  {'전략':<18} {'n':>4} {'win%':>6} {'weight':>7} {'status':>14}"]
    out.append("  " + "-" * 60)
    for w in sorted(weights, key=lambda x: -x.weight):
        out.append(
            f"  {_kr(w.strategy):<18} {w.n:>4} {w.win_rate * 100:>5.1f}% "
            f"{w.weight:>6.1f} {w.reason:>14}",
        )
    return "\n".join(out)


def _recommendations(db: Path) -> str:
    """Auto-derived recommendation for next week."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ks_ws.sources.strategy_weight_manager import compute_strategy_weights

    weights = compute_strategy_weights(db, days=14, n_min=5)
    recs = []
    for w in weights:
        if w.reason == "disabled":
            recs.append(
                f"  ! 비활성: {_kr(w.strategy)} (win {w.win_rate * 100:.1f}%, "
                f"n={w.n})",
            )
        elif w.reason == "high_winrate":
            recs.append(
                f"  + boost: {_kr(w.strategy)} (win {w.win_rate * 100:.1f}%, "
                f"n={w.n})",
            )
    if not recs:
        return "  (live 누적 부족 — backtest baseline 그대로 사용 권고)"
    return "\n".join(recs)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7, help="lookback days")
    args = p.parse_args()

    now_kst = datetime.now(UTC).astimezone()
    print(f"\n=== Weekly report @ {now_kst.strftime('%Y-%m-%d %H:%M KST')} ===")
    print(f"  (lookback {args.days} days)\n")

    print("[1] 매매 실적 요약")
    print(_trade_review_summary(Path("data/trade_review.sqlite"), args.days))

    print("\n[2] Universe 후보 (거래대금 폭증)")
    print(_universe_candidates_summary(
        Path("data/universe_candidates.sqlite"), args.days,
    ))

    print("\n[3] Strategy weight 현재 상태")
    print(_strategy_weights_summary(Path("data/trade_review.sqlite")))

    print("\n[4] 다음 주 권고")
    print(_recommendations(Path("data/trade_review.sqlite")))

    return 0


if __name__ == "__main__":
    sys.exit(main())
