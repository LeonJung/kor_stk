"""InstFgnFlowStrategy (J 수급 추적) — 외국인/기관 순매수 누적 추적 매매.

book strategy.md 의 J:
- entry: ProgramFlowEnter (기관) + ForeignNetBuy 누적 N일 양수 + 13:30 이후
- exit: 외국인/기관 매도세 전환 시 즉시
- hold: 1~3일 (단타-스윙 hybrid)
- 철학: 정보 우위 추종. 수급 빠지면 즉시 빠진다.

V1 단순:
- ProgramFlowEnter (institutional) 받으면 confidence 누적
- ForeignNetBuy (positive delta_krw) 받으면 streak counter 증가
- 둘 다 활성 + 13:30 이후 → BUY
- ProgramFlowExit 또는 ForeignNetBuy 음수 → SELL

streak counter = consecutive positive ForeignNetBuy events. 음수 1회로 reset.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from ks_ws.domain import Side, Signal
from ks_ws.events import (
    Event,
    ForeignNetBuy,
    ProgramFlowEnter,
    ProgramFlowExit,
)
from ks_ws.strategies.base import Strategy

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class _SymbolFlow:
    fgn_streak: int = 0  # consecutive positive ForeignNetBuy events
    fgn_total_krw: int = 0  # cumulative net buy
    last_program_enter_delta: int = 0
    in_position: bool = False
    history: list[int] = field(default_factory=list)


class InstFgnFlowStrategy(Strategy):
    name = "inst_fgn_flow"

    def __init__(
        self,
        *,
        watchlist: set[str] | None = None,
        entry_after_kst: time = time(13, 30),
        min_fgn_streak: int = 3,
        confidence: float = 0.6,
    ) -> None:
        if min_fgn_streak < 1:
            raise ValueError("min_fgn_streak must be >= 1")
        if not 0 < confidence <= 1:
            raise ValueError("confidence must be in (0, 1]")
        self.watchlist = set(watchlist) if watchlist else None
        self.entry_after_kst = entry_after_kst
        self.min_fgn_streak = min_fgn_streak
        self.confidence = confidence
        self._flows: dict[str, _SymbolFlow] = defaultdict(_SymbolFlow)

    def on_event(self, event: Event) -> list[Signal]:
        symbol = getattr(event, "symbol", None)
        if symbol is None:
            return []
        if self.watchlist is not None and symbol not in self.watchlist:
            return []
        flow = self._flows[symbol]

        if isinstance(event, ForeignNetBuy):
            if event.delta_krw > 0:
                flow.fgn_streak += 1
                flow.fgn_total_krw += event.delta_krw
            else:
                # Bearish flip → exit if open
                flow.fgn_streak = 0
                if flow.in_position:
                    flow.in_position = False
                    return [
                        Signal(
                            symbol=symbol, side=Side.SELL, confidence=1.0,
                            urgency="high", strategy=self.name, timestamp=event.timestamp,
                            note=f"foreign net SELL {event.delta_krw:,} → exit",
                        )
                    ]
            return self._maybe_enter(event, symbol, flow)

        if isinstance(event, ProgramFlowEnter):
            flow.last_program_enter_delta = event.delta_krw
            return self._maybe_enter(event, symbol, flow)

        if isinstance(event, ProgramFlowExit):
            if flow.in_position:
                flow.in_position = False
                return [
                    Signal(
                        symbol=symbol, side=Side.SELL, confidence=1.0,
                        urgency="high", strategy=self.name, timestamp=event.timestamp,
                        note=f"program exited delta_krw={event.delta_krw:,}",
                    )
                ]
            flow.last_program_enter_delta = 0
            return []

        return []

    def _maybe_enter(self, event: Event, symbol: str, flow: _SymbolFlow) -> list[Signal]:
        if flow.in_position:
            return []
        if flow.fgn_streak < self.min_fgn_streak:
            return []
        if flow.last_program_enter_delta <= 0:
            return []
        local_t = event.timestamp.astimezone(_KST).time()
        if local_t < self.entry_after_kst:
            return []
        flow.in_position = True
        return [
            Signal(
                symbol=symbol,
                side=Side.BUY,
                confidence=self.confidence,
                strategy=self.name,
                timestamp=event.timestamp,
                note=(
                    f"inst+fgn flow: streak={flow.fgn_streak}, "
                    f"prog_delta={flow.last_program_enter_delta:,}"
                ),
            )
        ]

    def open_positions(self) -> dict[str, _SymbolFlow]:
        return {sym: flow for sym, flow in self._flows.items() if flow.in_position}
