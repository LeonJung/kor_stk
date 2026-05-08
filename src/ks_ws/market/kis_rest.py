"""KIS REST market data fetchers.

조회용 tr_id 는 mock / live 가 동일하므로 base URL 만 환경에 따라 달라짐 —
Settings.env 가 그걸 처리하고 있어 호출자는 신경 쓸 필요 없음.

KIS 는 호출당 최대 ~100 행 (분봉/일봉) 또는 단일 스냅샷을 반환. 더 긴 기간은
호출자가 chunked 로 나눠서 호출해야 함 (rate limit: live 기준 ~20 req/sec).
"""

import logging
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel

from ks_ws.auth.token import get_token
from ks_ws.config import Settings, get_settings
from ks_ws.domain import Bar, OrderBook, OrderBookLevel
from ks_ws.kis.http import make_client

log = logging.getLogger("ks_ws.market.kis_rest")

_DAILY_BARS_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
_TR_ID_DAILY_BARS = "FHKST03010100"

_MINUTE_BARS_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
_TR_ID_MINUTE_BARS = "FHKST03010200"

_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
_TR_ID_PRICE = "FHKST01010100"

_ORDERBOOK_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
_TR_ID_ORDERBOOK = "FHKST01010200"

PeriodCode = Literal["D", "W", "M", "Y"]
_PERIOD_TO_TIMEFRAME: dict[PeriodCode, str] = {
    "D": "1d",
    "W": "1w",
    "M": "1mo",
    "Y": "1y",
}


def fetch_daily_bars(
    symbol: str,
    *,
    start: date,
    end: date,
    period: PeriodCode = "D",
    market: str = "J",
    settings: Settings | None = None,
) -> list[Bar]:
    """Fetch daily / weekly / monthly / yearly bars for a single symbol.

    Returns Bars sorted oldest-first. KIS itself returns newest-first; we
    reverse for the project convention.

    market: KRX 시장 구분 코드 — "J" (주식·ETF·ETN) 가 거의 모든 일반 종목 커버.
    period: "D" 일봉 / "W" 주봉 / "M" 월봉 / "Y" 년봉.
    """
    settings = settings or get_settings()
    token = get_token(settings)

    client = make_client(settings)
    try:
        resp = client.get(
            _DAILY_BARS_PATH,
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": period,
                # 0 = 수정주가 (back-adjusted; what backtests want).
                "FID_ORG_ADJ_PRC": "0",
            },
            headers={
                "authorization": f"Bearer {token}",
                "tr_id": _TR_ID_DAILY_BARS,
                "custtype": "P",
            },
        )
        resp.raise_for_status()
        return _parse_daily_response(symbol, period, resp.json())
    finally:
        client.close()


def _parse_daily_response(symbol: str, period: PeriodCode, data: dict) -> list[Bar]:
    if data.get("rt_cd") != "0":
        log.warning(
            "KIS returned non-success rt_cd=%s msg=%s",
            data.get("rt_cd"),
            data.get("msg1"),
        )
    timeframe = _PERIOD_TO_TIMEFRAME[period]
    rows = data.get("output2") or []
    bars: list[Bar] = []
    for row in rows:
        # KIS occasionally returns blank rows when the requested range exceeds
        # available history; skip them.
        date_str = row.get("stck_bsop_date")
        if not date_str:
            continue
        ts = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=UTC)
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                timeframe=timeframe,
                open=int(row["stck_oprc"]),
                high=int(row["stck_hgpr"]),
                low=int(row["stck_lwpr"]),
                close=int(row["stck_clpr"]),
                volume=int(row["acml_vol"]),
                value=int(row["acml_tr_pbmn"]),
            )
        )
    bars.sort(key=lambda b: b.timestamp)
    return bars


def fetch_minute_bars(
    symbol: str,
    *,
    end_time: str = "153000",
    include_past_data: bool = True,
    market: str = "J",
    settings: Settings | None = None,
) -> list[Bar]:
    """Fetch ~30 most-recent 1-minute bars ending at `end_time` (HHMMSS, KST).

    KIS limits the call to roughly 30 bars; for a longer history paginate by
    walking `end_time` backwards. Returns oldest-first.
    """
    settings = settings or get_settings()
    token = get_token(settings)
    client = make_client(settings)
    try:
        resp = client.get(
            _MINUTE_BARS_PATH,
            params={
                "FID_ETC_CLS_CODE": "",
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": end_time,
                "FID_PW_DATA_INCU_YN": "Y" if include_past_data else "N",
            },
            headers={
                "authorization": f"Bearer {token}",
                "tr_id": _TR_ID_MINUTE_BARS,
                "custtype": "P",
            },
        )
        resp.raise_for_status()
        return _parse_minute_response(symbol, resp.json())
    finally:
        client.close()


def _parse_minute_response(symbol: str, data: dict) -> list[Bar]:
    if data.get("rt_cd") != "0":
        log.warning(
            "KIS returned non-success rt_cd=%s msg=%s",
            data.get("rt_cd"),
            data.get("msg1"),
        )
    rows = data.get("output2") or []
    bars: list[Bar] = []
    for row in rows:
        date_str = row.get("stck_bsop_date")
        time_str = row.get("stck_cntg_hour")
        if not date_str or not time_str:
            continue
        ts = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                timeframe="1m",
                open=int(row["stck_oprc"]),
                high=int(row["stck_hgpr"]),
                low=int(row["stck_lwpr"]),
                close=int(row["stck_prpr"]),
                volume=int(row["cntg_vol"]),
                value=int(row["acml_tr_pbmn"]),
            )
        )
    bars.sort(key=lambda b: b.timestamp)
    return bars


class CurrentPrice(BaseModel):
    """Snapshot from inquire-price. Subset of KIS fields most strategies use."""

    symbol: str
    timestamp: datetime
    price: int  # 현재가
    open: int
    high: int
    low: int
    prev_close: int  # 전일 종가
    change: int  # 전일 대비
    change_pct: float  # 전일 대비율
    volume: int  # 누적 거래량
    value: int  # 누적 거래대금


def fetch_current_price(
    symbol: str,
    *,
    market: str = "J",
    settings: Settings | None = None,
) -> CurrentPrice:
    settings = settings or get_settings()
    token = get_token(settings)
    client = make_client(settings)
    try:
        resp = client.get(
            _PRICE_PATH,
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol},
            headers={
                "authorization": f"Bearer {token}",
                "tr_id": _TR_ID_PRICE,
                "custtype": "P",
            },
        )
        resp.raise_for_status()
        return _parse_price_response(symbol, resp.json())
    finally:
        client.close()


def _parse_price_response(symbol: str, data: dict) -> CurrentPrice:
    if data.get("rt_cd") != "0":
        log.warning(
            "KIS returned non-success rt_cd=%s msg=%s",
            data.get("rt_cd"),
            data.get("msg1"),
        )
    out = data.get("output") or {}
    # Previous close: KIS exposes 전일 종가 as 기준가 (stck_sdpr) on the
    # inquire-price snapshot. stck_prdy_clpr exists in some other endpoints
    # but is empty here — keep it as a fallback in case behavior changes.
    return CurrentPrice(
        symbol=symbol,
        timestamp=datetime.now(UTC),
        price=int(out.get("stck_prpr") or 0),
        open=int(out.get("stck_oprc") or 0),
        high=int(out.get("stck_hgpr") or 0),
        low=int(out.get("stck_lwpr") or 0),
        prev_close=int(out.get("stck_sdpr") or out.get("stck_prdy_clpr") or 0),
        change=int(out.get("prdy_vrss") or 0),
        change_pct=float(out.get("prdy_ctrt") or 0.0),
        volume=int(out.get("acml_vol") or 0),
        value=int(out.get("acml_tr_pbmn") or 0),
    )


def fetch_orderbook(
    symbol: str,
    *,
    market: str = "J",
    settings: Settings | None = None,
) -> OrderBook:
    """Fetch the 10-deep orderbook snapshot."""
    settings = settings or get_settings()
    token = get_token(settings)
    client = make_client(settings)
    try:
        resp = client.get(
            _ORDERBOOK_PATH,
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol},
            headers={
                "authorization": f"Bearer {token}",
                "tr_id": _TR_ID_ORDERBOOK,
                "custtype": "P",
            },
        )
        resp.raise_for_status()
        return _parse_orderbook_response(symbol, resp.json())
    finally:
        client.close()


def _parse_orderbook_response(symbol: str, data: dict) -> OrderBook:
    if data.get("rt_cd") != "0":
        log.warning(
            "KIS returned non-success rt_cd=%s msg=%s",
            data.get("rt_cd"),
            data.get("msg1"),
        )
    # KIS returns the 10-deep book in output1 with fields askp1..askp10 / bidp1..bidp10
    # and matching volumes askp_rsqn1..askp_rsqn10 / bidp_rsqn1..bidp_rsqn10.
    out = data.get("output1") or {}
    bids: list[OrderBookLevel] = []
    asks: list[OrderBookLevel] = []
    for i in range(1, 11):
        bp = int(out.get(f"bidp{i}") or 0)
        bv = int(out.get(f"bidp_rsqn{i}") or 0)
        ap = int(out.get(f"askp{i}") or 0)
        av = int(out.get(f"askp_rsqn{i}") or 0)
        if bp > 0:
            bids.append(OrderBookLevel(price=bp, volume=bv))
        if ap > 0:
            asks.append(OrderBookLevel(price=ap, volume=av))
    return OrderBook(
        symbol=symbol,
        timestamp=datetime.now(UTC),
        bids=tuple(bids),
        asks=tuple(asks),
    )
