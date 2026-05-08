"""KisFillFeed — subscribes to KIS 체결 통보 (H0STCNI0 / H0STCNI9) and
forwards parsed fill events to a callback.

Mock vs live tr_id:
- mock: H0STCNI9
- live: H0STCNI0
- tr_key: HTS ID (settings.hts_id)

Frames are AES-256-CBC encrypted by KIS for this stream. The
underlying KisRealtimeFeed captures the AES key + IV from the
subscription ack and exposes ``decrypt_payload`` so this feed can
turn each encrypted body into plain pipe-delimited text before
parsing.

Field layout (subset we care about, per KIS docs):
    [0]  CUST_ID         고객 ID (HTS ID)
    [1]  ACNT_NO         계좌번호
    [2]  ODER_NO         주문번호
    [3]  OODER_NO        원주문번호
    [4]  SELN_BYOV_CLS   매도매수구분 (01=매도, 02=매수)
    [5]  RCTF_CLS        정정구분
    [6]  ODER_KIND       주문종류
    [8]  STCK_SHRN_ISCD  종목코드
    [9]  CNTG_QTY        체결수량
    [10] CNTG_UNPR       체결단가
    [11] STCK_CNTG_HOUR  주식체결시간 HHMMSS
    [12] RFUS_YN         거부여부 (0=정상, 1=거부)
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

from ks_ws.config import Settings, get_settings
from ks_ws.domain import Side
from ks_ws.kis.realtime import KisRealtimeFeed

log = logging.getLogger("ks_ws.sources.fill_feed")

_KST = timezone(timedelta(hours=9))

_TR_IDS: dict[str, str] = {
    "mock": "H0STCNI9",
    "live": "H0STCNI0",
}


class FillEvent:
    """Plain dataclass-like record. Not a Pydantic model — fills come from
    KIS already-parsed and we just forward."""

    __slots__ = ("order_id", "price", "quantity", "side", "symbol", "timestamp")

    def __init__(
        self,
        *,
        order_id: str,
        symbol: str,
        side: Side,
        quantity: int,
        price: int,
        timestamp: datetime,
    ) -> None:
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.price = price
        self.timestamp = timestamp

    def __repr__(self) -> str:
        return (
            f"FillEvent(order_id={self.order_id}, symbol={self.symbol}, "
            f"side={self.side}, quantity={self.quantity}, price={self.price})"
        )


def _parse_kis_time(hhmmss: str) -> datetime:
    h, m, s = int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
    today_kst = datetime.now(_KST).date()
    naive = datetime(today_kst.year, today_kst.month, today_kst.day, h, m, s, tzinfo=_KST)
    return naive.astimezone(UTC)


def parse_fill_record(record: list[str]) -> FillEvent | None:
    """Parse a single H0STCNI0 record into a FillEvent. None on malformed
    or rejection rows."""
    if len(record) < 13:
        return None
    if record[12] == "1":  # 거부
        return None
    try:
        order_id = record[2]
        side = Side.BUY if record[4] == "02" else Side.SELL
        symbol = record[8]
        quantity = int(record[9])
        price = int(record[10])
        timestamp = _parse_kis_time(record[11])
    except (ValueError, IndexError):
        return None
    if quantity <= 0:
        return None
    return FillEvent(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        timestamp=timestamp,
    )


class KisFillFeed:
    """Subscribes to H0STCNI0 / H0STCNI9 on a (typically shared) feed,
    decrypts each frame, parses the record, and forwards FillEvents to
    a callback.

    The feed is provided by the caller — typically the same KisRealtimeFeed
    the market-data Hub already runs, so we share one WS connection. The
    callback is usually ``LiveExecutor.apply_fill_event`` (with side, qty,
    price unpacked) or any function accepting a FillEvent.
    """

    def __init__(
        self,
        feed: KisRealtimeFeed,
        callback: Callable[[FillEvent], None],
        settings: Settings | None = None,
    ) -> None:
        self._feed = feed
        self._callback = callback
        self._settings = settings or get_settings()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.received = 0
        self.parsed = 0
        self.errors = 0

    @property
    def tr_id(self) -> str:
        return _TR_IDS[self._settings.env]

    async def subscribe(self) -> None:
        """Send the H0STCNI0 / H0STCNI9 register message via the underlying
        feed. Caller is responsible for opening the feed first."""
        if not self._settings.hts_id:
            raise RuntimeError("KIS_HTS_ID is not configured — required for fill notifications")
        await self._feed.subscribe(self.tr_id, self._settings.hts_id)

    async def unsubscribe(self) -> None:
        if self._settings.hts_id:
            with contextlib.suppress(Exception):
                await self._feed.unsubscribe(self.tr_id, self._settings.hts_id)

    def handle_frame(self, raw: str) -> None:
        """Parse a single frame from the underlying feed and dispatch any
        contained FillEvents. Callers can drive this from their own read
        loop, or use ``run()`` for a self-contained task."""
        self.received += 1
        # Encrypted payloads still pass through KisRealtimeFeed.parse_frame —
        # decode flag tells us whether to decrypt the payload before parsing.
        tr_id, enc, records = KisRealtimeFeed.parse_frame(raw)
        if tr_id != self.tr_id:
            return
        if enc == "1":
            try:
                # records[0] is a list of fields; for encrypted frames the
                # whole payload is one base64 blob we need to decrypt and
                # re-split. We rebuild the raw payload from records[0].
                ciphertext = "^".join(records[0]) if records else ""
                plain = self._feed.decrypt_payload(tr_id, ciphertext)
                _, _, decrypted_records = KisRealtimeFeed.parse_frame(
                    f"0|{tr_id}|{len(records)}|{plain}"
                )
                records = decrypted_records
            except Exception as e:
                self.errors += 1
                log.warning("fill frame decrypt failed: %s", e)
                return
        for rec in records:
            event = parse_fill_record(rec)
            if event is None:
                continue
            self.parsed += 1
            try:
                self._callback(event)
            except Exception:
                log.exception("fill callback raised on %s", event.order_id)

    async def run(self) -> None:
        """Self-contained read loop — iterate the shared feed and dispatch
        fill frames. Other consumers of the same feed must use a different
        approach (e.g. teeing) since async iterators don't fan out."""
        async for raw in self._feed:
            self.handle_frame(raw)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
