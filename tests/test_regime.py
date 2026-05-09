"""Tests for MarketRegimeDetector + classify_regime."""

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.detectors.regime import MarketRegimeDetector, classify_regime
from ks_ws.domain import Bar


def _bars(closes: list[int]) -> list[Bar]:
    """Build daily bars with prescribed close prices, OHLV inferred."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Bar(
            symbol="KOSPI",
            timestamp=base + timedelta(days=i),
            timeframe="1d",
            open=c,
            high=c + 10,
            low=c - 10,
            close=c,
            volume=1000,
            value=c * 1000,
        )
        for i, c in enumerate(closes)
    ]


def test_unknown_when_too_few_bars():
    bars = _bars([100] * 30)
    assert classify_regime(bars, long_window=60) == "unknown"


def test_strong_uptrend_with_large_recent_gain():
    # 60 bars: flat 100s then surge in last 20 by +15%
    closes = [100] * 40 + list(range(100, 120))
    bars = _bars(closes)
    assert classify_regime(bars) == "strong_uptrend"


def test_uptrend_above_avg_modest_gain():
    # 50 bars at 100, then short window 105..109 (modest +4% surge,
    # above sideways band but below strong-uptrend +10%)
    closes = [100] * 40 + [105, 105, 105, 106, 106, 107, 107, 108, 108, 109,
                            109, 109, 109, 109, 109, 109, 109, 109, 109, 109]
    bars = _bars(closes)
    assert classify_regime(bars) == "uptrend"


def test_sideways_near_average():
    # constant prices = sideways
    bars = _bars([100] * 60)
    assert classify_regime(bars) == "sideways"


def test_downtrend_below_avg_recent_drop():
    # falling: short change -10%, last_close < long_avg
    closes = [120] * 40 + list(range(120, 100, -1))
    bars = _bars(closes)
    assert classify_regime(bars) == "downtrend"


def test_sideways_classification_band():
    """Custom sideways_band_pct relaxes/tightens the sideways window."""
    closes = [100] * 50 + [102, 102, 102, 102, 102, 102, 102, 102, 102, 102]
    bars = _bars(closes)
    # default (3% band): last 102 vs avg ~100.3 → ~1.7% above, sideways
    assert classify_regime(bars) == "sideways"
    # tighter 1% band: same data, no longer sideways
    assert classify_regime(bars, sideways_band_pct=0.5) == "uptrend"


# MarketRegimeDetector ---------------------------------------------------


def test_detector_starts_unknown():
    det = MarketRegimeDetector()
    assert det.current() == "unknown"


def test_detector_classifies_after_feed():
    det = MarketRegimeDetector()
    for bar in _bars([100] * 60):
        det.feed_bar(bar)
    assert det.current() == "sideways"
    assert det.bars_loaded == 60


def test_detector_caps_retention():
    det = MarketRegimeDetector(long_window=30, short_window=10)
    for bar in _bars([100] * 200):
        det.feed_bar(bar)
    # cap = 2 × long_window = 60
    assert det.bars_loaded == 60


def test_detector_validation():
    with pytest.raises(ValueError, match="must exceed"):
        MarketRegimeDetector(long_window=10, short_window=10)


def test_detector_provider_pattern_with_regime_gate():
    """Use detector.current as RegimeGate.regime_provider."""
    from ks_ws.strategies.base import Strategy
    from ks_ws.strategies.gates import RegimeGate
    from ks_ws.domain import Bar as BarCls, Side, Signal

    class _AlwaysBuy(Strategy):
        name = "ab"

        def on_bar(self, bar):
            return [
                Signal(
                    symbol=bar.symbol,
                    side=Side.BUY,
                    confidence=1.0,
                    strategy=self.name,
                    timestamp=bar.timestamp,
                )
            ]

    det = MarketRegimeDetector()
    for b in _bars([100] * 60):
        det.feed_bar(b)
    gate = RegimeGate(_AlwaysBuy(), allowed={"sideways"}, regime_provider=det.current)
    bar = _bars([100])[0]
    assert len(gate.on_bar(bar)) == 1
    # Now flip to uptrend
    for b in _bars([100, 110, 115, 120, 125, 130, 135, 140, 145, 150,
                    155, 160, 165, 170, 175, 180, 185, 190, 195, 200]):
        det.feed_bar(b)
    assert det.current() != "sideways"
    assert gate.on_bar(bar) == []
