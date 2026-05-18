"""VolumeFilter — vb 룰의 entry gate 용 거래대금 + 회전율 + RVOL 필터.

학습 결과 (2026-05-18, 백업 분봉 19.6M 분봉) 기반:
- 분봉 turnover (volume/listed_shares ppm) ≥ 500 → 30분 +3% 도달 확률 12.24% (baseline 1.70% 의 7배)
- 시총 대비 거래대금 (value/market_cap ppm) ≥ 100 도 비슷한 강도
- RVOL ≥ 2 = breakout follow-through 표준

식 (Option B / Standard):
    turnover_ppm = (bar.volume / listed_shares) * 1e6
    val_mcap_ppm = (bar.value / market_cap_krw) * 1e6
    rvol         = bar.value / avg_20m_bar_value
    pass = (turnover_ppm >= 500) AND (val_mcap_ppm >= 100) AND (rvol >= 2.0)

종목 메타데이터 (listed_shares, market_cap_krw) 는 UniverseRegistry 에서.
rolling avg_20m_bar_value 는 on_bar 마다 갱신.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class VolumeStats:
    """종목별 20-bar rolling avg + last bar value/volume cache."""
    avg_window: int = 20
    _buf: deque = field(default_factory=lambda: deque(maxlen=20))
    last_value: int = 0
    last_volume: int = 0

    def update(self, bar_value: int, bar_volume: int) -> None:
        self.last_value = bar_value
        self.last_volume = bar_volume
        if bar_value > 0:
            self._buf.append(bar_value)

    @property
    def avg_20m_value(self) -> float:
        if not self._buf:
            return 0.0
        return sum(self._buf) / len(self._buf)


class VolumeFilter:
    """Entry filter — turnover + val/mcap + RVOL 조건. 학습된 default 임계 = Standard.

    Args:
        listed_shares: {symbol: 발행주식수} (UniverseRegistry 에서)
        market_cap_krw: {symbol: 시가총액 KRW}
        preset: 'off' / 'conservative' / 'standard' / 'aggressive'
    """
    _PRESETS = {
        "off": (0.0, 0.0, 0.0),  # 비활성 = 모든 entry pass
        "conservative": (100.0, 50.0, 1.5),   # turnover_ppm, val_mcap_ppm, rvol
        "standard": (500.0, 100.0, 2.0),
        "aggressive": (1000.0, 300.0, 3.0),
    }

    def __init__(
        self,
        *,
        listed_shares: dict[str, int] | None = None,
        market_cap_krw: dict[str, int] | None = None,
        preset: Literal["off", "conservative", "standard", "aggressive"] = "standard",
        turnover_ppm_min: float | None = None,
        val_mcap_ppm_min: float | None = None,
        rvol_min: float | None = None,
    ) -> None:
        if preset not in self._PRESETS:
            raise ValueError(f"preset must be one of {list(self._PRESETS)}")
        defaults = self._PRESETS[preset]
        self.turnover_ppm_min = (
            turnover_ppm_min if turnover_ppm_min is not None else defaults[0]
        )
        self.val_mcap_ppm_min = (
            val_mcap_ppm_min if val_mcap_ppm_min is not None else defaults[1]
        )
        self.rvol_min = rvol_min if rvol_min is not None else defaults[2]
        self.listed_shares = dict(listed_shares or {})
        self.market_cap_krw = dict(market_cap_krw or {})
        self._stats: dict[str, VolumeStats] = {}
        self.preset = preset

    def on_bar(self, symbol: str, bar_value: int, bar_volume: int) -> None:
        stats = self._stats.get(symbol)
        if stats is None:
            stats = VolumeStats()
            self._stats[symbol] = stats
        stats.update(bar_value, bar_volume)

    def passes(self, symbol: str) -> bool:
        """현재 마지막 bar 가 entry filter 통과하는지."""
        if self.preset == "off":
            return True
        stats = self._stats.get(symbol)
        if stats is None or stats.last_value == 0:
            return False
        ls = self.listed_shares.get(symbol, 0)
        mc = self.market_cap_krw.get(symbol, 0)
        if ls <= 0 or mc <= 0:
            return False
        turnover_ppm = (stats.last_volume / ls) * 1e6
        val_mcap_ppm = (stats.last_value / mc) * 1e6
        avg = stats.avg_20m_value
        rvol = stats.last_value / avg if avg > 0 else 0
        return (
            turnover_ppm >= self.turnover_ppm_min
            and val_mcap_ppm >= self.val_mcap_ppm_min
            and rvol >= self.rvol_min
        )

    def debug(self, symbol: str) -> dict:
        stats = self._stats.get(symbol)
        if stats is None:
            return {}
        ls = self.listed_shares.get(symbol, 0)
        mc = self.market_cap_krw.get(symbol, 0)
        avg = stats.avg_20m_value
        return {
            "turnover_ppm": (stats.last_volume / ls) * 1e6 if ls else 0,
            "val_mcap_ppm": (stats.last_value / mc) * 1e6 if mc else 0,
            "rvol": stats.last_value / avg if avg else 0,
            "avg_20m": avg,
        }
