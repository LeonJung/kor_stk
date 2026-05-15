"""PivotHalfPullbackStrategy — pivot point ~ R1 절반 pullback 후 회복 BUY.

memory mapping `pivot_half_pullback` = 피벗절반눌림.

Pivot point classical formula (전일 H/L/C):
- P = (H + L + C) / 3
- R1 = 2P - L
- S1 = 2P - H
- half_target = (P + R1) / 2  — pivot point 와 R1 사이의 중간

알고리즘:
- 시초가 가 P 위 → R1 까지 상승 후 half_target 까지 pullback → P 까지 다시
  못 떨어지면 → half_target 회복 시 BUY (이격 후 trend continuation).
- 단순화 V1:
  - 진입 = price 가 half_target 아래에서 위로 cross (단, 직전 N분 high 가 R1
    근처에 도달했어야)
  - TP = R1 도달 또는 entry * (1+tp_pct)
  - SL = pivot P 이탈
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ks_ws.domain import Bar, Side, Signal, Tick
from ks_ws.storage.bars import BarStore
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class PivotLevels:
    p: int
    r1: int
    s1: int
    half_up: int  # (P + R1) / 2


def compute_pivot_levels(prev_bar: Bar) -> PivotLevels:
    p = (prev_bar.high + prev_bar.low + prev_bar.close) // 3
    r1 = 2 * p - prev_bar.low
    s1 = 2 * p - prev_bar.high
    return PivotLevels(p=p, r1=r1, s1=s1, half_up=(p + r1) // 2)


@dataclass
class _Pos:
    entry: int
    entry_time: datetime
    pivot: PivotLevels
    tp_price: int | None = None
    sl_price: int | None = None


@dataclass
class _DayState:
    date: object
    levels: PivotLevels
    max_seen: int  # max tick price seen today (R1 touch detection)
    reached_r1_area: bool = False  # has price come within r1*0.99?


class PivotHalfPullbackStrategy(Strategy):
    name = "pivot_half_pullback"
    style = "day_trade"  # 사용자 룰 (2026-05-15)

    def __init__(
        self,
        *,
        pivots: dict[str, PivotLevels],
        take_profit_pct: float = 2.5,
        stop_loss_pct: float = 2.0,
        max_hold_minutes: int = 240,
        r1_proximity_pct: float = 1.0,  # R1 의 1% 안까지 도달해야 setup
        confidence: float = 0.6,
        review_log: TradeReviewLog | None = None,
        atr_provider=None,
    ) -> None:
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0, 1]")
        if r1_proximity_pct <= 0:
            raise ValueError("r1_proximity_pct must be positive")
        self.pivots = dict(pivots)
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.r1_proximity_pct = r1_proximity_pct
        self.confidence = confidence
        self.review_log = review_log
        self.atr_provider = atr_provider
        self._open: dict[str, _Pos] = {}
        self._day: dict[str, _DayState] = {}
        self._was_above_half: dict[str, bool] = {}
        self._entered_today: set[tuple[str, object]] = set()

    def _record_review(self, pos: _Pos, tick: Tick, *, exit_reason: str,
                       exit_note: str) -> None:
        if self.review_log is None:
            return
        with contextlib.suppress(Exception):
            self.review_log.record(TradeReview(
                strategy=self.name, symbol=tick.symbol,
                entry_ts=pos.entry_time, entry_price=pos.entry, qty=1,
                exit_ts=tick.timestamp, exit_price=tick.price,
                pnl_krw=tick.price - pos.entry,
                exit_reason=exit_reason,
                entry_note=f"pivot_pullback P={pos.pivot.p} R1={pos.pivot.r1} entry={pos.entry}",
                exit_note=exit_note,
            ))

    def _kst_date(self, tick: Tick) -> object:
        return tick.timestamp.astimezone(_KST).date()

    def on_tick(self, tick: Tick) -> list[Signal]:
        levels = self.pivots.get(tick.symbol)
        if levels is None or levels.r1 <= levels.p:
            return []

        kst_date = self._kst_date(tick)
        day = self._day.get(tick.symbol)
        if day is None or day.date != kst_date:
            day = _DayState(date=kst_date, levels=levels, max_seen=tick.price)
            self._day[tick.symbol] = day
        if tick.price > day.max_seen:
            day.max_seen = tick.price
        # R1 proximity reached?
        r1_threshold = levels.r1 * (1 - self.r1_proximity_pct / 100)
        if day.max_seen >= r1_threshold:
            day.reached_r1_area = True

        # Exit
        pos = self._open.get(tick.symbol)
        if pos is not None:
            tp = pos.tp_price if pos.tp_price is not None else pos.entry * (1 + self.take_profit_pct / 100)
            sl_pivot = pos.pivot.p  # pivot 이탈 = SL
            sl_atr = pos.sl_price if pos.sl_price is not None else pos.entry * (1 - self.stop_loss_pct / 100)
            sl = max(sl_pivot, sl_atr)
            if tick.price >= levels.r1 or tick.price >= tp:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="TP",
                                    exit_note=f"TP @ {tick.price}")
                return [self._sig(tick, Side.SELL, note=f"TP @ {tick.price}")]
            if tick.price <= sl:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="SL",
                                    exit_note=f"SL @ {tick.price}")
                return [self._sig(tick, Side.SELL, urgency="high",
                                  note=f"SL @ {tick.price}")]
            if tick.timestamp - pos.entry_time >= self.max_hold:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="timeout",
                                    exit_note="hold timeout")
                return [self._sig(tick, Side.SELL, note="hold timeout")]
            return []

        # Entry — must have already reached R1 area + price now crossing half_up
        # from below.
        if not day.reached_r1_area:
            return []
        prev_above = self._was_above_half.get(tick.symbol, False)
        curr_above = tick.price > levels.half_up
        self._was_above_half[tick.symbol] = curr_above

        if not curr_above or prev_above:
            return []
        # 진입 시점에 pivot P 위에 있어야 (S1 쪽으로 떨어진 상황 X)
        if tick.price <= levels.p:
            return []
        day_key = (tick.symbol, kst_date)
        if day_key in self._entered_today:
            return []
        self._entered_today.add(day_key)
        from ks_ws.strategies._atr_helper import resolve_tp_sl
        tp_price, sl_price = resolve_tp_sl(
            tick.price, tick.symbol,
            atr_provider=self.atr_provider, style=self.style,
            fallback_tp_pct=self.take_profit_pct,
            fallback_sl_pct=self.stop_loss_pct,
        )
        self._open[tick.symbol] = _Pos(
            entry=tick.price, entry_time=tick.timestamp, pivot=levels,
            tp_price=tp_price, sl_price=sl_price,
        )
        return [Signal(
            symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
            strategy=self.name, timestamp=tick.timestamp,
            note=f"pivot_half_pullback: P={levels.p} half={levels.half_up} "
                 f"R1={levels.r1} @ {tick.price}",
        )]

    def _sig(self, tick: Tick, side: Side, *, note: str,
             urgency: str = "normal") -> Signal:
        return Signal(
            symbol=tick.symbol, side=side, confidence=1.0,
            urgency=urgency,  # type: ignore[arg-type]
            strategy=self.name, timestamp=tick.timestamp, note=note,
        )

    def open_positions(self) -> dict[str, _Pos]:
        return dict(self._open)


def compute_pivots(
    bar_store: BarStore, symbols: Sequence[str],
) -> dict[str, PivotLevels]:
    out: dict[str, PivotLevels] = {}
    for sym in symbols:
        bars = list(bar_store.read(sym, "1d"))
        if not bars:
            continue
        out[sym] = compute_pivot_levels(bars[-1])
    return out
