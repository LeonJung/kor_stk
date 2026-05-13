"""ColorStreakStrategy — N 양봉 연속 후 추세 진입 (양봉연속).

memory mapping `color_streak` = 양봉연속.

알고리즘:
- BarStore 의 1d (또는 1m) 시퀀스에서 최근 N 봉이 모두 양봉 (close > open)
- N 연속 양봉 setup 인 종목 → 다음 거래일 / 다음 분봉 진입 후보
- tick 가 setup 종목의 prev_close 위로 cross 시 BUY
- TP / SL / max_hold / no force_close

V1 = 일봉 streak 사용. 최근 3-5 일봉 연속 양봉.
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
    streak_n: int


class ColorStreakStrategy(Strategy):
    name = "color_streak"

    def __init__(
        self,
        *,
        setup: dict[str, tuple[int, int]],  # symbol → (prev_close, streak_n)
        take_profit_pct: float = 3.0,
        stop_loss_pct: float = 2.0,
        max_hold_minutes: int = 360,
        confidence: float = 0.6,
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
                entry_note=f"color_streak={pos.streak_n} 양봉 cross @ {pos.entry}",
                exit_note=exit_note,
            ))

    def _kst_date(self, tick: Tick) -> object:
        return tick.timestamp.astimezone(_KST).date()

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
            if tick.timestamp - pos.entry_time >= self.max_hold:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="timeout",
                                    exit_note="hold timeout")
                return [self._sig(tick, Side.SELL, note="hold timeout")]
            return []

        # Entry — only on streak setup
        rec = self.setup.get(tick.symbol)
        if not rec:
            return []
        prev_close, streak_n = rec
        if prev_close <= 0 or streak_n < 2:
            return []
        prev_rec = self._was_above.get(tick.symbol)
        prev_above = prev_rec[1] if (prev_rec and prev_rec[0] == kst_date) else False
        curr_above = tick.price > prev_close
        self._was_above[tick.symbol] = (kst_date, curr_above)

        if not curr_above or prev_above:
            return []
        day_key = (tick.symbol, kst_date)
        if day_key in self._entered_today:
            return []
        self._entered_today.add(day_key)
        self._open[tick.symbol] = _Pos(
            entry=tick.price, entry_time=tick.timestamp, streak_n=streak_n,
        )
        return [Signal(
            symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
            strategy=self.name, timestamp=tick.timestamp,
            note=f"color_streak={streak_n} prev_close={prev_close} @ {tick.price}",
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


def count_color_streak(bars: Sequence[Bar]) -> int:
    """Return number of consecutive green bars ending at the last bar.
    A green bar = close > open. 0 if last bar is not green."""
    n = 0
    for b in reversed(bars):
        if b.close > b.open:
            n += 1
        else:
            break
    return n


def compute_color_streak_setup(
    bar_store: BarStore, symbols: list[str], *, min_streak: int = 3,
) -> dict[str, tuple[int, int]]:
    """For each symbol, return (prev_close, streak_n) if streak >= min_streak."""
    out: dict[str, tuple[int, int]] = {}
    for sym in symbols:
        bars = list(bar_store.read(sym, "1d"))
        if len(bars) < min_streak:
            continue
        streak = count_color_streak(bars)
        if streak < min_streak:
            continue
        out[sym] = (bars[-1].close, streak)
    return out
