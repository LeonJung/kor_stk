"""A only (BC 적용 전) vs A+B+C (BC 적용 후) PnL 비교."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

JAB = Path("data/reports/vb_compare_20260518_jab")              # A/B/C 없음
A_ONLY = Path("data/reports/vb_compare_20260518_a_only")        # A only
ABC = Path("data/reports/vb_compare_20260518_entrygate")        # A+B+C


def _load(p): return pd.read_csv(p, dtype={"symbol": str}) if p.exists() else pd.DataFrame()
def _stats(df):
    if df.empty: return {"n": 0, "win_pct": 0, "pnl": 0, "mdd": 0, "mean_pnl": 0, "avg_hold": 0}
    wins = (df["pnl_krw"] > 0).sum()
    pnl = int(df["pnl_krw"].sum())
    df_s = df.sort_values("entry_ts")
    cum = df_s["pnl_krw"].cumsum()
    mdd = int((cum - cum.cummax()).min())
    return {"n": len(df), "win_pct": wins/len(df)*100, "pnl": pnl, "mdd": mdd,
            "mean_pnl": int(df["pnl_krw"].mean()), "avg_hold": float(df["hold_minutes"].mean())}


def main():
    print(f"JAB   (필터 없음): {JAB}")
    print(f"A only (시간대만): {A_ONLY}")
    print(f"A+B+C (다 적용): {ABC}\n")
    out = ["# A only vs A+B+C — BC 효과 분리\n",
           "1주 기준 PnL\n",
           "| Mode | 지표 | JAB (필터 X) | A only | A+B+C | A→ABC Δ |",
           "|---|---|---:|---:|---:|---:|"]
    for m, csv in [("V1","v1_trades.csv"),("V2","v2_trades.csv"),
                    ("V3","v3_trades.csv"),("V4","v4_trades.csv")]:
        j = _stats(_load(JAB/csv))
        a = _stats(_load(A_ONLY/csv))
        b = _stats(_load(ABC/csv))
        def f(x): return f"{x:+,.0f}" if isinstance(x,(int,float)) else str(x)
        out.append(f"| {m} | 거래수 | {j['n']:,} | {a['n']:,} | {b['n']:,} | {b['n']-a['n']:+,} |")
        out.append(f"| | **승률** | {j['win_pct']:.2f}% | **{a['win_pct']:.2f}%** | **{b['win_pct']:.2f}%** | **{b['win_pct']-a['win_pct']:+.2f}%p** |")
        out.append(f"| | PnL | {f(j['pnl'])} | {f(a['pnl'])} | {f(b['pnl'])} | {f(b['pnl']-a['pnl'])} |")
        out.append(f"| | 평균 PnL | {f(j['mean_pnl'])} | {f(a['mean_pnl'])} | {f(b['mean_pnl'])} | {f(b['mean_pnl']-a['mean_pnl'])} |")
        out.append(f"| | MDD | {f(j['mdd'])} | {f(a['mdd'])} | {f(b['mdd'])} | {f(b['mdd']-a['mdd'])} |")
        out.append(f"| | 평균 보유 (분) | {j['avg_hold']:.1f} | {a['avg_hold']:.1f} | {b['avg_hold']:.1f} | {b['avg_hold']-a['avg_hold']:+.1f} |")
    s = "\n".join(out)
    (A_ONLY / "comparison_a_only_vs_abc.md").write_text(s, encoding="utf-8")
    print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
