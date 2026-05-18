"""entry_gate (ABC: MTF + KOSPI regime + 시간대) 적용 전후 PnL/승률 비교.
1주 기준 (pnl_krw 컬럼 그대로)."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

PRIOR = Path("data/reports/vb_compare_20260518_jab")          # ABC 적용 전 (volume + jab)
NEW = Path("data/reports/vb_compare_20260518_entrygate")      # ABC 적용 후


def _load(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"symbol": str})


def _stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0, "win_pct": 0, "pnl": 0, "mdd": 0,
                "avg_hold": 0, "mean_pnl": 0}
    wins = (df["pnl_krw"] > 0).sum()
    pnl = int(df["pnl_krw"].sum())
    df_s = df.sort_values("entry_ts")
    cum = df_s["pnl_krw"].cumsum()
    mdd = int((cum - cum.cummax()).min())
    return {
        "n": len(df), "win_pct": wins / len(df) * 100,
        "pnl": pnl, "mdd": mdd,
        "avg_hold": float(df["hold_minutes"].mean()),
        "mean_pnl": int(df["pnl_krw"].mean()),
    }


def main() -> int:
    print(f"PRIOR (ABC 적용 전, volume+jab): {PRIOR}")
    print(f"NEW   (+ entry_gate ABC): {NEW}\n")
    modes = [("V1", "v1_trades.csv"), ("V2", "v2_trades.csv"),
             ("V3", "v3_trades.csv"), ("V4", "v4_trades.csv")]
    lines = []
    lines.append("# Entry Gate ABC (MTF + KOSPI regime + 시간대) 효과 비교\n")
    lines.append("(1주 기준 PnL)\n")
    lines.append("| Mode | 지표 | ABC 적용 전 | ABC 적용 | Δ |")
    lines.append("|---|---|---:|---:|---:|")
    for mode, csv in modes:
        p = _stats(_load(PRIOR / csv))
        n = _stats(_load(NEW / csv))
        def f(x):
            return f"{x:+,.0f}" if isinstance(x, (int, float)) else str(x)
        lines.append(f"| {mode} | 거래수 | {p['n']:,} | {n['n']:,} | {n['n']-p['n']:+,} |")
        lines.append(f"| | **승률** | **{p['win_pct']:.2f}%** | **{n['win_pct']:.2f}%** | **{n['win_pct']-p['win_pct']:+.2f}%p** |")
        lines.append(f"| | 합산 PnL | {f(p['pnl'])} | {f(n['pnl'])} | {f(n['pnl']-p['pnl'])} |")
        lines.append(f"| | 평균 PnL | {f(p['mean_pnl'])} | {f(n['mean_pnl'])} | {f(n['mean_pnl']-p['mean_pnl'])} |")
        lines.append(f"| | MDD | {f(p['mdd'])} | {f(n['mdd'])} | {f(n['mdd']-p['mdd'])} |")
        lines.append(f"| | 평균 보유 (분) | {p['avg_hold']:.1f} | {n['avg_hold']:.1f} | {n['avg_hold']-p['avg_hold']:+.1f} |")
    out = "\n".join(lines)
    (NEW / "comparison_with_jab.md").write_text(out, encoding="utf-8")
    print(out)
    print(f"\n[saved] → {NEW}/comparison_with_jab.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
