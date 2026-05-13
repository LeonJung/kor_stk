"""NR7BreakoutStrategy — Narrow Range 7 일봉 돌파.

알고리즘 (Toby Crabel):
- 최근 7 일봉 중 가장 작은 range (high - low) 인 봉이 *오늘 이전* 일봉이면 다음
  거래일을 NR7 setup 으로 분류 (= 전일이 NR7).
- 다음 거래일 (= 오늘) 가격이 전일 high 위로 cross 시 BUY (변동성 expansion).
- 청산: TP / SL / max_hold 또는 장 종료 직전 timeout.

이론적 근거: range 압축 후 expansion = mean reversion 이 아닌 trend continuation
의 시작 신호 (volatility contraction → expansion).

V1 = tick 기반:
- 시작 시 종목별 (prev_high, is_nr7) 외부 주입.
- is_nr7=True 인 종목만 entry 가능. False 면 strategy 자체 idle.
- edge detection (below → above prev_high) + same-day single entry.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
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


class NR7BreakoutStrategy(Strategy):
    name = "nr7_breakout"

    def __init__(
        self,
        *,
        setup: dict[str, tuple[int, bool]],  # symbol → (prev_high, is_nr7)
        take_profit_pct: float = 3.0,
        stop_loss_pct: float = 2.0,
        max_hold_minutes: int = 360,
        confidence: float = 0.6,
        timeout_at_kst: time = time(15, 25),
        review_log: TradeReviewLog | None = None,
    ) -> None:
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0, 1]")
        self.setup = dict(setup)
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.confidence = confidence
        self.timeout_at_kst = timeout_at_kst
        self.review_log = review_log
        self._open: dict[str, _Pos] = {}
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
                entry_note=f"nr7: prev_high cross @ {pos.entry}",
                exit_note=exit_note,
            ))

    def _kst_date(self, tick: Tick) -> object:
        return tick.timestamp.astimezone(_KST).date()

    def _kst_time(self, tick: Tick) -> time:
        return tick.timestamp.astimezone(_KST).time()

    def on_tick(self, tick: Tick) -> list[Signal]:
        kst_date = self._kst_date(tick)

        # Exit
        pos = self._open.get(tick.symbol)
        if pos is not None:
            tp = pos.entry * (1 + self.take_profit_pct / 100)
            sl = pos.entry * (1 - self.stop_loss_pct / 100)
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
            if (
                tick.timestamp - pos.entry_time >= self.max_hold
                or self._kst_time(tick) >= self.timeout_at_kst
            ):
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="timeout",
                                    exit_note="NR7 timeout")
                return [self._sig(tick, Side.SELL, note="NR7 timeout")]
            return []

        # Entry — only on NR7-setup symbols
        rec = self.setup.get(tick.symbol)
        if not rec or not rec[1]:
            return []
        prev_high = rec[0]
        if prev_high <= 0:
            return []
        prev_rec = self._was_above.get(tick.symbol)
        prev_above = prev_rec[1] if (prev_rec and prev_rec[0] == kst_date) else False
        curr_above = tick.price > prev_high
        self._was_above[tick.symbol] = (kst_date, curr_above)

        if self._kst_time(tick) >= self.timeout_at_kst:
            return []
        if not curr_above or prev_above:
            return []
        day_key = (tick.symbol, kst_date)
        if day_key in self._entered_today:
            return []
        self._entered_today.add(day_key)
        self._open[tick.symbol] = _Pos(entry=tick.price, entry_time=tick.timestamp)
        return [Signal(
            symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
            strategy=self.name, timestamp=tick.timestamp,
            note=f"nr7_brk: prev_high={prev_high} @ {tick.price}",
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


def is_nr7(bars: Sequence[Bar]) -> bool:
    """최근 7 봉의 마지막 봉이 NR7 인지 (= 가장 작은 range)."""
    if len(bars) < 7:
        return False
    last7 = list(bars[-7:])
    ranges = [b.high - b.low for b in last7]
    return ranges[-1] == min(ranges) and ranges[-1] > 0


def compute_nr7_setup(
    bar_store: BarStore, symbols: list[str],
) -> dict[str, tuple[int, bool]]:
    """For each symbol return (prev_high, is_nr7).
    Prev = the most-recent fully-closed daily bar.
    """
    out: dict[str, tuple[int, bool]] = {}
    for sym in symbols:
        bars = list(bar_store.read(sym, "1d"))
        if not bars:
            continue
        out[sym] = (bars[-1].high, is_nr7(bars))
    return out
