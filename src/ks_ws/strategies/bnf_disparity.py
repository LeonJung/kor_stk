"""BNFDisparityStrategy — 종가/25MA 이격도 평균회귀 (코타니 본인 룰).

기본 알고리즘 (BNF/小手川隆 의 핵심 룰):
- 분봉 close 25 이동평균 (MA25) 대비 -15% 이상 이탈 시 oversold → BUY 진입
- price 가 MA25 회복 (또는 일정 % 가까이) 시 SELL (mean revert)
- 추가 -3% 이탈 = trend reversion 깨짐 = SL

V1 = tick + 분봉 history 결합:
- BarStore 의 1m 분봉 시퀀스에서 최근 25개 close 의 평균 = MA25
- 매 tick 의 가격이 MA25 * (1 - threshold_pct/100) 이하로 cross-down 시 BUY
- TP = MA25 도달 (또는 entry * (1 + tp_pct))
- SL = entry * (1 - sl_pct)
- timeout (max_hold_minutes) — 시간 만료 강제 청산 X (no_force_close 룰)
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ks_ws.domain import Bar, Side, Signal, Tick
from ks_ws.storage.bars import BarStore
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy


@dataclass
class _Pos:
    entry: int
    entry_time: datetime
    ma25_at_entry: int


class BNFDisparityStrategy(Strategy):
    name = "bnf_disparity"

    def __init__(
        self,
        *,
        ma25_provider,
        disparity_pct: float = 15.0,
        take_profit_pct: float = 5.0,
        stop_loss_pct: float = 3.0,
        confidence: float = 0.55,
        max_hold_minutes: int = 480,
        review_log: TradeReviewLog | None = None,
    ) -> None:
        if disparity_pct <= 0 or take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0, 1]")
        self.ma25_provider = ma25_provider
        self.disparity_pct = disparity_pct
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.confidence = confidence
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.review_log = review_log
        self._open: dict[str, _Pos] = {}
        self._was_below: dict[str, bool] = {}
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
                entry_note=f"bnf_disp: ma25={pos.ma25_at_entry} entry={pos.entry}",
                exit_note=exit_note,
            ))

    def on_tick(self, tick: Tick) -> list[Signal]:
        ma25 = self.ma25_provider(tick.symbol)
        if ma25 is None or ma25 <= 0:
            return []

        # Exit
        pos = self._open.get(tick.symbol)
        if pos is not None:
            tp = pos.entry * (1 + self.take_profit_pct / 100)
            sl = pos.entry * (1 - self.stop_loss_pct / 100)
            # TP 1: MA25 회복
            if tick.price >= ma25:
                del self._open[tick.symbol]
                self._record_review(pos, tick, exit_reason="TP",
                                    exit_note=f"MA25 revert @ {tick.price}")
                return [self._sig(tick, Side.SELL,
                                  note=f"BNF MA25 revert @ {tick.price}")]
            # TP 2: entry +tp_pct (백업)
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

        # Entry — edge detection (above → below threshold band)
        threshold = ma25 * (1 - self.disparity_pct / 100)
        prev_below = self._was_below.get(tick.symbol, False)
        curr_below = tick.price <= threshold
        self._was_below[tick.symbol] = curr_below

        from datetime import UTC
        kst_date = tick.timestamp.astimezone(UTC).date()
        if not curr_below or prev_below:
            return []
        day_key = (tick.symbol, kst_date)
        if day_key in self._entered_today:
            return []
        self._entered_today.add(day_key)
        self._open[tick.symbol] = _Pos(
            entry=tick.price, entry_time=tick.timestamp,
            ma25_at_entry=int(ma25),
        )
        disparity = (tick.price - ma25) / ma25 * 100
        return [Signal(
            symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
            strategy=self.name, timestamp=tick.timestamp,
            note=f"bnf_disp: ma25={int(ma25)} price={tick.price} "
                 f"disparity={disparity:+.1f}%",
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


class BarStoreMA25Provider:
    """BarStore("1m") 최근 25개 close 평균을 lazy fetch + 캐시 (5분 TTL)."""

    def __init__(
        self, bar_store: BarStore, *, lookback: int = 25,
        ttl_seconds: int = 300,
    ) -> None:
        self._bar_store = bar_store
        self.lookback = lookback
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[datetime, int | None]] = {}

    def __call__(self, symbol: str) -> int | None:
        cached = self._cache.get(symbol)
        from datetime import UTC
        from datetime import datetime as _dt
        now = _dt.now(UTC)
        if cached and (now - cached[0]).total_seconds() < self.ttl:
            return cached[1]
        bars: Sequence[Bar] = list(self._bar_store.read(symbol, "1m"))
        if len(bars) < self.lookback:
            self._cache[symbol] = (now, None)
            return None
        ma = int(sum(b.close for b in bars[-self.lookback:]) / self.lookback)
        self._cache[symbol] = (now, ma)
        return ma
