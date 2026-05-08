"""ProgramFlowSource — periodic poller that feeds the ProgramFlowDetector
with net program-trade buy figures per symbol.

The source is generic over a fetcher callable: ``fetcher(symbol) -> int``
returns net program buy in KRW. The default ``kis_program_flow_fetcher``
calls the KIS REST endpoint for 종목별 프로그램매매 추이; tests and
replays can swap in any callable returning an int.

Run modes:
- ``step()``: synchronous, one full pass over the symbol list. For tests
  and tight integration scripts.
- ``start()`` / ``stop()``: async loop on ``interval_sec`` cadence. For
  live operation.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from ks_ws.auth.token import get_token
from ks_ws.config import Settings, get_settings
from ks_ws.detectors.program_flow import ProgramFlowDetector
from ks_ws.kis.http import make_client

log = logging.getLogger("ks_ws.sources.program_flow")

_PROGRAM_FLOW_PATH = "/uapi/domestic-stock/v1/quotations/program-trade-by-stock"
_TR_ID_PROGRAM_FLOW = "FHPPG04650201"

FlowFetcher = Callable[[str], int]


def kis_program_flow_fetcher(symbol: str, settings: Settings | None = None) -> int:
    """Call KIS for the latest cumulative net program-buy KRW on a symbol.

    Returns 0 if the response is missing the expected field — strategies see
    that as no flow rather than an error. The exact field name and tr_id may
    drift across KIS spec versions; verify against the live API and adjust
    if the parser starts logging "missing field" warnings.
    """
    settings = settings or get_settings()
    token = get_token(settings)
    client = make_client(settings)
    try:
        resp = client.get(
            _PROGRAM_FLOW_PATH,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            },
            headers={
                "authorization": f"Bearer {token}",
                "tr_id": _TR_ID_PROGRAM_FLOW,
                "custtype": "P",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        client.close()

    if data.get("rt_cd") != "0":
        log.warning(
            "KIS program-trade rt_cd=%s msg=%s",
            data.get("rt_cd"),
            data.get("msg1"),
        )
        return 0

    out = data.get("output1") or data.get("output") or {}
    # KIS returns net buy as `whol_ntby_qty` (전체 순매수 수량) or similar
    # field names. Try the common candidates; document drift with a warning.
    for key in ("whol_smtm_ntby_qty", "ntby_qty", "ntby_amt", "prgm_ntby_qty"):
        if key in out:
            try:
                return int(out[key])
            except (ValueError, TypeError):
                continue
    log.warning("program-trade response missing net-flow field; got keys=%s", list(out)[:5])
    return 0


class ProgramFlowSource:
    def __init__(
        self,
        detector: ProgramFlowDetector,
        symbols: Iterable[str],
        *,
        fetcher: FlowFetcher | None = None,
        interval_sec: float = 30.0,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")
        self._detector = detector
        self._symbols = list(symbols)
        self._fetcher: FlowFetcher = fetcher or kis_program_flow_fetcher
        self.interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.poll_count = 0

    @property
    def running(self) -> bool:
        return self._running

    def step(self) -> int:
        """One full pass over the symbol list. Returns count of symbols polled."""
        polled = 0
        now = datetime.now(UTC)
        for symbol in self._symbols:
            try:
                net = self._fetcher(symbol)
            except Exception as e:
                log.warning("program-flow fetch failed for %s: %s", symbol, e)
                continue
            self._detector.feed(symbol, net, now)
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
