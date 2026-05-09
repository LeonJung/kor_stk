"""BuyCriteria4Strategy (Sec 13) — 매수 4기준 (이슈/차트/수급/시장분위기)
boolean 조합 → confidence 0~1.

book Sec 13: 종가 베팅의 4 기준
1. 이슈 (재료, 호재 뉴스)
2. 차트 (캔들 패턴, 신고가, 라운드 피겨 등)
3. 수급 (외국인/기관 순매수, 거래대금)
4. 시장 분위기 (regime, 지수 트렌드)

4 기준 모두 통과 = confidence 1.0, 부분 충족 = N/4. min_criteria 미만이면
신호 X. weights 로 기준별 가중치 조정 가능.

V1: 외부 caller 가 매번 ``evaluate(symbol, criteria, when)`` 호출. caller 가
4 boolean 을 어떻게 평가할지는 caller 책임 (CombinedDetector / 외부 데이터
파이프라인 등).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ks_ws.domain import Side, Signal


@dataclass(frozen=True)
class Criteria4:
    issue: bool = False  # 이슈/재료 호재
    chart: bool = False  # 차트 패턴 양호
    supply_demand: bool = False  # 수급 (외국인/기관 매수)
    market_mood: bool = False  # 시장 분위기 (regime sideways/up)

    def passing_count(self) -> int:
        return sum((self.issue, self.chart, self.supply_demand, self.market_mood))


@dataclass
class BuyCriteria4Strategy:
    """Stateless meta-strategy. Caller invokes evaluate() with the four
    booleans + a current price/timestamp; returns BUY signal if score
    threshold met. Not a full Strategy subclass since hooks aren't relevant
    (criteria computation is offline / scheduled)."""

    name: str = "buy_criteria_4"
    min_criteria: int = 3  # default: at least 3/4
    weights: dict[str, float] = field(
        default_factory=lambda: {"issue": 0.35, "chart": 0.20, "supply_demand": 0.30, "market_mood": 0.15}
    )
    confidence_floor: float = 0.4

    def __post_init__(self) -> None:
        if not (1 <= self.min_criteria <= 4):
            raise ValueError("min_criteria must be in [1, 4]")
        if abs(sum(self.weights.values()) - 1.0) > 0.001:
            raise ValueError("weights must sum to 1.0")
        if not (0.0 <= self.confidence_floor <= 1.0):
            raise ValueError("confidence_floor must be in [0, 1]")

    def evaluate(
        self,
        *,
        symbol: str,
        criteria: Criteria4,
        when: datetime,
        side: Side = Side.BUY,
    ) -> Signal | None:
        passing = criteria.passing_count()
        if passing < self.min_criteria:
            return None
        score = (
            self.weights["issue"] * (1.0 if criteria.issue else 0.0)
            + self.weights["chart"] * (1.0 if criteria.chart else 0.0)
            + self.weights["supply_demand"] * (1.0 if criteria.supply_demand else 0.0)
            + self.weights["market_mood"] * (1.0 if criteria.market_mood else 0.0)
        )
        confidence = max(self.confidence_floor, min(1.0, score))
        return Signal(
            symbol=symbol,
            side=side,
            confidence=confidence,
            strategy=self.name,
            timestamp=when,
            note=(
                f"4-criteria: {passing}/4 "
                f"(issue={int(criteria.issue)} chart={int(criteria.chart)} "
                f"supply={int(criteria.supply_demand)} mood={int(criteria.market_mood)})"
            ),
        )


CriteriaEvaluator = Callable[[str, datetime], Criteria4]
