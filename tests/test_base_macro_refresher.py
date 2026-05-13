"""BaseMacroRefresher — 분봉 mtr.minute_score 변경 시 base_macro 자동 갱신."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.sources.base_macro_refresher import BaseMacroRefresher
from ks_ws.storage.bars import BarStore


def _bar(close: int, *, sym: str, tf: str, minutes_ago: int = 0,
         days_ago: int = 0) -> Bar:
    base = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)
    ts = base - timedelta(minutes=minutes_ago, days=days_ago)
    return Bar(
        symbol=sym, timeframe=tf, timestamp=ts,
        open=close, high=close + 5, low=close - 5,
        close=close, volume=10_000, value=close * 10_000,
    )


class _StubAlloc:
    def __init__(self) -> None:
        self.scores: dict[str, float] = {}

    def set_macro_score(self, symbol: str, score: float) -> None:
        self.scores[symbol] = score


class _StubDyn:
    def __init__(self) -> None:
        self.bases: dict[str, float] = {}

    def set_base_score(self, symbol: str, score: float) -> None:
        self.bases[symbol] = score


def _index_uptrend(n: int = 80) -> list[Bar]:
    return [_bar(100 + int(i * 0.25), sym="KOSPI", tf="1d", days_ago=n - i - 1)
            for i in range(n)]


def test_step_recomputes_score_per_symbol(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    # 005930 분봉 — 상승 추세 (95 → 105 over 30 bars, +10.5%)
    store.write([_bar(95 + i // 3, sym="005930", tf="1m", minutes_ago=30 - i)
                 for i in range(30)])
    alloc = _StubAlloc()
    dyn = _StubDyn()
    refr = BaseMacroRefresher(
        EventBus(), store, alloc, dyn,
        codes=["005930"],
        kospi_bars=_index_uptrend(),
        static_scores={"005930": (1.0, 1.0, 1.0)},  # neutral static inputs
    )
    n = refr.step()
    assert n == 1
    # mtr should be > 1.0 (uptrend index + up minute) → blend > 1.0
    assert alloc.scores["005930"] > 1.0
    assert dyn.bases["005930"] == alloc.scores["005930"]


def test_step_handles_missing_minute_bars(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    alloc = _StubAlloc()
    dyn = _StubDyn()
    refr = BaseMacroRefresher(
        EventBus(), store, alloc, dyn,
        codes=["005930"],
        kospi_bars=_index_uptrend(),
        static_scores={"005930": (1.0, 1.0, 1.0)},
    )
    n = refr.step()
    assert n == 1  # no minute bars → mtr.minute_score=1.0 → blend uses fallback


def test_step_skips_symbols_without_static_score(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    alloc = _StubAlloc()
    dyn = _StubDyn()
    refr = BaseMacroRefresher(
        EventBus(), store, alloc, dyn,
        codes=["005930", "000660"],
        kospi_bars=_index_uptrend(),
        static_scores={"005930": (1.0, 1.0, 1.0)},  # missing 000660
    )
    n = refr.step()
    assert n == 1
    assert "005930" in alloc.scores
    assert "000660" not in alloc.scores


def test_invalid_interval_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = BarStore(td)
        with pytest.raises(ValueError):
            BaseMacroRefresher(
                EventBus(), store, _StubAlloc(), _StubDyn(),
                codes=[], kospi_bars=[], static_scores={},
                interval_sec=0,
            )


def test_invalid_lookback_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = BarStore(td)
        with pytest.raises(ValueError):
            BaseMacroRefresher(
                EventBus(), store, _StubAlloc(), _StubDyn(),
                codes=[], kospi_bars=[], static_scores={},
                minute_lookback=0,
            )


def test_refresh_count_increments(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    alloc = _StubAlloc()
    dyn = _StubDyn()
    refr = BaseMacroRefresher(
        EventBus(), store, alloc, dyn,
        codes=["005930"],
        kospi_bars=_index_uptrend(),
        static_scores={"005930": (1.0, 1.0, 1.0)},
    )
    refr.step()
    refr.step()
    refr.step()
    assert refr.refresh_count == 3
