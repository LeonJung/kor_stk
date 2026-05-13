"""Pattern strategies — detector event → BUY signal.

Each pattern detector emits an Event (DoubleBottomDetected / BoxBreakoutDetected /
HeadShouldersDetected) when its pattern completes. The corresponding strategy
subscribes via on_event() and emits a BUY signal.

Exit logic identical to LiveBreakout — TP / SL / hold timeout. Same-day single
entry guard prevents the same pattern from re-firing within one day.

Strategies:
- DoubleBottomStrategy — W double-bottom breakout
- BoxBreakoutStrategy — N-day box range upside breakout + volume
- InverseHeadShouldersStrategy — H&S reversal (BUY)

All consume Tick on_tick for TP/SL exits while holding (parallels LiveBreakout).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ks_ws.domain import Side, Signal, Tick
from ks_ws.events import (
    BoxBreakoutDetected,
    DoubleBottomDetected,
    Event,
    HeadShouldersDetected,
)
from ks_ws.strategies.base import Strategy


@dataclass
class _Pos:
    entry: int
    entry_time: datetime


class _PatternStrategyBase(Strategy):
    """Common entry/exit machinery for pattern strategies."""

    name: str = "pattern_base"

    def __init__(
        self,
        *,
        take_profit_pct: float = 3.0,
        stop_loss_pct: float = 2.0,
        max_hold_minutes: int = 240,
        confidence: float = 0.6,
    ) -> None:
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("pct must be positive")
        if not 0 < confidence <= 1:
            raise ValueError("confidence in (0,1]")
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold = timedelta(minutes=max_hold_minutes)
        self.confidence = confidence
        self._open: dict[str, _Pos] = {}
        self._entered_today: set[tuple[str, object]] = set()

    def _exit_check(self, tick: Tick) -> list[Signal]:
        pos = self._open.get(tick.symbol)
        if pos is None:
            return []
        tp = pos.entry * (1 + self.take_profit_pct / 100)
        sl = pos.entry * (1 - self.stop_loss_pct / 100)
        if tick.price >= tp:
            del self._open[tick.symbol]
            return [self._sig(tick.symbol, tick.timestamp, Side.SELL,
                              note=f"TP @ {tick.price}")]
        if tick.price <= sl:
            del self._open[tick.symbol]
            return [self._sig(tick.symbol, tick.timestamp, Side.SELL,
                              urgency="high", note=f"SL @ {tick.price}")]
        if tick.timestamp - pos.entry_time >= self.max_hold:
            del self._open[tick.symbol]
            return [self._sig(tick.symbol, tick.timestamp, Side.SELL,
                              note="hold timeout")]
        return []

    def _enter(self, symbol: str, price: int, ts: datetime, *, note: str) -> list[Signal]:
        day_key = (symbol, ts.date())
        if day_key in self._entered_today or symbol in self._open:
            return []
        self._open[symbol] = _Pos(entry=price, entry_time=ts)
        self._entered_today.add(day_key)
        return [Signal(
            symbol=symbol, side=Side.BUY, confidence=self.confidence,
            strategy=self.name, timestamp=ts, note=note,
        )]

    def _sig(self, symbol: str, ts: datetime, side: Side, *,
             note: str, urgency: str = "normal") -> Signal:
        return Signal(
            symbol=symbol, side=side, confidence=1.0,
            urgency=urgency,  # type: ignore[arg-type]
            strategy=self.name, timestamp=ts, note=note,
        )

    def on_tick(self, tick: Tick) -> list[Signal]:
        return self._exit_check(tick)

    def open_positions(self) -> dict[str, _Pos]:
        return dict(self._open)


class DoubleBottomStrategy(_PatternStrategyBase):
    name = "double_bottom"

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, DoubleBottomDetected):
            return []
        # Use neckline breakout price as the entry reference
        return self._enter(
            event.symbol,
            event.neckline_price,
            event.timestamp,
            note=f"double_bottom W: neckline={event.neckline_price} target={event.target_price}",
        )


class BoxBreakoutStrategy(_PatternStrategyBase):
    name = "box_breakout"

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, BoxBreakoutDetected):
            return []
        return self._enter(
            event.symbol,
            event.breakout_price,
            event.timestamp,
            note=f"box_breakout: high={event.box_high} vol*{event.volume_multiplier:.1f}",
        )


class InverseHeadShouldersStrategy(_PatternStrategyBase):
    name = "inverse_head_shoulders"

    def on_event(self, event: Event) -> list[Signal]:
        if not isinstance(event, HeadShouldersDetected):
            return []
        if event.pattern != "inverse_head_shoulders":
            return []  # bearish H&S — no BUY
        return self._enter(
            event.symbol,
            event.neckline_price,
            event.timestamp,
            note=f"inv_h_s: neckline={event.neckline_price} target={event.target_price}",
        )
