"""Full portfolio end-to-end backtest — 11 strategies 동시 + TickReplay.

휴장일에도 즉시 실행 가능. configs/sample_portfolio.yaml 의 모든 strategy 를
load → 합성 시나리오 (Tick + Bar + Event 시퀀스) 를 TickReplayDriver 로
replay → per-strategy PnL CLI dashboard 출력.

시나리오는 다음 매매 패턴들을 동시에 발화:
- A 짝꿍 (LimitUpReached → follower entry/exit)
- G 시초가 모멘텀 (09:00 open → 09:05 surge)
- I 도지 종가베팅 (15:25 doji → 다음날 시초가 청산)
- F VWAP 회귀 (가격 deep dip + volume spike)
- J 수급 추적 (ProgramFlowEnter + ForeignNetBuy streak)
- K 바닥 거래량 (SixtyDayLow event)

실행::

    PYTHONPATH=src .venv/bin/python -m examples.full_portfolio_backtest
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from ks_ws.backtest.tick_replay import TickReplayDriver
from ks_ws.domain import Bar, OrderBook, OrderBookLevel, Tick
from ks_ws.events import (
    DojiCandle,
    ForeignNetBuy,
    GapUp,
    LimitUpBroken,
    LimitUpReached,
    OrderbookImbalance,
    ProgramFlowEnter,
    ProgramFlowExit,
    SixtyDayLow,
    VolumeSpike,
)
from ks_ws.strategies.config import load_portfolio
from ks_ws.storage.strategy_pnl import aggregate_strategy_pnl

_KST = ZoneInfo("Asia/Seoul")


def kst(year: int, month: int, day: int, hour: int = 9, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=_KST).astimezone(UTC)


def build_scenario():
    """Synthetic mixed scenario covering as many strategies as possible."""
    items: list = []

    # Day 1 — 2026-05-11 (Mon)
    d = (2026, 5, 11)

    # --- 09:00-09:50 OpeningMomentum window
    items.append(Tick(symbol="A005930", timestamp=kst(*d, 9, 0), price=70000, volume=1000))
    # 09:05 surge +6% → entry
    items.append(Tick(symbol="A005930", timestamp=kst(*d, 9, 5), price=74200, volume=5000))
    # 09:10 +3% from entry → take-profit
    items.append(Tick(symbol="A005930", timestamp=kst(*d, 9, 10), price=76450, volume=3000))

    # --- 09:15 짝꿍 매매: 위메이드맥스 leader → 위메이드 follower
    items.append(LimitUpReached(symbol="WMM", timestamp=kst(*d, 9, 15), limit_up_price=13000, prev_close=10000))
    items.append(Tick(symbol="WM", timestamp=kst(*d, 9, 16), price=10000, volume=100))
    items.append(Tick(symbol="WM", timestamp=kst(*d, 9, 18), price=10260, volume=200))  # +2.6% TP

    # --- 09:25 짝꿍 손절 케이스: another leader → broken
    items.append(LimitUpReached(symbol="LDR2", timestamp=kst(*d, 9, 25), limit_up_price=26000, prev_close=20000))
    items.append(Tick(symbol="FOL2", timestamp=kst(*d, 9, 26), price=15000, volume=100))  # captures entry
    items.append(Tick(symbol="FOL2", timestamp=kst(*d, 9, 27), price=14700, volume=100))  # price drops first
    items.append(LimitUpBroken(symbol="LDR2", timestamp=kst(*d, 9, 28), limit_up_price=26000, current_price=24500))

    # --- 14:00 ProgramFlowEnter + ForeignNetBuy streak (J entry)
    items.append(ProgramFlowEnter(symbol="A035720", timestamp=kst(*d, 14, 0), delta_krw=3_000_000_000, window_seconds=300))
    items.append(ForeignNetBuy(symbol="A035720", timestamp=kst(*d, 14, 5), delta_krw=1_500_000_000, window_seconds=300))
    items.append(ForeignNetBuy(symbol="A035720", timestamp=kst(*d, 14, 10), delta_krw=1_200_000_000, window_seconds=300))
    items.append(ForeignNetBuy(symbol="A035720", timestamp=kst(*d, 14, 15), delta_krw=1_000_000_000, window_seconds=300))  # streak=3 → BUY
    items.append(Tick(symbol="A035720", timestamp=kst(*d, 14, 16), price=50000, volume=100))

    # --- 15:25 도지 캔들 (ClosingBet entry)
    items.append(DojiCandle(symbol="A035420", timestamp=kst(*d, 15, 25), body_pct=0.1, range_pct=2.0, direction_hint="neutral"))
    items.append(Tick(symbol="A035420", timestamp=kst(*d, 15, 26), price=200000, volume=100))

    # --- 15:28 VolumeSpike + OrderbookImbalance + GapUp events for reference strategies
    items.append(VolumeSpike(symbol="A005930", timestamp=kst(*d, 15, 28), multiplier=4.0, window_seconds=60))
    items.append(OrderbookImbalance(symbol="A005930", timestamp=kst(*d, 15, 29), bid_to_ask_ratio=3.0, levels_used=5))
    items.append(GapUp(symbol="A000660", timestamp=kst(*d, 15, 30), gap_pct=6.0))

    # --- 15:30 SixtyDayLow (K entry)
    items.append(SixtyDayLow(
        symbol="A035720", timestamp=kst(*d, 15, 30),
        low_price=48000, current_price=49000, band_pct=2.1, volume_multiplier=4.0,
    ))

    # --- ProgramFlowExit (J exit) at 15:35
    items.append(Tick(symbol="A035720", timestamp=kst(*d, 15, 35), price=51000, volume=100))
    items.append(ProgramFlowExit(symbol="A035720", timestamp=kst(*d, 15, 36), delta_krw=-2_000_000_000, window_seconds=300))

    # Day 2 — 2026-05-12 (Tue)
    d2 = (2026, 5, 12)

    # ClosingBet next-day exit: A035420 시초가 +2.5% → take-profit
    items.append(Tick(symbol="A035420", timestamp=kst(*d2, 9, 0), price=200000, volume=100))  # captures entry_price
    items.append(Tick(symbol="A035420", timestamp=kst(*d2, 9, 5), price=205000, volume=100))  # +2.5% > 2.0% TP

    # K BottomVolumeSpike: A035720 +7% → take-profit
    items.append(Tick(symbol="A035720", timestamp=kst(*d2, 9, 10), price=53000, volume=100))  # +8% from 49000 entry

    return items


def main() -> int:
    print("=== Loading portfolio ===")
    strategies, allocator = load_portfolio("configs/sample_portfolio.yaml")
    for s in strategies:
        print(f"  - {type(s).__name__:35s} weight={allocator.weight_for(s.name)}")

    print()
    print("=== Building synthetic scenario ===")
    items = build_scenario()
    print(f"  {len(items)} items spanning two trading days")

    print()
    print("=== Running TickReplay ===")
    with TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "portfolio.sqlite"
        with TickReplayDriver(items, strategies, allocator=allocator, ledger_path=ledger_path) as driver:
            result = driver.run()
        print(f"  processed={result.items_processed}  intents={result.total_intents}  fills={len(result.fills)}")

        print()
        print("=== Per-strategy PnL ===")
        if not result.strategy_pnl:
            print("  (no completed round-trips)")
            return 0
        rows = sorted(result.strategy_pnl.values(), key=lambda s: s.realized_pnl_krw, reverse=True)
        print(f"  {'strategy':<28} {'trades':>6} {'win%':>5} {'avg+':>10} {'avg-':>10} {'total':>12} {'expect':>10}")
        print("  " + "─" * 92)
        for s in rows:
            print(
                f"  {s.strategy:<28} {s.trades:>6d} {s.win_rate*100:>4.0f}% "
                f"{s.avg_win_krw:>10,.0f} {s.avg_loss_krw:>10,.0f} "
                f"{s.realized_pnl_krw:>12,.0f} {s.expectancy_krw:>+10,.0f}"
            )
        print()
        total = sum(s.realized_pnl_krw for s in rows)
        total_trades = sum(s.trades for s in rows)
        print(f"  TOTAL {total_trades:>32d} trades, realized PnL = {total:>+12,.0f} KRW")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
