"""VWAPMeanReversionStrategy (F) — 장중 VWAP 평균 회귀 매매.

book technical_strategy.md 의 F:
- entry: VWAP 대비 -k·σ 이탈 + 거래량 spike (직전 5분 평균 ×3)
- exit: VWAP 회귀 시 / -2·σ 추가 이탈 시 손절
- regime: 횡보장 default 활성, 강한 호재 종목 제외 필터
- 철학: 추세가 아닌 평균 회귀. 큰 호재 종목엔 적용 X.

State:
- per-symbol VWAP running (Σ price·volume / Σ volume)
- price deviation 표준편차 σ rolling (최근 N tick)
- 직전 5분 거래량 평균
- 진입 시 entry σ 기록 → exit 가 VWAP 도달 시 청산

Tick 기반. Bar 도 받지만 (지정 timeframe) 1m bar 로부터 vwap baseline 갱신
가능 (V1 은 tick 만 사용해 단순화).
"""

import contextlib
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ks_ws.domain import Side, Signal, Tick
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy


@dataclass
class _SymbolState:
    sum_pv: float = 0.0  # Σ price * volume
    sum_v: float = 0.0  # Σ volume
    prices: deque[int] = field(default_factory=lambda: deque(maxlen=200))
    recent_volumes: deque[tuple[datetime, int]] = field(default_factory=deque)

    @property
    def vwap(self) -> float:
        return self.sum_pv / self.sum_v if self.sum_v > 0 else 0.0

    def update(self, tick: Tick) -> None:
        self.sum_pv += tick.price * tick.volume
        self.sum_v += tick.volume
        self.prices.append(tick.price)
        self.recent_volumes.append((tick.timestamp, tick.volume))

    def deviation_sigma(self) -> float:
        if len(self.prices) < 5:
            return 0.0
        mean = sum(self.prices) / len(self.prices)
        var = sum((p - mean) ** 2 for p in self.prices) / len(self.prices)
        return math.sqrt(var)

    def recent_volume_in_window(self, end: datetime, window: timedelta) -> int:
        # Trim old entries
        cutoff = end - window
        while self.recent_volumes and self.recent_volumes[0][0] < cutoff:
            self.recent_volumes.popleft()
        return sum(v for _, v in self.recent_volumes)


@dataclass
class _Position:
    symbol: str
    entry_price: int
    entry_time: datetime
    side: Side  # always BUY in V1 (mean reversion long-only)


class VWAPMeanReversionStrategy(Strategy):
    name = "vwap_reversion"

    def __init__(
        self,
        *,
        watchlist: set[str] | None = None,
        entry_sigma: float = 1.5,
        stop_sigma: float = 2.5,
        volume_spike_multiplier: float = 3.0,
        volume_window_seconds: int = 300,
        confidence: float = 0.5,
        review_log: TradeReviewLog | None = None,
    ) -> None:
        if entry_sigma <= 0 or stop_sigma <= entry_sigma:
            raise ValueError("stop_sigma must exceed entry_sigma > 0")
        if volume_spike_multiplier < 1:
            raise ValueError("volume_spike_multiplier must be >= 1")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        self.watchlist = set(watchlist) if watchlist else None
        self.entry_sigma = entry_sigma
        self.stop_sigma = stop_sigma
        self.volume_spike_multiplier = volume_spike_multiplier
        self.volume_window = timedelta(seconds=volume_window_seconds)
        self.confidence = confidence
        self.review_log = review_log
        self._state: dict[str, _SymbolState] = {}
        self._open: dict[str, _Position] = {}
        self._baseline_volume: dict[str, float] = {}

    def _record_review(self, pos: "_Position", tick: Tick, *, exit_reason: str,
                       exit_note: str) -> None:
        if self.review_log is None:
            return
        with contextlib.suppress(Exception):
            self.review_log.record(TradeReview(
                strategy=self.name, symbol=tick.symbol,
                entry_ts=pos.entry_time, entry_price=pos.entry_price, qty=1,
                exit_ts=tick.timestamp, exit_price=tick.price,
                pnl_krw=tick.price - pos.entry_price,
                exit_reason=exit_reason,
                entry_note=f"vwap_rev dip entry @ {pos.entry_price}",
                exit_note=exit_note,
            ))

    def on_tick(self, tick: Tick) -> list[Signal]:
        if self.watchlist is not None and tick.symbol not in self.watchlist:
            return []
        state = self._state.setdefault(tick.symbol, _SymbolState())
        state.update(tick)

        # Exit logic first
        pos = self._open.get(tick.symbol)
        if pos is not None:
            return self._maybe_exit(tick, pos, state)

        # Entry — needs enough history
        sigma = state.deviation_sigma()
        if sigma <= 0 or state.vwap <= 0:
            return []

        deviation = (tick.price - state.vwap) / sigma
        # mean reversion: only enter on -k·σ dip (long)
        if deviation > -self.entry_sigma:
            return []

        # Volume spike confirmation
        recent_v = state.recent_volume_in_window(tick.timestamp, self.volume_window)
        baseline = self._baseline_volume.get(tick.symbol, 0)
        if baseline <= 0:
            self._baseline_volume[tick.symbol] = recent_v / 10  # bootstrap proxy
            return []
        if recent_v < baseline * self.volume_spike_multiplier:
            return []

        # Enter
        self._open[tick.symbol] = _Position(
            symbol=tick.symbol,
            entry_price=tick.price,
            entry_time=tick.timestamp,
            side=Side.BUY,
        )
        return [
            Signal(
                symbol=tick.symbol,
                side=Side.BUY,
                confidence=self.confidence,
                strategy=self.name,
                timestamp=tick.timestamp,
                note=f"vwap dip {deviation:.2f}σ, vol_spike {recent_v / max(1, baseline):.1f}x",
            )
        ]

    def _maybe_exit(self, tick: Tick, pos: _Position, state: _SymbolState) -> list[Signal]:
        # Take profit when price returns to (or above) VWAP
        if tick.price >= state.vwap:
            del self._open[tick.symbol]
            self._record_review(pos, tick, exit_reason="TP",
                                exit_note=f"VWAP revert @ {tick.price}")
            return [
                Signal(
                    symbol=tick.symbol,
                    side=Side.SELL,
                    confidence=1.0,
                    strategy=self.name,
                    timestamp=tick.timestamp,
                    note=f"vwap mean-revert @ {tick.price} (vwap={state.vwap:.0f})",
                )
            ]
        # Stop loss on further deviation
        sigma = state.deviation_sigma()
        if sigma > 0:
            deviation = (tick.price - state.vwap) / sigma
            if deviation <= -self.stop_sigma:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="SL",
                                    exit_note=f"VWAP stop {deviation:.2f}sigma")
                return [
                    Signal(
                        symbol=tick.symbol,
                        side=Side.SELL,
                        confidence=1.0,
                        urgency="high",
                        strategy=self.name,
                        timestamp=tick.timestamp,
                        note=f"vwap stop {deviation:.2f}sigma",
                    )
                ]
        return []

    def open_positions(self) -> dict[str, _Position]:
        return dict(self._open)
