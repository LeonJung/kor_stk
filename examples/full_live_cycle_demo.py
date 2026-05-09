"""Full live cycle demo — 가상 1주일 매매 → JournalSystem 회고 →
SelfImprovingWeightUpdater 가중치 조정 → strategy_pnl 출력.

사용자 D-9 결정 ("Claude 가 매매 결과 회고") 의 자동화 보조 cycle 을
end-to-end 로 보여준다. 실 라이브 모드에서는 LiveExecutor 가 KisOrderRouter
를 통해 모의/실 KIS 모의서버에 주문 보내고, 본 demo 는 합성 시나리오로
동일 흐름 재현.

흐름:
1. configs/sample_portfolio.yaml load (11 strategies)
2. EnhancedRisk = Risk + LossResponseProtocol + PsychologyGuard 셋업
3. 5 거래일 합성 시나리오 (월~금) → TickReplay
4. fill 마다 JournalSystem.record + LossResponse/Psychology 자동 record
5. 주말 종료: aggregate_strategy_pnl + JournalSystem 상태 출력
6. SelfImprovingWeightUpdater 가 expectancy 보고 weight 자동 조정
7. 다음 주에 새 weight 로 다시 시작 가능 (시연 포인트)

휴장일 (오늘) 즉시 실행 가능. 평일에는 KIS API 활성화 추가.

실행::

    PYTHONPATH=src .venv/bin/python -m examples.full_live_cycle_demo
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from ks_ws.backtest.tick_replay import TickReplayDriver
from ks_ws.domain import OrderIntent, Side, Tick
from ks_ws.events import (
    DojiCandle,
    ForeignNetBuy,
    LimitUpReached,
    ProgramFlowEnter,
    SixtyDayLow,
)
from ks_ws.loss_response import LossResponseProtocol
from ks_ws.psychology import PsychologyGuard
from ks_ws.risk import EnhancedRisk, Risk
from ks_ws.storage.journal import JournalSystem
from ks_ws.storage.strategy_pnl import aggregate_strategy_pnl
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.config import load_portfolio
from ks_ws.strategies.self_improving import SelfImprovingWeightUpdater

_KST = ZoneInfo("Asia/Seoul")


def kst(year: int, month: int, day: int, hour: int = 9, minute: int = 0):
    return datetime(year, month, day, hour, minute, tzinfo=_KST).astimezone(UTC)


def synthetic_week():
    """Build a 5-trading-day synthetic scenario covering many strategies.

    Each weekday has a small set of mixed events to trigger different
    strategies' entry/exit. Designed to produce a non-trivial PnL spread
    across strategies for the SelfImprovingWeightUpdater to react to.
    """
    items = []

    # Mon 5/11 — opening momentum + 짝꿍
    d = (2026, 5, 11)
    items += [
        Tick(symbol="A005930", timestamp=kst(*d, 9, 0), price=70000, volume=1000),
        Tick(symbol="A005930", timestamp=kst(*d, 9, 5), price=74200, volume=5000),
        Tick(symbol="A005930", timestamp=kst(*d, 9, 10), price=76450, volume=3000),
        LimitUpReached(symbol="WMM", timestamp=kst(*d, 9, 15), limit_up_price=13000, prev_close=10000),
        Tick(symbol="WM", timestamp=kst(*d, 9, 16), price=10000, volume=100),
        Tick(symbol="WM", timestamp=kst(*d, 9, 18), price=10260, volume=200),
    ]
    # Tue 5/12 — 짝꿍 손절 사례
    d = (2026, 5, 12)
    items += [
        LimitUpReached(symbol="LDR2", timestamp=kst(*d, 9, 25), limit_up_price=26000, prev_close=20000),
        Tick(symbol="FOL2", timestamp=kst(*d, 9, 26), price=15000, volume=100),
        Tick(symbol="FOL2", timestamp=kst(*d, 9, 27), price=14700, volume=100),
        Tick(symbol="FOL2", timestamp=kst(*d, 9, 28), price=14600, volume=100),  # SL
    ]
    # Wed 5/13 — 수급 추적 entry + exit
    d = (2026, 5, 13)
    items += [
        ProgramFlowEnter(symbol="A035720", timestamp=kst(*d, 14, 0), delta_krw=3_000_000_000, window_seconds=300),
        ForeignNetBuy(symbol="A035720", timestamp=kst(*d, 14, 5), delta_krw=1_500_000_000, window_seconds=300),
        ForeignNetBuy(symbol="A035720", timestamp=kst(*d, 14, 10), delta_krw=1_200_000_000, window_seconds=300),
        ForeignNetBuy(symbol="A035720", timestamp=kst(*d, 14, 15), delta_krw=1_000_000_000, window_seconds=300),
        Tick(symbol="A035720", timestamp=kst(*d, 14, 16), price=50000, volume=100),
        Tick(symbol="A035720", timestamp=kst(*d, 14, 30), price=51000, volume=100),
        ForeignNetBuy(symbol="A035720", timestamp=kst(*d, 15, 30), delta_krw=-2_000_000_000, window_seconds=300),
    ]
    # Thu 5/14 — 도지 종가 베팅 entry + exit (Fri morning)
    d = (2026, 5, 14)
    items += [
        DojiCandle(symbol="A035420", timestamp=kst(*d, 15, 25), body_pct=0.1, range_pct=2.0, direction_hint="neutral"),
        Tick(symbol="A035420", timestamp=kst(*d, 15, 26), price=200000, volume=100),
    ]
    # Fri 5/15 — close out doji + 60-day low entry
    d = (2026, 5, 15)
    items += [
        Tick(symbol="A035420", timestamp=kst(*d, 9, 0), price=200000, volume=100),
        Tick(symbol="A035420", timestamp=kst(*d, 9, 5), price=205000, volume=100),  # +2.5% TP
        SixtyDayLow(
            symbol="A000270", timestamp=kst(*d, 10, 0),
            low_price=80000, current_price=82000, band_pct=2.5, volume_multiplier=4.0,
        ),
        Tick(symbol="A000270", timestamp=kst(*d, 10, 30), price=87800, volume=100),  # +7% TP
    ]
    return items


def main() -> int:
    print("=" * 70)
    print("FULL LIVE CYCLE DEMO — 1 week paper trading + reflection + improvement")
    print("=" * 70)

    print("\n--- 1) Loading portfolio ---")
    strategies, allocator = load_portfolio("configs/sample_portfolio.yaml")
    print(f"  {len(strategies)} strategies loaded")

    # Setup EnhancedRisk (LossResponseProtocol + PsychologyGuard auto-applied)
    loss = LossResponseProtocol(max_single_loss_krw=500_000)
    psy = PsychologyGuard()
    enhanced = EnhancedRisk(
        risk=Risk(max_position_per_symbol=100, daily_loss_limit_krw=10_000_000),
        loss_protocol=loss,
        psychology=psy,
    )
    print(f"  EnhancedRisk = base + LossProtocol + PsychologyGuard")
    print(f"    max_single_loss_krw=500K, daily_loss_limit_krw=10M")

    print("\n--- 2) Building 5-trading-day synthetic scenario ---")
    items = synthetic_week()
    print(f"  {len(items)} items spanning Mon-Fri")

    print("\n--- 3) Running TickReplay ---")
    with TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.sqlite"
        journal_path = Path(tmp) / "journal.sqlite"
        with TickReplayDriver(items, strategies, allocator=allocator, ledger_path=ledger_path) as driver:
            result = driver.run()
        print(f"  intents={result.total_intents}  fills={len(result.fills)}")

        print("\n--- 4) Per-strategy PnL (raw, before adjustment) ---")
        if not result.strategy_pnl:
            print("  (no completed round-trips this week)")
        rows = sorted(result.strategy_pnl.values(), key=lambda s: s.realized_pnl_krw, reverse=True)
        print(f"  {'strategy':<28} {'trades':>6} {'win%':>5} {'pnl':>12} {'expect':>10}")
        print("  " + "─" * 68)
        for s in rows:
            print(
                f"  {s.strategy:<28} {s.trades:>6d} {s.win_rate*100:>4.0f}% "
                f"{s.realized_pnl_krw:>12,.0f} {s.expectancy_krw:>+10,.0f}"
            )

        print("\n--- 5) JournalSystem — record reflections ---")
        journal = JournalSystem(journal_path)
        # Pair up fills: BUY → matching SELL, record one journal entry per round-trip
        fills_by_strategy: dict[str, list] = {}
        for intent, price in result.fills:
            for src in intent.sources or ("(unknown)",):
                fills_by_strategy.setdefault(src, []).append((intent, price))
        recorded = 0
        for strategy, lst in fills_by_strategy.items():
            opens: list[tuple[OrderIntent, int]] = []
            for intent, price in lst:
                if intent.side == Side.BUY:
                    opens.append((intent, price))
                else:  # SELL
                    if not opens:
                        continue
                    open_intent, open_price = opens.pop(0)
                    qty = min(open_intent.quantity, intent.quantity)
                    pnl = (price - open_price) * qty
                    journal.record(
                        symbol=intent.symbol,
                        strategy=strategy,
                        opened_at=open_intent.timestamp,
                        closed_at=intent.timestamp,
                        quantity=qty,
                        entry_price=open_price,
                        exit_price=price,
                        pnl_krw=pnl,
                    )
                    recorded += 1
        print(f"  {recorded} journal entries recorded (entry_reason / lesson 미기록 = Claude 회고 대기)")
        pending = journal.needs_reflection()
        print(f"  {len(pending)} entries need reflection (Claude review session 후 annotate)")

        print("\n--- 6) SelfImprovingWeightUpdater — adjust allocator weights ---")
        from ks_ws.storage.ledger import Ledger
        ledger = Ledger(ledger_path)
        try:
            updater = SelfImprovingWeightUpdater(
                ledger=ledger, learning_rate=0.2, normalize_by=10_000,
            )
            report = updater.update(allocator)
        finally:
            ledger.close()
        if not report.changes:
            print("  (no changes — no per-strategy PnL data)")
        else:
            print("  Weight adjustments:")
            for c in report.changes:
                arrow = "→" if c.new_weight != c.old_weight else "="
                print(
                    f"    {c.strategy:<28} {c.old_weight:.3f} {arrow} {c.new_weight:.3f}  "
                    f"(exp={c.expectancy_krw:>+10,.0f}  trades={c.trades})"
                )

        print("\n--- 7) Risk state at week end ---")
        print(f"  LossResponseProtocol.phase = {loss.phase}")
        print(f"  consecutive_losses = {loss.consecutive_losses}")
        print(f"  PsychologyGuard.global_fills = {len(psy._global_fills)}")

        journal.close()
    print("\n" + "=" * 70)
    print("Cycle complete. Next week: re-run with adjusted weights.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
