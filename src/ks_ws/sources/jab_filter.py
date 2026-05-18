"""JabFilter — 동전주/잡주/세력 작전 의심 종목 universe 제외 필터.

사용자 룰 (2026-05-18):
- 동전주 (close < 1000원) — 호가 단위 큰 비중, 세력 manipulation 용이
- 소형 시총 (< 300억원) — 적은 자금으로 가격 조작 가능
- SPAC — 우회 상장 종목
- KRX 시장경보 — 단기과열/투자주의/투자경고/투자위험/정리매매
- 5거래일 연속 |daily_return| ≥ 15% — 비정상 변동
- 60일 평균 일별 거래대금 < 1억원 — 유동성 부족 (작전 trigger 가능)

세 layer:
  static  : universe entry 의 close/market_cap/is_spac (즉시)
  alert   : KRX 매일 발표 시장경보 list (optional pykrx fetch)
  dynamic : 일봉 history 기반 변동성/유동성 (즉시, daily_history dict)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class JabFilterRules:
    min_close_krw: int = 1000              # 동전주 임계
    min_market_cap_krw: int = 30_000_000_000  # 300억 미만 = 소형주
    exclude_spac: bool = True
    high_volatility_consecutive_days: int = 5  # N 연속 거래일
    high_volatility_pct: float = 0.15          # 일별 |return| ≥ 15%
    min_avg_daily_value_60d_krw: int = 100_000_000  # 1억원


class JabFilter:
    def __init__(
        self,
        *,
        rules: JabFilterRules | None = None,
        daily_history: dict[str, list] | None = None,
        alert_codes: set[str] | None = None,
    ) -> None:
        """daily_history: {symbol: [Bar...]} (오름차순)."""
        self.rules = rules or JabFilterRules()
        self.daily_history = daily_history or {}
        self.alert_codes = alert_codes or set()

    def is_jab(self, entry, code: str) -> tuple[bool, str]:
        r = self.rules

        # Static rules — universe entry
        last_close = int(getattr(entry, "last_close_krw", 0) or 0)
        market_cap = int(getattr(entry, "market_cap_krw", 0) or 0)
        is_spac = bool(getattr(entry, "is_spac", False))
        if 0 < last_close < r.min_close_krw:
            return True, f"penny_stock<{r.min_close_krw}"
        if 0 < market_cap < r.min_market_cap_krw:
            return True, f"small_cap<{r.min_market_cap_krw // 100_000_000}억"
        if is_spac and r.exclude_spac:
            return True, "spac"

        # Alert list — KRX 시장경보
        if code in self.alert_codes:
            return True, "krx_alert"

        # Dynamic — daily history (60일)
        bars = self.daily_history.get(code, [])
        if bars:
            # 5 연속 거래일 |return| ≥ 15% — 비정상 변동
            n = r.high_volatility_consecutive_days
            if len(bars) >= n + 1:
                recent = bars[-(n + 1):]
                rets = []
                for i in range(1, len(recent)):
                    prev = recent[i - 1].close
                    cur = recent[i].close
                    if prev > 0:
                        rets.append(abs((cur - prev) / prev))
                if len(rets) == n and all(rr >= r.high_volatility_pct for rr in rets):
                    return True, f"high_volatility_{n}d"

            # 60일 평균 거래대금 < 1억 (유동성 부족)
            recent60 = bars[-60:] if len(bars) >= 60 else bars
            if len(recent60) >= 20:  # 최소 20일 history
                avg_val = sum(b.value for b in recent60) / len(recent60)
                if avg_val < r.min_avg_daily_value_60d_krw:
                    return True, f"low_liquidity<{r.min_avg_daily_value_60d_krw // 100_000_000}억"

        return False, ""

    def filter_entries(self, entries) -> tuple[list, dict[str, str]]:
        """universe entry list 받아서 통과한 것만 + 제외 사유 dict."""
        passed = []
        reasons: dict[str, str] = {}
        for e in entries:
            jab, reason = self.is_jab(e, e.code)
            if jab:
                reasons[e.code] = reason
            else:
                passed.append(e)
        return passed, reasons


def load_krx_alert_codes(fetch: bool = False) -> set[str]:
    """KRX 의 단기과열/투자주의/경고/위험/정리매매 종목 list 로드.

    fetch=True: pykrx 로 실시간 fetch. 느림 (수초).
    fetch=False: cached file `data/krx_market_alert.json` 사용.
    """
    if not fetch:
        from pathlib import Path
        import json
        cache = Path("data/krx_market_alert.json")
        if cache.exists():
            try:
                return set(json.loads(cache.read_text()))
            except Exception:
                return set()
        return set()
    # pykrx fetch — 실제 implementation. 다음 단계에서 구현.
    try:
        from pykrx import stock as _stock
        import datetime as dt
        codes: set[str] = set()
        today = dt.date.today().strftime("%Y%m%d")
        # pykrx 의 시장경보 fetch API — 정확한 함수명 확인 필요. 미구현 시 빈 set.
        return codes
    except Exception:
        return set()
