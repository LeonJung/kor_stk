"""VolumeProfile / POC — 가격대별 누적 거래량 + Point of Control.

technical_strategy.md §J3 (자리 / 차트 패턴 — 매물대):
"가격대별 누적 거래량 = POC (Point of Control). 매물 많은 자리 = 지지/저항.
매물 소화 후 돌파 = 매수 시점."

ks_ws tick capture (data/ticks.sqlite 5/12-5/13 약 6M tick) 와 연결해서 종목별
매물대 자동 합성. KIS 의 historical orderbook 부재 (think.md 2026-05-12) 의
우회 = 자체 tick 누적 → volume profile 재구성.

API:
- VolumeProfile — per-symbol 매물대 누적 (price bucket → volume).
- bucket_size = 가격 단위 (default 100원, KOSDAQ 호가단위 고려).
- feed(price, volume): tick 단위 누적.
- compute_poc(): 최대 volume bucket price.
- top_n_buckets(n): volume desc top N.
- value_area(coverage_pct=70): POC 부근 N% volume 차지 가격대 (지지/저항 범위).
- bus 통합 = VolumeProfileTracker — Tick.feed → 자동 누적, query 가능.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ks_ws.domain import Tick


@dataclass
class VolumeProfile:
    """Per-symbol volume profile. Price bucket = floor(price / bucket_size) * bucket_size."""

    bucket_size: int = 100  # KRW per bucket
    _buckets: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def __post_init__(self) -> None:
        if self.bucket_size <= 0:
            raise ValueError("bucket_size must be positive")

    def feed(self, price: int, volume: int) -> None:
        if price < 0 or volume < 0:
            raise ValueError("price and volume must be non-negative")
        bucket = (price // self.bucket_size) * self.bucket_size
        self._buckets[bucket] += volume

    def total_volume(self) -> int:
        return sum(self._buckets.values())

    def bucket_volume(self, price: int) -> int:
        bucket = (price // self.bucket_size) * self.bucket_size
        return self._buckets.get(bucket, 0)

    def poc(self) -> int | None:
        """Point of Control — bucket with max volume. None if empty."""
        if not self._buckets:
            return None
        return max(self._buckets, key=lambda b: self._buckets[b])

    def top_n_buckets(self, n: int) -> list[tuple[int, int]]:
        """Top N buckets by volume, sorted desc. List of (price, volume)."""
        if n <= 0:
            raise ValueError("n must be positive")
        return sorted(self._buckets.items(), key=lambda kv: -kv[1])[:n]

    def value_area(self, *, coverage_pct: float = 70.0) -> tuple[int, int] | None:
        """Return (low_price, high_price) bounding ``coverage_pct``% of total
        volume centered around POC. None if profile empty.

        Standard market-profile value area: expand outward from POC, adding
        the higher-volume neighbor each step until coverage reached.
        """
        if not 0 < coverage_pct <= 100:
            raise ValueError("coverage_pct must be in (0, 100]")
        poc = self.poc()
        if poc is None:
            return None
        total = self.total_volume()
        target = total * coverage_pct / 100
        included_vol = self._buckets[poc]
        low = high = poc
        while included_vol < target:
            below = low - self.bucket_size
            above = high + self.bucket_size
            v_below = self._buckets.get(below, 0)
            v_above = self._buckets.get(above, 0)
            if v_below == 0 and v_above == 0:
                # Try widening further to find any neighbor with volume; if
                # nothing exists, we've covered all the volume there is.
                step = self.bucket_size
                while step <= max(poc, max(self._buckets) - poc) * 2:
                    v_below = self._buckets.get(low - step, 0)
                    v_above = self._buckets.get(high + step, 0)
                    if v_below or v_above:
                        if v_below >= v_above:
                            low -= step
                            included_vol += v_below
                        else:
                            high += step
                            included_vol += v_above
                        break
                    step += self.bucket_size
                else:
                    break
                continue
            if v_below >= v_above:
                low = below
                included_vol += v_below
            else:
                high = above
                included_vol += v_above
        return (low, high)


class VolumeProfileTracker:
    """Per-symbol VolumeProfile container. Feed Ticks (or raw price+volume),
    query per-symbol profile / POC."""

    def __init__(self, *, bucket_size: int = 100) -> None:
        self.bucket_size = bucket_size
        self._profiles: dict[str, VolumeProfile] = {}

    def feed_tick(self, tick: Tick) -> None:
        if tick.volume <= 0:
            return  # snapshot ticks (WARM tier) have volume=0 — skip
        prof = self._profiles.setdefault(
            tick.symbol, VolumeProfile(bucket_size=self.bucket_size)
        )
        prof.feed(tick.price, tick.volume)

    def get(self, symbol: str) -> VolumeProfile | None:
        return self._profiles.get(symbol)

    def all_symbols(self) -> list[str]:
        return list(self._profiles)
