"""ClosingBetStrategy (I 도지 종가베팅) — 13:30 KST 이후 도지 캔들 종목에
종가 베팅, 다음 거래일 시초가 청산.

book technical_strategy.md 의 I:
- entry: 일봉 도지 + 13:30 이후 거래대금 평균 이상 + 최근 N일 추세 + 방향
- exit: 다음날 시초가 +2% 청산 / 시초가 -3% 갭 다운 손절
- hold: overnight, 1회 진입
- 철학: 도지 = 시장 망설임, 망설임 끝의 선택은 강함. 추세 컨펌 + 도지 결합 시만.

V1 단순 구현: DojiCandle event 받으면 BUY signal 발행. 다음 거래일 first
tick 으로 entry 시각 갱신 (entry_price 캡처). 다음날 시초가 ±N% 도달 시 SELL.
시간 게이트는 외부 TimeWindowGate 또는 Scheduler 호출로 enforce 권장
(13:30 이전엔 DojiCandle 자체가 emit 안 되어야 하지만 V1 은 strategy 자체가
last seen tick timestamp 의 KST hour 가 13:30+ 인지만 확인).
"""

import contextlib
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ks_ws.domain import Side, Signal, Tick
from ks_ws.events import DojiCandle, Event
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class _Position:
    symbol: str
    entry_time: datetime
    entry_price: int
    open_day: int  # year*366 + julian — for "next day" detection


class ClosingBetStrategy(Strategy):
    name = "closing_bet"

    def __init__(
        self,
        *,
        watchlist: set[str] | None = None,
        entry_after_kst: time = time(13, 30),
        take_profit_pct: float = 2.0,
        stop_loss_pct: float = 3.0,
        confidence: float = 0.5,
        review_log: TradeReviewLog | None = None,
    ) -> None:
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("take_profit_pct and stop_loss_pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        self.watchlist = set(watchlist) if watchlist else None
        self.entry_after_kst = entry_after_kst
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.confidence = confidence
        self.review_log = review_log
        self._open: dict[str, _Position] = {}
        self._last_observed_kst: datetime | None = None

    def _record_review(self, pos: _Position, tick: Tick, *, exit_reason: str,
                       exit_note: str) -> None:
        if self.review_log is None:
            return
        pnl = tick.price - pos.entry_price
        with contextlib.suppress(Exception):
            self.review_log.record(TradeReview(
                strategy=self.name, symbol=tick.symbol,
                entry_ts=pos.entry_time, entry_price=pos.entry_price, qty=1,
                exit_ts=tick.timestamp, exit_price=tick.price,
                pnl_krw=pnl, exit_reason=exit_reason,
                entry_note=f"closing_bet doji overnight (open_day={pos.open_day})",
                exit_note=exit_note,
            ))

    def on_tick(self, tick: Tick) -> list[Signal]:
        self._last_observed_kst = tick.timestamp.astimezone(_KST)
        pos = self._open.get(tick.symbol)
        if pos is None:
            return []
        # If this is the FIRST tick of the next trading day (or later) → captures
        # the open price as our entry reference (we held overnight)
        tick_day = _ordinal(tick.timestamp)
        if tick_day > pos.open_day and pos.entry_price == 0:
            pos.entry_price = tick.price
            return []
        if pos.entry_price == 0:
            # still pre-next-day; ignore (shouldn't normally happen)
            return []
        # TP / SL on the next-day session
        tp = pos.entry_price * (1 + self.take_profit_pct / 100)
        sl = pos.entry_price * (1 - self.stop_loss_pct / 100)
        if tick.price >= tp:
            del self._open[tick.symbol]
            self._record_review(pos, tick, exit_reason="TP",
                                exit_note=f"TP @ {tick.price}")
            return [self._exit(tick, note=f"take-profit @ {tick.price}")]
        if tick.price <= sl:
            del self._open[tick.symbol]
            self._record_review(pos, tick, exit_reason="SL",
                                exit_note=f"SL @ {tick.price}")
            return [self._exit(tick, note=f"stop-loss @ {tick.price}", urgency="high")]
        return []

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, DojiCandle):
            return []
        if self.watchlist is not None and event.symbol not in self.watchlist:
            return []
        if event.symbol in self._open:
            return []
        # Time gate — must be after entry_after_kst (default 13:30)
        local_t = event.timestamp.astimezone(_KST).time()
        if local_t < self.entry_after_kst:
            return []
        # Open a position (entry_price filled by next-day first tick)
        self._open[event.symbol] = _Position(
            symbol=event.symbol,
            entry_time=event.timestamp,
            entry_price=0,
            open_day=_ordinal(event.timestamp),
        )
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                confidence=self.confidence,
                strategy=self.name,
                timestamp=event.timestamp,
                note=f"doji bet (body={event.body_pct:.2f}% range={event.range_pct:.2f}%)",
            )
        ]

    def _exit(self, tick: Tick, *, note: str, urgency: str = "normal") -> Signal:
        return Signal(
            symbol=tick.symbol,
            side=Side.SELL,
            confidence=1.0,
            urgency=urgency,  # type: ignore[arg-type]
            strategy=self.name,
            timestamp=tick.timestamp,
            note=note,
        )

    def open_positions(self) -> dict[str, _Position]:
        return dict(self._open)


def _ordinal(ts: datetime) -> int:
    """Calendar-day ordinal in KST. Used to detect "next trading day"."""
    return ts.astimezone(_KST).date().toordinal()
