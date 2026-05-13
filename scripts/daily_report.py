r"""daily_report — 매일 20:30 cron 자동 실행 (paper_trade 종료 20:00 직후).

목적:
- 오늘 매매 결과 요약 (review_log 1일)
- ledger 현재 상태 (orders / fills / positions / realized PnL)
- universe_candidates 오늘 폭증 종목
- 사용자 룰 8 양식 (strategy 그룹화, 한국어, 종목별 4 요소)

cron::

    30 20 * * 1-5 cd /home/bpearson/ks_ws && \
      PYTHONPATH=src .venv/bin/python -m scripts.daily_report \
      > data/reports/daily_$(date +\%Y\%m\%d).txt

평일 (Mon-Fri) 20:30. 토요일은 weekly_report 가 09:00 에 실행 (주말 별도).

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.daily_report
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

_KST = timezone(timedelta(hours=9))

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


def _today_kst_iso() -> str:
    now_kst = datetime.now(UTC).astimezone(_KST)
    start_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_kst.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _review_today(db: Path) -> str:
    if not db.exists():
        return "(trade_review.sqlite missing)"
    today = _today_kst_iso()
    conn = sqlite3.connect(str(db))
    try:
        rows = list(conn.execute(
            "SELECT strategy, symbol, exit_reason, pnl_krw, entry_price, "
            "exit_price, entry_ts, exit_ts, macro_score_at_entry, entry_note "
            "FROM trade_reviews WHERE exit_ts >= ? ORDER BY exit_ts",
            (today,),
        ))
    finally:
        conn.close()
    if not rows:
        return "  (오늘 청산된 trade 없음)"

    by_strat: dict[str, list[dict]] = defaultdict(list)
    for (strategy, symbol, reason, pnl, ent_p, exit_p, ent_ts, exit_ts,
         macro, entry_note) in rows:
        by_strat[strategy].append({
            "symbol": symbol, "reason": reason, "pnl": pnl,
            "entry": ent_p, "exit": exit_p,
            "entry_ts": ent_ts, "exit_ts": exit_ts,
            "macro": macro, "entry_note": entry_note,
        })

    total_pnl = sum(r[3] for r in rows)
    out = [f"  Total today: {len(rows)} trades / {total_pnl:+,} KRW", ""]
    for strat in sorted(by_strat, key=lambda s: -sum(r["pnl"] for r in by_strat[s])):
        items = by_strat[strat]
        total = sum(r["pnl"] for r in items)
        wins = sum(1 for r in items if r["pnl"] > 0)
        win_rate = wins / len(items) * 100
        out.append(f"  📊 {_kr(strat)} ({strat}): n={len(items)} "
                   f"win={win_rate:.0f}% total={total:+,}")
        for r in sorted(items, key=lambda x: -x["pnl"])[:5]:
            pct = (r["exit"] - r["entry"]) / r["entry"] * 100
            out.append(
                f"    {r['symbol']} {_name(r['symbol']):<8} "
                f"entry={r['entry']:>8,} exit={r['exit']:>8,} "
                f"({pct:+.1f}%) pnl={r['pnl']:>+8,} reason={r['reason']}",
            )
    return "\n".join(out)


def _ledger_today(db: Path) -> str:
    if not db.exists():
        return "  (ledger missing)"
    today = _today_kst_iso()
    conn = sqlite3.connect(str(db))
    try:
        orders = list(conn.execute(
            "SELECT COUNT(*), side FROM orders WHERE submitted_at >= ? GROUP BY side",
            (today,),
        ))
        fills = list(conn.execute(
            "SELECT COUNT(*), side FROM fills WHERE filled_at >= ? GROUP BY side",
            (today,),
        ))
        pnl_total = conn.execute(
            "SELECT SUM((sell.price - buy.price) * sell.quantity) "
            "FROM fills sell JOIN fills buy ON sell.symbol = buy.symbol "
            "WHERE sell.side='sell' AND buy.side='buy' "
            "AND sell.filled_at >= ?",
            (today,),
        ).fetchone()[0] or 0
    finally:
        conn.close()
    order_str = " / ".join(f"{s.upper()}={n}" for n, s in orders) or "0"
    fill_str = " / ".join(f"{s.upper()}={n}" for n, s in fills) or "0"
    return (f"  orders: {order_str}\n  fills: {fill_str}\n"
            f"  rough total realized PnL: {pnl_total:+,} KRW "
            "(주의: FIFO 매칭 X 단순 추정)")


def _universe_candidates_today(db: Path) -> str:
    if not db.exists():
        return "  (universe_candidates.sqlite missing)"
    today = _today_kst_iso()
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT symbol, MAX(surge_ratio), COUNT(*) "
            "FROM candidates WHERE detected_at >= ? GROUP BY symbol "
            "ORDER BY 2 DESC LIMIT 5",
            (today,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "  (오늘 폭증 종목 없음)"
    out = []
    for sym, surge, count in rows:
        out.append(f"  {sym} {_name(sym):<10} max_surge={float(surge):.1f}x count={count}")
    return "\n".join(out)


def _retrospective(db: Path) -> str:
    """사용자 룰 8.3(c): '과거로 돌아간다면' 회고."""
    if not db.exists():
        return "  (no data)"
    today = _today_kst_iso()
    conn = sqlite3.connect(str(db))
    try:
        rows = list(conn.execute(
            "SELECT strategy, symbol, pnl_krw, entry_price, exit_price, "
            "exit_reason, macro_score_at_entry "
            "FROM trade_reviews WHERE exit_ts >= ? "
            "ORDER BY pnl_krw LIMIT 5",
            (today,),
        ))
    finally:
        conn.close()
    if not rows:
        return "  (오늘 청산된 trade 없음)"
    out = ["  [오늘 worst 5 trades — 과거로 돌아간다면?]"]
    for strategy, symbol, pnl, ent, ex, reason, macro in rows:
        pct = (ex - ent) / ent * 100 if ent else 0
        macro_s = f"{macro:.2f}" if macro is not None else "?"
        suggestion = ""
        if reason == "SL" and pct < -1.5:
            suggestion = " → SL threshold tighter (1.0%) 권고"
        elif reason == "timeout" and pnl < 0:
            suggestion = " → max_hold 짧게 (절반)"
        elif macro is not None and macro < 0.7:
            suggestion = f" → macro {macro_s} weak, entry 안 했어야"
        out.append(
            f"  - {_kr(strategy)} {symbol} {_name(symbol)} "
            f"entry={ent:,} exit={ex:,} ({pct:+.2f}%) "
            f"pnl={pnl:+,} reason={reason} macro={macro_s}{suggestion}",
        )
    return "\n".join(out)


def main() -> int:
    now_kst = datetime.now(UTC).astimezone(_KST)
    print(f"\n=== Daily report @ {now_kst.strftime('%Y-%m-%d %H:%M KST')} ===\n")

    print("[1] 오늘 매매 결과 (review_log)")
    print(_review_today(Path("data/trade_review.sqlite")))

    print("\n[2] Ledger 오늘 통계 (orders / fills / rough PnL)")
    print(_ledger_today(Path("data/paper_breakout_ledger.sqlite")))

    print("\n[3] Universe 후보 오늘 폭증")
    print(_universe_candidates_today(Path("data/universe_candidates.sqlite")))

    print("\n[4] 회고 (worst 5 + 과거 돌아간다면)")
    print(_retrospective(Path("data/trade_review.sqlite")))

    return 0


if __name__ == "__main__":
    # silence unused-import
    _ = statistics
    sys.exit(main())
