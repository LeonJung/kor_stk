"""ForeignNetBuySource — periodic poller that fetches net foreign-investor
buy/sell KRW per symbol and publishes ForeignNetBuy events on a bus.

V1: KIS REST endpoint for 외국인·기관 매매동향 (외국인 일별 순매수). Tests
swap in any callable returning ``int`` (positive = net foreign buy).

Run modes:
- ``step()``: synchronous, one full pass over symbols. Tests + tight scripts.
- ``start()`` / ``stop()``: async loop on ``interval_sec``. Live operation.

Output: publishes ``ForeignNetBuy`` events (with delta_krw + window_seconds)
on the provided EventBus. Strategies (J InstFgnFlow) consume them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta, timezone

from ks_ws.auth.token import get_token
from ks_ws.bus import EventBus
from ks_ws.config import Settings, get_settings
from ks_ws.events import ForeignNetBuy
from ks_ws.kis.http import make_client

log = logging.getLogger("ks_ws.sources.foreign_flow")

# Subject to change; the KIS spec for 외국인·기관 매매동향 has multiple
# endpoints depending on individual-stock vs market-wide. Adjust if a 404
# or wrong-shape response is observed.
_FOREIGN_FLOW_PATH = "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
_TR_ID_FOREIGN_FLOW = "FHPTJ04160001"

# KIS investor-trade-by-stock-daily 응답의 frgn_ntby_tr_pbmn 등 _pbmn 필드는
# **백만원 단위** (acml_tr_pbmn 의 원 단위와 다름). 우리 점수 입력은 원 단위 기준.
_PBMN_UNIT_TO_KRW = 1_000_000

ForeignFetcher = Callable[[str], int]


_KST = timezone(timedelta(hours=9))


def kis_foreign_flow_fetcher(
    symbol: str,
    settings: Settings | None = None,
    *,
    date_yyyymmdd: str | None = None,
) -> int:
    """Call KIS investor-trade-by-stock-daily for a single symbol/date,
    return net foreign buy in **KRW** (positive = net buying).

    Response field ``frgn_ntby_tr_pbmn`` (외국인 순매수 거래대금) is in unit
    of 백만원; we normalize back to KRW. ``output2`` carries the daily row.

    KIS mock has time-limit (rt_cd=2 "TIME LIMIT") outside 정규장 — caller
    typically passes ``date_yyyymmdd`` = 마지막 영업일 if running outside
    market hours. 0 returned on any error / missing field.
    """
    settings = settings or get_settings()
    token = get_token(settings)
    client = make_client(settings)
    try:
        date_str = date_yyyymmdd or datetime.now(_KST).strftime("%Y%m%d")
        resp = client.get(
            _FOREIGN_FLOW_PATH,
            headers={
                "authorization": f"Bearer {token}",
                "appkey": settings.app_key,
                "appsecret": settings.app_secret,
                "tr_id": _TR_ID_FOREIGN_FLOW,
                "tr_cont": "",
            },
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": date_str,
                "FID_ORG_ADJ_PRC": "",
                "FID_ETC_CLS_CODE": "",
            },
        )
        data = resp.json()
    finally:
        client.close()

    if data.get("rt_cd") != "0":
        log.warning(
            "KIS foreign-flow rt_cd=%s msg=%s",
            data.get("rt_cd"),
            data.get("msg1"),
        )
        return 0

    # investor-trade-by-stock-daily: daily 행은 output2, output1 은 누적/요약.
    rows = data.get("output2") or data.get("output1") or data.get("output") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        log.warning("foreign-flow response had no rows; data keys=%s", list(data)[:5])
        return 0
    head = rows[0]
    # 우선순위: 거래대금 (백만원, unit normalize) > 주식수 (단위 그대로, score 의미는 다름)
    for key, unit in (
        ("frgn_ntby_tr_pbmn", _PBMN_UNIT_TO_KRW),
        ("frgn_reg_ntby_pbmn", _PBMN_UNIT_TO_KRW),
        ("frgn_ntby_qty", 1),  # fallback: 주식수 → 단위 의미 다름 (caller 인지)
    ):
        if key in head:
            try:
                return int(head[key]) * unit
            except (ValueError, TypeError):
                continue
    log.warning("foreign-flow response missing net-buy field; got keys=%s", list(head)[:8])
    return 0


class ForeignNetBuySource:
    def __init__(
        self,
        bus: EventBus,
        symbols: Iterable[str],
        *,
        fetcher: ForeignFetcher | None = None,
        interval_sec: float = 60.0,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")
        self._bus = bus
        self._symbols = list(symbols)
        self._fetcher: ForeignFetcher = fetcher or kis_foreign_flow_fetcher
        self.interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.poll_count = 0

    @property
    def running(self) -> bool:
        return self._running

    def step(self) -> int:
        polled = 0
        now = datetime.now(UTC)
        window = int(self.interval_sec)
        for symbol in self._symbols:
            try:
                net = self._fetcher(symbol)
            except Exception as e:
                log.warning("foreign-flow fetch failed for %s: %s", symbol, e)
                continue
            self._bus.publish(
                ForeignNetBuy(
                    symbol=symbol, timestamp=now, delta_krw=net, window_seconds=window
                )
            )
            polled += 1
        self.poll_count += 1
        return polled

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.to_thread(self.step)
                await asyncio.sleep(self.interval_sec)
        except asyncio.CancelledError:
            pass
