"""DualThrustStrategy — Michael Chalek 양방향 변동성 돌파.

알고리즘:
- range = max(N일 high - N일 low close range 의 변형). 보통:
    range = max(HH - LC, HC - LL)
    HH = 최근 N일 high max, LC = 최근 N일 close min,
    HC = 최근 N일 close max, LL = 최근 N일 low min.
- buy_trigger = day_open + k1 * range
- sell_trigger = day_open - k2 * range
- price > buy_trigger → BUY
- price < sell_trigger → SELL (BUY 만 라이브; SELL trigger 는 보유 시 청산)

V1: BUY only (현물). N=5, k1=0.5, k2=0.5 default.
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


@dataclass
class _Pos:
    entry: int
    entry_time: datetime
    buy_trigger: int
    sell_trigger: int
    tp_price: int | None = None
    sl_price: int | None = None


def compute_dual_thrust_range(bars: Sequence[Bar]) -> int:
    """range = max(HH - LC, HC - LL) from the last N daily bars."""
    if len(bars) < 2:
        return 0
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    return max(max(highs) - min(closes), max(closes) - min(lows))


class DualThrustStrategy(Strategy):
    name = "dual_thrust"
    style = "day_trade"  # 사용자 룰 (2026-05-15)

    def __init__(
        self,
        *,
        ranges: dict[str, int],  # symbol → precomputed range
        k1: float = 0.5,
        k2: float = 0.5,
        take_profit_pct: float = 3.0,
        stop_loss_pct: float = 2.0,
        max_hold_minutes: int = 240,
        confidence: float = 0.6,
        review_log: TradeReviewLog | None = None,
        atr_provider=None,
    ) -> None:
        if not 0 < k1 < 2 or not 0 < k2 < 2:
            raise ValueError("k1, k2 must be in (0, 2)")
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0, 1]")
        self.ranges = dict(ranges)
        self.k1 = k1
        self.k2 = k2
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.confidence = confidence
        self.review_log = review_log
        self.atr_provider = atr_provider
        self._open: dict[str, _Pos] = {}
        self._day_open: dict[str, tuple[object, int]] = {}
        self._was_above: dict[str, tuple[object, bool]] = {}
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
                entry_note=f"dual_thrust: buy_trig={pos.buy_trigger} entry={pos.entry}",
                exit_note=exit_note,
            ))

    def _kst_date(self, tick: Tick) -> object:
        return tick.timestamp.astimezone(_KST).date()

    def on_tick(self, tick: Tick) -> list[Signal]:
        rng = self.ranges.get(tick.symbol)
        if not rng or rng <= 0:
            return []
        kst_date = self._kst_date(tick)

        # Track day open (first tick of KST date)
        day_rec = self._day_open.get(tick.symbol)
        if day_rec is None or day_rec[0] != kst_date:
            self._day_open[tick.symbol] = (kst_date, tick.price)
        day_open = self._day_open[tick.symbol][1]
        buy_trig = int(day_open + self.k1 * rng)

        # Exit
        pos = self._open.get(tick.symbol)
        if pos is not None:
            tp = pos.tp_price if pos.tp_price is not None else pos.entry * (1 + self.take_profit_pct / 100)
            sl = pos.sl_price if pos.sl_price is not None else pos.entry * (1 - self.stop_loss_pct / 100)
            # Exit on sell_trigger (downside breakout) 도 SELL 가능
            if tick.price < pos.sell_trigger:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="SL",
                                    exit_note=f"sell trig @ {tick.price}")
                return [self._sig(tick, Side.SELL, urgency="high",
                                  note=f"DT sell trig @ {tick.price}")]
            if tick.price >= tp:
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

        # Entry — edge detection (below → above buy_trigger)
        prev_rec = self._was_above.get(tick.symbol)
        prev_above = prev_rec[1] if (prev_rec and prev_rec[0] == kst_date) else False
        curr_above = tick.price > buy_trig
        self._was_above[tick.symbol] = (kst_date, curr_above)

        if not curr_above or prev_above:
            return []
        day_key = (tick.symbol, kst_date)
        if day_key in self._entered_today:
            return []
        sell_trig = int(day_open - self.k2 * rng)
        self._entered_today.add(day_key)
        from ks_ws.strategies._atr_helper import resolve_tp_sl
        tp_price, sl_price = resolve_tp_sl(
            tick.price, tick.symbol,
            atr_provider=self.atr_provider, style=self.style,
            fallback_tp_pct=self.take_profit_pct,
            fallback_sl_pct=self.stop_loss_pct,
        )
        self._open[tick.symbol] = _Pos(
            entry=tick.price, entry_time=tick.timestamp,
            buy_trigger=buy_trig, sell_trigger=sell_trig,
            tp_price=tp_price, sl_price=sl_price,
        )
        return [Signal(
            symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
            strategy=self.name, timestamp=tick.timestamp,
            note=f"dual_thrust: open={day_open} buy={buy_trig} k1={self.k1} "
                 f"range={rng}",
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


def compute_dual_thrust_ranges(
    bar_store: BarStore, symbols: list[str], *, lookback: int = 5,
) -> dict[str, int]:
    """For each symbol return DualThrust range from the last N daily bars."""
    out: dict[str, int] = {}
    for sym in symbols:
        bars = list(bar_store.read(sym, "1d"))
        if len(bars) < lookback:
            continue
        out[sym] = compute_dual_thrust_range(bars[-lookback:])
    return out
