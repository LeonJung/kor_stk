"""100만원/회 기준 PnL 재계산 — 기존 backtest CSV 의 pnl 을 한 주 → floor(1M/entry) 주.

사용:
    PYTHONPATH=src .venv/bin/python -m scripts.repnl_per_trade_amount \
        --src data/reports/vb_compare_20260518_jab \
        --amount 1000000
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd


def _stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0, "win_pct": 0, "pnl": 0, "mdd": 0,
                "avg_hold": 0, "mean_pnl": 0}
    wins = (df["pnl_krw"] > 0).sum()
    pnl = int(df["pnl_krw"].sum())
    df_sorted = df.sort_values("entry_ts")
    cum = df_sorted["pnl_krw"].cumsum()
    peak = cum.cummax()
    mdd = int((cum - peak).min())
    return {
        "n": len(df), "win_pct": wins / len(df) * 100,
        "pnl": pnl, "mdd": mdd,
        "avg_hold": float(df["hold_minutes"].mean()),
        "mean_pnl": int(df["pnl_krw"].mean()),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src", required=True,
                   help="backtest 결과 디렉토리 (v1/v2/v3/v4_trades.csv)")
    p.add_argument("--amount", type=int, default=1_000_000,
                   help="한 trade 당 매수 금액 KRW (default 1,000,000)")
    args = p.parse_args()

    src = Path(args.src)
    print(f"src={src}, amount={args.amount:,}원/회\n")

    out_lines: list[str] = []
    out_lines.append(f"# 한 trade {args.amount:,}원 매수 기준 PnL 재계산\n")
    out_lines.append(f"방식: qty = floor({args.amount:,} / entry_price), "
                     f"PnL = (exit - entry) × qty\n")
    out_lines.append("| Mode | 지표 | 한주 기준 (기존) | "
                     f"{args.amount//10000}만원 기준 (재계산) | 배율 |")
    out_lines.append("|---|---|---:|---:|---:|")

    print(f"{'Mode':<6}{'지표':<14}{'한주 기존':>16}{'1M 재계산':>18}{'배율':>10}")
    print("-" * 70)

    for mode, csv in [("V1", "v1_trades.csv"), ("V2", "v2_trades.csv"),
                      ("V3", "v3_trades.csv"), ("V4", "v4_trades.csv")]:
        path = src / csv
        if not path.exists():
            continue
        df_old = pd.read_csv(path, dtype={"symbol": str})
        df_new = df_old.copy()
        df_new["qty"] = (args.amount // df_new["entry_price"]).astype(int)
        df_new["pnl_per_share"] = df_new["exit_price"] - df_new["entry_price"]
        df_new["pnl_krw"] = df_new["pnl_per_share"] * df_new["qty"]

        old = _stats(df_old)
        new = _stats(df_new)

        # 한 주 PnL 그대로 (df_old)
        def fmt(x):
            return f"{x:+,.0f}" if isinstance(x, (int, float)) else str(x)

        for label, key in [("거래수", "n"), ("승률 %", "win_pct"),
                           ("합산 PnL", "pnl"), ("평균 PnL", "mean_pnl"),
                           ("MDD", "mdd")]:
            ov = old[key]
            nv = new[key]
            if key == "win_pct":
                # 승률은 같음 (qty 곱셈은 +/- 비례 X 영향)
                ov_s = f"{ov:.2f}%"
                nv_s = f"{nv:.2f}%"
                ratio = "—"
            else:
                ov_s = f"{ov:+,}"
                nv_s = f"{nv:+,}"
                ratio = f"{nv/ov:.1f}x" if ov else "—"
            print(f"{mode:<6}{label:<14}{ov_s:>16}{nv_s:>18}{ratio:>10}")
            out_lines.append(f"| {mode} | {label} | {ov_s} | {nv_s} | {ratio} |")

        # 종목당 avg entry_price 도 (참고)
        avg_entry = int(df_new["entry_price"].mean())
        avg_qty = int(df_new["qty"].mean())
        out_lines.append(
            f"| {mode} | avg entry / 평균 qty | — | "
            f"{avg_entry:,}원 / {avg_qty}주 | — |"
        )
        print(f"      avg entry: {avg_entry:,}원, 평균 qty: {avg_qty}주\n")

        # 새 CSV 도 저장
        new_path = src / csv.replace(".csv", f"_amount{args.amount}.csv")
        df_new.to_csv(new_path, index=False)

    out_path = src / f"repnl_amount{args.amount}.md"
    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"\n[saved] → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
