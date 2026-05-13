"""RealtimeInvestorFlow — KIS inquire-investor-time-by-market 분 단위 polling.

fundamental_strategy.md §B (수급 history) + §I 의 실시간 보강. 시장 단위 (KOSPI/
KOSDAQ) 분 단위 외인/기관/개인/증권/투자신탁/사모펀드/은행 순매수 거래대금 누적.

종목 단위 실시간은 KIS docs 부족 — 시장 단위가 우선. 종목 단위 외인 매매 추세는
investor-trade-by-stock-daily (일별, 어제 데이터까지) 로 보강.

API:
- KIS REST `/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market`
  tr_id `FHPTJ04030000`, FID_INPUT_ISCD=0001 (KOSPI) / 1001 (KOSDAQ)
- 응답: output[] = 분 단위 누적 row.  frgn_ntby_tr_pbmn (외인 순매수 거래대금
  KRW *백만), orgn_ntby_tr_pbmn (기관), prsn_ntby_tr_pbmn (개인).
- 단위 = 백만원 (investor-trade-by-stock-daily 와 동일).

용도:
- 시장 전체 외인 누적 매수 ↑ → fundamental Pattern 7 (regime activation) signal.
- 외인 + 기관 매수 동시 → strong risk-on signal.

설계:
- fetch_market_investor_flow(market='KOSPI') → MarketInvestorFlow (latest snapshot)
- RealtimeInvestorFlowSource — async poll (default 60s) → emit MarketFlow event
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ks_ws.auth.token import get_token
from ks_ws.bus import EventBus
from ks_ws.config import Settings, get_settings
from ks_ws.events import Event
from ks_ws.kis.http import make_client

log = logging.getLogger("ks_ws.sources.realtime_investor_flow")

_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market"
_TR_ID = "FHPTJ04030000"
_PBMN_UNIT_TO_KRW = 1_000_000  # 백만원 → 원

_MARKET_CODE = {"KOSPI": "0001", "KOSDAQ": "1001"}


class MarketInvestorFlow(Event):
    """Latest market-wide cumulative investor flow snapshot."""

    market: str  # "KOSPI" / "KOSDAQ"
    foreign_net_krw: int  # 외인 누적 순매수 (원)
    institution_net_krw: int  # 기관 누적
    individual_net_krw: int  # 개인 누적


@dataclass
class _FlowSnapshot:
    foreign_net_krw: int
    institution_net_krw: int
    individual_net_krw: int


def fetch_market_investor_flow(
    market: str = "KOSPI",
    settings: Settings | None = None,
) -> _FlowSnapshot | None:
    """Single-shot fetch of market-wide cumulative investor flow.

    Returns None on KIS error (rt_cd != 0, time limit, etc.). Time limit
    typically applies outside market hours.
    """
    if market not in _MARKET_CODE:
        raise ValueError(f"unknown market: {market!r}")
    settings = settings or get_settings()
    token = get_token(settings)
    client = make_client(settings)
    try:
        resp = client.get(
            _PATH,
            headers={
                "authorization": f"Bearer {token}",
                "appkey": settings.app_key,
                "appsecret": settings.app_secret,
                "tr_id": _TR_ID,
                "tr_cont": "",
            },
            params={
                "FID_INPUT_ISCD": _MARKET_CODE[market],
                "FID_INPUT_ISCD_2": _MARKET_CODE[market],
            },
        )
        data = resp.json()
    finally:
        client.close()

    if data.get("rt_cd") != "0":
        log.warning("KIS investor-time rt_cd=%s msg=%s", data.get("rt_cd"), data.get("msg1"))
        return None
    rows = data.get("output") or data.get("output1") or []
    if not rows:
        return None
    head = rows[0]  # latest minute row

    def _parse(key: str) -> int:
        v = head.get(key)
        if v is None or v == "":
            return 0
        try:
            return int(v) * _PBMN_UNIT_TO_KRW
        except (ValueError, TypeError):
            return 0

    return _FlowSnapshot(
        foreign_net_krw=_parse("frgn_ntby_tr_pbmn"),
        institution_net_krw=_parse("orgn_ntby_tr_pbmn"),
        individual_net_krw=_parse("prsn_ntby_tr_pbmn"),
    )


class RealtimeInvestorFlowSource:
    """Async poller. Every ``interval_sec`` calls fetch_market_investor_flow()
    for each market in ``markets`` and publishes MarketInvestorFlow events."""

    def __init__(
        self,
        bus: EventBus,
        *,
        markets: tuple[str, ...] = ("KOSPI",),
        interval_sec: float = 60.0,
        fetcher: Callable[[str], _FlowSnapshot | None] | None = None,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")
        for m in markets:
            if m not in _MARKET_CODE:
                raise ValueError(f"unknown market: {m!r}")
        self._bus = bus
        self.markets = markets
        self.interval_sec = interval_sec
        self._fetcher = fetcher or (lambda m: fetch_market_investor_flow(m))
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.poll_count = 0

    @property
    def running(self) -> bool:
        return self._running

    def step(self) -> int:
        polled = 0
        now = datetime.now(UTC)
        for market in self.markets:
            try:
                snap = self._fetcher(market)
            except Exception as e:
                log.warning("investor-flow fetch failed for %s: %s", market, e)
                continue
            if snap is None:
                continue
            self._bus.publish(MarketInvestorFlow(
                symbol="MARKET", timestamp=now, market=market,
                foreign_net_krw=snap.foreign_net_krw,
                institution_net_krw=snap.institution_net_krw,
                individual_net_krw=snap.individual_net_krw,
            ))
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


def score_from_market_flow(
    flow: MarketInvestorFlow,
    *,
    strong_krw: int = 1_000_000_000_000,  # 1조 = strong
) -> float:
    """Map market-wide (foreign + institution) net buy → fundamental score [0.7, 1.3].

    - foreign + institution ≥ +1조 → 1.3 (강한 risk-on, 종합 BUY signal boost)
    - == 0 → 1.0 (neutral)
    - ≤ -1조 → 0.7 (risk-off, BUY veto support)
    Linear interpolation.
    """
    if strong_krw <= 0:
        raise ValueError("strong_krw must be positive")
    smart_money = flow.foreign_net_krw + flow.institution_net_krw
    if smart_money >= strong_krw:
        return 1.3
    if smart_money <= -strong_krw:
        return 0.7
    return 1.0 + 0.3 * (smart_money / strong_krw)
