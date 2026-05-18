"""EntryGate — vb 룰의 entry 시점 추가 confluence filter.

3 layer (5/18 사용자 룰 — 승률 47% → 50%+):
  1) MTF — 종목 일봉 추세 align (close > 20MA AND > 50MA AND ATR 5일 상승)
  2) Market regime — KOSPI 일봉 close > 20MA AND > 50MA (시장 우호적)
  3) Time-of-day — 09:00~09:50 (단타핫존) + 13:30~15:30 (종가베팅 핫존) 만

VKOSPI 데이터 미보유 → KOSPI 일봉 추세로 regime gate 대체.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def _atr_from_bars(bars: list, period: int = 14) -> float:
    """N개 일봉의 ATR (Average True Range)."""
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(len(bars) - period, len(bars)):
        h = bars[i].high
        l = bars[i].low
        pc = bars[i - 1].close
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs) / len(trs)


@dataclass
class EntryGateConfig:
    enable_mtf: bool = True
    enable_market_regime: bool = True
    enable_time_window: bool = True
    # Time windows (KST hour:min). 15:20-15:30 = 장후 동시호가 (5/18 사용자 룰: 매매 X)
    time_windows: tuple = ((9, 0, 9, 50), (13, 30, 15, 20))
    # MTF — 종목 일봉
    mtf_ma_short: int = 20
    mtf_ma_long: int = 50
    mtf_require_ma50: bool = True       # False = MA20 만 (완화)
    mtf_require_atr_rising: bool = True
    # Market regime — KOSPI 일봉
    regime_ma_short: int = 20
    regime_ma_long: int = 50
    regime_require_ma50: bool = True    # False = MA20 만 (완화)


class EntryGate:
    def __init__(
        self,
        *,
        daily_history: dict[str, list],
        kospi_history: list | None = None,
        config: EntryGateConfig | None = None,
    ) -> None:
        self.daily_history = daily_history
        self.kospi_history = kospi_history or []
        self.cfg = config or EntryGateConfig()
        # Cache — MTF check is bar-stable (만 entry 시점 in same day = 같은 일봉 기준)
        self._mtf_cache: dict[str, bool] = {}

    # ---- 1) MTF ----
    def mtf_ok(self, symbol: str) -> bool:
        if not self.cfg.enable_mtf:
            return True
        if symbol in self._mtf_cache:
            return self._mtf_cache[symbol]
        bars = self.daily_history.get(symbol, [])
        ok = self._daily_trend_ok(
            bars,
            ma_short=self.cfg.mtf_ma_short,
            ma_long=self.cfg.mtf_ma_long,
            require_ma_long=self.cfg.mtf_require_ma50,
            require_atr_rising=self.cfg.mtf_require_atr_rising,
        )
        self._mtf_cache[symbol] = ok
        return ok

    @staticmethod
    def _daily_trend_ok(
        bars: list, *, ma_short: int = 20, ma_long: int = 50,
        require_ma_long: bool = True,
        require_atr_rising: bool = True,
    ) -> bool:
        min_bars = (ma_long if require_ma_long else ma_short) + 5
        if len(bars) < min_bars:
            return False
        last_close = bars[-1].close
        closes_short = [b.close for b in bars[-ma_short:]]
        ma_s = sum(closes_short) / ma_short
        if last_close <= ma_s:
            return False
        if require_ma_long:
            closes_long = [b.close for b in bars[-ma_long:]]
            ma_l = sum(closes_long) / ma_long
            if last_close <= ma_l:
                return False
        if require_atr_rising:
            atr_today = _atr_from_bars(bars[-15:], period=14)
            atr_5d_ago = _atr_from_bars(bars[-20:-5], period=14)
            if atr_today <= atr_5d_ago:
                return False
        return True

    # ---- 2) Market regime ----
    def regime_ok(self) -> bool:
        if not self.cfg.enable_market_regime:
            return True
        return self._daily_trend_ok(
            self.kospi_history,
            ma_short=self.cfg.regime_ma_short,
            ma_long=self.cfg.regime_ma_long,
            require_ma_long=self.cfg.regime_require_ma50,
            require_atr_rising=False,
        )

    # ---- 3) Time window ----
    def time_window_ok(self, ts: datetime) -> bool:
        if not self.cfg.enable_time_window:
            return True
        kst = ts.astimezone(_KST)
        cur = kst.hour * 60 + kst.minute
        for h1, m1, h2, m2 in self.cfg.time_windows:
            start = h1 * 60 + m1
            end = h2 * 60 + m2
            if start <= cur < end:
                return True
        return False

    # ---- combined ----
    def passes(self, symbol: str, ts: datetime) -> bool:
        return (
            self.time_window_ok(ts)
            and self.regime_ok()
            and self.mtf_ok(symbol)
        )
