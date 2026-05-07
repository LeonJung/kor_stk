"""ProgramFlowStrategy — buy when program-trade flow enters, sell when it exits.

Listens for `ProgramFlowEnter` / `ProgramFlowExit` from the detector
and emits a single Signal per event. Confidence on entry scales with
the magnitude of the net flow delta (capped at `confidence_cap_krw`),
so a 50억원 surge produces a stronger signal than a 12억원 one. Exit
confidence is fixed — exits are a binary "the wave is over, get out"
without graduated strength.

Sizing happens in the Allocator, not here.
"""

from datetime import UTC, datetime

from ks_ws.domain import Side, Signal
from ks_ws.events import Event, ProgramFlowEnter, ProgramFlowExit
from ks_ws.strategies.base import Strategy


class ProgramFlowStrategy(Strategy):
    name = "program_flow"

    def __init__(
        self,
        *,
        confidence_cap_krw: int = 5_000_000_000,  # 50억 → confidence 1.0
        exit_confidence: float = 0.7,
        urgency: str = "high",
    ) -> None:
        if confidence_cap_krw <= 0:
            raise ValueError("confidence_cap_krw must be positive")
        if not (0.0 <= exit_confidence <= 1.0):
            raise ValueError("exit_confidence must be in [0, 1]")
        self.confidence_cap_krw = confidence_cap_krw
        self.exit_confidence = exit_confidence
        self.urgency = urgency

    def on_event(self, event: Event) -> list[Signal]:
        now = datetime.now(UTC)
        if isinstance(event, ProgramFlowEnter):
            confidence = min(1.0, event.delta_krw / self.confidence_cap_krw)
            return [
                Signal(
                    symbol=event.symbol,
                    side=Side.BUY,
                    confidence=confidence,
                    urgency=self.urgency,  # type: ignore[arg-type]
                    strategy=self.name,
                    timestamp=now,
                    note=f"prog flow entered, delta_krw={event.delta_krw:,}",
                )
            ]
        if isinstance(event, ProgramFlowExit):
            return [
                Signal(
                    symbol=event.symbol,
                    side=Side.SELL,
                    confidence=self.exit_confidence,
                    urgency=self.urgency,  # type: ignore[arg-type]
                    strategy=self.name,
                    timestamp=now,
                    note=f"prog flow exited, delta_krw={event.delta_krw:,}",
                )
            ]
        return []
