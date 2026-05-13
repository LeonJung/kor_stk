"""SectorRotationDetector — 섹터 강도 + 순환매 신호 (fundamental P-B).

fundamental_strategy.md §H.4 주덕 채널 §I.A 패턴:
"한 섹터가 신고가 → 다른 섹터로 수급 이동. 반도체 → 바이오 → 로봇 →
자동차 → 조선 → 원전 → 증권 순환."

V1 구현:
- 각 종목 daily change % (close vs prev close)
- SectorClassifier 로 sector 매핑
- 각 sector 평균 change % = sector strength
- 시점 별 ranking — 강세 top / 약세 bottom
- rotation = 강세 sector 가 어제 vs 오늘 바뀔 때 emit

API:
- compute_sector_strength(returns_by_symbol, classifier) → dict[sector, float]
- SectorStrengthTracker — daily snapshot 누적 + 어제 vs 오늘 ranking 비교
- SectorRotation event emit on leader change
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ks_ws.bus import EventBus
from ks_ws.events import SectorRotation
from ks_ws.sources.sector import SectorClassifier


def compute_sector_strength(
    returns_by_symbol: dict[str, float],
    classifier: SectorClassifier,
) -> dict[str, float]:
    """Aggregate per-symbol daily returns (%) into per-sector mean strength.

    Symbols with sector "unknown" are skipped.
    Empty input or all-unknown → empty dict.
    """
    by_sector: dict[str, list[float]] = defaultdict(list)
    for sym, ret in returns_by_symbol.items():
        sec = classifier.classify(sym)
        if sec == "unknown":
            continue
        by_sector[sec].append(ret)
    return {sec: sum(rs) / len(rs) for sec, rs in by_sector.items() if rs}


def rank_sectors(strengths: dict[str, float]) -> list[tuple[str, float]]:
    """Sort sectors by strength desc → [(sector, strength), ...]."""
    return sorted(strengths.items(), key=lambda kv: -kv[1])


@dataclass
class SectorStrengthSnapshot:
    strengths: dict[str, float]
    ranking: list[tuple[str, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.ranking:
            self.ranking = rank_sectors(self.strengths)


class SectorStrengthTracker:
    """Daily snapshot 누적. ``snapshot(returns_by_symbol)`` 호출마다 sector
    강도 계산 + history append. 어제 vs 오늘 leader 가 바뀌면
    ``SectorRotation`` 이벤트 emit.
    """

    def __init__(
        self,
        bus: EventBus | None,
        classifier: SectorClassifier,
        *,
        min_strength_diff_pct: float = 0.5,
    ) -> None:
        self._bus = bus
        self.classifier = classifier
        self.min_strength_diff_pct = min_strength_diff_pct
        self._history: list[SectorStrengthSnapshot] = []
        self.emit_count = 0

    def snapshot(self, returns_by_symbol: dict[str, float]) -> SectorStrengthSnapshot:
        strengths = compute_sector_strength(returns_by_symbol, self.classifier)
        snap = SectorStrengthSnapshot(strengths=strengths)
        # Check rotation against previous snapshot
        if self._history and snap.ranking and self._bus is not None:
            prev = self._history[-1]
            if prev.ranking:
                prev_leader = prev.ranking[0][0]
                cur_leader, cur_strength = snap.ranking[0]
                if cur_leader != prev_leader:
                    # Significance check
                    prev_leader_strength_now = strengths.get(prev_leader, 0.0)
                    if (cur_strength - prev_leader_strength_now) >= self.min_strength_diff_pct:
                        # Next candidate = 2nd in current ranking
                        next_cand = (
                            snap.ranking[1]
                            if len(snap.ranking) > 1
                            else (cur_leader, cur_strength)
                        )
                        from datetime import UTC, datetime
                        ev = SectorRotation(
                            symbol="MARKET",
                            timestamp=datetime.now(UTC),
                            leading_sector=cur_leader,
                            leading_strength=cur_strength,
                            next_candidate_sector=next_cand[0],
                            next_candidate_strength=next_cand[1],
                        )
                        self._bus.publish(ev)
                        self.emit_count += 1
        self._history.append(snap)
        if len(self._history) > 60:  # cap memory
            self._history = self._history[-60:]
        return snap

    def latest(self) -> SectorStrengthSnapshot | None:
        return self._history[-1] if self._history else None

    def history_len(self) -> int:
        return len(self._history)
