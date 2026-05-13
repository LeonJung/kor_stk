"""review_log_analyze — TradeReviewLog 누적 데이터 strategy-level 분석.

사용자 룰 (`feedback_performance_report_format`):
- strategy 별 종목 그룹화
- 한국어 strategy 이름
- 종목별 4 요소 (trigger / exit / 회고 / simulation)

본 스크립트는 누적된 trade_review.sqlite 를 읽어:
1. strategy 별 누적 win_rate / mean_pnl / total_pnl / 종목별 분해
2. exit_reason 분해 (TP / SL / timeout) - 각 strategy 청산 패턴
3. macro_score_at_entry 분포 (low/mid/high) 별 승률 - fundamental 효과 검증
4. 보유 시간 분포 (< 10m / 10-60m / > 60m) 별 승률
5. 회고 후보 (각 strategy 의 손실 비율 / 가장 손해 큰 종목 / 가장 이익 큰 종목)

Usage::
    PYTHONPATH=src .venv/bin/python -m scripts.review_log_analyze
    PYTHONPATH=src .venv/bin/python -m scripts.review_log_analyze --strategy breakout
    PYTHONPATH=src .venv/bin/python -m scripts.review_log_analyze --days 7
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
    "breakout": "신고가매매",
    "closing_bet": "종가베팅",
    "pair_follow": "짝꿍매매",
    "scalping": "스캘핑",
    "limit_up": "상따",
    "double_bottom": "쌍바닥매매",
    "box_breakout": "박스권돌파매매",
    "inverse_head_shoulders": "역헤드앤숄더매매",
    "flag_pennant": "깃발페넌트매매",
    "cup_handle": "컵앤핸들매매",
    "triangle": "삼각수렴매매",
    "wedge": "웨지매매",
    "volatility_breakout": "변동성돌파",
    "vwap_reversion": "VWAP평균회귀",
    "nr7_breakout": "NR7돌파",
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


def _kr(strategy: str) -> str:
    return _STRATEGY_KR.get(strategy, strategy)


def _name(sym: str) -> str:
    return _NAMES.get(sym, "?")


def _hold_bucket(entry_iso: str, exit_iso: str) -> str:
    try:
        e = datetime.fromisoformat(entry_iso)
        x = datetime.fromisoformat(exit_iso)
        minutes = (x - e).total_seconds() / 60
    except (ValueError, TypeError):
        return "?"
    if minutes < 10:
        return "<10m"
    if minutes < 60:
        return "10-60m"
    if minutes < 240:
        return "1-4h"
    return ">4h"


def _macro_bucket(score: float | None) -> str:
    if score is None:
        return "?"
    if score < 0.5:
        return "low (<0.5)"
    if score < 1.0:
        return "mid (0.5-1.0)"
    if score < 1.3:
        return "high (1.0-1.3)"
    return "strong (>=1.3)"


def _load_rows(
    db: Path, *, since_iso: str | None = None, strategy: str | None = None,
) -> list[dict]:
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        sql = "SELECT * FROM trade_reviews WHERE 1=1"
        params: list[object] = []
        if since_iso:
            sql += " AND exit_ts >= ?"
            params.append(since_iso)
        if strategy:
            sql += " AND strategy = ?"
            params.append(strategy)
        sql += " ORDER BY exit_ts"
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        conn.close()


def _per_strategy(rows: list[dict]) -> str:
    out = ["[전략별 누적 승률 & 손익]"]
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_strategy[r["strategy"]].append(r)
    out.append(
        f"  {'전략':<14} {'n':>4} {'wins':>4} {'losses':>4} {'win%':>6} "
        f"{'mean_pnl':>10} {'median_pnl':>10} {'total_pnl':>14}"
    )
    out.append("  " + "-" * 80)
    for strat in sorted(by_strategy):
        items = by_strategy[strat]
        pnls = [r["pnl_krw"] for r in items]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        win_rate = wins / len(items) * 100 if items else 0.0
        out.append(
            f"  {_kr(strat):<14} {len(items):>4} {wins:>4} {losses:>4} "
            f"{win_rate:>5.1f}% {int(statistics.mean(pnls)):>+10,} "
            f"{int(statistics.median(pnls)):>+10,} {sum(pnls):>+14,}"
        )
    return "\n".join(out)


def _exit_reason_breakdown(rows: list[dict]) -> str:
    out = ["\n[전략 x 청산 사유 분해]"]
    by: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        by[(r["strategy"], r["exit_reason"])].append(r["pnl_krw"])
    out.append(f"  {'전략':<14} {'reason':<10} {'n':>4} {'win%':>6} {'mean_pnl':>10}")
    out.append("  " + "-" * 56)
    seen_strats: set[str] = set()
    for (strat, reason), pnls in sorted(by.items()):
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100 if pnls else 0.0
        strat_label = _kr(strat) if strat not in seen_strats else ""
        seen_strats.add(strat)
        out.append(
            f"  {strat_label:<14} {reason:<10} {len(pnls):>4} "
            f"{win_rate:>5.1f}% {int(statistics.mean(pnls)):>+10,}"
        )
    return "\n".join(out)


def _macro_score_breakdown(rows: list[dict]) -> str:
    out = ["\n[macro_score_at_entry 구간별 승률 (fundamental 효과 검증)]"]
    by: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        bucket = _macro_bucket(r.get("macro_score_at_entry"))
        by[(r["strategy"], bucket)].append(r["pnl_krw"])
    out.append(f"  {'전략':<14} {'macro':<18} {'n':>4} {'win%':>6} {'mean_pnl':>10}")
    out.append("  " + "-" * 64)
    seen_strats: set[str] = set()
    for (strat, bucket), pnls in sorted(by.items()):
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100 if pnls else 0.0
        strat_label = _kr(strat) if strat not in seen_strats else ""
        seen_strats.add(strat)
        out.append(
            f"  {strat_label:<14} {bucket:<18} {len(pnls):>4} "
            f"{win_rate:>5.1f}% {int(statistics.mean(pnls)):>+10,}"
        )
    return "\n".join(out)


def _hold_duration_breakdown(rows: list[dict]) -> str:
    out = ["\n[보유 시간 분포별 승률]"]
    by: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        bucket = _hold_bucket(r["entry_ts"], r["exit_ts"])
        by[(r["strategy"], bucket)].append(r["pnl_krw"])
    out.append(f"  {'전략':<14} {'hold':<10} {'n':>4} {'win%':>6} {'mean_pnl':>10}")
    out.append("  " + "-" * 56)
    seen_strats: set[str] = set()
    for (strat, bucket), pnls in sorted(by.items()):
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100 if pnls else 0.0
        strat_label = _kr(strat) if strat not in seen_strats else ""
        seen_strats.add(strat)
        out.append(
            f"  {strat_label:<14} {bucket:<10} {len(pnls):>4} "
            f"{win_rate:>5.1f}% {int(statistics.mean(pnls)):>+10,}"
        )
    return "\n".join(out)


def _per_symbol_breakdown(rows: list[dict]) -> str:
    out = ["\n[전략 x 종목별 손익 (top 5 win / top 5 loss)]"]
    by: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        by[(r["strategy"], r["symbol"])].append(r["pnl_krw"])
    aggregated: dict[str, list[tuple[str, int, int, float]]] = defaultdict(list)
    for (strat, sym), pnls in by.items():
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100
        aggregated[strat].append((sym, total, len(pnls), win_rate))
    for strat in sorted(aggregated):
        items = sorted(aggregated[strat], key=lambda x: -x[1])
        out.append(f"\n  📊 {_kr(strat)} ({strat}):")
        out.append(
            f"    {'symbol':<8} {'name':<10} {'total_pnl':>14} {'n':>3} {'win%':>6}"
        )
        for sym, total, n, win_rate in items[:5]:
            out.append(
                f"    {sym:<8} {_name(sym):<10} {total:>+14,} {n:>3} {win_rate:>5.1f}%"
            )
        if len(items) > 5:
            out.append("    ...")
            for sym, total, n, win_rate in items[-5:]:
                out.append(
                    f"    {sym:<8} {_name(sym):<10} {total:>+14,} {n:>3} "
                    f"{win_rate:>5.1f}%"
                )
    return "\n".join(out)


def _retrospective(rows: list[dict]) -> str:
    """rule 8.3 (c) 회고 후보 — strategy 별 가장 큰 손실 거래 → 어떻게 회피했어야?"""
    out = ["\n[회고 후보 — strategy 별 worst trades]"]
    by_strat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_strat[r["strategy"]].append(r)
    for strat in sorted(by_strat):
        worst = sorted(by_strat[strat], key=lambda r: r["pnl_krw"])[:3]
        if not worst:
            continue
        out.append(f"\n  📉 {_kr(strat)} ({strat}) 최악 3건:")
        for w in worst:
            pct = (w["exit_price"] - w["entry_price"]) / w["entry_price"] * 100
            out.append(
                f"    {w['symbol']} {_name(w['symbol'])} "
                f"entry={w['entry_price']:,} exit={w['exit_price']:,} "
                f"({pct:+.2f}%) pnl={w['pnl_krw']:+,} reason={w['exit_reason']} "
                f"macro={w.get('macro_score_at_entry') or '?'}"
            )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", default="data/trade_review.sqlite",
        help="trade_review.sqlite 경로",
    )
    parser.add_argument(
        "--strategy", default=None,
        help="특정 strategy 만 필터 (예: breakout)",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="최근 N일 내 청산만 분석",
    )
    args = parser.parse_args()

    db = Path(args.db)
    since_iso = None
    if args.days is not None:
        since = datetime.now(UTC) - timedelta(days=args.days)
        since_iso = since.isoformat()

    rows = _load_rows(db, since_iso=since_iso, strategy=args.strategy)
    if not rows:
        print(
            f"\n(no rows in {db} "
            f"{'after ' + since_iso if since_iso else ''} "
            f"{'strategy=' + args.strategy if args.strategy else ''})"
        )
        return 0

    span = f"({len(rows)} reviews"
    if since_iso:
        span += f", since {since_iso[:10]}"
    if args.strategy:
        span += f", strategy={args.strategy}"
    span += ")"
    print(f"\n=== review_log_analyze {span} ===\n")
    print(_per_strategy(rows))
    print(_exit_reason_breakdown(rows))
    print(_macro_score_breakdown(rows))
    print(_hold_duration_breakdown(rows))
    print(_per_symbol_breakdown(rows))
    print(_retrospective(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
