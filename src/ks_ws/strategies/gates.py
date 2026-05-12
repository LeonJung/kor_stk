"""Strategy wrappers — TimeWindowGate / RegimeGate.

매매 strategy 를 시간대 / 시장 regime 으로 감싸 conditional dispatch 한다.
원본 strategy 의 ``name`` 그대로 노출 → Allocator weight 와 ledger sources 모두
변경 없이 동작.

Wrap pattern (decorator 형식)::

    raw = PairFollowStrategy(...)
    gated = TimeWindowGate(
        raw,
        windows=[(time(9, 0), time(9, 50))],  # KST
    )
    regime = RegimeGate(gated, allowed={"sideways", "downtrend"}, regime_provider=fn)

각 wrapper 는 활성 조건 false 일 때 빈 list 를 반환해 Allocator/Risk 를 바이패스.
시간 비교는 ``Asia/Seoul`` 로컬 시각 기준 (한국 장 시간이 명확).
"""

from collections.abc import Callable
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ks_ws.domain import Bar, OrderBook, Signal, Tick
from ks_ws.events import Event
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")


class TimeWindowGate(Strategy):
    """Wrap a strategy so on_X methods only run inside any of the given KST
    time windows. Outside windows, returns empty list.

    Windows are list of (start, end) tuples in KST. half-open [start, end).
    Useful for enforcing technical_strategy.md 시간대 가이드 (09:00-09:50 핫존,
    09:03-09:25 OpeningMomentum, 13:30~ 종가베팅) without hardcoding inside
    strategies.
    """

    def __init__(
        self,
        inner: Strategy,
        *,
        windows: list[tuple[time, time]],
        timezone: str = "Asia/Seoul",
    ) -> None:
        if not windows:
            raise ValueError("windows must not be empty")
        for start, end in windows:
            if not isinstance(start, time) or not isinstance(end, time):
                raise TypeError("window endpoints must be datetime.time")
            if start >= end:
                raise ValueError(f"window start ({start}) must be < end ({end})")
        self.inner = inner
        self.name = inner.name
        self.windows = windows
        self.tz = ZoneInfo(timezone)

    def is_active(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        local = ts.astimezone(self.tz).time()
        return any(start <= local < end for start, end in self.windows)

    def on_bar(self, bar: Bar) -> list[Signal]:
        return self.inner.on_bar(bar) if self.is_active(bar.timestamp) else []

    def on_tick(self, tick: Tick) -> list[Signal]:
        return self.inner.on_tick(tick) if self.is_active(tick.timestamp) else []

    def on_orderbook(self, orderbook: OrderBook) -> list[Signal]:
        return (
            self.inner.on_orderbook(orderbook) if self.is_active(orderbook.timestamp) else []
        )

    def on_event(self, event: Event) -> list[Signal]:
        return self.inner.on_event(event) if self.is_active(event.timestamp) else []


class EntryWindowGate(Strategy):
    """Like TimeWindowGate, but only BUY signals are gated by time. SELL
    signals pass through unconditionally so existing positions can still be
    closed (TP/SL/timeout/force-close) outside the entry window.

    Use this for any strategy that opens long positions inside a defined
    entry window but may need to close them outside that window. Examples:
    - LiveBreakoutStrategy: entry 09:00-14:30, exits via TP/SL/max_hold any time
    - ClosingBetStrategy: entry 13:30-15:25, exit next-day 09:00-15:25
    """

    def __init__(
        self,
        inner: Strategy,
        *,
        windows: list[tuple[time, time]],
        timezone: str = "Asia/Seoul",
    ) -> None:
        from ks_ws.domain import Side  # local import to avoid cycle
        if not windows:
            raise ValueError("windows must not be empty")
        for start, end in windows:
            if not isinstance(start, time) or not isinstance(end, time):
                raise TypeError("window endpoints must be datetime.time")
            if start >= end:
                raise ValueError(f"window start ({start}) must be < end ({end})")
        self.inner = inner
        self.name = inner.name
        self.windows = windows
        self.tz = ZoneInfo(timezone)
        self._buy_side = Side.BUY

    def is_entry_active(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        local = ts.astimezone(self.tz).time()
        return any(start <= local < end for start, end in self.windows)

    def _filter(self, sigs: list[Signal], ts: datetime) -> list[Signal]:
        if self.is_entry_active(ts):
            return sigs
        return [s for s in sigs if s.side != self._buy_side]

    def on_bar(self, bar: Bar) -> list[Signal]:
        return self._filter(self.inner.on_bar(bar), bar.timestamp)

    def on_tick(self, tick: Tick) -> list[Signal]:
        return self._filter(self.inner.on_tick(tick), tick.timestamp)

    def on_orderbook(self, orderbook: OrderBook) -> list[Signal]:
        return self._filter(self.inner.on_orderbook(orderbook), orderbook.timestamp)

    def on_event(self, event: Event) -> list[Signal]:
        return self._filter(self.inner.on_event(event), event.timestamp)


RegimeProvider = Callable[[], str]


class RegimeGate(Strategy):
    """Wrap a strategy so on_X methods only run when current market regime is
    in ``allowed``. Regime is read from ``regime_provider()`` each call —
    cheap if backed by a cached detector.

    Regime values are arbitrary strings: ``"strong_uptrend"`` / ``"uptrend"``
    / ``"sideways"`` / ``"downtrend"`` (project convention). When the
    provider returns ``"unknown"`` the wrapper treats it as not-allowed
    (fail closed).
    """

    def __init__(
        self,
        inner: Strategy,
        *,
        allowed: set[str],
        regime_provider: RegimeProvider,
    ) -> None:
        if not allowed:
            raise ValueError("allowed regimes must not be empty")
        self.inner = inner
        self.name = inner.name
        self.allowed = set(allowed)
        self.regime_provider = regime_provider

    def is_active(self) -> bool:
        return self.regime_provider() in self.allowed

    def on_bar(self, bar: Bar) -> list[Signal]:
        return self.inner.on_bar(bar) if self.is_active() else []

    def on_tick(self, tick: Tick) -> list[Signal]:
        return self.inner.on_tick(tick) if self.is_active() else []

    def on_orderbook(self, orderbook: OrderBook) -> list[Signal]:
        return self.inner.on_orderbook(orderbook) if self.is_active() else []

    def on_event(self, event: Event) -> list[Signal]:
        return self.inner.on_event(event) if self.is_active() else []
