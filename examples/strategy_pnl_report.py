"""Per-strategy PnL CLI report — human-readable summary of ledger.sqlite.

용도: 가상돈 매매 또는 라이브 매매 후 strategy 별 성과 한 눈에 확인. 사용자
D-9 결정 ("Claude 가 매매 결과 회고") 의 첫 번째 단계 = 데이터 정리.

Usage::

    .venv/bin/python -m examples.strategy_pnl_report PATH/TO/ledger.sqlite

또는 옵션 없이 실행하면 default path (data/ledger.sqlite) 사용.

Output columns:
- strategy : strategy name (`Strategy.name`)
- trades   : 완료된 round-trip (FIFO 매칭) 횟수
- win_rate : 승률
- avg_win  : 평균 수익 KRW
- avg_loss : 평균 손실 KRW
- total    : 누적 realized PnL
- expect   : 거래당 기대값 (total / trades)
"""

from __future__ import annotations

import sys
from pathlib import Path

from ks_ws.storage.ledger import Ledger
from ks_ws.storage.strategy_pnl import aggregate_strategy_pnl


def _fmt_krw(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    n = int(abs(amount))
    if n >= 100_000_000:
        return f"{sign}{n/100_000_000:.2f}억"
    if n >= 10_000:
        return f"{sign}{n/10_000:.1f}만"
    return f"{sign}{n:,}"


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    path = Path(argv[0]) if argv else Path("data/ledger.sqlite")
    if not path.exists():
        print(f"ledger not found: {path}", file=sys.stderr)
        print("  hint: run examples/pair_follow_scenario.py or your live demo first.", file=sys.stderr)
        return 2

    ledger = Ledger(path)
    try:
        stats = aggregate_strategy_pnl(ledger)
    finally:
        ledger.close()

    if not stats:
        print(f"No completed round-trips in {path}.")
        return 0

    rows = sorted(stats.values(), key=lambda s: s.realized_pnl_krw, reverse=True)
    print()
    print(f"  Per-strategy PnL — {path}")
    print()
    headers = ("strategy", "trades", "wins", "loss", "win%", "avg+", "avg-", "total", "expect")
    print("  " + " ".join(_pad(h, w) for h, w in zip(headers, _COL_WIDTHS, strict=True)))
    print("  " + "─" * (sum(_COL_WIDTHS) + len(_COL_WIDTHS) - 1))
    total_pnl = 0.0
    total_trades = 0
    for s in rows:
        cells = (
            s.strategy,
            str(s.trades),
            str(s.wins),
            str(s.losses),
            f"{s.win_rate*100:.0f}%",
            _fmt_krw(s.avg_win_krw),
            _fmt_krw(s.avg_loss_krw),
            _fmt_krw(s.realized_pnl_krw),
            _fmt_krw(s.expectancy_krw),
        )
        total_pnl += s.realized_pnl_krw
        total_trades += s.trades
        print("  " + " ".join(_pad(c, w) for c, w in zip(cells, _COL_WIDTHS, strict=True)))
    print("  " + "─" * (sum(_COL_WIDTHS) + len(_COL_WIDTHS) - 1))
    summary = (
        "TOTAL", str(total_trades), "", "", "", "", "",
        _fmt_krw(total_pnl),
        _fmt_krw(total_pnl / total_trades) if total_trades else "—",
    )
    print("  " + " ".join(_pad(c, w) for c, w in zip(summary, _COL_WIDTHS, strict=True)))
    print()
    return 0


_COL_WIDTHS = (22, 6, 4, 4, 5, 9, 9, 11, 9)


def _pad(text: str, width: int) -> str:
    if len(text) >= width:
        return text[:width]
    return text + " " * (width - len(text))


if __name__ == "__main__":
    sys.exit(main())
