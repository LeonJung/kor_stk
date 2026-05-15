"""Stepped-anchor trailing exit for 장기 (long-term) strategies.

사용자 룰 (2026-05-15) — 장기 청산:
- 100% / 200% / 300% / ... 단계 도달 시 그 단계가 anchor
- 통과한 최고 단계 anchor 기준 -20% 떨어지면 익절
- anchor 아직 미활성 (= 100% 미도달) 상태에서 entry -20% 도달 시 손절
- 더 높은 단계 도달 시 anchor 갱신 (ratchet)

예 (entry = 100):
- 180 (80%) — anchor X, SL = 80, hold
- 220 (120%) → max_n = 2, anchor = 200, trailing = 160
- 280 (180%) → anchor 200 그대로, trailing = 160
- 320 (220%) → max_n = 3, anchor = 300, trailing = 240
- 245 → 240 위, hold
- 235 → trailing 240 이탈 → SELL 익절
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class LongTermTrailingState:
    """Per-position trailing state. Strategy 가 _Pos 안에 보관."""

    entry: int
    max_anchor_n: int = 1  # 100% 도달 전 = 1 (entry × 1)

    def update(self, price: int) -> None:
        """매 tick 시 호출. max_anchor_n 만 ratchet."""
        if price <= 0 or self.entry <= 0:
            return
        current_n = int(price / self.entry)
        if current_n > self.max_anchor_n:
            self.max_anchor_n = current_n

    def should_exit(
        self, price: int, *, initial_sl_pct: float = 20.0,
        trailing_pct: float = 20.0,
    ) -> tuple[Literal["hold", "tp", "sl"], int]:
        """Return (action, trigger_price).

        - "tp": anchor 활성 + anchor -trailing_pct% 이탈 (익절)
        - "sl": anchor 미활성 + entry -initial_sl_pct% 이탈 (손절)
        - "hold": 둘 다 아님
        """
        if self.max_anchor_n >= 2:
            anchor_price = self.entry * self.max_anchor_n
            trailing_price = int(anchor_price * (1 - trailing_pct / 100))
            if price <= trailing_price:
                return ("tp", trailing_price)
            return ("hold", trailing_price)
        sl_price = int(self.entry * (1 - initial_sl_pct / 100))
        if price <= sl_price:
            return ("sl", sl_price)
        return ("hold", sl_price)
