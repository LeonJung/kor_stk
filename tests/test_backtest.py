from datetime import UTC, datetime, timedelta

from ks_ws.backtest.driver import BacktestDriver, Position
from ks_ws.domain import Bar, Side, Signal
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
    bars = _bars_for("005930", [100, 110, 120, 130])
    driver = BacktestDriver.from_strategies(
        bars, [_BuyOnFirstSellOnThird()], allocator=Allocator(max_position_per_symbol=100)
    )
    result = driver.run()
    assert result.total_buys == 1
    assert result.total_sells == 1
    # buy filled at 110, sold at 130, qty capped at owned 50
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
    bars = _bars_for("005930", [100, 110, 120, 130])
    result = BacktestDriver.from_strategies(
        bars,
        [_BuyOnFirstSellOnThird()],
        allocator=Allocator(max_position_per_symbol=100),
        starting_cash_krw=10_000_000,
    ).run()
    # buy 50 @ 110 = -5500; sell 50 @ 130 = +6500
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
