"""Valuation snapshot — PER / PBR / EPS / BPS via KIS inquire-price.

fundamental_strategy.md §A (밸류에이션) 4 번째 source. KIS REST endpoint
``/uapi/domestic-stock/v1/quotations/inquire-price`` 가 시세 + 누적 거래 정보
80+ 필드와 함께 valuation 지표를 반환. 단발 호출만으로 PER/PBR/EPS/BPS 확보.

KIS mock 검증 (2026-05-13): 005930 삼성전자 PER=42.50, PBR=4.36, EPS=6564, BPS=63997.
시간 제약 X (foreign-flow / program-flow 와 달리 24h 가능).

API:
- fetch_valuation(symbol) → (per, pbr, eps, bps) | None
- score_from_per(per, *, neutral_per=15.0) — 저평가 → 점수 ↑ [0.7~1.3]
- score_from_pbr(pbr) — 저PBR (자산가치 미만) → 점수 ↑ [0.9~1.3]
- blend_per_pbr_score(per, pbr) — 두 input 평균
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ks_ws.auth.token import get_token
from ks_ws.config import Settings, get_settings
from ks_ws.kis.http import make_client

log = logging.getLogger("ks_ws.sources.valuation")

_INQUIRE_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
_TR_ID_INQUIRE_PRICE = "FHKST01010100"


@dataclass
class Valuation:
    per: float | None
    pbr: float | None
    eps: float | None
    bps: float | None


def fetch_valuation(symbol: str, settings: Settings | None = None) -> Valuation | None:
    """Single-shot fetch of (PER, PBR, EPS, BPS) for a stock. None on error."""
    settings = settings or get_settings()
    token = get_token(settings)
    client = make_client(settings)
    try:
        resp = client.get(
            _INQUIRE_PRICE_PATH,
            headers={
                "authorization": f"Bearer {token}",
                "appkey": settings.app_key,
                "appsecret": settings.app_secret,
                "tr_id": _TR_ID_INQUIRE_PRICE,
                "tr_cont": "",
            },
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        data = resp.json()
    finally:
        client.close()

    if data.get("rt_cd") != "0":
        log.warning("KIS inquire-price rt_cd=%s msg=%s", data.get("rt_cd"), data.get("msg1"))
        return None
    out = data.get("output") or {}

    def _parse(key: str) -> float | None:
        v = out.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    return Valuation(per=_parse("per"), pbr=_parse("pbr"), eps=_parse("eps"), bps=_parse("bps"))


def score_from_per(per: float | None, *, neutral_per: float = 15.0) -> float:
    """Map PER → score [0.7, 1.3]. 저평가(PER < neutral) → 점수 ↑.

    Anchors (default neutral_per=15):
    - per <= 5      → 1.3 (deep value)
    - per == 15     → 1.0 (neutral, KOSPI 평균 부근)
    - per == 30     → 0.85
    - per >= 50     → 0.7
    Negative PER (적자) → 0.8 (mild penalty, growth narrative 도 있음).
    """
    if per is None:
        return 1.0
    if per < 0:
        return 0.8
    if per <= 5:
        return 1.3
    if per <= neutral_per:
        # 5 → 1.3, neutral_per → 1.0
        return 1.3 - 0.3 * ((per - 5) / (neutral_per - 5))
    if per <= 30:
        # neutral_per → 1.0, 30 → 0.85
        return 1.0 - 0.15 * ((per - neutral_per) / (30 - neutral_per))
    if per <= 50:
        # 30 → 0.85, 50 → 0.7
        return 0.85 - 0.15 * ((per - 30) / 20)
    return 0.7


def score_from_pbr(pbr: float | None) -> float:
    """Map PBR → score [0.9, 1.3]. 저PBR (자산가치 미만) → 점수 ↑.

    - pbr <= 0.5 → 1.3 (deep value)
    - pbr == 1.0 → 1.15 (자산가치)
    - pbr == 2.0 → 1.05
    - pbr == 3.0 → 1.0 (neutral)
    - pbr >= 5.0 → 0.9
    """
    if pbr is None or pbr <= 0:
        return 1.0
    if pbr <= 0.5:
        return 1.3
    if pbr <= 1.0:
        return 1.3 - 0.15 * ((pbr - 0.5) / 0.5)  # 0.5→1.3, 1.0→1.15
    if pbr <= 2.0:
        return 1.15 - 0.10 * ((pbr - 1.0) / 1.0)  # 1.0→1.15, 2.0→1.05
    if pbr <= 3.0:
        return 1.05 - 0.05 * ((pbr - 2.0) / 1.0)  # 2.0→1.05, 3.0→1.00
    if pbr <= 5.0:
        return 1.00 - 0.10 * ((pbr - 3.0) / 2.0)  # 3.0→1.00, 5.0→0.90
    return 0.9


def blend_per_pbr_score(per: float | None, pbr: float | None) -> float:
    """평균 (단순). 둘 다 None 이면 1.0 (neutral)."""
    return (score_from_per(per) + score_from_pbr(pbr)) / 2.0
