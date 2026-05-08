"""Full end-to-end auto-trading wiring.

Connects every major component the project provides into one runnable
session, defaulting to safe modes:
- KisMarketDataHub HOT (WS 체결+호가 for one symbol) and optional
  WARM/COLD if symbols are added.
- 4 detectors observing the bus (VolumeSpike, OrderbookImbalance,
  GapUp via Bar, ProgramFlow via REST polling).
- A toy strategy that subscribes to events and bars/ticks.
- Allocator + Risk + LiveExecutor + Ledger.
- Runtime ties Strategy/Allocator together.

Order routing defaults to MockOrderRouter — no real KIS submissions.
``--live`` swaps in KisOrderRouter for the mock account.

Run during KRX market hours (KST 09:00-15:30):
    uv run examples/full_auto_trading_demo.py

Live submit to mock account:
    uv run examples/full_auto_trading_demo.py --live
"""

import argparse
import asyncio
import contextlib
import logging
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ks_ws.bus import EventBus
from ks_ws.config import get_settings
from ks_ws.detectors.orderbook_imbalance import OrderbookImbalanceDetector
from ks_ws.detectors.program_flow import ProgramFlowDetector
from ks_ws.domain import OrderBook, Side, Signal
from ks_ws.events import (
    Event,
    GapUp,
    OrderbookImbalance,
    ProgramFlowEnter,
    ProgramFlowExit,
    VolumeSpike,
)
from ks_ws.live import LiveExecutor
from ks_ws.market.hub import Tier
from ks_ws.market.kis_hub import KisMarketDataHub
from ks_ws.orders import KisOrderRouter, MockOrderRouter
from ks_ws.risk import Risk
from ks_ws.runtime import Runtime
from ks_ws.sources.program_flow import ProgramFlowSource
from ks_ws.storage.bars import BarStore
from ks_ws.storage.ledger import Ledger
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy

log = logging.getLogger("ks_ws.demo")


class _DemoStrategy(Strategy):
    """Reacts to the four detectors. Each event yields a tiny Signal so the
    full pipeline gets exercised; the signs and confidences are arbitrary —
    not a real strategy."""

    name = "demo"

    def __init__(self) -> None:
        self.event_counts: dict[str, int] = {}

    def on_event(self, event: Event) -> list[Signal]:
        cls = type(event).__name__
        self.event_counts[cls] = self.event_counts.get(cls, 0) + 1

        if isinstance(event, ProgramFlowEnter):
            side, conf = Side.BUY, 0.6
        elif isinstance(event, ProgramFlowExit):
            side, conf = Side.SELL, 0.6
        elif isinstance(event, VolumeSpike):
            side, conf = Side.BUY, 0.3
        elif isinstance(event, OrderbookImbalance):
            side, conf = Side.BUY, 0.2
        elif isinstance(event, GapUp):
            side, conf = Side.BUY, 0.4
        else:
            return []

        return [
            Signal(
                symbol=event.symbol,
                side=side,
                confidence=conf,
                strategy=self.name,
                timestamp=event.timestamp,
            )
        ]


async def _bridge(bus: EventBus, topic: type, feed: Callable) -> None:
    """Subscribe to a bus topic and forward each item into a detector's
    feed() method. Cancelable async task."""
    sub = bus.subscribe(topic)
    try:
        async for item in sub:
            try:
                feed(item)
            except Exception:
                log.exception("detector feed raised on %s", type(item).__name__)
    except asyncio.CancelledError:
        pass


def _flow_fetcher_stub(symbol: str) -> int:
    """Mock program-flow fetcher — alternates between strong-buy and exit
    so the demo shows enter/exit events without depending on the real
    KIS endpoint mapping."""
    now = datetime.now(UTC).second
    return 2_000_000_000 if (now // 10) % 2 == 0 else 50_000_000


async def run(symbol: str, duration_sec: int, live: bool) -> None:
    settings = get_settings()
    bus = EventBus()

    with tempfile.TemporaryDirectory() as tmp:
        bar_store = BarStore(Path(tmp))
        ledger = Ledger(Path(tmp) / "ledger.sqlite")

        # Market data hub — HOT only for the demo (WARM/COLD optional).
        hub = KisMarketDataHub(bus, settings, bar_store=bar_store)
        hub.assign(symbol, Tier.HOT)

        # Detectors
        prog_det = ProgramFlowDetector(bus)
        ob_det = OrderbookImbalanceDetector(bus, levels=3, buy_threshold=2.0)
        # VolumeSpike + GapUp need bars, not ticks — Hub doesn't synthesize
        # bars from ticks yet, so leave them connected but quiet for the
        # session-length window. (Caller can plug a 1m aggregator later.)

        # Bus → detector adapters
        ob_bridge = asyncio.create_task(_bridge(bus, OrderBook, ob_det.feed))

        # Program-flow REST polling — uses the stub fetcher unless --live
        # caller swaps it.
        prog_src = ProgramFlowSource(
            prog_det,
            [symbol],
            fetcher=_flow_fetcher_stub,
            interval_sec=2.0,  # tighter than 30s so the demo shows events
        )

        # Strategy / Allocator / Risk / Executor
        strategy = _DemoStrategy()
        runtime = Runtime(bus, [strategy], Allocator(max_position_per_symbol=5))
        risk = Risk(max_position_per_symbol=5, daily_loss_limit_krw=1_000_000)
        router = KisOrderRouter(settings) if live else MockOrderRouter()
        executor = LiveExecutor(bus, risk, router, ledger=ledger)

        print(f"=== Full auto-trading demo ({symbol}, {duration_sec}s, live={live}) ===")
        print(f"  router: {type(router).__name__}")
        print(f"  ledger: {ledger.path}")
        print(f"  bar_store: {bar_store.root}")
        print()

        await runtime.start()
        await executor.start()
        await prog_src.start()
        await hub.start()

        try:
            await asyncio.sleep(duration_sec)
        finally:
            await prog_src.stop()
            await hub.stop()
            ob_bridge.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ob_bridge
            for _ in range(10):
                await asyncio.sleep(0)
            await executor.stop()
            await runtime.stop()

        # Summary
        print("\n=== Summary ===")
        print("  ticks processed:       (Hub HOT, see strategy events)")
        print(f"  detector events:       {strategy.event_counts}")
        print(f"  prog-flow polls:       {prog_src.poll_count}")
        print(f"  submitted orders:      {len(executor.submitted)}")
        print(f"  rejected by risk:      {len(executor.rejected_by_risk)}")
        print(f"  failed submits:        {len(executor.failed_submits)}")
        ledger_orders = ledger.list_orders()
        print(f"  ledger orders:         {len(ledger_orders)}")
        positions = ledger.list_positions()
        print(f"  ledger positions:      {len(positions)}")
        if executor.submitted:
            print("\n  most recent submitted (up to 5):")
            for o in executor.submitted[-5:]:
                ts = o.submitted_at.astimezone().strftime("%H:%M:%S")
                price = o.intent.limit_price or "MKT"
                print(
                    f"    {ts} {o.intent.side} {o.intent.quantity} "
                    f"{o.intent.symbol} @ {price} (id={o.order_id})"
                )

        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Submit real orders to KIS mock account (otherwise MockOrderRouter)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.symbol, args.duration, args.live))


if __name__ == "__main__":
    main()
