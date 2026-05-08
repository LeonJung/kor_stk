"""VolumeSpikeDetector — emits a VolumeSpike event when a Bar's volume
exceeds ``multiplier`` * the rolling baseline volume for that symbol.

Baseline is a simple moving average over the last ``window`` bars
(per symbol). The detector keeps a deque of recent volumes; a Bar is
compared against the baseline computed *before* including the current
bar so a single huge print doesn't poison its own baseline.

Hysteresis: once a spike fires for a symbol, no further spike is
emitted until the symbol's volume drops back below ``cooldown_multiplier``
* baseline. Prevents one sustained surge from flooding the bus.
"""

from collections import deque
from collections.abc import MutableMapping

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.events import VolumeSpike


class VolumeSpikeDetector:
    def __init__(
        self,
        bus: EventBus,
        *,
        window: int = 20,
        multiplier: float = 3.0,
        cooldown_multiplier: float = 1.5,
    ) -> None:
        if window < 2:
            raise ValueError("window must be at least 2 bars")
        if multiplier <= cooldown_multiplier:
            raise ValueError("multiplier must be strictly greater than cooldown_multiplier")
        self._bus = bus
        self.window = window
        self.multiplier = multiplier
        self.cooldown_multiplier = cooldown_multiplier
        self._history: MutableMapping[str, deque[int]] = {}
        self._spiked: dict[str, bool] = {}

    def feed(self, bar: Bar) -> None:
        history = self._history.setdefault(bar.symbol, deque(maxlen=self.window))
        if len(history) < self.window:
            history.append(bar.volume)
            return
        baseline = sum(history) / len(history)
        ratio = bar.volume / baseline if baseline > 0 else 0
        in_spike = self._spiked.get(bar.symbol, False)
        if not in_spike and ratio >= self.multiplier:
            self._spiked[bar.symbol] = True
            self._bus.publish(
                VolumeSpike(
                    symbol=bar.symbol,
                    timestamp=bar.timestamp,
                    multiplier=ratio,
                    window_seconds=self.window,
                )
            )
        elif in_spike and ratio <= self.cooldown_multiplier:
            self._spiked[bar.symbol] = False
        history.append(bar.volume)
