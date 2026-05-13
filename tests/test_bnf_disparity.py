"""BNFDisparityStrategy — MA25 이격도 평균회귀."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.domain import Bar, Side, Tick
from ks_ws.storage.bars import BarStore
from ks_ws.strategies.bnf_disparity import (
    BarStoreMA25Provider,
    BNFDisparityStrategy,
)


def _tick(price: int, *, ts_offset_min: int = 0,
          sym: str = "005930") -> Tick:
    base = datetime(2026, 5, 13, tzinfo=UTC)
    return Tick(
        symbol=sym, price=price, volume=100,
        timestamp=base + timedelta(minutes=ts_offset_min),
    )


def _const_ma25(value: int):
    def provider(symbol: str) -> int | None:
        return value
    return provider


def test_entry_on_disparity_cross() -> None:
    # MA25 = 100. -15% threshold = 85.
    s = BNFDisparityStrategy(ma25_provider=_const_ma25(100), disparity_pct=15.0)
    assert s.on_tick(_tick(90)) == []  # above 85, no signal
    sigs = s.on_tick(_tick(80, ts_offset_min=1))  # cross down
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].strategy == "bnf_disparity"


def test_exit_on_ma25_revert() -> None:
    s = BNFDisparityStrategy(ma25_provider=_const_ma25(100), disparity_pct=15.0)
    s.on_tick(_tick(80))  # entry
    sigs = s.on_tick(_tick(100, ts_offset_min=1))  # MA25 revert
    assert sigs and sigs[0].side is Side.SELL
    assert "revert" in (sigs[0].note or "").lower()


def test_sl_exit() -> None:
    s = BNFDisparityStrategy(
        ma25_provider=_const_ma25(100), disparity_pct=15.0,
        take_profit_pct=5.0, stop_loss_pct=3.0,
    )
    s.on_tick(_tick(80))  # entry
    # SL = 80 * 0.97 = 77.6
    sigs = s.on_tick(_tick(77, ts_offset_min=1))
    assert sigs and sigs[0].side is Side.SELL
    assert sigs[0].urgency == "high"


def test_no_entry_above_threshold() -> None:
    s = BNFDisparityStrategy(ma25_provider=_const_ma25(100), disparity_pct=15.0)
    assert s.on_tick(_tick(95)) == []  # above 85


def test_no_entry_when_ma25_missing() -> None:
    s = BNFDisparityStrategy(ma25_provider=lambda sym: None)
    assert s.on_tick(_tick(80)) == []


def test_no_double_entry_same_day() -> None:
    s = BNFDisparityStrategy(ma25_provider=_const_ma25(100), disparity_pct=15.0)
    s.on_tick(_tick(80))  # entry
    s.on_tick(_tick(100, ts_offset_min=1))  # TP exit
    # Same day re-cross
    assert s.on_tick(_tick(80, ts_offset_min=2)) == []


def test_edge_detection_avoids_repeat() -> None:
    s = BNFDisparityStrategy(ma25_provider=_const_ma25(100), disparity_pct=15.0)
    sigs1 = s.on_tick(_tick(80))  # entry
    assert len(sigs1) == 1
    # Still below threshold — but already in position
    sigs2 = s.on_tick(_tick(75, ts_offset_min=1))
    assert all(sig.side is not Side.BUY for sig in sigs2)


def test_invalid_pct_raises() -> None:
    with pytest.raises(ValueError):
        BNFDisparityStrategy(ma25_provider=lambda s: 100, disparity_pct=0)


def test_bar_store_ma25_provider(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = [
        Bar(symbol="005930", timeframe="1m",
            timestamp=datetime(2026, 5, 13, tzinfo=UTC) - timedelta(minutes=25 - i),
            open=100, high=100, low=100, close=100 + i,
            volume=100, value=100_000)
        for i in range(25)
    ]
    store.write(bars)
    provider = BarStoreMA25Provider(store, lookback=25)
    ma = provider("005930")
    # closes = 100..124, mean = 112
    assert ma == 112


def test_bar_store_ma25_returns_none_for_short_history(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = [
        Bar(symbol="005930", timeframe="1m",
            timestamp=datetime(2026, 5, 13, tzinfo=UTC) - timedelta(minutes=i),
            open=100, high=100, low=100, close=100,
            volume=100, value=100_000)
        for i in range(10)
    ]
    store.write(bars)
    provider = BarStoreMA25Provider(store, lookback=25)
    assert provider("005930") is None
