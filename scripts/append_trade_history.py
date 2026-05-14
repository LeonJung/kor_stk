"""append_trade_history — 매매 결과를 trade_history.md 에 일자별 누적 append.

사용자 룰 (2026-05-14): 매매 결과 (날짜 / 시간대 / strategy / 종목 별 승률,
수익률, 수익량) 을 md 에 누적 기록.

cron 매일 20:35 KST (daily_report 직후) 자동 실행.

trade_review.sqlite + ledger 결합 분석. 한국어 strategy 이름 + 종목명.

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.append_trade_history
    PYTHONPATH=src .venv/bin/python -m scripts.append_trade_history --date 2026-05-14
"""

from __future__ import annotations

import argparse
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


def _time_bucket(dt: datetime) -> str:
    """KST 시간대 분류 — 사용자 룰 feedback_market_timing."""
    t = dt.astimezone(_KST).time()
    h, m = t.hour, t.minute
    hhmm = h * 60 + m
    if hhmm < 9 * 60:
        return "장전"
    if hhmm < 9 * 60 + 25:
        return "09:00-09:25 시초모멘텀"
    if hhmm < 9 * 60 + 50:
        return "09:25-09:50 단타핫존"
    if hhmm < 10 * 60:
        return "09:50-10:00"
    if hhmm < 13 * 60 + 30:
        return "10:00-13:30 신규진입조심"
    if hhmm < 15 * 60 + 30:
        return "13:30-15:30 종가/오후"
    if hhmm < 16 * 60:
        return "15:30-16:00 시간외 단일가전"
    return "16:00+ 시간외/미장 leading"


def _load_today_reviews(db: Path, target_date) -> list[dict]:
    if not db.exists():
        return []
    start = datetime.combine(target_date, datetime.min.time(), _KST)
    end = start + timedelta(days=1)
    conn = sqlite3.connect(str(db))
    try:
        rows = list(conn.execute(
            "SELECT strategy, symbol, entry_ts, entry_price, "
            "exit_ts, exit_price, pnl_krw, exit_reason, "
            "entry_note, exit_note, macro_score_at_entry "
            "FROM trade_reviews WHERE exit_ts >= ? AND exit_ts < ? "
            "ORDER BY exit_ts",
            (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
        ))
    finally:
        conn.close()
    cols = ["strategy", "symbol", "entry_ts", "entry_price",
            "exit_ts", "exit_price", "pnl_krw", "exit_reason",
            "entry_note", "exit_note", "macro"]
    return [dict(zip(cols, row, strict=True)) for row in rows]


def _format_section(target_date, reviews: list[dict]) -> str:
    if not reviews:
        return (f"\n## {target_date.isoformat()} (KST)\n"
                f"\n  (매매 데이터 없음 — paper_trade 미운영 또는 review_log 누락)\n")
    out = [f"\n## {target_date.isoformat()} (KST)\n"]
    total = sum(r["pnl_krw"] for r in reviews)
    out.append(f"**Total**: {len(reviews)} trades / {total:+,} KRW\n")

    # --- 1. Strategy 별 ---
    out.append("### Strategy 별 승률 / 수익률 / 수익량\n")
    out.append("| 전략 | n | wins | win% | mean_pnl | total_pnl |")
    out.append("|---|---|---|---|---|---|")
    by_s: dict[str, list[dict]] = defaultdict(list)
    for r in reviews:
        by_s[r["strategy"]].append(r)
    for s in sorted(by_s, key=lambda k: -sum(r["pnl_krw"] for r in by_s[k])):
        items = by_s[s]
        wins = sum(1 for r in items if r["pnl_krw"] > 0)
        wr = wins / len(items) * 100
        mean = int(statistics.mean(r["pnl_krw"] for r in items))
        total_s = sum(r["pnl_krw"] for r in items)
        out.append(f"| {_kr(s)} | {len(items)} | {wins} | {wr:.1f}% | "
                   f"{mean:+,} | {total_s:+,} |")

    # --- 2. 시간대 별 ---
    out.append("\n### 시간대 별 (entry 시각 기준)\n")
    out.append("| 시간대 | n | wins | win% | total_pnl |")
    out.append("|---|---|---|---|---|")
    by_b: dict[str, list[dict]] = defaultdict(list)
    for r in reviews:
        dt = datetime.fromisoformat(r["entry_ts"])
        by_b[_time_bucket(dt)].append(r)
    for b in sorted(by_b):
        items = by_b[b]
        wins = sum(1 for r in items if r["pnl_krw"] > 0)
        wr = wins / len(items) * 100
        total_b = sum(r["pnl_krw"] for r in items)
        out.append(f"| {b} | {len(items)} | {wins} | {wr:.1f}% | {total_b:+,} |")

    # --- 3. 종목 별 ---
    out.append("\n### 종목 별\n")
    out.append("| 종목 | name | n | wins | win% | total_pnl |")
    out.append("|---|---|---|---|---|---|")
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in reviews:
        by_sym[r["symbol"]].append(r)
    sym_sorted = sorted(by_sym, key=lambda k: -sum(r["pnl_krw"] for r in by_sym[k]))
    for sym in sym_sorted:
        items = by_sym[sym]
        wins = sum(1 for r in items if r["pnl_krw"] > 0)
        wr = wins / len(items) * 100
        total_sym = sum(r["pnl_krw"] for r in items)
        out.append(f"| {sym} | {_name(sym)} | {len(items)} | {wins} | "
                   f"{wr:.1f}% | {total_sym:+,} |")

    # --- 4. Strategy x 종목 매트릭스 ---
    out.append("\n### Strategy x 종목 매트릭스 (수익량)\n")
    by_ss: dict[tuple[str, str], int] = defaultdict(int)
    syms_set: set[str] = set()
    for r in reviews:
        by_ss[(r["strategy"], r["symbol"])] += r["pnl_krw"]
        syms_set.add(r["symbol"])
    syms = sorted(syms_set)
    strats = sorted(by_s)
    header = "| strategy \\ 종목 | " + " | ".join(syms) + " |"
    out.append(header)
    out.append("|" + "---|" * (len(syms) + 1))
    for s in strats:
        row = [_kr(s)]
        for sym in syms:
            pnl = by_ss.get((s, sym), 0)
            row.append(f"{pnl:+,}" if pnl else ".")
        out.append("| " + " | ".join(row) + " |")

    # --- 5. 최악 5 trades (회고) ---
    worst = sorted(reviews, key=lambda r: r["pnl_krw"])[:5]
    if worst:
        out.append("\n### Worst 5 trades — 회고\n")
        for r in worst:
            ent = r["entry_price"]
            ex = r["exit_price"]
            pct = (ex - ent) / ent * 100 if ent else 0
            macro = r.get("macro")
            ms = f"{macro:.2f}" if macro is not None else "?"
            out.append(
                f"- **{_kr(r['strategy'])}** {r['symbol']} {_name(r['symbol'])}: "
                f"entry={ent:,} → exit={ex:,} ({pct:+.2f}%) "
                f"pnl={r['pnl_krw']:+,} reason={r['exit_reason']} macro={ms}",
            )
    return "\n".join(out) + "\n"


def _ensure_md_header(md_path: Path) -> None:
    if md_path.exists():
        return
    md_path.write_text(
        "# Trade History\n\n"
        "매일 paper_trade 매매 결과 누적 (사용자 룰 2026-05-14).\n"
        "자동 생성: `scripts/append_trade_history.py` (cron 매일 20:35 KST).\n"
        "Source: `data/trade_review.sqlite`.\n\n"
        "포맷:\n"
        "- 날짜 / 시간대 / strategy / 종목 별 승률, 수익률, 수익량\n"
        "- 한국어 strategy 명 + 종목명\n"
        "- worst 5 회고 (사용자 룰 8.3c)\n",
        encoding="utf-8",
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="ISO date (default = today KST)")
    p.add_argument("--md", default="trade_history.md")
    args = p.parse_args()

    target = (datetime.fromisoformat(args.date).date()
              if args.date else datetime.now(_KST).date())

    md = Path(args.md)
    _ensure_md_header(md)

    reviews = _load_today_reviews(Path("data/trade_review.sqlite"), target)
    section = _format_section(target, reviews)

    # Check if this date already in md (idempotent)
    body = md.read_text(encoding="utf-8")
    marker = f"## {target.isoformat()} (KST)"
    if marker in body:
        # Replace existing section (between marker and next ## or EOF)
        import re
        pattern = re.compile(
            rf"(\n## {re.escape(target.isoformat())} \(KST\).*?)(?=\n## |\Z)",
            re.DOTALL,
        )
        body = pattern.sub(section.rstrip() + "\n", body)
    else:
        body = body.rstrip() + "\n" + section

    md.write_text(body, encoding="utf-8")
    print(f"trade_history.md updated for {target.isoformat()}: "
          f"{len(reviews)} trades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
