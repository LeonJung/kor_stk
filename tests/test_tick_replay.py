"""Tests for TickReplayDriver + scenario loader."""

from datetime import UTC, datetime, time, timedelta
from textwrap import dedent
from zoneinfo import ZoneInfo

import pytest

from ks_ws.backtest.tick_replay import (
    TickReplayDriver,
    load_scenario,
    merge_chronological,
    synthetic_ticks_from_bar,
)
from ks_ws.domain import Bar, OrderBook, OrderBookLevel, Tick
from ks_ws.events import LimitUpBroken, LimitUpReached
from ks_ws.strategies.opening_momentum import OpeningMomentumStrategy
from ks_ws.strategies.pair_follow import PairFollowStrategy

_KST = ZoneInfo("Asia/Seoul")


def _kst(h: int, m: int, s: int = 0):
    return datetime(2026, 5, 11, h, m, s, tzinfo=_KST).astimezone(UTC)


# Driver — PairFollow scenario --------------------------------------------


def test_pair_follow_take_profit_via_driver():
    pair = PairFollowStrategy(
        pairs={"LEADER": "FOLLOW"},
        take_profit_pct=2.5,
        stop_loss_pct=1.5,
    )
    items = [
        LimitUpReached(symbol="LEADER", timestamp=_kst(9, 10), limit_up_price=13000, prev_close=10000),
        Tick(symbol="FOLLOW", timestamp=_kst(9, 11), price=10000, volume=10),
        Tick(symbol="FOLLOW", timestamp=_kst(9, 13), price=10260, volume=10),  # +2.6%
    ]
    with TickReplayDriver(items, [pair]) as driver:
        result = driver.run()
    assert result.total_intents == 2  # buy + sell
    sides = [i.side.value for i in result.intents]
    assert sides == ["buy", "sell"]
    pnl = result.strategy_pnl["pair_follow"]
    assert pnl.trades == 1
    assert pnl.realized_pnl_krw > 0


def test_pair_follow_broken_exit_via_driver():
    pair = PairFollowStrategy(pairs={"LEADER": "FOLLOW"})
    items = [
        LimitUpReached(symbol="LEADER", timestamp=_kst(9, 10), limit_up_price=13000, prev_close=10000),
        Tick(symbol="FOLLOW", timestamp=_kst(9, 11), price=10000, volume=10),
        LimitUpBroken(symbol="LEADER", timestamp=_kst(9, 12), limit_up_price=13000, current_price=12500),
    ]
    with TickReplayDriver(items, [pair]) as driver:
        result = driver.run()
    assert result.total_intents == 2
    sides = [i.side.value for i in result.intents]
    assert sides == ["buy", "sell"]
    # No price movement → 0 PnL but a recorded trade
    pnl = result.strategy_pnl.get("pair_follow")
    assert pnl is not None
    assert pnl.trades == 1


def test_opening_momentum_via_driver():
    strat = OpeningMomentumStrategy(
        watchlist={"OPEN1"}, surge_pct=5.0, take_profit_pct=3.0,
        entry_window_kst=(time(9, 3), time(9, 25)),
    )
    items = [
        Tick(symbol="OPEN1", timestamp=_kst(9, 0), price=10000, volume=10),  # capture open
        Tick(symbol="OPEN1", timestamp=_kst(9, 5), price=10500, volume=10),  # +5% entry
        Tick(symbol="OPEN1", timestamp=_kst(9, 10), price=10815, volume=10),  # +3% from entry
    ]
    with TickReplayDriver(items, [strat]) as driver:
        result = driver.run()
    sides = [i.side.value for i in result.intents]
    assert sides == ["buy", "sell"]
    pnl = result.strategy_pnl["opening_momentum"]
    assert pnl.realized_pnl_krw > 0


def test_driver_chronological_ordering():
    """Out-of-order input should be sorted by timestamp before dispatch."""
    pair = PairFollowStrategy(pairs={"LEADER": "FOLLOW"})
    items = [
        Tick(symbol="FOLLOW", timestamp=_kst(9, 13), price=10260, volume=10),  # later
        LimitUpReached(symbol="LEADER", timestamp=_kst(9, 10), limit_up_price=13000, prev_close=10000),
        Tick(symbol="FOLLOW", timestamp=_kst(9, 11), price=10000, volume=10),
    ]
    with TickReplayDriver(items, [pair]) as driver:
        result = driver.run()
    assert result.total_intents == 2  # entry + exit


def test_driver_fill_price_uses_last_tick_by_default():
    pair = PairFollowStrategy(pairs={"LEADER": "FOLLOW"})
    items = [
        LimitUpReached(symbol="LEADER", timestamp=_kst(9, 10), limit_up_price=13000, prev_close=10000),
        Tick(symbol="FOLLOW", timestamp=_kst(9, 11), price=10000, volume=10),
    ]
    with TickReplayDriver(items, [pair]) as driver:
        result = driver.run()
    # Single buy intent should be filled at the latest FOLLOW tick price (10000)
    intent, fill_price = result.fills[0]
    assert intent.side.value == "buy"
    assert fill_price == 10000


# Scenario YAML loader ----------------------------------------------------


def test_load_scenario_yaml(tmp_path):
    yaml_text = dedent(
        """
        items:
          - tick:
              symbol: A005930
              ts: "2026-05-11T09:00:00+09:00"
              price: 70000
              volume: 100
          - event:
              type: LimitUpReached
              symbol: LEADER
              ts: "2026-05-11T09:10:00+09:00"
              limit_up_price: 13000
              prev_close: 10000
          - bar:
              symbol: A005930
              ts: "2026-05-11T09:00:00+09:00"
              timeframe: "1m"
              open: 70000
              high: 70100
              low: 69900
              close: 70050
              volume: 1000
              value: 70050000
          - orderbook:
              symbol: A005930
              ts: "2026-05-11T09:00:00+09:00"
              bids: [[70000, 100], [69990, 200]]
              asks: [[70050, 100]]
        """
    )
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    items = load_scenario(path)
    assert len(items) == 4
    assert isinstance(items[0], Tick)
    assert items[0].symbol == "A005930"
    assert isinstance(items[1], LimitUpReached)
    assert items[1].limit_up_price == 13000
    assert isinstance(items[2], Bar)
    assert items[2].close == 70050
    assert isinstance(items[3], OrderBook)
    assert items[3].bids[0] == OrderBookLevel(price=70000, volume=100)


def test_load_scenario_unknown_event_type(tmp_path):
    yaml_text = dedent(
        """
        items:
          - event:
              type: Unknown
              symbol: X
              ts: "2026-05-11T09:00:00+09:00"
        """
    )
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="unknown event type"):
        load_scenario(path)


# synthetic_ticks_from_bar ------------------------------------------------


def test_synthetic_ticks_from_bar_visits_ohlc():
    bar = Bar(
        symbol="X",
        timestamp=_kst(9, 0),
        timeframe="1m",
        open=10000,
        high=10500,
        low=9800,
        close=10100,
        volume=100,
        value=1_010_000,
    )
    ticks = synthetic_ticks_from_bar(bar, n_ticks=12)
    prices = [t.price for t in ticks]
    assert prices[0] == 10000  # opens at open
    assert max(prices) == 10500  # touches high
    assert min(prices) == 9800  # touches low
    assert prices[-1] == 10100  # ends at close


def test_synthetic_ticks_rejects_too_few():
    bar = Bar(
        symbol="X", timestamp=_kst(9, 0), timeframe="1m",
        open=10000, high=10100, low=9900, close=10050, volume=100, value=1_005_000,
    )
    with pytest.raises(ValueError, match="n_ticks must be >= 4"):
        synthetic_ticks_from_bar(bar, n_ticks=3)


# merge_chronological -----------------------------------------------------


def test_merge_chronological_two_streams():
    s1 = [
        Tick(symbol="A", timestamp=_kst(9, 0), price=100, volume=1),
        Tick(symbol="A", timestamp=_kst(9, 5), price=110, volume=1),
    ]
    s2 = [
        Tick(symbol="B", timestamp=_kst(9, 2), price=200, volume=1),
        Tick(symbol="B", timestamp=_kst(9, 4), price=210, volume=1),
    ]
    merged = merge_chronological(s1, s2)
    assert [m.timestamp for m in merged] == sorted(m.timestamp for m in merged)
    assert len(merged) == 4
