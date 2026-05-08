"""KisMarketDataHub — wires KIS realtime WS into MarketDataHub abstraction.

v1 covers the HOT tier only:
- Subscribes to H0STCNT0 (실시간 체결가) for every symbol assigned ``Tier.HOT``.
- Parses incoming pipe-delimited frames and publishes ``Tick`` records to
  the bus.
- Symbols assigned ``Tier.WARM`` or ``Tier.COLD`` are remembered but not
  automatically polled / batched yet — that's a follow-up where REST
  pollers and EOD batchers slot in cleanly without changing this surface.

Extending to 호가 (H0STASP0 → OrderBook) is straightforward — add a tr_id
case in ``_handle_records`` plus a parser. Left for a separate change so
this commit stays focused.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta, timezone

from ks_ws.bus import EventBus
from ks_ws.config import Settings, get_settings
from ks_ws.domain import Tick
from ks_ws.kis.realtime import KisRealtimeFeed
from ks_ws.market.hub import MarketDataHub, Tier

log = logging.getLogger("ks_ws.market.kis_hub")

_KST = timezone(timedelta(hours=9))
_TR_ID_TRADE = "H0STCNT0"  # 실시간 주식 체결가


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


class KisMarketDataHub(MarketDataHub):
    def __init__(self, bus: EventBus, settings: Settings | None = None) -> None:
        super().__init__(bus)
        self._settings = settings or get_settings()
        self._feed: KisRealtimeFeed | None = None
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._feed is not None:
            return
        self._feed = KisRealtimeFeed(self._settings)
        await self._feed.__aenter__()
        for symbol in self.symbols_by_tier(Tier.HOT):
            await self._feed.subscribe(_TR_ID_TRADE, symbol)
        self._reader_task = asyncio.create_task(self._read_frames())

        warm = self.symbols_by_tier(Tier.WARM)
        cold = self.symbols_by_tier(Tier.COLD)
        if warm:
            log.info("WARM tier symbols (%d) recorded but REST polling not yet wired", len(warm))
        if cold:
            log.info("COLD tier symbols (%d) recorded but EOD batch not yet wired", len(cold))

    async def stop(self) -> None:
        if self._feed is None:
            return
        for symbol in self.symbols_by_tier(Tier.HOT):
            try:
                await self._feed.unsubscribe(_TR_ID_TRADE, symbol)
            except Exception as e:
                log.warning("unsubscribe %s failed: %s", symbol, e)
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        await self._feed.__aexit__(None, None, None)
        self._feed = None
        self._reader_task = None

    async def _read_frames(self) -> None:
        assert self._feed is not None
        try:
            async for raw in self._feed:
                self._handle_frame(raw)
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
