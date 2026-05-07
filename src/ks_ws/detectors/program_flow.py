"""ProgramFlowDetector — emits enter/exit events when KRX program-trade
net buy flow crosses configured thresholds for a symbol.

The poller (or replay driver) calls `feed(symbol, net_buy_krw, ts)` with
a pre-aggregated net flow figure (typically from the KIS
`inquire-program-trade-by-stock` REST endpoint, sampled on a fixed
cadence). The detector keeps a tiny per-symbol state machine:

    not_entered ─── net >= entry_threshold ──→ entered    (publish Enter)
    entered     ─── net <= exit_threshold  ──→ not_entered (publish Exit)

`window_seconds` is metadata propagated onto the event so consumers
know what aggregation horizon the figure represents — the detector
itself does not maintain a rolling buffer in v1; that lives in the
poller / source driver. Once we wire a real KIS poller we can move
windowing in here without changing the published event shape.

Hysteresis (entry_threshold > exit_threshold) is recommended to avoid
chattering near the boundary.
"""

from datetime import datetime

from ks_ws.bus import EventBus
from ks_ws.events import ProgramFlowEnter, ProgramFlowExit


class ProgramFlowDetector:
    def __init__(
        self,
        bus: EventBus,
        *,
        window_seconds: int = 30,
        entry_threshold_krw: int = 1_000_000_000,  # 10억
        exit_threshold_krw: int = 100_000_000,  # 1억
    ) -> None:
        if entry_threshold_krw < exit_threshold_krw:
            raise ValueError("entry_threshold must be >= exit_threshold (hysteresis)")
        self._bus = bus
        self.window_seconds = window_seconds
        self.entry_threshold_krw = entry_threshold_krw
        self.exit_threshold_krw = exit_threshold_krw
        self._entered: dict[str, bool] = {}

    def is_entered(self, symbol: str) -> bool:
        return self._entered.get(symbol, False)

    def feed(self, symbol: str, net_buy_krw: int, timestamp: datetime) -> None:
        was_entered = self._entered.get(symbol, False)

        if not was_entered and net_buy_krw >= self.entry_threshold_krw:
            self._entered[symbol] = True
            self._bus.publish(
                ProgramFlowEnter(
                    symbol=symbol,
                    timestamp=timestamp,
                    delta_krw=net_buy_krw,
                    window_seconds=self.window_seconds,
                )
            )
        elif was_entered and net_buy_krw <= self.exit_threshold_krw:
            self._entered[symbol] = False
            self._bus.publish(
                ProgramFlowExit(
                    symbol=symbol,
                    timestamp=timestamp,
                    delta_krw=net_buy_krw,
                    window_seconds=self.window_seconds,
                )
            )
