"""measure_factor_ic — 7 factor 의 forward return 예측력 측정.

사용자 룰 (2026-05-15): 합성 점수의 weight 결정 전, 각 factor 가 실제로
예측력 (IC) 있는지 검증. overfitting 회피 = train_only (2025-08 cutoff).

측정 factor:
1. foreign_5d: 외인 5일 누적 순매수 (KRW)
2. value_rvol: 5일 거래대금 / 20일 거래대금 평균 (RVOL)
3. ma5_over_ma20: 5일선 / 20일선 비율 (>1 = 정배열)
4. near_60d_high: (현재가 / 60일 high) 비율
5. consec_green: 최근 5일 양봉 수 (0-5)
6. atr_pct_band: ATR % 가 1-4% 적정대 (1=in band, 0=out)
7. value_spike_1d: 어제 거래대금 / 5일 평균

Forward return: 다음 5 영업일 close-to-close 수익률.

출력: 각 factor 의 Mean IC / ICIR / Hit rate / 의미 (significant 여부)
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ks_ws.research.factor_ic import (
    TEST_CUTOFF,
    compute_factor_ic,
)
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("measure_factor_ic")


def _load_bars_by_symbol(bar_store, codes, days):
    """{symbol: list[Bar]} sorted by timestamp."""
    out = {}
    for sym in codes:
        bars = list(bar_store.read(sym, "1d"))
        if len(bars) >= 20:
            out[sym] = bars[-days:] if len(bars) > days else bars
    return out


def _load_foreign_flow_by_symbol(db_path="data/foreign_flow.sqlite"):
    """{symbol: {date: net_buy_krw}}."""
    import sqlite3
    if not Path(db_path).exists():
        return {}
    conn = sqlite3.connect(db_path)
    out: dict = defaultdict(dict)
    try:
        for sym, dt_str, net in conn.execute(
            "SELECT symbol, date, net_buy_krw FROM foreign_flow"
        ).fetchall():
            try:
                d = datetime.strptime(dt_str, "%Y%m%d").date()
                out[sym][d] = int(net)
            except ValueError:
                continue
    finally:
        conn.close()
    return dict(out)


def _bars_to_day_close(bars):
    """list[Bar] → {date: close}."""
    return {b.timestamp.astimezone(UTC).date(): b.close for b in bars}


def _bars_to_day_value(bars):
    return {b.timestamp.astimezone(UTC).date(): b.value for b in bars}


def _bars_to_day_high_low(bars):
    return {b.timestamp.astimezone(UTC).date(): (b.high, b.low, b.close, b.open)
            for b in bars}


def compute_factors(
    bars_by_sym: dict, foreign_by_sym: dict, forward_days: int = 5,
) -> tuple[dict, dict, dict]:
    """Return (factors_by_day_sym dict of dicts, return_by_day_sym, n_total_days)."""
    # First pass: 각 종목별로 daily factor + forward return 계산
    # 그 다음 day-indexed transposed.

    factor_names = ["foreign_5d", "value_rvol", "ma5_over_ma20",
                    "near_60d_high", "consec_green", "atr_pct_band",
                    "value_spike_1d"]
    factors_day: dict = {name: defaultdict(dict) for name in factor_names}
    returns_day: dict = defaultdict(dict)

    for sym, bars in bars_by_sym.items():
        if len(bars) < 60:
            continue
        ff = foreign_by_sym.get(sym, {})
        # date-indexed
        sorted_bars = sorted(bars, key=lambda b: b.timestamp)
        for i in range(60, len(sorted_bars) - forward_days):
            today_bar = sorted_bars[i]
            today_date = today_bar.timestamp.astimezone(UTC).date()
            forward_bar = sorted_bars[i + forward_days]
            if today_bar.close <= 0:
                continue
            forward_return = (forward_bar.close - today_bar.close) / today_bar.close
            returns_day[today_date][sym] = forward_return

            # F1: foreign_5d (sum of last 5 calendar days)
            cutoff = today_date - timedelta(days=7)
            f5d = sum(
                v for d, v in ff.items() if cutoff <= d <= today_date
            ) if ff else 0.0
            factors_day["foreign_5d"][today_date][sym] = float(f5d)

            # F2: value_rvol = avg(value, last 5) / avg(value, last 20)
            v5 = sum(b.value for b in sorted_bars[i-5:i]) / 5
            v20 = sum(b.value for b in sorted_bars[i-20:i]) / 20
            rvol = v5 / v20 if v20 > 0 else 1.0
            factors_day["value_rvol"][today_date][sym] = rvol

            # F3: ma5_over_ma20
            ma5 = sum(b.close for b in sorted_bars[i-5:i]) / 5
            ma20 = sum(b.close for b in sorted_bars[i-20:i]) / 20
            factors_day["ma5_over_ma20"][today_date][sym] = (
                ma5 / ma20 if ma20 > 0 else 1.0
            )

            # F4: near_60d_high = current / max(close, last 60)
            h60 = max(b.close for b in sorted_bars[i-60:i])
            factors_day["near_60d_high"][today_date][sym] = (
                today_bar.close / h60 if h60 > 0 else 1.0
            )

            # F5: consec_green (last 5)
            consec = sum(1 for b in sorted_bars[i-5:i] if b.close > b.open)
            factors_day["consec_green"][today_date][sym] = float(consec)

            # F6: atr_pct_band (1-4% in band = 1, else 0)
            trs = []
            for j in range(i - 14, i):
                if j > 0:
                    pc = sorted_bars[j-1].close
                    h = sorted_bars[j].high
                    l = sorted_bars[j].low
                    trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            if trs and today_bar.close > 0:
                atr = sum(trs) / len(trs)
                atr_pct = atr / today_bar.close * 100
                in_band = 1.0 if 1.0 <= atr_pct <= 4.0 else 0.0
                factors_day["atr_pct_band"][today_date][sym] = in_band

            # F7: value_spike_1d = yesterday value / 5d avg
            y_value = sorted_bars[i-1].value
            v5_prior = sum(b.value for b in sorted_bars[i-6:i-1]) / 5
            spike = y_value / v5_prior if v5_prior > 0 else 1.0
            factors_day["value_spike_1d"][today_date][sym] = spike

    return factors_day, dict(returns_day), len(returns_day)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=500,
                   help="universe size (default 500, KOSPI top)")
    p.add_argument("--days", type=int, default=700, help="lookback days")
    p.add_argument("--forward-days", type=int, default=5,
                   help="forward return horizon (default 5 영업일)")
    p.add_argument("--include-test", action="store_true",
                   help="test 기간 (2025-08+) 포함 (debug only)")
    p.add_argument("--min-symbols-per-day", type=int, default=30)
    args = p.parse_args()

    bar_store = BarStore("data")
    reg = UniverseRegistry("data/universe.sqlite")
    universe = reg.top_by_market_cap(args.top)
    codes = [e.code for e in universe]
    reg.close()
    log.info("universe: %d codes", len(codes))

    bars_by_sym = _load_bars_by_symbol(bar_store, codes, args.days)
    log.info("loaded bars for %d symbols (≥20 bars)", len(bars_by_sym))

    foreign_by_sym = _load_foreign_flow_by_symbol()
    log.info("loaded foreign flow for %d symbols", len(foreign_by_sym))

    log.info("computing 7 factors + forward return (horizon=%d days) ...",
             args.forward_days)
    factors_day, returns_day, n_days = compute_factors(
        bars_by_sym, foreign_by_sym, forward_days=args.forward_days,
    )
    log.info("processed %d trading days", n_days)

    train_only = not args.include_test
    log.info("train_only=%s (cutoff=%s)", train_only, TEST_CUTOFF)
    print()
    print(f"{'sig':>3} {'factor':30} {'mean_IC':>9} {'ICIR':>8} "
          f"{'hit%':>5} {'n_days':>7}")
    print("-" * 75)
    results = []
    for name in ["foreign_5d", "value_rvol", "ma5_over_ma20", "near_60d_high",
                 "consec_green", "atr_pct_band", "value_spike_1d"]:
        res = compute_factor_ic(
            name,
            factor_by_day_sym=factors_day[name],
            return_by_day_sym=returns_day,
            min_symbols_per_day=args.min_symbols_per_day,
            train_only=train_only,
        )
        results.append(res)
        sig = "✓" if res.significant else " "
        print(f"  {sig} {res.factor_name:30} {res.mean_ic:>+9.4f} "
              f"{res.icir:>+8.2f} {res.hit_rate*100:>4.0f}% {res.n_days:>7}")

    print()
    sig_count = sum(1 for r in results if r.significant)
    print(f"=> {sig_count}/{len(results)} factors significant "
          f"(|mean_IC|>0.02 + |ICIR|>0.5)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
