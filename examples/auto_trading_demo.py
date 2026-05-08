"""End-to-end auto-trading loop, dry-run by default.

Wires every piece together: KisMarketDataHub (HOT WS) → EventBus →
Runtime → Strategy → Allocator → OrderIntent → LiveExecutor → MockOrderRouter.

By default uses MockOrderRouter so no real orders hit KIS — strictly a
demo of the wiring. Pass --live to swap in KisOrderRouter (still mock
*account* via .env, but actual order submissions to KIS).

Run (during KRX market hours, dry):
    uv run examples/auto_trading_demo.py

Run (live submit to KIS mock account):
    uv run examples/auto_trading_demo.py --live
"""

import argparse
import asyncio
from datetime import UTC, datetime

from ks_ws.bus import EventBus
from ks_ws.config import get_settings
from ks_ws.domain import Side, Signal, Tick
from ks_ws.live import LiveExecutor
from ks_ws.market.hub import Tier
from ks_ws.market.kis_hub import KisMarketDataHub
from ks_ws.orders import KisOrderRouter, MockOrderRouter
from ks_ws.risk import Risk
from ks_ws.runtime import Runtime
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy


class _UpDownStrategy(Strategy):
    """Toy strategy — buy on first tick of a window, sell on the very next.
    Designed to generate alternating signals so the executor path is exercised
    without taking a meaningful position."""

    name = "up_down"

    def __init__(self) -> None:
        self._next_side: Side = Side.BUY

    def on_tick(self, tick: Tick) -> list[Signal]:
        side = self._next_side
        self._next_side = Side.SELL if side == Side.BUY else Side.BUY
        return [
            Signal(
                symbol=tick.symbol,
                side=side,
                confidence=0.05,  # tiny — Allocator builds a 1-share intent
                strategy=self.name,
                timestamp=tick.timestamp,
            )
        ]


async def run(symbol: str, duration_sec: int, live: bool) -> None:
    settings = get_settings()
    bus = EventBus()

    hub = KisMarketDataHub(bus, settings)
    hub.assign(symbol, Tier.HOT)

    runtime = Runtime(bus, [_UpDownStrategy()], Allocator(max_position_per_symbol=5))
    risk = Risk(max_position_per_symbol=5, daily_loss_limit_krw=1_000_000)
    router = KisOrderRouter(settings) if live else MockOrderRouter()
    executor = LiveExecutor(bus, risk, router)

    print(f"=== Auto-trading demo ({symbol}, {duration_sec}s, live={live}) ===")
    print(f"  router: {type(router).__name__}")
    print(
        f"  risk:   max_pos={risk.max_position_per_symbol}, "
        f"daily_loss={risk.daily_loss_limit_krw:,}\n"
    )

    await runtime.start()
    await executor.start()
    await hub.start()

    started_at = datetime.now(UTC)
    try:
        await asyncio.sleep(duration_sec)
    finally:
        await hub.stop()
        # Drain any in-flight intents before stopping the executor.
        for _ in range(10):
            await asyncio.sleep(0)
        await executor.stop()
        await runtime.stop()

    elapsed = (datetime.now(UTC) - started_at).total_seconds()
    print(f"\n=== Summary ({elapsed:.1f}s) ===")
    print(f"  positions:        {executor.positions}")
    print(f"  submitted orders: {len(executor.submitted)}")
    print(f"  rejected by risk: {len(executor.rejected_by_risk)}")
    print(f"  failed submits:   {len(executor.failed_submits)}")
    if executor.submitted:
        print("\n  most recent 5:")
        for o in executor.submitted[-5:]:
            print(
                f"    {o.submitted_at.astimezone().strftime('%H:%M:%S')} "
                f"{o.intent.side} {o.intent.quantity} {o.intent.symbol} "
                f"@ {o.intent.limit_price or 'MKT'} (id={o.order_id})"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Submit real orders to KIS mock account (otherwise MockOrderRouter)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.symbol, args.duration, args.live))


if __name__ == "__main__":
    main()
