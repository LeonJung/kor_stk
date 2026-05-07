import asyncio
from datetime import UTC, datetime

from ks_ws.bus import EventBus
from ks_ws.domain import Bar, OrderBook, OrderBookLevel, OrderIntent, Side, Signal, Tick
from ks_ws.events import Event, ProgramFlowEnter
from ks_ws.runtime import Runtime
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy


def _now():
    return datetime.now(UTC)


def _bar(symbol="005930"):
    return Bar(
        symbol=symbol,
        timestamp=_now(),
        timeframe="1m",
        open=70_000,
        high=70_100,
        low=69_900,
        close=70_050,
        volume=1_000,
        value=70_050_000,
    )


def _tick(symbol="005930"):
    return Tick(symbol=symbol, timestamp=_now(), price=70_000, volume=10)


def _orderbook(symbol="005930"):
    return OrderBook(
        symbol=symbol,
        timestamp=_now(),
        bids=(OrderBookLevel(price=69_900, volume=100),),
        asks=(OrderBookLevel(price=70_100, volume=200),),
    )


# Test strategies ----------------------------------------------------------


class _RecordingBuyOnBar(Strategy):
    name = "buy_on_bar"

    def __init__(self) -> None:
        self.bars: list[Bar] = []

    def on_bar(self, bar: Bar) -> list[Signal]:
        self.bars.append(bar)
        return [
            Signal(
                symbol=bar.symbol,
                side=Side.BUY,
                confidence=0.5,
                strategy=self.name,
                timestamp=_now(),
            )
        ]


class _RecordingSellOnBar(Strategy):
    name = "sell_on_bar"

    def on_bar(self, bar: Bar) -> list[Signal]:
        return [
            Signal(
                symbol=bar.symbol,
                side=Side.SELL,
                confidence=0.3,
                strategy=self.name,
                timestamp=_now(),
            )
        ]


class _BuyOnEvent(Strategy):
    name = "event_buyer"

    def on_event(self, event: Event) -> list[Signal]:
        if isinstance(event, ProgramFlowEnter):
            return [
                Signal(
                    symbol=event.symbol,
                    side=Side.BUY,
                    confidence=1.0,
                    strategy=self.name,
                    timestamp=_now(),
                )
            ]
        return []


class _RaisingStrategy(Strategy):
    name = "raises"

    def on_bar(self, bar: Bar) -> list[Signal]:
        raise RuntimeError("intentional failure")


class _OrderbookRecorder(Strategy):
    name = "ob_rec"

    def __init__(self) -> None:
        self.books: list[OrderBook] = []

    def on_orderbook(self, ob: OrderBook) -> list[Signal]:
        self.books.append(ob)
        return []


class _TickRecorder(Strategy):
    name = "tick_rec"

    def __init__(self) -> None:
        self.ticks: list[Tick] = []

    def on_tick(self, tick: Tick) -> list[Signal]:
        self.ticks.append(tick)
        return []


# step() — synchronous mode -----------------------------------------------


def test_step_dispatches_bar_and_publishes_intent():
    bus = EventBus()
    intent_sub = bus.subscribe(OrderIntent)
    strat = _RecordingBuyOnBar()
    rt = Runtime(bus, [strat], Allocator(max_position_per_symbol=100))
    rt.setup()

    bus.publish(_bar())
    processed = rt.step()

    assert processed == 1
    assert len(strat.bars) == 1
    assert intent_sub.qsize() == 1
    intent = intent_sub.get_nowait()
    assert intent.side == Side.BUY
    assert intent.symbol == "005930"
    assert intent.quantity == 50  # 0.5 confidence * 100 max


def test_step_processes_multiple_topics_in_one_call():
    bus = EventBus()
    bar_strat = _RecordingBuyOnBar()
    tick_strat = _TickRecorder()
    ob_strat = _OrderbookRecorder()
    rt = Runtime(bus, [bar_strat, tick_strat, ob_strat], Allocator())
    rt.setup()

    bus.publish(_bar())
    bus.publish(_tick())
    bus.publish(_orderbook())

    processed = rt.step()
    assert processed == 3
    assert len(bar_strat.bars) == 1
    assert len(tick_strat.ticks) == 1
    assert len(ob_strat.books) == 1


def test_step_combines_signals_across_strategies():
    """Two strategies both look at bars; allocator nets their signals."""
    bus = EventBus()
    intent_sub = bus.subscribe(OrderIntent)
    rt = Runtime(
        bus,
        [_RecordingBuyOnBar(), _RecordingSellOnBar()],
        Allocator(max_position_per_symbol=100),
    )
    rt.setup()

    bus.publish(_bar())
    rt.step()

    assert intent_sub.qsize() == 1
    intent = intent_sub.get_nowait()
    # buy 0.5 - sell 0.3 = 0.2 net buy -> 20 shares
    assert intent.side == Side.BUY
    assert intent.quantity == 20
    assert set(intent.sources) == {"buy_on_bar", "sell_on_bar"}


def test_step_isolates_strategy_exceptions():
    bus = EventBus()
    intent_sub = bus.subscribe(OrderIntent)
    ok = _RecordingBuyOnBar()
    bad = _RaisingStrategy()
    rt = Runtime(bus, [bad, ok], Allocator(max_position_per_symbol=100))
    rt.setup()

    bus.publish(_bar())
    rt.step()

    # ok strategy still ran despite bad's exception
    assert len(ok.bars) == 1
    assert intent_sub.qsize() == 1


def test_step_emits_no_intent_when_strategies_silent():
    bus = EventBus()
    intent_sub = bus.subscribe(OrderIntent)
    rt = Runtime(bus, [_TickRecorder()], Allocator())
    rt.setup()

    bus.publish(_tick())
    rt.step()

    assert intent_sub.qsize() == 0


def test_step_returns_zero_when_nothing_queued():
    bus = EventBus()
    rt = Runtime(bus, [_RecordingBuyOnBar()], Allocator())
    assert rt.step() == 0  # auto-setup but nothing to process


def test_event_subscription_catches_subclass_events():
    bus = EventBus()
    intent_sub = bus.subscribe(OrderIntent)
    rt = Runtime(bus, [_BuyOnEvent()], Allocator(max_position_per_symbol=100))
    rt.setup()

    bus.publish(
        ProgramFlowEnter(
            symbol="005930",
            timestamp=_now(),
            delta_krw=2_000_000_000,
            window_seconds=30,
        )
    )
    rt.step()

    assert intent_sub.qsize() == 1
    intent = intent_sub.get_nowait()
    assert intent.side == Side.BUY


# start() / stop() — continuous mode --------------------------------------


def test_continuous_mode_dispatches_published_bar():
    async def run():
        bus = EventBus()
        intent_sub = bus.subscribe(OrderIntent)
        rt = Runtime(bus, [_RecordingBuyOnBar()], Allocator(max_position_per_symbol=100))
        await rt.start()
        bus.publish(_bar())
        # Yield enough times for the dispatch task to consume the queue.
        for _ in range(5):
            await asyncio.sleep(0)
        await rt.stop()
        return intent_sub.qsize()

    assert asyncio.run(run()) == 1


def test_stop_is_idempotent_and_clears_state():
    async def run():
        bus = EventBus()
        rt = Runtime(bus, [_RecordingBuyOnBar()], Allocator())
        await rt.start()
        await rt.stop()
        await rt.stop()  # second call should be a no-op
        return rt.running

    assert asyncio.run(run()) is False


def test_start_is_idempotent():
    async def run():
        bus = EventBus()
        rt = Runtime(bus, [_RecordingBuyOnBar()], Allocator())
        await rt.start()
        await rt.start()  # second call is a no-op
        running = rt.running
        await rt.stop()
        return running

    assert asyncio.run(run()) is True


def test_setup_is_idempotent():
    bus = EventBus()
    rt = Runtime(bus, [_RecordingBuyOnBar()], Allocator())
    rt.setup()
    initial_count = bus.subscription_count
    rt.setup()
    assert bus.subscription_count == initial_count
