"""RegimeGate — Tier 4 시장 regime gate.

사용자 룰 (2026-05-15): VKOSPI / KOSPI 5일 추세 / NASDAQ 전일 동향 으로
"risk-on" 판정. risk-off 시 BUY signal 차단 (또는 비중 ↓).

조건 (default):
- VKOSPI < 25: risk-on (정상)
- KOSPI 5일선 > 20일선 (상승추세): risk-on
- 전일 NASDAQ +0.5% 이상: risk-on

3개 중 2개 이상 충족 시 ACTIVE. 미만 시 IDLE → BUY signal 차단.

입력 source:
- VKOSPI: BarStore("VKOSPI", "1d") 또는 외부 fetch (TODO)
- KOSPI: BarStore("KOSPI", "1d") 최근 20봉
- NASDAQ: 외부 fetch (TODO — global_sector_leading 후속)

V1 = KOSPI 추세만 (즉시 사용 가능). VKOSPI/NASDAQ 는 추후.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ks_ws.storage.bars import BarStore


@dataclass(frozen=True)
class RegimeSnapshot:
    timestamp: datetime
    kospi_trend_up: bool   # 5일선 > 20일선
    vkospi_low: bool | None  # < 25 (None = data 없음)
    nasdaq_strong: bool | None  # +0.5% (None = data 없음)
    score: int  # 충족 조건 수 (0~3)
    active: bool  # score ≥ threshold

    def reason(self) -> str:
        parts = []
        if self.kospi_trend_up:
            parts.append("kospi_up")
        if self.vkospi_low:
            parts.append("vkospi_low")
        if self.nasdaq_strong:
            parts.append("nasdaq_strong")
        return ",".join(parts) if parts else "none"


class RegimeGate:
    """V1: KOSPI 5일 vs 20일 추세 + 외부 source 확장 가능.

    `is_active()` 가 False 면 BUY 차단 의도. 호출자 (Allocator / strategy) 가
    이걸 보고 weight 0 처리.
    """

    def __init__(
        self,
        bar_store: BarStore | None = None,
        *,
        kospi_symbol: str = "KOSPI",
        min_score: int = 1,  # default: KOSPI 추세 하나만 OK (V1)
    ) -> None:
        self.bar_store = bar_store
        self.kospi_symbol = kospi_symbol
        self.min_score = min_score
        self._last_snapshot: RegimeSnapshot | None = None
        self._last_kospi_check: datetime | None = None
        # 외부 inputs (caller 가 update)
        self._vkospi: float | None = None
        self._nasdaq_pct: float | None = None

    def set_vkospi(self, value: float | None) -> None:
        self._vkospi = value

    def set_nasdaq_prev_close_pct(self, pct: float | None) -> None:
        self._nasdaq_pct = pct

    def _kospi_trend_up(self, now: datetime) -> bool:
        if self.bar_store is None:
            return True  # data 없으면 conservative default True
        bars = list(self.bar_store.read(self.kospi_symbol, "1d"))
        if len(bars) < 20:
            return True
        recent = bars[-20:]
        ma5 = sum(b.close for b in recent[-5:]) / 5
        ma20 = sum(b.close for b in recent) / 20
        return ma5 > ma20

    def snapshot(self, now: datetime | None = None) -> RegimeSnapshot:
        from datetime import UTC
        ts = now or datetime.now(UTC)
        kospi_up = self._kospi_trend_up(ts)
        vkospi_low = (self._vkospi is not None and self._vkospi < 25)
        nasdaq_strong = (self._nasdaq_pct is not None and self._nasdaq_pct > 0.5)
        score = (
            (1 if kospi_up else 0)
            + (1 if vkospi_low else 0)
            + (1 if nasdaq_strong else 0)
        )
        active = score >= self.min_score
        snap = RegimeSnapshot(
            timestamp=ts,
            kospi_trend_up=kospi_up,
            vkospi_low=vkospi_low if self._vkospi is not None else None,
            nasdaq_strong=nasdaq_strong if self._nasdaq_pct is not None else None,
            score=score, active=active,
        )
        self._last_snapshot = snap
        return snap

    def is_active(self, now: datetime | None = None) -> bool:
        return self.snapshot(now).active
