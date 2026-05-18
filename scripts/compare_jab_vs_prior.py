"""jab filter 적용 backtest (vb_compare_20260518_jab) vs
volume_filter 만 (vb_compare_20260518_volfilter) PnL 비교."""
from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd

PRIOR = Path("data/reports/vb_compare_20260518_volfilter")  # volume filter 단독
NEW = Path("data/reports/vb_compare_20260518_jab")          # + jab filter


def _load(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, dtype={"symbol": str})
    df["symbol"] = df["symbol"].str.zfill(6)
    return df


def _stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0, "win_pct": 0, "pnl": 0, "mdd": 0, "avg_hold": 0, "mean_pnl": 0}
    wins = (df["pnl_krw"] > 0).sum()
    pnl = int(df["pnl_krw"].sum())
    df_sorted = df.sort_values("entry_ts")
    cum = df_sorted["pnl_krw"].cumsum()
    peak = cum.cummax()
    mdd = int((cum - peak).min())
    return {
        "n": len(df), "win_pct": wins / len(df) * 100, "pnl": pnl, "mdd": mdd,
        "avg_hold": float(df["hold_minutes"].mean()),
        "mean_pnl": int(df["pnl_krw"].mean()),
    }


def main() -> int:
    print(f"PRIOR (volume filter 단독): {PRIOR}")
    print(f"NEW   (+ jab filter): {NEW}\n")
    modes = [("V1", "v1_trades.csv"), ("V2", "v2_trades.csv"),
             ("V3", "v3_trades.csv"), ("V4", "v4_trades.csv")]
    lines: list[str] = []
    lines.append("# Jab Filter Effect — volume filter 만 vs + jab filter\n")
    lines.append("| Mode | 지표 | volume only | +jab | Δ |")
    lines.append("|---|---|---:|---:|---:|")
    for mode_name, csv in modes:
        p = _stats(_load(PRIOR / csv))
        n = _stats(_load(NEW / csv))
        def f(x): return f"{x:+,.0f}" if isinstance(x, (int, float)) else str(x)
        lines.append(f"| {mode_name} | 거래수 | {p['n']:,} | {n['n']:,} | {n['n']-p['n']:+,} |")
        lines.append(f"| | 승률 | {p['win_pct']:.2f}% | {n['win_pct']:.2f}% | {n['win_pct']-p['win_pct']:+.2f}%p |")
        lines.append(f"| | 합산 PnL | {f(p['pnl'])} | {f(n['pnl'])} | {f(n['pnl']-p['pnl'])} |")
        lines.append(f"| | 평균 PnL | {f(p['mean_pnl'])} | {f(n['mean_pnl'])} | {f(n['mean_pnl']-p['mean_pnl'])} |")
        lines.append(f"| | MDD | {f(p['mdd'])} | {f(n['mdd'])} | {f(n['mdd']-p['mdd'])} |")
        lines.append(f"| | 평균 보유 (분) | {p['avg_hold']:.1f} | {n['avg_hold']:.1f} | {n['avg_hold']-p['avg_hold']:+.1f} |")
    lines.append("")
    out = "\n".join(lines)
    out_path = NEW / "comparison_with_volfilter_only.md"
    out_path.write_text(out, encoding="utf-8")
    print(out)
    print(f"\n[saved] → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
