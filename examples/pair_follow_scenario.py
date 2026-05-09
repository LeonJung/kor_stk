"""End-to-end synthetic scenario for PairFollow + OpeningMomentum.

휴장일 (오늘 = 토요일) 에도 돌릴 수 있는 합성 tick scenario. 짝꿍 + 09:00
모멘텀 두 strategy 를 EventBus + Runtime + Allocator 로 묶어 OrderIntent 가
정상 흐르는지 + per-strategy PnL 집계까지 확인한다.

흐름:
1. PairFollow + OpeningMomentum 두 strategy 등록
2. 합성 tick / event 시퀀스 publish
3. OrderIntent 들 ledger 기록 (mock fill price = tick price)
4. aggregate_strategy_pnl 출력

실행::

    .venv/bin/python -m examples.pair_follow_scenario
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from ks_ws.bus import EventBus
from ks_ws.domain import OrderIntent, Side, Tick
from ks_ws.events import LimitUpBroken, LimitUpReached
from ks_ws.orders import SubmittedOrder
from ks_ws.runtime import Runtime
from ks_ws.storage.ledger import Ledger
from ks_ws.storage.strategy_pnl import aggregate_strategy_pnl
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.opening_momentum import OpeningMomentumStrategy
from ks_ws.strategies.pair_follow import PairFollowStrategy

_KST = ZoneInfo("Asia/Seoul")


def kst(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 5, 11, hour, minute, second, tzinfo=_KST).astimezone(UTC)


def main() -> None:
    bus = EventBus()

    # Strategies — PairFollow주력 + OpeningMomentum (TimeWindowGate wrapped)
    pair = PairFollowStrategy(
        pairs={"LEADER": "FOLLOW"},
        take_profit_pct=2.5,
        stop_loss_pct=1.5,
        hold_timeout_seconds=300,
        flat_timeout_seconds=60,
    )
    # OpeningMomentum 자체가 entry_window 를 enforce 하므로 별도 gate wrap 불필요.
    opening = OpeningMomentumStrategy(
        watchlist={"OPEN1"}, surge_pct=5.0, take_profit_pct=3.0,
        entry_window_kst=(time(9, 3), time(9, 25)),
    )

    allocator = Allocator(max_position_per_symbol=10)
    runtime = Runtime(bus, [pair, opening], allocator)
    opening_raw = opening  # alias for reset call below
    runtime.setup()

    # Ledger to record orders/fills (mock fills at tick price)
    with TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "ledger.sqlite")
        intents_sub = bus.subscribe(OrderIntent)

        def drain_and_fill(fill_price_for_symbol: dict[str, int]) -> None:
            runtime.step()
            while intents_sub.qsize() > 0:
                intent: OrderIntent = intents_sub.get_nowait()
                order_id = f"o-{len(ledger.list_orders()) + 1}"
                submitted = SubmittedOrder(
                    order_id=order_id, intent=intent, submitted_at=intent.timestamp
                )
                ledger.record_order(submitted)
                fill_price = fill_price_for_symbol.get(intent.symbol, 0)
                ledger.apply_fill(
                    order_id=order_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=intent.quantity,
                    price=fill_price,
                )
                print(
                    f"  → {intent.side.value:4s} {intent.symbol:6s} qty={intent.quantity} "
                    f"@ {fill_price:>7,} (sources={intent.sources})"
                )

        # Scenario A — PairFollow ----------------------------------------
        print("\n=== Scenario A: 짝꿍 매매 (LEADER 상한가 → FOLLOW 진입 → 익절) ===")
        # 09:10 LEADER hits limit-up
        bus.publish(
            LimitUpReached(
                symbol="LEADER",
                timestamp=kst(9, 10),
                limit_up_price=13000,
                prev_close=10000,
            )
        )
        drain_and_fill({"FOLLOW": 10000})
        # 09:11 first FOLLOW tick → captures entry
        bus.publish(Tick(symbol="FOLLOW", timestamp=kst(9, 11), price=10000, volume=10))
        drain_and_fill({})
        # 09:13 FOLLOW +2.6% → take-profit triggers
        bus.publish(Tick(symbol="FOLLOW", timestamp=kst(9, 13), price=10260, volume=10))
        drain_and_fill({"FOLLOW": 10260})

        # Scenario B — PairFollow 손절 -----------------------------------
        print("\n=== Scenario B: 짝꿍 매매 (LEADER 다시 상한가 → FOLLOW 손절) ===")
        bus.publish(
            LimitUpReached(
                symbol="LEADER",
                timestamp=kst(9, 20),
                limit_up_price=13000,
                prev_close=10000,
            )
        )
        drain_and_fill({"FOLLOW": 11000})
        bus.publish(Tick(symbol="FOLLOW", timestamp=kst(9, 21), price=11000, volume=10))
        drain_and_fill({})
        # FOLLOW -1.6%
        bus.publish(Tick(symbol="FOLLOW", timestamp=kst(9, 22), price=10825, volume=10))
        drain_and_fill({"FOLLOW": 10825})

        # Scenario C — OpeningMomentum (TimeWindowGate 검증) -----------
        print("\n=== Scenario C: OpeningMomentum (09:00 open, 09:05 +5% → entry → 익절) ===")
        # 09:00 first tick - capture open price (BUT outside gate, so no entry yet)
        bus.publish(Tick(symbol="OPEN1", timestamp=kst(9, 0), price=10000, volume=10))
        drain_and_fill({})  # gate blocks; no signal
        # 09:05 within gate, +5% surge
        bus.publish(Tick(symbol="OPEN1", timestamp=kst(9, 5), price=10500, volume=10))
        drain_and_fill({"OPEN1": 10500})
        # 09:10 +3% from entry = take-profit
        bus.publish(Tick(symbol="OPEN1", timestamp=kst(9, 10), price=10815, volume=10))
        drain_and_fill({"OPEN1": 10815})

        # Scenario D — Gate 차단 테스트 -----------------------------------
        print("\n=== Scenario D: OpeningMomentum after 09:25 → gate blocks entry ===")
        # OPEN1 already opened earlier; reset to test fresh gate behavior
        opening_raw.reset_for_new_session()
        bus.publish(Tick(symbol="OPEN1", timestamp=kst(9, 30), price=10000, volume=10))
        drain_and_fill({})  # gate blocks, no entry even at +5%
        bus.publish(Tick(symbol="OPEN1", timestamp=kst(9, 31), price=10500, volume=10))
        drain_and_fill({})

        # PnL aggregation -------------------------------------------------
        print("\n=== Per-strategy PnL ===")
        stats = aggregate_strategy_pnl(ledger)
        for name, s in stats.items():
            print(
                f"  {name:20s} trades={s.trades:2d}  win_rate={s.win_rate:.0%}  "
                f"pnl={s.realized_pnl_krw:>10,.0f} KRW  expectancy={s.expectancy_krw:>+8,.0f}"
            )

        ledger.close()


if __name__ == "__main__":
    main()
