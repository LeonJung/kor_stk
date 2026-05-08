"""KisMarketDataHub — wires KIS realtime WS + REST polling into the
MarketDataHub abstraction.

Hot tier (WS):
- Subscribes to H0STCNT0 (실시간 체결가) for every Tier.HOT symbol →
  publishes Tick to the bus.
- Subscribes to H0STASP0 (실시간 주식호가) for every Tier.HOT symbol →
  publishes OrderBook (10-deep) to the bus.

Warm tier (REST polling):
- For every Tier.WARM symbol, the hub polls fetch_current_price on a
  configurable cadence and publishes a synthesized Tick (price + 0
  volume — the snapshot is point-in-time, not a trade) plus the actual
  CurrentPrice payload. The REST rate limiter throttles automatically.

Cold tier (EOD batch):
- On start(), if a BarStore is provided, the hub fetches the last
  ``cold_lookback_days`` daily bars for every Tier.COLD symbol via
  fetch_daily_bars and writes them to BarStore. One-shot at startup;
  scheduled rotation belongs to a future job runner.

Frame parsers live as module-level functions so tests can drive them
without a real WS connection.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta, timezone

from ks_ws.bus import EventBus
from ks_ws.config import Settings, get_settings
from ks_ws.domain import OrderBook, OrderBookLevel, Tick
from ks_ws.kis.realtime import KisRealtimeFeed
from ks_ws.market.hub import MarketDataHub, Tier
from ks_ws.market.kis_rest import fetch_current_price, fetch_daily_bars
from ks_ws.storage.bars import BarStore

log = logging.getLogger("ks_ws.market.kis_hub")

_KST = timezone(timedelta(hours=9))
_TR_ID_TRADE = "H0STCNT0"  # 실시간 주식 체결가
_TR_ID_ORDERBOOK = "H0STASP0"  # 실시간 주식 호가


def _parse_kis_time(hhmmss: str) -> datetime:
    """Convert a KIS HHMMSS string (KST) into a tz-aware UTC datetime
    using today's KST date."""
    h, m, s = int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
    today_kst = datetime.now(_KST).date()
    naive = datetime(today_kst.year, today_kst.month, today_kst.day, h, m, s, tzinfo=_KST)
    return naive.astimezone(UTC)


def parse_trade_record(record: list[str]) -> Tick | None:
    """Parse a single H0STCNT0 record into a Tick.

    Field layout (subset we use):
        [0]  MKSC_SHRN_ISCD   종목코드
        [1]  STCK_CNTG_HOUR   체결시각 HHMMSS
        [2]  STCK_PRPR        체결가
        [12] CNTG_VOL         체결량
    """
    if len(record) < 13:
        return None
    try:
        symbol = record[0]
        ts = _parse_kis_time(record[1])
        price = int(record[2])
        volume = int(record[12])
    except (ValueError, IndexError):
        return None
    return Tick(symbol=symbol, timestamp=ts, price=price, volume=volume)


def parse_orderbook_record(record: list[str]) -> OrderBook | None:
    """Parse a single H0STASP0 record into a 10-deep OrderBook.

    Field layout (per KIS docs, the columns we use):
        [0]      MKSC_SHRN_ISCD   종목코드
        [1]      BSOP_HOUR        영업시간 HHMMSS
        [3..12]  ASKP1..ASKP10    매도호가 1~10
        [13..22] BIDP1..BIDP10    매수호가 1~10
        [23..32] ASKP_RSQN1..10   매도호가 잔량 1~10
        [33..42] BIDP_RSQN1..10   매수호가 잔량 1~10
    """
    if len(record) < 43:
        return None
    try:
        symbol = record[0]
        ts = _parse_kis_time(record[1])
        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []
        for i in range(10):
            ap = int(record[3 + i] or 0)
            av = int(record[23 + i] or 0)
            bp = int(record[13 + i] or 0)
            bv = int(record[33 + i] or 0)
            if ap > 0:
                asks.append(OrderBookLevel(price=ap, volume=av))
            if bp > 0:
                bids.append(OrderBookLevel(price=bp, volume=bv))
    except (ValueError, IndexError):
        return None
    return OrderBook(
        symbol=symbol,
        timestamp=ts,
        bids=tuple(bids),
        asks=tuple(asks),
    )


class KisMarketDataHub(MarketDataHub):
    def __init__(
        self,
        bus: EventBus,
        settings: Settings | None = None,
        *,
        subscribe_orderbook: bool = True,
        warm_poll_interval_sec: float = 30.0,
        bar_store: BarStore | None = None,
        cold_lookback_days: int = 90,
    ) -> None:
        super().__init__(bus)
        self._settings = settings or get_settings()
        self._subscribe_orderbook = subscribe_orderbook
        self.warm_poll_interval_sec = warm_poll_interval_sec
        self._bar_store = bar_store
        self.cold_lookback_days = cold_lookback_days
        self._feed: KisRealtimeFeed | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._warm_task: asyncio.Task[None] | None = None
        self._cold_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._feed is not None:
            return
        self._feed = KisRealtimeFeed(self._settings)
        await self._feed.__aenter__()
        for symbol in self.symbols_by_tier(Tier.HOT):
            await self._feed.subscribe(_TR_ID_TRADE, symbol)
            if self._subscribe_orderbook:
                await self._feed.subscribe(_TR_ID_ORDERBOOK, symbol)
        self._reader_task = asyncio.create_task(self._read_frames())

        if self.symbols_by_tier(Tier.WARM):
            self._warm_task = asyncio.create_task(self._warm_poll_loop())

        cold = self.symbols_by_tier(Tier.COLD)
        if cold:
            if self._bar_store is None:
                log.info(
                    "COLD tier symbols (%d) recorded but no BarStore — skipping batch",
                    len(cold),
                )
            else:
                # One-shot batch at startup. Run in a thread so the WS read
                # loop isn't blocked on the (rate-limited) REST round-trips.
                self._cold_task = asyncio.create_task(self._cold_batch_load())

    async def stop(self) -> None:
        if self._feed is None:
            return
        for symbol in self.symbols_by_tier(Tier.HOT):
            try:
                await self._feed.unsubscribe(_TR_ID_TRADE, symbol)
                if self._subscribe_orderbook:
                    await self._feed.unsubscribe(_TR_ID_ORDERBOOK, symbol)
            except Exception as e:
                log.warning("unsubscribe %s failed: %s", symbol, e)
        if self._warm_task is not None:
            self._warm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._warm_task
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        await self._feed.__aexit__(None, None, None)
        self._feed = None
        self._reader_task = None
        self._warm_task = None

    async def _read_frames(self) -> None:
        assert self._feed is not None
        try:
            async for raw in self._feed:
                self._handle_frame(raw)
        except asyncio.CancelledError:
            pass

    async def _cold_batch_load(self) -> None:
        """Fetch the most recent ``cold_lookback_days`` daily bars for every
        COLD symbol and persist them to BarStore. One-shot at startup.
        """
        assert self._bar_store is not None
        end = datetime.now(UTC).date()
        start = end - timedelta(days=self.cold_lookback_days)
        for symbol in self.symbols_by_tier(Tier.COLD):
            try:
                bars = await asyncio.to_thread(
                    fetch_daily_bars,
                    symbol,
                    start=start,
                    end=end,
                    settings=self._settings,
                )
            except Exception as e:
                log.warning("COLD batch failed for %s: %s", symbol, e)
                continue
            if not bars:
                continue
            try:
                await asyncio.to_thread(self._bar_store.write, bars)
            except Exception as e:
                log.warning("BarStore.write failed for %s: %s", symbol, e)
                continue
            log.info("COLD batch wrote %d bars for %s", len(bars), symbol)

    async def _warm_poll_loop(self) -> None:
        """Poll fetch_current_price for every WARM symbol on a cadence and
        publish a synthesized Tick (volume=0, price = current). The REST
        rate limiter handles per-call throttling automatically."""
        try:
            while True:
                for symbol in self.symbols_by_tier(Tier.WARM):
                    try:
                        snap = await asyncio.to_thread(
                            fetch_current_price, symbol, settings=self._settings
                        )
                    except Exception as e:
                        log.warning("WARM poll failed for %s: %s", symbol, e)
                        continue
                    self._bus.publish(snap)
                    self._bus.publish(
                        Tick(
                            symbol=snap.symbol,
                            timestamp=snap.timestamp,
                            price=snap.price,
                            volume=0,
                        )
                    )
                await asyncio.sleep(self.warm_poll_interval_sec)
        except asyncio.CancelledError:
            pass

    def _handle_frame(self, raw: str) -> None:
        assert self._feed is not None
        tr_id, _enc, records = self._feed.parse_frame(raw)
        if tr_id == _TR_ID_TRADE:
            for rec in records:
                tick = parse_trade_record(rec)
                if tick is not None:
                    self._bus.publish(tick)
        elif tr_id == _TR_ID_ORDERBOOK:
            for rec in records:
                ob = parse_orderbook_record(rec)
                if ob is not None:
                    self._bus.publish(ob)
