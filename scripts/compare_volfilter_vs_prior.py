"""기존 vb_compare_20260517_4way 결과 vs 새 vb_compare_20260518_volfilter 결과 비교.

핵심 차이:
- 기존: 백업 X (fetch 진행 전), volume_filter X
- 새 거: 백업 데이터 + volume_filter standard (turnover≥500ppm, val_mcap≥100ppm, RVOL≥2)

표 = V1/V2/V3/V4 각각 prior vs new PnL/승률/MDD/trade 수 비교.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

import pandas as pd

PRIOR = Path("data/reports/vb_compare_20260517_4way")
NEW = Path("data/reports/vb_compare_20260518_volfilter")


def _load(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, dtype={"symbol": str})
    df["symbol"] = df["symbol"].str.zfill(6)
    return df


def _stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0, "win_pct": 0, "pnl": 0, "mdd": 0, "avg_hold": 0}
    wins = (df["pnl_krw"] > 0).sum()
    pnl = int(df["pnl_krw"].sum())
    df_sorted = df.sort_values("entry_ts")
    cum = df_sorted["pnl_krw"].cumsum()
    peak = cum.cummax()
    mdd = int((cum - peak).min())
    return {
        "n": len(df),
        "win_pct": wins / len(df) * 100,
        "pnl": pnl,
        "mdd": mdd,
        "avg_hold": float(df["hold_minutes"].mean()),
        "mean_pnl": int(df["pnl_krw"].mean()),
    }


def main() -> int:
    print(f"PRIOR (5/17 no volume filter): {PRIOR}")
    print(f"NEW   (5/18 volume filter standard): {NEW}")
    print()

    modes = [
        ("V1", "v1_trades.csv"),
        ("V2", "v2_trades.csv"),
        ("V3", "v3_trades.csv"),
        ("V4", "v4_trades.csv"),
    ]
    lines: list[str] = []
    lines.append("# Volume Filter Effect — Prior (5/17) vs New (5/18 backup + volfilter)\n")
    lines.append("| Mode | 지표 | Prior | New | Δ |")
    lines.append("|---|---|---:|---:|---:|")

    for mode_name, csv in modes:
        prior_df = _load(PRIOR / csv)
        new_df = _load(NEW / csv)
        p = _stats(prior_df)
        n = _stats(new_df)

        def fmt(x, fmt_str="{:+,.0f}"):
            return fmt_str.format(x) if isinstance(x, (int, float)) else str(x)

        lines.append(
            f"| {mode_name} | 거래수 | {p['n']:,} | {n['n']:,} | "
            f"{n['n'] - p['n']:+,} |"
        )
        lines.append(
            f"| | 승률 | {p['win_pct']:.2f}% | {n['win_pct']:.2f}% | "
            f"{n['win_pct'] - p['win_pct']:+.2f}%p |"
        )
        lines.append(
            f"| | 합산 PnL | {fmt(p['pnl'])} | {fmt(n['pnl'])} | "
            f"{fmt(n['pnl'] - p['pnl'])} |"
        )
        lines.append(
            f"| | 평균 PnL | {fmt(p['mean_pnl'])} | {fmt(n['mean_pnl'])} | "
            f"{fmt(n['mean_pnl'] - p['mean_pnl'])} |"
        )
        lines.append(
            f"| | MDD | {fmt(p['mdd'])} | {fmt(n['mdd'])} | "
            f"{fmt(n['mdd'] - p['mdd'])} |"
        )
        lines.append(
            f"| | 평균 보유 (분) | {p['avg_hold']:.1f} | {n['avg_hold']:.1f} | "
            f"{n['avg_hold'] - p['avg_hold']:+.1f} |"
        )

    lines.append("")
    lines.append("## 해석 가이드")
    lines.append("- **거래수 ↓ + 승률 ↑** = volume filter 가 fake breakout 거름 (이상적)")
    lines.append("- **거래수 ↓ + PnL 비례 ↑** = trade 당 효율 ↑")
    lines.append("- **승률 그대로 + 거래수만 ↓** = filter 가 entry 일관성 유지하며 보수화")
    lines.append("- **승률 ↓** = filter 가 wrong signal 자름 (재조정 필요)")

    out = "\n".join(lines)
    out_path = NEW / "comparison_with_prior.md"
    out_path.write_text(out, encoding="utf-8")
    print(out)
    print(f"\n[saved] → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
