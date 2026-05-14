"""ATR (Average True Range) provider — 변동성 기반 TP/SL 동적 계산.

사용자 명시 2026-05-14 (B): 각 strategy 가 entry 시점 ATR 기준으로 TP/SL 자동
계산. 종목별 + 시점별로 적정 TP/SL 달라짐.

True Range = max(high - low, |high - prev_close|, |low - prev_close|)
ATR_n = N-period (default 14) average of TR.

스타일별 ATR 주기:
- 스캘핑: 1분봉 ATR (14)
- 단타: 5분봉 ATR (14)
- 스윙: 15분봉 ATR (14)
- 중기: 일봉 ATR (14)
- 장기: 일봉/주봉 ATR (14)

TP/SL = ATR x multiplier (스타일별):
- 스캘핑: TP 1.0 / SL 0.5
- 단타: TP 2.0 / SL 1.0
- 스윙: TP 4.0 / SL 2.0
- 중기: TP 8.0 / SL 3.0

설계:
- `compute_atr(bars, period=14)` — 순수 함수
- `BarStoreATRProvider(bar_store, timeframe, period, ttl)` — caching wrapper
- Strategy 가 entry 시 `atr_provider(symbol)` 호출 → TP/SL 계산
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ks_ws.domain import Bar
    from ks_ws.storage.bars import BarStore


def compute_atr(bars: Sequence[Bar], period: int = 14) -> float:
    """ATR_n 계산. bars 는 시간순 정렬, 최소 (period + 1) 봉 필요."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        hi = bars[i].high
        lo = bars[i].low
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
    # Wilder smoothing (단순 평균으로 V1 시작)
    return sum(trs[-period:]) / period


@dataclass
class ATRSnapshot:
    symbol: str
    timeframe: str
    period: int
    atr: float
    bars_used: int
    computed_at: datetime


class BarStoreATRProvider:
    """BarStore 의 historical bars 기반 ATR 계산 + cache.

    Strategy 가 entry 시점에 `provider(symbol)` 호출 → ATR 반환.
    cache TTL (default 1시간) 이내 같은 symbol 호출 = cache hit.
    """

    def __init__(
        self,
        bar_store: BarStore,
        *,
        timeframe: str = "1d",
        period: int = 14,
        ttl_seconds: int = 3600,
    ) -> None:
        if period <= 0:
            raise ValueError("period must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._bar_store = bar_store
        self.timeframe = timeframe
        self.period = period
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, ATRSnapshot] = {}

    def __call__(self, symbol: str) -> float:
        """Return ATR for symbol. 0.0 if insufficient data."""
        snap = self.get_snapshot(symbol)
        return snap.atr if snap else 0.0

    def get_snapshot(self, symbol: str) -> ATRSnapshot | None:
        cached = self._cache.get(symbol)
        now = datetime.now(UTC)
        if cached and (now - cached.computed_at).total_seconds() < self.ttl_seconds:
            return cached
        bars = list(self._bar_store.read(symbol, self.timeframe))
        if len(bars) < self.period + 1:
            return None
        bars.sort(key=lambda b: b.timestamp)
        atr = compute_atr(bars, period=self.period)
        snap = ATRSnapshot(
            symbol=symbol, timeframe=self.timeframe, period=self.period,
            atr=atr, bars_used=len(bars), computed_at=now,
        )
        self._cache[symbol] = snap
        return snap


# Style multipliers (사용자 확정 2026-05-15).
# KOSPI top 종목 일봉 ATR ~5% 기준 재조정. 기존 4/2 → 2/1 (스윙 기준).
# ATR 자체가 크므로 multiplier 절감해야 사용자 doc 의 TP/SL 범위 부합.
ATR_MULTIPLIERS = {
    "scalping": {"tp": 0.3, "sl": 0.15},
    "day_trade": {"tp": 0.5, "sl": 0.3},
    "swing": {"tp": 2.0, "sl": 1.0},
    "mid_term": {"tp": 5.0, "sl": 2.0},
    # long_term = 별도 (단계별 trailing 100%+)
}


def compute_tp_sl(
    entry_price: int,
    atr: float,
    style: str,
    *,
    fallback_tp_pct: float = 3.0,
    fallback_sl_pct: float = 2.0,
) -> tuple[int, int]:
    """ATR + 스타일 기반 TP/SL 계산. ATR=0 시 fallback.

    Returns (tp_price, sl_price). BUY 가정 (TP > entry > SL).
    """
    if style not in ATR_MULTIPLIERS:
        raise ValueError(f"unknown style: {style!r}")
    if atr <= 0:
        # Fallback to fixed pct
        tp = entry_price * (1 + fallback_tp_pct / 100)
        sl = entry_price * (1 - fallback_sl_pct / 100)
        return int(tp), int(sl)
    mult = ATR_MULTIPLIERS[style]
    tp = entry_price + atr * mult["tp"]
    sl = entry_price - atr * mult["sl"]
    return int(tp), int(sl)


def compute_tp_sl_pct(
    atr_pct: float, style: str,
    *, fallback_tp_pct: float = 3.0, fallback_sl_pct: float = 2.0,
) -> tuple[float, float]:
    """ATR 가 entry_price 의 % 로 주어졌을 때 TP/SL % 반환."""
    if style not in ATR_MULTIPLIERS:
        raise ValueError(f"unknown style: {style!r}")
    if atr_pct <= 0:
        return fallback_tp_pct, fallback_sl_pct
    mult = ATR_MULTIPLIERS[style]
    return atr_pct * mult["tp"], atr_pct * mult["sl"]
