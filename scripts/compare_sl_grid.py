"""SL grid (1.0/1.5/2.0/2.5/3.0%) backtest 비교 — V2 + A+C strict."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

AMOUNT = 100_000_000  # 1억/회

VARIANTS = [
    ("SL 1.0%", Path("data/reports/vb_compare_20260518_ac_sl_10")),
    ("SL 1.5%", Path("data/reports/vb_compare_20260518_ac_sl_15")),
    ("SL 2.0%", Path("data/reports/vb_compare_20260518_ac_sl_20")),
    ("SL 2.5%", Path("data/reports/vb_compare_20260518_ac_sl_25")),
    ("SL 3.0%", Path("data/reports/vb_compare_20260518_ac_sl_30")),
]


def _stats(path):
    if not path.exists(): return None
    df = pd.read_csv(path, dtype={"symbol": str})
    if df.empty: return None
    df["qty"] = (AMOUNT // df["entry_price"]).astype(int)
    df["pnl_1억"] = (df["exit_price"] - df["entry_price"]) * df["qty"]
    wins = (df["pnl_1억"] > 0).sum()
    pnl = int(df["pnl_1억"].sum())
    df_s = df.sort_values("entry_ts")
    cum = df_s["pnl_1억"].cumsum()
    mdd = int((cum - cum.cummax()).min())
    avg_w = df[df["pnl_1억"] > 0]["pnl_1억"].mean() if wins > 0 else 0
    avg_l = df[df["pnl_1억"] < 0]["pnl_1억"].mean() if (df["pnl_1억"] < 0).any() else 0
    return {"n": len(df), "win": wins/len(df)*100, "pnl": pnl, "mdd": mdd,
            "avg_w": int(avg_w), "avg_l": int(avg_l),
            "mean": int(df["pnl_1억"].mean()), "hold": float(df["hold_minutes"].mean())}


def main():
    out = ["# SL grid 비교 — V2 + A+C strict (1억/회 매수)\n"]
    out.append("| 변형 | n | 승률 | PnL | 평균/회 | avg win | avg loss | R/R | MDD | 보유(분) |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    f = lambda x: f"{x:+,.0f}" if isinstance(x,(int,float)) else str(x)
    print(f"{'변형':<10}{'n':>5}{'승률':>8}{'PnL':>16}{'avg_w':>12}{'avg_l':>12}{'R/R':>7}{'MDD':>16}")
    print("-" * 90)
    for label, d in VARIANTS:
        s = _stats(d / "v2_trades.csv")
        if not s:
            print(f"  {label}: pending")
            continue
        rr = abs(s["avg_w"] / s["avg_l"]) if s["avg_l"] else 0
        print(f"  {label:<10}{s['n']:>5}{s['win']:>7.1f}%{f(s['pnl']):>16}{f(s['avg_w']):>12}{f(s['avg_l']):>12}{rr:>7.2f}{f(s['mdd']):>16}")
        out.append(f"| {label} | {s['n']:,} | {s['win']:.1f}% | {f(s['pnl'])} | "
                   f"{f(s['mean'])} | {f(s['avg_w'])} | {f(s['avg_l'])} | "
                   f"{rr:.2f} | {f(s['mdd'])} | {s['hold']:.0f} |")
    Path("data/reports/vb_compare_20260518_sl_grid.md").write_text(
        "\n".join(out), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
