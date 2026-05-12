"""MarketRegimeDetector — KOSPI 추세/breadth/drawdown 기반 regime classifier.

regime 카테고리 (technical_strategy.md 활성화 매트릭스 기준):
- ``strong_uptrend`` — 60일 이동평균 위 + 최근 20일 +10% 이상 + 강한 폭
- ``uptrend`` — 60일 이동평균 위 + 횡보 아닌 상승
- ``sideways`` — 60일 이동평균 ±3% 안 + 변동성 낮음
- ``downtrend`` — 60일 이동평균 아래 + 최근 20일 -5% 이하

판정에 필요한 입력:
- ``index_bars`` : KOSPI/KOSDAQ 일봉 sequence (최소 60일)
- ``current_drawdown_pct`` : 옵션, 현재 가격 vs 60일 고점 거리 %

본 detector 는 stateless classifier — 매 호출마다 입력 bars 로부터 regime
값을 계산. RegimeGate 의 ``regime_provider`` callable 로 직접 사용 가능.
"""

from collections.abc import Sequence

from ks_ws.domain import Bar


def classify_regime(
    bars: Sequence[Bar],
    *,
    long_window: int = 60,
    short_window: int = 20,
    sideways_band_pct: float = 3.0,
    strong_uptrend_pct: float = 10.0,
    downtrend_pct: float = -5.0,
) -> str:
    """Classify market regime from a sequence of (chronologically sorted)
    Bars (typically KOSPI 일봉).

    Returns one of: ``strong_uptrend`` / ``uptrend`` / ``sideways`` /
    ``downtrend`` / ``unknown``.

    ``unknown`` is returned when there are fewer than ``long_window`` bars.
    Allows RegimeGate to "fail closed" for regimes-aware strategies before
    enough history is loaded.
    """
    if len(bars) < long_window:
        return "unknown"

    long_window_bars = list(bars[-long_window:])
    short_window_bars = list(bars[-short_window:])

    last_close = long_window_bars[-1].close
    long_avg = sum(b.close for b in long_window_bars) / len(long_window_bars)
    short_start = short_window_bars[0].close
    short_change_pct = (last_close - short_start) / short_start * 100

    distance_from_avg_pct = (last_close - long_avg) / long_avg * 100

    if distance_from_avg_pct > 0:
        if short_change_pct >= strong_uptrend_pct:
            return "strong_uptrend"
        if abs(distance_from_avg_pct) <= sideways_band_pct and abs(short_change_pct) <= sideways_band_pct:
            return "sideways"
        return "uptrend"
    # below average
    if short_change_pct <= downtrend_pct:
        return "downtrend"
    if abs(distance_from_avg_pct) <= sideways_band_pct and abs(short_change_pct) <= sideways_band_pct:
        return "sideways"
    return "downtrend"


class MarketRegimeDetector:
    """Stateful adapter — feed bars over time, ask for current regime.

    Useful as a RegimeGate.regime_provider: instantiate, hook ``feed_bar``
    into a Bar subscription on the EventBus (or wherever index bars come
    from), then pass ``detector.current`` (or a lambda wrapping it) to
    RegimeGate.
    """

    def __init__(
        self,
        *,
        long_window: int = 60,
        short_window: int = 20,
        sideways_band_pct: float = 3.0,
    ) -> None:
        if long_window <= short_window:
            raise ValueError("long_window must exceed short_window")
        self.long_window = long_window
        self.short_window = short_window
        self.sideways_band_pct = sideways_band_pct
        self._bars: list[Bar] = []

    def feed_bar(self, bar: Bar) -> None:
        self._bars.append(bar)
        # Cap retained history at 2× long_window for memory efficiency
        cap = 2 * self.long_window
        if len(self._bars) > cap:
            self._bars = self._bars[-cap:]

    def current(self) -> str:
        return classify_regime(
            self._bars,
            long_window=self.long_window,
            short_window=self.short_window,
            sideways_band_pct=self.sideways_band_pct,
        )

    @property
    def bars_loaded(self) -> int:
        return len(self._bars)
