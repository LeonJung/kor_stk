"""GapUpDetector — emits a GapUp event when a Bar opens with a gap
above the configured percentage versus the previous trading day's
closing price.

The detector takes a sequence of daily-bar prints (typically the first
bar each trading day fed in chronological order) and tracks per-symbol
prior-close. A bar is reported as a gap-up only if the prior close is
known and the open exceeds that close by the threshold.

Caller responsibility: feed only the relevant timeframe (typically
"1d" first bar of the session). Feeding minute bars indiscriminately
would compare every bar's open to the previous bar's close and flood
the bus.
"""

from ks_ws.bus import EventBus
from ks_ws.domain import Bar
from ks_ws.events import GapUp


class GapUpDetector:
    def __init__(
        self,
        bus: EventBus,
        *,
        gap_pct_threshold: float = 3.0,
    ) -> None:
        if gap_pct_threshold <= 0:
            raise ValueError("gap_pct_threshold must be positive")
        self._bus = bus
        self.gap_pct_threshold = gap_pct_threshold
        self._prev_close: dict[str, int] = {}

    def feed(self, bar: Bar) -> None:
        prev = self._prev_close.get(bar.symbol)
        # Update the running prev-close before deciding so subsequent bars
        # always use the latest known close from this stream.
        self._prev_close[bar.symbol] = bar.close
        if prev is None or prev <= 0:
            return
        gap_pct = (bar.open - prev) / prev * 100
        if gap_pct >= self.gap_pct_threshold:
            self._bus.publish(
                GapUp(
                    symbol=bar.symbol,
                    timestamp=bar.timestamp,
                    gap_pct=gap_pct,
                )
            )
