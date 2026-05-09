"""PairFollowStrategy — 짝꿍 매매.

만쥬 (원정연) 책의 주력 매매. 대장주가 상한가 진입하면 후속주 매수, 4가지
시나리오로 청산:

1. **익절** — 후속주 가격 ≥ entry × (1 + take_profit_pct) → SELL
2. **손절** — 후속주 가격 ≤ entry × (1 - stop_loss_pct) → SELL  (-1~2%)
3. **횡보 청산** — entry 후 ``flat_timeout_seconds`` 동안 ±0.5% 안에서 횡보 → SELL
   (5분 룰의 정확한 의미: 추세 follow-through 약함)
4. **시간 청산** — 보유 ``hold_timeout_seconds`` 초과 → SELL (default 300s = 5분)
5. **매수 기준 훼손** — leader 가 LimitUpBroken 이벤트 발행 → SELL

Entry 는 LimitUpReached event 받았을 때 매핑된 follower 에 BUY signal.
보유 시간 평균 1~5분, 익절 +2~3%, 손절 -1~2% (블로그 cross-check 정량 룰).

Pair 매핑은 ``pairs: dict[leader, follower]`` 로 주입. 동적 detection 은
별도 (PreMarketWatchlistBuilder 영역).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from ks_ws.domain import Side, Signal, Tick
from ks_ws.events import Event, LimitUpBroken, LimitUpReached
from ks_ws.strategies.base import Strategy


@dataclass
class _Position:
    follower: str
    leader: str
    entry_price: int
    entry_time: datetime
    last_price: int
    flat_high: int  # rolling max price since entry
    flat_low: int  # rolling min price since entry
    flat_started: datetime  # last time price moved out of ±flat_band


class PairFollowStrategy(Strategy):
    name = "pair_follow"

    def __init__(
        self,
        *,
        pairs: dict[str, str],
        take_profit_pct: float = 2.5,
        stop_loss_pct: float = 1.5,
        hold_timeout_seconds: int = 300,
        flat_timeout_seconds: int = 60,
        flat_band_pct: float = 0.5,
        confidence: float = 0.7,
    ) -> None:
        if not pairs:
            raise ValueError("pairs must not be empty")
        if take_profit_pct <= 0 or stop_loss_pct <= 0:
            raise ValueError("take_profit_pct and stop_loss_pct must be positive")
        if hold_timeout_seconds < flat_timeout_seconds:
            raise ValueError("hold_timeout_seconds must be >= flat_timeout_seconds")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        self.pairs = dict(pairs)  # leader -> follower
        # reverse map for fast lookup on follower's price tick
        self._followers_to_leader = {v: k for k, v in self.pairs.items()}
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.hold_timeout = timedelta(seconds=hold_timeout_seconds)
        self.flat_timeout = timedelta(seconds=flat_timeout_seconds)
        self.flat_band_pct = flat_band_pct
        self.confidence = confidence
        self._open: dict[str, _Position] = {}  # follower symbol -> position

    # Event hooks ----------------------------------------------------------

    def on_event(self, event: Event) -> list[Signal]:
        if isinstance(event, LimitUpReached):
            return self._maybe_enter(event)
        if isinstance(event, LimitUpBroken):
            return self._exit_on_broken(event)
        return []

    def _maybe_enter(self, event: LimitUpReached) -> list[Signal]:
        follower = self.pairs.get(event.symbol)
        if follower is None or follower in self._open:
            return []
        # Defer the actual entry price snapshot to the first follower tick
        # — record a "pending" entry: we BUY now and the follower's first
        # subsequent tick price becomes our reference.
        self._open[follower] = _Position(
            follower=follower,
            leader=event.symbol,
            entry_price=0,  # filled on first tick
            entry_time=event.timestamp,
            last_price=0,
            flat_high=0,
            flat_low=0,
            flat_started=event.timestamp,
        )
        return [
            Signal(
                symbol=follower,
                side=Side.BUY,
                confidence=self.confidence,
                strategy=self.name,
                timestamp=event.timestamp,
                note=f"leader {event.symbol} reached limit-up @ {event.limit_up_price}",
            )
        ]

    def _exit_on_broken(self, event: LimitUpBroken) -> list[Signal]:
        # Find any open follower positions whose leader is this symbol
        outs: list[Signal] = []
        for follower, pos in list(self._open.items()):
            if pos.leader != event.symbol:
                continue
            outs.append(
                Signal(
                    symbol=follower,
                    side=Side.SELL,
                    confidence=1.0,
                    urgency="high",
                    strategy=self.name,
                    timestamp=event.timestamp,
                    note=f"leader {event.symbol} limit-up broken",
                )
            )
            del self._open[follower]
        return outs

    # Tick monitoring (exits) ---------------------------------------------

    def on_tick(self, tick: Tick) -> list[Signal]:
        pos = self._open.get(tick.symbol)
        if pos is None:
            return []
        # First tick after entry — capture the reference price
        if pos.entry_price == 0:
            pos.entry_price = tick.price
            pos.last_price = tick.price
            pos.flat_high = tick.price
            pos.flat_low = tick.price
            pos.flat_started = tick.timestamp
            return []

        pos.last_price = tick.price

        # Take-profit
        tp_price = pos.entry_price * (1 + self.take_profit_pct / 100)
        if tick.price >= tp_price:
            del self._open[tick.symbol]
            return [self._exit(tick, pos, note=f"take-profit @ {tick.price}")]

        # Stop-loss
        sl_price = pos.entry_price * (1 - self.stop_loss_pct / 100)
        if tick.price <= sl_price:
            del self._open[tick.symbol]
            return [self._exit(tick, pos, note=f"stop-loss @ {tick.price}", urgency="high")]

        # Hard hold timeout
        if tick.timestamp - pos.entry_time >= self.hold_timeout:
            del self._open[tick.symbol]
            return [self._exit(tick, pos, note="hold timeout")]

        # Flat-range detection — ±flat_band_pct around entry
        band = pos.entry_price * self.flat_band_pct / 100
        if abs(tick.price - pos.entry_price) > band:
            # price moved out of band — reset the flat window
            pos.flat_started = tick.timestamp
            pos.flat_high = tick.price
            pos.flat_low = tick.price
        else:
            pos.flat_high = max(pos.flat_high, tick.price)
            pos.flat_low = min(pos.flat_low, tick.price)
            if tick.timestamp - pos.flat_started >= self.flat_timeout:
                del self._open[tick.symbol]
                return [self._exit(tick, pos, note="flat timeout (no follow-through)")]

        return []

    def _exit(self, tick: Tick, pos: _Position, *, note: str, urgency: str = "normal") -> Signal:
        return Signal(
            symbol=pos.follower,
            side=Side.SELL,
            confidence=1.0,
            urgency=urgency,  # type: ignore[arg-type]
            strategy=self.name,
            timestamp=tick.timestamp,
            note=note,
        )

    # Inspection -----------------------------------------------------------

    def open_positions(self) -> dict[str, _Position]:
        return dict(self._open)
