"""KIS REST market data fetchers.

v1: 국내주식 일/주/월/년봉 (inquire-daily-itemchartprice, tr_id FHKST03010100).
조회용 tr_id 는 mock / live 가 동일하므로 base URL 만 환경에 따라 달라짐 —
Settings.env 가 그걸 처리하고 있어 호출자는 신경 쓸 필요 없음.

KIS 는 호출당 최대 ~100 행을 반환. 더 긴 기간은 호출자가 chunked 로 나눠서
호출해야 함 (rate limit: live 기준 ~20 req/sec).
"""

import logging
from datetime import UTC, date, datetime
from typing import Literal

from ks_ws.auth.token import get_token
from ks_ws.config import Settings, get_settings
from ks_ws.domain import Bar
from ks_ws.kis.http import make_client

log = logging.getLogger("ks_ws.market.kis_rest")

_DAILY_BARS_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
_TR_ID_DAILY_BARS = "FHKST03010100"

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
