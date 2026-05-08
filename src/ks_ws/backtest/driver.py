"""Backtest driver — replay historical Bars through the same Runtime that
runs live. Identical Strategy / Allocator code; no special-cased backtest
logic anywhere downstream.

Fill model: when a Strategy emits an OrderIntent at time T for symbol X,
the driver buffers it and fills it at the close of X's *next* bar after T,
optionally with bps slippage applied (positive on buys, negative on
sells). Avoids lookahead and captures natural decision-to-execution
latency.

Costs (KRX retail defaults):
- ``commission_bps`` (default 1.5 bps = 0.015%): brokerage fee, both sides.
- ``sell_tax_bps`` (default 18 bps = 0.18%): KRX 거래세 + 농어촌특별세
  combined, sells only.
Costs are deducted from cash and recorded on each Trade. Realized PnL
on a sell is net of (commission + tax) and the matching cost basis is
inflated by buy-side commission via the running average — so backtest
PnL reflects what would actually land in the account.

Cross-symbol intents simply wait until their target symbol's next bar
arrives. An intent for a symbol that never appears again sits unfilled —
the result reports the count.

Shorting is disallowed: a SELL intent is filled only up to the current
position; the rest is discarded.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ks_ws.bus import EventBus, Subscription
from ks_ws.domain import Bar, OrderIntent, Side
from ks_ws.market.hub import MockMarketDataHub
from ks_ws.risk import Risk
from ks_ws.runtime import Runtime
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy

log = logging.getLogger("ks_ws.backtest")


@dataclass
class Position:
    quantity: int = 0
    average_cost: float = 0.0  # KRW per share

    def add(self, qty: int, price: int) -> None:
        if qty <= 0:
            return
        total_cost = self.average_cost * self.quantity + price * qty
        self.quantity += qty
        self.average_cost = total_cost / self.quantity

    def remove(self, qty: int, price: int) -> tuple[int, int]:
        """Returns (sold_qty, realized_pnl_krw). Caps at current position."""
        if qty <= 0 or self.quantity == 0:
            return 0, 0
        sold = min(qty, self.quantity)
        realized = round((price - self.average_cost) * sold)
        self.quantity -= sold
        if self.quantity == 0:
            self.average_cost = 0.0
        return sold, realized


@dataclass
class Trade:
    timestamp: datetime
    symbol: str
    side: Side
    quantity: int
    price: int
    realized_pnl_krw: int = 0  # nonzero only on sells that close against a position
    commission_krw: int = 0
    tax_krw: int = 0  # sells only


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl_krw: int = 0
    cash_krw: int = 0
    bars_processed: int = 0
    last_prices: dict[str, int] = field(default_factory=dict)
    unfilled_intents: int = 0
    total_commission_krw: int = 0
    total_tax_krw: int = 0

    @property
    def total_costs_krw(self) -> int:
        return self.total_commission_krw + self.total_tax_krw

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def total_buys(self) -> int:
        return sum(1 for t in self.trades if t.side == Side.BUY)

    @property
    def total_sells(self) -> int:
        return sum(1 for t in self.trades if t.side == Side.SELL)

    @property
    def winning_sells(self) -> int:
        return sum(1 for t in self.trades if t.side == Side.SELL and t.realized_pnl_krw > 0)

    @property
    def losing_sells(self) -> int:
        return sum(1 for t in self.trades if t.side == Side.SELL and t.realized_pnl_krw < 0)

    @property
    def win_rate(self) -> float:
        sells = self.total_sells
        return self.winning_sells / sells if sells else 0.0

    @property
    def unrealized_pnl_krw(self) -> int:
        total = 0
        for symbol, pos in self.positions.items():
            if pos.quantity == 0:
                continue
            last = self.last_prices.get(symbol)
            if last is None:
                continue
            total += round((last - pos.average_cost) * pos.quantity)
        return total

    @property
    def total_pnl_krw(self) -> int:
        return self.realized_pnl_krw + self.unrealized_pnl_krw


class BacktestDriver:
    def __init__(
        self,
        bars: Iterable[Bar],
        runtime: Runtime,
        bus: EventBus,
        hub: MockMarketDataHub,
        *,
        starting_cash_krw: int = 100_000_000,
        risk: Risk | None = None,
        commission_bps: float = 1.5,
        sell_tax_bps: float = 18.0,
        slippage_bps: float = 0.0,
    ) -> None:
        self._bars = list(bars)
        self._runtime = runtime
        self._bus = bus
        self._hub = hub
        self._risk = risk
        self.commission_bps = commission_bps
        self.sell_tax_bps = sell_tax_bps
        self.slippage_bps = slippage_bps
        self._intent_sub: Subscription[OrderIntent] | None = None
        self._pending: dict[str, list[OrderIntent]] = defaultdict(list)
        self._result = BacktestResult(cash_krw=starting_cash_krw)

    @classmethod
    def from_strategies(
        cls,
        bars: Iterable[Bar],
        strategies: Iterable[Strategy],
        *,
        allocator: Allocator | None = None,
        starting_cash_krw: int = 100_000_000,
        risk: Risk | None = None,
        commission_bps: float = 1.5,
        sell_tax_bps: float = 18.0,
        slippage_bps: float = 0.0,
    ) -> "BacktestDriver":
        """Convenience: build the bus / hub / runtime around a strategy list."""
        bus = EventBus()
        hub = MockMarketDataHub(bus)
        rt = Runtime(bus, strategies, allocator or Allocator())
        return cls(
            bars,
            rt,
            bus,
            hub,
            starting_cash_krw=starting_cash_krw,
            risk=risk,
            commission_bps=commission_bps,
            sell_tax_bps=sell_tax_bps,
            slippage_bps=slippage_bps,
        )

    def run(self) -> BacktestResult:
        self._runtime.setup()
        self._intent_sub = self._bus.subscribe(OrderIntent)
        try:
            sorted_bars = sorted(self._bars, key=lambda b: b.timestamp)
            for bar in sorted_bars:
                # Fill any intents waiting for THIS symbol against current bar's close.
                # This realizes the next-bar-after-decision fill model.
                self._fill_pending_for_symbol(bar)
                self._result.last_prices[bar.symbol] = bar.close
                self._hub.feed_bar(bar)
                self._runtime.step()
                self._collect_new_intents()
                self._result.bars_processed += 1
        finally:
            self._intent_sub.close()
            self._intent_sub = None
        self._result.unfilled_intents = sum(len(v) for v in self._pending.values())
        return self._result

    def _collect_new_intents(self) -> None:
        assert self._intent_sub is not None
        while self._intent_sub.qsize() > 0:
            try:
                intent = self._intent_sub.get_nowait()
            except StopAsyncIteration:
                break
            self._pending[intent.symbol].append(intent)

    def _fill_pending_for_symbol(self, bar: Bar) -> None:
        intents = self._pending.pop(bar.symbol, [])
        for intent in intents:
            self._fill(intent, bar)

    def _slipped_fill_price(self, side: Side, base_price: int) -> int:
        """Apply bps slippage in the unfavorable direction. Buyers pay
        more, sellers receive less."""
        if self.slippage_bps == 0:
            return base_price
        adj = base_price * self.slippage_bps / 10_000
        if side == Side.BUY:
            return round(base_price + adj)
        return round(base_price - adj)

    def _fill(self, intent: OrderIntent, bar: Bar) -> None:
        fill_price = self._slipped_fill_price(intent.side, bar.close)
        pos = self._result.positions.setdefault(intent.symbol, Position())

        # Optional risk gate. Treats the entire backtest run as a single
        # trading day for the daily-loss circuit breaker.
        if self._risk is not None:
            approved = self._risk.check(
                intent,
                current_position=pos.quantity,
                realized_pnl_today_krw=self._result.realized_pnl_krw,
            )
            if approved is None:
                return
            intent = approved

        gross = fill_price * intent.quantity
        commission = round(gross * self.commission_bps / 10_000)

        if intent.side == Side.BUY:
            pos.add(intent.quantity, fill_price)
            self._result.cash_krw -= gross + commission
            self._result.total_commission_krw += commission
            self._result.trades.append(
                Trade(
                    timestamp=bar.timestamp,
                    symbol=intent.symbol,
                    side=Side.BUY,
                    quantity=intent.quantity,
                    price=fill_price,
                    commission_krw=commission,
                )
            )
            return

        # SELL
        sold, realized_gross = pos.remove(intent.quantity, fill_price)
        if sold == 0:
            return
        gross_proceeds = fill_price * sold
        commission_sell = round(gross_proceeds * self.commission_bps / 10_000)
        tax = round(gross_proceeds * self.sell_tax_bps / 10_000)
        net_realized = realized_gross - commission_sell - tax
        self._result.cash_krw += gross_proceeds - commission_sell - tax
        self._result.realized_pnl_krw += net_realized
        self._result.total_commission_krw += commission_sell
        self._result.total_tax_krw += tax
        self._result.trades.append(
            Trade(
                timestamp=bar.timestamp,
                symbol=intent.symbol,
                side=Side.SELL,
                quantity=sold,
                price=fill_price,
                realized_pnl_krw=net_realized,
                commission_krw=commission_sell,
                tax_krw=tax,
            )
        )
