"""LiveBreakoutStrategy — 60일 신고가 돌파 + 거래량 ↑ → BUY (live 버전).

Backtest V2 의 'breakout' simulator (81% win, +6.76M / 60일 lookback) 의
라이브 적용. 분봉 close 가 60-day daily close 의 max 를 **신규 돌파**
+ 직전 tick window 거래량의 1.5배면 BUY 1회 진입. 이후 +tp / -sl 도달 시 SELL.

진짜 돌파매매 (책 = 만쥬/원정연 Section 10) 사상 반영:
- **edge detection**: 가격이 60일 high 를 *아래에서 위로 cross* 하는 순간만 trigger.
  단순 above-high 가 아니라 prev_above → curr_above 의 false→true 전이.
- **same-day single entry**: 동일 종목·동일 일자에 1회 진입만 허용.
  TP/SL/timeout 청산 후에도 같은 날 재진입 X. (다음 거래일은 다시 가능)
- 위 두 가드가 추가되기 전엔 가격이 high 위에 머무는 동안 sell-rebuy
  churn (사팔사팔) 이 발생했음 → 5/12 paper trade 에서 009150 13B/6S 등.

Per-symbol state:
- high60: 60일 일봉 close 의 max (외부 주입, 매일 1회 update)
- entry: 매수 가격 (진입 후)
- entry_time: 진입 시각
"""

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from ks_ws.domain import Side, Signal, Tick
from ks_ws.storage.trade_review import TradeReview, TradeReviewLog
from ks_ws.strategies.base import Strategy


@dataclass
class _Pos:
    entry: int
    entry_time: datetime


class LiveBreakoutStrategy(Strategy):
    name = "breakout"

    def __init__(
        self,
        *,
        high60: dict[str, int],
        take_profit_pct: float = 2.0,
        stop_loss_pct: float = 3.0,
        max_hold_minutes: int = 60,
        confidence: float = 0.7,
        review_log: TradeReviewLog | None = None,
    ) -> None:
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0,1]")
        self.high60 = dict(high60)
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.confidence = confidence
        self.review_log = review_log
        self._open: dict[str, _Pos] = {}
        self._recent_ticks: dict[str, list[Tick]] = {}
        # edge detection: was previous tick above high60?
        self._was_above: dict[str, bool] = {}
        # same-day single entry guard: (symbol, date) already-entered set
        self._entered_today: set[tuple[str, object]] = set()

    def _record_review(self, pos: _Pos, tick: Tick, *, exit_reason: str,
                       exit_note: str) -> None:
        if self.review_log is None:
            return
        pnl = tick.price - pos.entry  # qty=1 hint (Allocator decides real qty)
        with contextlib.suppress(Exception):
            self.review_log.record(TradeReview(
                strategy=self.name, symbol=tick.symbol,
                entry_ts=pos.entry_time, entry_price=pos.entry, qty=1,
                exit_ts=tick.timestamp, exit_price=tick.price,
                pnl_krw=pnl, exit_reason=exit_reason,
                entry_note=f"breakout: {pos.entry} > 60d high",
                exit_note=exit_note,
            ))

    def on_tick(self, tick: Tick) -> list[Signal]:
        recents = self._recent_ticks.setdefault(tick.symbol, [])
        recents.append(tick)
        if len(recents) > 50:
            recents.pop(0)

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

        h60 = self.high60.get(tick.symbol)
        if h60 is None:
            return []

        is_above = tick.price > h60
        was_above = self._was_above.get(tick.symbol, False)
        self._was_above[tick.symbol] = is_above

        # B: edge detection — only the false→true cross qualifies.
        if not is_above or was_above:
            return []

        # A: same-day single entry — block re-entry on the same KST date.
        # tick.timestamp.date() — caller is responsible for using KST tz if relevant;
        # for cross-day boundary semantics the date in tick's own tz is sufficient.
        day_key = (tick.symbol, tick.timestamp.date())
        if day_key in self._entered_today:
            return []

        if len(recents) < 20:
            return []
        avg_vol = sum(t.volume for t in recents[-21:-1]) / 20
        if avg_vol > 0 and tick.volume < avg_vol * 1.5:
            return []

        self._open[tick.symbol] = _Pos(entry=tick.price, entry_time=tick.timestamp)
        self._entered_today.add(day_key)
        return [
            Signal(
                symbol=tick.symbol, side=Side.BUY, confidence=self.confidence,
                strategy=self.name, timestamp=tick.timestamp,
                note=f"breakout: {tick.price} > 60d high {h60} + vol*{tick.volume/avg_vol:.1f}",
            )
        ]

    def _sig(self, tick: Tick, side: Side, *, note: str, urgency: str = "normal") -> Signal:
        return Signal(
            symbol=tick.symbol, side=side, confidence=1.0,
            urgency=urgency,  # type: ignore[arg-type]
            strategy=self.name, timestamp=tick.timestamp, note=note,
        )

    def open_positions(self) -> dict[str, _Pos]:
        return dict(self._open)


def compute_high60(bar_store, codes: list[str]) -> dict[str, int]:
    """Return {symbol: max(close) of last 60 daily bars}."""
    out: dict[str, int] = {}
    for c in codes:
        bars = list(bar_store.read(c, "1d"))
        if not bars:
            continue
        recent = bars[-60:]
        out[c] = max(b.close for b in recent)
    return out
