"""SectorClassifier — 종목 → GICS sector 매핑 (fundamental §H).

fundamental_strategy.md §H.1: GICS (글로벌 표준, 외인 관점) vs WICS (한국 특화)
vs KRX (KSIC). 기본 = GICS. 11 sectors / 24 industry groups.

API:
- SectorClassifier(mapping=DEFAULT_KOSPI_TOP30_GICS) — explicit injection
- classify(symbol) → sector str (or "unknown")
- same_sector(s1, s2) → bool
- sector_members(sector) → list[symbol]
- 추가 매핑은 set_mapping(symbol, sector) — runtime 갱신.

DEFAULT_KOSPI_TOP30_GICS 는 한국 시총 상위 30 종목의 GICS sector 수동 매핑
(2026-05 시점 추정). 사용자가 추가 종목 매핑 시 set_mapping. WICS / KRX
변종 매핑은 별도 dict 로 inject.
"""

from __future__ import annotations

from collections import defaultdict

# GICS 11 sector 표준 명칭 (한국어 병기는 fundamental_strategy.md §H.2 참조).
GICS_SECTORS: tuple[str, ...] = (
    "Information Technology",
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Health Care",
    "Financials",
    "Industrials",
    "Energy",
    "Materials",
    "Utilities",
    "Real Estate",
)

# 한국 시총 상위 30 종목 GICS sector 매핑 (2026-05 시점 추정).
# 출처 ks_ws/fundamental_strategy.md §H.2 + KRX 업종 + GICS 표준.
DEFAULT_KOSPI_TOP30_GICS: dict[str, str] = {
    # IT/반도체/하드웨어
    "005930": "Information Technology",  # 삼성전자
    "000660": "Information Technology",  # SK하이닉스
    "000990": "Information Technology",  # DB하이텍
    "042700": "Information Technology",  # 한미반도체
    "009150": "Information Technology",  # 삼성전기
    "034220": "Information Technology",  # LG디스플레이
    "006400": "Industrials",             # 삼성SDI (2차전지 = 산업재)
    "373220": "Industrials",             # LG에너지솔루션 (2차전지)
    "247540": "Industrials",             # 에코프로비엠
    # Communication
    "035420": "Communication Services",  # NAVER
    "035720": "Communication Services",  # 카카오
    "017670": "Communication Services",  # SK텔레콤
    "030200": "Communication Services",  # KT
    # 자동차/소비재
    "005380": "Consumer Discretionary",  # 현대차
    "000270": "Consumer Discretionary",  # 기아
    "012330": "Consumer Discretionary",  # 현대모비스
    "051910": "Consumer Discretionary",  # LG화학 (생활화학)
    # 헬스케어/바이오
    "207940": "Health Care",             # 삼성바이오로직스
    "068270": "Health Care",             # 셀트리온
    "302440": "Health Care",             # SK바이오사이언스
    "326030": "Health Care",             # SK바이오팜
    # Materials (소재/철강)
    "005490": "Materials",               # POSCO홀딩스
    "010130": "Materials",               # 고려아연
    "011170": "Materials",               # 롯데케미칼
    # Financials
    "105560": "Financials",              # KB금융
    "055550": "Financials",              # 신한지주
    "138040": "Financials",              # 메리츠금융지주
    "086790": "Financials",              # 하나금융지주
    "316140": "Financials",              # 우리금융지주
    "032830": "Financials",              # 삼성생명
    "088350": "Financials",              # 한화생명
    # Industrials (조선/방산/엔진/중공업)
    "329180": "Industrials",             # HD현대중공업
    "012450": "Industrials",             # 한화에어로스페이스
    "267260": "Industrials",             # HD현대일렉트릭
    "010120": "Industrials",             # LS ELECTRIC
    "042660": "Industrials",             # 한화오션
    # Energy
    "096770": "Energy",                  # SK이노베이션
    "010950": "Energy",                  # S-Oil
    # Consumer Staples
    "271560": "Consumer Staples",        # 오리온
    "097950": "Consumer Staples",        # CJ제일제당
    "006800": "Financials",              # 미래에셋증권 (증권 = Financials)
    # Real Estate / Utilities (한국 시장 작음)
    "015760": "Utilities",               # 한국전력
    "036460": "Utilities",               # 한국가스공사
    # 기타
    "028260": "Industrials",             # 삼성물산 (지주 + 건설/상사)
    "402340": "Information Technology",  # SK스퀘어 (지주, 단 IT 자회사 위주)
    "034020": "Industrials",             # 두산에너빌리티 (원전/발전)
}


class SectorClassifier:
    """Symbol → GICS sector lookup with runtime extension."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping: dict[str, str] = dict(mapping or DEFAULT_KOSPI_TOP30_GICS)
        self._validate(self._mapping)

    @staticmethod
    def _validate(mapping: dict[str, str]) -> None:
        for sym, sec in mapping.items():
            if sec not in GICS_SECTORS:
                raise ValueError(f"unknown GICS sector for {sym!r}: {sec!r}")

    def classify(self, symbol: str) -> str:
        return self._mapping.get(symbol, "unknown")

    def set_mapping(self, symbol: str, sector: str) -> None:
        if sector not in GICS_SECTORS and sector != "unknown":
            raise ValueError(f"unknown GICS sector: {sector!r}")
        self._mapping[symbol] = sector

    def same_sector(self, s1: str, s2: str) -> bool:
        sec1 = self.classify(s1)
        sec2 = self.classify(s2)
        if sec1 == "unknown" or sec2 == "unknown":
            return False
        return sec1 == sec2

    def sector_members(self, sector: str) -> list[str]:
        if sector not in GICS_SECTORS:
            raise ValueError(f"unknown GICS sector: {sector!r}")
        return sorted(s for s, sec in self._mapping.items() if sec == sector)

    def all_sectors_with_members(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for sym, sec in self._mapping.items():
            out[sec].append(sym)
        return {sec: sorted(syms) for sec, syms in out.items()}

    def __len__(self) -> int:
        return len(self._mapping)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._mapping
