"""TapeBurstStrategy — 분 단위 tick 수 평균의 N배 폭증 시 momentum BUY (체결폭주).

memory mapping `tape_burst` = 체결폭주.

알고리즘:
- 각 종목별로 분 단위 tick 카운트 누적
- 최근 1분 tick 수가 직전 N분 평균의 burst_ratio (default 3x) 이상 시 burst
- burst 발생 + 가격 직전 분 close 위 → BUY (직전 분과 같은 분 안에서)
- TP / SL / max_hold (짧음 = 15분 default)

V1: tick-only state machine. tick 마다 (분, 카운트) bucket 누적, 1분 경계에서
지난 분 vs 직전 N분 평균 비교.
"""

from __future__ import annotations

import contextlib
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ks_ws.domain import Side, Signal, Tick
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class _Pos:
    entry: int
    entry_time: datetime
    burst_ratio: float


@dataclass
class _SymState:
    current_minute: datetime | None = None
    current_count: int = 0
    history: deque = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.history is None:
            self.history = deque(maxlen=20)


class TapeBurstStrategy(Strategy):
    name = "tape_burst"

    def __init__(
        self,
        *,
        baseline_minutes: int = 10,
        burst_ratio: float = 3.0,
        min_baseline_count: int = 10,
        take_profit_pct: float = 1.5,
        stop_loss_pct: float = 1.0,
        max_hold_minutes: int = 15,
        confidence: float = 0.55,
        review_log: TradeReviewLog | None = None,
    ) -> None:
        if baseline_minutes < 3:
            raise ValueError("baseline_minutes must be >= 3")
        if burst_ratio <= 1.0:
            raise ValueError("burst_ratio must be > 1")
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0, 1]")
        self.baseline_minutes = baseline_minutes
        self.burst_ratio = burst_ratio
        self.min_baseline_count = min_baseline_count
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.confidence = confidence
        self.review_log = review_log
        self._open: dict[str, _Pos] = {}
        self._state: dict[str, _SymState] = {}
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
                entry_note=f"tape_burst ratio={pos.burst_ratio:.1f}x @ {pos.entry}",
                exit_note=exit_note,
            ))

    def _bucket_minute(self, ts: datetime) -> datetime:
        return ts.replace(second=0, microsecond=0)

    def _kst_date(self, tick: Tick) -> object:
        return tick.timestamp.astimezone(_KST).date()

    def on_tick(self, tick: Tick) -> list[Signal]:
        st = self._state.setdefault(tick.symbol, _SymState())
        minute = self._bucket_minute(tick.timestamp)

        # Detect minute boundary: flush previous minute into history,
        # check burst from the *just-closed* minute.
        burst_detected = False
        burst_ratio = 0.0
        if st.current_minute is None:
            st.current_minute = minute
            st.current_count = 1
        elif minute != st.current_minute:
            # new minute — close out previous
            closed_count = st.current_count
            closed_minute = st.current_minute
            st.history.append((closed_minute, closed_count))
            st.current_minute = minute
            st.current_count = 1
            # Compute burst from closed minute
            if len(st.history) >= self.baseline_minutes + 1:
                baseline_window = list(st.history)[-(self.baseline_minutes + 1):-1]
                baseline_avg = sum(c for _, c in baseline_window) / len(baseline_window)
                if baseline_avg >= self.min_baseline_count:
                    ratio = closed_count / baseline_avg
                    if ratio >= self.burst_ratio:
                        burst_detected = True
                        burst_ratio = ratio
        else:
            st.current_count += 1

        # Exit logic first
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

        # Entry on burst detection
        if not burst_detected:
            return []
        kst_date = self._kst_date(tick)
        day_key = (tick.symbol, kst_date)
        if day_key in self._entered_today:
            return []
        self._entered_today.add(day_key)
        self._open[tick.symbol] = _Pos(
            entry=tick.price, entry_time=tick.timestamp,
            burst_ratio=burst_ratio,
        )
        return [Signal(
            symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
            strategy=self.name, timestamp=tick.timestamp,
            note=f"tape_burst {burst_ratio:.1f}x @ {tick.price}",
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
