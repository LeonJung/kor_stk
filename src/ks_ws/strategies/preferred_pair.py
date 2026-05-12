"""PreferredCommonPairStrategy (H) — 우선주/본주 가격 비율 평균 회귀.

book technical_strategy.md 의 H:
- entry: 우선주/본주 비율이 N일 이동평균 대비 ±2σ 이탈
- exit: 평균 회귀 시 / ±3σ 이탈 시 손절
- regime: 횡보장 또는 하락장 (상승장에선 default 비활성)
- 철학: "하다하다 우선주로도 돈놀이" 분위기에서만 유의미

Position 관리: 양방향 동시 진입 (시장중립). 비율이 너무 높음 = 우선주 비싼
상태 = 우선주 SELL + 본주 BUY. 비율이 너무 낮음 = 우선주 싼 상태 = 우선주
BUY + 본주 SELL.

V1: 단순 ratio rolling mean/std 추적, σ 기반 trigger. RegimeGate 외부 wrap
필수 (sideways/downtrend 만 활성).

Pair 정의: pairs = {preferred_symbol: common_symbol}.
"""

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from ks_ws.domain import Side, Signal, Tick
from ks_ws.strategies.base import Strategy


@dataclass
class _PairState:
    preferred: str
    common: str
    pref_last_price: int = 0
    common_last_price: int = 0
    ratios: deque[float] = field(default_factory=lambda: deque(maxlen=90))


@dataclass
class _Position:
    pair_key: str  # preferred symbol identifier
    direction: str  # "long_pref_short_common" or "long_common_short_pref"
    entry_ratio: float
    entry_time: datetime


class PreferredCommonPairStrategy(Strategy):
    name = "preferred_common_pair"

    def __init__(
        self,
        *,
        pairs: dict[str, str],
        entry_sigma: float = 2.0,
        stop_sigma: float = 3.0,
        warmup_samples: int = 30,
        confidence: float = 0.4,
    ) -> None:
        if not pairs:
            raise ValueError("pairs must not be empty")
        if entry_sigma <= 0 or stop_sigma <= entry_sigma:
            raise ValueError("stop_sigma must exceed entry_sigma > 0")
        if warmup_samples < 5:
            raise ValueError("warmup_samples must be >= 5")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        self.pairs = dict(pairs)  # preferred -> common
        self.entry_sigma = entry_sigma
        self.stop_sigma = stop_sigma
        self.warmup_samples = warmup_samples
        self.confidence = confidence
        self._symbol_to_pair: dict[str, str] = {}
        self._states: dict[str, _PairState] = {}
        for pref, common in self.pairs.items():
            state = _PairState(preferred=pref, common=common)
            self._states[pref] = state
            self._symbol_to_pair[pref] = pref
            self._symbol_to_pair[common] = pref
        self._open: dict[str, _Position] = {}  # keyed by preferred symbol

    def on_tick(self, tick: Tick) -> list[Signal]:
        pair_key = self._symbol_to_pair.get(tick.symbol)
        if pair_key is None:
            return []
        state = self._states[pair_key]
        if tick.symbol == state.preferred:
            state.pref_last_price = tick.price
        else:
            state.common_last_price = tick.price

        if state.pref_last_price <= 0 or state.common_last_price <= 0:
            return []

        ratio = state.pref_last_price / state.common_last_price
        state.ratios.append(ratio)

        # Manage open positions
        pos = self._open.get(pair_key)
        if pos is not None:
            return self._maybe_exit(tick, pos, state, ratio)

        # Need warmup
        if len(state.ratios) < self.warmup_samples:
            return []

        mean = statistics.fmean(state.ratios)
        sigma = statistics.pstdev(state.ratios)
        if sigma <= 0:
            return []
        deviation = (ratio - mean) / sigma

        if deviation >= self.entry_sigma:
            # ratio too high → SELL preferred + BUY common
            self._open[pair_key] = _Position(
                pair_key=pair_key,
                direction="long_common_short_pref",
                entry_ratio=ratio,
                entry_time=tick.timestamp,
            )
            return [
                Signal(
                    symbol=state.preferred, side=Side.SELL, confidence=self.confidence,
                    strategy=self.name, timestamp=tick.timestamp,
                    note=f"pair ratio {deviation:+.2f}σ — short pref",
                ),
                Signal(
                    symbol=state.common, side=Side.BUY, confidence=self.confidence,
                    strategy=self.name, timestamp=tick.timestamp,
                    note=f"pair ratio {deviation:+.2f}σ — long common",
                ),
            ]
        if deviation <= -self.entry_sigma:
            self._open[pair_key] = _Position(
                pair_key=pair_key,
                direction="long_pref_short_common",
                entry_ratio=ratio,
                entry_time=tick.timestamp,
            )
            return [
                Signal(
                    symbol=state.preferred, side=Side.BUY, confidence=self.confidence,
                    strategy=self.name, timestamp=tick.timestamp,
                    note=f"pair ratio {deviation:+.2f}σ — long pref",
                ),
                Signal(
                    symbol=state.common, side=Side.SELL, confidence=self.confidence,
                    strategy=self.name, timestamp=tick.timestamp,
                    note=f"pair ratio {deviation:+.2f}σ — short common",
                ),
            ]
        return []

    def _maybe_exit(
        self,
        tick: Tick,
        pos: _Position,
        state: _PairState,
        ratio: float,
    ) -> list[Signal]:
        mean = statistics.fmean(state.ratios)
        sigma = statistics.pstdev(state.ratios)
        if sigma <= 0:
            return []
        deviation = (ratio - mean) / sigma

        # Take-profit: reverted to (or beyond) mean
        if pos.direction == "long_common_short_pref" and deviation <= 0:
            return self._close(tick, pos, state, "ratio reverted to mean")
        if pos.direction == "long_pref_short_common" and deviation >= 0:
            return self._close(tick, pos, state, "ratio reverted to mean")

        # Stop-loss: further divergence
        if abs(deviation) >= self.stop_sigma:
            return self._close(tick, pos, state, f"stop {deviation:+.2f}σ", urgency="high")
        return []

    def _close(
        self,
        tick: Tick,
        pos: _Position,
        state: _PairState,
        note: str,
        urgency: str = "normal",
    ) -> list[Signal]:
        del self._open[pos.pair_key]
        # Reverse the open positions
        if pos.direction == "long_common_short_pref":
            return [
                Signal(symbol=state.preferred, side=Side.BUY, confidence=1.0, urgency=urgency,  # type: ignore[arg-type]
                       strategy=self.name, timestamp=tick.timestamp, note=note),
                Signal(symbol=state.common, side=Side.SELL, confidence=1.0, urgency=urgency,  # type: ignore[arg-type]
                       strategy=self.name, timestamp=tick.timestamp, note=note),
            ]
        return [
            Signal(symbol=state.preferred, side=Side.SELL, confidence=1.0, urgency=urgency,  # type: ignore[arg-type]
                   strategy=self.name, timestamp=tick.timestamp, note=note),
            Signal(symbol=state.common, side=Side.BUY, confidence=1.0, urgency=urgency,  # type: ignore[arg-type]
                   strategy=self.name, timestamp=tick.timestamp, note=note),
        ]

    def open_positions(self) -> dict[str, _Position]:
        return dict(self._open)
