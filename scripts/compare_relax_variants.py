"""B/C 완화안 vs strict 비교 (V2 중심)."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

VARIANTS = [
    ("A only",                  Path("data/reports/vb_compare_20260518_a_only")),
    ("A+B (strict)",            Path("data/reports/vb_compare_20260518_ab")),
    ("A+B (lite: MA20)",        Path("data/reports/vb_compare_20260518_ab_lite")),
    ("A+C (strict)",            Path("data/reports/vb_compare_20260518_ac")),
    ("A+C (lite: ATR off)",     Path("data/reports/vb_compare_20260518_ac_lite")),
    ("A+C (soft: MA20+ATRoff)", Path("data/reports/vb_compare_20260518_ac_soft")),
    ("A+B+C (strict)",          Path("data/reports/vb_compare_20260518_entrygate")),
]


def _load(p): return pd.read_csv(p, dtype={"symbol": str}) if p.exists() else pd.DataFrame()
def _stats(df):
    if df.empty: return {"n": 0, "win": 0, "pnl": 0, "mdd": 0, "mean": 0, "hold": 0}
    wins = (df["pnl_krw"] > 0).sum()
    pnl = int(df["pnl_krw"].sum())
    df_s = df.sort_values("entry_ts")
    cum = df_s["pnl_krw"].cumsum()
    mdd = int((cum - cum.cummax()).min())
    return {"n": len(df), "win": wins/len(df)*100, "pnl": pnl, "mdd": mdd,
            "mean": int(df["pnl_krw"].mean()), "hold": float(df["hold_minutes"].mean())}


def main():
    f = lambda x: f"{x:+,.0f}" if isinstance(x,(int,float)) else str(x)
    out = ["# B/C 완화안 비교 (V2 중심)\n",
           "1주 기준 PnL\n",
           "| 변형 | n | 승률 | PnL | 평균 | MDD | 보유(분) |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for mode_name, csv in [("V1","v1_trades.csv"),("V2","v2_trades.csv"),
                             ("V3","v3_trades.csv"),("V4","v4_trades.csv")]:
        out.append(f"")
        out.append(f"## {mode_name}\n")
        out.append("| 변형 | n | 승률 | PnL | 평균 | MDD | 보유(분) |")
        out.append("|---|---:|---:|---:|---:|---:|---:|")
        print(f"\n## {mode_name}")
        print(f"{'변형':<28}{'n':>6}{'승률':>8}{'PnL':>12}{'평균':>10}{'MDD':>12}{'보유':>8}")
        for label, d in VARIANTS:
            s = _stats(_load(d / csv))
            if s["n"] == 0: continue
            print(f"  {label:<26}{s['n']:>6,}{s['win']:>7.1f}%{f(s['pnl']):>12}{f(s['mean']):>10}{f(s['mdd']):>12}{s['hold']:>8.0f}")
            out.append(f"| {label} | {s['n']:,} | {s['win']:.1f}% | "
                       f"{f(s['pnl'])} | {f(s['mean'])} | {f(s['mdd'])} | {s['hold']:.0f} |")
    Path("data/reports/vb_compare_20260518_relax_variants.md").write_text(
        "\n".join(out), encoding="utf-8")
    print("\n[saved] → data/reports/vb_compare_20260518_relax_variants.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
