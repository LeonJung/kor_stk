"""DominantThemeDetector — 거래대금 ∩ 등락률 상위 종목들의 테마 자동 추출.

book "광기 노트 ②" 사상: 시장에서 돈이 몰리고 등락이 큰 종목들은 보통 같은
테마로 묶여 있다. 거래대금 상위 N개 ∩ 등락률 상위 N개 → 그 종목들의 테마
mapping (외부 데이터) → 그 테마들 중 가장 많이 묶인 것 = 오늘의 dominant
theme.

ThemePairBuilder 가 사용하는 ``theme_of`` mapping 을 자동으로 채우는 보조
모듈. 실 운영 시 매일 09:00 직전에 실행 → DominantThemeReport → 결과를
PairFollow.pairs 갱신 / OpeningMomentum.watchlist 결정 등에 활용.

V1 입력: 종목별 (turnover, change_pct, theme) tuple. 외부 데이터 (KRX 거래대금
ranking + FnGuide 테마 분류 등) 가 source. 본 모듈은 이를 받아 정렬 + 교집합
+ theme aggregation 만 수행.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolStats:
    symbol: str
    turnover_krw: int  # 거래대금
    change_pct: float  # 등락률
    theme: str | None = None


@dataclass(frozen=True)
class ThemeReport:
    dominant_themes: tuple[tuple[str, int], ...]  # (theme, count) ranked desc
    intersection_symbols: tuple[str, ...]  # symbols in both top lists
    theme_of_symbol: dict[str, str]  # for downstream ThemePairBuilder


class DominantThemeDetector:
    def __init__(
        self,
        *,
        top_n_turnover: int = 100,
        top_n_change: int = 100,
        min_change_pct: float = 5.0,
    ) -> None:
        if top_n_turnover < 1 or top_n_change < 1:
            raise ValueError("top_n must be >= 1")
        if min_change_pct <= 0:
            raise ValueError("min_change_pct must be positive")
        self.top_n_turnover = top_n_turnover
        self.top_n_change = top_n_change
        self.min_change_pct = min_change_pct

    def analyze(self, stats: Iterable[SymbolStats]) -> ThemeReport:
        materialized = list(stats)
        # Top by turnover
        by_turnover = sorted(materialized, key=lambda s: s.turnover_krw, reverse=True)
        top_t = {s.symbol for s in by_turnover[: self.top_n_turnover]}
        # Top by absolute change_pct (only if above floor)
        eligible = [s for s in materialized if abs(s.change_pct) >= self.min_change_pct]
        by_change = sorted(eligible, key=lambda s: abs(s.change_pct), reverse=True)
        top_c = {s.symbol for s in by_change[: self.top_n_change]}
        # Intersection
        inter = top_t & top_c
        inter_symbols = tuple(sorted(inter))
        # Theme aggregation — only count symbols in intersection that have a theme
        theme_counts: Counter[str] = Counter()
        theme_of_symbol: dict[str, str] = {}
        for s in materialized:
            if s.symbol in inter and s.theme:
                theme_counts[s.theme] += 1
                theme_of_symbol[s.symbol] = s.theme
        dominant = tuple(theme_counts.most_common())
        return ThemeReport(
            dominant_themes=dominant,
            intersection_symbols=inter_symbols,
            theme_of_symbol=theme_of_symbol,
        )
