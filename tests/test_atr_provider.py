"""ATR provider tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.domain import Bar
from ks_ws.sources.atr_provider import (
    ATR_MULTIPLIERS,
    BarStoreATRProvider,
    compute_atr,
    compute_tp_sl,
    compute_tp_sl_pct,
)
from ks_ws.storage.bars import BarStore


def _bar(*, h: int, low: int, c: int, days_ago: int = 0) -> Bar:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    return Bar(
        symbol="005930", timeframe="1d",
        timestamp=base - timedelta(days=days_ago),
        open=(h + low) // 2, high=h, low=low, close=c,
        volume=1_000, value=c * 1_000,
    )


def test_compute_atr_basic() -> None:
    # 15 bars (period 14 + 1)
    bars = []
    for i in range(15):
        bars.append(_bar(h=110 + i, low=90 + i, c=100 + i, days_ago=14 - i))
    atr = compute_atr(bars, period=14)
    # 각 TR = max(20, |high-prev_close|, |low-prev_close|) ~ 20-21
    assert 15 < atr < 25


def test_compute_atr_insufficient_data() -> None:
    bars = [_bar(h=100, low=95, c=98, days_ago=i) for i in range(5)]
    assert compute_atr(bars, period=14) == 0.0


def test_compute_atr_invalid_period() -> None:
    with pytest.raises(ValueError):
        compute_atr([], period=0)


def test_provider_with_bar_store(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = []
    for i in range(20):
        bars.append(Bar(
            symbol="005930", timeframe="1d",
            timestamp=datetime(2026, 5, 15, tzinfo=UTC) - timedelta(days=19 - i),
            open=100, high=110, low=90, close=100,
            volume=1_000, value=100_000,
        ))
    store.write(bars)
    provider = BarStoreATRProvider(store, timeframe="1d", period=14)
    atr = provider("005930")
    assert atr > 0  # 20 range, 일정 데이터라 ATR ~ 20


def test_provider_cache(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    bars = [Bar(symbol="005930", timeframe="1d",
                timestamp=datetime(2026, 5, 15, tzinfo=UTC) - timedelta(days=19 - i),
                open=100, high=110, low=90, close=100,
                volume=1_000, value=100_000) for i in range(20)]
    store.write(bars)
    provider = BarStoreATRProvider(store, ttl_seconds=3600)
    snap1 = provider.get_snapshot("005930")
    snap2 = provider.get_snapshot("005930")
    assert snap1 is snap2  # cache hit


def test_provider_missing_data(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    provider = BarStoreATRProvider(store)
    assert provider("UNKNOWN") == 0.0
    assert provider.get_snapshot("UNKNOWN") is None


def test_compute_tp_sl_swing() -> None:
    # entry 285000, ATR 5000, style swing
    tp, sl = compute_tp_sl(285000, 5000.0, "swing")
    # TP = entry + 4 x ATR = 305000, SL = entry - 2 x ATR = 275000
    assert tp == 305000
    assert sl == 275000


def test_compute_tp_sl_scalping() -> None:
    tp, sl = compute_tp_sl(100000, 500.0, "scalping")
    # TP = 100000 + 1 x 500 = 100500, SL = 100000 - 0.5 x 500 = 99750
    assert tp == 100500
    assert sl == 99750


def test_compute_tp_sl_fallback() -> None:
    # ATR 0 → fallback pct
    tp, sl = compute_tp_sl(100000, 0.0, "swing",
                           fallback_tp_pct=3.0, fallback_sl_pct=2.0)
    assert tp == 103000
    assert sl == 98000


def test_compute_tp_sl_unknown_style() -> None:
    with pytest.raises(ValueError):
        compute_tp_sl(100000, 1000.0, "unknown_style")


def test_compute_tp_sl_pct_swing() -> None:
    tp_pct, sl_pct = compute_tp_sl_pct(2.0, "swing")
    # atr_pct 2.0 x swing(tp 4 / sl 2) = (8.0%, 4.0%)
    assert tp_pct == 8.0
    assert sl_pct == 4.0


def test_atr_multipliers_keys() -> None:
    assert set(ATR_MULTIPLIERS) == {"scalping", "day_trade", "swing", "mid_term"}
    for v in ATR_MULTIPLIERS.values():
        assert v["tp"] > v["sl"] > 0
