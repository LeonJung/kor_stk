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

    is_sideways = (
        abs(distance_from_avg_pct) <= sideways_band_pct
        and abs(short_change_pct) <= sideways_band_pct
    )
    if distance_from_avg_pct > 0:
        if short_change_pct >= strong_uptrend_pct:
            return "strong_uptrend"
        if is_sideways:
            return "sideways"
        return "uptrend"
    # below average
    if short_change_pct <= downtrend_pct:
        return "downtrend"
    if is_sideways:
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
        # Cap retained history at 2x long_window for memory efficiency
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


# --- v3 (2026-05-13) — fundamental_strategy.md §E + Pattern 7 보강 ---


_REGIME_BASE_SCORE = {
    "strong_uptrend": 1.5,
    "uptrend": 1.2,
    "sideways": 1.0,
    "downtrend": 0.5,
    "unknown": 1.0,
}


def _vkospi_score(vkospi: float) -> float:
    """Map VKOSPI level → [0.3, 1.4]. 책 광기 노트 + risk-off threshold 25 기준.

    < 15  → 1.4 (risk-on)
    15-20 → 1.2
    20-25 → 1.0 (neutral)
    25-30 → 0.7
    >= 30 → 0.3 (risk-off, 매수 strategy 비활성 권장)
    """
    if vkospi < 15:
        return 1.4
    if vkospi < 20:
        return 1.2
    if vkospi < 25:
        return 1.0
    if vkospi < 30:
        return 0.7
    return 0.3


def _market_value_score(market_value_krw: int) -> float:
    """Map 시장 전체 일별 거래대금 → [0.6, 1.3]. 책 광기 노트 기준선
    (12조↑ 좋음 / 5조↓ 위축).

    >= 12_000_000_000_000 → 1.3 (활발)
    8조-12조             → 1.1
    5조-8조              → 1.0 (neutral)
    < 5_000_000_000_000   → 0.6 (위축, 자동매매 자제 권장)
    """
    if market_value_krw >= 12_000_000_000_000:
        return 1.3
    if market_value_krw >= 8_000_000_000_000:
        return 1.1
    if market_value_krw >= 5_000_000_000_000:
        return 1.0
    return 0.6


def compute_regime_score(
    bars: Sequence[Bar],
    *,
    vkospi: float | None = None,
    market_value_krw: int | None = None,
    long_window: int = 60,
    short_window: int = 20,
    weight_trend: float = 0.4,
    weight_vkospi: float = 0.3,
    weight_market_value: float = 0.3,
) -> float:
    """Combined market regime score in [0.0, 1.5].

    Combines 3 components — trend (classify_regime 기반), VKOSPI risk-off,
    시장 전체 거래대금 — into one macro_score directly usable as
    FundamentalAllocator.set_macro_score() input. Missing components (None)
    are skipped and weights re-normalized over present components.

    All None → 1.0 (neutral fallback).
    """
    components: list[tuple[float, float]] = []  # (score, weight)

    # Trend (always present, even if "unknown" — score 1.0)
    regime = classify_regime(
        bars, long_window=long_window, short_window=short_window
    )
    components.append((_REGIME_BASE_SCORE[regime], weight_trend))

    if vkospi is not None:
        if vkospi < 0:
            raise ValueError("vkospi must be non-negative")
        components.append((_vkospi_score(vkospi), weight_vkospi))

    if market_value_krw is not None:
        if market_value_krw < 0:
            raise ValueError("market_value_krw must be non-negative")
        components.append((_market_value_score(market_value_krw), weight_market_value))

    wsum = sum(w for _, w in components)
    if wsum <= 0:
        return 1.0
    blended = sum(s * w for s, w in components) / wsum
    return max(0.0, min(1.5, blended))


class MarketRegimeV3:
    """Stateful v3 adapter — feed_bar / set_vkospi / set_market_value 누적 후
    score() 호출. FundamentalAllocator.set_macro_score() 입력 직접 활용.

    v2 의 stateful MarketRegimeDetector 와 별도 — v3 는 점수 [0.0, 1.5] 반환,
    v2 는 string 카테고리 반환. RegimeGate (string) 와 FundamentalAllocator
    (score) 양쪽 모두 지원하려면 둘 다 운용.
    """

    def __init__(
        self,
        *,
        long_window: int = 60,
        short_window: int = 20,
        weight_trend: float = 0.4,
        weight_vkospi: float = 0.3,
        weight_market_value: float = 0.3,
    ) -> None:
        if long_window <= short_window:
            raise ValueError("long_window must exceed short_window")
        self.long_window = long_window
        self.short_window = short_window
        self.weight_trend = weight_trend
        self.weight_vkospi = weight_vkospi
        self.weight_market_value = weight_market_value
        self._bars: list[Bar] = []
        self._vkospi: float | None = None
        self._market_value_krw: int | None = None

    def feed_bar(self, bar: Bar) -> None:
        self._bars.append(bar)
        cap = 2 * self.long_window
        if len(self._bars) > cap:
            self._bars = self._bars[-cap:]

    def set_vkospi(self, value: float) -> None:
        if value < 0:
            raise ValueError("vkospi must be non-negative")
        self._vkospi = value

    def set_market_value_krw(self, value: int) -> None:
        if value < 0:
            raise ValueError("market_value_krw must be non-negative")
        self._market_value_krw = value

    def score(self) -> float:
        return compute_regime_score(
            self._bars,
            vkospi=self._vkospi,
            market_value_krw=self._market_value_krw,
            long_window=self.long_window,
            short_window=self.short_window,
            weight_trend=self.weight_trend,
            weight_vkospi=self.weight_vkospi,
            weight_market_value=self.weight_market_value,
        )
