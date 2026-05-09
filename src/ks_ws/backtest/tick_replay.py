"""TickReplay — bar/tick/orderbook/event 시퀀스를 시간순 publish 하여 strategy
회로 전체를 검증하는 휴장일 친화 백테스트 driver.

기존 ``BacktestDriver`` (driver.py) 는 bar 만 처리하고 fill 모델 (next-bar
close + cost) 을 자체 구현. 본 ``TickReplayDriver`` 는 그 위에 다음을 추가:

- Tick / OrderBook / Event 도 시간순 mix → strategy 의 모든 hook 검증
- "다음 tick 시점 fill" 모델 (즉시 체결 to keep things simple, slippage opt)
- per-strategy PnL 자동 집계 (Ledger + aggregate_strategy_pnl)
- scenario yaml loader: tick/event 시퀀스를 yaml 로 정의 → CI 회귀 가능

특히 PairFollow / OpeningMomentum / DojiCloseBet 같은 분/초 단위 strategy 의
tick-level 회귀에 사용. BacktestDriver 와 직교 (둘 다 유효한 use case).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from heapq import heappop, heappush
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from ks_ws.bus import EventBus
from ks_ws.domain import Bar, OrderBook, OrderBookLevel, OrderIntent, Side, Tick
from ks_ws.events import Event, GapUp, LimitUpBroken, LimitUpReached, VolumeSpike
from ks_ws.orders import SubmittedOrder
from ks_ws.runtime import Runtime
from ks_ws.storage.ledger import Ledger
from ks_ws.storage.strategy_pnl import StrategyStats, aggregate_strategy_pnl
from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy

# -- Result --------------------------------------------------------------


@dataclass
class TickReplayResult:
    intents: list[OrderIntent]
    fills: list[tuple[OrderIntent, int]]  # (intent, fill_price)
    strategy_pnl: dict[str, StrategyStats]
    items_processed: int

    @property
    def total_intents(self) -> int:
        return len(self.intents)


# -- Driver --------------------------------------------------------------


class TickReplayDriver:
    def __init__(
        self,
        items: Iterable[Bar | Tick | OrderBook | Event],
        strategies: Iterable[Strategy],
        *,
        allocator: Allocator | None = None,
        fill_price_for: dict[str, int] | None = None,
        last_tick_fill: bool = True,
        ledger_path: Path | None = None,
    ) -> None:
        """``fill_price_for`` 는 symbol → 기본 fill 가격. ``last_tick_fill`` 가
        True 면 해당 symbol 의 가장 최근 Tick 가격으로 fill (가장 현실적).
        둘 다 없으면 fill 가격 0 (기록은 되지만 PnL 0)."""
        self.items = list(items)
        self.bus = EventBus()
        self.runtime = Runtime(self.bus, strategies, allocator or Allocator())
        self.runtime.setup()
        self.intents_sub = self.bus.subscribe(OrderIntent)
        self.fill_price_for = dict(fill_price_for or {})
        self.last_tick_fill = last_tick_fill
        self._last_price: dict[str, int] = {}
        self._ledger_owner = ledger_path is None
        self._ledger_dir: TemporaryDirectory | None = None
        if ledger_path is None:
            self._ledger_dir = TemporaryDirectory()
            ledger_path = Path(self._ledger_dir.name) / "ledger.sqlite"
        self.ledger = Ledger(ledger_path)
        self._intents: list[OrderIntent] = []
        self._fills: list[tuple[OrderIntent, int]] = []

    def __enter__(self) -> "TickReplayDriver":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.ledger.close()
        if self._ledger_dir is not None:
            self._ledger_dir.cleanup()

    # -- Replay --------------------------------------------------------

    def run(self) -> TickReplayResult:
        from collections import defaultdict

        sorted_items = sorted(self.items, key=_item_key)
        pending: dict[str, list[OrderIntent]] = defaultdict(list)
        processed = 0
        for item in sorted_items:
            # 1) If this is a Tick, drain any pending intents for that symbol
            #    against this tick price (intents from prior events get filled
            #    on the next observed price for their symbol).
            if isinstance(item, Tick):
                self._last_price[item.symbol] = item.price
                for intent in pending.pop(item.symbol, []):
                    self._record_fill(intent, fill_price=item.price)

            # 2) Publish + dispatch
            self.bus.publish(item)
            self.runtime.step()

            # 3) Collect new intents. Fill immediately if we know a price
            #    (last observed tick), otherwise pend until the next tick.
            while self.intents_sub.qsize() > 0:
                intent: OrderIntent = self.intents_sub.get_nowait()
                self._intents.append(intent)
                if isinstance(item, Tick) and item.symbol == intent.symbol:
                    self._record_fill(intent, fill_price=item.price)
                elif self.last_tick_fill and intent.symbol in self._last_price:
                    self._record_fill(intent, fill_price=self._last_price[intent.symbol])
                else:
                    pending[intent.symbol].append(intent)
            processed += 1

        # 4) Anything still pending at end → fill at last known price (or
        #    static default), or 0 if neither is available.
        for symbol, intents in pending.items():
            fallback = self._last_price.get(symbol, self.fill_price_for.get(symbol, 0))
            for intent in intents:
                self._record_fill(intent, fill_price=fallback)

        return TickReplayResult(
            intents=list(self._intents),
            fills=list(self._fills),
            strategy_pnl=aggregate_strategy_pnl(self.ledger),
            items_processed=processed,
        )

    def _record_fill(self, intent: OrderIntent, *, fill_price: int) -> None:
        order_id = f"o-{len(self._fills) + 1}"
        submitted = SubmittedOrder(
            order_id=order_id, intent=intent, submitted_at=intent.timestamp
        )
        self.ledger.record_order(submitted)
        self.ledger.apply_fill(
            order_id=order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=fill_price,
        )
        self._fills.append((intent, fill_price))


def _item_key(item: Bar | Tick | OrderBook | Event) -> tuple[datetime, int]:
    """Stable sort key — by timestamp, then by item-type rank so that within
    the same timestamp Bars are processed before Ticks before OrderBooks
    before Events. Mostly cosmetic; prevents test flakiness."""
    rank = {Bar: 0, Tick: 1, OrderBook: 2}.get(type(item), 3)
    return (item.timestamp, rank)


# -- Scenario YAML loader -----------------------------------------------


_EVENT_REGISTRY: dict[str, type[Event]] = {
    "GapUp": GapUp,
    "VolumeSpike": VolumeSpike,
    "LimitUpReached": LimitUpReached,
    "LimitUpBroken": LimitUpBroken,
}


def load_scenario(path: str | Path) -> list[Bar | Tick | OrderBook | Event]:
    """Load a YAML scenario file describing a chronological mix of items.

    Schema::

        items:
          - tick: {symbol: A005930, ts: "2026-05-11T09:00:00+09:00",
                   price: 70000, volume: 10}
          - event: {type: LimitUpReached, symbol: A005930,
                    ts: "2026-05-11T09:10:00+09:00",
                    limit_up_price: 13000, prev_close: 10000}
          - bar: {symbol: A005930, ts: "2026-05-11T09:00:00+09:00",
                  timeframe: "1m", open: 70000, high: 70100, low: 69900,
                  close: 70050, volume: 1000, value: 70_050_000}
          - orderbook: {symbol: A005930, ts: "...",
                        bids: [[70000, 100]], asks: [[70050, 100]]}
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [_decode_item(spec) for spec in data.get("items", [])]


def _decode_item(spec: dict) -> Bar | Tick | OrderBook | Event:
    if "tick" in spec:
        s = spec["tick"]
        return Tick(
            symbol=s["symbol"],
            timestamp=_parse_ts(s["ts"]),
            price=int(s["price"]),
            volume=int(s.get("volume", 0)),
            aggressor=Side(s["aggressor"]) if s.get("aggressor") else None,
        )
    if "bar" in spec:
        s = spec["bar"]
        return Bar(
            symbol=s["symbol"],
            timestamp=_parse_ts(s["ts"]),
            timeframe=s["timeframe"],
            open=int(s["open"]),
            high=int(s["high"]),
            low=int(s["low"]),
            close=int(s["close"]),
            volume=int(s["volume"]),
            value=int(s["value"]),
        )
    if "orderbook" in spec:
        s = spec["orderbook"]
        return OrderBook(
            symbol=s["symbol"],
            timestamp=_parse_ts(s["ts"]),
            bids=tuple(OrderBookLevel(price=int(p), volume=int(v)) for p, v in s["bids"]),
            asks=tuple(OrderBookLevel(price=int(p), volume=int(v)) for p, v in s["asks"]),
        )
    if "event" in spec:
        s = dict(spec["event"])
        cls_name = s.pop("type")
        ts = _parse_ts(s.pop("ts"))
        symbol = s.pop("symbol")
        cls = _EVENT_REGISTRY.get(cls_name)
        if cls is None:
            raise ValueError(f"unknown event type {cls_name!r}")
        return cls(symbol=symbol, timestamp=ts, **s)
    raise ValueError(f"scenario item must have one of tick/bar/orderbook/event: {spec!r}")


def _parse_ts(raw: str | datetime) -> datetime:
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(raw)


# -- Synthetic tick generator ------------------------------------------


def synthetic_ticks_from_bar(bar: Bar, *, n_ticks: int = 10) -> list[Tick]:
    """Approximate intra-bar tick sequence: open → high → low → close, evenly
    spaced timestamps within the bar. ``n_ticks`` divides the bar duration
    evenly. Useful for back-filling tick replay from sparser bar data."""
    if n_ticks < 4:
        raise ValueError("n_ticks must be >= 4 for OHLC representation")
    # Equal-time waypoints between open / high / low / close
    waypoints = [bar.open, bar.high, bar.low, bar.close]
    expanded: list[int] = []
    per_segment = max(1, n_ticks // (len(waypoints) - 1))
    for i in range(len(waypoints) - 1):
        a, b = waypoints[i], waypoints[i + 1]
        for k in range(per_segment):
            t = k / per_segment
            expanded.append(int(a + (b - a) * t))
    expanded = expanded[: n_ticks - 1]
    expanded.append(bar.close)
    # Distribute volume equally
    per_tick_vol = max(1, bar.volume // len(expanded))
    # Spread timestamps over 1 minute by default (caller can override later)
    from datetime import timedelta as _td

    span = _td(minutes=1) if bar.timeframe == "1m" else _td(minutes=5)
    step = span / max(1, len(expanded))
    out: list[Tick] = []
    for i, price in enumerate(expanded):
        out.append(
            Tick(
                symbol=bar.symbol,
                timestamp=bar.timestamp + step * i,
                price=price,
                volume=per_tick_vol,
            )
        )
    return out


# -- Helper for combined chronological merge ---------------------------


def merge_chronological(
    *streams: Iterable[Bar | Tick | OrderBook | Event],
) -> list[Bar | Tick | OrderBook | Event]:
    """Merge multiple already-sorted streams into a single chronological list.
    Stable across same timestamp using insertion order + heap counter."""
    counter = count()
    heap: list[tuple[datetime, int, Any]] = []
    for stream in streams:
        for item in stream:
            heappush(heap, (item.timestamp, next(counter), item))
    out: list[Bar | Tick | OrderBook | Event] = []
    while heap:
        _, _, item = heappop(heap)
        out.append(item)
    return out
