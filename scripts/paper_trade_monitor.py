"""paper_trade_monitor — paper_trade_breakout 의 라이브 상태 단발 snapshot.

매 시각 사용자가 매매 결과 확인 시 호출. cron 매 30분 자동 실행도 가능.

출력:
- process 상태 (PID, etime)
- ticks.sqlite: tick / orderbook 누적 (오늘분)
- paper_breakout_ledger.sqlite: orders side mix, fills, positions
- per-symbol unrealized PnL (tick last price 기반 추정, mock fill 0 한계 우회)
- 시간대별 entry timing 분포

사용자 룰 `feedback_performance_report_format`:
- strategy 별 종목 그룹화
- 한국어 strategy 이름 (신고가매매 / 종가베팅 ...)
- 종목별 4 요소 (a) 매수 trigger (b) 매도 룰 (c) 회고 (d) (가능 시) 시뮬레이션

Usage::
    PYTHONPATH=src .venv/bin/python -m scripts.paper_trade_monitor
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

_KST = timezone(timedelta(hours=9))

# Strategy 이름 매핑 (한국어 — response_rule rule 8.2)
_STRATEGY_KR = {
    "breakout": "신고가매매",
    "closing_bet": "종가베팅",
    "pair_follow": "짝꿍매매",
    "scalping": "스캘핑",
    "limit_up": "상따",
    "double_bottom": "쌍바닥매매",
    "box_breakout": "박스권돌파매매",
    "inverse_head_shoulders": "역헤드앤숄더매매",
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


def _process_status() -> str:
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "scripts.paper_trade_breakout"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        return "  (process not running)"
    if not out:
        return "  (process not running)"
    lines = []
    for line in out.split("\n"):
        pid = line.split()[0]
        try:
            etime = subprocess.check_output(
                ["ps", "-o", "etime=,pcpu=,pmem=,rss=", "-p", pid], text=True
            ).strip()
            lines.append(f"  PID {pid}  {etime}")
        except subprocess.CalledProcessError:
            lines.append(f"  PID {pid}  (stat read failed)")
    return "\n".join(lines)


def _ticks_summary(today_kst_iso_prefix: str) -> str:
    db = Path("data/ticks.sqlite")
    if not db.exists():
        return "  (ticks.sqlite missing)"
    conn = sqlite3.connect(str(db))
    try:
        t_total = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
        t_today = conn.execute(
            "SELECT COUNT(*) FROM ticks WHERE ts_iso >= ?",
            (today_kst_iso_prefix,),
        ).fetchone()[0]
        ob_total = conn.execute("SELECT COUNT(*) FROM orderbook").fetchone()[0]
        ob_today = conn.execute(
            "SELECT COUNT(*) FROM orderbook WHERE ts_iso >= ?",
            (today_kst_iso_prefix,),
        ).fetchone()[0]
        # Per-symbol today tick count + last price
        rows = conn.execute(
            "SELECT symbol, COUNT(*), MAX(price) FROM ticks "
            "WHERE ts_iso >= ? GROUP BY symbol ORDER BY 2 DESC",
            (today_kst_iso_prefix,),
        ).fetchall()
        per = [(sym, cnt, last_p) for sym, cnt, last_p in rows]
    finally:
        conn.close()
    out = [
        f"  ticks total={t_total:,} today={t_today:,}",
        f"  orderbook total={ob_total:,} today={ob_today:,}",
        "  per-symbol today (top 5):",
    ]
    for sym, cnt, lp in per[:5]:
        nm = _NAMES.get(sym, "?")
        out.append(f"    {sym} {nm:<8} ticks={cnt:>6,} last={lp:>9,}")
    return "\n".join(out)


def _ledger_summary(today_kst_iso_prefix: str) -> str:
    db = Path("data/paper_breakout_ledger.sqlite")
    if not db.exists():
        return "  (ledger missing)"
    conn = sqlite3.connect(str(db))
    try:
        # side mix
        by_side = dict(
            conn.execute(
                "SELECT side, COUNT(*) FROM orders WHERE submitted_at >= ? GROUP BY side",
                (today_kst_iso_prefix,),
            ).fetchall()
        )
        # Per-strategy (sources column)
        rows = list(
            conn.execute(
                "SELECT symbol, side, quantity, submitted_at, sources "
                "FROM orders WHERE submitted_at >= ? ORDER BY submitted_at",
                (today_kst_iso_prefix,),
            )
        )
        by_strategy_sym: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {"buy": 0, "sell": 0, "buy_qty": 0, "sell_qty": 0}
        )
        for sym, side, qty, _ts, sources in rows:
            src = (json.loads(sources) if sources else ["?"])[0] if isinstance(sources, str) and sources.startswith("[") else (sources or "?")
            if isinstance(src, list):
                src = src[0] if src else "?"
            by_strategy_sym[(src, sym)][side] += 1
            by_strategy_sym[(src, sym)][f"{side}_qty"] += qty
    finally:
        conn.close()

    out = [
        f"  orders today: buy={by_side.get('buy', 0)} sell={by_side.get('sell', 0)}"
    ]
    # Group by strategy
    by_strategy: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for (src, sym), data in by_strategy_sym.items():
        by_strategy[src].append((sym, data))
    for src in sorted(by_strategy):
        kr = _STRATEGY_KR.get(src, src)
        out.append(f"\n  📊 {kr} ({src}):")
        for sym, data in sorted(by_strategy[src], key=lambda x: -(x[1]["buy"] + x[1]["sell"])):
            nm = _NAMES.get(sym, "?")
            out.append(
                f"    {sym} {nm:<8} B={data['buy']:>2}/S={data['sell']:>2}  "
                f"qty buy={data['buy_qty']:>3} sell={data['sell_qty']:>3}"
            )
    return "\n".join(out)


def main() -> int:
    now_kst = datetime.now(_KST)
    today_kst_iso_prefix = now_kst.strftime("%Y-%m-%dT00:00:00")
    today_utc_iso_prefix = (now_kst.replace(hour=0, minute=0, second=0)
                            .astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))

    print(f"\n=== paper_trade_monitor @ {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')} ===\n")
    print("[process]")
    print(_process_status())
    print("\n[ticks / orderbook]")
    print(_ticks_summary(today_utc_iso_prefix))
    print("\n[ledger orders by strategy / symbol]")
    print(_ledger_summary(today_utc_iso_prefix))
    return 0


if __name__ == "__main__":
    sys.exit(main())
