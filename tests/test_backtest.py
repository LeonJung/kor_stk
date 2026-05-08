from datetime import UTC, datetime, timedelta

from ks_ws.backtest.driver import BacktestDriver, Position
from ks_ws.domain import Bar, Side, Signal
from ks_ws.risk import Risk
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy


def _bars_for(symbol: str, closes: list[int], start_ts: datetime | None = None) -> list[Bar]:
    """Synthetic bar series — flat OHLV around `close`, 1-second cadence."""
    start_ts = start_ts or datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    return [
        Bar(
            symbol=symbol,
            timestamp=start_ts + timedelta(seconds=i),
            timeframe="1s",
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1_000,
            value=c * 1_000,
        )
        for i, c in enumerate(closes)
    ]


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


def test_position_add_to_empty():
    p = Position()
    p.add(10, 100)
    assert p.quantity == 10
    assert p.average_cost == 100.0


def test_position_add_compounds_average_cost():
    p = Position()
    p.add(10, 100)
    p.add(10, 120)
    assert p.quantity == 20
    assert p.average_cost == 110.0


def test_position_remove_returns_sold_and_realized():
    p = Position()
    p.add(10, 100)
    sold, pnl = p.remove(5, 150)
    assert sold == 5
    assert pnl == 250  # (150 - 100) * 5
    assert p.quantity == 5


def test_position_remove_caps_at_current_quantity():
    p = Position()
    p.add(10, 100)
    sold, pnl = p.remove(50, 150)
    assert sold == 10
    assert pnl == 500
    assert p.quantity == 0
    assert p.average_cost == 0.0


def test_position_remove_from_empty_is_noop():
    p = Position()
    sold, pnl = p.remove(10, 100)
    assert sold == 0
    assert pnl == 0


# ---------------------------------------------------------------------------
# Test strategies
# ---------------------------------------------------------------------------


class _BuyOnFirstSellOnThird(Strategy):
    name = "buy_sell"

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def on_bar(self, bar: Bar) -> list[Signal]:
        c = self._counts.get(bar.symbol, 0) + 1
        self._counts[bar.symbol] = c
        if c == 1:
            return [
                Signal(
                    symbol=bar.symbol,
                    side=Side.BUY,
                    confidence=0.5,
                    strategy=self.name,
                    timestamp=bar.timestamp,
                )
            ]
        if c == 3:
            return [
                Signal(
                    symbol=bar.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    strategy=self.name,
                    timestamp=bar.timestamp,
                )
            ]
        return []


class _BuyEveryBar(Strategy):
    name = "always_buy"

    def on_bar(self, bar: Bar) -> list[Signal]:
        return [
            Signal(
                symbol=bar.symbol,
                side=Side.BUY,
                confidence=0.1,
                strategy=self.name,
                timestamp=bar.timestamp,
            )
        ]


class _SellOnceStrategy(Strategy):
    """Emits a single SELL on the first bar (no position yet → wasted intent)."""

    name = "sell_once"

    def __init__(self) -> None:
        self._sent = False

    def on_bar(self, bar: Bar) -> list[Signal]:
        if self._sent:
            return []
        self._sent = True
        return [
            Signal(
                symbol=bar.symbol,
                side=Side.SELL,
                confidence=1.0,
                strategy=self.name,
                timestamp=bar.timestamp,
            )
        ]


# ---------------------------------------------------------------------------
# BacktestDriver
# ---------------------------------------------------------------------------


def test_empty_bars_produces_empty_result():
    driver = BacktestDriver.from_strategies([], [_BuyEveryBar()])
    result = driver.run()
    assert result.total_trades == 0
    assert result.bars_processed == 0
    assert result.unfilled_intents == 0


def test_strategy_with_no_signals_produces_no_trades():
    bars = _bars_for("005930", [100, 110, 120])
    driver = BacktestDriver.from_strategies(bars, [Strategy()])
    result = driver.run()
    assert result.total_trades == 0
    assert result.bars_processed == 3


def test_buy_intent_fills_on_next_bar():
    """Strategy emits buy on bar 1; fill should land at bar 2's close, not bar 1's."""
    bars = _bars_for("005930", [100, 110])
    driver = BacktestDriver.from_strategies(
        bars, [_BuyOnFirstSellOnThird()], allocator=Allocator(max_position_per_symbol=100)
    )
    result = driver.run()
    assert result.total_buys == 1
    trade = result.trades[0]
    assert trade.side == Side.BUY
    assert trade.price == 110  # bar 2 close, not bar 1
    assert trade.quantity == 50  # 0.5 confidence * 100 max


def test_buy_then_sell_records_realized_pnl():
    """No-cost backtest exercising raw round-trip PnL."""
    bars = _bars_for("005930", [100, 110, 120, 130])
    driver = BacktestDriver.from_strategies(
        bars,
        [_BuyOnFirstSellOnThird()],
        allocator=Allocator(max_position_per_symbol=100),
        commission_bps=0,
        sell_tax_bps=0,
    )
    result = driver.run()
    assert result.total_buys == 1
    assert result.total_sells == 1
    sell = next(t for t in result.trades if t.side == Side.SELL)
    assert sell.price == 130
    assert sell.quantity == 50
    assert sell.realized_pnl_krw == (130 - 110) * 50  # 1000
    assert result.realized_pnl_krw == 1000
    assert result.win_rate == 1.0


def test_position_closes_after_round_trip():
    bars = _bars_for("005930", [100, 110, 120, 130])
    result = BacktestDriver.from_strategies(
        bars, [_BuyOnFirstSellOnThird()], allocator=Allocator(max_position_per_symbol=100)
    ).run()
    assert result.positions["005930"].quantity == 0


def test_sell_with_no_position_is_dropped():
    bars = _bars_for("005930", [100, 110])
    result = BacktestDriver.from_strategies(bars, [_SellOnceStrategy()]).run()
    # sell intent emitted on bar 1, would fill on bar 2, but no position → no trade
    assert result.total_trades == 0
    assert result.cash_krw == 100_000_000  # unchanged


def test_cash_decreases_on_buy_and_increases_on_sell():
    """No-cost cash math: buy 50 @ 110 / sell 50 @ 130."""
    bars = _bars_for("005930", [100, 110, 120, 130])
    result = BacktestDriver.from_strategies(
        bars,
        [_BuyOnFirstSellOnThird()],
        allocator=Allocator(max_position_per_symbol=100),
        starting_cash_krw=10_000_000,
        commission_bps=0,
        sell_tax_bps=0,
    ).run()
    expected = 10_000_000 - 5500 + 6500
    assert result.cash_krw == expected


def test_unrealized_pnl_uses_last_seen_price():
    """Position open at end of run; unrealized = (last - avg) * qty."""
    bars = _bars_for("005930", [100, 110, 120])  # buy fills on 110; no sell
    result = BacktestDriver.from_strategies(
        bars, [_BuyOnFirstSellOnThird()], allocator=Allocator(max_position_per_symbol=100)
    ).run()
    # position 50 @ 110, last price 120 → unrealized 50 * (120-110) = 500
    assert result.last_prices["005930"] == 120
    assert result.unrealized_pnl_krw == 500
    assert result.total_pnl_krw == 500  # no realized yet


def test_unfilled_intents_counted_when_no_subsequent_bar():
    """SELL intent emitted on the last bar has no next bar to fill against."""
    bars = _bars_for("005930", [100, 110, 120])
    result = BacktestDriver.from_strategies(
        bars, [_BuyOnFirstSellOnThird()], allocator=Allocator(max_position_per_symbol=100)
    ).run()
    # bar 3 emits sell, never fills (no bar 4) → 1 unfilled
    assert result.unfilled_intents == 1


def test_cross_symbol_intents_fill_independently():
    a_bars = _bars_for("005930", [100, 110], start_ts=datetime(2026, 5, 8, 9, 0, tzinfo=UTC))
    b_bars = _bars_for("000660", [200, 220], start_ts=datetime(2026, 5, 8, 9, 0, 5, tzinfo=UTC))
    bars = a_bars + b_bars
    result = BacktestDriver.from_strategies(
        bars, [_BuyOnFirstSellOnThird()], allocator=Allocator(max_position_per_symbol=100)
    ).run()
    # two symbols, each gets one buy on its second bar
    assert result.total_buys == 2
    a_trade = next(t for t in result.trades if t.symbol == "005930")
    b_trade = next(t for t in result.trades if t.symbol == "000660")
    assert a_trade.price == 110
    assert b_trade.price == 220


def test_average_cost_compounds_over_multiple_buys():
    bars = _bars_for("005930", [100, 110, 130, 150])
    # _BuyEveryBar: buy on every bar with confidence 0.1 → 10 shares each
    # Fills: bar2 @ 110, bar3 @ 130, bar4 @ 150 (intent from bar 1 fills bar 2, etc.)
    result = BacktestDriver.from_strategies(
        bars, [_BuyEveryBar()], allocator=Allocator(max_position_per_symbol=100)
    ).run()
    pos = result.positions["005930"]
    # 10 @ 110 + 10 @ 130 + 10 @ 150 = 30 shares, avg 130
    assert pos.quantity == 30
    assert pos.average_cost == 130.0


def test_bars_passed_in_any_order_get_sorted():
    bars = _bars_for("005930", [100, 110, 120])
    shuffled = [bars[2], bars[0], bars[1]]
    result = BacktestDriver.from_strategies(
        shuffled, [_BuyOnFirstSellOnThird()], allocator=Allocator(max_position_per_symbol=100)
    ).run()
    # First-bar buy must fill on second bar @ 110, not on third @ 120
    assert result.trades[0].price == 110


def test_win_rate_with_mixed_outcomes():
    """Two round trips: one wins, one loses."""

    class _OneShot(Strategy):
        name = "oneshot"

        def __init__(self) -> None:
            self._buys: dict[str, int] = {}
            self._sells: dict[str, int] = {}

        def on_bar(self, bar: Bar) -> list[Signal]:
            c = self._buys.get(bar.symbol, 0) + 1
            self._buys[bar.symbol] = c
            if c == 1:
                return [
                    Signal(
                        symbol=bar.symbol,
                        side=Side.BUY,
                        confidence=0.5,
                        strategy=self.name,
                        timestamp=bar.timestamp,
                    )
                ]
            if c == 3:
                return [
                    Signal(
                        symbol=bar.symbol,
                        side=Side.SELL,
                        confidence=1.0,
                        strategy=self.name,
                        timestamp=bar.timestamp,
                    )
                ]
            return []

    a = _bars_for("AAA", [100, 110, 120, 130], start_ts=datetime(2026, 5, 8, 9, 0, tzinfo=UTC))
    b = _bars_for("BBB", [200, 195, 190, 180], start_ts=datetime(2026, 5, 8, 9, 0, 10, tzinfo=UTC))
    result = BacktestDriver.from_strategies(
        a + b, [_OneShot()], allocator=Allocator(max_position_per_symbol=100)
    ).run()
    assert result.total_sells == 2
    assert result.winning_sells == 1
    assert result.losing_sells == 1
    assert result.win_rate == 0.5


def test_starting_cash_default_is_one_hundred_million_krw():
    result = BacktestDriver.from_strategies([], []).run()
    assert result.cash_krw == 100_000_000


# ---------------------------------------------------------------------------
# Risk integration
# ---------------------------------------------------------------------------


class _BuyMaxOnce(Strategy):
    """Emit a single full-confidence buy on the first bar of each symbol."""

    name = "buy_max_once"

    def __init__(self) -> None:
        self._sent: set[str] = set()

    def on_bar(self, bar: Bar) -> list[Signal]:
        if bar.symbol in self._sent:
            return []
        self._sent.add(bar.symbol)
        return [
            Signal(
                symbol=bar.symbol,
                side=Side.BUY,
                confidence=1.0,
                strategy=self.name,
                timestamp=bar.timestamp,
            )
        ]


def test_risk_caps_quantity_to_position_limit():
    """Allocator wants 100 (full confidence * max=100); Risk caps to 30."""
    bars = _bars_for("005930", [100, 110])
    result = BacktestDriver.from_strategies(
        bars,
        [_BuyMaxOnce()],
        allocator=Allocator(max_position_per_symbol=100),
        risk=Risk(max_position_per_symbol=30),
    ).run()
    assert result.total_buys == 1
    assert result.trades[0].quantity == 30


def test_risk_blocks_buys_when_at_cap():
    """Three sequential full-confidence buys, each fills at next bar — Risk
    cap=50 lets the first land, the next two are rejected outright."""
    bars = _bars_for("005930", [100, 110, 120, 130])

    class _AlwaysBuy(Strategy):
        name = "always_buy"

        def on_bar(self, bar: Bar) -> list[Signal]:
            return [
                Signal(
                    symbol=bar.symbol,
                    side=Side.BUY,
                    confidence=1.0,
                    strategy=self.name,
                    timestamp=bar.timestamp,
                )
            ]

    result = BacktestDriver.from_strategies(
        bars,
        [_AlwaysBuy()],
        allocator=Allocator(max_position_per_symbol=50),
        risk=Risk(max_position_per_symbol=50),
    ).run()
    assert result.total_buys == 1
    assert result.positions["005930"].quantity == 50


def test_risk_does_not_cap_sells():
    """Sell intent passes through Risk regardless of position cap."""
    bars = _bars_for("005930", [100, 110, 120, 130])

    class _BuyThenSellAll(Strategy):
        name = "round_trip"

        def __init__(self) -> None:
            self._counts: dict[str, int] = {}

        def on_bar(self, bar: Bar) -> list[Signal]:
            c = self._counts.get(bar.symbol, 0) + 1
            self._counts[bar.symbol] = c
            if c == 1:
                return [
                    Signal(
                        symbol=bar.symbol,
                        side=Side.BUY,
                        confidence=0.5,
                        strategy=self.name,
                        timestamp=bar.timestamp,
                    )
                ]
            if c == 3:
                return [
                    Signal(
                        symbol=bar.symbol,
                        side=Side.SELL,
                        confidence=1.0,
                        strategy=self.name,
                        timestamp=bar.timestamp,
                    )
                ]
            return []

    result = BacktestDriver.from_strategies(
        bars,
        [_BuyThenSellAll()],
        allocator=Allocator(max_position_per_symbol=100),
        risk=Risk(max_position_per_symbol=50),
    ).run()
    # Buy capped to 50 by Risk; sell wants to dump (not capped) and clears it
    assert result.total_buys == 1
    assert result.total_sells == 1
    assert result.positions["005930"].quantity == 0


def test_default_commission_and_tax_are_applied():
    """One round trip with default commission (1.5 bps) + sell tax (18 bps).
    Verify cash, realized PnL, and trade-level commission/tax fields."""
    bars = _bars_for("005930", [100, 110, 120, 130])
    result = BacktestDriver.from_strategies(
        bars, [_BuyOnFirstSellOnThird()], allocator=Allocator(max_position_per_symbol=100)
    ).run()

    buy = next(t for t in result.trades if t.side == Side.BUY)
    sell = next(t for t in result.trades if t.side == Side.SELL)
    # Buy 50 @ 110: gross = 5500, commission = round(5500 * 1.5/10000) = 1
    assert buy.quantity == 50
    assert buy.price == 110
    assert buy.commission_krw == round(5500 * 1.5 / 10_000)
    # Sell 50 @ 130: gross = 6500, commission ~= 1, tax = round(6500 * 18/10000) = 12
    assert sell.commission_krw == round(6500 * 1.5 / 10_000)
    assert sell.tax_krw == round(6500 * 18 / 10_000)
    # realized_pnl = (130-110)*50 - sell_commission - tax
    assert sell.realized_pnl_krw == 1000 - sell.commission_krw - sell.tax_krw
    assert result.total_commission_krw == buy.commission_krw + sell.commission_krw
    assert result.total_tax_krw == sell.tax_krw


def test_zero_costs_with_explicit_zeros():
    bars = _bars_for("005930", [100, 110, 120, 130])
    result = BacktestDriver.from_strategies(
        bars,
        [_BuyOnFirstSellOnThird()],
        allocator=Allocator(max_position_per_symbol=100),
        commission_bps=0,
        sell_tax_bps=0,
    ).run()
    assert result.total_commission_krw == 0
    assert result.total_tax_krw == 0
    sell = next(t for t in result.trades if t.side == Side.SELL)
    assert sell.realized_pnl_krw == 1000  # full gross 1000 with no costs


def test_buy_slippage_increases_fill_price():
    bars = _bars_for("005930", [100, 110])
    result = BacktestDriver.from_strategies(
        bars,
        [_BuyOnFirstSellOnThird()],
        allocator=Allocator(max_position_per_symbol=100),
        slippage_bps=100,  # 1% adverse
    ).run()
    buy = result.trades[0]
    # Bar2 close = 110, slippage 1% upward → 111
    assert buy.price == 111


def test_sell_slippage_decreases_fill_price():
    bars = _bars_for("005930", [100, 110, 120, 130])
    result = BacktestDriver.from_strategies(
        bars,
        [_BuyOnFirstSellOnThird()],
        allocator=Allocator(max_position_per_symbol=100),
        slippage_bps=100,
        commission_bps=0,
        sell_tax_bps=0,
    ).run()
    sell = next(t for t in result.trades if t.side == Side.SELL)
    # Bar4 close = 130, slippage 1% downward → 129
    assert sell.price == 129


def test_risk_daily_loss_circuit_blocks_further_buys():
    """Force a losing round trip; once realized PnL ≤ -limit, next buy is
    blocked."""

    class _BuyThenSellThenBuyAgain(Strategy):
        name = "loss_then_buy"

        def __init__(self) -> None:
            self._counts: dict[str, int] = {}

        def on_bar(self, bar: Bar) -> list[Signal]:
            c = self._counts.get(bar.symbol, 0) + 1
            self._counts[bar.symbol] = c
            if c == 1:
                return [
                    Signal(
                        symbol=bar.symbol,
                        side=Side.BUY,
                        confidence=1.0,
                        strategy=self.name,
                        timestamp=bar.timestamp,
                    )
                ]
            if c == 3:
                return [
                    Signal(
                        symbol=bar.symbol,
                        side=Side.SELL,
                        confidence=1.0,
                        strategy=self.name,
                        timestamp=bar.timestamp,
                    )
                ]
            if c == 5:
                return [
                    Signal(
                        symbol=bar.symbol,
                        side=Side.BUY,
                        confidence=1.0,
                        strategy=self.name,
                        timestamp=bar.timestamp,
                    )
                ]
            return []

    # Bar 1=100 (decision), Bar 2=200 (buy fills @ 200, qty 100 → -20000 cash),
    # Bar 3=50 (decision sell), Bar 4=10 (sell fills @ 10 → realized = (10-200)*100 = -19000),
    # Bar 5=15 (decision buy), Bar 6=20 (buy intent would fill, blocked by loss circuit).
    bars = _bars_for("005930", [100, 200, 50, 10, 15, 20])
    result = BacktestDriver.from_strategies(
        bars,
        [_BuyThenSellThenBuyAgain()],
        allocator=Allocator(max_position_per_symbol=100),
        risk=Risk(max_position_per_symbol=100, daily_loss_limit_krw=10_000),
    ).run()
    # First buy + first sell happen; second buy blocked by daily-loss circuit
    assert result.total_buys == 1
    assert result.total_sells == 1
    assert result.realized_pnl_krw <= -10_000
