"""ForeignFlowStrategy — 외국인 순매수 KRW spike → BUY (외국인수급).

memory `feedback_strategy_decisions` + technical mapping 8.2 (외국인수급):
- ForeignNetBuy event 의 ``delta_krw`` 가 spike threshold 이상 시 BUY
- 외인 매수가 지속될수록 더 큰 비중 → confidence = signal strength
- TP / SL / timeout
- same-day single entry

ForeignNetBuy 는 KIS investor-trade-by-stock-daily 의 분/시간 단위 변화량.
ForeignNetBuySource 가 60s polling 으로 publish.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ks_ws.domain import Side, Signal, Tick
from ks_ws.events import Event, ForeignNetBuy
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class _Pos:
    entry: int
    entry_time: datetime
    foreign_flow_at_entry: int
    tp_price: int | None = None
    sl_price: int | None = None


class ForeignFlowStrategy(Strategy):
    name = "foreign_flow"
    style = "mid_term"  # 사용자 룰 (2026-05-15) — 중기 2주-6개월

    def __init__(
        self,
        *,
        watchlist: set[str] | None = None,
        strong_threshold_krw: int = 100_000_000_000,  # 1000억 = strong
        take_profit_pct: float = 20.0,
        stop_loss_pct: float = 8.0,
        max_hold_minutes: int = 60 * 24 * 30,  # 30일 default (중기)
        confidence: float = 0.6,
        review_log: TradeReviewLog | None = None,
        atr_provider=None,
    ) -> None:
        if strong_threshold_krw <= 0:
            raise ValueError("strong_threshold_krw must be positive")
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0, 1]")
        self.watchlist = set(watchlist) if watchlist else None
        self.strong_threshold_krw = strong_threshold_krw
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.confidence = confidence
        self.review_log = review_log
        self.atr_provider = atr_provider
        self._open: dict[str, _Pos] = {}
        self._latest_flow: dict[str, int] = {}
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
                entry_note=f"foreign_flow: net={pos.foreign_flow_at_entry:+,}",
                exit_note=exit_note,
            ))

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, ForeignNetBuy):
            return []
        if self.watchlist is not None and event.symbol not in self.watchlist:
            return []
        self._latest_flow[event.symbol] = event.delta_krw
        # Entry on strong foreign net buy
        if event.delta_krw < self.strong_threshold_krw:
            return []
        if event.symbol in self._open:
            return []
        kst_date = event.timestamp.astimezone(_KST).date()
        day_key = (event.symbol, kst_date)
        if day_key in self._entered_today:
            return []
        # We don't have a price from this event (no Tick attached) — defer entry
        # to next tick. Mark intent via _latest_flow + check at on_tick.
        return []

    def on_tick(self, tick: Tick) -> list[Signal]:
        # Exit logic first
        pos = self._open.get(tick.symbol)
        if pos is not None:
            tp = pos.tp_price if pos.tp_price is not None else pos.entry * (1 + self.take_profit_pct / 100)
            sl = pos.sl_price if pos.sl_price is not None else pos.entry * (1 - self.stop_loss_pct / 100)
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

        # Entry — strong foreign net buy seen recently?
        flow = self._latest_flow.get(tick.symbol)
        if flow is None or flow < self.strong_threshold_krw:
            return []
        if self.watchlist is not None and tick.symbol not in self.watchlist:
            return []
        kst_date = tick.timestamp.astimezone(_KST).date()
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
            entry=tick.price, entry_time=tick.timestamp,
            foreign_flow_at_entry=flow,
            tp_price=tp_price, sl_price=sl_price,
        )
        # Consume the flow trigger (one entry per spike)
        self._latest_flow[tick.symbol] = 0
        return [Signal(
            symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
            strategy=self.name, timestamp=tick.timestamp,
            note=f"foreign_flow: 외인 net={flow:+,} KRW @ {tick.price}",
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
